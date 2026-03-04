# file: src/analyze_overlap.py
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")

PIP_VALUE = 0.0001  # EUR_USD


@dataclass(frozen=True)
class LabelRow:
    date_ny: str
    day_class: str
    reentry_time_ny: Optional[datetime]
    first_sweep_side: str  # "HIGH" | "LOW" | ""
    asia_high: Optional[float]
    asia_low: Optional[float]
    sweep_buffer_pips: float


@dataclass(frozen=True)
class Ohlc:
    h: float
    l: float
    c: float


@dataclass(frozen=True)
class CandleSeries:
    ts: List[datetime]          # UTC timestamps ascending
    mid_high: List[float]
    mid_low: List[float]
    mid_close: List[float]


@dataclass
class OutcomeCounts:
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
        self.n += 1.0
        self.tp += 0.5
        self.sl += 0.5

    def ev_r(self) -> float:
        # for R=1: EV = TP - SL, NONE = 0
        return (self.tp - self.sl) / self.n if self.n else float("nan")

    def tp_rate(self) -> float:
        return self.tp / self.n if self.n else float("nan")

    def sl_rate(self) -> float:
        return self.sl / self.n if self.n else float("nan")


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


def _load_labels(path: Path) -> Dict[str, LabelRow]:
    out: Dict[str, LabelRow] = {}
    with path.open("r", newline="") as f:
        for r in csv.DictReader(f):
            date_ny = (r.get("date_ny") or "").strip()
            if not date_ny:
                continue
            out[date_ny] = LabelRow(
                date_ny=date_ny,
                day_class=(r.get("day_class") or "").strip(),
                reentry_time_ny=_to_dt(r.get("reentry_time_ny", "")),
                first_sweep_side=(r.get("first_sweep_side") or "").strip(),
                asia_high=_to_float(r.get("asia_high", "")),
                asia_low=_to_float(r.get("asia_low", "")),
                sweep_buffer_pips=_to_float(r.get("sweep_buffer_pips", "")) or 2.0,
            )
    return out


def _load_cached_payload(cache_dir: Path, oanda_env: str, instrument: str, granularity: str, trade_date_ny: str, price: str) -> dict:
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


