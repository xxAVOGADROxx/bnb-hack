"""DexFeed: DexScreener payload -> aggregated PancakeSwap PoolView."""
from agent.market.dex import DexFeed, _pancake_pairs

CAKE = "0x0E09FaBB73Bd3Ade0a17ECC321fD13a19e81cE82"


def pair(dex="pancakeswap", chain="bsc", base=CAKE, liq=100_000.0,
         buys=10, sells=4, vol=50_000.0, price="1.40", label="v3",
         addr="0xPOOL"):
    return {
        "chainId": chain, "dexId": dex, "pairAddress": addr,
        "labels": [label],
        "baseToken": {"address": base, "symbol": "Cake"},
        "quoteToken": {"address": "0xWBNB", "symbol": "WBNB"},
        "priceUsd": price,
        "txns": {"h1": {"buys": buys, "sells": sells}},
        "volume": {"h24": vol},
        "liquidity": {"usd": liq},
    }


def feed_with(payload):
    f = DexFeed()
    f._get_json = lambda url, params=None: payload
    return f


def test_aggregates_across_pancake_pools_and_picks_deepest():
    payload = {"pairs": [
        pair(liq=300_000.0, buys=5, sells=1, addr="0xDEEP", label="v3"),
        pair(liq=100_000.0, buys=2, sells=3, addr="0xSHALLOW", label="v2"),
        pair(dex="biswap", liq=999_999.0),          # other venue: excluded
        pair(chain="ethereum", liq=999_999.0),      # other chain: excluded
        pair(base="0xOTHER", liq=999_999.0),        # token is quote: excluded
    ]}
    v = feed_with(payload).pool_view("CAKE", CAKE)
    assert v.liquidity_usd == 400_000.0
    assert v.main_pool == "0xDEEP" and v.main_pool_label == "v3"
    assert v.buys_h1 == 7 and v.sells_h1 == 4
    assert v.vol_h24_usd == 100_000.0
    assert v.price_usd == 1.40
    assert round(v.flow_ratio, 2) == 1.75


def test_base_token_filter_is_case_insensitive():
    pairs = [pair(base=CAKE.upper())]
    assert len(_pancake_pairs(CAKE.lower(), pairs)) == 1


def test_no_pancake_pools_returns_none():
    assert feed_with({"pairs": [pair(dex="biswap")]}).pool_view("X", CAKE) is None
    assert feed_with({"pairs": []}).pool_view("X", CAKE) is None
    assert feed_with({}).pool_view("X", CAKE) is None


def test_api_error_fails_open_to_none():
    f = DexFeed()

    def boom(url, params=None):
        raise RuntimeError("504")
    f._get_json = boom
    assert f.pool_view("CAKE", CAKE) is None


def test_flow_ratio_never_divides_by_zero():
    v = feed_with({"pairs": [pair(buys=8, sells=0)]}).pool_view("CAKE", CAKE)
    assert v.flow_ratio == 8.0
