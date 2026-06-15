"""Strategy registry — the single place strategies are registered and built.

Adding a strategy is one line in `_BUILDERS`. Selection is by name (from
`config/risk.yaml` `strategy: active`, or the `--strategy` CLI flag).
"""
from __future__ import annotations

from agent.strategies.base import Strategy
from agent.strategies.mean_reversion import MeanReversionStrategy
from agent.strategies.trend import TrendStrategy

# name -> zero-arg builder. Defaults are baked into each strategy's constructor.
_BUILDERS: dict[str, type] = {
    TrendStrategy.name: TrendStrategy,
    MeanReversionStrategy.name: MeanReversionStrategy,
}

DEFAULT = TrendStrategy.name


def available() -> list[str]:
    return sorted(_BUILDERS)


def build(name: str) -> Strategy:
    """Instantiate the strategy registered under `name`. Raises ValueError with
    the available names if it is unknown — fail loud, not silently on default."""
    builder = _BUILDERS.get(name)
    if builder is None:
        raise ValueError(
            f"unknown strategy {name!r}; available: {', '.join(available())}")
    return builder()
