# file: src/analyze_stratified_overlap.py
from __future__ import annotations

import argparse
import csv
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd

NY_TZ = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")

# Matches src/analyze_overlap.py (EUR_USD)
PIP_VALUE = 0.0001


@dataclass(frozen=True)
class Ohlc:
    h: float
    l: float
    c: float


@dataclass(frozen=True)
class CandleSeries:
    ts: List[datetime]  # UTC timestamps ascending
    mid_high: List[float]
    mid_low: List[float]
    mid_close: List[float]


@dataclass(frozen=True)
class BinEdges:
    asia: List[float]
    sweep: List[float]
    spread: List[float]


def _to_bool(x) -> bool:
    if pd.isna(x):
        return False
    if isinstance(x, bool):
        return x
    s = str(x).strip().lower()
    if s in ("1", "true", "t", "yes", "y"):
        return True
    if s in ("0", "false", "f", "no", "n"):
        return False
    try:
        return float(s) != 0.0
    except Exception:
        return bool(s)


def _load_cached_payload(
    cache_dir: Path,
    oanda_env: str,
    instrument: str,
    granularity: str,
    trade_date_ny: str,
    price: str,
) -> dict:
    p = cache_dir / oanda_env / instrument / granularity / trade_date_ny / f"{price}.json"
    obj = json.loads(p.read_text())
    return obj["payload"]


def _parse_ohlc_map(payload: dict, price_key: str) -> Dict[datetime, Ohlc]:
    out: Dict[datetime, Ohlc] = {}
    for c in payload.get("candles", []):
        if not c.get("complete", True):
            continue
        ts = datetime.fromisoformat(c["time"].replace("Z", "+00:00")).astimezone(UTC_TZ)
        p = c[price_key]
        out[ts] = Ohlc(h=float(p["h"]), l=float(p["l"]), c=float(p["c"]))
    return out


def _build_mid_series(bid_payload: dict, ask_payload: dict) -> CandleSeries:
    bid = _parse_ohlc_map(bid_payload, "bid")
    ask = _parse_ohlc_map(ask_payload, "ask")
    common = sorted(set(bid.keys()) & set(ask.keys()))

    ts: List[datetime] = []
    mid_high: List[float] = []
    mid_low: List[float] = []
    mid_close: List[float] = []

    for t in common:
        b = bid[t]
        a = ask[t]
        ts.append(t)
        mid_high.append((b.h + a.h) / 2.0)
        mid_low.append((b.l + a.l) / 2.0)
        mid_close.append((b.c + a.c) / 2.0)

    return CandleSeries(ts=ts, mid_high=mid_high, mid_low=mid_low, mid_close=mid_close)


def _compute_sweep_extreme_0300_0500_ny(
    *,
    series: CandleSeries,
    trade_date_ny: str,
    asia_high: float,
    asia_low: float,
    buffer_pips: float,
) -> Tuple[Optional[float], Optional[float]]:
    """
    EXACTLY matches src/analyze_overlap.py:
      - London window fixed at 03:00–05:00 NY
      - uses mid highs/lows
      - triggers beyond (Asia boundary ± buffer)
    Returns (sweep_high_extreme, sweep_low_extreme).
    """
    buf = buffer_pips * PIP_VALUE
    upper = asia_high + buf
    lower = asia_low - buf

    trade_day_ny = datetime.fromisoformat(trade_date_ny + "T00:00:00").replace(tzinfo=NY_TZ)
    london_start_ny = trade_day_ny.replace(hour=3, minute=0)
    london_end_ny = trade_day_ny.replace(hour=5, minute=0)

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


def _quantile_edges(series: pd.Series, bins: int) -> List[float]:
    s = series.dropna().astype(float)
    if s.empty:
        return [-math.inf, math.inf]
    qs = [i / bins for i in range(1, bins)]
    edges = [float(s.quantile(q)) for q in qs]

    out = [-math.inf]
    for e in edges:
        if e <= out[-1]:
            e = out[-1] + 1e-9
        out.append(e)
    out.append(math.inf)
    return out


def _bucket(series: pd.Series, edges: List[float], bins: int) -> pd.Series:
    labels = [f"Q{i+1}" for i in range(bins)]
    return pd.cut(series.astype(float), bins=edges, labels=labels, include_lowest=True, right=True)


