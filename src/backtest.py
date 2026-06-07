# file: src/backtest.py
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.oanda.candle_cache import CandleCache, CandleCacheKey
from src.oanda.oanda_client import OandaApiError, OandaClient, load_oanda_config
from src.strategy.london_day_classifier import (
    Candle,
    MarketClosedOrNoDataError,
    build_session_windows_for_date,
    classify_london_day,
    compute_asia_range,
    parse_oanda_candles,
)

NY_TZ = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")
PIP_VALUE = 0.0001  # EUR_USD


def _is_weekend_in_new_york(dt_ny: datetime) -> bool:
    return dt_ny.weekday() >= 5


def _iter_trading_dates_backwards(
    end_date_ny: datetime, trading_days: int
) -> List[datetime]:
    dates: List[datetime] = []
    cursor = end_date_ny.astimezone(NY_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    while len(dates) < trading_days:
        if not _is_weekend_in_new_york(cursor):
            dates.append(cursor)
        cursor -= timedelta(days=1)

    dates.reverse()
    return dates


def _to_rfc3339(dt: datetime) -> str:
    return dt.astimezone(UTC_TZ).isoformat()


def _fmt_seconds(seconds: float) -> str:
    seconds_int = int(max(0.0, seconds))
    m, s = divmod(seconds_int, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}h{m:02d}m{s:02d}s"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _print_progress(
    *,
    idx: int,
    total: int,
    trade_date_ny: datetime,
    ok_count: int,
    err_count: int,
    candles_count: Optional[int],
    started_monotonic: float,
    every: int,
) -> None:
    if every <= 0:
        every = 1
    if (idx % every != 0) and (idx != total):
        return

    elapsed = time.monotonic() - started_monotonic
    avg = elapsed / max(1, idx)
    remaining = avg * (total - idx)

    candles_part = f" candles={candles_count}" if candles_count is not None else ""
    print(
        f"[{idx:>4}/{total}] day={trade_date_ny.date()} ok={ok_count} err={err_count}"
        f"{candles_part} elapsed={_fmt_seconds(elapsed)} eta={_fmt_seconds(remaining)}",
        flush=True,
    )


@dataclass(frozen=True)
class _Ohlc:
    o: float
    h: float
    l: float
    c: float


def _parse_oanda_ohlc_map(payload: dict, price_key: str) -> Dict[datetime, _Ohlc]:
    out: Dict[datetime, _Ohlc] = {}
    for c in payload.get("candles", []):
        if not c.get("complete", True):
            continue
        ts = datetime.fromisoformat(c["time"].replace("Z", "+00:00")).astimezone(UTC_TZ)
        p = c[price_key]
        out[ts] = _Ohlc(
            o=float(p["o"]), h=float(p["h"]), l=float(p["l"]), c=float(p["c"])
        )
    return out


def _synthesize_mid_candles_from_bid_ask(
    bid_payload: dict, ask_payload: dict
) -> List[Candle]:
    bid = _parse_oanda_ohlc_map(bid_payload, "bid")
    ask = _parse_oanda_ohlc_map(ask_payload, "ask")
    common_ts = sorted(set(bid.keys()) & set(ask.keys()))

    candles: List[Candle] = []
    for ts in common_ts:
        b = bid[ts]
        a = ask[ts]
        candles.append(
            Candle(timestamp_utc=ts, high=(b.h + a.h) / 2.0, low=(b.l + a.l) / 2.0)
        )
    return candles


def _synthesize_mid_close_map_from_bid_ask(
    bid_payload: dict, ask_payload: dict
) -> Dict[datetime, float]:
    bid = _parse_oanda_ohlc_map(bid_payload, "bid")
    ask = _parse_oanda_ohlc_map(ask_payload, "ask")
    common_ts = set(bid.keys()) & set(ask.keys())
    return {ts: (bid[ts].c + ask[ts].c) / 2.0 for ts in common_ts}


