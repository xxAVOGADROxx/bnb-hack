"""Competition leaderboard — read-only side process (STRATEGY's risk-posture
input + demo feature). Never touches the wallet or the trading loop.

  python scripts/leaderboard.py            one refresh, print the board
  python scripts/leaderboard.py --watch    refresh every hour, forever
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import datetime, timezone

from agent.config import load_config
from agent.cmc.client import CMCClient
from agent.monitor.leaderboard import LeaderboardMonitor, posture
from agent.tokens import TokenRegistry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("leaderboard")


def render(board, our_wallet: str) -> None:
    now = datetime.now(timezone.utc)
    print(f"\n=== BNB HACK leaderboard (approx, ours) — {now:%Y-%m-%d %H:%M} UTC ===")
    print(f"{'#':>3} {'wallet':<14} {'value USD':>12} {'return':>8}  flags")
    for i, s in enumerate(board, 1):
        ret = f"{s.ret_pct:+.2f}%" if s.ret_pct is not None else "—"
        flags = " ".join(p for p in (
            "<= $1!" if s.sub_dollar else "", "<-- US" if s.is_us else "")) .strip()
        print(f"{i:>3} {s.wallet[:8]}…{s.wallet[-4:]} {s.usd:>12,.2f} {ret:>8}  {flags}")
    ours = next((s for s in board if s.is_us), None)
    field = [s.ret_pct for s in board if s.ret_pct is not None and not s.is_us]
    if ours:
        print("\nposture:", posture(ours.ret_pct, field))
    elif our_wallet:
        print(f"\nNOTE: our wallet {our_wallet} is NOT among the registered participants yet!")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="refresh hourly forever")
    ap.add_argument("--interval", type=int, default=3600, help="seconds between refreshes")
    args = ap.parse_args()

    cfg = load_config(dry_run=True)  # read-only: dry_run only relaxes key checks
    our_wallet = os.environ.get("AGENT_WALLET_ADDRESS", "")
    monitor = LeaderboardMonitor(
        CMCClient(cfg.cmc_api_key), TokenRegistry(), cfg.tokens.allowlist, our_wallet
    )
    while True:
        try:
            render(monitor.refresh(), our_wallet)
        except Exception:  # keep the watcher alive; trading never depends on this
            log.exception("refresh failed")
        if not args.watch:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
