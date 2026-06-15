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
    # Dynamic conviction (sizing weight, 0..1). Composite of three normalized
    # sub-scores; weights sum to 1. Tunables — backtest, don't trust as truths.
    min_conviction: float = 0.20    # floor for a valid 4/4 entry (weak but real)
    trend_full_ratio: float = 0.50  # EMA-spread/daily-range at which trend = full
    macd_full_pct: float = 0.30     # MACD histogram as % of price at full momentum
    w_trend: float = 0.40
    w_momentum: float = 0.35
    w_rsi: float = 0.25
    grey_conviction_mult: float = 0.60  # 3/4 grey-zone entries size smaller
    # Edge gate is DECOUPLED from sizing: it uses a conservative, fixed fraction
    # of the daily range as the edge estimate (so a strong-conviction signal
    # can't loosen the "is there enough edge to beat friction?" filter). This
    # keeps the selectivity that the backtest shows matters; conviction only
    # changes position size, never whether the trade clears the min-edge gate.
    edge_conviction_ref: float = 0.30


DEFAULT_PARAMS = SignalParams()


@dataclass(frozen=True)
class Signal:
    token: str
    action: Action
    conviction: float        # 0..1 — position-size weight
    grey_zone: bool          # True -> candidate for the x402 premium branch
    expected_move_pct: float # conservative edge estimate for the min-edge gate
    reason: str
    daily_range_pct: float = 0.0  # raw avg daily range — drives vol-targeted sizing


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


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def vol_mult(daily_range_pct: float, vol_target_pct: float, vol_floor: float = 0.5) -> float:
    """Risk-parity sizing multiplier: scale a position DOWN when the token is
    more volatile than the target daily range, so each position contributes a
    similar risk budget (protects the drawdown gate). 1.0 at/below target,
    shrinking above it, never above 1.0. vol_target_pct<=0 disables."""
    if vol_target_pct <= 0 or daily_range_pct <= 0:
        return 1.0
    return max(vol_floor, min(1.0, vol_target_pct / daily_range_pct))


def volume_confirms(volumes: list[float], lookback: int = 24, ratio: float = 1.0) -> bool:
    """Entry confirmation (#11): the latest volume_24h must be >= ratio x its
    own trailing-`lookback`-bar mean — attention rising, not fading. Backtested
    (scripts/vol_filter_bt.py): ratio 1.0 cut the worst gross-negative entries
    and roughly halved the fee-driven loss; tighter ratios overshoot. ratio<=0
    (or too short a series) disables the gate (returns True)."""
    if ratio <= 0 or len(volumes) <= lookback:
        return True
    window = volumes[-lookback - 1:-1]  # the `lookback` bars BEFORE the latest
    avg = sum(window) / len(window) if window else 0.0
    return avg <= 0 or volumes[-1] >= ratio * avg


def conviction_score(
    ema_fast: float, ema_slow: float, macd_hist: float, rsi_now: float,
    price: float, move: float, p: SignalParams,
) -> tuple[float, dict]:
    """Composite entry conviction in [min_conviction, 1.0] — how strong the
    setup is, used for position sizing (and, via expected_move, the edge gate).
    Three normalized drivers so weak and strong setups size differently:
      trend    — EMA fast/slow spread relative to the token's own daily range
      momentum — MACD histogram (line-signal) as a % of price
      rsi_room — headroom below overbought (more room = more to run)
    """
    spread_ratio = ((ema_fast - ema_slow) / ema_slow * 100 / move) if move > 0 else 0.0
    trend = _clamp01(spread_ratio / p.trend_full_ratio)
    momentum = _clamp01((macd_hist / price * 100) / p.macd_full_pct) if price > 0 else 0.0
    rsi_room = _clamp01((p.rsi_overbought - rsi_now) / (p.rsi_overbought - 50.0))
    raw = p.w_trend * trend + p.w_momentum * momentum + p.w_rsi * rsi_room
    conviction = p.min_conviction + raw * (1.0 - p.min_conviction)
    return conviction, {"t": round(trend, 2), "m": round(momentum, 2), "r": round(rsi_room, 2)}


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

    macd_hist = macd_line.iloc[-1] - macd_sig.iloc[-1]
    conviction, drivers = conviction_score(
        ema_fast, ema_slow, macd_hist, rsi_now, price, move, params)
    drv = f"conv(t{drivers['t']}/m{drivers['m']}/r{drivers['r']})"

    # Edge estimate for the risk engine's min-edge gate: a conservative fixed
    # fraction of the daily range — independent of conviction, so sizing and
    # gating are separate knobs (see edge_conviction_ref).
    edge = move * params.edge_conviction_ref

    if met == 4:
        return Signal(token, Action.BUY, conviction, False,
                      edge, f"entry: {detail} {drv}", daily_range_pct=move)
    if met == 3:
        # 3/4 grey-zone candidate for the x402 tie-break; sized smaller.
        grey_conv = conviction * params.grey_conviction_mult
        return Signal(token, Action.HOLD, grey_conv, True,
                      edge, f"grey zone (3/4): {detail} {drv}", daily_range_pct=move)
    return Signal(token, Action.HOLD, 0.0, False, move, detail, daily_range_pct=move)
