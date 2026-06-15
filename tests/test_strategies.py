import pytest

from agent.signals import technical
from agent.signals.technical import Action
from agent.strategies import registry
from agent.strategies.base import MarketContext, Strategy


def ctx(closes, holding=False):
    return MarketContext("TKN", closes, [1.0] * len(closes), holding=holding)


def test_registry_lists_and_builds():
    assert set(registry.available()) >= {"trend", "mean_reversion"}
    s = registry.build("trend")
    assert isinstance(s, Strategy) and s.name == "trend"


def test_unknown_strategy_fails_loud():
    with pytest.raises(ValueError, match="unknown strategy"):
        registry.build("does_not_exist")


def test_trend_wrapper_matches_technical():
    closes = [100.0 * (1.003 if i % 2 == 0 else 0.999) for i in range(120)]
    direct = technical.evaluate("TKN", closes, holding=False)
    plugin = registry.build("trend").evaluate(ctx(closes))
    assert plugin.action == direct.action
    assert plugin.conviction == direct.conviction


def test_mean_reversion_buys_oversold_dip():
    # steady decline -> price below its EMA, RSI deeply oversold
    closes = [100.0 * (0.99 ** i) for i in range(120)]
    sig = registry.build("mean_reversion").evaluate(ctx(closes))
    assert sig.action == Action.BUY
    assert 0.0 < sig.conviction <= 1.0


def test_mean_reversion_exits_when_reverted_above_mean():
    closes = [100.0 * (1.01 ** i) for i in range(120)]  # uptrend, price above EMA
    sig = registry.build("mean_reversion").evaluate(ctx(closes, holding=True))
    assert sig.action == Action.SELL


def test_mean_reversion_holds_without_setup():
    closes = [100.0 + (i % 3) for i in range(120)]  # flat, not oversold
    sig = registry.build("mean_reversion").evaluate(ctx(closes))
    assert sig.action == Action.HOLD
