# file: src/dry_run_stream.py
from __future__ import annotations

import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from src.oanda.oanda_client import OandaClient, load_oanda_config
from src.strategy.gated_strategy import (
    GateConfig,
    LiveState,
    evaluate_decision,
    update_state_from_tick,
)

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def _mid_from_price_msg(msg: dict) -> float | None:
    # PRICE message format: bids/asks lists
    bids = msg.get("bids") or []
    asks = msg.get("asks") or []
    if not bids or not asks:
        return None
    try:
        b = float(bids[0]["price"])
        a = float(asks[0]["price"])
        return (a + b) / 2.0
    except Exception:
        return None


def _ts_utc_from_msg(msg: dict) -> datetime | None:
    t = msg.get("time")
    if not t:
        return None
    # RFC3339 with Z
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(UTC)
    except Exception:
        return None


def main() -> int:
    instrument = os.environ.get("FX_INSTRUMENT", "EUR_USD").strip()
    account_id = (os.environ.get("OANDA_ACCOUNT_ID") or "").strip()
    if not account_id:
        print(
            "Missing OANDA_ACCOUNT_ID (required for pricing stream).", file=sys.stderr
        )
        return 2

    gate = GateConfig(
        max_sweep_depth_pips=float(os.environ.get("GATE_MAX_SWEEP_DEPTH_PIPS", "8")),
        min_asia_range_pips=float(os.environ.get("GATE_MIN_ASIA_RANGE_PIPS", "22.5")),
        sweep_buffer_pips=float(os.environ.get("SWEEP_BUFFER_PIPS", "1")),
        reentry_deadline_minutes=int(os.environ.get("REENTRY_DEADLINE_MINUTES", "10")),
    )

    config = load_oanda_config()
    client = OandaClient(config)

    # Per-day state
    current_trade_date_ny = datetime.now(tz=NY).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    state = LiveState()

    printed_pass_for_day = False

    print("=== DRY RUN (no orders) ===")
    print(f"OANDA_ENV={os.environ.get('OANDA_ENV','practice')}")
    print(f"instrument={instrument}")
    print(
        "gates:",
        f"sweep_depth_pips<={gate.max_sweep_depth_pips}",
        f"asia_range_pips>={gate.min_asia_range_pips}",
        f"buffer_pips={gate.sweep_buffer_pips}",
        f"reentry_deadline_minutes={gate.reentry_deadline_minutes}",
        flush=True,
    )

    for msg in client.stream_pricing(account_id=account_id, instruments=instrument):
        msg_type = msg.get("type")

        if msg_type == "HEARTBEAT":
            continue
        if msg_type != "PRICE":
            continue

        ts_utc = _ts_utc_from_msg(msg)
        mid = _mid_from_price_msg(msg)
        if ts_utc is None or mid is None:
            continue

        now_ny = ts_utc.astimezone(NY)
        trade_date_ny = now_ny.replace(hour=0, minute=0, second=0, microsecond=0)

        # rollover at NY midnight
        if trade_date_ny.date() != current_trade_date_ny.date():
            current_trade_date_ny = trade_date_ny
            state = LiveState()
            printed_pass_for_day = False
            print(f"\n--- New NY day: {current_trade_date_ny.date()} ---", flush=True)

        state = update_state_from_tick(
            state=state,
            tick_time_utc=ts_utc,
            mid_price=mid,
            trade_date_ny=current_trade_date_ny,
            gate=gate,
        )

        # Only log the first PASS per day, at the moment we have reentry and pass gates.
        if not printed_pass_for_day:
            d = evaluate_decision(state=state, gate=gate)
            if d.reason == "PASS":
                printed_pass_for_day = True
                print(
                    f"[PASS] {current_trade_date_ny.date()} "
                    f"asia_range_pips={d.asia_range_pips:.2f} "
                    f"sweep_depth_pips={d.sweep_depth_pips:.2f} "
                    f"sweep={d.first_sweep_side}@{d.first_sweep_time_ny} "
                    f"reentry={d.reentry_time_ny}",
                    flush=True,
                )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
