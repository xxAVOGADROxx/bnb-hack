"""Risk engine — explicit, configurable, fail-closed guardrails.

Pure logic: judges proposals against config + state, never touches the
network. Every verdict (approved or rejected, with the rule that fired) is
logged by the caller to the decision log.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agent.config import RiskConfig, TokensConfig


class RiskState(str, Enum):
    NORMAL = "normal"
    ALERT = "alert"                  # drawdown >= alert: notify human
    PAUSE_ENTRIES = "pause_entries"  # drawdown >= pause: no new positions
    HARD_STOP = "hard_stop"          # drawdown >= hard stop: flatten to stables


@dataclass(frozen=True)
class TradeProposal:
    from_token: str
    to_token: str
    usd: float
    expected_edge_pct: float
    is_entry: bool  # opening/increasing risk (vs closing / de-risking to stables)
    reason: str


@dataclass(frozen=True)
class Verdict:
    approved: bool
    rule: str
    detail: str


class RiskEngine:
    def __init__(self, risk: RiskConfig, tokens: TokensConfig):
        self.risk = risk
        self.tokens = tokens

    # -- portfolio level ----------------------------------------------------
    def drawdown_pct(self, portfolio_usd: float, high_water_mark_usd: float) -> float:
        if high_water_mark_usd <= 0:
            return 0.0
        return max(0.0, (high_water_mark_usd - portfolio_usd) / high_water_mark_usd * 100)

    def drawdown_state(self, portfolio_usd: float, high_water_mark_usd: float) -> RiskState:
        dd = self.drawdown_pct(portfolio_usd, high_water_mark_usd)
        if dd >= self.risk.drawdown.hard_stop_pct:
            return RiskState.HARD_STOP
        if dd >= self.risk.drawdown.pause_entries_pct:
            return RiskState.PAUSE_ENTRIES
        if dd >= self.risk.drawdown.alert_pct:
            return RiskState.ALERT
        return RiskState.NORMAL

    # -- per trade ------------------------------------------------------------
    def evaluate(
        self,
        p: TradeProposal,
        *,
        portfolio_usd: float,
        state: RiskState,
        open_positions: int,
        trades_today: int,
        signal_age_min: float,
    ) -> Verdict:
        # Allowlist is absolute: only the 149 eligible tokens, fail-closed.
        if not self.tokens.allowlist:
            return Verdict(False, "allowlist", "allowlist empty — fill config/tokens.yaml")
        for tok in (p.from_token, p.to_token):
            if tok not in self.tokens.allowlist:
                return Verdict(False, "allowlist", f"{tok} not in the 149 eligible tokens")

        # De-risking (exits, flatten-to-stables) is always allowed past here;
        # everything below restricts risk-increasing entries.
        if state == RiskState.HARD_STOP and p.is_entry:
            return Verdict(False, "drawdown_hard_stop", "hard stop active: de-risking only")
        if not p.is_entry:
            return Verdict(True, "exit", "de-risking trade allowed")

        if state == RiskState.PAUSE_ENTRIES:
            return Verdict(False, "drawdown_pause", "entries paused by drawdown ladder")
        if signal_age_min > self.risk.max_signal_age_min:
            return Verdict(False, "stale_data", f"signal {signal_age_min:.1f} min old")
        if trades_today >= self.risk.max_trades_per_day:
            return Verdict(False, "daily_trade_cap", f"{trades_today} trades already today")
        if open_positions >= self.risk.max_concurrent:
            return Verdict(False, "max_concurrent", f"{open_positions} positions open")
        max_usd = portfolio_usd * self.risk.max_position_pct / 100
        if p.usd > max_usd:
            return Verdict(False, "position_size", f"${p.usd:.0f} > cap ${max_usd:.0f}")
        if p.expected_edge_pct < self.risk.min_expected_edge_pct:
            return Verdict(
                False, "min_edge",
                f"edge {p.expected_edge_pct:.2f}% below floor {self.risk.min_expected_edge_pct:.2f}%",
            )
        return Verdict(True, "all_checks", "entry approved")
