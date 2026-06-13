"""Entrypoint: python -m agent [--live] [--once]

Dry-run is the default: quotes are real, no transaction is ever signed.
Live mode requires the explicit --live flag.
"""
from __future__ import annotations

import argparse
import logging
import signal
from datetime import datetime, timezone

from agent.config import load_config
from agent.logger import setup_logging
from agent.loop import Agent


def parse_utc(s: str) -> datetime:
    """ISO timestamp -> aware UTC datetime. Naive input is taken AS UTC —
    never the machine's local timezone (no UTC-vs-local confusion)."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent")
    parser.add_argument("--live", action="store_true",
                        help="sign and execute real transactions (default: dry-run)")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--max-hours", type=float, default=None,
                        help="stop cleanly after N hours (bounded test window)")
    parser.add_argument("--start-at", type=parse_utc, default=None, metavar="UTC",
                        help="wait and start the loop at this exact UTC time "
                             "(e.g. 2026-06-22T00:00Z)")
    parser.add_argument("--stop-at", type=parse_utc, default=None, metavar="UTC",
                        help="stop cleanly at this exact UTC time "
                             "(e.g. 2026-06-28T23:59Z)")
    parser.add_argument("--report-every-min", type=float, default=0.0,
                        help="every N minutes, write a uniquely-named ops "
                             "report (digest + leaderboard) to data/reports/ "
                             "and summarize it to Telegram (0 = off)")
    parser.add_argument("--canary", action="store_true",
                        help="do one small real round-trip to validate the live "
                             "execution path, then exit (use with --live)")
    parser.add_argument("--flatten", action="store_true",
                        help="one-shot: close every non-stable position into "
                             "USDT and exit (end of competition / emergency)")
    parser.add_argument("--paper-equity", type=float, default=0.0,
                        help="DRY-RUN ONLY: size entries as if the portfolio "
                             "were this big, so test windows exercise the full "
                             "entry path (proposal -> risk engine -> quote)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    cfg = load_config(dry_run=not args.live)
    agent = Agent(cfg, paper_equity=args.paper_equity if not args.live else 0.0)
    # Clean shutdown: finish the in-flight cycle (any swap is synchronous), then
    # exit with no pending tx. Covers `docker stop` (SIGTERM) and Ctrl-C (SIGINT).
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, agent.request_stop)
    if args.canary:
        agent.canary_roundtrip()
        return
    if args.flatten:
        agent.flatten()
        return
    agent.run(once=args.once, max_hours=args.max_hours,
              start_at=args.start_at, stop_at=args.stop_at,
              report_every_min=args.report_every_min)


if __name__ == "__main__":
    main()