def _ev(series: pd.Series) -> float:
    """
    Accepts either numeric outcomes in {-1,0,+1} OR string outcomes:
      TP -> +1
      SL -> -1
      NONE -> 0
    """
    if series is None or len(series) == 0:
        return float("nan")

    def to_r(x):
        if pd.isna(x):
            return 0.0
        # already numeric?
        if isinstance(x, (int, float)) and not isinstance(x, bool):
            return float(x)
        s = str(x).strip().upper()
        if s in ("TP", "+1", "WIN"):
            return 1.0
        if s in ("SL", "-1", "LOSS"):
            return -1.0
        if s in ("NONE", "0", "NO", "N", ""):
            return 0.0
        # last resort: try numeric parse
        try:
            return float(s)
        except Exception:
            # unknown token -> treat as 0 so it doesn't crash
            return 0.0

    vals = series.map(to_r).astype(float)
    if vals.empty:
        return float("nan")
    return float(vals.mean())

def _load_overlap_outcomes_day_level(overlap_csv: Path, which_outcome: str) -> pd.DataFrame:
    """
    overlap CSV is one row per (date_ny, pool). Presence implies pool signaled.
    Creates one row per date with:
      - group: overlap / a_only / b_only / neither
      - a_outcome: outcome_* from pool A row
      - a_signals, b_signals, both_signals
    """
    df = pd.read_csv(overlap_csv)
    required = {"date_ny", "pool", f"outcome_{which_outcome}", "both_signals"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"overlap_outcomes missing columns: {sorted(missing)}. Got: {df.columns.tolist()}")

    df["_pool"] = df["pool"].astype(str).str.upper().str.strip()
    df["_both"] = df["both_signals"].apply(_to_bool)

    pools_by_date = df.groupby("date_ny")["_pool"].apply(lambda s: set(s.tolist()))
    day = pd.DataFrame({"date_ny": pools_by_date.index})
    day["a_signals"] = ["A" in ss for ss in pools_by_date.values]
    day["b_signals"] = ["B" in ss for ss in pools_by_date.values]
    day["both_signals"] = df.groupby("date_ny")["_both"].any().reindex(day["date_ny"]).values

    def _group(row) -> str:
        a = bool(row["a_signals"])
        b = bool(row["b_signals"])
        if a and b:
            return "overlap"
        if a and not b:
            return "a_only"
        if b and not a:
            return "b_only"
        return "neither"

    day["group"] = day.apply(_group, axis=1)

    a_rows = df[df["_pool"] == "A"][["date_ny", f"outcome_{which_outcome}"]].copy()
    a_rows = a_rows.rename(columns={f"outcome_{which_outcome}": "a_outcome"}).drop_duplicates(subset=["date_ny"])
    day = day.merge(a_rows, on="date_ny", how="left")

    return day