def _london_spread_stats_pips(
    bid_payload: dict,
    ask_payload: dict,
    london_window_start_ny: datetime,
    london_window_end_ny: datetime,
) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    bid = _parse_oanda_ohlc_map(bid_payload, "bid")
    ask = _parse_oanda_ohlc_map(ask_payload, "ask")
    spreads: List[float] = []

    for ts in sorted(set(bid.keys()) & set(ask.keys())):
        ts_ny = ts.astimezone(NY_TZ)
        if not (london_window_start_ny <= ts_ny < london_window_end_ny):
            continue
        spreads.append((ask[ts].c - bid[ts].c) / PIP_VALUE)

    if not spreads:
        return None, None, None
    spreads.sort()
    avg = sum(spreads) / len(spreads)
    p95 = spreads[int(0.95 * (len(spreads) - 1))]
    mx = spreads[-1]
    return avg, p95, mx


def _nearest_timestamp_key(
    ts_map: Dict[datetime, object],
    target_ts_utc: datetime,
    tolerance_seconds: int = 61,
) -> Optional[datetime]:
    if target_ts_utc in ts_map:
        return target_ts_utc
    for delta_s in (60, -60, 120, -120, 180, -180):
        candidate = target_ts_utc + timedelta(seconds=delta_s)
        if (
            candidate in ts_map
            and abs((candidate - target_ts_utc).total_seconds()) <= tolerance_seconds
        ):
            return candidate
    best: Optional[datetime] = None
    best_abs = float("inf")
    for k in ts_map.keys():
        diff = abs((k - target_ts_utc).total_seconds())
        if diff <= tolerance_seconds and diff < best_abs:
            best = k
            best_abs = diff
    return best


def _spread_at_time_pips(
    bid_payload: dict, ask_payload: dict, event_time_ny: datetime
) -> Optional[float]:
    if event_time_ny.tzinfo is None:
        event_time_ny = event_time_ny.replace(tzinfo=NY_TZ)
    bid = _parse_oanda_ohlc_map(bid_payload, "bid")
    ask = _parse_oanda_ohlc_map(ask_payload, "ask")
    key = _nearest_timestamp_key(bid, event_time_ny.astimezone(UTC_TZ))
    if key is None or key not in ask:
        return None
    return (ask[key].c - bid[key].c) / PIP_VALUE


def _p95_spread_in_window_pips(
    bid_payload: dict,
    ask_payload: dict,
    window_start_ny: datetime,
    window_end_ny: datetime,
) -> Optional[float]:
    bid = _parse_oanda_ohlc_map(bid_payload, "bid")
    ask = _parse_oanda_ohlc_map(ask_payload, "ask")
    spreads: List[float] = []

    for ts in sorted(set(bid.keys()) & set(ask.keys())):
        ts_ny = ts.astimezone(NY_TZ)
        if not (window_start_ny <= ts_ny < window_end_ny):
            continue
        spreads.append((ask[ts].c - bid[ts].c) / PIP_VALUE)

    if not spreads:
        return None
    spreads.sort()
    return spreads[int(0.95 * (len(spreads) - 1))]


def _excursions_30m_after_reentry_pips(
    mid_close_map_utc: Dict[datetime, float],
    mid_candles: List[Candle],
    reentry_time_ny: datetime,
    sweep_side: Optional[str],
) -> Tuple[Optional[float], Optional[float]]:
    if reentry_time_ny.tzinfo is None:
        reentry_time_ny = reentry_time_ny.replace(tzinfo=NY_TZ)

    start_ny = reentry_time_ny
    end_ny = reentry_time_ny + timedelta(minutes=30)

    candle_by_ts: Dict[datetime, Candle] = {c.timestamp_utc: c for c in mid_candles}
    reentry_ts_utc = reentry_time_ny.astimezone(UTC_TZ)

    ref_key = _nearest_timestamp_key(mid_close_map_utc, reentry_ts_utc)
    ref_price = mid_close_map_utc.get(ref_key) if ref_key is not None else None
    if ref_price is None:
        candle_key = _nearest_timestamp_key(candle_by_ts, reentry_ts_utc)
        if candle_key is not None:
            c = candle_by_ts[candle_key]
            ref_price = (c.high + c.low) / 2.0

    if ref_price is None or not sweep_side:
        return None, None

    lows: List[float] = []
    highs: List[float] = []
    for c in mid_candles:
        t_ny = c.timestamp_utc.astimezone(NY_TZ)
        if not (start_ny <= t_ny < end_ny):
            continue
        lows.append(c.low)
        highs.append(c.high)

    if not lows or not highs:
        return None, None

    min_price = min(lows)
    max_price = max(highs)

    if sweep_side == "HIGH":
        favorable = (ref_price - min_price) / PIP_VALUE
        adverse = (max_price - ref_price) / PIP_VALUE
    else:
        favorable = (max_price - ref_price) / PIP_VALUE
        adverse = (ref_price - min_price) / PIP_VALUE

    return favorable, adverse


