# file: src/strategy/london_day_classifier.py
"""
London day classifier for the "Asia range -> London sweep" framework.

We compute:
  - asia_high, asia_low for Asia window (7pm -> 3am NY time)
  - observe "sweep" during London decision window (3am -> 5am NY time)
  - classify into:
      * MEAN_REVERSION
      * TREND
      * DOUBLE_SWEEP
      * RANGE_INSIDE

This module uses VERBOSE snake_case names (per user preference):
  - asia_high / asia_low (instead of AH/AL)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, List, Literal, Optional, Tuple

from dateutil import parser
from zoneinfo import ZoneInfo

# Note: Backtest will also emit MARKET_CLOSED when we have no candles in the Asia window.
DayClass = Literal[
    "MEAN_REVERSION", "TREND", "DOUBLE_SWEEP", "RANGE_INSIDE", "MARKET_CLOSED"
]
SweepSide = Literal["HIGH", "LOW"]


class MarketClosedOrNoDataError(RuntimeError):
    """
    Raised when we cannot compute Asia range due to missing candles.

    This is expected on rare days (e.g., Christmas / New Year market closures)
    or if the requested time range doesn't include the Asia session.
    """


@dataclass(frozen=True)
class Candle:
    """
    Represents one candle using the selected price type (mid/bid/ask).

    We keep only what we need for classification:
      - timestamp
      - high
      - low
    """

    timestamp_utc: datetime
    high: float
    low: float


@dataclass(frozen=True)
class AsiaRange:
    """Computed Asia session range and the time window used."""

    asia_window_start_ny: datetime
    asia_window_end_ny: datetime
    asia_high: float
    asia_low: float

    @property
    def asia_range_pips(self) -> float:
        # For EURUSD-like pairs, pip is 0.0001
        return (self.asia_high - self.asia_low) / 0.0001


@dataclass(frozen=True)
class ClassificationResult:
    """Final label plus useful metadata for logging/backtesting."""

    day_class: DayClass
    asia_high: float
    asia_low: float
    first_sweep_side: Optional[SweepSide]
    first_sweep_time_ny: Optional[datetime]
    reentry_time_ny: Optional[datetime]
    double_sweep: bool


def parse_oanda_candles(oanda_payload: dict, price_key: str = "mid") -> List[Candle]:
    """
    Parse OANDA candles payload into Candle objects.

    price_key is usually:
      - "mid" when price="M"
      - "bid" when price="B"
      - "ask" when price="A"
    """
    candles: List[Candle] = []
    for c in oanda_payload.get("candles", []):
        if not c.get("complete", True):
            # For historical ranges, candles should be complete, but we skip partial just in case.
            continue

        timestamp_utc = parser.isoparse(c["time"]).astimezone(ZoneInfo("UTC"))
        price_obj = c[price_key]
        high = float(price_obj["h"])
        low = float(price_obj["l"])
        candles.append(Candle(timestamp_utc=timestamp_utc, high=high, low=low))

    return candles


def _to_new_york_time(timestamp_utc: datetime) -> datetime:
    return timestamp_utc.astimezone(ZoneInfo("America/New_York"))


def compute_asia_range(
    candles: Iterable[Candle],
    asia_window_start_ny: datetime,
    asia_window_end_ny: datetime,
) -> AsiaRange:
    """
    Compute asia_high and asia_low over the given NY-time window.

    We convert each candle timestamp to NY time and include candles whose
    start time falls within [start, end).
    """
    asia_high: Optional[float] = None
    asia_low: Optional[float] = None

    for candle in candles:
        candle_time_ny = _to_new_york_time(candle.timestamp_utc)
        if not (asia_window_start_ny <= candle_time_ny < asia_window_end_ny):
            continue

        asia_high = candle.high if asia_high is None else max(asia_high, candle.high)
        asia_low = candle.low if asia_low is None else min(asia_low, candle.low)

    if asia_high is None or asia_low is None:
        raise MarketClosedOrNoDataError(
            "No candles found in the Asia window (market closed or no data for that window)."
        )

    return AsiaRange(
        asia_window_start_ny=asia_window_start_ny,
        asia_window_end_ny=asia_window_end_ny,
        asia_high=asia_high,
        asia_low=asia_low,
    )


def classify_london_day(
    candles: List[Candle],
    asia_range: AsiaRange,
    london_window_start_ny: datetime,
    london_window_end_ny: datetime,
    sweep_buffer_pips: float = 2.0,
    reentry_deadline_minutes: int = 20,
    evaluate_double_sweep_until_ny: Optional[datetime] = None,
) -> ClassificationResult:
    """
    Classify a day based on sweep and re-entry behavior.

    Definitions (default EURUSD assumptions):
      - pip = 0.0001
      - sweep_buffer_pips: how far beyond asia_high/asia_low price must trade to count as sweep
      - re-entry: after HIGH sweep, trade back <= asia_high; after LOW sweep, trade back >= asia_low
      - decision window: [london_window_start_ny, london_window_end_ny)

    If evaluate_double_sweep_until_ny is provided, we check for sweeping the *other* side
    up to that time (commonly 8am NY if you want).
    """
    pip_value = 0.0001
    sweep_buffer_price = sweep_buffer_pips * pip_value

    first_sweep_side: Optional[SweepSide] = None
    first_sweep_time_ny: Optional[datetime] = None
    reentry_time_ny: Optional[datetime] = None
    double_sweep = False

    # Track whether either side was ever swept (for double sweep detection)
    swept_high = False
    swept_low = False

    # We will scan candles in time order.
    sorted_candles = sorted(candles, key=lambda c: c.timestamp_utc)

    # --- 1) Find first sweep during the London decision window ---
    for candle in sorted_candles:
        candle_time_ny = _to_new_york_time(candle.timestamp_utc)
        if not (london_window_start_ny <= candle_time_ny < london_window_end_ny):
            continue

        # Check if this candle swept above asia_high.
        if candle.high > asia_range.asia_high + sweep_buffer_price:
            swept_high = True
            if first_sweep_side is None:
                first_sweep_side = "HIGH"
                first_sweep_time_ny = candle_time_ny

        # Check if this candle swept below asia_low.
        if candle.low < asia_range.asia_low - sweep_buffer_price:
            swept_low = True
            if first_sweep_side is None:
                first_sweep_side = "LOW"
                first_sweep_time_ny = candle_time_ny

        if first_sweep_side is not None:
            break

    # If no sweep in the decision window, it's a range/inside day for this model.
    if first_sweep_side is None:
        return ClassificationResult(
            day_class="RANGE_INSIDE",
            asia_high=asia_range.asia_high,
            asia_low=asia_range.asia_low,
            first_sweep_side=None,
            first_sweep_time_ny=None,
            reentry_time_ny=None,
            double_sweep=False,
        )

    # --- 2) After first sweep, look for re-entry before deadline ---
    assert first_sweep_time_ny is not None
    reentry_deadline_ny = first_sweep_time_ny + timedelta(
        minutes=reentry_deadline_minutes
    )

    for candle in sorted_candles:
        candle_time_ny = _to_new_york_time(candle.timestamp_utc)

        # We only care about candles after the first sweep time.
        if candle_time_ny < first_sweep_time_ny:
            continue

        # Stop checking for re-entry once the deadline passes.
        if candle_time_ny > reentry_deadline_ny:
            break

        if first_sweep_side == "HIGH":
            # Re-entry means price came back to or below asia_high.
            if candle.low <= asia_range.asia_high:
                reentry_time_ny = candle_time_ny
                break

        if first_sweep_side == "LOW":
            # Re-entry means price came back to or above asia_low.
            if candle.high >= asia_range.asia_low:
                reentry_time_ny = candle_time_ny
                break

    # --- 3) Optional: check for double sweep later in the morning ---
    if evaluate_double_sweep_until_ny is not None:
        for candle in sorted_candles:
            candle_time_ny = _to_new_york_time(candle.timestamp_utc)
            if candle_time_ny < first_sweep_time_ny:
                continue
            if candle_time_ny > evaluate_double_sweep_until_ny:
                break

            if candle.high > asia_range.asia_high + sweep_buffer_price:
                swept_high = True
            if candle.low < asia_range.asia_low - sweep_buffer_price:
                swept_low = True

            if swept_high and swept_low:
                double_sweep = True
                break

    # --- 4) Final classification ---
    if double_sweep:
        day_class: DayClass = "DOUBLE_SWEEP"
    elif reentry_time_ny is not None:
        day_class = "MEAN_REVERSION"
    else:
        day_class = "TREND"

    return ClassificationResult(
        day_class=day_class,
        asia_high=asia_range.asia_high,
        asia_low=asia_range.asia_low,
        first_sweep_side=first_sweep_side,
        first_sweep_time_ny=first_sweep_time_ny,
        reentry_time_ny=reentry_time_ny,
        double_sweep=double_sweep,
    )


def build_session_windows_for_date(
    trade_date_ny: datetime,
) -> Tuple[datetime, datetime, datetime, datetime, datetime]:
    """
    Given a NY-local date (any time on that date), return:

      - asia_window_start_ny: 7:00pm on the prior calendar day
      - asia_window_end_ny:   3:00am on the trade date
      - london_window_start_ny: 3:00am on the trade date
      - london_window_end_ny:   5:00am on the trade date
      - double_sweep_check_end_ny: 8:00am on the trade date (optional evaluation window)
    """
    ny = ZoneInfo("America/New_York")
    trade_date_ny = trade_date_ny.astimezone(ny)

    trade_day_start = trade_date_ny.replace(hour=0, minute=0, second=0, microsecond=0)
    prior_day = trade_day_start - timedelta(days=1)

    asia_window_start_ny = prior_day.replace(hour=19, minute=0)  # 7:00pm
    asia_window_end_ny = trade_day_start.replace(hour=3, minute=0)  # 3:00am

    london_window_start_ny = trade_day_start.replace(hour=3, minute=0)  # 3:00am
    london_window_end_ny = trade_day_start.replace(hour=5, minute=0)  # 5:00am

    double_sweep_check_end_ny = trade_day_start.replace(hour=8, minute=0)  # 8:00am

    return (
        asia_window_start_ny,
        asia_window_end_ny,
        london_window_start_ny,
        london_window_end_ny,
        double_sweep_check_end_ny,
    )
