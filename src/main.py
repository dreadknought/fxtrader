# file: src/main.py
"""
Run a single-day London classification for EUR_USD.

Usage examples:
  python -m src.main 2026-02-24
"""

from __future__ import annotations

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from src.oanda.oanda_client import OandaClient, load_oanda_config
from src.strategy.london_day_classifier import (
    parse_oanda_candles,
    build_session_windows_for_date,
    classify_london_day,
    compute_asia_range,
)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python -m src.main YYYY-MM-DD")
        return 2

    # Interpret the input date as a New York local trading date.
    ny = ZoneInfo("America/New_York")
    trade_date_ny = datetime.fromisoformat(sys.argv[1]).replace(tzinfo=ny)

    (
        asia_window_start_ny,
        asia_window_end_ny,
        london_window_start_ny,
        london_window_end_ny,
        double_sweep_check_end_ny,
    ) = build_session_windows_for_date(trade_date_ny)

    # Fetch enough candles to cover 7pm prior day through 8am trade day.
    # We'll use M1 candles for clean sweep/re-entry timing.
    fetch_start_utc = asia_window_start_ny.astimezone(ZoneInfo("UTC")).isoformat()
    fetch_end_utc = double_sweep_check_end_ny.astimezone(ZoneInfo("UTC")).isoformat()

    config = load_oanda_config()
    client = OandaClient(config)

    payload = client.get_candles(
        instrument="EUR_USD",
        granularity="M1",
        time_from_rfc3339=fetch_start_utc,
        time_to_rfc3339=fetch_end_utc,
        price="M",
    )

    candles = parse_oanda_candles(payload, price_key="mid")

    asia_range = compute_asia_range(
        candles=candles,
        asia_window_start_ny=asia_window_start_ny,
        asia_window_end_ny=asia_window_end_ny,
    )

    result = classify_london_day(
        candles=candles,
        asia_range=asia_range,
        london_window_start_ny=london_window_start_ny,
        london_window_end_ny=london_window_end_ny,
        sweep_buffer_pips=2.0,
        reentry_deadline_minutes=20,
        evaluate_double_sweep_until_ny=double_sweep_check_end_ny,
    )

    print("=== London Day Classification ===")
    print(f"Trade date (NY): {trade_date_ny.date()}")
    print(f"Asia window:     {asia_window_start_ny}  ->  {asia_window_end_ny}")
    print(f"Asia high:       {result.asia_high:.5f}")
    print(f"Asia low:        {result.asia_low:.5f}")
    print(f"Decision window: {london_window_start_ny}  ->  {london_window_end_ny}")
    print(f"First sweep:     {result.first_sweep_side} at {result.first_sweep_time_ny}")
    print(f"Re-entry:        {result.reentry_time_ny}")
    print(f"Double sweep:    {result.double_sweep}")
    print(f"DAY CLASS:       {result.day_class}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
