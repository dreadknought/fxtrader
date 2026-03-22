# file: src/trade_stream.py
from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import urllib3

from src.oanda.oanda_client import OandaApiError, OandaClient, load_oanda_config
from src.strategy.candidate_v1 import CANDIDATE_V1, CANDIDATE_V1_RISK
from src.strategy.gated_strategy import GateConfig, LiveState, evaluate_decision, update_state_from_tick
from src.strategy.london_day_classifier import build_session_windows_for_date

NY = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
PIP_VALUE = 0.0001  # EUR_USD


@dataclass
class RuntimeState:
    current_trade_date_ny: datetime
    day_state: LiveState
    asia_backfilled: bool = False

    # Snapshot taken immediately after Asia backfill (for sizing + P&L lookup)
    nav_for_sizing: float | None = None
    balance_snapshot: float | None = None
    last_txn_id_snapshot: str | None = None

    traded: bool = False
    trade_id: str | None = None
    trade_open_time_ny: datetime | None = None


def _safe_float(x) -> float | None:
    try:
        return float(x)
    except Exception:
        return None


def _parse_tick_time_utc(msg: dict) -> datetime | None:
    t = msg.get("time")
    if not t:
        return None
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(UTC)
    except Exception:
        return None


def _bid_ask_mid(msg: dict) -> tuple[float, float, float] | None:
    bids = msg.get("bids") or []
    asks = msg.get("asks") or []
    if not bids or not asks:
        return None
    try:
        bid = float(bids[0]["price"])
        ask = float(asks[0]["price"])
        mid = (bid + ask) / 2.0
        return bid, ask, mid
    except Exception:
        return None


def _to_rfc3339_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=NY)
    return dt.astimezone(UTC).isoformat()


def _compute_asia_high_low_from_mid_candles(payload: dict) -> tuple[float, float]:
    highs: list[float] = []
    lows: list[float] = []
    for c in payload.get("candles", []):
        if not c.get("complete", True):
            continue
        m = c.get("mid")
        if not m:
            continue
        try:
            highs.append(float(m["h"]))
            lows.append(float(m["l"]))
        except Exception:
            continue
    if not highs or not lows:
        raise ValueError("No usable candles to compute Asia range.")
    return max(highs), min(lows)


def _backfill_asia_range_from_rest(client: OandaClient, instrument: str, trade_date_ny: datetime) -> tuple[float, float]:
    asia_start_ny, asia_end_ny, _brit_start_ny, _brit_end_ny, _double_sweep_end_ny = build_session_windows_for_date(
        trade_date_ny
    )
    payload = client.get_candles(
        instrument=instrument,
        granularity="M1",
        time_from_rfc3339=_to_rfc3339_utc(asia_start_ny),
        time_to_rfc3339=_to_rfc3339_utc(asia_end_ny),
        price="M",
    )
    return _compute_asia_high_low_from_mid_candles(payload)


def _sl_tp_from_mid(mid: float, is_long: bool, stop_pips: float, tp_pips: float) -> tuple[float, float]:
    if is_long:
        sl = mid - stop_pips * PIP_VALUE
        tp = mid + tp_pips * PIP_VALUE
    else:
        sl = mid + stop_pips * PIP_VALUE
        tp = mid - tp_pips * PIP_VALUE
    return sl, tp


def _extract_trade_id(order_resp: dict) -> str | None:
    tx = order_resp.get("orderFillTransaction") or {}
    to = tx.get("tradeOpened") or {}
    if "tradeID" in to:
        return str(to["tradeID"])
    tos = tx.get("tradesOpened") or []
    if isinstance(tos, list) and tos:
        tid = tos[0].get("tradeID")
        if tid:
            return str(tid)
    return None


def _trade_ids_snapshot(client: OandaClient) -> set[str]:
    ids: set[str] = set()
    for t in client.get_open_trades():
        tid = t.get("id")
        if tid is not None:
            ids.add(str(tid))
    return ids


def _find_new_trade_id_by_diff(client: OandaClient, before_ids: set[str]) -> str | None:
    try:
        after = client.get_open_trades()
    except Exception:
        return None

    new_trades = []
    for t in after:
        tid = t.get("id")
        if tid is None:
            continue
        tid_s = str(tid)
        if tid_s not in before_ids:
            new_trades.append(t)

    if not new_trades:
        return None

    def _open_time_key(tr: dict) -> str:
        return str(tr.get("openTime") or "")

    new_trades.sort(key=_open_time_key, reverse=True)
    return str(new_trades[0].get("id")) if new_trades[0].get("id") is not None else None


