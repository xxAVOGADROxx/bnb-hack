"""Layer 1 — market regime gate.

Classifies global context (Fear & Greed, BTC dominance, total mcap trend)
into a regime that governs HOW MUCH capital is deployed. It never picks
tokens. Cached upstream via the CMC client TTLs (recompute ~every 20 min).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

log = logging.getLogger(__name__)


class Regime(str, Enum):
    RISK_ON = "risk_on"          # normal exposure, watchlist tradable
    CONFLICTED = "conflicted"    # reduced exposure; only branch that may pay x402
    RISK_OFF = "risk_off"        # fall back to stables, minimal activity


@dataclass(frozen=True)
class RegimeView:
    regime: Regime
    fear_greed: float | None
    btc_dominance: float | None
    detail: str


def classify(global_metrics: dict, fear_greed: dict) -> RegimeView:
    """Placeholder thresholds — calibrate with backtest before live week.

    TODO(strategy): real rules. v1 sketch:
      - F&G extreme (<=20 or >=80) -> RISK_OFF
      - mixed momentum vs dominance signals -> CONFLICTED
      - otherwise -> RISK_ON
    """
    fg_value = _fear_greed_value(fear_greed)
    btc_dom = (global_metrics or {}).get("btc_dominance")

    if fg_value is not None and (fg_value <= 20 or fg_value >= 80):
        return RegimeView(Regime.RISK_OFF, fg_value, btc_dom, "fear&greed extreme")
    if fg_value is None or btc_dom is None:
        return RegimeView(Regime.CONFLICTED, fg_value, btc_dom, "incomplete regime data")
    return RegimeView(Regime.RISK_ON, fg_value, btc_dom, "default placeholder rule")


def _fear_greed_value(fear_greed: dict) -> float | None:
    value = (fear_greed or {}).get("value")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
