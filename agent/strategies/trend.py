"""Trend-following strategy — the default, and the one with backtest evidence.

This is the production signal: EMA structure + MACD momentum + RSI, requiring all
conditions to align for an entry, with a dynamic conviction score and a grey-zone
flag for the x402 premium tie-break. It wraps `agent/signals/technical.py` so the
existing, validated logic is unchanged; the plugin layer only makes it selectable
alongside other strategies.
"""
from __future__ import annotations

from agent.signals import technical
from agent.signals.technical import DEFAULT_PARAMS, SignalParams
from agent.strategies.base import MarketContext, Signal


class TrendStrategy:
    name = "trend"

    def __init__(self, params: SignalParams = DEFAULT_PARAMS):
        self.params = params

    def evaluate(self, ctx: MarketContext) -> Signal:
        return technical.evaluate(
            ctx.token, ctx.closes, holding=ctx.holding, params=self.params)
