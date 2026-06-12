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


FG_EXTREME_FEAR = 20.0
FG_EXTREME_GREED = 80.0


@dataclass(frozen=True)
class RegimeView:
    regime: Regime
    fear_greed: float | None
    btc_dominance: float | None
    detail: str
    # Extra conviction an entry must show under this regime (0 = no floor).
    # Used by the extreme-fear branch: half-size AND only top setups.
    entry_conviction_floor: float = 0.0


def classify(global_metrics: dict, fear_greed: dict,
             fear_conviction_floor: float = 0.50) -> RegimeView:
    """Asymmetric Fear & Greed gate (v2).

    v1 treated both F&G extremes as RISK_OFF (no entries at all). The 12 jun
    12h dry-run showed extreme fear can persist for days, which would reduce a
    whole live week to compliance trades. The extremes are not symmetric:
    extreme *greed* is froth on extended trends (chasing it is buying tops),
    while in extreme *fear* our entry rules already demand trend=up + MACD
    bull — i.e. a confirmed bounce, not a falling knife. So:

      - F&G >= 80 -> RISK_OFF (never chase euphoria)
      - F&G <= 20 -> CONFLICTED: entries at half scale AND only above a
        conviction floor (keeps most of the protection the same dry-run
        proved valuable — ZEC fell 4.3% under the blocked signals)
      - incomplete data -> CONFLICTED (fail-cautious)
      - otherwise -> RISK_ON
    """
    fg_value = _fear_greed_value(fear_greed)
    btc_dom = (global_metrics or {}).get("btc_dominance")

    if fg_value is not None and fg_value >= FG_EXTREME_GREED:
        return RegimeView(Regime.RISK_OFF, fg_value, btc_dom, "extreme greed")
    if fg_value is not None and fg_value <= FG_EXTREME_FEAR:
        return RegimeView(
            Regime.CONFLICTED, fg_value, btc_dom,
            "extreme fear: half-size, conviction floor "
            f"{fear_conviction_floor:.2f}",
            entry_conviction_floor=fear_conviction_floor,
        )
    if fg_value is None or btc_dom is None:
        return RegimeView(Regime.CONFLICTED, fg_value, btc_dom, "incomplete regime data")
    return RegimeView(Regime.RISK_ON, fg_value, btc_dom, "f&g neutral band")


def _fear_greed_value(fear_greed: dict) -> float | None:
    value = (fear_greed or {}).get("value")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
