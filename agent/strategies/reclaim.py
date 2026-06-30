"""Reclaim strategy — double-bottom / floor-reclaim bounce entry.

Hypothesis under test (user idea, 25 jun 2026, live crash): after a sustained
drop, a token retests a recent floor (the "0.1398 tocado dos veces" double
bottom) and turns up; buy once upward momentum is CONFIRMED by consecutive
higher closes, not while the knife is still falling. This differs from `bounce`
(which gates on MACD-histogram turn + below-mean) by gating on (a) proximity to
the rolling N-bar low and (b) K consecutive up bars, plus a conviction bonus
when a genuine double-bottom retest is present.

Entry (flat), ALL must hold:
  - downtrend backdrop: price below the slow EMA,
  - near a recent floor: within `near_low_pct` of the `down_lookback`-bar low,
  - reclaim confirmed: `consecutive_up` consecutive higher closes ending now.
Conviction scales with how stretched below the mean we are, and gets a bonus
when the floor was tested twice (two troughs near the low, separated in time =
double bottom). Edge estimate = snapback room to the slow EMA, capped at ~1.5
daily ranges, so only setups with real room clear the min-edge / cost floors.

Exit (holding):
  - reverted to the mean (price >= slow EMA) -> take it,
  - reclaim failed: the last bar closed down while still below the fast EMA.

NOTE: `consecutive_up` defaults to 3 (not the user's intraday 5). On hourly
bars 5 consecutive up closes almost never occurs -> ~0 trades -> inconclusive;
3 is the MORE PERMISSIVE setting (more entries, closer to the bottom), so it is
a GENEROUS test of the idea. If 3 loses, the stricter 5-green version loses
harder (later entries, same friction).

VERDICT (scripts/strategy_bt.py, 25 jun 2026, live cfg) — REJECTED, does NOT
beat `trend`. Kept registered (NOT active); default stays `trend`.
    window   reclaim           trend
    7d       -0.15% (1t,0w)    0.00%
    20d      -0.31% (2t,0w)   -0.66%
    1-year    0.00% (0 trades) +1.27%
Two failure modes, both fatal: (a) when it DOES fire (intraday) it goes 0-for —
gross was ~flat/tiny-positive (+$0.86 / +$0.04) but the ~1.3% round-trip
friction turned every bounce into a net loss (7d: net -$7.43); (b) the
double-bottom + consecutive-up gate is so restrictive it NEVER triggered on the
1-year daily series (0 trades) — i.e. it would sit out the whole year, the
opposite of "take the opportunity". Even in the live crash window the idea was
built for, the rule traded once and lost to fees. EXTENDS the `bounce` /
`mean_reversion` lesson: confirmation-gated counter-trend bounce buying is
gross-thin and net-negative against the live fee floor. `trend` stays default.

Everything else — regime gate, volume confirmation, cooldown, vol-target
sizing, the fixed 8% stop and the drawdown ladder — is the shared safety layer
in the loop/risk engine and applies unchanged.
"""
from __future__ import annotations

import pandas as pd

from agent.signals.technical import Action, avg_daily_range_pct, ema, rsi
from agent.strategies.base import MarketContext, Signal

MIN_BARS = 60


def _consecutive_up(s: pd.Series) -> int:
    """Count of consecutive higher closes ending at the last bar."""
    c = 0
    for i in range(len(s) - 1, 0, -1):
        if s.iloc[i] > s.iloc[i - 1]:
            c += 1
        else:
            break
    return c


def _double_bottom(window: pd.Series, near_low_pct: float, min_sep: int) -> bool:
    """True if the window's low was tested at least twice: two bars within
    `near_low_pct` of the window minimum, separated by >= `min_sep` bars (so a
    single multi-bar dip doesn't count as two touches)."""
    lo = float(window.min())
    if lo <= 0:
        return False
    band = lo * (1.0 + near_low_pct / 100.0)
    touches = [i for i, v in enumerate(window.tolist()) if v <= band]
    return len(touches) >= 2 and (touches[-1] - touches[0]) >= min_sep


class ReclaimStrategy:
    name = "reclaim"

    def __init__(
        self,
        ema_fast: int = 20,
        ema_slow: int = 50,
        rsi_period: int = 14,
        rsi_exit: float = 60.0,
        down_lookback: int = 20,   # window for the "recent floor"
        near_low_pct: float = 2.0,  # how close to that floor counts as a retest
        consecutive_up: int = 3,    # higher closes required to confirm the turn
        min_sep: int = 3,           # bar gap for a double bottom to count
        stretch_ref_pct: float = 4.0,
        min_conviction: float = 0.20,
        db_bonus: float = 0.15,     # conviction bonus when a double bottom is present
    ):
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.rsi_period = rsi_period
        self.rsi_exit = rsi_exit
        self.down_lookback = down_lookback
        self.near_low_pct = near_low_pct
        self.consecutive_up = consecutive_up
        self.min_sep = min_sep
        self.stretch_ref_pct = stretch_ref_pct
        self.min_conviction = min_conviction
        self.db_bonus = db_bonus

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
        drange = avg_daily_range_pct(s)
        below_mean_pct = (mean - price) / mean * 100 if mean else 0.0

        win = s.tail(self.down_lookback)
        floor = float(win.min())
        near_low_pct_now = (price - floor) / floor * 100 if floor else 0.0

        # Exit: reverted to the mean, RSI normalized, or the reclaim failed
        # (closed down again while still below the fast EMA).
        if ctx.holding:
            if price >= mean:
                return Signal(ctx.token, Action.SELL, 1.0, False, 0.0,
                              f"reverted to mean: rsi {r:.0f}")
            if r >= self.rsi_exit:
                return Signal(ctx.token, Action.SELL, 1.0, False, 0.0,
                              f"momentum normalized: rsi {r:.0f}")
            if price < prev and price < fast:
                return Signal(ctx.token, Action.SELL, 1.0, False, 0.0,
                              f"reclaim failed: closed down below fast EMA, rsi {r:.0f}")
            return Signal(ctx.token, Action.HOLD, 0.0, False, 0.0,
                          f"holding reclaim: rsi {r:.0f}, {below_mean_pct:+.1f}% vs mean")

        # Entry: downtrend backdrop + near a recent floor + confirmed reclaim.
        downtrend = price < mean
        near_floor = near_low_pct_now <= self.near_low_pct
        reclaimed = _consecutive_up(s) >= self.consecutive_up
        if downtrend and near_floor and reclaimed:
            conv = self.min_conviction + max(0.0, min(
                1.0, below_mean_pct / self.stretch_ref_pct)) * (1.0 - self.min_conviction)
            if _double_bottom(win, self.near_low_pct, self.min_sep):
                conv = min(1.0, conv + self.db_bonus)
            edge = min(below_mean_pct, 1.5 * drange) if drange > 0 else below_mean_pct
            return Signal(ctx.token, Action.BUY, conv, False, edge,
                          f"reclaim: {near_low_pct_now:.1f}% off {self.down_lookback}b floor, "
                          f"{_consecutive_up(s)} up bars, {below_mean_pct:.1f}% below mean",
                          daily_range_pct=drange)
        return Signal(ctx.token, Action.HOLD, 0.0, False, 0.0,
                      f"no setup: {near_low_pct_now:.1f}% off floor, "
                      f"up_bars={_consecutive_up(s)}, downtrend={downtrend}",
                      daily_range_pct=drange)