def _is_trade_open(client: OandaClient, trade_id: str) -> bool:
    for t in client.get_open_trades():
        if str(t.get("id")) == str(trade_id):
            return True
    return False


def _compute_units_from_nav(*, nav_usd: float, stop_pips: float, risk_pct: float) -> int:
    """
    EUR_USD, USD account:
      pip_value_per_unit ~= 0.0001 USD per pip per 1 unit
    Risk at stop:
      risk_usd = nav_usd * risk_pct
      units = risk_usd / (stop_pips * 0.0001)
    """
    if nav_usd <= 0 or stop_pips <= 0 or risk_pct <= 0:
        return 0
    risk_usd = nav_usd * risk_pct
    units_float = risk_usd / (stop_pips * PIP_VALUE)
    return int(units_float)


def _summarize_no_trade(*, rt: RuntimeState, gate: GateConfig, now_ny: datetime) -> str:
    """
    Derive a human-readable diagnosis using the MR state at the time we exit.
    """
    s = rt.day_state
    if not rt.asia_backfilled:
        return "no_asia_backfill"

    if s.asia_high is None or s.asia_low is None:
        return "asia_missing"

    asia_range_pips = (s.asia_high - s.asia_low) / PIP_VALUE

    if asia_range_pips < gate.min_asia_range_pips:
        return f"asia_gate_failed(range={asia_range_pips:.2f}p < {gate.min_asia_range_pips}p)"

    if s.first_sweep_side is None or s.first_sweep_time_ny is None:
        return "no_sweep_in_british_window"

    if s.max_penetration_pips > gate.max_sweep_depth_pips:
        return f"deep_sweep(max_penetration={s.max_penetration_pips:.2f}p > {gate.max_sweep_depth_pips}p)"

    if s.reentry_time_ny is None:
        deadline = s.first_sweep_time_ny + timedelta(minutes=gate.reentry_deadline_minutes)
        if now_ny > deadline:
            return f"sweep_no_reentry_by_deadline(deadline={deadline})"
        return "sweep_seen_waiting_for_reentry"

    d = evaluate_decision(state=s, gate=gate)
    return f"reentry_seen_but_no_pass(reason={d.reason})"


def _log_state(prefix: str, *, rt: RuntimeState, gate: GateConfig, now_ny: datetime) -> None:
    s = rt.day_state
    asia_hi = s.asia_high
    asia_lo = s.asia_low
    asia_range_pips = None
    if asia_hi is not None and asia_lo is not None:
        asia_range_pips = (asia_hi - asia_lo) / PIP_VALUE

    print(
        f"{prefix} date={rt.current_trade_date_ny.date()} now={now_ny} "
        f"asia_hi={asia_hi} asia_lo={asia_lo} asia_range_pips={asia_range_pips} "
        f"sweep_side={s.first_sweep_side} sweep_time={s.first_sweep_time_ny} "
        f"max_penetration_pips={s.max_penetration_pips} reentry_time={s.reentry_time_ny} "
        f"gate(min_asia={gate.min_asia_range_pips}, max_sweep={gate.max_sweep_depth_pips}, re_deadline={gate.reentry_deadline_minutes}m)",
        flush=True,
    )


