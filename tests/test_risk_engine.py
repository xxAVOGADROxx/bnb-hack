from agent.config import DrawdownConfig, RiskConfig, TokensConfig
from agent.risk.engine import RiskEngine, RiskState, TradeProposal


def make_engine(allowlist=("BNB", "ETH", "USDT", "USDC")) -> RiskEngine:
    risk = RiskConfig(
        drawdown=DrawdownConfig(alert_pct=10, pause_entries_pct=15, hard_stop_pct=20),
        max_position_pct=25,
        max_concurrent=3,
        max_trades_per_day=4,
        max_slippage_pct=1.0,
        min_expected_edge_pct=2.0,
        daily_trade_deadline_utc="20:00",
        min_portfolio_usd=5.0,
        max_signal_age_min=10,
        cycle_interval_s=300,
        regime_cache_min=20,
    )
    tokens = TokensConfig(allowlist=allowlist, watchlist=("BNB",), stables=("USDT", "USDC"))
    return RiskEngine(risk, tokens)


def entry(usd=1000.0, edge=3.0, frm="USDT", to="BNB") -> TradeProposal:
    return TradeProposal(frm, to, usd, edge, is_entry=True, reason="test")


OK = dict(portfolio_usd=5000, state=RiskState.NORMAL, open_positions=0,
          trades_today=0, signal_age_min=1)


def test_drawdown_ladder():
    e = make_engine()
    assert e.drawdown_state(5000, 5000) == RiskState.NORMAL
    assert e.drawdown_state(4400, 5000) == RiskState.ALERT          # 12%
    assert e.drawdown_state(4200, 5000) == RiskState.PAUSE_ENTRIES  # 16%
    assert e.drawdown_state(3900, 5000) == RiskState.HARD_STOP      # 22%


def test_empty_allowlist_fails_closed():
    e = make_engine(allowlist=())
    assert not e.evaluate(entry(), **OK).approved


def test_token_outside_allowlist_rejected():
    e = make_engine()
    v = e.evaluate(entry(to="SCAMCOIN"), **OK)
    assert not v.approved and v.rule == "allowlist"


def test_entry_blocked_when_paused_but_exit_allowed():
    e = make_engine()
    paused = {**OK, "state": RiskState.PAUSE_ENTRIES}
    assert not e.evaluate(entry(), **paused).approved
    exit_trade = TradeProposal("BNB", "USDT", 1000, 0, is_entry=False, reason="exit")
    assert e.evaluate(exit_trade, **paused).approved


def test_hard_stop_allows_only_derisking():
    e = make_engine()
    stopped = {**OK, "state": RiskState.HARD_STOP}
    assert not e.evaluate(entry(), **stopped).approved
    flatten = TradeProposal("BNB", "USDT", 1000, 0, is_entry=False, reason="flatten")
    assert e.evaluate(flatten, **stopped).approved


def test_position_size_cap():
    e = make_engine()
    v = e.evaluate(entry(usd=2000), **OK)  # > 25% of 5000
    assert not v.approved and v.rule == "position_size"


def test_min_edge_floor():
    e = make_engine()
    v = e.evaluate(entry(edge=1.0), **OK)
    assert not v.approved and v.rule == "min_edge"


def test_daily_trade_cap():
    e = make_engine()
    v = e.evaluate(entry(), **{**OK, "trades_today": 4})
    assert not v.approved and v.rule == "daily_trade_cap"


def test_stale_signal_blocks_entry():
    e = make_engine()
    v = e.evaluate(entry(), **{**OK, "signal_age_min": 30})
    assert not v.approved and v.rule == "stale_data"


def test_clean_entry_approved():
    e = make_engine()
    assert e.evaluate(entry(usd=1200, edge=3.5), **OK).approved
