"""ATR/ADX (Wilder) sanity: units, ranges and regime discrimination."""
import pandas as pd

from agent.signals.technical import adx, atr_pct


def series(kind: str, n: int = 120):
    if kind == "trend":  # steady +1%/bar climb
        closes = pd.Series([100 * 1.01 ** i for i in range(n)])
    else:                # flat chop: ±0.2% around 100
        closes = pd.Series([100 + (0.2 if i % 2 else -0.2) for i in range(n)])
    highs = closes * 1.004
    lows = closes * 0.996
    return highs, lows, closes


def test_atr_pct_positive_and_scales_with_range():
    h, lo, c = series("chop")
    narrow = atr_pct(h, lo, c).iloc[-1]
    wide = atr_pct(c * 1.02, c * 0.98, c).iloc[-1]
    assert narrow > 0
    assert wide > narrow * 3  # 4% bar range vs ~0.8% -> materially larger


def test_adx_high_in_trend_low_in_chop():
    ht, lt, ct = series("trend")
    hc, lc, cc = series("chop")
    trending = adx(ht, lt, ct).iloc[-1]
    choppy = adx(hc, lc, cc).iloc[-1]
    assert trending > 25          # classic "trending" reading
    assert choppy < trending
    assert 0 <= choppy <= 100 and 0 <= trending <= 100


def test_adx_survives_constant_prices():
    c = pd.Series([100.0] * 80)
    out = adx(c, c, c)
    assert out.notna().iloc[-1] and 0 <= out.iloc[-1] <= 100
