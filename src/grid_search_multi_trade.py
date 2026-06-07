# file: src/grid_search_multi_trade.py
from __future__ import annotations

import argparse
import csv
import math
from pathlib import Path
from typing import Dict, List, Optional

from src.backtest_multi_trade import run_multi_trade_backtest


def _parse_float_list(raw: str) -> List[float]:
    return [float(x.strip()) for x in raw.split(",") if x.strip()]


def _parse_int_list(raw: str) -> List[int]:
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def _parse_bool_list(raw: str) -> List[bool]:
    vals: List[bool] = []
    for x in raw.split(","):
        s = x.strip().lower()
        if not s:
            continue
        if s in {"1", "true", "t", "yes", "y", "on"}:
            vals.append(True)
        elif s in {"0", "false", "f", "no", "n", "off"}:
            vals.append(False)
        else:
            raise ValueError(f"Invalid bool list value: {x!r}")
    return vals


def _score_row(row: Dict[str, str], *, min_trades: int) -> float:
    total_trades = int(row["total_trades"])
    if total_trades < min_trades:
        return float("-inf")

    expectancy_r = float(row["expectancy_r"])
    median_r = float(row["median_r"])
    positive_day_rate = float(row["positive_day_rate"])
    max_drawdown_r = float(row["max_drawdown_r"])
    max_consecutive_losers = int(row["max_consecutive_losers"])
    overlap_trade_rate = float(row["overlap_trade_rate"])

    return (
        (1.5 * expectancy_r)
        + (0.7 * median_r)
        + (0.8 * positive_day_rate)
        - (0.08 * max_drawdown_r)
        - (0.2 * max_consecutive_losers)
        - (0.15 * overlap_trade_rate)
    )


