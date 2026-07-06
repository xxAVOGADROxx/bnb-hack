"""Liquidity sentinel (#7) — exit when the DEX pool backing a held token drains.

Price-based protections (stop-loss, EMA exit) lag the worst BEP-20 tail risk:
liquidity leaving the pool (rug, incentive migration, panic LP withdrawal).
By the time price prints the damage, the exit fills against an empty book.
This watches the *pool liquidity* of each held token and cuts the position
when it drops hard below the entry-time baseline.

Depth source (2026-07-05): AGGREGATED PancakeSwap liquidity across ALL the
token's pools (V2 + V3) via the free DexScreener API (agent/market/dex.py) —
the V2-only getReserves read underneath missed liquidity that migrated to V3
(CAKE's main pool is V3) and left ZEC/BCH "uncovered". Baselines record their
source ("agg:dexscreener" vs a concrete v2 pair address) and are only ever
compared against the SAME source, so a deploy or an API outage can't
manufacture a phantom drain.

Fallback: direct on-chain PancakeSwap v2 `getReserves()` via the shared RPC
pool (agent/chain.py) — CREATE2-derived USDT/WBNB pairs, pool USD = 2x the
quote-side reserve, BNB priced from the canonical WBNB/USDT pool. A token
below `min_ref_usd` in both sources is *uncovered*: logged once, left alone.

Fail-open by design: any API/RPC error / empty read -> no forced action. This
guards a tail risk; the regular exits still own the common cases.

Caveat (pre-existing): a WBNB-quoted pool's USD value moves with the BNB
price, so a very large BNB drop can read as a partial "drain". The 40%
default threshold, short momentum holds and max_concurrent:1 keep this
immaterial; a native-reserve baseline is the follow-up if it ever bites.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from Crypto.Hash import keccak

from agent.chain import Rpc
from agent.state.store import StateStore

log = logging.getLogger(__name__)

# PancakeSwap v2 on BSC (pair addresses are CREATE2-deterministic).
_FACTORY = bytes.fromhex("ca143ce32fe78f1f7019d7d551a6402fc5350c73")
_INIT_CODE_HASH = bytes.fromhex(
    "00fb7f630766e6a796048ea87d01acd3068e8ff67d078148a3fa3f4a84f69bd5")
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
USDT = "0x55d398326f99059fF775485246999027B3197955"
_QUOTE_DECIMALS = 18          # USDT and WBNB are both 18-decimal on BSC
_SEL_GET_RESERVES = "0902f1ac"  # getReserves() -> (uint112,uint112,uint32)


def _keccak(b: bytes) -> bytes:
    h = keccak.new(digest_bits=256)
    h.update(b)
    return h.digest()


def pancake_v2_pair(token_a: str, token_b: str) -> str:
    """Deterministic PancakeSwap v2 pair address for two BEP-20 tokens."""
    t0, t1 = sorted(t.lower().removeprefix("0x") for t in (token_a, token_b))
    salt = _keccak(bytes.fromhex(t0) + bytes.fromhex(t1))
    return "0x" + _keccak(b"\xff" + _FACTORY + salt + _INIT_CODE_HASH)[12:].hex()


@dataclass(frozen=True)
class LiquidityVerdict:
    exit_now: bool
    drop_pct: float
    pool: str
    baseline_usd: float
    current_usd: float

    @property
    def detail(self) -> str:
        return (f"pool {self.pool[:10]}.. liquidity ${self.current_usd:,.0f} "
                f"vs baseline ${self.baseline_usd:,.0f} ({self.drop_pct:+.1f}%)")


# Baseline "pool" tag for the aggregated DexScreener depth source. Baselines
# are compared strictly same-source: an AGG baseline is only re-read via the
# aggregator, a v2-pair baseline only via getReserves.
AGG_SOURCE = "agg:dexscreener"


class LiquiditySentinel:
    def __init__(self, store: StateStore, min_ref_usd: float = 100_000.0,
                 exit_drop_pct: float = 40.0, rpc: Rpc | None = None,
                 dex=None):
        self.store = store
        self.min_ref_usd = min_ref_usd
        self.exit_drop_pct = exit_drop_pct
        self.rpc = rpc or Rpc()
        self.dex = dex  # agent.market.dex.DexFeed | None (None -> v2-only)

    # -- on-chain reads ----------------------------------------------------
    def _reserves(self, pair: str) -> tuple[int, int] | None:
        """(reserve0, reserve1) raw, or None if the pair has no code / the read
        fails / the pool is empty. Fail-open: never raises."""
        try:
            raw = self.rpc.eth_call(pair, "0x" + _SEL_GET_RESERVES)
        except Exception as e:  # noqa: BLE001 — any node/decode error -> fail open
            log.debug("getReserves failed for %s: %s", pair, e)
            return None
        b = raw[2:] if raw.startswith("0x") else raw
        if len(b) < 128:  # no contract at the CREATE2 address -> "0x"
            return None
        r0, r1 = int(b[0:64], 16), int(b[64:128], 16)
        return (r0, r1) if (r0 or r1) else None

    @staticmethod
    def _quote_side(token_addr: str, quote_addr: str, r0: int, r1: int) -> int:
        """The reserve belonging to `quote_addr`. v2 orders reserves by
        token0 = the numerically lower address."""
        a = token_addr.lower().removeprefix("0x")
        q = quote_addr.lower().removeprefix("0x")
        return r0 if q < a else r1

    def _bnb_price_usd(self) -> float:
        """USD/BNB from the canonical WBNB/USDT v2 pool reserves (both 18-dec,
        so the ratio is the price). 0.0 if unreadable -> WBNB pools value 0."""
        res = self._reserves(pancake_v2_pair(WBNB, USDT))
        if not res:
            return 0.0
        r0, r1 = res
        usdt_res = self._quote_side(WBNB, USDT, r0, r1)
        wbnb_res = self._quote_side(USDT, WBNB, r0, r1)
        return usdt_res / wbnb_res if wbnb_res > 0 else 0.0

    def _pool_usd(self, token_addr: str, quote_addr: str,
                  quote_price: float) -> tuple[str, float]:
        """(pair, total_pool_usd). ~2x the quote-side value; 0 if unreadable."""
        pair = pancake_v2_pair(token_addr, quote_addr)
        res = self._reserves(pair)
        if not res:
            return pair, 0.0
        qres = self._quote_side(token_addr, quote_addr, *res)
        return pair, 2.0 * (qres / 10 ** _QUOTE_DECIMALS) * quote_price

    def _deepest_pool(self, token_address: str) -> tuple[str | None, float]:
        """Deepest of the token's USDT/WBNB v2 pools: (pool, liquidity_usd).
        (None, best_liq) when neither clears min_ref_usd -> token is uncovered."""
        candidates = [
            self._pool_usd(token_address, USDT, 1.0),
            self._pool_usd(token_address, WBNB, self._bnb_price_usd()),
        ]
        best, best_liq = None, 0.0
        for pool, liq in candidates:
            if liq > best_liq:
                best, best_liq = pool, liq
        if best is None or best_liq < self.min_ref_usd:
            return None, best_liq
        return best, best_liq

    def _pool_usd_for(self, token_address: str, pool: str) -> float:
        """Re-value a KNOWN baseline pool the same way it was first measured,
        inferring its quote token from which CREATE2 address it matches."""
        low = pool.lower()
        if low == pancake_v2_pair(token_address, USDT).lower():
            quote_addr, quote_price = USDT, 1.0
        elif low == pancake_v2_pair(token_address, WBNB).lower():
            quote_addr, quote_price = WBNB, self._bnb_price_usd()
        else:
            return 0.0  # unrecognized pool -> fail open
        res = self._reserves(pool)
        if not res:
            return 0.0
        qres = self._quote_side(token_address, quote_addr, *res)
        return 2.0 * (qres / 10 ** _QUOTE_DECIMALS) * quote_price

    # -- lifecycle ---------------------------------------------------------
    def on_entry(self, token: str, token_address: str) -> None:
        """Record the entry-time liquidity baseline (fail-open, never raises).
        Preferred source: aggregated PancakeSwap depth (V2+V3); if the
        aggregator is unreachable, fall back to the direct v2 read."""
        if self.dex is not None:
            view = None
            try:
                view = self.dex.pool_view(token, token_address)
            except Exception as e:  # noqa: BLE001 — belt and braces: fail open
                log.debug("dex pool_view raised for %s: %s", token, e)
            if view is not None and view.liquidity_usd > 0:
                if view.liquidity_usd < self.min_ref_usd:
                    log.info(
                        "%s: aggregated pancake liquidity $%.0f below $%.0f "
                        "floor — sentinel uncovered",
                        token, view.liquidity_usd, self.min_ref_usd)
                    self.store.record_pool_baseline(token, None, view.liquidity_usd)
                else:
                    self.store.record_pool_baseline(
                        token, AGG_SOURCE, view.liquidity_usd)
                return
        pool, liq = self._deepest_pool(token_address)
        if pool is None:
            log.info("%s: no v2 reference pool >= $%.0f — sentinel uncovered",
                     token, self.min_ref_usd)
        self.store.record_pool_baseline(token, pool, liq)

    def check(self, token: str, token_address: str) -> LiquidityVerdict | None:
        """For a held token: compare pool liquidity to the entry baseline.
        None = uncovered or no data this cycle (never forces an action)."""
        base = self.store.pool_baseline(token)
        if base is None:  # restart with an open position: adopt, don't guess
            self.on_entry(token, token_address)
            return None
        pool, base_liq = base.get("pool"), float(base.get("liq") or 0.0)
        if not pool or base_liq <= 0:
            return None  # uncovered token
        if pool == AGG_SOURCE:
            # Same-source comparison only: aggregator baseline needs an
            # aggregator read (an outage -> fail open, never a phantom drain).
            if self.dex is None:
                return None
            try:
                view = self.dex.pool_view(token, token_address)
            except Exception:  # noqa: BLE001
                return None
            if view is None or view.liquidity_usd <= 0:
                return None
            liq = view.liquidity_usd
        else:
            liq = self._pool_usd_for(token_address, pool)
        if liq <= 0:
            return None  # unreadable this cycle -> fail open
        drop = (1 - liq / base_liq) * 100
        return LiquidityVerdict(
            exit_now=drop >= self.exit_drop_pct, drop_pct=round(drop, 1),
            pool=pool, baseline_usd=base_liq, current_usd=liq,
        )

    def clear(self, token: str) -> None:
        self.store.clear_pool_baseline(token)
