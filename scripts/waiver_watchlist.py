"""Project the watchlist at competition-week WAIVER pricing.

The waiver (0.7% -> 0.077%/leg) isn't live until the trading week, so we can't
measure it directly today. Instead we re-quote each candidate's round-trip at
the real CURRENT (standard) pricing, then subtract the fee delta to project the
waiver round-trip, and count how many clear the <=1.5% friction ceiling under
each. This shows how much the LIQUID watchlist widens during the week.

  waiver_rt = standard_rt - 2*(0.7% - 0.077%) = standard_rt - 1.246%
  (clamped at the ~0.154% pure-fee floor; impact is unaffected by the waiver)

Usage: .venv/bin/python scripts/waiver_watchlist.py [--size-usd 750]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.liquidity_filter import DEFAULT_CANDIDATES, measure  # noqa: E402
from agent.config import DATA_DIR  # noqa: E402
from agent.twak.client import TwakClient, TwakError  # noqa: E402

CEILING = 1.5            # friction ceiling for watchlist inclusion (%)
WAIVER_DELTA = 2 * (0.7 - 0.077)   # 1.246pp removed from round-trip at waiver
WAIVER_FLOOR = 2 * 0.077            # 0.154% — fee can't go below this


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size-usd", type=float, default=750.0)
    args = ap.parse_args()

    addresses = json.loads((DATA_DIR / "bsc_addresses.json").read_text())
    # broad pool: the 19 screened candidates + the measured high-vol set
    pool = list(DEFAULT_CANDIDATES)
    try:
        hv = json.loads((DATA_DIR / "liquidity_report.highvol.json").read_text())
        pool += [r["symbol"] for r in hv.get("results", [])]
    except Exception:  # noqa: BLE001
        pass
    pool = [s for s in dict.fromkeys(pool) if s in addresses]

    t = TwakClient(chain="bsc", dry_run=True)
    print(f"re-quoting {len(pool)} candidates at ${args.size_usd:.0f} (live quotes, no tx)...\n")
    rows = []
    for sym in pool:
        try:
            std = measure(t, sym, args.size_usd, addresses[sym])["round_trip_cost_pct"]
        except (TwakError, ValueError, KeyError) as e:
            print(f"  {sym:<7} FAILED {str(e)[:60]}")
            continue
        waiver = max(WAIVER_FLOOR, std - WAIVER_DELTA)
        rows.append((sym, std, waiver))

    rows.sort(key=lambda r: r[1])
    std_in = [r[0] for r in rows if r[1] <= CEILING]
    wv_in = [r[0] for r in rows if r[2] <= CEILING]
    added = [r[0] for r in rows if r[2] <= CEILING and r[1] > CEILING]

    print(f"{'token':<8}{'std rt%':>9}{'waiver rt%':>12}{'std?':>6}{'waiver?':>9}")
    print("-" * 44)
    for sym, std, wv in rows:
        print(f"{sym:<8}{std:>9.2f}{wv:>12.2f}{'  ✓' if std <= CEILING else '  ·':>6}"
              f"{'  ✓' if wv <= CEILING else '  ·':>9}")

    print(f"\nwatchlist @ standard ({len(std_in)}): {', '.join(std_in)}")
    print(f"watchlist @ waiver   ({len(wv_in)}): {', '.join(wv_in)}")
    print(f"ADDED by the waiver  ({len(added)}): {', '.join(added) or '—'}")
    (DATA_DIR / "waiver_watchlist.json").write_text(json.dumps(
        {"ceiling_pct": CEILING, "size_usd": args.size_usd,
         "standard": std_in, "waiver": wv_in, "added": added,
         "rows": [{"symbol": s, "std": round(a, 3), "waiver": round(b, 3)} for s, a, b in rows]},
        indent=1))


if __name__ == "__main__":
    main()
