"""Mean-reversion strategy — EXPERIMENTAL (not yet validated through the full
backtest pipeline; the default remains `trend`).

Rationale: the 1-year analysis showed that in the current bear/fear regime the
edge is mean-reversion — oversold bounces, not breakouts (this is why the
long-term trend filter was rejected). This strategy buys statistically stretched
dips and exits on reversion to the mean. It is provided to demonstrate the
plugin contract and as a candidate to backtest before the regime turns.

Selection is opt-in (`strategy: active: mean_reversion`); ship only after it
clears the same evidence bar as every other mechanism — see CONTRIBUTING.md.
"""
from __future__ import annotations

import pandas as pd

from agent.signals.technical import Action, avg_daily_range_pct, ema, rsi
from agent.strategies.base import MarketContext, Signal

MIN_BARS = 60
RSI_OVERSOLD = 30.0
RSI_MEAN = 50.0
STRETCH_REF_PCT = 6.0  # distance below the mean (% of price) that scores full conviction


class MeanReversionStrategy:
    name = "mean_reversion"

    def __init__(self, ema_span: int = 20, rsi_period: int = 14):
        self.ema_span = ema_span
        self.rsi_period = rsi_period

    def evaluate(self, ctx: MarketContext) -> Signal:
        closes = ctx.closes
        if len(closes) < MIN_BARS:
            return Signal(ctx.token, Action.HOLD, 0.0, False, 0.0,
                          "insufficient history")
        s = pd.Series(closes)
        price = float(s.iloc[-1])
        mean = float(ema(s, self.ema_span).iloc[-1])
        r = float(rsi(s, self.rsi_period).iloc[-1])
        drange = avg_daily_range_pct(s)
        below_mean_pct = (mean - price) / mean * 100 if mean else 0.0

        # Exit: reverted back to (or above) the mean, or momentum normalized.
        if ctx.holding:
            if price >= mean or r >= RSI_MEAN:
                return Signal(ctx.token, Action.SELL, 0.0, False, 0.0,
                              f"reverted: rsi {r:.0f}, {below_mean_pct:+.1f}% vs mean")
            return Signal(ctx.token, Action.HOLD, 0.0, False, 0.0,
                          f"holding dip: rsi {r:.0f}")

        # Entry: oversold AND stretched below the mean.
        if r <= RSI_OVERSOLD and below_mean_pct > 0:
            conv = max(0.0, min(1.0, below_mean_pct / STRETCH_REF_PCT))
            edge = min(below_mean_pct, drange)  # conservative: revert at most a daily range
            return Signal(ctx.token, Action.BUY, conv, False, edge,
                          f"oversold dip: rsi {r:.0f}, {below_mean_pct:.1f}% below mean",
                          daily_range_pct=drange)
        return Signal(ctx.token, Action.HOLD, 0.0, False, 0.0,
                      f"no setup: rsi {r:.0f}, {below_mean_pct:+.1f}% vs mean",
                      daily_range_pct=drange)
