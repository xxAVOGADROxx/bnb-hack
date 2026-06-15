"""CoinMarketCap Pro REST client.

Design rules:
- Always query by CMC `id`, never by symbol (symbols collide).
- Batch ids into single calls (one credit per quotes/latest batch).
- TTL cache per endpoint so the regime is recomputed every N minutes,
  not every tick, and credits are budgeted.
- Read-only: CMC thinks, TWAK executes.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://pro-api.coinmarketcap.com"


class CMCError(RuntimeError):
    pass


def usd_quote(coin: dict) -> dict:
    """Extract the USD quote dict from a coin object, tolerating the API's
    shape drift ("quote" sometimes arrives as a single-element list)."""
    q = coin.get("quote")
    if isinstance(q, list):
        q = q[0] if q else {}
    if not isinstance(q, dict):
        return {}
    usd = q.get("USD", q)
    if isinstance(usd, list):
        usd = usd[0] if usd else {}
    return usd if isinstance(usd, dict) else {}


class CMCClient:
    def __init__(self, api_key: str, timeout: int = 30):
        self.session = requests.Session()
        self.session.headers.update(
            {"X-CMC_PRO_API_KEY": api_key, "Accept": "application/json"}
        )
        self.timeout = timeout
        self._cache: dict[tuple, tuple[float, Any]] = {}

    def _get(self, path: str, params: dict | None = None, ttl_s: int = 0) -> dict:
        params = params or {}
        key = (path, tuple(sorted(params.items())))
        if ttl_s:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl_s:
                return hit[1]
        resp = self.session.get(f"{BASE_URL}{path}", params=params, timeout=self.timeout)
        body = resp.json()
        status = body.get("status", {})
        # error_code is int 0 on most endpoints but string "0" on some (e.g.
        # fear-and-greed) — normalize before deciding success.
        error_code = status.get("error_code")
        if resp.status_code != 200 or error_code not in (0, "0", None, ""):
            raise CMCError(
                f"CMC {path} -> {resp.status_code}: {status.get('error_message', resp.text[:200])}"
            )
        data = body.get("data", {})
        if ttl_s:
            self._cache[key] = (time.monotonic(), data)
        return data

    # -- one-time setup --------------------------------------------------
    def id_map(self, symbols: list[str]) -> dict:
        """Resolve symbols -> CMC ids once; cache the result on disk upstream."""
        return self._get("/v1/cryptocurrency/map", {"symbol": ",".join(symbols)})

    # -- layer 2: per-token signal ----------------------------------------
    def quotes_latest(self, ids: list[int], ttl_s: int = 60) -> dict:
        """Returns {id (int): coin object}. v3 responds with a list — normalize."""
        data = self._get(
            "/v3/cryptocurrency/quotes/latest",
            {"id": ",".join(map(str, ids)), "convert": "USD"},
            ttl_s=ttl_s,
        )
        # Observed shapes: top-level list of coins, dict keyed by id, and
        # dict whose values are single-element lists. Normalize all three.
        if isinstance(data, list):
            return {item["id"]: item for item in data}
        return {
            int(k): (v[0] if isinstance(v, list) and v else v)
            for k, v in data.items()
        }

    def ohlcv_latest(self, ids: list[int], ttl_s: int = 60) -> dict:
        return self._get(
            "/v2/cryptocurrency/ohlcv/latest",
            {"id": ",".join(map(str, ids)), "convert": "USD"},
            ttl_s=ttl_s,
        )

    def ohlcv_historical(self, id_: int, interval: str, count: int) -> dict:
        """NOT available on the Hobbyist tier (403) — kept for higher tiers."""
        return self._get(
            "/v2/cryptocurrency/ohlcv/historical",
            {"id": id_, "interval": interval, "count": count, "convert": "USD"},
        )

    def closes_historical(
        self, id_: int, interval: str = "1h", count: int = 200, ttl_s: int = 1800
    ) -> list[float]:
        """Historical close series via /v2/cryptocurrency/quotes/historical
        (verified available on this tier for 5m/1h/daily). Oldest first.
        EMA/MACD/RSI only need closes, so this replaces OHLCV for TA."""
        return [price for _, price in self.series_historical(id_, interval, count, ttl_s)]

    def series_historical(
        self, id_: int, interval: str = "1h", count: int = 200, ttl_s: int = 1800
    ) -> list[tuple[str, float]]:
        """Like closes_historical but keeps timestamps: [(iso_ts, price), ...]
        oldest first. NOTE: the Hobbyist plan caps history at 1 month."""
        data = self._get(
            "/v2/cryptocurrency/quotes/historical",
            {"id": id_, "interval": interval, "count": count, "convert": "USD"},
            ttl_s=ttl_s,
        )
        out = []
        for point in data.get("quotes", []):
            price = usd_quote(point).get("price")
            ts = point.get("timestamp") or usd_quote(point).get("timestamp")
            if price is not None and ts:
                out.append((ts, float(price)))
        return out

    def series_with_volume(
        self, id_: int, interval: str = "1h", count: int = 200, ttl_s: int = 1800
    ) -> list[tuple[str, float, float]]:
        """Like series_historical but also carries volume_24h per bar:
        [(iso_ts, price, volume_24h), ...] oldest first. One call yields both
        the TA closes and the volume-confirmation series (no extra credits)."""
        data = self._get(
            "/v2/cryptocurrency/quotes/historical",
            {"id": id_, "interval": interval, "count": count, "convert": "USD"},
            ttl_s=ttl_s,
        )
        out = []
        for point in data.get("quotes", []):
            q = usd_quote(point)
            price = q.get("price")
            ts = point.get("timestamp") or q.get("timestamp")
            if price is not None and ts:
                out.append((ts, float(price), float(q.get("volume_24h") or 0.0)))
        return out

    # -- layer 1: market regime -------------------------------------------
    def global_metrics(self, ttl_s: int = 1200) -> dict:
        return self._get("/v1/global-metrics/quotes/latest", {"convert": "USD"}, ttl_s=ttl_s)

    def fear_greed_latest(self, ttl_s: int = 1200) -> dict:
        # TODO: confirm exact path against https://pro.coinmarketcap.com/llms.txt
        return self._get("/v3/fear-and-greed/latest", ttl_s=ttl_s)

    # -- DEX API (pool-level, BSC) ------------------------------------------
    def dex_pair_quotes_latest(
        self, pool_addresses: list[str], ttl_s: int = 240
    ) -> dict[str, dict]:
        """CMC DEX API: {pool_address (lowercase): USD quote dict} with real
        pool `liquidity`. network_id 14 = BSC; batched (1 credit per pool).

        NOTE (verified 12 jun): the DEX *discovery* endpoint
        /v4/dex/spot-pairs/latest ignores all its documented filters
        (base_asset_contract_address, limit, ...) and returns an unfiltered
        firehose — pools must be derived deterministically (CREATE2, see
        agent/risk/liquidity.py) and quoted directly here instead.
        """
        data = self._get(
            "/v4/dex/pairs/quotes/latest",
            {"network_id": 14, "contract_address": ",".join(pool_addresses)},
            ttl_s=ttl_s,
        )
        out: dict[str, dict] = {}
        for d in data if isinstance(data, list) else []:
            q = d.get("quote")
            q = q[0] if isinstance(q, list) and q else q
            if isinstance(q, dict) and d.get("contract_address"):
                out[str(d["contract_address"]).lower()] = q
        return out

    # -- credit guardrail ---------------------------------------------------
    def key_info(self) -> dict:
        return self._get("/v1/key/info")
