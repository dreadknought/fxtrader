# file: src/simulate_r_matrix.py
"""
R-matrix simulator for the London sweep research project.

Reads a labels CSV (e.g. out/grid/EUR_USD_d250_buf3_re10.csv), loads per-day BID+ASK
candles from the disk cache, synthesizes mid-like candles, and simulates trade outcomes.

Models:
  1) fixed stop (pip stop distance)
  2) structure stop (stop beyond sweep extreme +/- buffer)

For each model we simulate:
  - horizons (minutes)
  - targets (R multiples)
  - fixed-stops list (pips) [fixed model only]

Key improvement vs prior version:
  Intrabar ambiguity handling:
    When BOTH TP and SL are within the same 1-minute candle, we cannot know which hit first.
    We report three variants:
      - worst_case: SL first
      - best_case:  TP first
      - neutral:    counts as 0.5 TP and 0.5 SL

Output:
  - CSV summary (tp/sl/none counts and rates) written to --out-summary
  - Prints top rows by tp_rate for each ambiguity mode

Assumptions:
  - Uses MID (synthetic) highs/lows to check barrier touches.
  - Entry at synthetic mid close at the entry candle.
  - No additional slippage beyond what’s implicit in mid pricing (this simulator is about
    stop/target feasibility, not execution modeling). You can layer costs later.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")

PIP_VALUE = 0.0001  # EUR_USD


@dataclass(frozen=True)
class Ohlc:
    o: float
    h: float
    l: float
    c: float


@dataclass(frozen=True)
class CandleSeries:
    ts: List[datetime]          # UTC timestamps (ascending)
    mid_high: List[float]
    mid_low: List[float]
    mid_close: List[float]
    spread_pips: List[float]    # spread at close (ask_close - bid_close)


@dataclass
class OutcomeCounts:
    """
    We store fractional counts to support the neutral intrabar assumption.
    """
    n: float = 0.0
    tp: float = 0.0
    sl: float = 0.0
    none: float = 0.0

    def add(self, outcome: str) -> None:
        self.n += 1.0
        if outcome == "TP":
            self.tp += 1.0
        elif outcome == "SL":
            self.sl += 1.0
        else:
            self.none += 1.0

    def add_neutral_intrabar(self) -> None:
        """
        Used when both TP and SL are inside the same candle.
        """
        self.n += 1.0
        self.tp += 0.5
        self.sl += 0.5

    def tp_rate(self) -> float:
        return self.tp / self.n if self.n else float("nan")

    def sl_rate(self) -> float:
        return self.sl / self.n if self.n else float("nan")


def _parse_oanda_ohlc_map(payload: dict, price_key: str) -> Dict[datetime, Ohlc]:
    out: Dict[datetime, Ohlc] = {}
    for c in payload.get("candles", []):
        if not c.get("complete", True):
            continue
        ts = datetime.fromisoformat(c["time"].replace("Z", "+00:00")).astimezone(UTC_TZ)
        p = c[price_key]
        out[ts] = Ohlc(o=float(p["o"]), h=float(p["h"]), l=float(p["l"]), c=float(p["c"]))
    return out


def _load_cached_payload(
    cache_dir: Path,
    oanda_env: str,
    instrument: str,
    granularity: str,
    trade_date_ny: str,
    price: str,
) -> dict:
    path = cache_dir / oanda_env / instrument / granularity / trade_date_ny / f"{price}.json"
    obj = json.loads(path.read_text())
    return obj["payload"]


def _build_series_from_bid_ask(bid_payload: dict, ask_payload: dict) -> CandleSeries:
    bid = _parse_oanda_ohlc_map(bid_payload, "bid")
    ask = _parse_oanda_ohlc_map(ask_payload, "ask")
    common = sorted(set(bid.keys()) & set(ask.keys()))

    ts: List[datetime] = []
    mid_high: List[float] = []
    mid_low: List[float] = []
    mid_close: List[float] = []
    spread_pips: List[float] = []

    for t in common:
        b = bid[t]
        a = ask[t]
        ts.append(t)
        mid_high.append((b.h + a.h) / 2.0)
        mid_low.append((b.l + a.l) / 2.0)
        mid_close.append((b.c + a.c) / 2.0)
        spread_pips.append((a.c - b.c) / PIP_VALUE)

    return CandleSeries(ts=ts, mid_high=mid_high, mid_low=mid_low, mid_close=mid_close, spread_pips=spread_pips)


def _to_float(s: str) -> Optional[float]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_dt(s: str) -> Optional[datetime]:
    s = (s or "").strip()
    if not s:
        return None
    return datetime.fromisoformat(s)


def _find_index_at_or_after(series: CandleSeries, t_utc: datetime) -> Optional[int]:
    for i, ts in enumerate(series.ts):
        if ts >= t_utc:
            return i
    return None


def _window_end_index(series: CandleSeries, start_idx: int, horizon_minutes: int) -> int:
    start_ts = series.ts[start_idx]
    end_ts = start_ts + timedelta(minutes=horizon_minutes)
    end_idx = start_idx
    for j in range(start_idx, len(series.ts)):
        if series.ts[j] >= end_ts:
            break
        end_idx = j
    return end_idx


def _intrabar_flags(
    *,
    direction: str,  # "LONG" | "SHORT"
    candle_hi: float,
    candle_lo: float,
    stop_price: float,
    target_price: float,
) -> Tuple[bool, bool]:
    """
    Returns (stop_touched, target_touched) within a candle using hi/lo.
    """
    if direction == "LONG":
        stop_touched = candle_lo <= stop_price
        target_touched = candle_hi >= target_price
    else:
        stop_touched = candle_hi >= stop_price
        target_touched = candle_lo <= target_price
    return stop_touched, target_touched


def _simulate_fixed_stop_all_ambiguity_modes(
    *,
    series: CandleSeries,
    entry_idx: int,
    direction: str,  # "LONG" | "SHORT"
    stop_pips: float,
    target_r: float,
    horizon_minutes: int,
) -> Tuple[str, str, str]:
    """
    Returns (worst_case, best_case, neutral_case) outcomes among {"TP","SL","NONE","NEUTRAL_INTRABAR"}.
    Neutral case returns "NEUTRAL_INTRABAR" when both touched in same candle.
    """
    entry = series.mid_close[entry_idx]
    stop_dist = stop_pips * PIP_VALUE
    target_dist = stop_pips * target_r * PIP_VALUE

    if direction == "LONG":
        stop_price = entry - stop_dist
        target_price = entry + target_dist
    else:
        stop_price = entry + stop_dist
        target_price = entry - target_dist

    end_idx = _window_end_index(series, entry_idx, horizon_minutes)

    for i in range(entry_idx, end_idx + 1):
        hi = series.mid_high[i]
        lo = series.mid_low[i]

        stop_hit, tp_hit = _intrabar_flags(
            direction=direction,
            candle_hi=hi,
            candle_lo=lo,
            stop_price=stop_price,
            target_price=target_price,
        )

        if stop_hit and tp_hit:
            # unknown ordering
            return "SL", "TP", "NEUTRAL_INTRABAR"
        if stop_hit:
            return "SL", "SL", "SL"
        if tp_hit:
            return "TP", "TP", "TP"

    return "NONE", "NONE", "NONE"


def _compute_sweep_extreme(
    *,
    series: CandleSeries,
    london_start_ny: datetime,
    london_end_ny: datetime,
    asia_high: float,
    asia_low: float,
    buffer_pips: float,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Returns (sweep_high_extreme, sweep_low_extreme) within London window:
      - sweep_high_extreme: max mid_high among candles that exceed asia_high+buffer
      - sweep_low_extreme: min mid_low among candles that go below asia_low-buffer
    """
    buf = buffer_pips * PIP_VALUE
    upper = asia_high + buf
    lower = asia_low - buf

    sweep_high: Optional[float] = None
    sweep_low: Optional[float] = None

    for i, ts_utc in enumerate(series.ts):
        ts_ny = ts_utc.astimezone(NY_TZ)
        if ts_ny < london_start_ny or ts_ny >= london_end_ny:
            continue

        if series.mid_high[i] > upper:
            sweep_high = series.mid_high[i] if sweep_high is None else max(sweep_high, series.mid_high[i])
        if series.mid_low[i] < lower:
            sweep_low = series.mid_low[i] if sweep_low is None else min(sweep_low, series.mid_low[i])

    return sweep_high, sweep_low


