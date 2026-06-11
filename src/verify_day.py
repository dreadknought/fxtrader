from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from src.strategy.candidate_v1 import CANDIDATE_V1

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
PIP_VALUE = 0.0001  # EUR_USD


@dataclass(frozen=True)
class Candle:
    time_utc: datetime
    open: float
    high: float
    low: float
    close: float

    @property
    def time_ny(self) -> datetime:
        return self.time_utc.astimezone(NY)


class CandleClient(Protocol):
    env: str

    def get_candles(
        self,
        instrument: str,
        granularity: str,
        time_from_rfc3339: str,
        time_to_rfc3339: str,
        price: str = "M",
    ) -> dict: ...


def _build_session_windows_for_date(
    trade_date_ny: datetime,
) -> tuple[datetime, datetime, datetime, datetime]:
    trade_day_start = trade_date_ny.astimezone(NY).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    prior_day = trade_day_start - timedelta(days=1)
    return (
        prior_day.replace(hour=19, minute=0),
        trade_day_start.replace(hour=3, minute=0),
        trade_day_start.replace(hour=3, minute=0),
        trade_day_start.replace(hour=5, minute=0),
    )


def _rfc3339_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _parse_candles(payload: dict) -> list[Candle]:
    candles: list[Candle] = []
    for raw in payload.get("candles", []):
        if not raw.get("complete", True) or not raw.get("mid"):
            continue
        mid = raw["mid"]
        candles.append(
            Candle(
                time_utc=datetime.fromisoformat(
                    str(raw["time"]).replace("Z", "+00:00")
                ).astimezone(UTC),
                open=float(mid["o"]),
                high=float(mid["h"]),
                low=float(mid["l"]),
                close=float(mid["c"]),
            )
        )
    return sorted(candles, key=lambda candle: candle.time_utc)


