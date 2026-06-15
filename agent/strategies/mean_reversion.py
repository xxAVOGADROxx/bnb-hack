"""Mean-reversion strategy — BACKTESTED AND REJECTED; kept as a worked example
of the plugin contract. The default remains `trend`.

Rationale tested: whether naive oversold-dip buying (RSI<=30 + below the mean)
captures the regime's mean-reversion. It does NOT. Head-to-head on the live
config (`scripts/strategy_bt.py`): on the meaningful windows it is gross-negative
and far worse than `trend` — 20d -5.49% (gross -135, 5/17 stops) and 1-year
-6.47% (14/25 stops) vs trend -0.41% / -1.65%. It catches falling knives: in a
downtrend "oversold" gets more oversold, so the dips keep dropping into the stop.

The lesson refines the earlier note: SHORT-TERM mean-reversion *with momentum
confirmation* (what `trend` already does via pullback + MACD) works; naive
oversold buying without confirmation does not. Do not select for production.
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
