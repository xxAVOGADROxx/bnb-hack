"""Persistent agent state (JSON on disk, atomic writes).

The on-chain wallet is the source of truth for POSITIONS; this store keeps
only what the chain cannot tell us across restarts: high-water mark, hourly
snapshots, and per-day trade counters (for the >=1 trade/day rule and the
daily cap).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from agent.config import DATA_DIR


def _utc_date(now: datetime) -> str:
    return now.astimezone(timezone.utc).strftime("%Y-%m-%d")


class StateStore:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA_DIR / "state.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        return {
            "baseline_usd": None,       # portfolio value at competition hour 0
            "high_water_mark_usd": 0.0,
            "snapshots": [],            # [{ts, usd}] hourly, official method
            "trades_by_day": {},        # {"YYYY-MM-DD": count} UTC days
        }

    def _save(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self._state, f, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # -- baseline / high-water mark ---------------------------------------
    @property
    def baseline_usd(self) -> float | None:
        return self._state["baseline_usd"]

    def set_baseline(self, usd: float) -> None:
        self._state["baseline_usd"] = usd
        self._save()

    @property
    def high_water_mark_usd(self) -> float:
        return float(self._state["high_water_mark_usd"])

    def observe_portfolio(self, usd: float) -> None:
        if usd > self.high_water_mark_usd:
            self._state["high_water_mark_usd"] = usd
            self._save()

    # -- hourly snapshots (official scoring method, replicated) -------------
    def last_snapshot_ts(self) -> datetime | None:
        snaps = self._state["snapshots"]
        return datetime.fromisoformat(snaps[-1]["ts"]) if snaps else None

    def record_snapshot(self, now: datetime, usd: float) -> None:
        self._state["snapshots"].append(
            {"ts": now.astimezone(timezone.utc).isoformat(), "usd": usd}
        )
        self._save()

    # -- trade counters -------------------------------------------------------
    def trades_today(self, now: datetime) -> int:
        return int(self._state["trades_by_day"].get(_utc_date(now), 0))

    def record_trade(self, now: datetime) -> None:
        day = _utc_date(now)
        self._state["trades_by_day"][day] = self._state["trades_by_day"].get(day, 0) + 1
        self._save()

    # -- entry prices (for the stop-loss; chain can't tell us our cost basis) --
    def entry_price(self, token: str) -> float | None:
        v = (self._state.get("entry_prices") or {}).get(token)
        return float(v) if v is not None else None

    def record_entry(self, token: str, price: float) -> None:
        self._state.setdefault("entry_prices", {})[token] = price
        self._save()

    def clear_entry(self, token: str) -> None:
        if (self._state.get("entry_prices") or {}).pop(token, None) is not None:
            self._save()

    # -- liquidity sentinel baselines (#7) ------------------------------------
    def pool_baseline(self, token: str) -> dict | None:
        return (self._state.get("pool_baselines") or {}).get(token)

    def record_pool_baseline(self, token: str, pool: str | None, liq: float) -> None:
        # pool=None is meaningful: the token has no covered reference pool.
        self._state.setdefault("pool_baselines", {})[token] = {"pool": pool, "liq": liq}
        self._save()

    def clear_pool_baseline(self, token: str) -> None:
        if (self._state.get("pool_baselines") or {}).pop(token, None) is not None:
            self._save()
