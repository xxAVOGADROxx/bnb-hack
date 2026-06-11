"""Layer 2 — per-token entry/exit signal (v1).

Low-frequency trend following on hourly closes: hunt 3-5%+ moves, never
scalp — the ~1.3-2% measured friction floor makes tight setups unprofitable
regardless of win rate. Indicators are computed locally in pure pandas
(deterministic, no TA library dependency) from CMC historical quotes.

v1 rules (STRATEGY §3) — ALL parameters are backtest tunables, not truths:
  entry  = uptrend (EMA fast > EMA slow) + MACD bullish + price above slow
           EMA + RSI not overbought
  exit   = price loses the slow EMA, or RSI blow-off, or MACD turns bearish
           while the trend is gone
  grey zone = 3 of 4 entry conditions -> candidate for the x402 premium
           tie-break branch (only acted on in CONFLICTED regime)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Sequence

import pandas as pd

log = logging.getLogger(__name__)


class Action(str, Enum):
    BUY = "buy"
    SELL = "sell"
    HOLD = "hold"


@dataclass(frozen=True)
class SignalParams:
    ema_fast: int = 20
    ema_slow: int = 50
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    rsi_period: int = 14
    rsi_overbought: float = 70.0
    rsi_blowoff: float = 80.0
    min_bars: int = 60


DEFAULT_PARAMS = SignalParams()


@dataclass(frozen=True)
class Signal:
    token: str
    action: Action
    conviction: float        # 0..1
    grey_zone: bool          # True -> candidate for the x402 premium branch
    expected_move_pct: float # recent average daily range, scaled by conviction
    reason: str


def ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def rsi(closes: pd.Series, period: int = 14) -> pd.Series:
    delta = closes.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rs = gain / loss.replace(0, 1e-12)
    return 100 - 100 / (1 + rs)


def macd(closes: pd.Series, fast: int, slow: int, signal: int) -> tuple[pd.Series, pd.Series]:
    line = ema(closes, fast) - ema(closes, slow)
    return line, line.ewm(span=signal, adjust=False).mean()


def avg_daily_range_pct(closes: pd.Series, bars_per_day: int = 24, days: int = 7) -> float:
    """Mean (max-min)/min per day over the recent window — how much this
    token actually moves, used to estimate the edge available to capture."""
    tail = closes.tail(bars_per_day * days)
    if len(tail) < bars_per_day:
        return 0.0
    ranges = []
    for i in range(0, len(tail) - bars_per_day + 1, bars_per_day):
        day = tail.iloc[i : i + bars_per_day]
        lo = day.min()
        if lo > 0:
            ranges.append((day.max() - lo) / lo * 100)
    return float(sum(ranges) / len(ranges)) if ranges else 0.0


def evaluate(
    token: str,
    closes: Sequence[float],
    holding: bool = False,
    params: SignalParams = DEFAULT_PARAMS,
) -> Signal:
    if len(closes) < params.min_bars:
        return Signal(token, Action.HOLD, 0.0, False, 0.0,
                      f"insufficient history ({len(closes)} bars)")

    s = pd.Series(list(closes), dtype=float)
    price = s.iloc[-1]
    ema_fast = ema(s, params.ema_fast).iloc[-1]
    ema_slow = ema(s, params.ema_slow).iloc[-1]
    macd_line, macd_sig = macd(s, params.macd_fast, params.macd_slow, params.macd_signal)
    rsi_now = rsi(s, params.rsi_period).iloc[-1]
    move = avg_daily_range_pct(s)

    uptrend = ema_fast > ema_slow
    macd_bull = macd_line.iloc[-1] > macd_sig.iloc[-1]
    above_slow = price > ema_slow
    rsi_ok = rsi_now < params.rsi_overbought
    conditions = [uptrend, macd_bull, above_slow, rsi_ok]
    met = sum(conditions)
    detail = (f"trend={'up' if uptrend else 'down'} macd={'bull' if macd_bull else 'bear'} "
              f"px{'>' if above_slow else '<'}ema{params.ema_slow} rsi={rsi_now:.0f}")

    # Exits are evaluated first and only matter while holding.
    if holding and (not above_slow or rsi_now >= params.rsi_blowoff
                    or (not macd_bull and not uptrend)):
        return Signal(token, Action.SELL, 1.0, False, move, f"exit: {detail}")

    if met == 4:
        # Conviction scales with how far the fast EMA leads the slow one,
        # normalized by the token's own daily range.
        spread_pct = (ema_fast - ema_slow) / ema_slow * 100
        conviction = max(0.3, min(1.0, spread_pct / move if move > 0 else 0.3))
        return Signal(token, Action.BUY, conviction, False,
                      move * conviction, f"entry: {detail}")
    if met == 3:
        return Signal(token, Action.HOLD, 0.5, True, move * 0.5,
                      f"grey zone (3/4): {detail}")
    return Signal(token, Action.HOLD, 0.0, False, move, detail)