def _simulate_structure_stop_all_ambiguity_modes(
    *,
    series: CandleSeries,
    entry_idx: int,
    sweep_side: str,  # "HIGH" or "LOW"
    sweep_high_extreme: Optional[float],
    sweep_low_extreme: Optional[float],
    buffer_pips: float,
    target_r: float,
    horizon_minutes: int,
) -> Optional[Tuple[str, str, str]]:
    """
    Stop is placed beyond the sweep extreme +/- buffer.
    Returns None if structure stop can't be formed (missing sweep extreme or stop_pips<=0).
    Otherwise returns outcomes (worst,best,neutral) where neutral can be "NEUTRAL_INTRABAR".
    """
    entry = series.mid_close[entry_idx]
    buf = buffer_pips * PIP_VALUE

    if sweep_side == "HIGH":
        # short after high sweep; stop above sweep_high_extreme + buffer
        if sweep_high_extreme is None:
            return None
        stop_price = sweep_high_extreme + buf
        stop_pips = (stop_price - entry) / PIP_VALUE
        if stop_pips <= 0:
            return None
        target_price = entry - (stop_pips * target_r * PIP_VALUE)
        direction = "SHORT"
    else:
        # long after low sweep; stop below sweep_low_extreme - buffer
        if sweep_low_extreme is None:
            return None
        stop_price = sweep_low_extreme - buf
        stop_pips = (entry - stop_price) / PIP_VALUE
        if stop_pips <= 0:
            return None
        target_price = entry + (stop_pips * target_r * PIP_VALUE)
        direction = "LONG"

    end_idx = _window_end_index(series, entry_idx, horizon_minutes)

    for i in range(entry_idx, end_idx + 1):
        hi = series.mid_high[i]
        lo = series.mid_low[i]

        stop_hit, tp_hit = _intrabar_flags(
            direction=direction,
            candle_hi=hi,
            candle_lo=lo,
            stop_price=stop_price,
            target_price=target_price,
        )

        if stop_hit and tp_hit:
            return ("SL", "TP", "NEUTRAL_INTRABAR")
        if stop_hit:
            return ("SL", "SL", "SL")
        if tp_hit:
            return ("TP", "TP", "TP")

    return ("NONE", "NONE", "NONE")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels-csv", required=True, help="Labels CSV (e.g. out/grid/EUR_USD_d250_buf3_re10.csv)")
    ap.add_argument("--cache-dir", default="out/candle_cache")
    ap.add_argument("--oanda-env", default="practice")
    ap.add_argument("--instrument", default="EUR_USD")
    ap.add_argument("--granularity", default="M1")

    ap.add_argument("--horizons", default="30,60,90,180", help="Comma minutes (default: 30,60,90,180)")
    ap.add_argument("--targets", default="1,2", help="Comma R values (default: 1,2)")
    ap.add_argument("--fixed-stops", default="8,10,12,15,20,25", help="Comma pip stops for fixed model")

    ap.add_argument("--out-summary", default="out/r_matrix_summary.csv")
    ap.add_argument("--top", type=int, default=15)

    args = ap.parse_args()

    labels_path = Path(args.labels_csv)
    cache_dir = Path(args.cache_dir)

    horizons = [int(x.strip()) for x in args.horizons.split(",") if x.strip()]
    targets = [float(x.strip()) for x in args.targets.split(",") if x.strip()]
    fixed_stops = [float(x.strip()) for x in args.fixed_stops.split(",") if x.strip()]

    with labels_path.open("r", newline="") as f:
        labels_rows = list(csv.DictReader(f))

    if not labels_rows:
        raise SystemExit(f"No rows found in {labels_path}")

    # infer buffer from CSV (assume consistent per file)
    inferred_buf = _to_float(labels_rows[0].get("sweep_buffer_pips", ""))
    buffer_pips = inferred_buf if inferred_buf is not None else 2.0

    # stats maps:
    # key includes ambiguity mode
    #   <mode>|<model>|h<horizon>|R<R>|stop<stop>
    stats: Dict[str, OutcomeCounts] = {}

    def bump(key: str, outcome: str) -> None:
        if key not in stats:
            stats[key] = OutcomeCounts()
        if outcome == "NEUTRAL_INTRABAR":
            stats[key].add_neutral_intrabar()
        else:
            stats[key].add(outcome)

    for r in labels_rows:
        if (r.get("day_class") or "").strip() != "MEAN_REVERSION":
            continue

        trade_date = (r.get("date_ny") or "").strip()
        if not trade_date:
            continue

        reentry_time_ny = _to_dt(r.get("reentry_time_ny", ""))
        sweep_side = (r.get("first_sweep_side") or "").strip()
        asia_high = _to_float(r.get("asia_high", ""))
        asia_low = _to_float(r.get("asia_low", ""))

        if reentry_time_ny is None or sweep_side not in ("HIGH", "LOW") or asia_high is None or asia_low is None:
            continue

        # Load candles from cache
        bid_payload = _load_cached_payload(cache_dir, args.oanda_env, args.instrument, args.granularity, trade_date, "B")
        ask_payload = _load_cached_payload(cache_dir, args.oanda_env, args.instrument, args.granularity, trade_date, "A")
        series = _build_series_from_bid_ask(bid_payload, ask_payload)

        entry_idx = _find_index_at_or_after(series, reentry_time_ny.astimezone(UTC_TZ))
        if entry_idx is None:
            continue

        direction = "SHORT" if sweep_side == "HIGH" else "LONG"

        # London window (03:00-05:00 NY)
        trade_day_ny = datetime.fromisoformat(trade_date + "T00:00:00").replace(tzinfo=NY_TZ)
        london_start_ny = trade_day_ny.replace(hour=3, minute=0)
        london_end_ny = trade_day_ny.replace(hour=5, minute=0)

        sweep_high_ext, sweep_low_ext = _compute_sweep_extreme(
            series=series,
            london_start_ny=london_start_ny,
            london_end_ny=london_end_ny,
            asia_high=asia_high,
            asia_low=asia_low,
            buffer_pips=buffer_pips,
        )

        for horizon in horizons:
            for R in targets:
                # fixed stop model
                for stop_pips in fixed_stops:
                    worst, best, neutral = _simulate_fixed_stop_all_ambiguity_modes(
                        series=series,
                        entry_idx=entry_idx,
                        direction=direction,
                        stop_pips=stop_pips,
                        target_r=R,
                        horizon_minutes=horizon,
                    )

                    bump(f"worst|fixed|h{horizon}|R{R:g}|stop{stop_pips:g}", worst)
                    bump(f"best|fixed|h{horizon}|R{R:g}|stop{stop_pips:g}", best)
                    bump(f"neutral|fixed|h{horizon}|R{R:g}|stop{stop_pips:g}", neutral)

                # structure stop model
                out = _simulate_structure_stop_all_ambiguity_modes(
                    series=series,
                    entry_idx=entry_idx,
                    sweep_side=sweep_side,
                    sweep_high_extreme=sweep_high_ext,
                    sweep_low_extreme=sweep_low_ext,
                    buffer_pips=buffer_pips,
                    target_r=R,
                    horizon_minutes=horizon,
                )
                if out is not None:
                    worst2, best2, neutral2 = out
                    bump(f"worst|struct|h{horizon}|R{R:g}|stopSTRUCT", worst2)
                    bump(f"best|struct|h{horizon}|R{R:g}|stopSTRUCT", best2)
                    bump(f"neutral|struct|h{horizon}|R{R:g}|stopSTRUCT", neutral2)

    # Build output rows
    out_rows: List[Dict[str, str]] = []
    for key, c in stats.items():
        mode, model, hpart, rpart, stop_part = key.split("|")
        horizon_min = hpart[1:]
        R = rpart[1:]
        stop = stop_part.replace("stop", "")

        out_rows.append(
            {
                "ambiguity_mode": mode,
                "model": model,
                "horizon_min": horizon_min,
                "R": R,
                "stop": stop,
                "n": f"{c.n:.1f}",
                "tp": f"{c.tp:.1f}",
                "sl": f"{c.sl:.1f}",
                "none": f"{c.none:.1f}",
                "tp_rate": f"{c.tp_rate():.3f}",
                "sl_rate": f"{c.sl_rate():.3f}",
            }
        )

    # Sort within each mode by tp_rate desc
    def _tp_rate(row: Dict[str, str]) -> float:
        try:
            return float(row["tp_rate"])
        except Exception:
            return float("-inf")

    out_rows.sort(key=_tp_rate, reverse=True)

    out_path = Path(args.out_summary)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "ambiguity_mode",
        "model",
        "horizon_min",
        "R",
        "stop",
        "n",
        "tp",
        "sl",
        "none",
        "tp_rate",
        "sl_rate",
    ]
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)

    print(f"Wrote: {out_path}")

    # Print top N per ambiguity mode
    for mode in ("worst", "neutral", "best"):
        print(f"\nTop {args.top} by tp_rate (mode={mode})")
        shown = 0
        for r in out_rows:
            if r["ambiguity_mode"] != mode:
                continue
            print(
                f"{r['model']:>6} h={r['horizon_min']:>3} R={r['R']:>3} stop={r['stop']:>7} "
                f"n={r['n']:>6} tp_rate={r['tp_rate']} sl_rate={r['sl_rate']} none={r['none']}"
            )
            shown += 1
            if shown >= args.top:
                break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())