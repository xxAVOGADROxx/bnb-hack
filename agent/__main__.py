"""Entrypoint: python -m agent [--live] [--once]

Dry-run is the default: quotes are real, no transaction is ever signed.
Live mode requires the explicit --live flag.
"""
from __future__ import annotations

import argparse
import logging
import signal

from agent.config import load_config
from agent.logger import setup_logging
from agent.loop import Agent


def main() -> None:
    parser = argparse.ArgumentParser(prog="agent")
    parser.add_argument("--live", action="store_true",
                        help="sign and execute real transactions (default: dry-run)")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--max-hours", type=float, default=None,
                        help="stop cleanly after N hours (bounded test window)")
    parser.add_argument("--canary", action="store_true",
                        help="do one small real round-trip to validate the live "
                             "execution path, then exit (use with --live)")
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
    agent.run(once=args.once, max_hours=args.max_hours)


if __name__ == "__main__":
    main()
