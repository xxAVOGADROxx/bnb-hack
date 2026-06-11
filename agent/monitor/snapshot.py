"""Self-monitoring — replicate the official scoring method on our own wallet.

Hourly snapshot of portfolio USD value, cumulative return vs the hour-0
baseline, and max drawdown from the high-water mark. Drawdown is measured
the way the judge measures it (hourly snapshots), not just on internal ticks.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from agent.state.store import StateStore

log = logging.getLogger(__name__)

SNAPSHOT_INTERVAL = timedelta(hours=1)


@dataclass(frozen=True)
class Metrics:
    portfolio_usd: float
    return_pct: float | None      # vs baseline; None before baseline is set
    drawdown_pct: float
    high_water_mark_usd: float


def maybe_snapshot(store: StateStore, portfolio_usd: float, now: datetime | None = None) -> Metrics:
    """Record an hourly snapshot if due, update HWM, return current metrics."""
    now = now or datetime.now(timezone.utc)
    store.observe_portfolio(portfolio_usd)

    last = store.last_snapshot_ts()
    if last is None or now - last >= SNAPSHOT_INTERVAL:
        store.record_snapshot(now, portfolio_usd)
        log.info("hourly snapshot: $%.2f", portfolio_usd)

    hwm = store.high_water_mark_usd
    drawdown = 0.0 if hwm <= 0 else max(0.0, (hwm - portfolio_usd) / hwm * 100)
    baseline = store.baseline_usd
    ret = None if not baseline else (portfolio_usd / baseline - 1) * 100
    return Metrics(portfolio_usd, ret, drawdown, hwm)
