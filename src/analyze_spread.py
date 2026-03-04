# file: src/analyze_spread.py
"""
Analyze spread stats from backtest CSV (produced by src.backtest --include-spread).

Outputs:
- overall spread stats during London window
- spread stats by day_class
- spread stats for sweep days vs non-sweep days
- simple "cost sanity" buckets (how often p95 spread exceeds X pips)
"""

from __future__ import annotations

import argparse
import csv
import math
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class SpreadRow:
    day_class: str
    first_sweep_side: str
    spread_avg: Optional[float]
    spread_p95: Optional[float]
    spread_max: Optional[float]


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


def load_rows(path: Path) -> List[SpreadRow]:
    rows: List[SpreadRow] = []
    with path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        required = {
            "day_class",
            "first_sweep_side",
            "london_spread_avg_pips",
            "london_spread_p95_pips",
            "london_spread_max_pips",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV missing required columns: {sorted(missing)}")

        for r in reader:
            rows.append(
                SpreadRow(
                    day_class=(r.get("day_class") or "").strip(),
                    first_sweep_side=(r.get("first_sweep_side") or "").strip(),
                    spread_avg=_to_float(r.get("london_spread_avg_pips", "")),
                    spread_p95=_to_float(r.get("london_spread_p95_pips", "")),
                    spread_max=_to_float(r.get("london_spread_max_pips", "")),
                )
            )
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path", type=str, nargs="?", default="out/labels_spread.csv")
    args = ap.parse_args()

    path = Path(args.csv_path)
    rows = load_rows(path)

    # Filter to days where spread stats exist (MARKET_CLOSED may have blanks)
    rows_with_spread = [r for r in rows if r.spread_avg is not None and r.spread_p95 is not None]

    all_avg = [r.spread_avg for r in rows_with_spread if r.spread_avg is not None]
    all_p95 = [r.spread_p95 for r in rows_with_spread if r.spread_p95 is not None]
    all_max = [r.spread_max for r in rows_with_spread if r.spread_max is not None]

    print(f"Loaded rows: {len(rows)}")
    print(f"Rows with spread stats: {len(rows_with_spread)}")

    _print_summary("London spread AVG (pips) - all days", _summarize(all_avg))
    _print_summary("London spread P95 (pips) - all days", _summarize(all_p95))
    _print_summary("London spread MAX (pips) - all days", _summarize(all_max))

    # By class
    by_class_avg: Dict[str, List[float]] = defaultdict(list)
    by_class_p95: Dict[str, List[float]] = defaultdict(list)
    by_class_max: Dict[str, List[float]] = defaultdict(list)

    for r in rows_with_spread:
        by_class_avg[r.day_class].append(r.spread_avg)   # type: ignore[arg-type]
        by_class_p95[r.day_class].append(r.spread_p95)   # type: ignore[arg-type]
        if r.spread_max is not None:
            by_class_max[r.day_class].append(r.spread_max)

    for cls in sorted(by_class_avg.keys()):
        _print_summary(f"{cls} - London spread AVG (pips)", _summarize(by_class_avg[cls]))
        _print_summary(f"{cls} - London spread P95 (pips)", _summarize(by_class_p95[cls]))
        _print_summary(f"{cls} - London spread MAX (pips)", _summarize(by_class_max.get(cls, [])))

    # Sweep vs non-sweep
    sweep_days = [r for r in rows_with_spread if r.first_sweep_side]
    nosweep_days = [r for r in rows_with_spread if not r.first_sweep_side]

    _print_summary("SWEEP days - London spread P95 (pips)", _summarize([r.spread_p95 for r in sweep_days if r.spread_p95 is not None]))
    _print_summary("NO-SWEEP days - London spread P95 (pips)", _summarize([r.spread_p95 for r in nosweep_days if r.spread_p95 is not None]))

    # Cost sanity buckets on P95 (how often spreads are nasty)
    buckets = [0.6, 0.8, 1.0, 1.5, 2.0]
    p95_vals = [r.spread_p95 for r in rows_with_spread if r.spread_p95 is not None]
    p95_vals.sort()

    if p95_vals:
        print("\n== P95 spread exceedance rates (London window) ==")
        n = len(p95_vals)
        for b in buckets:
            exceed = sum(1 for v in p95_vals if v > b)
            print(f"P(P95_spread > {b:.1f} pips) = {exceed}/{n} = {exceed / n:.1%}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())