def _fmt_time(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M %Z") if value else "-"


def _fmt_price(value: float) -> str:
    return f"{value:.5f}"


def _print_candle(prefix: str, candle: Candle) -> None:
    print(
        f"{prefix}: {_fmt_time(candle.time_ny)} "
        f"O={_fmt_price(candle.open)} H={_fmt_price(candle.high)} "
        f"L={_fmt_price(candle.low)} C={_fmt_price(candle.close)}"
    )


def _verify_day(client: CandleClient, trade_day: date) -> int:
    cfg = CANDIDATE_V1
    trade_date_ny = datetime.combine(trade_day, datetime.min.time(), tzinfo=NY)
    (
        asia_start_ny,
        asia_end_ny,
        british_start_ny,
        british_end_ny,
    ) = _build_session_windows_for_date(trade_date_ny)

    payload = client.get_candles(
        instrument=cfg.instrument,
        granularity="M1",
        time_from_rfc3339=_rfc3339_utc(asia_start_ny),
        time_to_rfc3339=_rfc3339_utc(british_end_ny),
        price="M",
    )
    candles = _parse_candles(payload)
    asia = [
        candle
        for candle in candles
        if asia_start_ny <= candle.time_ny < asia_end_ny
    ]
    british = [
        candle
        for candle in candles
        if british_start_ny <= candle.time_ny < british_end_ny
    ]
    expected_asia_candles = int(
        (asia_end_ny.astimezone(UTC) - asia_start_ny.astimezone(UTC)).total_seconds()
        // 60
    )
    expected_british_candles = int(
        (
            british_end_ny.astimezone(UTC) - british_start_ny.astimezone(UTC)
        ).total_seconds()
        // 60
    )

    print("=== fxtrader candle verification ===")
    print(f"OANDA environment: {client.env}")
    print(f"Instrument: {cfg.instrument}")
    print(f"Trade date: {trade_day} (America/New_York)")
    print(f"Asia window: {_fmt_time(asia_start_ny)} -> {_fmt_time(asia_end_ny)}")
    print(
        f"British window: {_fmt_time(british_start_ny)} -> "
        f"{_fmt_time(british_end_ny)}"
    )
    print(
        f"Completed M1 candles: Asia={len(asia)}/{expected_asia_candles} "
        f"British={len(british)}/{expected_british_candles}"
    )
    print()

    if not asia:
        print("RESULT: UNVERIFIED - no completed Asia candles returned")
        return 2
    if not british:
        print("RESULT: UNVERIFIED - no completed British-window candles returned")
        return 2

    asia_high_candle = max(asia, key=lambda candle: candle.high)
    asia_low_candle = min(asia, key=lambda candle: candle.low)
    asia_high = asia_high_candle.high
    asia_low = asia_low_candle.low
    asia_range_pips = (asia_high - asia_low) / PIP_VALUE
    upper = asia_high + cfg.sweep_buffer_pips * PIP_VALUE
    lower = asia_low - cfg.sweep_buffer_pips * PIP_VALUE

    british_high_candle = max(british, key=lambda candle: candle.high)
    british_low_candle = min(british, key=lambda candle: candle.low)
    high_penetration = max(0.0, (british_high_candle.high - upper) / PIP_VALUE)
    low_penetration = max(0.0, (lower - british_low_candle.low) / PIP_VALUE)

    print(
        f"Asia range: high={_fmt_price(asia_high)} low={_fmt_price(asia_low)} "
        f"range={asia_range_pips:.2f}p"
    )
    _print_candle("Asia high candle", asia_high_candle)
    _print_candle("Asia low candle", asia_low_candle)
    print(
        f"Sweep thresholds: HIGH>{_fmt_price(upper)} "
        f"LOW<{_fmt_price(lower)} (buffer={cfg.sweep_buffer_pips:.1f}p)"
    )
    print()
    _print_candle("British high candle", british_high_candle)
    _print_candle("British low candle", british_low_candle)
    print(
        f"Boundary penetration: HIGH={high_penetration:.2f}p "
        f"LOW={low_penetration:.2f}p"
    )
    print()

    if (
        len(asia) != expected_asia_candles
        or len(british) != expected_british_candles
    ):
        print(
            "RESULT: UNVERIFIED - candle coverage is incomplete, so missing "
            "minutes could change the reconstructed decision"
        )
        return 2

    if asia_range_pips < cfg.gate_min_asia_range_pips:
        print(
            "RESULT: CONFIRMED no trade - Asia range gate failed "
            f"({asia_range_pips:.2f}p < {cfg.gate_min_asia_range_pips:.2f}p)"
        )
        return 0

    sweep_candles = [
        candle for candle in british if candle.high > upper or candle.low < lower
    ]
    if not sweep_candles:
        print("RESULT: CONFIRMED no trade - no_sweep_in_british_window")
        return 0

    first_sweep = sweep_candles[0]
    swept_high = first_sweep.high > upper
    swept_low = first_sweep.low < lower
    _print_candle("First sweep candle", first_sweep)

    if swept_high and swept_low:
        print(
            "RESULT: AMBIGUOUS - the first sweep candle crossed both boundaries; "
            "M1 data cannot determine which side swept first"
        )
        return 1

    side = "HIGH" if swept_high else "LOW"
    boundary = asia_high if side == "HIGH" else asia_low
    deadline_ny = first_sweep.time_ny + timedelta(
        minutes=cfg.reentry_deadline_minutes
    )
    same_minute_reentry_possible = (
        first_sweep.low <= boundary if side == "HIGH" else first_sweep.high >= boundary
    )

    later_candles = [
        candle
        for candle in british
        if first_sweep.time_ny < candle.time_ny <= deadline_ny
    ]
    reentry_candle = next(
        (
            candle
            for candle in later_candles
            if (
                candle.low <= asia_high
                if side == "HIGH"
                else candle.high >= asia_low
            )
        ),
        None,
    )

    through_reentry = [
        candle
        for candle in british
        if first_sweep.time_ny
        <= candle.time_ny
        <= (reentry_candle.time_ny if reentry_candle else deadline_ny)
    ]
    if side == "HIGH":
        max_penetration = max(
            max(0.0, (candle.high - upper) / PIP_VALUE)
            for candle in through_reentry
        )
    else:
        max_penetration = max(
            max(0.0, (lower - candle.low) / PIP_VALUE)
            for candle in through_reentry
        )

    print(f"First sweep side: {side}")
    print(f"Reentry deadline: {_fmt_time(deadline_ny)}")
    print(f"Maximum observed penetration through deadline/reentry: {max_penetration:.2f}p")
    if same_minute_reentry_possible:
        print(
            "Note: the first sweep candle also touched the reentry boundary; "
            "intraminute ordering is unknown."
        )
    if reentry_candle:
        _print_candle("First provable later-minute reentry candle", reentry_candle)

    if max_penetration > cfg.gate_max_sweep_depth_pips:
        qualifier = "AMBIGUOUS" if same_minute_reentry_possible else "CONFIRMED"
        print(
            f"RESULT: {qualifier} no trade - deep_sweep "
            f"({max_penetration:.2f}p > {cfg.gate_max_sweep_depth_pips:.2f}p)"
        )
        return 1 if qualifier == "AMBIGUOUS" else 0

    if reentry_candle:
        print(
            f"RESULT: LIKELY trade setup - {side} sweep and reentry within "
            f"{cfg.reentry_deadline_minutes} minutes; compare against tick-level worker logs"
        )
        return 0

    if same_minute_reentry_possible:
        print(
            "RESULT: AMBIGUOUS - sweep and possible reentry occurred in the same "
            "M1 candle, and no later-minute reentry proves the sequence"
        )
        return 1

    print("RESULT: CONFIRMED no trade - sweep_no_reentry_by_deadline")
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill OANDA M1 midpoint candles and independently verify the "
            "candidate-v1 decision for a New York trade date."
        )
    )
    parser.add_argument(
        "date",
        nargs="?",
        default=datetime.now(NY).date().isoformat(),
        help="New York trade date in YYYY-MM-DD format (default: today)",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        trade_day = date.fromisoformat(args.date)
    except ValueError:
        raise SystemExit(f"Invalid date {args.date!r}; expected YYYY-MM-DD")

    from src.oanda.oanda_client import OandaClient, load_oanda_config

    client = OandaClient(load_oanda_config())
    return _verify_day(client, trade_day)


if __name__ == "__main__":
    raise SystemExit(main())
