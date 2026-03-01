# file: src/backtest.py
"""
Backtest / label London day classifications over many trading days.

Sessions (New York time):
  - Asia:   19:00 -> 03:00
  - London: 03:00 -> 05:00

Data fetch:
  - Default: uses MID candles (1 request/day)
  - With --include-spread: uses BID + ASK candles (2 requests/day) and synthesizes MID
    so we can compute spread stats and still classify on mid-like prices.

Outputs CSV rows per trading day.

Environment variables:
  - OANDA_ENV ("practice" or "live", default: "practice")
  - OANDA_PRACTICE_KEY (required when OANDA_ENV=practice)
  - OANDA_LIVE_KEY (required when OANDA_ENV=live)
  - OANDA_KEY (optional fallback if env-specific key not set)
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.oanda.oanda_client import OandaApiError, OandaClient, load_oanda_config
from src.strategy.london_day_classifier import (
    Candle,
    MarketClosedOrNoDataError,
    build_session_windows_for_date,
    classify_london_day,
    compute_asia_range,
    parse_oanda_candles,
)

NY_TZ = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")

PIP_VALUE_EURUSD = 0.0001  # good enough for now; later: derive per-instrument


def _is_weekend_in_new_york(dt_ny: datetime) -> bool:
    return dt_ny.weekday() >= 5  # Sat/Sun


def _iter_trading_dates_backwards(end_date_ny: datetime, trading_days: int) -> List[datetime]:
    """
    Return NY-local dates (at midnight) for the last `trading_days`, skipping weekends.
    Returned oldest -> newest.
    """
    dates: List[datetime] = []
    cursor = end_date_ny.astimezone(NY_TZ).replace(hour=0, minute=0, second=0, microsecond=0)

    while len(dates) < trading_days:
        if not _is_weekend_in_new_york(cursor):
            dates.append(cursor)
        cursor -= timedelta(days=1)

    dates.reverse()
    return dates


def _to_rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC_TZ).isoformat()


def _fmt_seconds(seconds: float) -> str:
    seconds_int = int(max(0.0, seconds))
    m, s = divmod(seconds_int, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _print_progress(
    *,
    idx: int,
    total: int,
    trade_date_ny: datetime,
    ok_count: int,
    err_count: int,
    candles_count: Optional[int],
    started_monotonic: float,
    every: int,
) -> None:
    if every <= 0:
        every = 1
    if (idx % every != 0) and (idx != total):
        return

    elapsed = time.monotonic() - started_monotonic
    avg = elapsed / max(1, idx)
    remaining = avg * (total - idx)

    candles_part = f" candles={candles_count}" if candles_count is not None else ""
    print(
        f"[{idx:>4}/{total}] day={trade_date_ny.date()} ok={ok_count} err={err_count}"
        f"{candles_part} elapsed={_fmt_seconds(elapsed)} eta={_fmt_seconds(remaining)}",
        flush=True,
    )


@dataclass(frozen=True)
class _Ohlc:
    o: float
    h: float
    l: float
    c: float


def _parse_oanda_ohlc_map(payload: dict, price_key: str) -> Dict[datetime, _Ohlc]:
    """
    Returns: timestamp_utc -> OHLC for the given price_key ("bid" or "ask" or "mid").
    Only includes complete candles.
    """
    out: Dict[datetime, _Ohlc] = {}
    for c in payload.get("candles", []):
        if not c.get("complete", True):
            continue
        ts = datetime.fromisoformat(c["time"].replace("Z", "+00:00")).astimezone(UTC_TZ)
        p = c[price_key]
        out[ts] = _Ohlc(
            o=float(p["o"]),
            h=float(p["h"]),
            l=float(p["l"]),
            c=float(p["c"]),
        )
    return out


def _synthesize_mid_candles_from_bid_ask(bid_payload: dict, ask_payload: dict) -> List[Candle]:
    """
    Create Candle(timestamp_utc, high, low) using the average of bid/ask highs and lows.

    If timestamps don’t perfectly align (rare), we only keep intersections.
    """
    bid = _parse_oanda_ohlc_map(bid_payload, "bid")
    ask = _parse_oanda_ohlc_map(ask_payload, "ask")

    common_ts = sorted(set(bid.keys()) & set(ask.keys()))
    candles: List[Candle] = []

    for ts in common_ts:
        b = bid[ts]
        a = ask[ts]

        mid_high = (b.h + a.h) / 2.0
        mid_low = (b.l + a.l) / 2.0
        candles.append(Candle(timestamp_utc=ts, high=mid_high, low=mid_low))

    return candles


def _london_spread_stats_pips(
    *,
    bid_payload: dict,
    ask_payload: dict,
    london_window_start_ny: datetime,
    london_window_end_ny: datetime,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Compute (avg, p95, max) spread in pips inside the London decision window
    using spread = ask_close - bid_close.

    Returns (avg, p95, max). If no data, returns (None, None, None).
    """
    bid = _parse_oanda_ohlc_map(bid_payload, "bid")
    ask = _parse_oanda_ohlc_map(ask_payload, "ask")

    spreads: List[float] = []

    for ts in sorted(set(bid.keys()) & set(ask.keys())):
        ts_ny = ts.astimezone(NY_TZ)
        if not (london_window_start_ny <= ts_ny < london_window_end_ny):
            continue

        spread = ask[ts].c - bid[ts].c
        spreads.append(spread / PIP_VALUE_EURUSD)

    if not spreads:
        return None, None, None

    spreads.sort()
    avg = sum(spreads) / len(spreads)
    p95_index = int(0.95 * (len(spreads) - 1))
    p95 = spreads[p95_index]
    mx = spreads[-1]
    return avg, p95, mx


