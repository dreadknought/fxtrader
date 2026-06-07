# file: src/backtest_multi_trade.py
from __future__ import annotations

import argparse
import csv
import math
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from dateutil import parser

from src.oanda.candle_cache import CandleCache, CandleCacheKey
from src.oanda.oanda_client import OandaClient, load_oanda_config
from src.strategy.london_day_classifier import (
    MarketClosedOrNoDataError,
    build_session_windows_for_date,
)

NY_TZ = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")
PIP_VALUE = 0.0001

SignalSide = Literal["HIGH", "LOW"]
Direction = Literal["LONG", "SHORT"]
ExitReason = Literal["TP", "SL", "TIME_STOP", "END_OF_DATA"]


@dataclass(frozen=True)
class PriceCandle:
    timestamp_utc: datetime
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    ask_open: float
    ask_high: float
    ask_low: float
    ask_close: float
    mid_open: float
    mid_high: float
    mid_low: float
    mid_close: float

    @property
    def timestamp_ny(self) -> datetime:
        return self.timestamp_utc.astimezone(NY_TZ)


@dataclass(frozen=True)
class AsiaRange:
    asia_high: float
    asia_low: float
    asia_range_pips: float


@dataclass(frozen=True)
class SignalEvent:
    signal_id: str
    date_ny: str
    side: SignalSide
    direction: Direction
    sweep_time_ny: datetime
    reentry_time_ny: datetime
    sweep_depth_pips: float
    asia_range_pips: float


@dataclass(frozen=True)
class SimulatedTrade:
    signal: SignalEvent
    entry_time_ny: datetime
    exit_time_ny: datetime
    direction: Direction
    entry_price: float
    stop_price: float
    tp_price: float
    exit_price: float
    exit_reason: ExitReason
    pnl_pips: float
    r_multiple: float
    overlapped_with_existing_trade: bool


@dataclass(frozen=True)
class DaySummary:
    date_ny: str
    num_signals: int
    num_trades: int
    num_long: int
    num_short: int
    num_overlaps: int
    total_pnl_pips: float
    total_r: float
    won: int
    lost: int
    net_positive_day: int
    first_entry_ny: str
    last_exit_ny: str


@dataclass(frozen=True)
class BacktestSummary:
    instrument: str
    trading_days_requested: int
    trading_days_processed: int
    trading_days_with_data: int
    total_signals: int
    total_trades: int
    trades_per_day: float
    win_rate: float
    expectancy_r: float
    median_r: float
    positive_day_rate: float
    max_drawdown_r: float
    max_consecutive_losers: int
    overlap_trade_rate: float
    total_pnl_pips: float
    total_r: float
    api_calls: int
    cache_hits: int
    cache_misses: int


def _parse_bool(value: str) -> bool:
    s = (value or "").strip().lower()
    if s in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean value: {value!r}")


