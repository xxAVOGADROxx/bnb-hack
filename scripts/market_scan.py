"""One-off market regime scan: is it worth running the bot for real?

Pulls 1h closes for the live watchlist via CMC and, per token, measures trend,
realized volatility, and how often moves clear the measured ~1.7% round-trip
friction wall. Prints a verdict on whether the current regime offers net edge.
"""
from __future__ import annotations

import statistics as st
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.cmc.client import CMCClient  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.tokens import TokenRegistry  # noqa: E402

FRICTION_RT = 1.7  # measured mean round-trip %, price_impact 0 = pure fee


def pct(a: float, b: float) -> float:
    return (a / b - 1) * 100 if b else 0.0


def main() -> None:
    cfg = load_config(dry_run=True)
    cmc = CMCClient(cfg.cmc_api_key)
    reg = TokenRegistry()
    watch = cfg.tokens.watchlist
    reg.ensure_id_map(cmc, watch)

    print(f"friction wall (round-trip): {FRICTION_RT:.1f}%  "
          f"-> a long must gain > {FRICTION_RT:.1f}% just to break even\n")
    hdr = (f"{'tok':<6}{'last':>10}{'24h%':>8}{'72h%':>8}{'7d%':>8}"
           f"{'hVol%':>7}{'dVol%':>7}{'24h>fr':>8}{'trend':>8}")
    print(hdr)
    print("-" * len(hdr))

    rows = []
    for t in watch:
        cid = reg.cmc_id(t)
        if cid is None:
            continue
        try:
            closes = cmc.closes_historical(cid, interval="1h", count=200)
        except Exception as e:  # noqa: BLE001
            print(f"{t:<6}  ERR {str(e)[:40]}")
            continue
        if len(closes) < 48:
            continue
        last = closes[-1]
        r24 = pct(last, closes[-25]) if len(closes) > 25 else 0.0
        r72 = pct(last, closes[-73]) if len(closes) > 73 else 0.0
        r7d = pct(last, closes[0])
        rets = [pct(closes[i], closes[i - 1]) for i in range(1, len(closes))]
        hvol = st.pstdev(rets)
        dvol = hvol * (24 ** 0.5)
        # fraction of 24h forward windows whose |move| clears the friction wall
        wins = [abs(pct(closes[i + 24], closes[i])) for i in range(len(closes) - 24)]
        frac = 100 * sum(1 for w in wins if w >= FRICTION_RT) / len(wins) if wins else 0
        # trend: last vs its own 24-bar mean
        sma24 = sum(closes[-24:]) / 24
        trend = "up" if last > sma24 else "dn"
        rows.append((t, r24, r72, r7d, dvol, frac, trend))
        print(f"{t:<6}{last:>10.4g}{r24:>8.2f}{r72:>8.2f}{r7d:>8.2f}"
              f"{hvol:>7.2f}{dvol:>7.2f}{frac:>7.0f}%{trend:>8}")

    print("-" * len(hdr))
    if not rows:
        print("no data")
        return
    up = sum(1 for r in rows if r[6] == "up")
    med_dvol = st.median(r[4] for r in rows)
    med_r7d = st.median(r[3] for r in rows)
    med_frac = st.median(r[5] for r in rows)
    print(f"\nSUMMARY  tokens={len(rows)}  up-trend={up}/{len(rows)}  "
          f"median 7d={med_r7d:+.1f}%  median daily-vol={med_dvol:.1f}%  "
          f"median 24h-move-clears-friction={med_frac:.0f}%")
    print(f"friction round-trip = {FRICTION_RT:.1f}%  "
          f"(need net move > friction to profit)")


if __name__ == "__main__":
    main()
