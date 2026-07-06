"""On-chain venue intelligence — free, keyless DEX aggregator APIs.

The signal stack reads CEX candles (Binance) but execution happens on
PancakeSwap: this module is the bot's eyes on the venue it actually trades.
Sources (all verified reachable from the host, no API key):

  - DexScreener  https://api.dexscreener.com   (300 req/min)
      per-token pool list with USD liquidity, price and buy/sell transaction
      counts per 5m/1h/6h/24h window -> aggregated PancakeSwap view here.
  - GeckoTerminal https://api.geckoterminal.com (30 req/min — scripts only)
      per-POOL hourly OHLCV, used by backtests to study on-chain flow.
  - DefiLlama    https://coins.llama.fi         (valuation cross-check)

Consumers:
  - LiquiditySentinel: aggregated PancakeSwap liquidity (V2+V3) replaces the
    V2-only getReserves depth, which missed tokens whose liquidity migrated
    to V3 pools (e.g. CAKE's main pool) and left ZEC/BCH "uncovered".
  - loop.py: shadow-logs pool order flow on BUY signals ("dex_flow" decision
    records) so a flow gate can be calibrated offline before it ever gates.

Fail-open everywhere: any network/shape error returns None and the caller
carries on — this layer adds information, never a new way to stop trading.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

DEXSCREENER_TOKENS = "https://api.dexscreener.com/latest/dex/tokens/{address}"
GECKOTERMINAL_OHLCV = ("https://api.geckoterminal.com/api/v2/networks/bsc"
                       "/pools/{pool}/ohlcv/{timeframe}")
LLAMA_PRICES = "https://coins.llama.fi/prices/current/bsc:{address}"

# Only the venue we execute on: depth elsewhere is depth we can't sell into.
_DEX_ID = "pancakeswap"
_CHAIN_ID = "bsc"


@dataclass(frozen=True)
class PoolView:
    """Aggregated PancakeSwap picture for one token (all its pools)."""
    token: str
    liquidity_usd: float        # summed across pools, V2 + V3
    main_pool: str              # deepest pool address
    main_pool_label: str        # "v2" / "v3" / "" when unlabeled
    price_usd: float | None     # main pool price
    buys_h1: int                # taker buys, summed across pools, last hour
    sells_h1: int
    vol_h24_usd: float

    @property
    def flow_ratio(self) -> float:
        """Buys per sell over the last hour (1.0 = balanced)."""
        return self.buys_h1 / max(self.sells_h1, 1)


class DexFeed:
    def __init__(self, timeout: int = 10, ttl_s: int = 180):
        self.timeout = timeout
        self.ttl_s = ttl_s
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._cache: dict[str, tuple[float, Any]] = {}

    def _get_json(self, url: str, params: dict | None = None) -> Any:
        """GET with TTL cache; raises on any failure (callers fail open)."""
        key = url + repr(sorted((params or {}).items()))
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.ttl_s:
            return hit[1]
        r = self.session.get(url, params=params, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        self._cache[key] = (time.monotonic(), data)
        return data

    # -- aggregated PancakeSwap view (live loop) -----------------------------
    def pool_view(self, symbol: str, address: str) -> PoolView | None:
        """Sum this token's PancakeSwap pools into one venue picture, or None
        (API down / no pools / unexpected shape). Never raises."""
        try:
            data = self._get_json(DEXSCREENER_TOKENS.format(address=address))
            pairs = _pancake_pairs(address, (data or {}).get("pairs") or [])
            if not pairs:
                return None
            liq = sum(_liq_usd(p) for p in pairs)
            buys = sum(_txn(p, "h1", "buys") for p in pairs)
            sells = sum(_txn(p, "h1", "sells") for p in pairs)
            vol = sum(float((p.get("volume") or {}).get("h24") or 0.0) for p in pairs)
            main = max(pairs, key=_liq_usd)
            price = main.get("priceUsd")
            return PoolView(
                token=symbol,
                liquidity_usd=liq,
                main_pool=str(main.get("pairAddress") or ""),
                main_pool_label=(main.get("labels") or [""])[0],
                price_usd=float(price) if price is not None else None,
                buys_h1=buys, sells_h1=sells, vol_h24_usd=vol,
            )
        except Exception as e:  # noqa: BLE001 — information layer: fail open
            log.debug("dexscreener pool_view failed for %s: %s", symbol, e)
            return None

    # -- valuation cross-check (scripts / diagnostics) -----------------------
    def llama_price_usd(self, address: str) -> float | None:
        try:
            data = self._get_json(LLAMA_PRICES.format(address=address))
            coin = (data or {}).get("coins", {}).get(f"bsc:{address}") or {}
            price = coin.get("price")
            return float(price) if price is not None else None
        except Exception as e:  # noqa: BLE001
            log.debug("defillama price failed for %s: %s", address, e)
            return None

    # -- per-pool hourly candles (backtests; 30 req/min — do NOT call in the
    #    live loop) -----------------------------------------------------------
    def pool_ohlcv(self, pool: str, timeframe: str = "hour", aggregate: int = 1,
                   limit: int = 500) -> list[tuple[int, float, float, float, float, float]]:
        """[(unix_ts, o, h, l, c, volume_usd), ...] oldest first; [] on error."""
        try:
            data = self._get_json(
                GECKOTERMINAL_OHLCV.format(pool=pool, timeframe=timeframe),
                {"aggregate": aggregate, "limit": min(int(limit), 1000)})
            rows = (((data or {}).get("data") or {}).get("attributes") or {}
                    ).get("ohlcv_list") or []
            return sorted(tuple(map(float, r)) for r in rows)
        except Exception as e:  # noqa: BLE001
            log.debug("geckoterminal ohlcv failed for %s: %s", pool, e)
            return []


def _pancake_pairs(address: str, pairs: list[dict]) -> list[dict]:
    """This token's PancakeSwap pools on BSC where it is the BASE token (as
    the quote token the buy/sell counts would read inverted)."""
    addr = address.lower()
    return [
        p for p in pairs
        if p.get("chainId") == _CHAIN_ID and p.get("dexId") == _DEX_ID
        and str((p.get("baseToken") or {}).get("address", "")).lower() == addr
    ]


def _liq_usd(pair: dict) -> float:
    return float((pair.get("liquidity") or {}).get("usd") or 0.0)


def _txn(pair: dict, window: str, side: str) -> int:
    return int(((pair.get("txns") or {}).get(window) or {}).get(side) or 0)
