"""Data-dir housekeeping: keep the container's disk footprint flat forever.

The bot is a long-running hobby testbed now — nothing may accumulate without
bound. Once per UTC day the loop calls housekeep(), which enforces a
RETENTION_DAYS window on everything that grows:

- data/reports/                    -> files older than the window are DELETED
  (each 📋 report is a point-in-time digest; the numbers that matter live in
  the ledgers below).
- data/decisions.jsonl             -> lines older than the window are MOVED
  data/leaderboard_snapshots.jsonl    into a <name>.archive.jsonl.gz sibling
  (append: concatenated gzip members are valid — read back with gzip.open or
  zcat). NOTHING is lost: shadow/backtest calibration can read the archive
  whenever it needs deep history.

data/roundtrips.jsonl (the real-money ledger) is deliberately NOT rotated:
it grows ~150 bytes per trade and is the permanent record.

Fail-open like every side task: housekeep() never raises into the loop.
"""
from __future__ import annotations

import gzip
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent.config import DATA_DIR

log = logging.getLogger(__name__)

RETENTION_DAYS = 7
ROTATED_LOGS = ("decisions.jsonl", "leaderboard_snapshots.jsonl")


def prune_reports(reports_dir: Path, now: datetime,
                  days: int = RETENTION_DAYS) -> int:
    """Delete report files older than the window (by mtime). Returns count."""
    if not reports_dir.is_dir():
        return 0
    cutoff = now - timedelta(days=days)
    removed = 0
    for p in reports_dir.iterdir():
        if not p.is_file():
            continue
        mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
        if mtime < cutoff:
            p.unlink()
            removed += 1
    return removed


def rotate_jsonl(path: Path, now: datetime,
                 days: int = RETENTION_DAYS) -> tuple[int, int]:
    """Move lines whose `ts` is older than the window into a .archive.jsonl.gz
    sibling; rewrite the live file with the recent lines only. Returns
    (kept, archived). Lines without a parseable ts stay in the live file —
    losing data to a parse bug would be worse than keeping a stray line."""
    if not path.exists():
        return 0, 0
    cutoff = (now - timedelta(days=days)).isoformat()
    keep, old = [], []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ts = json.loads(line).get("ts", "")
        except ValueError:
            ts = ""
        # Same ISO-8601 shape throughout -> lexicographic compare is temporal.
        (old if ts and ts < cutoff else keep).append(line)
    if not old:
        return len(keep), 0
    archive = path.with_name(path.stem + ".archive.jsonl.gz")
    with gzip.open(archive, "at", encoding="utf-8") as f:
        f.write("\n".join(old) + "\n")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(keep) + ("\n" if keep else ""), encoding="utf-8")
    os.replace(tmp, path)
    return len(keep), len(old)


def housekeep(now: datetime | None = None, data_dir: Path | None = None) -> None:
    """Run the full retention pass. Never raises (called from the live loop)."""
    now = now or datetime.now(timezone.utc)
    data_dir = data_dir or DATA_DIR
    try:
        removed = prune_reports(data_dir / "reports", now)
        parts = [f"reports: -{removed}"]
        for name in ROTATED_LOGS:
            kept, archived = rotate_jsonl(data_dir / name, now)
            if archived:
                parts.append(f"{name}: {archived} lines -> archive, {kept} kept")
        log.info("housekeeping (%dd retention): %s",
                 RETENTION_DAYS, "; ".join(parts))
    except Exception:  # noqa: BLE001 — housekeeping must never kill trading
        log.exception("housekeeping failed (ignored)")
