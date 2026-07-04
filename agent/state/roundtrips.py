"""Round-trip ledger: one JSONL line per executed entry/exit.

Fills the observability gap found 2026-07-04: per-trade P&L had to be
reconstructed by hand from container logs. Append-only, best-effort — a
ledger write failure must never block or fail a trade, so errors are logged
and swallowed. Dry-run trades are skipped (the ledger is the LIVE record).

Read it with scripts/pnl_report.py (pairs open->close per token, FIFO).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from agent.config import DATA_DIR

log = logging.getLogger(__name__)

PATH = DATA_DIR / "roundtrips.jsonl"


def record(
    kind: str,          # "open" | "close"
    token: str,
    price: float,
    usd: float,
    reason: str,
    tx_hash: str | None = None,
    dry_run: bool = False,
) -> None:
    if dry_run:
        return
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": kind,
        "token": token,
        "price": price,
        "usd": round(float(usd), 2),
        "reason": reason,
        "tx": tx_hash,
    }
    try:
        with open(PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
    except OSError as e:
        log.warning("roundtrip ledger write failed: %s", e)
