"""One-shot ops digest: the last N hours of decisions + leaderboard standing.

Same report the running agent emits with --report-every-min, but on demand —
for a morning review of an unattended night, or to (re)generate the file you
hand to a reviewer. Writes data/reports/daily-<timestamp>Z.json.

Usage: .venv/bin/python scripts/daily_digest.py [--hours 24] [--notify]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.alerts import Alerter  # noqa: E402
from agent.cmc.client import CMCClient  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.monitor import digest as digest_mod  # noqa: E402
from agent.monitor.leaderboard import LeaderboardMonitor  # noqa: E402
from agent.tokens import TokenRegistry  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--notify", action="store_true", help="send the summary to Telegram")
    args = ap.parse_args()

    cfg = load_config(dry_run=True)
    now = datetime.now(timezone.utc)
    digest = digest_mod.build_digest(now - timedelta(hours=args.hours), now)

    board = None
    try:
        board = LeaderboardMonitor(
            CMCClient(cfg.cmc_api_key), TokenRegistry(), cfg.tokens.allowlist,
            our_wallet=os.environ.get("AGENT_WALLET_ADDRESS", "")).refresh()
    except Exception as e:  # noqa: BLE001 — board is best-effort
        print(f"leaderboard unavailable: {e}")

    path = digest_mod.write_report(digest, board, tag="daily")
    summary = digest_mod.summary_line(digest, board)
    print(summary)
    print(f"report -> {path}")
    if args.notify:
        Alerter(cfg.telegram_bot_token, cfg.telegram_chat_id).notify(
            summary + f"\n📄 {path.name}")
    # full digest to stdout for piping into a review
    print(json.dumps(digest, indent=1)[:2000])


if __name__ == "__main__":
    main()
