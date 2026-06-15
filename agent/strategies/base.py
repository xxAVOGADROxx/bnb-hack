"""Strategy plugin contract.

A strategy is a pluggable signal generator: it takes market data for one token
and returns a `Signal` (BUY / HOLD / SELL + conviction + expected edge). That is
the ENTIRE responsibility — the universal guardrails (regime gate, volume
confirmation, edge floor, cooldown, sizing, stop-loss) live in the loop and the
risk engine and apply to whatever strategy is active. So strategies stay small
and swappable, and the safety layer is shared.

Add a strategy: implement this protocol, then register it in
`agent/strategies/registry.py`. Select it via `config/risk.yaml` (`strategy:
active`) or the `--strategy` CLI flag.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Re-export the signal types so strategies import everything from one place.
from agent.signals.technical import Action, Signal  # noqa: F401


@dataclass(frozen=True)
class MarketContext:
    """Everything a strategy may read for one token this cycle. Market data
    only — a strategy holds its own parameters."""
    token: str
    closes: list[float]
    volumes: list[float]
    holding: bool


@runtime_checkable
class Strategy(Protocol):
    """A pluggable signal generator. `name` is the registry key and the value
    written to the decision log."""

    name: str

    def evaluate(self, ctx: MarketContext) -> Signal:
        ...
