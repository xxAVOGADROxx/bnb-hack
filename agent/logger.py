"""Structured logging.

Every trading decision is appended to data/decisions.jsonl as one line:
signal -> rule -> action -> tx hash. This file is the audit trail that
survives restarts and feeds the demo (guardrails must be visible in logs).
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent.config import DATA_DIR


def setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        stream=sys.stdout,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )


class DecisionLog:
    """Append-only JSONL decision log."""

    def __init__(self, path: Path | None = None):
        self.path = path or DATA_DIR / "decisions.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, event: str, **fields) -> None:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **fields,
        }
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
