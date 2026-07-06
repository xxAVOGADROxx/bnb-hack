"""DerivFeed: OKX payloads -> DerivView, squeeze fingerprint, fail-open."""
from agent.market.derivatives import DerivFeed, DerivView


def view(oi_chg=-5.0):
    return DerivView(token="ADA", funding_rate=-0.0002, oi_usd=20e6,
                     oi_chg_24h_pct=oi_chg, ls_ratio=1.8,
                     long_liq_usd=10_000, short_liq_usd=90_000,
                     liq_window_h=12.0)


def feed_with(routes: dict):
    f = DerivFeed()
    f._get = lambda path, params=None, ttl_s=0: routes[path]
    return f


ROUTES = {
    "/api/v5/public/instruments": [
        {"instId": "ADA-USDT-SWAP", "ctVal": "100"},
        {"instId": "ETH-USDT-SWAP", "ctVal": "0.1"},
    ],
    "/api/v5/public/funding-rate": [{"fundingRate": "-0.00025"}],
    # newest first; 25 rows so a 24h change is computable: 20M now vs 25M then
    "/api/v5/rubik/stat/contracts/open-interest-history":
        [["t", "1", "1", "20000000"]] + [["t", "1", "1", "22000000"]] * 23
        + [["t", "1", "1", "25000000"]],
    "/api/v5/rubik/stat/contracts/long-short-account-ratio": [["t", "1.81"]],
    "/api/v5/public/liquidation-orders": [{"details": [
        {"posSide": "short", "side": "buy", "sz": "1000", "bkPx": "0.20",
         "ts": "1783200000000"},
        {"posSide": "long", "side": "sell", "sz": "50", "bkPx": "0.19",
         "ts": "1783207200000"},
    ]}],
}


def test_snapshot_parses_all_components():
    v = feed_with(ROUTES).snapshot("ADA")
    assert v.funding_rate == -0.00025
    assert v.oi_usd == 20_000_000.0
    assert v.oi_chg_24h_pct == -20.0          # 20M vs 25M 24 bars ago
    assert v.ls_ratio == 1.81
    # short liq: 1000 contracts x 100 ADA x $0.20 = $20,000
    assert v.short_liq_usd == 20_000
    # long liq: 50 x 100 x $0.19 = $950
    assert v.long_liq_usd == 950
    assert v.liq_window_h == 2.0


def test_no_okx_swap_returns_none():
    assert feed_with(ROUTES).snapshot("CAKE") is None


def test_api_down_fails_open_to_none():
    f = DerivFeed()

    def boom(path, params=None, ttl_s=0):
        raise RuntimeError("503")
    f._get = boom
    assert f.snapshot("ADA") is None


def test_partial_outage_keeps_what_it_could_read():
    routes = dict(ROUTES)
    routes["/api/v5/rubik/stat/contracts/open-interest-history"] = []
    v = feed_with(routes).snapshot("ADA")
    assert v is not None and v.oi_usd is None and v.funding_rate == -0.00025


def test_squeeze_fingerprint_needs_both_legs():
    assert view(oi_chg=-5.0).squeeze_fingerprint(px_chg_24h_pct=3.0) is True
    assert view(oi_chg=-5.0).squeeze_fingerprint(px_chg_24h_pct=1.0) is False
    assert view(oi_chg=+4.0).squeeze_fingerprint(px_chg_24h_pct=3.0) is False
