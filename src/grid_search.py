# file: src/grid_search.py
"""
Grid-search parameter permutations for the London sweep classifier.

Uses src.backtest.run_backtest().

Key improvement:
- Supports disk candle cache (Option A), so after the first run fills cache,
  subsequent permutations reuse data with *zero* OANDA requests.

Recommended workflow:
1) Run a small grid once to fill cache:
   uv run python -m src.grid_search --days 250 --cache-dir out/candle_cache

2) Re-run bigger grids instantly (still using same cache):
   uv run python -m src.grid_search --days 1000 --cache-dir out/candle_cache --buffers ... --reentries ...
"""

from __future__ import annotations

import argparse
import csv
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from src.backtest import run_backtest


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
        return float("nan")
    if q <= 0:
        return sorted_vals[0]
    if q >= 1:
        return sorted_vals[-1]
    idx = int(q * (len(sorted_vals) - 1))
    return sorted_vals[idx]


@dataclass(frozen=True)
class RunMetrics:
    n_days_total: int
    n_mr_reentry: int

    net_mean: float
    net_p50: float
    net_p75: float
    net_p90: float
    net_p95: float

    adverse_p50: float
    adverse_p90: float
    adverse_p95: float

    spread_reentry_p50: float
    spread_reentry_p90: float

    p_net_gt_0: float
    p_net_gt_2: float
    p_net_gt_5: float


def _safe_stats(vals: List[float]) -> Tuple[float, float, float, float, float]:
    vals = [v for v in vals if v is not None and not math.isnan(v)]
    if not vals:
        return (float("nan"),) * 5
    vals.sort()
    mean = sum(vals) / len(vals)
    return (
        mean,
        _quantile(vals, 0.50),
        _quantile(vals, 0.75),
        _quantile(vals, 0.90),
        _quantile(vals, 0.95),
    )


def _rate_gt(vals: List[float], threshold: float) -> float:
    vals = [v for v in vals if v is not None and not math.isnan(v)]
    if not vals:
        return float("nan")
    return sum(1 for v in vals if v > threshold) / len(vals)