def run_backtest(
    *,
    instrument: str,
    trading_days: int,
    output_csv_path: Path,
    granularity: str,
    sweep_buffer_pips: float,
    reentry_deadline_minutes: int,
    include_spread: bool,
    progress_every: int,
    sleep_seconds_between_requests: float,
    max_requests: int,
) -> None:
    config = load_oanda_config()
    client = OandaClient(config)

    end_date_ny = datetime.now(tz=NY_TZ)
    trade_dates_ny = _iter_trading_dates_backwards(end_date_ny=end_date_ny, trading_days=trading_days)

    if len(trade_dates_ny) > max_requests:
        trade_dates_ny = trade_dates_ny[-max_requests:]

    # Counts
    class_counts: Dict[str, int] = {
        "MEAN_REVERSION": 0,
        "TREND": 0,
        "DOUBLE_SWEEP": 0,
        "RANGE_INSIDE": 0,
        "MARKET_CLOSED": 0,
    }

    total_requests = 0
    total_errors = 0
    ok_days = 0

    started = time.monotonic()
    total_days = len(trade_dates_ny)

    # CSV rows
    results: List[Dict[str, str]] = []

    for idx, trade_date_ny in enumerate(trade_dates_ny, start=1):
        candles_count: Optional[int] = None

        (
            asia_window_start_ny,
            asia_window_end_ny,
            london_window_start_ny,
            london_window_end_ny,
            double_sweep_check_end_ny,
        ) = build_session_windows_for_date(trade_date_ny)

        fetch_start = _to_rfc3339(asia_window_start_ny)
        fetch_end = _to_rfc3339(double_sweep_check_end_ny)

        spread_avg = spread_p95 = spread_max = None

        try:
            if include_spread:
                # 2 requests/day: BID + ASK
                bid_payload = client.get_candles(
                    instrument=instrument,
                    granularity=granularity,
                    time_from_rfc3339=fetch_start,
                    time_to_rfc3339=fetch_end,
                    price="B",
                )
                ask_payload = client.get_candles(
                    instrument=instrument,
                    granularity=granularity,
                    time_from_rfc3339=fetch_start,
                    time_to_rfc3339=fetch_end,
                    price="A",
                )
                total_requests += 2

                candles = _synthesize_mid_candles_from_bid_ask(bid_payload, ask_payload)
                candles_count = len(candles)

                spread_avg, spread_p95, spread_max = _london_spread_stats_pips(
                    bid_payload=bid_payload,
                    ask_payload=ask_payload,
                    london_window_start_ny=london_window_start_ny,
                    london_window_end_ny=london_window_end_ny,
                )
            else:
                # 1 request/day: MID
                mid_payload = client.get_candles(
                    instrument=instrument,
                    granularity=granularity,
                    time_from_rfc3339=fetch_start,
                    time_to_rfc3339=fetch_end,
                    price="M",
                )
                total_requests += 1

                candles = parse_oanda_candles(mid_payload, price_key="mid")
                candles_count = len(candles)

            asia_range = compute_asia_range(
                candles=candles,
                asia_window_start_ny=asia_window_start_ny,
                asia_window_end_ny=asia_window_end_ny,
            )

            classification = classify_london_day(
                candles=candles,
                asia_range=asia_range,
                london_window_start_ny=london_window_start_ny,
                london_window_end_ny=london_window_end_ny,
                sweep_buffer_pips=sweep_buffer_pips,
                reentry_deadline_minutes=reentry_deadline_minutes,
                evaluate_double_sweep_until_ny=double_sweep_check_end_ny,
            )

            class_counts[classification.day_class] = class_counts.get(classification.day_class, 0) + 1
            ok_days += 1

            row: Dict[str, str] = {
                "date_ny": str(trade_date_ny.date()),
                "instrument": instrument,
                "sweep_buffer_pips": str(sweep_buffer_pips),
                "reentry_deadline_minutes": str(reentry_deadline_minutes),
                "asia_high": f"{classification.asia_high:.5f}",
                "asia_low": f"{classification.asia_low:.5f}",
                "first_sweep_side": classification.first_sweep_side or "",
                "first_sweep_time_ny": classification.first_sweep_time_ny.isoformat()
                if classification.first_sweep_time_ny
                else "",
                "reentry_time_ny": classification.reentry_time_ny.isoformat() if classification.reentry_time_ny else "",
                "double_sweep": str(classification.double_sweep),
                "day_class": classification.day_class,
            }

            if include_spread:
                row.update(
                    {
                        "london_spread_avg_pips": "" if spread_avg is None else f"{spread_avg:.3f}",
                        "london_spread_p95_pips": "" if spread_p95 is None else f"{spread_p95:.3f}",
                        "london_spread_max_pips": "" if spread_max is None else f"{spread_max:.3f}",
                    }
                )

            results.append(row)

        except MarketClosedOrNoDataError:
            class_counts["MARKET_CLOSED"] += 1
            row = {
                "date_ny": str(trade_date_ny.date()),
                "instrument": instrument,
                "sweep_buffer_pips": str(sweep_buffer_pips),
                "reentry_deadline_minutes": str(reentry_deadline_minutes),
                "asia_high": "",
                "asia_low": "",
                "first_sweep_side": "",
                "first_sweep_time_ny": "",
                "reentry_time_ny": "",
                "double_sweep": "",
                "day_class": "MARKET_CLOSED",
            }
            if include_spread:
                row.update(
                    {
                        "london_spread_avg_pips": "" if spread_avg is None else f"{spread_avg:.3f}",
                        "london_spread_p95_pips": "" if spread_p95 is None else f"{spread_p95:.3f}",
                        "london_spread_max_pips": "" if spread_max is None else f"{spread_max:.3f}",
                    }
                )
            results.append(row)

        except OandaApiError as e:
            total_errors += 1
            print(f"[WARN] OANDA error on {trade_date_ny.date()}: {e}", file=sys.stderr)

        except Exception as e:
            total_errors += 1
            print(f"[WARN] Unexpected error on {trade_date_ny.date()}: {e}", file=sys.stderr)

        _print_progress(
            idx=idx,
            total=total_days,
            trade_date_ny=trade_date_ny,
            ok_count=ok_days,
            err_count=total_errors,
            candles_count=candles_count,
            started_monotonic=started,
            every=progress_every,
        )

        if sleep_seconds_between_requests > 0:
            time.sleep(sleep_seconds_between_requests)

    # Write CSV
    output_csv_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "date_ny",
        "instrument",
        "sweep_buffer_pips",
        "reentry_deadline_minutes",
        "asia_high",
        "asia_low",
        "first_sweep_side",
        "first_sweep_time_ny",
        "reentry_time_ny",
        "double_sweep",
        "day_class",
    ]
    if include_spread:
        fieldnames.extend(
            [
                "london_spread_avg_pips",
                "london_spread_p95_pips",
                "london_spread_max_pips",
            ]
        )

    with output_csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(r)

    # Summary
    total_labeled = sum(class_counts.values())
    print("=== Backtest Summary ===")
    print(f"Instrument: {instrument}")
    print(f"Trading days attempted: {len(trade_dates_ny)}")
    print(f"OANDA requests made: {total_requests}")
    print(f"Errors: {total_errors}")
    print(f"Output CSV: {output_csv_path}")
    if total_labeled > 0:
        for k in ["MEAN_REVERSION", "TREND", "DOUBLE_SWEEP", "RANGE_INSIDE", "MARKET_CLOSED"]:
            count = class_counts.get(k, 0)
            pct = (count / total_labeled) * 100.0
            print(f"{k:14s}: {count:4d}  ({pct:5.1f}%)")


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m src.backtest",
        description="Run London sweep day classification backtest and output CSV.",
    )

    p.add_argument("trading_days", type=int, nargs="?", default=250, help="Number of trading days (weekends skipped).")
    p.add_argument("instrument", type=str, nargs="?", default="EUR_USD", help="Instrument (e.g., EUR_USD).")
    p.add_argument(
        "output_csv",
        type=str,
        nargs="?",
        default="out/london_day_labels.csv",
        help="Output CSV path.",
    )

    p.add_argument("--granularity", type=str, default="M1", help="Candle granularity (default: M1).")
    p.add_argument("--buffer-pips", type=float, default=2.0, help="Sweep buffer in pips (default: 2.0).")
    p.add_argument("--reentry-minutes", type=int, default=20, help="Re-entry deadline minutes (default: 20).")
    p.add_argument(
        "--include-spread",
        action="store_true",
        help="Use BID+ASK candles (2 req/day), compute London spread stats, and synthesize MID for classification.",
    )
    p.add_argument("--progress-every", type=int, default=5, help="Print progress every N days (default: 5).")
    p.add_argument(
        "--sleep",
        type=float,
        default=0.05,
        help="Sleep seconds between requests to be polite (default: 0.05).",
    )
    p.add_argument(
        "--max-requests",
        type=int,
        default=600,
        help="Safety cap: max trading days processed (default: 600).",
    )
    return p


def main() -> int:
    args = _build_arg_parser().parse_args()

    run_backtest(
        instrument=args.instrument,
        trading_days=args.trading_days,
        output_csv_path=Path(args.output_csv),
        granularity=args.granularity,
        sweep_buffer_pips=args.buffer_pips,
        reentry_deadline_minutes=args.reentry_minutes,
        include_spread=args.include_spread,
        progress_every=args.progress_every,
        sleep_seconds_between_requests=args.sleep,
        max_requests=args.max_requests,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())