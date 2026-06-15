import math

from agent.signals.technical import Action, avg_daily_range_pct, evaluate

import pandas as pd


def trend(start: float, pct_per_bar: float, bars: int, wobble: float = 0.0) -> list[float]:
    out, price = [], start
    for i in range(bars):
        price *= 1 + pct_per_bar
        out.append(price * (1 + wobble * math.sin(i)))
    return out


def test_insufficient_history_holds():
    sig = evaluate("ETH", [100.0] * 10)
    assert sig.action == Action.HOLD and "insufficient" in sig.reason


def test_uptrend_after_pullback_buys():
    # The entry needs all four conditions at once, which in practice means a
    # base uptrend, a pullback that cools RSI, and a fresh push (MACD bull).
    closes, p = [], 100.0
    for i in range(150):
        p *= 1.007 if i % 2 == 0 else 0.996
        closes.append(p)
    for _ in range(30):
        p *= 0.998
        closes.append(p)
    for i in range(21):
        p *= 1.006 if i % 2 == 0 else 0.998
        closes.append(p)
    sig = evaluate("ETH", closes)
    assert sig.action == Action.BUY
    assert 0.0 < sig.conviction <= 1.0
    assert sig.expected_move_pct > 0


def test_parabolic_move_without_pullbacks_is_grey_zone():
    closes = trend(100, 0.004, 200, wobble=0.001)  # RSI pinned ~100
    sig = evaluate("ETH", closes)
    assert sig.action == Action.HOLD and sig.grey_zone


def test_downtrend_does_not_buy():
    closes = trend(100, -0.004, 200, wobble=0.001)
    sig = evaluate("ETH", closes)
    assert sig.action != Action.BUY


def test_downtrend_exits_when_holding():
    closes = trend(100, 0.004, 150) + trend(100 * 1.004**150, -0.01, 50)
    sig = evaluate("ETH", closes, holding=True)
    assert sig.action == Action.SELL


def test_not_holding_never_sells():
    closes = trend(100, -0.01, 200)
    sig = evaluate("ETH", closes, holding=False)
    assert sig.action == Action.HOLD


def test_avg_daily_range():
    flat = pd.Series([100.0] * 24 * 7)
    assert avg_daily_range_pct(flat) == 0.0
    moving = pd.Series(trend(100, 0.002, 24 * 7))
    assert avg_daily_range_pct(moving) > 0


# -- volume confirmation (#11) ----------------------------------------------
from agent.signals.technical import volume_confirms  # noqa: E402


def test_volume_confirms_rising_passes():
    # last bar above the trailing-24 mean -> entry allowed
    vols = [100.0] * 24 + [150.0]
    assert volume_confirms(vols, lookback=24, ratio=1.0) is True


def test_volume_confirms_fading_blocks():
    # last bar below the trailing mean -> entry blocked
    vols = [100.0] * 24 + [80.0]
    assert volume_confirms(vols, lookback=24, ratio=1.0) is False


def test_volume_confirms_ratio_threshold():
    # exactly at the mean passes 1.0 but fails a 1.15 ratio
    vols = [100.0] * 24 + [100.0]
    assert volume_confirms(vols, lookback=24, ratio=1.0) is True
    assert volume_confirms(vols, lookback=24, ratio=1.15) is False


def test_volume_confirms_disabled_or_short_series_passes():
    assert volume_confirms([100.0] * 25, lookback=24, ratio=0.0) is True   # disabled
    assert volume_confirms([100.0] * 10, lookback=24, ratio=1.0) is True   # too short
    assert volume_confirms([0.0] * 24 + [0.0], lookback=24, ratio=1.0) is True  # no volume data
