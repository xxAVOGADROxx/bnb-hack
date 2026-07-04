"""Momentum / breakout strategy — the "navaja" (10% high-variance sleeve).

DELIBERATELY -EV in the current regime, and deployed anyway by explicit user
decision (26 jun) as a BOUNDED-LOSS LOTTERY TICKET on a final-days move — NOT an
edge. Backtests this session (scratchpad navaja_bt / navaja2_bt) showed breakout
entries on the eligible liquid universe found ~1 signal in 10 days and lost, and
"let it run" did worse than selling at the mean — there is no asymmetric upside
to harvest in our universe right now. So this rides on hope, with a hard cap that
makes a full wipeout survivable:

  config when active: max_position_pct:10, max_concurrent:1  -> worst case ~-10%
  of portfolio = the drawdown ladder's ALERT level, far from pause(15)/stop(20)
  and the ~30% DQ. Revert to `mean_reversion` (defensive) after the window.

Plays the leaders' game (momentum) on a slice that cannot DQ us. Entry: a fresh
LOOKBACK-bar breakout with momentum turning up and not yet exhausted. Exit: let
winners run while price holds the fast EMA; sell when momentum breaks back below
it (or MACD rolls over) — take whatever the run gave, uncapped on the way up. The
shared safety layer (regime gate, volume confirm, vol-target sizing, fixed 8%
stop, drawdown ladder) lives in the loop/risk engine and applies unchanged.
"""
from __future__ import annotations

import pandas as pd

from agent.signals.technical import Action, avg_daily_range_pct, ema, macd, rsi
from agent.strategies.base import MarketContext, Signal

MIN_BARS = 60


class MomentumStrategy:
    name = "momentum"

    def __init__(
        self,
        lookback: int = 24,        # bars for the breakout high (1 day on 1h)
        ema_fast: int = 9,         # the trailing line we ride; break below = exit
        rsi_lo: float = 50.0,      # entry needs momentum...
        rsi_hi: float = 75.0,      # ...but not a blow-off top
        macd_fast: int = 12,
        macd_slow: int = 26,
        macd_signal: int = 9,
        min_conviction: float = 0.30,
        breakout_ref_pct: float = 2.0,  # breakout size that scores full conviction
        min_edge_pct: float = 3.0,      # claim a daily-range continuation (clears the gate)
        # Exit hysteresis — best combo from scripts/exit_hysteresis_bt.py
        # (2026-07-04): cuts net loss ~35% across 240h/720h x 1%/2% cost, no
        # extra stop-outs. The marginal trigger was the MACD leg: one falling
        # hist bar on a down tick round-tripped entries in minutes.
        exit_ema_buffer_pct: float = 1.0,  # EMA break must exceed this margin
        exit_confirm_bars: int = 2,        # consecutive bars below the EMA required
        macd_confirm_bars: int = 2,        # consecutive falling-hist bars required
    ):
        self.lookback = lookback
        self.ema_fast = ema_fast
        self.rsi_lo = rsi_lo
        self.rsi_hi = rsi_hi
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.min_conviction = min_conviction
        self.breakout_ref_pct = breakout_ref_pct
        self.min_edge_pct = min_edge_pct
        self.exit_ema_buffer_pct = exit_ema_buffer_pct
        self.exit_confirm_bars = max(1, exit_confirm_bars)
        self.macd_confirm_bars = max(1, macd_confirm_bars)

    def evaluate(self, ctx: MarketContext) -> Signal:
        closes = ctx.closes
        if len(closes) < MIN_BARS:
            return Signal(ctx.token, Action.HOLD, 0.0, False, 0.0,
                          "insufficient history")
        s = pd.Series(list(closes), dtype=float)
        price = float(s.iloc[-1])
        prev = float(s.iloc[-2])
        fast_series = ema(s, self.ema_fast)
        fast = float(fast_series.iloc[-1])
        r = float(rsi(s, 14).iloc[-1])
        line, sig = macd(s, self.macd_fast, self.macd_slow, self.macd_signal)
        hist = line - sig
        hist_now = float(hist.iloc[-1])
        hist_prev = float(hist.iloc[-2])
        drange = avg_daily_range_pct(s)
        prior_high = float(s.iloc[-(self.lookback + 1):-1].max())
        breakout_pct = (price / prior_high - 1) * 100 if prior_high else 0.0

        # Exit: let it run while it holds the fast EMA; sell when momentum breaks
        # back below it, or MACD rolls over on a down bar. The 8% stop backstops a
        # gap-down between cycles. Hysteresis (backtested 2026-07-04): the break
        # must clear a margin below the EMA and/or persist for N bars, so a
        # 0.3% wiggle minutes after entry doesn't round-trip us into friction.
        if ctx.holding:
            ema_break = all(
                float(s.iloc[-k]) < float(fast_series.iloc[-k])
                * (1 - self.exit_ema_buffer_pct / 100)
                for k in range(1, self.exit_confirm_bars + 1)
            )
            macd_break = price < prev and all(
                float(hist.iloc[-k]) < float(hist.iloc[-k - 1])
                for k in range(1, self.macd_confirm_bars + 1)
            )
            if ema_break or macd_break:
                return Signal(ctx.token, Action.SELL, 1.0, False, 0.0,
                              f"momentum break: rsi {r:.0f}, {breakout_pct:+.1f}% vs {self.lookback}h high")
            return Signal(ctx.token, Action.HOLD, 0.0, False, 0.0,
                          f"riding momentum: rsi {r:.0f}, {breakout_pct:+.1f}% vs {self.lookback}h high")

        # Entry: a fresh breakout, momentum turning up, not yet exhausted.
        broke_out = breakout_pct > 0
        momentum_up = hist_now > hist_prev and price > prev
        not_exhausted = self.rsi_lo <= r <= self.rsi_hi
        if broke_out and momentum_up and not_exhausted:
            conv = self.min_conviction + max(0.0, min(
                1.0, breakout_pct / self.breakout_ref_pct)) * (1.0 - self.min_conviction)
            edge = max(drange, self.min_edge_pct)
            return Signal(ctx.token, Action.BUY, conv, False, edge,
                          f"breakout: rsi {r:.0f}, +{breakout_pct:.1f}% over {self.lookback}h high, "
                          f"momentum up (hist {hist_prev:+.4f}->{hist_now:+.4f})",
                          daily_range_pct=drange)
        return Signal(ctx.token, Action.HOLD, 0.0, False, 0.0,
                      f"no breakout: rsi {r:.0f}, {breakout_pct:+.1f}% vs {self.lookback}h high")