def _fmt_dt(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    return dt.astimezone(NY_TZ).isoformat()


def _quantile(sorted_vals: Sequence[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    if q <= 0:
        return sorted_vals[0]
    if q >= 1:
        return sorted_vals[-1]
    idx = int(q * (len(sorted_vals) - 1))
    return sorted_vals[idx]


def _safe_mean(vals: Iterable[float]) -> float:
    vals = [v for v in vals if not math.isnan(v)]
    if not vals:
        return float("nan")
    return sum(vals) / len(vals)


def _to_rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC_TZ).isoformat()


def _is_weekend_in_new_york(dt_ny: datetime) -> bool:
    return dt_ny.weekday() >= 5


def _iter_trading_dates_backwards(
    end_date_ny: datetime, trading_days: int
) -> List[datetime]:
    dates: List[datetime] = []
    cursor = end_date_ny.astimezone(NY_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    while len(dates) < trading_days:
        if not _is_weekend_in_new_york(cursor):
            dates.append(cursor)
        cursor -= timedelta(days=1)
    dates.reverse()
    return dates


def _cache_get(
    cache: CandleCache, *, key: CandleCacheKey, fetch_fn
) -> Tuple[Dict[str, Any], bool]:
    result = cache.get_or_fetch(key=key, fetch_fn=fetch_fn)
    if isinstance(result, tuple):
        if len(result) >= 2:
            return result[0], bool(result[1])
        return result[0], False
    return result, False


def _parse_price_candles_from_bid_ask(
    bid_payload: Dict[str, Any], ask_payload: Dict[str, Any]
) -> List[PriceCandle]:
    bid_rows = {
        c["time"]: c for c in bid_payload.get("candles", []) if c.get("complete", True)
    }
    ask_rows = {
        c["time"]: c for c in ask_payload.get("candles", []) if c.get("complete", True)
    }

    candles: List[PriceCandle] = []
    for ts in sorted(set(bid_rows.keys()) & set(ask_rows.keys())):
        b = bid_rows[ts]["bid"]
        a = ask_rows[ts]["ask"]
        bid_open = float(b["o"])
        bid_high = float(b["h"])
        bid_low = float(b["l"])
        bid_close = float(b["c"])
        ask_open = float(a["o"])
        ask_high = float(a["h"])
        ask_low = float(a["l"])
        ask_close = float(a["c"])
        candles.append(
            PriceCandle(
                timestamp_utc=parser.isoparse(ts).astimezone(UTC_TZ),
                bid_open=bid_open,
                bid_high=bid_high,
                bid_low=bid_low,
                bid_close=bid_close,
                ask_open=ask_open,
                ask_high=ask_high,
                ask_low=ask_low,
                ask_close=ask_close,
                mid_open=(bid_open + ask_open) / 2.0,
                mid_high=(bid_high + ask_high) / 2.0,
                mid_low=(bid_low + ask_low) / 2.0,
                mid_close=(bid_close + ask_close) / 2.0,
            )
        )
    return candles


def _compute_asia_range(
    candles: Sequence[PriceCandle],
    *,
    asia_window_start_ny: datetime,
    asia_window_end_ny: datetime,
) -> AsiaRange:
    asia_high: Optional[float] = None
    asia_low: Optional[float] = None

    for candle in candles:
        t_ny = candle.timestamp_ny
        if not (asia_window_start_ny <= t_ny < asia_window_end_ny):
            continue
        asia_high = (
            candle.mid_high if asia_high is None else max(asia_high, candle.mid_high)
        )
        asia_low = candle.mid_low if asia_low is None else min(asia_low, candle.mid_low)

    if asia_high is None or asia_low is None:
        raise MarketClosedOrNoDataError("No candles found in Asia window.")

    return AsiaRange(
        asia_high=asia_high,
        asia_low=asia_low,
        asia_range_pips=(asia_high - asia_low) / PIP_VALUE,
    )


@dataclass
class _PendingSweep:
    first_sweep_time_ny: datetime
    max_penetration_pips: float


def _scan_signal_events(
    *,
    candles: Sequence[PriceCandle],
    trade_date_ny: datetime,
    asia_range: AsiaRange,
    london_window_start_ny: datetime,
    london_window_end_ny: datetime,
    sweep_buffer_pips: float,
    reentry_deadline_minutes: int,
    gate_max_sweep_depth_pips: Optional[float],
    gate_min_asia_range_pips: Optional[float],
) -> List[SignalEvent]:
    upper = asia_range.asia_high + (sweep_buffer_pips * PIP_VALUE)
    lower = asia_range.asia_low - (sweep_buffer_pips * PIP_VALUE)

    pending_high: Optional[_PendingSweep] = None
    pending_low: Optional[_PendingSweep] = None
    signals: List[SignalEvent] = []
    signal_counter = 0

    scan_end_ny = london_window_end_ny + timedelta(minutes=reentry_deadline_minutes)

    for candle in candles:
        t_ny = candle.timestamp_ny
        if t_ny < london_window_start_ny:
            continue
        if t_ny > scan_end_ny:
            break

        if (
            pending_high is not None
            and t_ny
            > pending_high.first_sweep_time_ny
            + timedelta(minutes=reentry_deadline_minutes)
        ):
            pending_high = None
        if (
            pending_low is not None
            and t_ny
            > pending_low.first_sweep_time_ny
            + timedelta(minutes=reentry_deadline_minutes)
        ):
            pending_low = None

        if london_window_start_ny <= t_ny < london_window_end_ny:
            if candle.mid_high > upper:
                penetration_pips = (candle.mid_high - upper) / PIP_VALUE
                if pending_high is None:
                    pending_high = _PendingSweep(
                        first_sweep_time_ny=t_ny, max_penetration_pips=penetration_pips
                    )
                else:
                    pending_high.max_penetration_pips = max(
                        pending_high.max_penetration_pips, penetration_pips
                    )

            if candle.mid_low < lower:
                penetration_pips = (lower - candle.mid_low) / PIP_VALUE
                if pending_low is None:
                    pending_low = _PendingSweep(
                        first_sweep_time_ny=t_ny, max_penetration_pips=penetration_pips
                    )
                else:
                    pending_low.max_penetration_pips = max(
                        pending_low.max_penetration_pips, penetration_pips
                    )

        if pending_high is not None:
            pending_high.max_penetration_pips = max(
                pending_high.max_penetration_pips,
                max(0.0, (candle.mid_high - upper) / PIP_VALUE),
            )
            if candle.mid_low <= asia_range.asia_high:
                depth_ok = (
                    gate_max_sweep_depth_pips is None
                    or pending_high.max_penetration_pips <= gate_max_sweep_depth_pips
                )
                asia_ok = (
                    gate_min_asia_range_pips is None
                    or asia_range.asia_range_pips >= gate_min_asia_range_pips
                )
                if depth_ok and asia_ok:
                    signal_counter += 1
                    signals.append(
                        SignalEvent(
                            signal_id=f"{trade_date_ny.date()}-H-{signal_counter}",
                            date_ny=str(trade_date_ny.date()),
                            side="HIGH",
                            direction="SHORT",
                            sweep_time_ny=pending_high.first_sweep_time_ny,
                            reentry_time_ny=t_ny,
                            sweep_depth_pips=pending_high.max_penetration_pips,
                            asia_range_pips=asia_range.asia_range_pips,
                        )
                    )
                pending_high = None

        if pending_low is not None:
            pending_low.max_penetration_pips = max(
                pending_low.max_penetration_pips,
                max(0.0, (lower - candle.mid_low) / PIP_VALUE),
            )
            if candle.mid_high >= asia_range.asia_low:
                depth_ok = (
                    gate_max_sweep_depth_pips is None
                    or pending_low.max_penetration_pips <= gate_max_sweep_depth_pips
                )
                asia_ok = (
                    gate_min_asia_range_pips is None
                    or asia_range.asia_range_pips >= gate_min_asia_range_pips
                )
                if depth_ok and asia_ok:
                    signal_counter += 1
                    signals.append(
                        SignalEvent(
                            signal_id=f"{trade_date_ny.date()}-L-{signal_counter}",
                            date_ny=str(trade_date_ny.date()),
                            side="LOW",
                            direction="LONG",
                            sweep_time_ny=pending_low.first_sweep_time_ny,
                            reentry_time_ny=t_ny,
                            sweep_depth_pips=pending_low.max_penetration_pips,
                            asia_range_pips=asia_range.asia_range_pips,
                        )
                    )
                pending_low = None

    return signals


def _find_entry_candle(
    candles: Sequence[PriceCandle], entry_time_ny: datetime
) -> Optional[int]:
    for idx, candle in enumerate(candles):
        if candle.timestamp_ny == entry_time_ny:
            return idx
    return None


def _simulate_single_trade(
    *,
    candles: Sequence[PriceCandle],
    signal: SignalEvent,
    stop_pips: float,
    tp_pips: float,
    time_stop_minutes: int,
    overlapped_with_existing_trade: bool,
) -> Optional[SimulatedTrade]:
    entry_idx = _find_entry_candle(candles, signal.reentry_time_ny)
    if entry_idx is None:
        return None

    entry_candle = candles[entry_idx]
    entry_price = (
        entry_candle.ask_close if signal.direction == "LONG" else entry_candle.bid_close
    )

    if signal.direction == "LONG":
        stop_price = entry_price - (stop_pips * PIP_VALUE)
        tp_price = entry_price + (tp_pips * PIP_VALUE)
    else:
        stop_price = entry_price + (stop_pips * PIP_VALUE)
        tp_price = entry_price - (tp_pips * PIP_VALUE)

    deadline_ny = signal.reentry_time_ny + timedelta(minutes=time_stop_minutes)

    for candle in candles[entry_idx + 1 :]:
        t_ny = candle.timestamp_ny

        if signal.direction == "LONG":
            stop_hit = candle.bid_low <= stop_price
            tp_hit = candle.bid_high >= tp_price
            if stop_hit and tp_hit:
                exit_price = stop_price
                pnl_pips = (exit_price - entry_price) / PIP_VALUE
                return SimulatedTrade(
                    signal=signal,
                    entry_time_ny=signal.reentry_time_ny,
                    exit_time_ny=t_ny,
                    direction=signal.direction,
                    entry_price=entry_price,
                    stop_price=stop_price,
                    tp_price=tp_price,
                    exit_price=exit_price,
                    exit_reason="SL",
                    pnl_pips=pnl_pips,
                    r_multiple=pnl_pips / stop_pips,
                    overlapped_with_existing_trade=overlapped_with_existing_trade,
                )
            if stop_hit:
                exit_price = stop_price
                pnl_pips = (exit_price - entry_price) / PIP_VALUE
                return SimulatedTrade(
                    signal,
                    signal.reentry_time_ny,
                    t_ny,
                    signal.direction,
                    entry_price,
                    stop_price,
                    tp_price,
                    exit_price,
                    "SL",
                    pnl_pips,
                    pnl_pips / stop_pips,
                    overlapped_with_existing_trade,
                )
            if tp_hit:
                exit_price = tp_price
                pnl_pips = (exit_price - entry_price) / PIP_VALUE
                return SimulatedTrade(
                    signal,
                    signal.reentry_time_ny,
                    t_ny,
                    signal.direction,
                    entry_price,
                    stop_price,
                    tp_price,
                    exit_price,
                    "TP",
                    pnl_pips,
                    pnl_pips / stop_pips,
                    overlapped_with_existing_trade,
                )
            if t_ny >= deadline_ny:
                exit_price = candle.bid_close
                pnl_pips = (exit_price - entry_price) / PIP_VALUE
                return SimulatedTrade(
                    signal,
                    signal.reentry_time_ny,
                    t_ny,
                    signal.direction,
                    entry_price,
                    stop_price,
                    tp_price,
                    exit_price,
                    "TIME_STOP",
                    pnl_pips,
                    pnl_pips / stop_pips,
                    overlapped_with_existing_trade,
                )
        else:
            stop_hit = candle.ask_high >= stop_price
            tp_hit = candle.ask_low <= tp_price
            if stop_hit and tp_hit:
                exit_price = stop_price
                pnl_pips = (entry_price - exit_price) / PIP_VALUE
                return SimulatedTrade(
                    signal,
                    signal.reentry_time_ny,
                    t_ny,
                    signal.direction,
                    entry_price,
                    stop_price,
                    tp_price,
                    exit_price,
                    "SL",
                    pnl_pips,
                    pnl_pips / stop_pips,
                    overlapped_with_existing_trade,
                )
            if stop_hit:
                exit_price = stop_price
                pnl_pips = (entry_price - exit_price) / PIP_VALUE
                return SimulatedTrade(
                    signal,
                    signal.reentry_time_ny,
                    t_ny,
                    signal.direction,
                    entry_price,
                    stop_price,
                    tp_price,
                    exit_price,
                    "SL",
                    pnl_pips,
                    pnl_pips / stop_pips,
                    overlapped_with_existing_trade,
                )
            if tp_hit:
                exit_price = tp_price
                pnl_pips = (entry_price - exit_price) / PIP_VALUE
                return SimulatedTrade(
                    signal,
                    signal.reentry_time_ny,
                    t_ny,
                    signal.direction,
                    entry_price,
                    stop_price,
                    tp_price,
                    exit_price,
                    "TP",
                    pnl_pips,
                    pnl_pips / stop_pips,
                    overlapped_with_existing_trade,
                )
            if t_ny >= deadline_ny:
                exit_price = candle.ask_close
                pnl_pips = (entry_price - exit_price) / PIP_VALUE
                return SimulatedTrade(
                    signal,
                    signal.reentry_time_ny,
                    t_ny,
                    signal.direction,
                    entry_price,
                    stop_price,
                    tp_price,
                    exit_price,
                    "TIME_STOP",
                    pnl_pips,
                    pnl_pips / stop_pips,
                    overlapped_with_existing_trade,
                )

    last = candles[-1]
    if signal.direction == "LONG":
        exit_price = last.bid_close
        pnl_pips = (exit_price - entry_price) / PIP_VALUE
    else:
        exit_price = last.ask_close
        pnl_pips = (entry_price - exit_price) / PIP_VALUE
    return SimulatedTrade(
        signal,
        signal.reentry_time_ny,
        last.timestamp_ny,
        signal.direction,
        entry_price,
        stop_price,
        tp_price,
        exit_price,
        "END_OF_DATA",
        pnl_pips,
        pnl_pips / stop_pips,
        overlapped_with_existing_trade,
    )


def _summarize_trades(
    *,
    instrument: str,
    trading_days_requested: int,
    trading_days_processed: int,
    trading_days_with_data: int,
    total_signals: int,
    trades: Sequence[SimulatedTrade],
    day_summaries: Sequence[DaySummary],
    api_calls: int,
    cache_hits: int,
    cache_misses: int,
) -> BacktestSummary:
    r_vals = [t.r_multiple for t in trades]
    r_sorted = sorted(r_vals)
    win_rate = (
        (sum(1 for t in trades if t.pnl_pips > 0) / len(trades))
        if trades
        else float("nan")
    )
    expectancy_r = _safe_mean(r_vals)
    median_r = _quantile(r_sorted, 0.5) if r_sorted else float("nan")
    positive_day_rate = (
        sum(1 for d in day_summaries if d.net_positive_day) / len(day_summaries)
        if day_summaries
        else float("nan")
    )

    cumulative_r = 0.0
    peak_r = 0.0
    max_drawdown_r = 0.0
    max_consecutive_losers = 0
    current_consecutive_losers = 0
    for trade in trades:
        cumulative_r += trade.r_multiple
        peak_r = max(peak_r, cumulative_r)
        max_drawdown_r = max(max_drawdown_r, peak_r - cumulative_r)
        if trade.r_multiple < 0:
            current_consecutive_losers += 1
            max_consecutive_losers = max(
                max_consecutive_losers, current_consecutive_losers
            )
        else:
            current_consecutive_losers = 0

    return BacktestSummary(
        instrument=instrument,
        trading_days_requested=trading_days_requested,
        trading_days_processed=trading_days_processed,
        trading_days_with_data=trading_days_with_data,
        total_signals=total_signals,
        total_trades=len(trades),
        trades_per_day=(
            (len(trades) / trading_days_with_data)
            if trading_days_with_data
            else float("nan")
        ),
        win_rate=win_rate,
        expectancy_r=expectancy_r,
        median_r=median_r,
        positive_day_rate=positive_day_rate,
        max_drawdown_r=max_drawdown_r,
        max_consecutive_losers=max_consecutive_losers,
        overlap_trade_rate=(
            (sum(1 for t in trades if t.overlapped_with_existing_trade) / len(trades))
            if trades
            else 0.0
        ),
        total_pnl_pips=sum(t.pnl_pips for t in trades),
        total_r=sum(t.r_multiple for t in trades),
        api_calls=api_calls,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
    )


def run_multi_trade_backtest(
    *,
    instrument: str,
    trading_days: int,
    granularity: str,
    sweep_buffer_pips: float,
    reentry_deadline_minutes: int,
    gate_max_sweep_depth_pips: Optional[float],
    gate_min_asia_range_pips: Optional[float],
    stop_pips: float,
    tp_pips: float,
    time_stop_minutes: int,
    max_trades_per_day: int,
    cooldown_minutes: int,
    allow_same_side_repeat: bool,
    opposite_side_only_after_close: bool,
    allow_overlapping_trades: bool,
    cache_dir: Optional[Path],
    progress_every: int = 25,
    sleep_seconds_between_requests: float = 0.05,
) -> Tuple[List[Dict[str, str]], List[Dict[str, str]], BacktestSummary]:
    config = load_oanda_config()
    client = OandaClient(config)
    cache = CandleCache(cache_dir) if cache_dir else None
    oanda_env = os.environ.get("OANDA_ENV", "practice").strip().lower()

    trade_dates_ny = _iter_trading_dates_backwards(datetime.now(tz=NY_TZ), trading_days)
    api_calls = 0
    cache_hits = 0
    cache_misses = 0
    total_signals = 0
    trading_days_with_data = 0
    trade_rows: List[Dict[str, str]] = []
    day_rows: List[Dict[str, str]] = []
    all_trades: List[SimulatedTrade] = []

    started = time.monotonic()

    for idx, trade_date_ny in enumerate(trade_dates_ny, start=1):
        (
            asia_window_start_ny,
            _asia_window_end_ny,
            london_window_start_ny,
            london_window_end_ny,
            double_sweep_check_end_ny,
        ) = build_session_windows_for_date(trade_date_ny)

        fetch_start = _to_rfc3339(asia_window_start_ny)
        fetch_end = _to_rfc3339(double_sweep_check_end_ny)
        trade_date_str = str(trade_date_ny.date())

        def _fetch(price: str) -> Dict[str, Any]:
            return client.get_candles(
                instrument=instrument,
                granularity=granularity,
                time_from_rfc3339=fetch_start,
                time_to_rfc3339=fetch_end,
                price=price,
            )

        if cache:
            bid_key = CandleCacheKey(
                oanda_env,
                instrument,
                granularity,
                trade_date_str,
                "B",
                fetch_start,
                fetch_end,
            )
            ask_key = CandleCacheKey(
                oanda_env,
                instrument,
                granularity,
                trade_date_str,
                "A",
                fetch_start,
                fetch_end,
            )
            bid_payload, bid_hit = _cache_get(
                cache, key=bid_key, fetch_fn=lambda: _fetch("B")
            )
            ask_payload, ask_hit = _cache_get(
                cache, key=ask_key, fetch_fn=lambda: _fetch("A")
            )
            cache_hits += int(bid_hit) + int(ask_hit)
            cache_misses += int(not bid_hit) + int(not ask_hit)
            api_calls += int(not bid_hit) + int(not ask_hit)
        else:
            bid_payload = _fetch("B")
            ask_payload = _fetch("A")
            api_calls += 2

        candles = _parse_price_candles_from_bid_ask(bid_payload, ask_payload)

        try:
            asia_range = _compute_asia_range(
                candles,
                asia_window_start_ny=asia_window_start_ny,
                asia_window_end_ny=london_window_start_ny,
            )
        except MarketClosedOrNoDataError:
            day_rows.append(
                {
                    "date_ny": trade_date_str,
                    "num_signals": "0",
                    "num_trades": "0",
                    "num_long": "0",
                    "num_short": "0",
                    "num_overlaps": "0",
                    "total_pnl_pips": "0.000",
                    "total_r": "0.000",
                    "won": "0",
                    "lost": "0",
                    "net_positive_day": "0",
                    "first_entry_ny": "",
                    "last_exit_ny": "",
                }
            )
            continue

        trading_days_with_data += 1

        signals = _scan_signal_events(
            candles=candles,
            trade_date_ny=trade_date_ny,
            asia_range=asia_range,
            london_window_start_ny=london_window_start_ny,
            london_window_end_ny=london_window_end_ny,
            sweep_buffer_pips=sweep_buffer_pips,
            reentry_deadline_minutes=reentry_deadline_minutes,
            gate_max_sweep_depth_pips=gate_max_sweep_depth_pips,
            gate_min_asia_range_pips=gate_min_asia_range_pips,
        )
        total_signals += len(signals)

        open_trades: List[SimulatedTrade] = []
        closed_trades: List[SimulatedTrade] = []
        opened_sides: List[SignalSide] = []
        last_closed_side: Optional[SignalSide] = None
        cooldown_until: Optional[datetime] = None

        for signal in signals:
            just_closed: List[SimulatedTrade] = [
                t for t in open_trades if t.exit_time_ny <= signal.reentry_time_ny
            ]
            if just_closed:
                just_closed.sort(key=lambda t: t.exit_time_ny)
                for closed in just_closed:
                    last_closed_side = closed.signal.side
                    if not allow_overlapping_trades:
                        cooldown_until = closed.exit_time_ny + timedelta(
                            minutes=cooldown_minutes
                        )
                closed_trades.extend(just_closed)
                open_trades = [
                    t for t in open_trades if t.exit_time_ny > signal.reentry_time_ny
                ]

            if len(opened_sides) >= max_trades_per_day:
                continue
            if not allow_overlapping_trades and open_trades:
                continue
            if cooldown_until is not None and signal.reentry_time_ny < cooldown_until:
                continue
            if (
                opposite_side_only_after_close
                and last_closed_side is not None
                and signal.side == last_closed_side
            ):
                continue
            if not allow_same_side_repeat and signal.side in opened_sides:
                continue

            overlapped = bool(open_trades)
            trade = _simulate_single_trade(
                candles=candles,
                signal=signal,
                stop_pips=stop_pips,
                tp_pips=tp_pips,
                time_stop_minutes=time_stop_minutes,
                overlapped_with_existing_trade=overlapped,
            )
            if trade is None:
                continue

            open_trades.append(trade)
            opened_sides.append(signal.side)

        if open_trades:
            closed_trades.extend(sorted(open_trades, key=lambda t: t.exit_time_ny))

        closed_trades.sort(
            key=lambda t: (t.entry_time_ny, t.exit_time_ny, t.signal.signal_id)
        )
        all_trades.extend(closed_trades)

        total_pnl_pips = sum(t.pnl_pips for t in closed_trades)
        total_r = sum(t.r_multiple for t in closed_trades)
        num_overlaps = sum(1 for t in closed_trades if t.overlapped_with_existing_trade)

        day_summary = DaySummary(
            date_ny=trade_date_str,
            num_signals=len(signals),
            num_trades=len(closed_trades),
            num_long=sum(1 for t in closed_trades if t.direction == "LONG"),
            num_short=sum(1 for t in closed_trades if t.direction == "SHORT"),
            num_overlaps=num_overlaps,
            total_pnl_pips=total_pnl_pips,
            total_r=total_r,
            won=sum(1 for t in closed_trades if t.pnl_pips > 0),
            lost=sum(1 for t in closed_trades if t.pnl_pips < 0),
            net_positive_day=int(total_pnl_pips > 0),
            first_entry_ny=_fmt_dt(
                closed_trades[0].entry_time_ny if closed_trades else None
            ),
            last_exit_ny=_fmt_dt(
                closed_trades[-1].exit_time_ny if closed_trades else None
            ),
        )
        day_rows.append(
            {
                "date_ny": day_summary.date_ny,
                "num_signals": str(day_summary.num_signals),
                "num_trades": str(day_summary.num_trades),
                "num_long": str(day_summary.num_long),
                "num_short": str(day_summary.num_short),
                "num_overlaps": str(day_summary.num_overlaps),
                "total_pnl_pips": f"{day_summary.total_pnl_pips:.3f}",
                "total_r": f"{day_summary.total_r:.3f}",
                "won": str(day_summary.won),
                "lost": str(day_summary.lost),
                "net_positive_day": str(day_summary.net_positive_day),
                "first_entry_ny": day_summary.first_entry_ny,
                "last_exit_ny": day_summary.last_exit_ny,
            }
        )

        for trade_num_in_day, trade in enumerate(closed_trades, start=1):
            trade_rows.append(
                {
                    "date_ny": trade.signal.date_ny,
                    "trade_num_in_day": str(trade_num_in_day),
                    "signal_id": trade.signal.signal_id,
                    "direction": trade.direction,
                    "sweep_side": trade.signal.side,
                    "sweep_time_ny": _fmt_dt(trade.signal.sweep_time_ny),
                    "reentry_time_ny": _fmt_dt(trade.signal.reentry_time_ny),
                    "entry_time_ny": _fmt_dt(trade.entry_time_ny),
                    "exit_time_ny": _fmt_dt(trade.exit_time_ny),
                    "entry_price": f"{trade.entry_price:.5f}",
                    "stop_price": f"{trade.stop_price:.5f}",
                    "tp_price": f"{trade.tp_price:.5f}",
                    "exit_price": f"{trade.exit_price:.5f}",
                    "exit_reason": trade.exit_reason,
                    "pnl_pips": f"{trade.pnl_pips:.3f}",
                    "r_multiple": f"{trade.r_multiple:.3f}",
                    "overlapped_with_existing_trade": (
                        "1" if trade.overlapped_with_existing_trade else "0"
                    ),
                    "asia_range_pips": f"{trade.signal.asia_range_pips:.3f}",
                    "sweep_depth_pips": f"{trade.signal.sweep_depth_pips:.3f}",
                    "buffer_pips": f"{sweep_buffer_pips:g}",
                    "reentry_deadline_minutes": str(reentry_deadline_minutes),
                }
            )

        if progress_every > 0 and (
            (idx % progress_every == 0) or idx == len(trade_dates_ny)
        ):
            elapsed = time.monotonic() - started
            avg = elapsed / max(1, idx)
            remain = avg * (len(trade_dates_ny) - idx)
            print(
                f"[{idx}/{len(trade_dates_ny)}] {trade_date_str} signals={len(signals)} trades={len(closed_trades)} "
                f"api_calls={api_calls} cache_hits={cache_hits} eta={int(remain)}s",
                flush=True,
            )

        if sleep_seconds_between_requests > 0 and api_calls > 0:
            time.sleep(sleep_seconds_between_requests)

    summary = _summarize_trades(
        instrument=instrument,
        trading_days_requested=trading_days,
        trading_days_processed=len(trade_dates_ny),
        trading_days_with_data=trading_days_with_data,
        total_signals=total_signals,
        trades=all_trades,
        day_summaries=[
            DaySummary(
                date_ny=row["date_ny"],
                num_signals=int(row["num_signals"]),
                num_trades=int(row["num_trades"]),
                num_long=int(row["num_long"]),
                num_short=int(row["num_short"]),
                num_overlaps=int(row["num_overlaps"]),
                total_pnl_pips=float(row["total_pnl_pips"]),
                total_r=float(row["total_r"]),
                won=int(row["won"]),
                lost=int(row["lost"]),
                net_positive_day=int(row["net_positive_day"]),
                first_entry_ny=row["first_entry_ny"],
                last_exit_ny=row["last_exit_ny"],
            )
            for row in day_rows
        ],
        api_calls=api_calls,
        cache_hits=cache_hits,
        cache_misses=cache_misses,
    )
    return trade_rows, day_rows, summary


def _write_csv(rows: Sequence[Dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _summary_row(summary: BacktestSummary) -> Dict[str, str]:
    return {
        "instrument": summary.instrument,
        "trading_days_requested": str(summary.trading_days_requested),
        "trading_days_processed": str(summary.trading_days_processed),
        "trading_days_with_data": str(summary.trading_days_with_data),
        "total_signals": str(summary.total_signals),
        "total_trades": str(summary.total_trades),
        "trades_per_day": f"{summary.trades_per_day:.6f}",
        "win_rate": f"{summary.win_rate:.6f}",
        "expectancy_r": f"{summary.expectancy_r:.6f}",
        "median_r": f"{summary.median_r:.6f}",
        "positive_day_rate": f"{summary.positive_day_rate:.6f}",
        "max_drawdown_r": f"{summary.max_drawdown_r:.6f}",
        "max_consecutive_losers": str(summary.max_consecutive_losers),
        "overlap_trade_rate": f"{summary.overlap_trade_rate:.6f}",
        "total_pnl_pips": f"{summary.total_pnl_pips:.6f}",
        "total_r": f"{summary.total_r:.6f}",
        "api_calls": str(summary.api_calls),
        "cache_hits": str(summary.cache_hits),
        "cache_misses": str(summary.cache_misses),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", default="EUR_USD")
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--granularity", default="M1")
    ap.add_argument("--cache-dir", default="out/candle_cache")
    ap.add_argument("--out-dir", default="out/multi_trade")
    ap.add_argument("--out-trades", default="")
    ap.add_argument("--out-days", default="")
    ap.add_argument("--out-summary", default="")
    ap.add_argument("--buffer-pips", type=float, default=1.0)
    ap.add_argument("--reentry-minutes", type=int, default=10)
    ap.add_argument("--gate-max-sweep-depth-pips", type=float, default=8.0)
    ap.add_argument("--gate-min-asia-range-pips", type=float, default=22.5)
    ap.add_argument("--stop-pips", type=float, default=10.0)
    ap.add_argument("--tp-pips", type=float, default=10.0)
    ap.add_argument("--time-stop-minutes", type=int, default=90)
    ap.add_argument("--max-trades-per-day", type=int, default=1)
    ap.add_argument("--cooldown-minutes", type=int, default=0)
    ap.add_argument("--allow-same-side-repeat", type=_parse_bool, default=False)
    ap.add_argument("--opposite-side-only-after-close", type=_parse_bool, default=False)
    ap.add_argument("--allow-overlapping-trades", type=_parse_bool, default=False)
    ap.add_argument("--progress-every", type=int, default=25)
    ap.add_argument("--sleep", type=float, default=0.05)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    trades_path = Path(args.out_trades) if args.out_trades else out_dir / "trades.csv"
    days_path = Path(args.out_days) if args.out_days else out_dir / "days.csv"
    summary_path = (
        Path(args.out_summary) if args.out_summary else out_dir / "summary.csv"
    )
    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    trade_rows, day_rows, summary = run_multi_trade_backtest(
        instrument=args.instrument,
        trading_days=args.days,
        granularity=args.granularity,
        sweep_buffer_pips=args.buffer_pips,
        reentry_deadline_minutes=args.reentry_minutes,
        gate_max_sweep_depth_pips=args.gate_max_sweep_depth_pips,
        gate_min_asia_range_pips=args.gate_min_asia_range_pips,
        stop_pips=args.stop_pips,
        tp_pips=args.tp_pips,
        time_stop_minutes=args.time_stop_minutes,
        max_trades_per_day=args.max_trades_per_day,
        cooldown_minutes=args.cooldown_minutes,
        allow_same_side_repeat=args.allow_same_side_repeat,
        opposite_side_only_after_close=args.opposite_side_only_after_close,
        allow_overlapping_trades=args.allow_overlapping_trades,
        cache_dir=cache_dir,
        progress_every=args.progress_every,
        sleep_seconds_between_requests=args.sleep,
    )

    _write_csv(trade_rows, trades_path)
    _write_csv(day_rows, days_path)
    _write_csv([_summary_row(summary)], summary_path)

    print(f"Wrote trades: {trades_path}")
    print(f"Wrote days: {days_path}")
    print(f"Wrote summary: {summary_path}")
    print(
        f"trades={summary.total_trades} expectancy_R={summary.expectancy_r:.4f} "
        f"median_R={summary.median_r:.4f} positive_day_rate={summary.positive_day_rate:.4f} "
        f"max_dd_R={summary.max_drawdown_r:.4f} overlap_rate={summary.overlap_trade_rate:.4f}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
