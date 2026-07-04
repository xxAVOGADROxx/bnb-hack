"""Per-trade P&L report from the round-trip ledger (data/roundtrips.jsonl).

Pairs open->close events per token (FIFO), prints one row per round trip
with gross % (exit px / entry px), an estimated net % (gross minus the
token's MEASURED round-trip friction from data/liquidity_report.json), hold
time and exit reason, then a summary. Open (unpaired) positions are listed
at the end. Stdlib + repo only, no network.

Usage: .venv/bin/python scripts/pnl_report.py [--file data/roundtrips.jsonl]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import DATA_DIR  # noqa: E402


def friction_pct() -> dict[str, float]:
    try:
        liq = json.loads((DATA_DIR / "liquidity_report.json").read_text())
        return {r["symbol"]: float(r["round_trip_cost_pct"])
                for r in liq.get("results", [])}
    except (OSError, ValueError, KeyError):
        return {}


def load_events(path: Path) -> list[dict]:
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                print(f"  (skipping malformed line: {line[:60]})")
    return events


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=str(DATA_DIR / "roundtrips.jsonl"))
    args = ap.parse_args()
    path = Path(args.file)
    if not path.exists():
        print(f"no ledger yet at {path} (it starts with the first trade "
              "after the 2026-07-04 deploy)")
        return

    fric = friction_pct()
    open_legs: dict[str, list[dict]] = {}
    trips = []
    for ev in load_events(path):
        tok = ev["token"]
        if ev["kind"] == "open":
            open_legs.setdefault(tok, []).append(ev)
        elif ev["kind"] == "close":
            if open_legs.get(tok):
                trips.append((open_legs[tok].pop(0), ev))
            else:
                trips.append((None, ev))  # close with no recorded open

    print(f"{'token':<7}{'entry (UTC)':<17}{'hold':>7}{'entry$':>10}"
          f"{'exit$':>10}{'gross%':>8}{'net%~':>8}{'usd':>8}  exit reason")
    n = wins = 0
    tot_net_usd = 0.0
    for o, c in trips:
        if o is None:
            print(f"{c['token']:<7}{'(no open leg)':<17}{'':>7}{'':>10}"
                  f"{c['price']:>10.4g}{'':>8}{'':>8}{c['usd']:>8.2f}  {c['reason'][:44]}")
            continue
        t0 = datetime.fromisoformat(o["ts"])
        t1 = datetime.fromisoformat(c["ts"])
        hold_min = (t1 - t0).total_seconds() / 60
        hold = f"{hold_min / 60:.1f}h" if hold_min >= 90 else f"{hold_min:.0f}m"
        gross = (c["price"] / o["price"] - 1) * 100 if o["price"] else 0.0
        net = gross - fric.get(o["token"], 1.0)
        net_usd = o["usd"] * net / 100
        n += 1
        wins += net > 0
        tot_net_usd += net_usd
        print(f"{o['token']:<7}{o['ts'][5:16]:<17}{hold:>7}{o['price']:>10.4g}"
              f"{c['price']:>10.4g}{gross:>+8.2f}{net:>+8.2f}{o['usd']:>8.2f}"
              f"  {c['reason'][:44]}")

    if n:
        print(f"\nround trips {n} | wins (net) {wins} ({wins / n * 100:.0f}%) "
              f"| est net P&L ${tot_net_usd:+.2f}")
    still_open = [ev for legs in open_legs.values() for ev in legs]
    for ev in still_open:
        print(f"OPEN   {ev['token']:<6} since {ev['ts'][5:16]} "
              f"@ {ev['price']:.4g} (${ev['usd']:.2f})")


if __name__ == "__main__":
    main()
