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


def render(board, our_wallet: str, show_all: bool = False, min_usd: float = 0.0) -> None:
    now = datetime.now(timezone.utc)
    total = len(board)
    # Default view: only real, rule-abiding participants (started with real
    # capital, eligible value > $1, no off-list junk inflating their bag).
    # --min-usd narrows further to wallets holding real capital right now.
    shown = board if show_all else [s for s in board if s.eligible]
    if min_usd > 0:
        shown = [s for s in shown if s.usd >= min_usd]
    dropped = total - len(shown)
    scope = "ALL registered" if show_all else "ELIGIBLE participants only"
    if min_usd > 0:
        scope += f" (>= ${min_usd:,.0f})"
    print(f"\n=== BNB HACK leaderboard (approx, ours) — {now:%Y-%m-%d %H:%M} UTC ===")
    print(f"    {scope} — {len(shown)} shown"
          + ("" if show_all else f", {dropped} dust/ineligible hidden (use --all to see)"))
    print(f"{'#':>3} {'wallet':<14} {'value USD':>12} {'return':>8}  flags")
    for i, s in enumerate(shown, 1):
        ret = f"{s.ret_pct:+.2f}%" if s.ret_pct is not None else "—"
        flags = " ".join(p for p in (
            "dust" if not getattr(s, "eligible", False) else "",
            "<-- US" if s.is_us else "")).strip()
        print(f"{i:>3} {s.wallet[:8]}…{s.wallet[-4:]} {s.usd:>12,.2f} {ret:>8}  {flags}")
    # Rank + posture are measured against the eligible field (real rivals),
    # not the dust, regardless of which view is printed.
    ours = next((s for s in board if s.is_us), None)
    field = [s.ret_pct for s in board
             if s.eligible and s.ret_pct is not None and not s.is_us]
    if ours and ours.eligible:
        rank = 1 + sum(1 for r in field if r > (ours.ret_pct or 0))
        print(f"\nus: rank {rank}/{len(field) + 1} among eligible — "
              f"${ours.usd:,.2f} ({ours.ret_pct:+.2f}%)")
        print("posture:", posture(ours.ret_pct, field))
    elif ours:
        print(f"\nNOTE: our wallet is registered but not currently eligible "
              f"(value ${ours.usd:,.2f}, baseline ${ours.baseline_usd or 0:,.2f}).")
    elif our_wallet:
        print(f"\nNOTE: our wallet {our_wallet} is NOT among the registered participants yet!")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", action="store_true", help="refresh hourly forever")
    ap.add_argument("--interval", type=int, default=3600, help="seconds between refreshes")
    ap.add_argument("--all", action="store_true",
                    help="show every registered wallet incl. dust/ineligible (default: eligible only)")
    ap.add_argument("--min-usd", type=float, default=0.0,
                    help="only show wallets at/above this USD value (e.g. --min-usd 100 for real-capital rivals)")
    args = ap.parse_args()

    cfg = load_config(dry_run=True)  # read-only: dry_run only relaxes key checks
    our_wallet = os.environ.get("AGENT_WALLET_ADDRESS", "")
    monitor = LeaderboardMonitor(
        CMCClient(cfg.cmc_api_key), TokenRegistry(), cfg.tokens.allowlist, our_wallet
    )
    while True:
        try:
            render(monitor.refresh(), our_wallet, show_all=args.all, min_usd=args.min_usd)
        except Exception:  # keep the watcher alive; trading never depends on this
            log.exception("refresh failed")
        if not args.watch:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
