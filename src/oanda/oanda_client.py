# file: src/oanda/oanda_client.py
"""
Minimal OANDA v20 REST client for fetching candles.

Auth / environment:
- OANDA_ENV controls which host + key to use ("practice" default)
    - practice  -> https://api-fxpractice.oanda.com -> uses OANDA_PRACTICE_KEY
    - live      -> https://api-fxtrade.oanda.com    -> uses OANDA_LIVE_KEY
- Backwards-compatible fallback: if the env-specific key is not set, falls back to OANDA_KEY.

Notes:
- All env vars are stripped to avoid trailing newline/whitespace issues.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, Optional

import requests


@dataclass(frozen=True)
class OandaConfig:
    """Holds connection configuration for OANDA REST API."""
    api_key: str
    base_url: str


class OandaApiError(RuntimeError):
    """Raised when OANDA returns an error response."""


def load_oanda_config() -> OandaConfig:
    """
    Load OANDA config from environment variables.

    Required:
      - OANDA_ENV: "practice" or "live" (default: "practice")
      - OANDA_PRACTICE_KEY when OANDA_ENV=practice
      - OANDA_LIVE_KEY when OANDA_ENV=live

    Backwards-compatible fallback:
      - OANDA_KEY (used if the env-specific key is not set)
    """
    environment = os.environ.get("OANDA_ENV", "practice")
    environment = environment.strip().lower()

    if environment not in ("practice", "live"):
        raise ValueError('OANDA_ENV must be "practice" or "live"')

    key_var = "OANDA_PRACTICE_KEY" if environment == "practice" else "OANDA_LIVE_KEY"
    api_key = (os.environ.get(key_var) or os.environ.get("OANDA_KEY") or "").strip()

    if not api_key:
        raise ValueError(
            f"Missing environment variable {key_var} (or fallback OANDA_KEY). "
            f"Current OANDA_ENV={environment!r}"
        )

    base_url = (
        "https://api-fxpractice.oanda.com"
        if environment == "practice"
        else "https://api-fxtrade.oanda.com"
    )

    return OandaConfig(api_key=api_key, base_url=base_url)


class OandaClient:
    """
    Very small wrapper around the OANDA REST API.

    This is intentionally minimal:
      - one session
      - one helper to GET JSON
      - candles fetcher
    """

    def __init__(self, config: OandaConfig, timeout_seconds: int = 20) -> None:
        self._config = config
        self._timeout_seconds = timeout_seconds

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self._config.api_key}",
                "Accept-Datetime-Format": "RFC3339",
            }
        )

    def _get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        url = f"{self._config.base_url}{path}"
        response = self._session.get(url, params=params, timeout=self._timeout_seconds)

        # Helpful error message when debugging auth / rate-limit / bad request issues.
        if not response.ok:
            try:
                payload = response.json()
            except Exception:
                payload = {"raw_text": response.text}

            raise OandaApiError(
                f"OANDA request failed: {response.status_code} {response.reason} "
                f"url={url} params={params} payload={payload}"
            )

        return response.json()

    def get_candles(
        self,
        instrument: str,
        granularity: str,
        time_from_rfc3339: str,
        time_to_rfc3339: str,
        price: str = "M",
    ) -> Dict[str, Any]:
        """
        Fetch candles for an instrument.

        granularity examples: "M1", "M5", "H1"
        price: "M" (mid), "B" (bid), "A" (ask)
        """
        return self._get_json(
            f"/v3/instruments/{instrument}/candles",
            params={
                "granularity": granularity,
                "from": time_from_rfc3339,
                "to": time_to_rfc3339,
                "price": price,
            },
        )