def _compute_sweep_depth_pips_fixed_0300_0500_ny(
    *,
    trade_date_ny: datetime,
    candles: List[Candle],
    asia_high: float,
    asia_low: float,
    buffer_pips: float,
    first_sweep_side: Optional[str],
) -> Optional[float]:
    """
    Computes sweep depth beyond the Asia boundary±buffer inside a fixed 03:00–05:00 NY window.

    Depth definitions (pips):
      upper = asia_high + buffer
      lower = asia_low  - buffer

      depth_high = max(0, max_high_in_window - upper)
      depth_low  = max(0, lower - min_low_in_window)

    Returned sweep depth uses first_sweep_side when known:
      - HIGH -> depth_high
      - LOW  -> depth_low
      - else -> max(depth_high, depth_low)
    """
    # NY-local day window (same day)
    day_ny = trade_date_ny.astimezone(NY_TZ).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    start_ny = day_ny.replace(hour=3, minute=0)
    end_ny = day_ny.replace(hour=5, minute=0)

    upper = asia_high + (buffer_pips * PIP_VALUE)
    lower = asia_low - (buffer_pips * PIP_VALUE)

    max_h: Optional[float] = None
    min_l: Optional[float] = None

    for c in candles:
        t_ny = c.timestamp_utc.astimezone(NY_TZ)
        if not (start_ny <= t_ny < end_ny):
            continue
        max_h = c.high if max_h is None else max(max_h, c.high)
        min_l = c.low if min_l is None else min(min_l, c.low)

    if max_h is None or min_l is None:
        return None

    depth_high = max(0.0, (max_h - upper) / PIP_VALUE)
    depth_low = max(0.0, (lower - min_l) / PIP_VALUE)

    side = (first_sweep_side or "").strip().upper()
    if side == "HIGH":
        return depth_high
    if side == "LOW":
        return depth_low
    return max(depth_high, depth_low)


def _cache_get(
    cache: CandleCache,
    *,
    key: CandleCacheKey,
    fetch_fn,
) -> Tuple[Dict[str, Any], bool]:
    """
    Normalize CandleCache.get_or_fetch() across versions.

    Supports:
      - payload
      - (payload, hit)
      - (payload, hit, ...)
    """
    result = cache.get_or_fetch(key=key, fetch_fn=fetch_fn)

    if isinstance(result, tuple):
        if len(result) >= 2:
            return result[0], bool(result[1])
        # weird edge: (payload,)
        return result[0], False

    # older versions that returned payload only
    return result, False


