"""Bounce strategy — BACKTESTED AND REJECTED. Kept registered (not active) as a
worked example of confirmation-gated counter-trend entry; the default remains
`trend`. Do NOT select for production without a backtest that beats `trend`.

Hypothesis tested: the `mean_reversion` rejection note says NAIVE oversold buying
catches falling knives, but that short-term mean-reversion *with momentum
confirmation* works. `trend` can't take these — it requires a full uptrend (EMA
fast > slow AND price above the slow EMA), so it sits out every counter-trend
bounce in a bear regime. `bounce` buys an oversold, below-mean token ONLY once
momentum has turned up (MACD histogram rising AND the last bar closed up), i.e.
after the snapback has started, not while the knife is still falling. The
expected-edge estimate is the distance back to the mean (capped at a daily
range), so only setups with real snapback room clear the universal min-edge and
per-token cost floors.

Verdict (scripts/strategy_bt.py, 25 jun 2026, live cfg) — LOSES in every window,
worse than `trend` in every window:
    window   bounce            trend     mean_reversion
    7d       -0.49% (0w/3)     0.00%     +0.24%
    20d      -1.09% (0w/6)    -0.34%     +0.08%
    1-year   -5.92% (3w/10)   +1.27%    -11.70%
Zero wins in both intraday windows — every entry lost. The confirmation gate did
NOT rescue it: in this regime counter-trend entries still get run over, and the
~1.3% round-trip friction eats the small snapbacks (7d: gross -$10.73 but fees
$22.72 -> net -$33.45). This EXTENDS the `mean_reversion` lesson: even WITH
momentum confirmation, counter-trend bounce buying is gross/net-negative against
the live fee floor. `trend` (few trades, only net-positive approach) stays the
production default.

Everything else — regime gate, volume confirmation, cooldown, vol-target sizing,
the fixed 8% stop and the drawdown ladder — is the shared safety layer in the
loop/risk engine and applies unchanged.
"""
from __future__ import annotations

import pandas as pd

from agent.signals.technical import Action, avg_daily_range_pct, ema, macd, rsi
from agent.strategies.base import MarketContext, Signal

MIN_BARS = 60


class BounceStrategy:
    name = "bounce"

    def __init__(
        self,
        ema_fast: int = 20,
        ema_slow: int = 50,
        rsi_period: int = 14,
        rsi_oversold: float = 35.0,
        rsi_exit: float = 55.0,
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        stretch_ref_pct: float = 4.0,  # below-mean % that scores full conviction
        min_conviction: float = 0.20,
    ):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_exit = rsi_exit
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.stretch_ref_pct = stretch_ref_pct
        self.min_conviction = min_conviction

    def evaluate(self, ctx: MarketContext) -> Signal:
        closes = ctx.closes
        if len(closes) < MIN_BARS:
            return Signal(ctx.token, Action.HOLD, 0.0, False, 0.0,
                          "insufficient history")
        s = pd.Series(list(closes), dtype=float)
        price = float(s.iloc[-1])
        prev = float(s.iloc[-2])
        mean = float(ema(s, self.ema_slow).iloc[-1])
        fast = float(ema(s, self.ema_fast).iloc[-1])
        r = float(rsi(s, self.rsi_period).iloc[-1])
        line, sig = macd(s, self.macd_fast, self.macd_slow, self.macd_signal)
        hist = line - sig
        hist_now = float(hist.iloc[-1])
        hist_prev = float(hist.iloc[-2])
        drange = avg_daily_range_pct(s)
        below_mean_pct = (mean - price) / mean * 100 if mean else 0.0

        # Exit: reverted to the mean, momentum normalized, or the bounce rolled
        # back over (momentum fading while still below the fast EMA = failed).
        if ctx.holding:
            if price >= mean:
                return Signal(ctx.token, Action.SELL, 1.0, False, 0.0,
                              f"reverted to mean: rsi {r:.0f}")
            if r >= self.rsi_exit:
                return Signal(ctx.token, Action.SELL, 1.0, False, 0.0,
                              f"momentum normalized: rsi {r:.0f}")
            if hist_now < hist_prev and price < fast:
                return Signal(ctx.token, Action.SELL, 1.0, False, 0.0,
                              f"bounce failed: momentum rolling over, rsi {r:.0f}")
            return Signal(ctx.token, Action.HOLD, 0.0, False, 0.0,
                          f"holding bounce: rsi {r:.0f}, {below_mean_pct:+.1f}% vs mean")

        # Entry: oversold AND below the mean AND momentum has TURNED UP
        # (histogram rising and the last bar closed up). The confirmation is
        # what separates this from naive knife-catching.
        oversold = r <= self.rsi_oversold
        stretched = below_mean_pct > 0
        momentum_turning = hist_now > hist_prev and price > prev
        if oversold and stretched and momentum_turning:
            conv = self.min_conviction + max(0.0, min(
                1.0, below_mean_pct / self.stretch_ref_pct)) * (1.0 - self.min_conviction)
            # Edge = snapback room to the mean, capped at ~1.5 daily ranges so a
            # deep stretch can't claim more than the token realistically travels.
            edge = min(below_mean_pct, 1.5 * drange) if drange > 0 else below_mean_pct
            return Signal(ctx.token, Action.BUY, conv, False, edge,
                          f"bounce: rsi {r:.0f}, {below_mean_pct:.1f}% below mean, "
                          f"momentum up (hist {hist_prev:+.4f}->{hist_now:+.4f})",
                          daily_range_pct=drange)
        return Signal(ctx.token, Action.HOLD, 0.0, False, 0.0,
                      f"no setup: rsi {r:.0f}, {below_mean_pct:+.1f}% vs mean, "
                      f"mom_up={momentum_turning}", daily_range_pct=drange)
