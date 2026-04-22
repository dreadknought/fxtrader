# C:\Users\dread\dev\fxtrader\src\oanda\oanda_client.py

from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter


@dataclass(frozen=True)
class OandaConfig:
    """
    Your env selection rule:
      - OANDA_ENV == "live" => live
      - anything else       => practice
    """

    api_key: str
    base_url: str  # REST host
    stream_url: str  # STREAM host
    account_id: str
    env: str  # "live" or "practice"


class OandaApiError(RuntimeError):
    """Raised when OANDA returns an error response."""


def _strip_quotes(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
        return v[1:-1]
    return v


def _parse_env_line(line: str) -> Optional[Tuple[str, str]]:
    """
    Very small dotenv parser:
      - ignores blank lines and comments starting with '#'
      - supports KEY=VALUE
      - supports quoted values
      - supports optional 'export KEY=VALUE'
    """
    s = line.strip()
    if not s or s.startswith("#"):
        return None

    if s.startswith("export "):
        s = s[len("export ") :].strip()

    if "=" not in s:
        return None

    key, val = s.split("=", 1)
    key = key.strip()
    if not key:
        return None

    # Allow inline comments if value is unquoted (simple heuristic)
    val = val.strip()
    if val and val[0] not in {"'", '"'}:
        if " #" in val:
            val = val.split(" #", 1)[0].rstrip()
        elif "\t#" in val:
            val = val.split("\t#", 1)[0].rstrip()

    val = _strip_quotes(val)
    return key, val


def _load_dotenv_file(path: Path, *, override: bool) -> bool:
    """
    Loads KEY=VALUE lines into os.environ.
    Returns True if the file existed and was loaded, else False.
    """
    if not path.exists() or not path.is_file():
        return False

    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            parsed = _parse_env_line(raw)
            if not parsed:
                continue
            k, v = parsed
            if override or (k not in os.environ):
                os.environ[k] = v
        return True
    except Exception:
        # If dotenv loading fails, we intentionally do not crash here;
        # load_oanda_config() will raise a clear error if required vars are missing.
        return False


def _maybe_load_env() -> None:
    """
    Ensure env vars exist in non-interactive contexts (cron / task scheduler / systemd).

    Search order:
      1) FXTRADER_ENV_FILE (explicit path)
      2) ./.env.trade      (repo-local recommended)
      3) ./.env

    Behavior:
      - By default, does NOT override existing os.environ values.
      - To override, set FXTRADER_ENV_OVERRIDE=1.
    """
    override = (os.environ.get("FXTRADER_ENV_OVERRIDE") or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "y",
    }

    explicit = (os.environ.get("FXTRADER_ENV_FILE") or "").strip()
    if explicit:
        _load_dotenv_file(Path(explicit).expanduser(), override=override)
        return

    cwd = Path.cwd()
    if _load_dotenv_file(cwd / ".env.trade", override=override):
        return
    _load_dotenv_file(cwd / ".env", override=override)


def load_oanda_config() -> OandaConfig:
    # Load dotenv-style env for cron/task-scheduler contexts
    _maybe_load_env()

    env_raw = (os.environ.get("OANDA_ENV") or "").strip().lower()
    is_live = env_raw == "live"
    env = "live" if is_live else "practice"

    api_key = (
        os.environ.get("OANDA_LIVE_KEY")
        if is_live
        else os.environ.get("OANDA_PRACTICE_KEY") or ""
    ).strip()
    if not api_key:
        missing = "OANDA_LIVE_KEY" if is_live else "OANDA_PRACTICE_KEY"
        raise ValueError(f"Missing {missing} (OANDA_ENV={env_raw!r} -> env={env!r}).")

    account_id = (
        os.environ.get("OANDA_LIVE_ACCOUNT_ID")
        if is_live
        else os.environ.get("OANDA_PRACTICE_ACCOUNT_ID") or ""
    ).strip()
    if not account_id:
        missing = "OANDA_LIVE_ACCOUNT_ID" if is_live else "OANDA_PRACTICE_ACCOUNT_ID"
        raise ValueError(f"Missing {missing} (OANDA_ENV={env_raw!r} -> env={env!r}).")

    base_url = (
        "https://api-fxtrade.oanda.com"
        if is_live
        else "https://api-fxpractice.oanda.com"
    )
    stream_url = (
        "https://stream-fxtrade.oanda.com"
        if is_live
        else "https://stream-fxpractice.oanda.com"
    )

    return OandaConfig(
        api_key=api_key,
        base_url=base_url,
        stream_url=stream_url,
        account_id=account_id,
        env=env,
    )


class _TCPKeepAliveAdapter(HTTPAdapter):
    """
    Small adapter that enables TCP keepalive where the platform supports it.
    This does not guarantee prevention of stream disconnects, but it can help
    long-lived sockets fail less dumbly through intermediaries/NATs.
    """

    def init_poolmanager(self, *args, **kwargs):
        socket_options = list(HTTPAdapter().socket_options)

        # Enable SO_KEEPALIVE where available.
        if hasattr(socket, "SO_KEEPALIVE"):
            socket_options.append((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1))

        # Linux-specific keepalive tuning, guarded so Windows/macOS do not explode.
        if hasattr(socket, "TCP_KEEPIDLE"):
            socket_options.append((socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30))
        if hasattr(socket, "TCP_KEEPINTVL"):
            socket_options.append((socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 10))
        if hasattr(socket, "TCP_KEEPCNT"):
            socket_options.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3))

        kwargs["socket_options"] = socket_options
        return super().init_poolmanager(*args, **kwargs)