def _summarize_trade_pnl_from_transactions(
    *,
    client: OandaClient,
    trade_id: str,
    since_id: str,
    balance_snapshot: float | None,
    nav_snapshot: float | None,
) -> str:
    """
    Best-effort: sum any {pl, financing, commission} fields from transactions
    since 'since_id' that reference this trade_id.
    """
    tx_payload = client.get_transactions_since_id(since_id=since_id)
    txs = tx_payload.get("transactions", []) or []

    pl = 0.0
    financing = 0.0
    commission = 0.0
    matched = 0

    def mentions_trade(obj: object) -> bool:
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "tradeID" and str(v) == str(trade_id):
                    return True
                if mentions_trade(v):
                    return True
        elif isinstance(obj, list):
            return any(mentions_trade(v) for v in obj)
        else:
            if str(obj) == str(trade_id):
                return True
        return False

    for tx in txs:
        if not mentions_trade(tx):
            continue
        matched += 1
        fv = _safe_float(tx.get("pl"))
        if fv is not None:
            pl += fv
        fv = _safe_float(tx.get("financing"))
        if fv is not None:
            financing += fv
        fv = _safe_float(tx.get("commission"))
        if fv is not None:
            commission += fv

    bal_now = None
    nav_now = None
    try:
        acct = client.get_account_summary().get("account", {})
        bal_now = _safe_float(acct.get("balance"))
        nav_now = _safe_float(acct.get("NAV"))
    except Exception:
        pass

    parts = [
        f"trade_id={trade_id}",
        f"matched_txns={matched}",
        f"pl={pl:.2f}",
        f"financing={financing:.2f}",
        f"commission={commission:.2f}",
        f"net={pl + financing - commission:.2f}",
    ]
    if balance_snapshot is not None and bal_now is not None:
        parts.append(f"balance: {balance_snapshot:.2f} -> {bal_now:.2f} (Δ{(bal_now - balance_snapshot):+.2f})")
    if nav_snapshot is not None and nav_now is not None:
        parts.append(f"NAV: {nav_snapshot:.2f} -> {nav_now:.2f} (Δ{(nav_now - nav_snapshot):+.2f})")
    return " | ".join(parts)


