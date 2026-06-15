"""x402 payments ledger — one append-only record of money in and out.

Both sides of the protocol write here: the leaderboard server records each
CHARGE it settles (revenue), and the premium-data branch records each SPEND it
pays (cost). The periodic report reads it so the operator sees x402 revenue and
spend next to trades. Append-only JSONL; reads tolerate partial lines.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from agent.config import DATA_DIR

LEDGER_PATH = DATA_DIR / "payments.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append(row: dict, path: Path = LEDGER_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row) + "\n"
    # Append atomically enough for a single writer; never raise into callers.
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
        f.flush()
        os.fsync(f.fileno())


def record_charge(amount: float, asset: str, from_addr: str, resource: str,
                  tx: str, path: Path = LEDGER_PATH) -> None:
    """Revenue: a settled incoming x402 payment for a sold resource."""
    _append({"ts": _now(), "dir": "in", "amount": amount, "asset": asset,
             "counterparty": from_addr, "resource": resource, "tx": tx}, path)


def record_spend(amount: float, asset: str, to: str, reason: str,
                 tx: str, path: Path = LEDGER_PATH) -> None:
    """Cost: an outgoing x402 payment for premium data/inference."""
    _append({"ts": _now(), "dir": "out", "amount": amount, "asset": asset,
             "counterparty": to, "resource": reason, "tx": tx}, path)


def read_rows(since: str | None = None, until: str | None = None,
              path: Path = LEDGER_PATH) -> list[dict]:
    rows = []
    if not path.exists():
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            ts = r.get("ts", "")
            if since and ts < since:
                continue
            if until and ts > until:
                continue
            rows.append(r)
    return rows


def summarize(since: str | None = None, until: str | None = None,
              path: Path = LEDGER_PATH) -> dict:
    """Counts + totals per direction, for a report window."""
    rows = read_rows(since, until, path)
    charges = [r for r in rows if r.get("dir") == "in"]
    spends = [r for r in rows if r.get("dir") == "out"]
    return {
        "charges": len(charges),
        "charged": round(sum(float(r.get("amount", 0)) for r in charges), 6),
        "spends": len(spends),
        "spent": round(sum(float(r.get("amount", 0)) for r in spends), 6),
        "net": round(sum(float(r.get("amount", 0)) for r in charges)
                     - sum(float(r.get("amount", 0)) for r in spends), 6),
    }
