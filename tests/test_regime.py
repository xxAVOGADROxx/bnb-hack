"""Asymmetric F&G regime gate (#4)."""
from agent.signals.regime import Regime, classify

GM = {"btc_dominance": 55.0}


def fg(v):
    return {"value": v}


def test_extreme_greed_blocks_everything():
    v = classify(GM, fg(85))
    assert v.regime == Regime.RISK_OFF
    assert v.entry_conviction_floor == 0.0  # irrelevant: scale is 0


def test_extreme_fear_is_conflicted_with_conviction_floor():
    v = classify(GM, fg(15), fear_conviction_floor=0.45)
    assert v.regime == Regime.CONFLICTED
    assert v.entry_conviction_floor == 0.45
    assert "extreme fear" in v.detail


def test_neutral_band_is_risk_on_no_floor():
    for val in (25, 50, 79):
        v = classify(GM, fg(val))
        assert v.regime == Regime.RISK_ON
        assert v.entry_conviction_floor == 0.0


def test_boundaries():
    assert classify(GM, fg(20)).regime == Regime.CONFLICTED  # <=20 fear
    assert classify(GM, fg(80)).regime == Regime.RISK_OFF    # >=80 greed
    assert classify(GM, fg(21)).regime == Regime.RISK_ON


def test_incomplete_data_fails_cautious():
    assert classify({}, fg(50)).regime == Regime.CONFLICTED
    assert classify(GM, {}).regime == Regime.CONFLICTED
    assert classify(GM, {"value": "n/a"}).regime == Regime.CONFLICTED
