"""Trade execution: quote -> guardrails -> swap -> post-trade verification.

All execution flows through TWAK (sole execution layer, local signing).
Every step — including rejections — lands in the decision log with the
signal, the rule that fired, the action and the tx hash.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from agent.logger import DecisionLog
from agent.risk.engine import RiskEngine, RiskState, TradeProposal, Verdict
from agent.state.store import StateStore
from agent.tokens import TokenRegistry
from agent.twak.client import AnyTwak, TwakError

log = logging.getLogger(__name__)


def _price_impact_pct(quote_raw: dict) -> float:
    """Quote shape (verified live): priceImpact arrives as a string percent."""
    try:
        return float(quote_raw.get("priceImpact") or 0)
    except (TypeError, ValueError):
        # Unparseable impact: treat as infinite so the trade is rejected.
        return float("inf")


class Executor:
    def __init__(
        self,
        twak: AnyTwak,
        risk: RiskEngine,
        store: StateStore,
        decisions: DecisionLog,
        max_slippage_pct: float,
        registry: TokenRegistry | None = None,
        alerter=None,
    ):
        self.twak = twak
        self.risk = risk
        self.store = store
        self.decisions = decisions
        self.max_slippage_pct = max_slippage_pct
        self.alerter = alerter
        # Risk checks run on symbols; twak calls run on contract addresses
        # (the CLI symbol resolver on BSC is unreliable — see agent/tokens.py).
        self.registry = registry or TokenRegistry()

    def execute(
        self,
        proposal: TradeProposal,
        *,
        portfolio_usd: float,
        state: RiskState,
        open_positions: int,
        signal_age_min: float,
    ) -> dict | None:
        """Run one proposal through risk + execution. Returns the swap result
        or None if rejected/failed (already logged either way)."""
        now = datetime.now(timezone.utc)
        verdict: Verdict = self.risk.evaluate(
            proposal,
            portfolio_usd=portfolio_usd,
            state=state,
            open_positions=open_positions,
            trades_today=self.store.trades_today(now, getattr(self.twak, "dry_run", False)),
            signal_age_min=signal_age_min,
            now=now,
        )
        base = {
            "from": proposal.from_token,
            "to": proposal.to_token,
            "usd": proposal.usd,
            "signal_reason": proposal.reason,
            "rule": verdict.rule,
            "detail": verdict.detail,
        }
        if not verdict.approved:
            self.decisions.append("trade_rejected", **base)
            log.info("rejected [%s]: %s", verdict.rule, verdict.detail)
            return None

        from_ref = self.registry.execution_ref(proposal.from_token)
        to_ref = self.registry.execution_ref(proposal.to_token)
        try:
            # Quote first: twak enforces the slippage tolerance on execution,
            # but we also reject up front on quoted price impact.
            quote = self.twak.quote(from_ref, to_ref, proposal.usd, self.max_slippage_pct)
            impact = _price_impact_pct(quote.raw)
            if impact > self.max_slippage_pct:
                self.decisions.append(
                    "trade_rejected", rule_override="price_impact",
                    price_impact_pct=impact, **base,
                )
                log.info("rejected [price_impact]: %.2f%% > %.2f%%", impact, self.max_slippage_pct)
                return None
            # Exits sell ~the held token amount, but NEVER the exact full balance:
            # the on-chain integer round-trips through a float and back, which can
            # ask for a few wei MORE than we hold, and routers revert on a
            # 100%-of-balance transfer ("BEP20: transfer amount exceeds balance").
            # A 0.1% dust haircut guarantees amount < balance; the ~$0.08 left is
            # negligible and filtered as sub-$1 dust next reconcile.
            sell_amount = proposal.amount * 0.999 if proposal.amount else proposal.amount
            result = self.twak.swap(
                from_ref, to_ref, proposal.usd, self.max_slippage_pct,
                amount=sell_amount,
            )
        except TwakError as e:
            self.decisions.append("trade_failed", error=str(e), **base)
            log.error("swap failed: %s", e)
            if self.alerter:
                self.alerter.notify(
                    f"⚠️ swap FAILED {proposal.from_token}->{proposal.to_token} "
                    f"${proposal.usd:.2f}: {e}"
                )
            return None

        tx_hash = result.get("hash") or result.get("txHash")
        # Record into the mode's own ledger: dry-run mirrors its own cadence
        # (no compliance spam) WITHOUT inflating the live counter compliance
        # reads — a leftover dry-run trade must never suppress the real one.
        self.store.record_trade(now, bool(result.get("dry_run")))
        if not result.get("dry_run"):
            # TODO(server): post-swap verification — confirm the balance delta
            # matches the quote within tolerance; alert on mismatch.
            if self.alerter:
                out = result.get("output") or ""
                link = result.get("explorer") or (
                    f"https://bscscan.com/tx/{tx_hash}" if tx_hash else "")
                self.alerter.notify(
                    f"✅ swap {proposal.from_token}->{proposal.to_token} ${proposal.usd:.2f}"
                    f"{(' -> ' + out) if out else ''}\n{proposal.reason}\n{link}"
                )

        self.decisions.append(
            "trade_executed",
            tx_hash=tx_hash,
            dry_run=bool(result.get("dry_run")),
            **base,
        )
        return result
