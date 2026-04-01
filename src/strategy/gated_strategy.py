# file: src/strategy/gated_strategy.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple
from zoneinfo import ZoneInfo

from src.strategy.london_day_classifier import (
    AsiaRange,
    Candle,
    SweepSide,
    build_session_windows_for_date,
)

NY = ZoneInfo("America/New_York")
PIP_VALUE = 0.0001  # EUR_USD


@dataclass(frozen=True)
class GateConfig:
    # Your validated defaults
    max_sweep_depth_pips: float = 8.0
    min_asia_range_pips: float = 22.5
    sweep_buffer_pips: float = 1.0
    reentry_deadline_minutes: int = 10


@dataclass(frozen=True)
class LiveState:
    # Asia
    asia_high: Optional[float] = None
    asia_low: Optional[float] = None

    # First sweep
    first_sweep_side: Optional[SweepSide] = None
    first_sweep_time_ny: Optional[datetime] = None

    # Sweep depth tracking (beyond boundary ± buffer)
    max_penetration_pips: float = 0.0

    # Reentry
    reentry_time_ny: Optional[datetime] = None


@dataclass(frozen=True)
class Decision:
    should_trade: bool
    reason: str

    asia_range_pips: Optional[float]
    sweep_depth_pips: Optional[float]

    first_sweep_side: Optional[SweepSide]
    first_sweep_time_ny: Optional[datetime]
    reentry_time_ny: Optional[datetime]


def _now_ny_from_utc(ts_utc: datetime) -> datetime:
    return ts_utc.astimezone(NY)


def update_state_from_tick(
    *,
    state: LiveState,
    tick_time_utc: datetime,
    mid_price: float,
    trade_date_ny: datetime,
    gate: GateConfig,
) -> LiveState:
    """
    Online update:
      - During Asia window: update asia_high/asia_low from ticks
      - During London decision window: detect first sweep and update max penetration depth
      - Detect reentry (price back inside Asia boundary) after a sweep
    """
    (
        asia_start_ny,
        asia_end_ny,
        london_start_ny,
        london_end_ny,
        _double_sweep_end_ny,
    ) = build_session_windows_for_date(trade_date_ny)

    t_ny = _now_ny_from_utc(tick_time_utc)

    asia_high = state.asia_high
    asia_low = state.asia_low

    # 1) Asia window updates
    if asia_start_ny <= t_ny < asia_end_ny:
        asia_high = mid_price if asia_high is None else max(asia_high, mid_price)
        asia_low = mid_price if asia_low is None else min(asia_low, mid_price)

        return LiveState(
            asia_high=asia_high,
            asia_low=asia_low,
            first_sweep_side=state.first_sweep_side,
            first_sweep_time_ny=state.first_sweep_time_ny,
            max_penetration_pips=state.max_penetration_pips,
            reentry_time_ny=state.reentry_time_ny,
        )

    # If we don't have Asia yet, we can't do anything else
    if asia_high is None or asia_low is None:
        return state

    # Boundaries with buffer
    upper = asia_high + gate.sweep_buffer_pips * PIP_VALUE
    lower = asia_low - gate.sweep_buffer_pips * PIP_VALUE

    # 2) London decision window: sweep detection + penetration depth
    if london_start_ny <= t_ny < london_end_ny:
        first_side = state.first_sweep_side
        first_time = state.first_sweep_time_ny
        max_pen = state.max_penetration_pips

        # detect first sweep (like classifier: first time it crosses beyond boundary±buffer)
        if first_side is None:
            if mid_price > upper:
                first_side = "HIGH"
                first_time = t_ny
            elif mid_price < lower:
                first_side = "LOW"
                first_time = t_ny

        # update penetration depth after sweep
        if first_side == "HIGH" and mid_price > upper:
            pen = (mid_price - upper) / PIP_VALUE
            max_pen = max(max_pen, pen)
        elif first_side == "LOW" and mid_price < lower:
            pen = (lower - mid_price) / PIP_VALUE
            max_pen = max(max_pen, pen)

        # reentry detection after sweep (like classifier: crosses back inside Asia boundary)
        reentry_time = state.reentry_time_ny
        if first_side == "HIGH" and reentry_time is None and mid_price <= asia_high:
            reentry_time = t_ny
        elif first_side == "LOW" and reentry_time is None and mid_price >= asia_low:
            reentry_time = t_ny

        return LiveState(
            asia_high=asia_high,
            asia_low=asia_low,
            first_sweep_side=first_side,
            first_sweep_time_ny=first_time,
            max_penetration_pips=max_pen,
            reentry_time_ny=reentry_time,
        )

    return state


