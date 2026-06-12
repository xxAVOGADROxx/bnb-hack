"""Liquidity sentinel (#7) — exit when the DEX pool backing a held token drains.

Price-based protections (stop-loss, EMA exit) lag the worst BEP-20 tail risk:
liquidity leaving the pool (rug, incentive migration, panic LP withdrawal).
By the time price prints the damage, the exit fills against a empty book.
This watches the *pool liquidity* of each held token via the CMC DEX API and
cuts the position when it drops hard below the entry-time baseline.

Reference pools are derived **deterministically** (PancakeSwap v2 CREATE2 —
factory + keccak256, verified against the canonical CAKE/WBNB pool) because
the DEX API's discovery endpoint ignores its filters. A token whose deepest
v2 pool is below `min_ref_usd` is *uncovered* (e.g. ZEC/BCH route through
other venues): the sentinel logs that once and stays out of the way.

Fail-open by design: no data / API error -> no forced action. This guards a
tail risk; the regular exits still own the common cases.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from Crypto.Hash import keccak

from agent.cmc.client import CMCClient, CMCError
from agent.state.store import StateStore

log = logging.getLogger(__name__)

# PancakeSwap v2 on BSC (pair addresses are CREATE2-deterministic).
_FACTORY = bytes.fromhex("ca143ce32fe78f1f7019d7d551a6402fc5350c73")
_INIT_CODE_HASH = bytes.fromhex(
    "00fb7f630766e6a796048ea87d01acd3068e8ff67d078148a3fa3f4a84f69bd5")
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
USDT = "0x55d398326f99059fF775485246999027B3197955"


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


class LiquiditySentinel:
    def __init__(self, cmc: CMCClient, store: StateStore,
                 min_ref_usd: float = 100_000.0, exit_drop_pct: float = 40.0):
        self.cmc = cmc
        self.store = store
        self.min_ref_usd = min_ref_usd
        self.exit_drop_pct = exit_drop_pct

    def _deepest_pool(self, token_address: str) -> tuple[str | None, float]:
        """Deepest of the token's USDT/WBNB v2 pools: (pool, liquidity_usd).
        (None, 0) when neither clears min_ref_usd -> token is uncovered."""
        pools = [pancake_v2_pair(token_address, USDT),
                 pancake_v2_pair(token_address, WBNB)]
        quotes = self.cmc.dex_pair_quotes_latest(pools)
        best, best_liq = None, 0.0
        for p in pools:
            liq = float(quotes.get(p.lower(), {}).get("liquidity") or 0.0)
            if liq > best_liq:
                best, best_liq = p, liq
        if best is None or best_liq < self.min_ref_usd:
            return None, best_liq
        return best, best_liq

    def on_entry(self, token: str, token_address: str) -> None:
        """Record the entry-time liquidity baseline (best-effort, fail-open)."""
        try:
            pool, liq = self._deepest_pool(token_address)
        except CMCError as e:
            log.warning("%s liquidity baseline unavailable (%s)", token, e)
            return
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
        try:
            quotes = self.cmc.dex_pair_quotes_latest([pool])
        except CMCError as e:
            log.warning("%s liquidity check unavailable (%s)", token, e)
            return None
        liq = float(quotes.get(pool.lower(), {}).get("liquidity") or 0.0)
        if liq <= 0:
            return None  # API returned nothing usable — fail open
        drop = (1 - liq / base_liq) * 100
        return LiquidityVerdict(
            exit_now=drop >= self.exit_drop_pct, drop_pct=round(drop, 1),
            pool=pool, baseline_usd=base_liq, current_usd=liq,
        )

    def clear(self, token: str) -> None:
        self.store.clear_pool_baseline(token)