class OandaClient:
    def __init__(
        self,
        config: OandaConfig,
        *,
        rest_timeout_seconds: int = 20,
        stream_connect_timeout_seconds: int = 20,
        stream_read_timeout_seconds: int = 90,
    ) -> None:
        self._config = config
        self._rest_timeout_seconds = rest_timeout_seconds
        self._stream_connect_timeout_seconds = stream_connect_timeout_seconds
        self._stream_read_timeout_seconds = stream_read_timeout_seconds

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self._config.api_key}",
                "Accept-Datetime-Format": "RFC3339",
                "Content-Type": "application/json",
            }
        )

        # Mount adapters for both REST and streaming hosts.
        adapter = _TCPKeepAliveAdapter(pool_connections=10, pool_maxsize=10)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

    @property
    def env(self) -> str:
        return self._config.env

    @property
    def base_url(self) -> str:
        return self._config.base_url

    @property
    def stream_url(self) -> str:
        return self._config.stream_url

    @property
    def account_id(self) -> str:
        return self._config.account_id

    def _get_json(
        self, url: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        r = self._session.get(url, params=params, timeout=self._rest_timeout_seconds)
        if not r.ok:
            try:
                payload = r.json()
            except Exception:
                payload = {"raw_text": r.text}
            raise OandaApiError(
                f"OANDA GET failed: {r.status_code} {r.reason} url={url} params={params} payload={payload}"
            )
        return r.json()

    def _post_json(self, url: str, body: Dict[str, Any]) -> Dict[str, Any]:
        r = self._session.post(
            url, data=json.dumps(body), timeout=self._rest_timeout_seconds
        )
        if not r.ok:
            try:
                payload = r.json()
            except Exception:
                payload = {"raw_text": r.text}
            raise OandaApiError(
                f"OANDA POST failed: {r.status_code} {r.reason} url={url} body={body} payload={payload}"
            )
        return r.json()

    def _put_json(
        self, url: str, body: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        r = self._session.put(
            url, data=json.dumps(body or {}), timeout=self._rest_timeout_seconds
        )
        if not r.ok:
            try:
                payload = r.json()
            except Exception:
                payload = {"raw_text": r.text}
            raise OandaApiError(
                f"OANDA PUT failed: {r.status_code} {r.reason} url={url} body={body} payload={payload}"
            )
        return r.json()

    # ------------------------
    # REST: Candles
    # ------------------------
    def get_candles(
        self,
        instrument: str,
        granularity: str,
        time_from_rfc3339: str,
        time_to_rfc3339: str,
        price: str = "M",
    ) -> Dict[str, Any]:
        url = f"{self._config.base_url}/v3/instruments/{instrument}/candles"
        return self._get_json(
            url,
            params={
                "granularity": granularity,
                "from": time_from_rfc3339,
                "to": time_to_rfc3339,
                "price": price,
            },
        )

    # ------------------------
    # STREAM: Pricing
    # ------------------------
    def stream_pricing(
        self, *, instruments: str
    ) -> Generator[Dict[str, Any], None, None]:
        url = f"{self._config.stream_url}/v3/accounts/{self._config.account_id}/pricing/stream"
        params = {"instruments": instruments}

        with self._session.get(
            url,
            params=params,
            stream=True,
            timeout=(
                self._stream_connect_timeout_seconds,
                self._stream_read_timeout_seconds,
            ),
        ) as r:
            if not r.ok:
                try:
                    payload = r.json()
                except Exception:
                    payload = {"raw_text": r.text}
                raise OandaApiError(
                    f"OANDA pricing stream failed: {r.status_code} {r.reason} url={url} params={params} payload={payload}"
                )

            for raw_line in r.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue

                try:
                    msg = json.loads(raw_line)
                except json.JSONDecodeError:
                    # Ignore junk lines, but do not pretend they are impossible.
                    # The caller owns reconnect logic; malformed single lines should
                    # not kill the whole worker.
                    continue

                if isinstance(msg, dict):
                    yield msg

    # ------------------------
    # REST: Orders
    # ------------------------
    def place_market_order(
        self,
        *,
        instrument: str,
        units: int,
        stop_loss_price: Optional[float] = None,
        take_profit_price: Optional[float] = None,
        client_tag: Optional[str] = None,
    ) -> Dict[str, Any]:
        if units == 0:
            raise ValueError("units must be non-zero")

        order: Dict[str, Any] = {
            "type": "MARKET",
            "instrument": instrument,
            "units": str(int(units)),
            "timeInForce": "FOK",
            "positionFill": "DEFAULT",
        }
        if client_tag:
            order["clientExtensions"] = {"tag": client_tag}
        if stop_loss_price is not None:
            order["stopLossOnFill"] = {"price": f"{stop_loss_price:.5f}"}
        if take_profit_price is not None:
            order["takeProfitOnFill"] = {"price": f"{take_profit_price:.5f}"}

        url = f"{self._config.base_url}/v3/accounts/{self._config.account_id}/orders"
        return self._post_json(url, {"order": order})

    # ------------------------
    # REST: Trades management
    # ------------------------
    def get_open_trades(self) -> List[Dict[str, Any]]:
        """
        Returns list of open trades. Each trade has fields like:
          - id
          - instrument
          - currentUnits
          - price
          - unrealizedPL
        """
        url = (
            f"{self._config.base_url}/v3/accounts/{self._config.account_id}/openTrades"
        )
        payload = self._get_json(url)
        return payload.get("trades", [])

    def close_trade(self, *, trade_id: str) -> Dict[str, Any]:
        url = f"{self._config.base_url}/v3/accounts/{self._config.account_id}/trades/{trade_id}/close"
        return self._put_json(url, body={})

    # ------------------------
    # REST: Account + Transactions
    # ------------------------
    def get_account_summary(self) -> Dict[str, Any]:
        """
        GET /v3/accounts/{accountID}/summary
        Useful fields: account.balance, account.NAV, account.lastTransactionID, etc.
        """
        url = f"{self._config.base_url}/v3/accounts/{self._config.account_id}/summary"
        return self._get_json(url)

    def get_transactions_since_id(self, *, since_id: str) -> Dict[str, Any]:
        """
        GET /v3/accounts/{accountID}/transactions/sinceid?id={since_id}
        Returns transactions after the given id and includes a lastTransactionID.
        """
        url = f"{self._config.base_url}/v3/accounts/{self._config.account_id}/transactions/sinceid"
        return self._get_json(url, params={"id": since_id})