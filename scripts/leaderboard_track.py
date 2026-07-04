"""Leaderboard tracker: snapshot our rank + the attrition picture over time.

Downloads the public dashboard (standings JSON is embedded inline in the
HTML), computes our rank among ELIGIBLE agents by ret_pct, the top-10 cutoff
and the drawdown attrition around us, prints a short summary and appends one
JSON line to data/leaderboard_history.jsonl. Stdlib only — meant for cron:

  5 9,21 * * * cd /home/broncano/apps/bnb-hack && \
      .venv/bin/python scripts/leaderboard_track.py >> data/leaderboard_track.log 2>&1

NOTE: never add our capital/strategy to the record — this file may be shown
around; it only stores what the public dashboard already publishes.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

URL = "https://bnbhackleaderboard.pages.dev/"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "leaderboard_history.jsonl"


def our_agent() -> str:
    """Our wallet address, from the environment or .env — never hardcoded
    here (public repo; the repo<->wallet link stays local)."""
    addr = os.environ.get("AGENT_WALLET_ADDRESS", "")
    if not addr and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text().splitlines():
            if line.startswith("AGENT_WALLET_ADDRESS="):
                addr = line.split("=", 1)[1].strip().strip('"')
                break
    if not addr:
        sys.exit("AGENT_WALLET_ADDRESS not set (env or .env)")
    return addr.lower()


def fetch_entries() -> list[dict]:
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    entries = []
    for m in re.finditer(r'\{"agent":.*?"rank": (?:null|\d+)\}', html):
        try:
            entries.append(json.loads(m.group(0)))
        except ValueError:
            pass
    return entries


def main() -> None:
    us = our_agent()
    entries = fetch_entries()
    if not entries:
        print(f"{datetime.now(timezone.utc).isoformat()} ERROR: no entries parsed")
        sys.exit(1)
    elig = sorted((e for e in entries if e.get("eligible")),
                  key=lambda e: e.get("ret_pct", -999), reverse=True)
    rank, me = next(((i, e) for i, e in enumerate(elig, 1) if e["agent"] == us),
                    (None, None))
    cutoff10 = elig[9]["ret_pct"] if len(elig) >= 10 else None
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "rank": rank,
        "eligible": len(elig),
        "total": len(entries),
        "ret_pct": me.get("ret_pct") if me else None,
        "value": me.get("value") if me else None,
        "dd_pct": me.get("dd_pct") if me else None,
        "trades": me.get("trades") if me else None,
        "missing_days": me.get("missing_days") if me else None,
        "top10_cutoff_ret": cutoff10,
        "gap_to_top10": (round(cutoff10 - me["ret_pct"], 3)
                         if me and cutoff10 is not None else None),
        "leader_ret": elig[0]["ret_pct"] if elig else None,
        # Attrition watch: rivals ranked above us running hot drawdowns are
        # candidates to fall past us (official DQ ~30%).
        "above_us_dd15": ([e["agent"][:10] for e in elig[: (rank or 1) - 1]
                           if e.get("dd_pct", 0) > 15] if rank else []),
        "neighbors": [
            {"rank": i, "agent": e["agent"][:10], "ret": e["ret_pct"],
             "dd": e["dd_pct"]}
            for i, e in enumerate(elig, 1)
            if rank and abs(i - rank) <= 3 and e["agent"] != us
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    print(f"{rec['ts']} rank {rank}/{len(elig)} ret {rec['ret_pct']}% "
          f"dd {rec['dd_pct']}% | top10 needs >{cutoff10}% "
          f"(gap {rec['gap_to_top10']}pp) | above-us dd>15%: "
          f"{len(rec['above_us_dd15'])}")


if __name__ == "__main__":
    main()