def run_backtest(
    *,
    instrument: str,
    trading_days: int,
    output_csv_path: Path,
    granularity: str,
    sweep_buffer_pips: float,
    reentry_deadline_minutes: int,
    include_spread: bool,
    progress_every: int,
    sleep_seconds_between_requests: float,
    max_requests: int,
    cache_dir: Optional[Path] = None,
    cache_verbose: bool = False,
    # NEW: gates (optional; when None -> no gate)
    gate_max_sweep_depth_pips: Optional[float] = None,
    gate_min_asia_range_pips: Optional[float] = None,
) -> None:
    config = load_oanda_config()
    client = OandaClient(config)

    oanda_env = os.environ.get("OANDA_ENV", "practice").strip().lower()
    cache = CandleCache(cache_dir) if cache_dir else None

    end_date_ny = datetime.now(tz=NY_TZ)
    trade_dates_ny = _iter_trading_dates_backwards(
        end_date_ny=end_date_ny, trading_days=trading_days
    )
    if len(trade_dates_ny) > max_requests:
        trade_dates_ny = trade_dates_ny[-max_requests:]

    class_counts: Dict[str, int] = {
        "MEAN_REVERSION": 0,
        "TREND": 0,
        "DOUBLE_SWEEP": 0,
        "RANGE_INSIDE": 0,
        "MARKET_CLOSED": 0,
    }

    api_calls = 0
    cache_hits = 0
    cache_misses = 0
    total_errors = 0
    ok_days = 0

    started = time.monotonic()
    total_days = len(trade_dates_ny)
    results: List[Dict[str, str]] = []

    for idx, trade_date_ny in enumerate(trade_dates_ny, start=1):
        candles_count: Optional[int] = None
        did_network = False

        (
            asia_window_start_ny,
            asia_window_end_ny,
            london_window_start_ny,
            london_window_end_ny,
            double_sweep_check_end_ny,
        ) = build_session_windows_for_date(trade_date_ny)

        fetch_start = _to_rfc3339(asia_window_start_ny)
        fetch_end = _to_rfc3339(double_sweep_check_end_ny)
        trade_date_str = str(trade_date_ny.date())

        spread_avg = spread_p95 = spread_max = None
        spread_at_sweep = spread_at_reentry = spread_p95_5m_after_reentry = None
        favorable_30m = adverse_30m = net_favorable_after_cost_30m = None

        # NEW: gating metrics
        asia_range_pips: Optional[float] = None
        sweep_depth_pips: Optional[float] = None
        took_trade: int = 0
        gate_reason: str = ""

        try:

            def _fetch(price: str) -> dict:
                return client.get_candles(
                    instrument=instrument,
                    granularity=granularity,
                    time_from_rfc3339=fetch_start,
                    time_to_rfc3339=fetch_end,
                    price=price,
                )

            if include_spread:
                if cache:
                    bid_key = CandleCacheKey(
                        oanda_env,
                        instrument,
                        granularity,
                        trade_date_str,
                        "B",
                        fetch_start,
                        fetch_end,
                    )
                    ask_key = CandleCacheKey(
                        oanda_env,
                        instrument,
                        granularity,
                        trade_date_str,
                        "A",
                        fetch_start,
                        fetch_end,
                    )

                    bid_payload, bid_hit = _cache_get(
                        cache, key=bid_key, fetch_fn=lambda: _fetch("B")
                    )
                    ask_payload, ask_hit = _cache_get(
                        cache, key=ask_key, fetch_fn=lambda: _fetch("A")
                    )

                    cache_hits += int(bid_hit) + int(ask_hit)
                    cache_misses += int(not bid_hit) + int(not ask_hit)
                    api_calls += int(not bid_hit) + int(not ask_hit)
                    did_network = (not bid_hit) or (not ask_hit)

                    if cache_verbose and (idx == 1 or did_network):
                        print(
                            f"[CACHE] {trade_date_str} B={'HIT' if bid_hit else 'MISS'} "
                            f"A={'HIT' if ask_hit else 'MISS'}",
                            flush=True,
                        )
                else:
                    bid_payload = _fetch("B")
                    ask_payload = _fetch("A")
                    api_calls += 2
                    did_network = True

                candles = _synthesize_mid_candles_from_bid_ask(bid_payload, ask_payload)
                candles_count = len(candles)
                mid_close_map_utc = _synthesize_mid_close_map_from_bid_ask(
                    bid_payload, ask_payload
                )

                spread_avg, spread_p95, spread_max = _london_spread_stats_pips(
                    bid_payload,
                    ask_payload,
                    london_window_start_ny,
                    london_window_end_ny,
                )

                asia_range = compute_asia_range(
                    candles=candles,
                    asia_window_start_ny=asia_window_start_ny,
                    asia_window_end_ny=asia_window_end_ny,
                )

                classification = classify_london_day(
                    candles=candles,
                    asia_range=asia_range,
                    london_window_start_ny=london_window_start_ny,
                    london_window_end_ny=london_window_end_ny,
                    sweep_buffer_pips=sweep_buffer_pips,
                    reentry_deadline_minutes=reentry_deadline_minutes,
                    evaluate_double_sweep_until_ny=double_sweep_check_end_ny,
                )

                if classification.first_sweep_time_ny is not None:
                    spread_at_sweep = _spread_at_time_pips(
                        bid_payload, ask_payload, classification.first_sweep_time_ny
                    )

                if classification.reentry_time_ny is not None:
                    spread_at_reentry = _spread_at_time_pips(
                        bid_payload, ask_payload, classification.reentry_time_ny
                    )
                    spread_p95_5m_after_reentry = _p95_spread_in_window_pips(
                        bid_payload,
                        ask_payload,
                        classification.reentry_time_ny,
                        classification.reentry_time_ny + timedelta(minutes=5),
                    )
                    favorable_30m, adverse_30m = _excursions_30m_after_reentry_pips(
                        mid_close_map_utc,
                        candles,
                        classification.reentry_time_ny,
                        classification.first_sweep_side,
                    )
                    if favorable_30m is not None and spread_at_reentry is not None:
                        net_favorable_after_cost_30m = favorable_30m - (
                            2.0 * spread_at_reentry
                        )

                # NEW: compute gate metrics (needs mid candles; include_spread ensures we have them)
                asia_range_pips = (
                    classification.asia_high - classification.asia_low
                ) / PIP_VALUE
                sweep_depth_pips = _compute_sweep_depth_pips_fixed_0300_0500_ny(
                    trade_date_ny=trade_date_ny,
                    candles=candles,
                    asia_high=classification.asia_high,
                    asia_low=classification.asia_low,
                    buffer_pips=sweep_buffer_pips,
                    first_sweep_side=classification.first_sweep_side,
                )

            else:
                # NOTE: without BID/ASK we can't reproduce your sweep-depth metric reliably,
                # but we can still compute Asia range. Sweep depth will be blank.
                if cache:
                    mid_key = CandleCacheKey(
                        oanda_env,
                        instrument,
                        granularity,
                        trade_date_str,
                        "M",
                        fetch_start,
                        fetch_end,
                    )
                    mid_payload, mid_hit = _cache_get(
                        cache, key=mid_key, fetch_fn=lambda: _fetch("M")
                    )
                    cache_hits += int(mid_hit)
                    cache_misses += int(not mid_hit)
                    api_calls += int(not mid_hit)
                    did_network = not mid_hit
                    if cache_verbose and (idx == 1 or did_network):
                        print(
                            f"[CACHE] {trade_date_str} M={'HIT' if mid_hit else 'MISS'}",
                            flush=True,
                        )
                else:
                    mid_payload = _fetch("M")
                    api_calls += 1
                    did_network = True

                candles = parse_oanda_candles(mid_payload, price_key="mid")
                candles_count = len(candles)

                asia_range = compute_asia_range(
                    candles=candles,
                    asia_window_start_ny=asia_window_start_ny,
                    asia_window_end_ny=asia_window_end_ny,
                )

                classification = classify_london_day(
                    candles=candles,
                    asia_range=asia_range,
                    london_window_start_ny=london_window_start_ny,
                    london_window_end_ny=london_window_end_ny,
                    sweep_buffer_pips=sweep_buffer_pips,
                    reentry_deadline_minutes=reentry_deadline_minutes,
                    evaluate_double_sweep_until_ny=double_sweep_check_end_ny,
                )

                asia_range_pips = (
                    classification.asia_high - classification.asia_low
                ) / PIP_VALUE
                sweep_depth_pips = (
                    None  # requires BID/ASK mid synth for parity with your research
                )

            class_counts[classification.day_class] = (
                class_counts.get(classification.day_class, 0) + 1
            )
            ok_days += 1

            # NEW: decide "signal" and apply gates.
            # "Signal" here mirrors your overlap usage: mean reversion setup with a reentry time.
            signals = (classification.day_class == "MEAN_REVERSION") and (
                classification.reentry_time_ny is not None
            )

            if not signals:
                took_trade = 0
                gate_reason = "no_signal"
            else:
                # Apply gates if configured; otherwise take the trade.
                # Gate 1: sweep depth
                if gate_max_sweep_depth_pips is not None:
                    if sweep_depth_pips is None:
                        took_trade = 0
                        gate_reason = "missing_sweep_depth"
                    elif sweep_depth_pips > gate_max_sweep_depth_pips:
                        took_trade = 0
                        gate_reason = "deep_sweep"
                    else:
                        took_trade = 1
                        gate_reason = ""
                else:
                    took_trade = 1
                    gate_reason = ""

                # Gate 2: Asia range (only if we're still taking the trade)
                if took_trade == 1 and gate_min_asia_range_pips is not None:
                    if asia_range_pips is None:
                        took_trade = 0
                        gate_reason = "missing_asia_range"
                    elif asia_range_pips < gate_min_asia_range_pips:
                        took_trade = 0
                        gate_reason = "small_asia_range"

            row: Dict[str, str] = {
                "date_ny": trade_date_str,
                "instrument": instrument,
                "sweep_buffer_pips": str(sweep_buffer_pips),
                "reentry_deadline_minutes": str(reentry_deadline_minutes),
                "asia_high": f"{classification.asia_high:.5f}",
                "asia_low": f"{classification.asia_low:.5f}",
                "first_sweep_side": classification.first_sweep_side or "",
                "first_sweep_time_ny": (
                    classification.first_sweep_time_ny.isoformat()
                    if classification.first_sweep_time_ny
                    else ""
                ),
                "reentry_time_ny": (
                    classification.reentry_time_ny.isoformat()
                    if classification.reentry_time_ny
                    else ""
                ),
                "double_sweep": str(classification.double_sweep),
                "day_class": classification.day_class,
                # NEW columns:
                "asia_range_pips": (
                    "" if asia_range_pips is None else f"{asia_range_pips:.3f}"
                ),
                "sweep_depth_pips": (
                    "" if sweep_depth_pips is None else f"{sweep_depth_pips:.3f}"
                ),
                "signals": "1" if signals else "0",
                "took_trade": str(int(took_trade)),
                "gate_reason": gate_reason,
            }

            if include_spread:
                row.update(
                    {
                        "london_spread_avg_pips": (
                            "" if spread_avg is None else f"{spread_avg:.3f}"
                        ),
                        "london_spread_p95_pips": (
                            "" if spread_p95 is None else f"{spread_p95:.3f}"
                        ),
                        "london_spread_max_pips": (
                            "" if spread_max is None else f"{spread_max:.3f}"
                        ),
                        "spread_at_sweep_pips": (
                            "" if spread_at_sweep is None else f"{spread_at_sweep:.3f}"
                        ),
                        "spread_at_reentry_pips": (
                            ""
                            if spread_at_reentry is None
                            else f"{spread_at_reentry:.3f}"
                        ),
                        "spread_p95_5m_after_reentry_pips": (
                            ""
                            if spread_p95_5m_after_reentry is None
                            else f"{spread_p95_5m_after_reentry:.3f}"
                        ),
                        "favorable_excursion_30m_pips": (
                            "" if favorable_30m is None else f"{favorable_30m:.1f}"
                        ),
                        "adverse_excursion_30m_pips": (
                            "" if adverse_30m is None else f"{adverse_30m:.1f}"
                        ),
                        "net_favorable_after_cost_30m_pips": (
                            ""
                            if net_favorable_after_cost_30m is None
                            else f"{net_favorable_after_cost_30m:.1f}"
                        ),
                    }
                )

            results.append(row)

        except MarketClosedOrNoDataError:
            class_counts["MARKET_CLOSED"] += 1
            results.append(
                {
                    "date_ny": trade_date_str,
                    "instrument": instrument,
                    "sweep_buffer_pips": str(sweep_buffer_pips),
                    "reentry_deadline_minutes": str(reentry_deadline_minutes),
                    "asia_high": "",
                    "asia_low": "",
                    "first_sweep_side": "",
                    "first_sweep_time_ny": "",
                    "reentry_time_ny": "",
                    "double_sweep": "",
                    "day_class": "MARKET_CLOSED",
                    # NEW columns:
                    "asia_range_pips": "",
                    "sweep_depth_pips": "",
                    "signals": "0",
                    "took_trade": "0",
                    "gate_reason": "market_closed",
                }
            )

        except OandaApiError as e:
            total_errors += 1
            print(f"[WARN] OANDA error on {trade_date_str}: {e}", file=sys.stderr)

        except Exception as e:
            total_errors += 1
            print(f"[WARN] Unexpected error on {trade_date_str}: {e}", file=sys.stderr)

        _print_progress(
            idx=idx,
            total=total_days,
            trade_date_ny=trade_date_ny,
            ok_count=ok_days,
            err_count=total_errors,
            candles_count=candles_count,
            started_monotonic=started,
            every=progress_every,
        )

        if did_network and sleep_seconds_between_requests > 0:
            time.sleep(sleep_seconds_between_requests)

    output_csv_path.parent.mkdir(parents=True, exist_ok=True)

    fieldnames = [
        "date_ny",
        "instrument",
        "sweep_buffer_pips",
        "reentry_deadline_minutes",
        "asia_high",
        "asia_low",
        "asia_range_pips",  # NEW
        "first_sweep_side",
        "first_sweep_time_ny",
        "reentry_time_ny",
        "sweep_depth_pips",  # NEW
        "double_sweep",
        "day_class",
        "signals",  # NEW
        "took_trade",  # NEW
        "gate_reason",  # NEW
    ]
    if include_spread:
        fieldnames += [
            "london_spread_avg_pips",
            "london_spread_p95_pips",
            "london_spread_max_pips",
            "spread_at_sweep_pips",
            "spread_at_reentry_pips",
            "spread_p95_5m_after_reentry_pips",
            "favorable_excursion_30m_pips",
            "adverse_excursion_30m_pips",
            "net_favorable_after_cost_30m_pips",
        ]

    with output_csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            w.writerow(r)

    total_labeled = sum(class_counts.values())
    print("=== Backtest Summary ===")
    print(f"Instrument: {instrument}")
    print(f"Trading days attempted: {len(trade_dates_ny)}")
    print(f"API calls made: {api_calls}")
    if cache:
        print(
            f"Cache hits: {cache_hits}  Cache misses: {cache_misses}  Cache dir: {cache_dir}"
        )
    print(f"Errors: {total_errors}")
    print(f"Output CSV: {output_csv_path}")

    if gate_max_sweep_depth_pips is not None or gate_min_asia_range_pips is not None:
        print("=== Gates ===")
        print(f"gate_max_sweep_depth_pips: {gate_max_sweep_depth_pips}")
        print(f"gate_min_asia_range_pips: {gate_min_asia_range_pips}")

    if total_labeled > 0:
        for k in [
            "MEAN_REVERSION",
            "TREND",
            "DOUBLE_SWEEP",
            "RANGE_INSIDE",
            "MARKET_CLOSED",
        ]:
            count = class_counts.get(k, 0)
            pct = (count / total_labeled) * 100.0
            print(f"{k:14s}: {count:4d}  ({pct:5.1f}%)")


