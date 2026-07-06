"""Free market-data feed — a drop-in for the paid CMC client's *read* surface.

agent/loop.py consumed four things from CoinMarketCap Pro (a $35/mo key that
expired 2026-07-03 and, valuing the whole book at $0, tripped a false HARD
STOP). This feed serves the same interface from keyless, free public sources:

  - series_with_volume() : hourly closes + volume  -> Binance public klines
  - quotes_latest()      : live spot price          -> Binance ticker (stop-loss only)
  - fear_greed_latest()  : Fear & Greed index       -> alternative.me
  - global_metrics()     : BTC dominance            -> CoinGecko global

Holdings VALUATION is deliberately *not* here: it is done on-chain via the
PancakeSwap execution client (agent/execution/pancake.py:price_usd), so the
mark equals what a sell would realize and a dead feed can never again value
the wallet at $0. See agent/state/reconcile.py.

Callers key everything by CMC integer id (already threaded through loop.py);
we reverse that to a symbol via the persisted data/id_map.json and query the
matching Binance USDT pair. Failures raise FeedError: loop.py's freshness
gates catch it (no data -> no new entries).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

import requests

log = logging.getLogger(__name__)


class FeedError(RuntimeError):
    """A market-data read failed. Loop treats it as a freshness gate."""


# Legacy alias: agent/cmc/client.py (old backtest scripts) re-exports this
# same class, so `except CMCError` and `except FeedError` are equivalent.
CMCError = FeedError


def usd_quote(coin: dict) -> dict:
    """Extract the USD quote dict from a CMC-shaped coin object, tolerating
    shape drift ("quote" sometimes arrives as a single-element list). The free
    feed emits the same shape, so downstream parsing is unchanged."""
    q = coin.get("quote")
    if isinstance(q, list):
        q = q[0] if q else {}
    if not isinstance(q, dict):
        return {}
    usd = q.get("USD", q)
    if isinstance(usd, list):
        usd = usd[0] if usd else {}
    return usd if isinstance(usd, dict) else {}

# data-api.binance.vision is the public market-data mirror: keyless and, unlike
# api.binance.com, not geo-restricted on cloud hosts. Fall through to the main
# hosts if it is ever unreachable.
BINANCE_HOSTS = (
    "https://data-api.binance.vision",
    "https://api.binance.com",
    "https://api-gcp.binance.com",
)
FNG_URL = "https://api.alternative.me/fng/"
COINGECKO_GLOBAL = "https://api.coingecko.com/api/v3/global"

# Watchlist symbols whose Binance ticker differs from the CMC symbol. Empty for
# the current universe (all trade as <SYMBOL>USDT); kept as the extension point.
SYMBOL_OVERRIDES: dict[str, str] = {}


class MarketFeed:
    def __init__(self, registry, timeout: int = 15):
        self.registry = registry
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._cache: dict[tuple, tuple[float, Any]] = {}
        # CMC id -> symbol (reverse of registry.id_map).
        self._id_to_symbol: dict[int, str] = {
            int(meta["id"]): sym
            for sym, meta in (getattr(registry, "id_map", {}) or {}).items()
            if isinstance(meta, dict) and meta.get("id") is not None
        }

    # -- infra --------------------------------------------------------------
    def _get(self, url: str, params: dict | None = None, ttl_s: int = 0) -> Any:
        key = (url, tuple(sorted((params or {}).items())))
        if ttl_s:
            hit = self._cache.get(key)
            if hit and time.monotonic() - hit[0] < ttl_s:
                return hit[1]
        try:
            r = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as e:
            raise FeedError(f"{url} -> request failed: {e}") from e
        if r.status_code != 200:
            raise FeedError(f"{url} -> {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
        except ValueError as e:
            raise FeedError(f"{url} -> non-JSON response: {e}") from e
        if ttl_s:
            self._cache[key] = (time.monotonic(), data)
        return data

    def _get_binance(self, path: str, params: dict, ttl_s: int = 0) -> Any:
        """GET a Binance market-data path, failing over across public hosts."""
        last: Exception | None = None
        for host in BINANCE_HOSTS:
            try:
                return self._get(f"{host}{path}", params, ttl_s=ttl_s)
            except FeedError as e:
                last = e
                continue
        raise FeedError(f"binance {path} unreachable on all hosts: {last}")

    def _symbol(self, cmc_id) -> str:
        sym = self._id_to_symbol.get(int(cmc_id))
        if not sym:
            raise FeedError(f"no symbol for CMC id {cmc_id!r} in id_map")
        return SYMBOL_OVERRIDES.get(sym, sym)

    def _pair(self, cmc_id) -> str:
        return f"{self._symbol(cmc_id)}USDT"

    # -- per-token signal series -------------------------------------------
    def series_with_volume(
        self, id_, interval: str = "1h", count: int = 200, ttl_s: int = 240
    ) -> list[tuple[str, float, float]]:
        """[(iso_ts, close, volume), ...] oldest first — same shape CMC gave.

        `volume` is a reconstructed rolling-24h quote volume (sum of the last
        24 hourly bars) so the calibrated volume-confirmation gate keeps the
        meaning it had against CMC's volume_24h field."""
        pair = self._pair(id_)
        raw = self._get_binance(
            "/api/v3/klines",
            {"symbol": pair, "interval": interval, "limit": min(int(count), 1000)},
            ttl_s=ttl_s,
        )
        if not isinstance(raw, list) or not raw:
            raise FeedError(f"no klines for {pair}")
        # kline row: [openTime, open, high, low, close, vol, closeTime, quoteVol, ...]
        ts = [datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).isoformat() for k in raw]
        closes = [float(k[4]) for k in raw]
        qvols = [float(k[7]) for k in raw]
        win = 24 if interval == "1h" else 1
        out = []
        for i in range(len(closes)):
            v24 = sum(qvols[max(0, i - win + 1): i + 1])
            out.append((ts[i], closes[i], v24))
        return out

    def ohlcv_series(
        self, id_, interval: str = "1h", count: int = 200, ttl_s: int = 240
    ) -> list[tuple[str, float, float, float, float]]:
        """[(iso_ts, high, low, close, volume_24h), ...] oldest first — the
        true-OHLC companion of series_with_volume (ATR/ADX need highs/lows).
        Same klines request underneath, so the two share one HTTP cache entry
        and one rate-limit slot."""
        pair = self._pair(id_)
        raw = self._get_binance(
            "/api/v3/klines",
            {"symbol": pair, "interval": interval, "limit": min(int(count), 1000)},
            ttl_s=ttl_s,
        )
        if not isinstance(raw, list) or not raw:
            raise FeedError(f"no klines for {pair}")
        win = 24 if interval == "1h" else 1
        qvols = [float(k[7]) for k in raw]
        out = []
        for i, k in enumerate(raw):
            ts = datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc).isoformat()
            v24 = sum(qvols[max(0, i - win + 1): i + 1])
            out.append((ts, float(k[2]), float(k[3]), float(k[4]), v24))
        return out

    # -- live spot price (stop-loss check only; NOT valuation) -------------
    def quotes_latest(self, ids: list[int], ttl_s: int = 60) -> dict:
        """{cmc_id: {"quote": {"USD": {"price": float}}}} so usd_quote() works
        unchanged. Used for the ~1-min-fresh stop-loss check in loop.py."""
        pairs: dict[int, str] = {}
        for cid in ids:
            try:
                pairs[int(cid)] = self._pair(cid)
            except FeedError:
                continue
        if not pairs:
            return {}
        symbols_param = "[" + ",".join(f'"{p}"' for p in pairs.values()) + "]"
        data = self._get_binance(
            "/api/v3/ticker/price", {"symbols": symbols_param}, ttl_s=ttl_s
        )
        by_pair = {d["symbol"]: float(d["price"]) for d in data} if isinstance(data, list) else {}
        out = {}
        for cid, pair in pairs.items():
            p = by_pair.get(pair)
            if p is not None:
                out[cid] = {"quote": {"USD": {"price": p}}}
        return out

    # -- market regime ------------------------------------------------------
    def global_metrics(self, ttl_s: int = 1200) -> dict:
        try:
            data = self._get(COINGECKO_GLOBAL, ttl_s=ttl_s)
            dom = ((data or {}).get("data", {}) or {}).get(
                "market_cap_percentage", {}).get("btc")
            return {"btc_dominance": float(dom) if dom is not None else None}
        except FeedError as e:
            # Non-fatal: regime.classify treats a missing dominance as
            # fail-cautious (CONFLICTED), same as it did on a CMC hiccup.
            log.warning("btc dominance unavailable (%s)", e)
            return {"btc_dominance": None}

    def fear_greed_latest(self, ttl_s: int = 1200) -> dict:
        data = self._get(FNG_URL, {"limit": 1}, ttl_s=ttl_s)
        arr = (data or {}).get("data") or []
        if not arr:
            raise FeedError("fear&greed: empty response")
        return {"value": float(arr[0]["value"])}