def _write_csv(rows: List[Dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", default="EUR_USD")
    ap.add_argument("--days", type=int, default=250)
    ap.add_argument("--granularity", default="M1")
    ap.add_argument("--cache-dir", default="out/candle_cache")
    ap.add_argument("--out-dir", default="out/grid_multi")
    ap.add_argument("--summary", default="out/grid_multi_summary.csv")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--min-trades", type=int, default=40)
    ap.add_argument("--progress-every", type=int, default=25)
    ap.add_argument("--sleep", type=float, default=0.05)

    ap.add_argument("--buffers", default="1")
    ap.add_argument("--reentries", default="10")
    ap.add_argument("--max-sweep-depths", default="8")
    ap.add_argument("--min-asia-ranges", default="22.5")
    ap.add_argument("--stop-pips", type=float, default=10.0)
    ap.add_argument("--tp-pips", type=float, default=10.0)
    ap.add_argument("--time-stop-minutes", type=int, default=90)

    ap.add_argument("--max-trades-per-day", default="1,2,3")
    ap.add_argument("--cooldowns", default="0,5,10")
    ap.add_argument("--allow-same-side-repeat", default="false,true")
    ap.add_argument("--opposite-side-only-after-close", default="false,true")
    ap.add_argument("--allow-overlapping-trades", default="false,true")
    args = ap.parse_args()

    buffers = _parse_float_list(args.buffers)
    reentries = _parse_int_list(args.reentries)
    max_sweep_depths = _parse_float_list(args.max_sweep_depths)
    min_asia_ranges = _parse_float_list(args.min_asia_ranges)
    max_trades_per_day_vals = _parse_int_list(args.max_trades_per_day)
    cooldowns = _parse_int_list(args.cooldowns)
    allow_same_side_repeat_vals = _parse_bool_list(args.allow_same_side_repeat)
    opposite_side_only_vals = _parse_bool_list(args.opposite_side_only_after_close)
    allow_overlap_vals = _parse_bool_list(args.allow_overlapping_trades)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    combos = []
    for buffer_pips in buffers:
        for reentry in reentries:
            for max_sweep_depth in max_sweep_depths:
                for min_asia_range in min_asia_ranges:
                    for max_trades_per_day in max_trades_per_day_vals:
                        for cooldown in cooldowns:
                            for allow_same_side_repeat in allow_same_side_repeat_vals:
                                for (
                                    opposite_side_only_after_close
                                ) in opposite_side_only_vals:
                                    for allow_overlapping_trades in allow_overlap_vals:
                                        combos.append(
                                            {
                                                "buffer_pips": buffer_pips,
                                                "reentry_minutes": reentry,
                                                "gate_max_sweep_depth_pips": max_sweep_depth,
                                                "gate_min_asia_range_pips": min_asia_range,
                                                "max_trades_per_day": max_trades_per_day,
                                                "cooldown_minutes": cooldown,
                                                "allow_same_side_repeat": allow_same_side_repeat,
                                                "opposite_side_only_after_close": opposite_side_only_after_close,
                                                "allow_overlapping_trades": allow_overlapping_trades,
                                            }
                                        )

    print(f"Running {len(combos)} multi-trade permutations")
    summary_rows: List[Dict[str, str]] = []

    for idx, combo in enumerate(combos, start=1):
        run_name = (
            f"{args.instrument}_d{args.days}_buf{combo['buffer_pips']:g}_re{combo['reentry_minutes']}"
            f"_sd{combo['gate_max_sweep_depth_pips']:g}_ar{combo['gate_min_asia_range_pips']:g}"
            f"_mtd{combo['max_trades_per_day']}_cd{combo['cooldown_minutes']}"
            f"_ssr{int(combo['allow_same_side_repeat'])}_opp{int(combo['opposite_side_only_after_close'])}"
            f"_ov{int(combo['allow_overlapping_trades'])}"
        )
        run_dir = out_dir / run_name
        print(f"[{idx}/{len(combos)}] {run_name}", flush=True)

        trade_rows, day_rows, summary = run_multi_trade_backtest(
            instrument=args.instrument,
            trading_days=args.days,
            granularity=args.granularity,
            sweep_buffer_pips=float(combo["buffer_pips"]),
            reentry_deadline_minutes=int(combo["reentry_minutes"]),
            gate_max_sweep_depth_pips=float(combo["gate_max_sweep_depth_pips"]),
            gate_min_asia_range_pips=float(combo["gate_min_asia_range_pips"]),
            stop_pips=args.stop_pips,
            tp_pips=args.tp_pips,
            time_stop_minutes=args.time_stop_minutes,
            max_trades_per_day=int(combo["max_trades_per_day"]),
            cooldown_minutes=int(combo["cooldown_minutes"]),
            allow_same_side_repeat=bool(combo["allow_same_side_repeat"]),
            opposite_side_only_after_close=bool(
                combo["opposite_side_only_after_close"]
            ),
            allow_overlapping_trades=bool(combo["allow_overlapping_trades"]),
            cache_dir=cache_dir,
            progress_every=args.progress_every,
            sleep_seconds_between_requests=args.sleep,
        )

        if trade_rows:
            _write_csv(trade_rows, run_dir / "trades.csv")
        if day_rows:
            _write_csv(day_rows, run_dir / "days.csv")

        row: Dict[str, str] = {
            "run_name": run_name,
            "instrument": args.instrument,
            "days": str(args.days),
            "granularity": args.granularity,
            "buffer_pips": f"{combo['buffer_pips']:g}",
            "reentry_minutes": str(combo["reentry_minutes"]),
            "gate_max_sweep_depth_pips": f"{combo['gate_max_sweep_depth_pips']:g}",
            "gate_min_asia_range_pips": f"{combo['gate_min_asia_range_pips']:g}",
            "stop_pips": f"{args.stop_pips:g}",
            "tp_pips": f"{args.tp_pips:g}",
            "time_stop_minutes": str(args.time_stop_minutes),
            "max_trades_per_day": str(combo["max_trades_per_day"]),
            "cooldown_minutes": str(combo["cooldown_minutes"]),
            "allow_same_side_repeat": str(combo["allow_same_side_repeat"]).lower(),
            "opposite_side_only_after_close": str(
                combo["opposite_side_only_after_close"]
            ).lower(),
            "allow_overlapping_trades": str(combo["allow_overlapping_trades"]).lower(),
            "total_signals": str(summary.total_signals),
            "total_trades": str(summary.total_trades),
            "trades_per_day": f"{summary.trades_per_day:.6f}",
            "win_rate": f"{summary.win_rate:.6f}",
            "expectancy_r": f"{summary.expectancy_r:.6f}",
            "median_r": f"{summary.median_r:.6f}",
            "positive_day_rate": f"{summary.positive_day_rate:.6f}",
            "max_drawdown_r": f"{summary.max_drawdown_r:.6f}",
            "max_consecutive_losers": str(summary.max_consecutive_losers),
            "overlap_trade_rate": f"{summary.overlap_trade_rate:.6f}",
            "total_pnl_pips": f"{summary.total_pnl_pips:.6f}",
            "total_r": f"{summary.total_r:.6f}",
            "api_calls": str(summary.api_calls),
            "cache_hits": str(summary.cache_hits),
            "cache_misses": str(summary.cache_misses),
            "trades_csv": str(run_dir / "trades.csv"),
            "days_csv": str(run_dir / "days.csv"),
        }
        score = _score_row(row, min_trades=args.min_trades)
        row["score"] = "" if score == float("-inf") else f"{score:.6f}"
        summary_rows.append(row)

    def _sort_key(r: Dict[str, str]) -> float:
        s = r.get("score", "")
        return float(s) if s else float("-inf")

    summary_rows.sort(key=_sort_key, reverse=True)
    summary_path = Path(args.summary)
    _write_csv(summary_rows, summary_path)

    print("\n=== TOP CONFIGS ===")
    for row in summary_rows[: args.top]:
        print(
            f"score={row['score']:>10} trades={row['total_trades']:>4} expR={row['expectancy_r']:>8} "
            f"medR={row['median_r']:>8} posDay={row['positive_day_rate']:>8} dd={row['max_drawdown_r']:>8} "
            f"mtd={row['max_trades_per_day']} cd={row['cooldown_minutes']} ov={row['allow_overlapping_trades']} "
            f"ssr={row['allow_same_side_repeat']} opp={row['opposite_side_only_after_close']} run={row['run_name']}"
        )

    print(f"\nWrote summary: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
