# file: src/oanda/candle_cache.py
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple


@dataclass(frozen=True)
class CandleCacheKey:
    oanda_env: str
    instrument: str
    granularity: str
    trade_date_ny: str  # YYYY-MM-DD
    price: str  # "A" | "B" | "M"
    time_from_rfc3339: str
    time_to_rfc3339: str


class CandleCache:
    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = cache_dir

    def _path_for_key(self, key: CandleCacheKey) -> Path:
        base = (
            self._cache_dir
            / key.oanda_env
            / key.instrument
            / key.granularity
            / key.trade_date_ny
        )
        base.mkdir(parents=True, exist_ok=True)
        return base / f"{key.price}.json"

    def _meta_dict(self, key: CandleCacheKey) -> Dict[str, Any]:
        # Keep from/to for audit/debug, but do NOT require them to match for reuse.
        return {
            "oanda_env": key.oanda_env,
            "instrument": key.instrument,
            "granularity": key.granularity,
            "trade_date_ny": key.trade_date_ny,
            "price": key.price,
            "time_from_rfc3339": key.time_from_rfc3339,
            "time_to_rfc3339": key.time_to_rfc3339,
        }

    def load_if_valid(self, key: CandleCacheKey) -> Dict[str, Any] | None:
        path = self._path_for_key(key)
        if not path.exists():
            return None

        try:
            raw = json.loads(path.read_text())
        except Exception:
            return None

        if not isinstance(raw, dict):
            return None
        meta = raw.get("meta")
        payload = raw.get("payload")
        if not isinstance(meta, dict) or not isinstance(payload, dict):
            return None

        required = {
            "oanda_env": key.oanda_env,
            "instrument": key.instrument,
            "granularity": key.granularity,
            "trade_date_ny": key.trade_date_ny,
            "price": key.price,
        }
        for k, v in required.items():
            if meta.get(k) != v:
                return None

        return payload

    def save(self, key: CandleCacheKey, payload: Dict[str, Any]) -> None:
        path = self._path_for_key(key)
        obj = {"meta": self._meta_dict(key), "payload": payload}
        path.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))

    def get_or_fetch(
        self, *, key: CandleCacheKey, fetch_fn
    ) -> Tuple[Dict[str, Any], bool]:
        cached = self.load_if_valid(key)
        if cached is not None:
            return cached, True
        payload = fetch_fn()
        self.save(key, payload)
        return payload, False