def evaluate_decision(
    *,
    state: LiveState,
    gate: GateConfig,
) -> Decision:
    """
    Apply your validated gates when:
      - we have a sweep + reentry (i.e. mean reversion setup in your model)
    """
    if state.asia_high is None or state.asia_low is None:
        return Decision(
            should_trade=False,
            reason="missing_asia",
            asia_range_pips=None,
            sweep_depth_pips=None,
            first_sweep_side=state.first_sweep_side,
            first_sweep_time_ny=state.first_sweep_time_ny,
            reentry_time_ny=state.reentry_time_ny,
        )

    asia_range_pips = (state.asia_high - state.asia_low) / PIP_VALUE

    # require sweep + reentry
    if state.first_sweep_side is None or state.first_sweep_time_ny is None:
        return Decision(
            should_trade=False,
            reason="no_sweep",
            asia_range_pips=asia_range_pips,
            sweep_depth_pips=None,
            first_sweep_side=state.first_sweep_side,
            first_sweep_time_ny=state.first_sweep_time_ny,
            reentry_time_ny=state.reentry_time_ny,
        )

    if state.reentry_time_ny is None:
        return Decision(
            should_trade=False,
            reason="no_reentry_yet",
            asia_range_pips=asia_range_pips,
            sweep_depth_pips=state.max_penetration_pips,
            first_sweep_side=state.first_sweep_side,
            first_sweep_time_ny=state.first_sweep_time_ny,
            reentry_time_ny=None,
        )

    # deadline check
    deadline = state.first_sweep_time_ny.replace()  # copy
    deadline = deadline + __import__("datetime").timedelta(
        minutes=gate.reentry_deadline_minutes
    )
    if state.reentry_time_ny > deadline:
        return Decision(
            should_trade=False,
            reason="reentry_late",
            asia_range_pips=asia_range_pips,
            sweep_depth_pips=state.max_penetration_pips,
            first_sweep_side=state.first_sweep_side,
            first_sweep_time_ny=state.first_sweep_time_ny,
            reentry_time_ny=state.reentry_time_ny,
        )

    # Gate 1: shallow sweep
    if state.max_penetration_pips > gate.max_sweep_depth_pips:
        return Decision(
            should_trade=False,
            reason="deep_sweep",
            asia_range_pips=asia_range_pips,
            sweep_depth_pips=state.max_penetration_pips,
            first_sweep_side=state.first_sweep_side,
            first_sweep_time_ny=state.first_sweep_time_ny,
            reentry_time_ny=state.reentry_time_ny,
        )

    # Gate 2: Asia range minimum
    if asia_range_pips < gate.min_asia_range_pips:
        return Decision(
            should_trade=False,
            reason="small_asia_range",
            asia_range_pips=asia_range_pips,
            sweep_depth_pips=state.max_penetration_pips,
            first_sweep_side=state.first_sweep_side,
            first_sweep_time_ny=state.first_sweep_time_ny,
            reentry_time_ny=state.reentry_time_ny,
        )

    return Decision(
        should_trade=True,
        reason="PASS",
        asia_range_pips=asia_range_pips,
        sweep_depth_pips=state.max_penetration_pips,
        first_sweep_side=state.first_sweep_side,
        first_sweep_time_ny=state.first_sweep_time_ny,
        reentry_time_ny=state.reentry_time_ny,
    )