def _load_labels(labels_csv: Path) -> pd.DataFrame:
    df = pd.read_csv(labels_csv)
    required = {
        "date_ny",
        "instrument",
        "sweep_buffer_pips",
        "asia_high",
        "asia_low",
        "first_sweep_side",
        "spread_at_reentry_pips",
        "spread_p95_5m_after_reentry_pips",
    }
    missing = required - set(df.columns)
    if missing:
        raise ValueError(
            f"labels-a missing columns: {sorted(missing)}. "
            f"Regenerate with --include-spread. Got: {df.columns.tolist()}"
        )
    df = df.drop_duplicates(subset=["date_ny"]).set_index("date_ny")
    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--overlap-outcomes", required=True, help="CSV from src.analyze_overlap --out-csv")
    ap.add_argument("--labels-a", required=True, help="A labels CSV (spread-enabled)")
    ap.add_argument("--cache-dir", required=True, help="Candle cache dir (e.g., out/candle_cache)")
    ap.add_argument("--oanda-env", required=True, help="practice or live (matches cache path)")
    ap.add_argument("--instrument", required=True, help="e.g., EUR_USD")
    ap.add_argument("--granularity", default="M1", help="e.g., M1 (matches cache path)")
    ap.add_argument("--which-outcome", choices=["worst", "neutral", "best"], default="neutral")
    ap.add_argument("--spread-metric", choices=["p95_5m", "at_reentry"], default="p95_5m")
    ap.add_argument("--bins", type=int, default=3)
    ap.add_argument("--out-per-day", default="out/stratified_per_day.csv")
    ap.add_argument("--out-csv", default="out/stratified_ev.csv")
    args = ap.parse_args()

    overlap_csv = Path(args.overlap_outcomes)
    labels_a_csv = Path(args.labels_a)
    cache_dir = Path(args.cache_dir)

    day = _load_overlap_outcomes_day_level(overlap_csv, args.which_outcome)
    labels = _load_labels(labels_a_csv)

    # Merge day-level groups/outcomes with A label features
    merged = day.set_index("date_ny").join(labels, how="left")

    # Asia range in pips (EUR_USD pip size per repo)
    merged["asia_range_pips"] = (merged["asia_high"].astype(float) - merged["asia_low"].astype(float)) / PIP_VALUE

    # Spread metric selection
    if args.spread_metric == "p95_5m":
        merged["spread_metric"] = merged["spread_p95_5m_after_reentry_pips"].astype(float)
    else:
        merged["spread_metric"] = merged["spread_at_reentry_pips"].astype(float)

    # Compute sweep depth (pips) using the SAME sweep window/extreme logic as analyze_overlap.py.
    # Definition:
    #   depth_high = max(0, sweep_high_extreme - (asia_high + buf))
    #   depth_low  = max(0, (asia_low - buf) - sweep_low_extreme)
    # Use first_sweep_side to select which side depth to use.
    sweep_depths: List[Optional[float]] = []
    miss_count = 0

    for trade_date_ny, r in merged.iterrows():
        try:
            asia_high = float(r["asia_high"])
            asia_low = float(r["asia_low"])
            buf_pips = float(r["sweep_buffer_pips"])
        except Exception:
            sweep_depths.append(float("nan"))
            continue

        try:
            bid_payload = _load_cached_payload(cache_dir, args.oanda_env, args.instrument, args.granularity, trade_date_ny, "B")
            ask_payload = _load_cached_payload(cache_dir, args.oanda_env, args.instrument, args.granularity, trade_date_ny, "A")
        except Exception:
            # cache miss / file missing
            miss_count += 1
            sweep_depths.append(float("nan"))
            continue

        series = _build_mid_series(bid_payload, ask_payload)
        sweep_high, sweep_low = _compute_sweep_extreme_0300_0500_ny(
            series=series,
            trade_date_ny=trade_date_ny,
            asia_high=asia_high,
            asia_low=asia_low,
            buffer_pips=buf_pips,
        )

        upper = asia_high + buf_pips * PIP_VALUE
        lower = asia_low - buf_pips * PIP_VALUE

        depth_high = 0.0
        depth_low = 0.0
        if sweep_high is not None and sweep_high > upper:
            depth_high = (sweep_high - upper) / PIP_VALUE
        if sweep_low is not None and sweep_low < lower:
            depth_low = (lower - sweep_low) / PIP_VALUE

        side = str(r.get("first_sweep_side") or "").strip().upper()
        if side == "HIGH":
            sweep_depths.append(depth_high)
        elif side == "LOW":
            sweep_depths.append(depth_low)
        else:
            # If side unknown, use max depth as a proxy
            sweep_depths.append(max(depth_high, depth_low))

    merged["sweep_depth_pips"] = pd.Series(sweep_depths, index=merged.index)

    # Build quantile bins (computed over rows where the feature exists)
    edges = BinEdges(
        asia=_quantile_edges(merged["asia_range_pips"], args.bins),
        sweep=_quantile_edges(merged["sweep_depth_pips"], args.bins),
        spread=_quantile_edges(merged["spread_metric"], args.bins),
    )

    merged["asia_bin"] = _bucket(merged["asia_range_pips"], edges.asia, args.bins)
    merged["sweep_bin"] = _bucket(merged["sweep_depth_pips"], edges.sweep, args.bins)
    merged["spread_bin"] = _bucket(merged["spread_metric"], edges.spread, args.bins)

    # Write per-day merged
    out_per_day = Path(args.out_per_day)
    out_per_day.parent.mkdir(parents=True, exist_ok=True)
    merged.reset_index().to_csv(out_per_day, index=False)

    # Aggregate EV of A outcomes by group + bins
    # Only consider days where A outcome exists (A signaled)
    filt = merged["a_outcome"].notna()
    agg = (
        merged[filt]
        .groupby(["group", "asia_bin", "sweep_bin", "spread_bin"], dropna=False)["a_outcome"]
        .agg(n="count", ev=_ev)
        .reset_index()
        .sort_values(["ev", "n"], ascending=[False, False])
    )

    out_csv = Path(args.out_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    agg.to_csv(out_csv, index=False)

    print("=== Stratified bins ===")
    print(f"Instrument:  {args.instrument} (pip={PIP_VALUE})")
    print(f"Asia edges:  {edges.asia}")
    print(f"Sweep edges: {edges.sweep}")
    print(f"Spread edges:{edges.spread}")
    if miss_count:
        print(f"[WARN] Missing cache for {miss_count} dates (sweep_depth_pips set to NaN).")
    print()
    print("=== Top buckets (A outcome EV) ===")
    for grp in ["a_only", "overlap", "b_only"]:
        sub = agg[agg["group"] == grp].head(10)
        if sub.empty:
            continue
        print(f"\n-- {grp} --")
        print(sub.to_string(index=False))


if __name__ == "__main__":
    main()