def _build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.backtest")
    p.add_argument("trading_days", type=int, nargs="?", default=250)
    p.add_argument("instrument", type=str, nargs="?", default="EUR_USD")
    p.add_argument("output_csv", type=str, nargs="?", default="out/labels.csv")
    p.add_argument("--granularity", type=str, default="M1")
    p.add_argument("--buffer-pips", type=float, default=2.0)
    p.add_argument("--reentry-minutes", type=int, default=20)
    p.add_argument("--include-spread", action="store_true")
    p.add_argument("--progress-every", type=int, default=5)
    p.add_argument("--sleep", type=float, default=0.05)
    p.add_argument("--max-requests", type=int, default=600)
    p.add_argument("--cache-dir", type=str, default="")
    p.add_argument("--cache-verbose", action="store_true")

    # NEW: optional gates
    p.add_argument(
        "--gate-max-sweep-depth-pips",
        type=float,
        default=None,
        help="If set, only take MEAN_REVERSION signals when sweep_depth_pips <= this value (pips).",
    )
    p.add_argument(
        "--gate-min-asia-range-pips",
        type=float,
        default=None,
        help="If set, only take MEAN_REVERSION signals when asia_range_pips >= this value (pips).",
    )

    return p


def main() -> int:
    args = _build_arg_parser().parse_args()
    cache_dir = Path(args.cache_dir) if args.cache_dir else None

    run_backtest(
        instrument=args.instrument,
        trading_days=args.trading_days,
        output_csv_path=Path(args.output_csv),
        granularity=args.granularity,
        sweep_buffer_pips=args.buffer_pips,
        reentry_deadline_minutes=args.reentry_minutes,
        include_spread=args.include_spread,
        progress_every=args.progress_every,
        sleep_seconds_between_requests=args.sleep,
        max_requests=args.max_requests,
        cache_dir=cache_dir,
        cache_verbose=args.cache_verbose,
        gate_max_sweep_depth_pips=args.gate_max_sweep_depth_pips,
        gate_min_asia_range_pips=args.gate_min_asia_range_pips,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