def _compute_sweep_extreme(
    *,
    series: CandleSeries,
    trade_date_ny: str,
    asia_high: float,
    asia_low: float,
    buffer_pips: float,
) -> Tuple[Optional[float], Optional[float]]:
    """
    Uses the London window fixed at 03:00-05:00 NY for the trade date.
    Returns (sweep_high_extreme, sweep_low_extreme) using mid highs/lows.
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


def _intrabar_flags(direction: str, hi: float, lo: float, stop_price: float, target_price: float) -> Tuple[bool, bool]:
    if direction == "LONG":
        return (lo <= stop_price, hi >= target_price)
    return (hi >= stop_price, lo <= target_price)


def _simulate_structure_r1_h90(
    *,
    series: CandleSeries,
    entry_idx: int,
    sweep_side: str,  # "HIGH" or "LOW"
    sweep_high_extreme: Optional[float],
    sweep_low_extreme: Optional[float],
    buffer_pips: float,
    horizon_minutes: int = 90,
) -> Optional[Tuple[str, str, str]]:
    """
    Structure stop, R=1 only, for the given horizon.
    Returns (worst,best,neutral) outcomes.
    Neutral returns "NEUTRAL_INTRABAR" on a same-candle TP+SL touch.
    """
    entry = series.mid_close[entry_idx]
    buf = buffer_pips * PIP_VALUE

    if sweep_side == "HIGH":
        # short, stop above sweep high
        if sweep_high_extreme is None:
            return None
        stop_price = sweep_high_extreme + buf
        stop_pips = (stop_price - entry) / PIP_VALUE
        if stop_pips <= 0:
            return None
        target_price = entry - (stop_pips * 1.0 * PIP_VALUE)
        direction = "SHORT"
    elif sweep_side == "LOW":
        # long, stop below sweep low
        if sweep_low_extreme is None:
            return None
        stop_price = sweep_low_extreme - buf
        stop_pips = (entry - stop_price) / PIP_VALUE
        if stop_pips <= 0:
            return None
        target_price = entry + (stop_pips * 1.0 * PIP_VALUE)
        direction = "LONG"
    else:
        return None

    end_idx = _window_end_index(series, entry_idx, horizon_minutes)

    for i in range(entry_idx, end_idx + 1):
        hi = series.mid_high[i]
        lo = series.mid_low[i]
        stop_hit, tp_hit = _intrabar_flags(direction, hi, lo, stop_price, target_price)

        if stop_hit and tp_hit:
            return ("SL", "TP", "NEUTRAL_INTRABAR")
        if stop_hit:
            return ("SL", "SL", "SL")
        if tp_hit:
            return ("TP", "TP", "TP")

    return ("NONE", "NONE", "NONE")


def _add_outcome(counts: OutcomeCounts, outcome: str) -> None:
    if outcome == "NEUTRAL_INTRABAR":
        counts.add_neutral_intrabar()
    else:
        counts.add(outcome)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels-a", required=True, help="CSV for pool A (e.g. buf=1 re=10)")
    ap.add_argument("--labels-b", required=True, help="CSV for pool B (e.g. buf=3 re=10)")
    ap.add_argument("--cache-dir", default="out/candle_cache")
    ap.add_argument("--oanda-env", default="practice")
    ap.add_argument("--instrument", default="EUR_USD")
    ap.add_argument("--granularity", default="M1")
    ap.add_argument("--out-csv", default="out/overlap_summary.csv")
    args = ap.parse_args()

    labels_a = _load_labels(Path(args.labels_a))
    labels_b = _load_labels(Path(args.labels_b))

    all_dates = sorted(set(labels_a.keys()) | set(labels_b.keys()))

    cache_dir = Path(args.cache_dir)

    # Overlap buckets
    both = 0
    only_a = 0
    only_b = 0
    neither = 0

    # Conditional EV stats (structure-stop R=1 horizon=90)
    # We compute per-day outcomes using cached data.
    # Keep three ambiguity modes.
    a_all = {"worst": OutcomeCounts(), "neutral": OutcomeCounts(), "best": OutcomeCounts()}
    b_all = {"worst": OutcomeCounts(), "neutral": OutcomeCounts(), "best": OutcomeCounts()}

    a_given_b = {"worst": OutcomeCounts(), "neutral": OutcomeCounts(), "best": OutcomeCounts()}
    a_given_not_b = {"worst": OutcomeCounts(), "neutral": OutcomeCounts(), "best": OutcomeCounts()}

    b_given_a = {"worst": OutcomeCounts(), "neutral": OutcomeCounts(), "best": OutcomeCounts()}
    b_given_not_a = {"worst": OutcomeCounts(), "neutral": OutcomeCounts(), "best": OutcomeCounts()}

    def is_signal(x: Optional[LabelRow]) -> bool:
        return (
            x is not None
            and x.day_class == "MEAN_REVERSION"
            and x.reentry_time_ny is not None
            and x.first_sweep_side in ("HIGH", "LOW")
            and x.asia_high is not None
            and x.asia_low is not None
        )

    # For output CSV rows
    out_rows: List[Dict[str, str]] = []

    for d in all_dates:
        ra = labels_a.get(d)
        rb = labels_b.get(d)

        sa = is_signal(ra)
        sb = is_signal(rb)

        if sa and sb:
            both += 1
        elif sa and not sb:
            only_a += 1
        elif sb and not sa:
            only_b += 1
        else:
            neither += 1

        # We only simulate outcomes on days where that config signals (MEAN_REVERSION+reentry)
        # using each config's own buffer_pips (important).
        for tag, row in (("A", ra), ("B", rb)):
            if not is_signal(row):
                continue

            assert row is not None
            bid_payload = _load_cached_payload(cache_dir, args.oanda_env, args.instrument, args.granularity, row.date_ny, "B")
            ask_payload = _load_cached_payload(cache_dir, args.oanda_env, args.instrument, args.granularity, row.date_ny, "A")
            series = _build_mid_series(bid_payload, ask_payload)

            entry_idx = _find_index_at_or_after(series, row.reentry_time_ny.astimezone(UTC_TZ))  # type: ignore[union-attr]
            if entry_idx is None:
                continue

            sweep_high_ext, sweep_low_ext = _compute_sweep_extreme(
                series=series,
                trade_date_ny=row.date_ny,
                asia_high=row.asia_high,  # type: ignore[arg-type]
                asia_low=row.asia_low,    # type: ignore[arg-type]
                buffer_pips=row.sweep_buffer_pips,
            )

            out = _simulate_structure_r1_h90(
                series=series,
                entry_idx=entry_idx,
                sweep_side=row.first_sweep_side,
                sweep_high_extreme=sweep_high_ext,
                sweep_low_extreme=sweep_low_ext,
                buffer_pips=row.sweep_buffer_pips,
                horizon_minutes=90,
            )
            if out is None:
                continue

            worst, best, neutral = out

            # overall stats
            if tag == "A":
                _add_outcome(a_all["worst"], worst)
                _add_outcome(a_all["best"], best)
                _add_outcome(a_all["neutral"], neutral)
                # conditionals
                if sb:
                    _add_outcome(a_given_b["worst"], worst)
                    _add_outcome(a_given_b["best"], best)
                    _add_outcome(a_given_b["neutral"], neutral)
                else:
                    _add_outcome(a_given_not_b["worst"], worst)
                    _add_outcome(a_given_not_b["best"], best)
                    _add_outcome(a_given_not_b["neutral"], neutral)
            else:
                _add_outcome(b_all["worst"], worst)
                _add_outcome(b_all["best"], best)
                _add_outcome(b_all["neutral"], neutral)
                # conditionals
                if sa:
                    _add_outcome(b_given_a["worst"], worst)
                    _add_outcome(b_given_a["best"], best)
                    _add_outcome(b_given_a["neutral"], neutral)
                else:
                    _add_outcome(b_given_not_a["worst"], worst)
                    _add_outcome(b_given_not_a["best"], best)
                    _add_outcome(b_given_not_a["neutral"], neutral)

            out_rows.append(
                {
                    "date_ny": row.date_ny,
                    "pool": tag,
                    "buffer_pips": f"{row.sweep_buffer_pips:g}",
                    "sweep_side": row.first_sweep_side,
                    "outcome_worst": worst,
                    "outcome_neutral": neutral,
                    "outcome_best": best,
                    "both_signals": "1" if (sa and sb) else "0",
                }
            )

    # Write per-day outcomes CSV (so you can audit specific days later)
    out_path = Path(args.out_csv)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", newline="") as f:
        fieldnames = ["date_ny", "pool", "buffer_pips", "sweep_side", "outcome_worst", "outcome_neutral", "outcome_best", "both_signals"]
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)

    # Print overlap summary
    print("=== Overlap summary ===")
    print(f"Total dates compared: {len(all_dates)}")
    print(f"Both signal: {both}")
    print(f"Only A signals: {only_a}")
    print(f"Only B signals: {only_b}")
    print(f"Neither signals: {neither}")

    def print_counts(title: str, d: Dict[str, OutcomeCounts]) -> None:
        print(f"\n== {title} (structure-stop R=1, horizon=90) ==")
        for mode in ("worst", "neutral", "best"):
            c = d[mode]
            print(
                f"{mode:>7}: n={c.n:.1f} tp={c.tp:.1f} sl={c.sl:.1f} none={c.none:.1f} "
                f"tp_rate={c.tp_rate():.3f} sl_rate={c.sl_rate():.3f} EV_R={c.ev_r():.3f}"
            )

    print_counts("Pool A overall", a_all)
    print_counts("Pool B overall", b_all)

    print_counts("Pool A | given Pool B signals", a_given_b)
    print_counts("Pool A | given Pool B does NOT signal", a_given_not_b)

    print_counts("Pool B | given Pool A signals", b_given_a)
    print_counts("Pool B | given Pool A does NOT signal", b_given_not_a)

    print(f"\nWrote per-day outcomes: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())