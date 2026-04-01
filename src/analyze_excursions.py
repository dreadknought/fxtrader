# file: src/analyze_excursions.py
"""
Analyze post-reentry excursions + spread-at-event costs from labels_spread_excursions.csv.

Focus:
- MEAN_REVERSION days only (where you actually have a re-entry time)
- spread at reentry, spread p95 5m after reentry
- favorable/adverse excursion over next 30m (mid-like)
- net_favorable_after_cost_30m_pips

Outputs:
- Summary stats (mean/median/p90/p95/max) for each metric
- Win-rate style buckets:
    * P(net_favorable > 0)
    * P(net_favorable > 1,2,3,5)
- Same buckets for favorable excursion before costs
"""

from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Dict, List, Optional


def _to_float(s: str) -> Optional[float]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _quantile(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        raise ValueError("no values")
    if q <= 0:
        return sorted_vals[0]
    if q >= 1:
        return sorted_vals[-1]
    idx = int(q * (len(sorted_vals) - 1))
    return sorted_vals[idx]


def _summarize(vals: List[float]) -> Dict[str, float]:
    vals = [v for v in vals if v is not None and not math.isnan(v)]
    if not vals:
        return {}
    vals.sort()
    return {
        "n": float(len(vals)),
        "mean": sum(vals) / len(vals),
        "p50": _quantile(vals, 0.50),
        "p75": _quantile(vals, 0.75),
        "p90": _quantile(vals, 0.90),
        "p95": _quantile(vals, 0.95),
        "p99": _quantile(vals, 0.99),
        "max": vals[-1],
    }


def _print_summary(title: str, stats: Dict[str, float]) -> None:
    print(f"\n== {title} ==")
    if not stats:
        print("(no data)")
        return
    print(
        "n={n:.0f} mean={mean:.3f} p50={p50:.3f} p75={p75:.3f} "
        "p90={p90:.3f} p95={p95:.3f} p99={p99:.3f} max={max:.3f}".format(**stats)
    )


def _bucket_rates(vals: List[float], thresholds: List[float]) -> None:
    vals = [v for v in vals if v is not None and not math.isnan(v)]
    if not vals:
        print("(no data)")
        return
    n = len(vals)
    for t in thresholds:
        k = sum(1 for v in vals if v > t)
        print(f"P(> {t:g}) = {k}/{n} = {k/n:.1%}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "csv_path", type=str, nargs="?", default="out/labels_spread_excursions.csv"
    )
    args = ap.parse_args()

    path = Path(args.csv_path)
    with path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        needed = {
            "day_class",
            "reentry_time_ny",
            "spread_at_reentry_pips",
            "spread_p95_5m_after_reentry_pips",
            "favorable_excursion_30m_pips",
            "adverse_excursion_30m_pips",
            "net_favorable_after_cost_30m_pips",
        }
        missing = needed - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV missing columns: {sorted(missing)}")

        spread_at_reentry: List[float] = []
        spread_p95_5m: List[float] = []
        favorable_30m: List[float] = []
        adverse_30m: List[float] = []
        net_30m: List[float] = []

        rows = 0
        mr_rows = 0

        for r in reader:
            rows += 1
            if (r.get("day_class") or "").strip() != "MEAN_REVERSION":
                continue

            # Only count rows that actually have a reentry time and metrics.
            if not (r.get("reentry_time_ny") or "").strip():
                continue

            mr_rows += 1

            spread_at_reentry.append(
                _to_float(r.get("spread_at_reentry_pips", "")) or float("nan")
            )
            spread_p95_5m.append(
                _to_float(r.get("spread_p95_5m_after_reentry_pips", "")) or float("nan")
            )
            favorable_30m.append(
                _to_float(r.get("favorable_excursion_30m_pips", "")) or float("nan")
            )
            adverse_30m.append(
                _to_float(r.get("adverse_excursion_30m_pips", "")) or float("nan")
            )
            net_30m.append(
                _to_float(r.get("net_favorable_after_cost_30m_pips", ""))
                or float("nan")
            )

    print(f"Loaded rows: {rows}")
    print(f"MEAN_REVERSION rows with reentry: {mr_rows}")

    _print_summary("Spread at reentry (pips)", _summarize(spread_at_reentry))
    _print_summary("Spread P95 in 5m after reentry (pips)", _summarize(spread_p95_5m))

    _print_summary("Favorable excursion next 30m (pips)", _summarize(favorable_30m))
    _print_summary("Adverse excursion next 30m (pips)", _summarize(adverse_30m))
    _print_summary("Net favorable after cost next 30m (pips)", _summarize(net_30m))

    print("\n== Favorable excursion bucket rates (before costs) ==")
    _bucket_rates(favorable_30m, thresholds=[0, 1, 2, 3, 5, 8, 10])

    print("\n== Net favorable bucket rates (after costs) ==")
    _bucket_rates(net_30m, thresholds=[0, 1, 2, 3, 5])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
