"""Liquidity sentinel (#7): CREATE2 pool derivation + drain detection via the
aggregated DexScreener depth source (2026-07-05), with same-source baselines
and fail-open on any missing data."""
from agent.market.dex import PoolView
from agent.risk.liquidity import (
    AGG_SOURCE, USDT, WBNB, LiquiditySentinel, pancake_v2_pair,
)
from agent.state.store import StateStore

CAKE = "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82"
CAKE_WBNB_POOL = "0x0ed7e52944161450477ee417de9cd3a859b14fd0"  # canonical


def view(liq: float) -> PoolView:
    return PoolView(token="CAKE", liquidity_usd=liq, main_pool="0xpool",
                    main_pool_label="v3", price_usd=1.4, buys_h1=10,
                    sells_h1=5, vol_h24_usd=1_000_000.0)


class FakeDex:
    """pool_view returns the canned view; None simulates an API outage."""
    def __init__(self, v: PoolView | None):
        self.v = v

    def pool_view(self, symbol, address):
        return self.v


class ExplodingRpc:
    """The v2 fallback must not be reached when the aggregator answers."""
    def eth_call(self, *a, **k):
        raise AssertionError("on-chain fallback used despite dex data")


def make(tmp_path, dex):
    store = StateStore(path=tmp_path / "state.json")
    return LiquiditySentinel(store, min_ref_usd=100_000, exit_drop_pct=40,
                             rpc=ExplodingRpc(), dex=dex), store


def test_pair_derivation_matches_canonical_pool():
    assert pancake_v2_pair(CAKE, WBNB) == CAKE_WBNB_POOL
    assert pancake_v2_pair(WBNB, CAKE) == CAKE_WBNB_POOL  # order-independent
    assert pancake_v2_pair(CAKE, USDT) != CAKE_WBNB_POOL


def test_agg_baseline_recorded_and_drain_detected(tmp_path):
    s, store = make(tmp_path, FakeDex(view(1_000_000.0)))
    s.on_entry("CAKE", CAKE)
    base = store.pool_baseline("CAKE")
    assert base["pool"] == AGG_SOURCE and base["liq"] == 1_000_000.0

    v = s.check("CAKE", CAKE)               # unchanged liquidity -> hold
    assert v is not None and not v.exit_now

    s.dex = FakeDex(view(550_000.0))        # -45% -> drain
    v = s.check("CAKE", CAKE)
    assert v.exit_now and v.drop_pct == 45.0

    s.clear("CAKE")
    assert store.pool_baseline("CAKE") is None


def test_below_floor_is_uncovered_and_never_forces_action(tmp_path):
    s, store = make(tmp_path, FakeDex(view(50_000.0)))  # < min_ref_usd
    s.on_entry("ZEC", CAKE)
    assert store.pool_baseline("ZEC")["pool"] is None
    assert s.check("ZEC", CAKE) is None      # fail-open


def test_api_outage_fails_open_never_phantom_drain(tmp_path):
    s, store = make(tmp_path, FakeDex(view(500_000.0)))
    s.on_entry("CAKE", CAKE)
    s.dex = FakeDex(None)                    # aggregator down this cycle
    assert s.check("CAKE", CAKE) is None
    s.dex = None                             # source removed entirely
    assert s.check("CAKE", CAKE) is None


def test_restart_adopts_baseline_then_monitors(tmp_path):
    s, store = make(tmp_path, FakeDex(view(800_000.0)))
    # restart with an open position: first check adopts, second monitors
    assert s.check("CAKE", CAKE) is None
    v = s.check("CAKE", CAKE)
    assert v is not None and not v.exit_now


def test_legacy_v2_baseline_still_compared_on_chain(tmp_path):
    """A pre-deploy baseline (concrete v2 pair address) keeps using the
    on-chain read — never the aggregator (same-source rule)."""
    reserves = 200_000 * 10**18  # quote-side USDT reserve -> $400k pool

    class FakeRpc:
        def eth_call(self, pair, data):
            r0 = f"{reserves:064x}"
            r1 = f"{reserves:064x}"
            ts = f"{0:064x}"
            return "0x" + r0 + r1 + ts

    store = StateStore(path=tmp_path / "state.json")
    s = LiquiditySentinel(store, min_ref_usd=100_000, exit_drop_pct=40,
                          rpc=FakeRpc(), dex=FakeDex(view(9_999_999.0)))
    usdt_pair = pancake_v2_pair(CAKE, USDT)
    store.record_pool_baseline("CAKE", usdt_pair, 400_000.0)
    v = s.check("CAKE", CAKE)
    assert v is not None and v.pool == usdt_pair and not v.exit_now
