# file: src/strategy/candidate_v1.py
from __future__ import annotations

from dataclasses import dataclass

# Candidate_v1: validated on your 1000-day run
# Pool A: buffer=1, reentry_deadline=10
# Gates:
#   - sweep_depth_pips <= 8
#   - asia_range_pips  >= 22.5
# Sweep monitoring window:
#   - 03:00–05:00 NY


@dataclass(frozen=True)
class CandidateV1Config:
    instrument: str = "EUR_USD"

    # Pool A parameters
    sweep_buffer_pips: float = 1.0
    reentry_deadline_minutes: int = 10

    # Gates (validated)
    gate_max_sweep_depth_pips: float = 8.0
    gate_min_asia_range_pips: float = 22.5

    # Sweep monitoring window (NY time)
    london_start_hour_ny: int = 3
    london_end_hour_ny: int = 5

    # Safety: trade limiter
    max_trades_per_day: int = 1


@dataclass(frozen=True)
class CandidateV1Risk:
    # Start small; you can tune once execution is stable.
    risk_per_trade_pct: float = 0.02
    min_units: int = 1
    max_units: int = 2000000
    units: int = 1000000
    stop_pips: float = 10.0
    take_profit_pips: float = 10.0


# Export the instances that trade_stream imports
CANDIDATE_V1 = CandidateV1Config()
CANDIDATE_V1_RISK = CandidateV1Risk()