def _process_stream_message(
    *,
    client: OandaClient,
    cfg,
    risk,
    gate: GateConfig,
    rt: RuntimeState,
    msg: dict,
    time_stop_minutes: int,
    risk_pct: float,
    min_units: int,
    max_units: int,
) -> tuple[RuntimeState, int | None]:
    """
    Process one pricing/heartbeat message.
    Returns:
      (rt, None) to keep running
      (rt, exit_code) to stop the worker
    """
    msg_type = msg.get("type")
    if msg_type not in {"PRICE", "HEARTBEAT"}:
        return rt, None

    ts_utc = _parse_tick_time_utc(msg)
    if ts_utc is None:
        return rt, None
    now_ny = ts_utc.astimezone(NY)
    trade_date_ny = now_ny.replace(hour=0, minute=0, second=0, microsecond=0)

    if trade_date_ny.date() != rt.current_trade_date_ny.date():
        rt = RuntimeState(current_trade_date_ny=trade_date_ny, day_state=LiveState())
        print(f"\n--- New NY day: {trade_date_ny.date()} ---", flush=True)

    asia_start_ny, asia_end_ny, brit_start_ny, brit_end_ny, _double_sweep_end_ny = build_session_windows_for_date(
        rt.current_trade_date_ny
    )

    # Monitor after trading
    if rt.traded and rt.trade_id and rt.trade_open_time_ny:
        deadline = rt.trade_open_time_ny + timedelta(minutes=time_stop_minutes)

        try:
            open_now = _is_trade_open(client, rt.trade_id)
        except Exception as e:
            print(f"[MONITOR ERROR] {rt.current_trade_date_ny.date()} {e}", file=sys.stderr, flush=True)
            time.sleep(5)
            return rt, None

        if not open_now:
            print(f"[CLOSED] {rt.current_trade_date_ny.date()} trade {rt.trade_id} closed (SL/TP).", flush=True)
            if rt.last_txn_id_snapshot:
                try:
                    summary = _summarize_trade_pnl_from_transactions(
                        client=client,
                        trade_id=rt.trade_id,
                        since_id=rt.last_txn_id_snapshot,
                        balance_snapshot=rt.balance_snapshot,
                        nav_snapshot=rt.nav_for_sizing,
                    )
                    print(f"[P&L] {rt.current_trade_date_ny.date()} {summary}", flush=True)
                except Exception as e:
                    print(f"[P&L ERROR] {e}", file=sys.stderr, flush=True)
            return rt, 0

        if now_ny >= deadline:
            try:
                client.close_trade(trade_id=rt.trade_id)
            except Exception as e:
                print(f"[CLOSE ERROR] trade {rt.trade_id} {e}", file=sys.stderr, flush=True)
                time.sleep(5)
                return rt, None

            print(
                f"[CLOSE] {rt.current_trade_date_ny.date()} trade {rt.trade_id} closed due to +{time_stop_minutes}m time stop.",
                flush=True,
            )

            time.sleep(2)
            if not _is_trade_open(client, rt.trade_id):
                print(f"[CLOSED] {rt.current_trade_date_ny.date()} trade {rt.trade_id} closed (time stop).", flush=True)
                if rt.last_txn_id_snapshot:
                    try:
                        summary = _summarize_trade_pnl_from_transactions(
                            client=client,
                            trade_id=rt.trade_id,
                            since_id=rt.last_txn_id_snapshot,
                            balance_snapshot=rt.balance_snapshot,
                            nav_snapshot=rt.nav_for_sizing,
                        )
                        print(f"[P&L] {rt.current_trade_date_ny.date()} {summary}", flush=True)
                    except Exception as e:
                        print(f"[P&L ERROR] {e}", file=sys.stderr, flush=True)
                return rt, 0

        return rt, None

    # If British window ended and we never traded, exit with summary
    if now_ny >= brit_end_ny:
        diag = _summarize_no_trade(rt=rt, gate=gate, now_ny=now_ny)
        _log_state("[NO TRADE SUMMARY]", rt=rt, gate=gate, now_ny=now_ny)
        print(f"[EXIT] {rt.current_trade_date_ny.date()} no trade today (British window ended). diag={diag}", flush=True)
        return rt, 0

    # Asia backfill at/after British window start
    if (not rt.asia_backfilled) and (now_ny >= brit_start_ny):
        try:
            asia_high, asia_low = _backfill_asia_range_from_rest(client, cfg.instrument, rt.current_trade_date_ny)
        except Exception as e:
            print(f"[ASIA BACKFILL ERROR] {rt.current_trade_date_ny.date()} {e}", file=sys.stderr, flush=True)
            return rt, None

        rt.asia_backfilled = True
        rt.day_state = LiveState(
            asia_high=asia_high,
            asia_low=asia_low,
            first_sweep_side=rt.day_state.first_sweep_side,
            first_sweep_time_ny=rt.day_state.first_sweep_time_ny,
            max_penetration_pips=rt.day_state.max_penetration_pips,
            reentry_time_ny=rt.day_state.reentry_time_ny,
        )

        asia_range_pips = (asia_high - asia_low) / PIP_VALUE
        print(
            f"[ASIA BACKFILLED] {rt.current_trade_date_ny.date()} hi={asia_high:.5f} lo={asia_low:.5f} range={asia_range_pips:.2f}p",
            flush=True,
        )

        if asia_range_pips < cfg.gate_min_asia_range_pips:
            _log_state("[NO TRADE SUMMARY]", rt=rt, gate=gate, now_ny=now_ny)
            print(f"[EXIT] {rt.current_trade_date_ny.date()} no trade today (asia_range too small).", flush=True)
            return rt, 0

        try:
            acct = client.get_account_summary().get("account", {})
            nav = _safe_float(acct.get("NAV"))
            bal = _safe_float(acct.get("balance"))
            last_txn = acct.get("lastTransactionID")
            rt.nav_for_sizing = nav if nav is not None else bal
            rt.balance_snapshot = bal
            rt.last_txn_id_snapshot = str(last_txn) if last_txn is not None else None
            print(
                f"[ACCT SNAPSHOT] {rt.current_trade_date_ny.date()} NAV={rt.nav_for_sizing} balance={rt.balance_snapshot} lastTxn={rt.last_txn_id_snapshot}",
                flush=True,
            )
        except Exception as e:
            print(f"[ACCT SNAPSHOT ERROR] {e}", file=sys.stderr, flush=True)
            rt.nav_for_sizing = None
            rt.balance_snapshot = None
            rt.last_txn_id_snapshot = None

    if msg_type != "PRICE":
        return rt, None

    px = _bid_ask_mid(msg)
    if px is None:
        return rt, None
    bid, ask, mid = px

    prev = rt.day_state

    rt.day_state = update_state_from_tick(
        state=rt.day_state,
        tick_time_utc=ts_utc,
        mid_price=mid,
        trade_date_ny=rt.current_trade_date_ny,
        gate=gate,
    )

    if prev.first_sweep_side is None and rt.day_state.first_sweep_side is not None:
        _log_state("[SWEEP]", rt=rt, gate=gate, now_ny=now_ny)

    if prev.reentry_time_ny is None and rt.day_state.reentry_time_ny is not None:
        _log_state("[REENTRY]", rt=rt, gate=gate, now_ny=now_ny)

    if rt.day_state.max_penetration_pips > cfg.gate_max_sweep_depth_pips:
        _log_state("[NO TRADE SUMMARY]", rt=rt, gate=gate, now_ny=now_ny)
        print(
            f"[EXIT] {rt.current_trade_date_ny.date()} no trade today (deep sweep {rt.day_state.max_penetration_pips:.2f}p).",
            flush=True,
        )
        return rt, 0

    if rt.day_state.first_sweep_time_ny is not None and rt.day_state.reentry_time_ny is None:
        deadline = rt.day_state.first_sweep_time_ny + timedelta(minutes=cfg.reentry_deadline_minutes)
        if now_ny > deadline:
            _log_state("[NO TRADE SUMMARY]", rt=rt, gate=gate, now_ny=now_ny)
            print(f"[EXIT] {rt.current_trade_date_ny.date()} no trade today (reentry missed).", flush=True)
            return rt, 0

    decision = evaluate_decision(state=rt.day_state, gate=gate)
    if decision.reason != "PASS":
        return rt, None

    if decision.first_sweep_side == "HIGH":
        is_long = False
    elif decision.first_sweep_side == "LOW":
        is_long = True
    else:
        _log_state("[NO TRADE SUMMARY]", rt=rt, gate=gate, now_ny=now_ny)
        print(f"[EXIT] {rt.current_trade_date_ny.date()} no trade today (invalid sweep side).", flush=True)
        return rt, 0

    if rt.nav_for_sizing is None:
        print("[EXIT] Missing NAV snapshot (account summary) — refusing to place trade.", flush=True)
        return rt, 2

    raw_units = _compute_units_from_nav(nav_usd=rt.nav_for_sizing, stop_pips=risk.stop_pips, risk_pct=risk_pct)
    sized_units = max(min_units, min(max_units, abs(raw_units)))
    units = sized_units if is_long else -sized_units

    sl, tp = _sl_tp_from_mid(mid, is_long=is_long, stop_pips=risk.stop_pips, tp_pips=risk.take_profit_pips)
    tag = f"fxtrader_candidate_v1_{rt.current_trade_date_ny.date()}"

    print(
        f"[SIZING] NAV={rt.nav_for_sizing:.2f} risk_pct={risk_pct:.4f} stop_pips={risk.stop_pips:.2f} "
        f"raw_units={raw_units} clamped_units={sized_units}",
        flush=True,
    )

    try:
        open_before = _trade_ids_snapshot(client)
    except Exception:
        open_before = set()

    try:
        resp = client.place_market_order(
            instrument=cfg.instrument,
            units=units,
            stop_loss_price=sl,
            take_profit_price=tp,
            client_tag=tag,
        )
    except OandaApiError as e:
        print(f"[ORDER ERROR] {rt.current_trade_date_ny.date()} {e}", file=sys.stderr, flush=True)
        return rt, None

    trade_id = _extract_trade_id(resp) or _find_new_trade_id_by_diff(client, before_ids=open_before)
    if not trade_id:
        print(f"[ORDER WARN] Order placed but could not determine trade_id. resp_keys={list(resp.keys())}", flush=True)
        return rt, 2

    rt.traded = True
    rt.trade_id = trade_id
    rt.trade_open_time_ny = now_ny

    print(
        f"[ORDER PASS] {rt.current_trade_date_ny.date()} trade_id={trade_id} side={'LONG' if is_long else 'SHORT'} units={units} "
        f"bid={bid:.5f} ask={ask:.5f} mid={mid:.5f} SL={sl:.5f} TP={tp:.5f} "
        f"asia_range_pips={decision.asia_range_pips:.2f} sweep_depth_pips={decision.sweep_depth_pips:.2f} "
        f"sweep={decision.first_sweep_side}@{decision.first_sweep_time_ny} reentry={decision.reentry_time_ny}",
        flush=True,
    )

    return rt, None