def analyze_run(csv_path: Path) -> RunMetrics:
    with csv_path.open("r", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    net_vals: List[float] = []
    adverse_vals: List[float] = []
    spread_reentry_vals: List[float] = []

    for r in rows:
        if (r.get("day_class") or "").strip() != "MEAN_REVERSION":
            continue
        if not (r.get("reentry_time_ny") or "").strip():
            continue

        net = _to_float(r.get("net_favorable_after_cost_30m_pips", ""))
        adverse = _to_float(r.get("adverse_excursion_30m_pips", ""))
        spread_reentry = _to_float(r.get("spread_at_reentry_pips", ""))

        if net is not None:
            net_vals.append(net)
        if adverse is not None:
            adverse_vals.append(adverse)
        if spread_reentry is not None:
            spread_reentry_vals.append(spread_reentry)

    net_mean, net_p50, net_p75, net_p90, net_p95 = _safe_stats(net_vals)
    adverse_mean, adverse_p50, adverse_p90, adverse_p95, _ = _safe_stats(adverse_vals)
    spread_mean, spread_p50, spread_p90, _, _ = _safe_stats(spread_reentry_vals)

    return RunMetrics(
        n_days_total=len(rows),
        n_mr_reentry=len(net_vals),
        net_mean=net_mean,
        net_p50=net_p50,
        net_p75=net_p75,
        net_p90=net_p90,
        net_p95=net_p95,
        adverse_p50=adverse_p50,
        adverse_p90=adverse_p90,
        adverse_p95=adverse_p95,
        spread_reentry_p50=spread_p50,
        spread_reentry_p90=spread_p90,
        p_net_gt_0=_rate_gt(net_vals, 0.0),
        p_net_gt_2=_rate_gt(net_vals, 2.0),
        p_net_gt_5=_rate_gt(net_vals, 5.0),
    )


def score_run(metrics: RunMetrics, *, min_samples: int) -> float:
    if metrics.n_mr_reentry < min_samples:
        return float("-inf")

    # Default scoring: reward net edge, penalize adverse tail.
    return (
        (1.0 * metrics.net_p50)
        + (0.4 * metrics.net_mean)
        + (3.0 * (metrics.p_net_gt_2 if not math.isnan(metrics.p_net_gt_2) else 0.0))
        - (0.06 * metrics.adverse_p95 if not math.isnan(metrics.adverse_p95) else 0.0)
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", default="EUR_USD")
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--granularity", default="M1")

    ap.add_argument(
        "--buffers", default="1,2,3,5", help="Comma list of sweep buffers in pips."
    )
    ap.add_argument(
        "--reentries", default="10,20,30,45", help="Comma list of reentry minutes."
    )

    ap.add_argument("--out-dir", default="out/grid")
    ap.add_argument("--summary", default="out/grid_summary.csv")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--min-samples", type=int, default=40)
    ap.add_argument("--sleep", type=float, default=0.05)
    ap.add_argument("--progress-every", type=int, default=25)

    ap.add_argument(
        "--cache-dir",
        default="out/candle_cache",
        help="Cache dir for candle JSON. Use empty to disable.",
    )
    args = ap.parse_args()

    buffers = [float(x.strip()) for x in args.buffers.split(",") if x.strip()]
    reentries = [int(x.strip()) for x in args.reentries.split(",") if x.strip()]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    combos = [(b, r) for b in buffers for r in reentries]
    print(
        f"Running {len(combos)} permutations with caching={'ON' if cache_dir else 'OFF'}"
    )

    summary_rows: List[Dict[str, str]] = []

    for i, (buffer_pips, reentry_min) in enumerate(combos, start=1):
        run_name = f"{args.instrument}_d{args.days}_buf{buffer_pips:g}_re{reentry_min}"
        csv_path = out_dir / f"{run_name}.csv"

        print(f"\n[{i}/{len(combos)}] RUN {run_name}")
        run_backtest(
            instrument=args.instrument,
            trading_days=args.days,
            output_csv_path=csv_path,
            granularity=args.granularity,
            sweep_buffer_pips=buffer_pips,
            reentry_deadline_minutes=reentry_min,
            include_spread=True,
            progress_every=args.progress_every,
            sleep_seconds_between_requests=args.sleep,
            max_requests=10_000,
            cache_dir=cache_dir,
        )

        metrics = analyze_run(csv_path)
        score = score_run(metrics, min_samples=args.min_samples)

        summary_rows.append(
            {
                "instrument": args.instrument,
                "days": str(args.days),
                "buffer_pips": f"{buffer_pips:g}",
                "reentry_minutes": str(reentry_min),
                "score": "" if score == float("-inf") else f"{score:.4f}",
                "n_mr_reentry": str(metrics.n_mr_reentry),
                "net_p50": f"{metrics.net_p50:.3f}",
                "net_mean": f"{metrics.net_mean:.3f}",
                "p_net_gt_0": f"{metrics.p_net_gt_0:.3f}",
                "p_net_gt_2": f"{metrics.p_net_gt_2:.3f}",
                "p_net_gt_5": f"{metrics.p_net_gt_5:.3f}",
                "adverse_p95": f"{metrics.adverse_p95:.3f}",
                "spread_reentry_p50": f"{metrics.spread_reentry_p50:.3f}",
                "spread_reentry_p90": f"{metrics.spread_reentry_p90:.3f}",
                "csv_path": str(csv_path),
            }
        )

    def _score_key(r: Dict[str, str]) -> float:
        s = r.get("score", "")
        return float(s) if s else float("-inf")

    summary_rows.sort(key=_score_key, reverse=True)

    summary_path = Path(args.summary)
    summary_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = list(summary_rows[0].keys()) if summary_rows else []
    with summary_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(summary_rows)

    print("\n=== TOP CONFIGS ===")
    for r in summary_rows[: args.top]:
        print(
            f"score={r['score']:>8} buf={r['buffer_pips']:>4} re={r['reentry_minutes']:>3} "
            f"nMR={r['n_mr_reentry']:>3} net_p50={r['net_p50']:>6} "
            f"P(net>2)={r['p_net_gt_2']:>5} adverse_p95={r['adverse_p95']:>6} "
            f"csv={r['csv_path']}"
        )

    print(f"\nWrote summary: {summary_path}")
    print(f"Cache dir: {cache_dir if cache_dir else '(disabled)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
