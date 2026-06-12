"""Liquidity sentinel (#7): CREATE2 pool derivation + drain detection."""
from agent.risk.liquidity import (
    USDT, WBNB, LiquiditySentinel, pancake_v2_pair,
)
from agent.state.store import StateStore

CAKE = "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82"
CAKE_WBNB_POOL = "0x0ed7e52944161450477ee417de9cd3a859b14fd0"  # canonical


class FakeCMC:
    """Maps pool address (lower) -> liquidity; raising or omitting = no data."""
    def __init__(self, liquidity: dict[str, float]):
        self.liquidity = liquidity

    def dex_pair_quotes_latest(self, pools, ttl_s=240):
        return {p.lower(): {"liquidity": self.liquidity[p.lower()]}
                for p in pools if p.lower() in self.liquidity}


def make(tmp_path, liquidity):
    store = StateStore(path=tmp_path / "state.json")
    return LiquiditySentinel(FakeCMC(liquidity), store,
                             min_ref_usd=100_000, exit_drop_pct=40), store


def test_pair_derivation_matches_canonical_pool():
    assert pancake_v2_pair(CAKE, WBNB) == CAKE_WBNB_POOL
    assert pancake_v2_pair(WBNB, CAKE) == CAKE_WBNB_POOL  # order-independent


def test_baseline_recorded_and_drain_detected(tmp_path):
    pool = pancake_v2_pair(CAKE, WBNB)
    liq = {pool: 1_000_000.0, pancake_v2_pair(CAKE, USDT).lower(): 50_000.0}
    liq = {k.lower(): v for k, v in liq.items()}
    s, store = make(tmp_path, liq)

    s.on_entry("CAKE", CAKE)
    assert store.pool_baseline("CAKE")["pool"].lower() == pool.lower()

    v = s.check("CAKE", CAKE)               # unchanged liquidity -> hold
    assert v is not None and not v.exit_now

    s.cmc.liquidity[pool.lower()] = 550_000.0   # -45% -> drain
    v = s.check("CAKE", CAKE)
    assert v.exit_now and v.drop_pct == 45.0

    s.clear("CAKE")
    assert store.pool_baseline("CAKE") is None


def test_uncovered_token_never_forces_action(tmp_path):
    s, store = make(tmp_path, {})            # no pool clears the floor
    s.on_entry("ZEC", CAKE)
    assert store.pool_baseline("ZEC")["pool"] is None
    assert s.check("ZEC", CAKE) is None      # fail-open


def test_restart_adopts_baseline_then_monitors(tmp_path):
    pool = pancake_v2_pair(CAKE, WBNB).lower()
    s, store = make(tmp_path, {pool: 800_000.0})
    # restart with an open position: first check adopts, second monitors
    assert s.check("CAKE", CAKE) is None
    v = s.check("CAKE", CAKE)
    assert v is not None and not v.exit_now


def test_api_failure_fails_open(tmp_path):
    pool = pancake_v2_pair(CAKE, WBNB).lower()
    s, store = make(tmp_path, {pool: 500_000.0})
    s.on_entry("CAKE", CAKE)
    s.cmc.liquidity = {}                      # API returns nothing usable
    assert s.check("CAKE", CAKE) is None