def main() -> int:
    cfg = CANDIDATE_V1
    risk = CANDIDATE_V1_RISK

    gate = GateConfig(
        max_sweep_depth_pips=cfg.gate_max_sweep_depth_pips,
        min_asia_range_pips=cfg.gate_min_asia_range_pips,
        sweep_buffer_pips=cfg.sweep_buffer_pips,
        reentry_deadline_minutes=cfg.reentry_deadline_minutes,
    )

    oanda_cfg = load_oanda_config()
    client = OandaClient(oanda_cfg)

    risk_pct = float(getattr(risk, "risk_per_trade_pct"))
    min_units = int(getattr(risk, "min_units", 1))
    max_units_raw = getattr(risk, "max_units", None)
    max_units = int(max_units_raw) if max_units_raw is not None else 200_000

    print("=== trade_stream (cron worker) ===")
    print(f"OANDA_ENV={oanda_cfg.env} rest={oanda_cfg.base_url} stream={oanda_cfg.stream_url} account_id={oanda_cfg.account_id}")
    print(f"instrument={cfg.instrument}")
    print(
        f"candidate_v1: buffer={cfg.sweep_buffer_pips} re_deadline={cfg.reentry_deadline_minutes}m "
        f"gates: sweep<={cfg.gate_max_sweep_depth_pips} asia>={cfg.gate_min_asia_range_pips}"
    )
    print(
        f"risk: risk_pct={risk_pct} stop={risk.stop_pips}p tp={risk.take_profit_pips}p min_units={min_units} max_units={max_units}",
        flush=True,
    )
    print("Windows (NY): Asia=(D-1 19:00 -> D 03:00), British=(D 03:00 -> D 05:00)")
    print("Exit condition: exit only after trade is closed (SL/TP or +90m) OR we decide no trade today.", flush=True)

    today_ny = datetime.now(tz=NY).replace(hour=0, minute=0, second=0, microsecond=0)
    rt = RuntimeState(current_trade_date_ny=today_ny, day_state=LiveState())

    time_stop_minutes = 90
    consecutive_stream_failures = 0

    while True:
        now_ny = datetime.now(tz=NY)

        # If we're past the British window and no trade is open, there is no point reconnecting forever.
        _asia_start_ny, _asia_end_ny, _brit_start_ny, brit_end_ny, _double_sweep_end_ny = build_session_windows_for_date(
            rt.current_trade_date_ny
        )
        if now_ny >= brit_end_ny and not (rt.traded and rt.trade_id):
            diag = _summarize_no_trade(rt=rt, gate=gate, now_ny=now_ny)
            _log_state("[NO TRADE SUMMARY]", rt=rt, gate=gate, now_ny=now_ny)
            print(f"[EXIT] {rt.current_trade_date_ny.date()} no trade today (stream loop past British window). diag={diag}", flush=True)
            return 0

        try:
            print(
                f"[STREAM CONNECT] {datetime.now(tz=NY).isoformat()} "
                f"date={rt.current_trade_date_ny.date()} traded={rt.traded} trade_id={rt.trade_id}",
                flush=True,
            )
            for msg in client.stream_pricing(instruments=cfg.instrument):
                consecutive_stream_failures = 0
                rt, exit_code = _process_stream_message(
                    client=client,
                    cfg=cfg,
                    risk=risk,
                    gate=gate,
                    rt=rt,
                    msg=msg,
                    time_stop_minutes=time_stop_minutes,
                    risk_pct=risk_pct,
                    min_units=min_units,
                    max_units=max_units,
                )
                if exit_code is not None:
                    return exit_code

            print("[STREAM END] pricing stream ended cleanly; reconnecting.", flush=True)
            time.sleep(1)

        except (
            requests.exceptions.ChunkedEncodingError,
            requests.exceptions.ConnectionError,
            urllib3.exceptions.ProtocolError,
        ) as e:
            consecutive_stream_failures += 1
            print(
                f"[STREAM DROPPED] {datetime.now(tz=NY).isoformat()} "
                f"failure={consecutive_stream_failures} err={type(e).__name__}: {e}",
                file=sys.stderr,
                flush=True,
            )

            if consecutive_stream_failures >= 10:
                print("[STREAM ABORT] Too many consecutive stream failures.", file=sys.stderr, flush=True)
                return 2

            time.sleep(min(10, 1 + consecutive_stream_failures))
            continue

        except Exception as e:
            consecutive_stream_failures += 1
            print(
                f"[STREAM ERROR] {datetime.now(tz=NY).isoformat()} "
                f"failure={consecutive_stream_failures} err={type(e).__name__}: {e}",
                file=sys.stderr,
                flush=True,
            )

            if consecutive_stream_failures >= 5:
                print("[STREAM ABORT] Too many consecutive unexpected stream errors.", file=sys.stderr, flush=True)
                return 2

            time.sleep(min(10, 1 + consecutive_stream_failures))
            continue


if __name__ == "__main__":
    raise SystemExit(main())