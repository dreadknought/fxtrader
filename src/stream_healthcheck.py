# file: src/stream_healthcheck.py
from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from src.oanda.oanda_client import OandaClient, load_oanda_config

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")

# This tool is a sanity check. It is NOT a separate workflow.
# You use it when you:
#  - first wire up streaming
#  - switch OANDA_ENV to live for the first time
#  - rotate keys
#  - debug stream/auth issues
PIP_VALUE = 0.0001  # EUR_USD


def main() -> None:
    cfg = load_oanda_config()
    client = OandaClient(cfg)

    instrument = os.environ.get("FX_INSTRUMENT", "EUR_USD").strip()

    print(f"OANDA_ENV={cfg.env} base_url={cfg.base_url} account_id={cfg.account_id}")
    print(f"Streaming pricing for {instrument}... (printing 20 PRICE ticks)")
    print("-" * 80)

    n = 0
    for msg in client.stream_pricing(instruments=instrument):
        msg_type = msg.get("type")

        # Heartbeats are expected; ignore them.
        if msg_type == "HEARTBEAT":
            continue
        if msg_type != "PRICE":
            continue

        bids = msg.get("bids") or []
        asks = msg.get("asks") or []
        if not bids or not asks:
            continue

        try:
            bid = float(bids[0]["price"])
            ask = float(asks[0]["price"])
            mid = (bid + ask) / 2.0

            ts = datetime.fromisoformat(msg["time"].replace("Z", "+00:00")).astimezone(NY)
            spread_pips = (ask - bid) / PIP_VALUE
        except Exception:
            continue

        print(f"{ts.isoformat()}  bid={bid:.5f} ask={ask:.5f} mid={mid:.5f} spread={spread_pips:.2f}p")

        n += 1
        if n >= 20:
            break

    print("-" * 80)
    print("OK: stream is alive.")


if __name__ == "__main__":
    main()