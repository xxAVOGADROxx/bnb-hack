#!/usr/bin/env bash
# Quick read-only monitor: our leaderboard standing, recent executed swaps,
# and the latest fee summary. Reads the agent's data/ files only (no keys, no
# transactions). Run from the repo root on the host:
#     bash deploy/status.sh
# or, if data/ is owned by the container user and unreadable on the host:
#     docker compose exec agent bash deploy/status.sh
set -euo pipefail
cd "$(dirname "$0")/.."

python3 - <<'PY'
import json, glob, os
D = "data"

# --- leaderboard ---------------------------------------------------------
try:
    b = json.load(open(f"{D}/leaderboard.json"))
    board = b.get("board", [])
    me = next((x for x in board if x.get("is_us")), None)
    print(f"LEADERBOARD  (as of {b.get('ts','?')})")
    if me:
        rank = 1 + sum(1 for x in board if x["usd"] > me["usd"])
        ret = f"{me['ret_pct']:+.2f}%" if me.get("ret_pct") is not None else "n/a (pre-window)"
        print(f"  US -> rank {rank}/{len(board)} | ${me['usd']:.2f} | return {ret}")
    for i, x in enumerate(board[:5], 1):
        tag = "  <-- US" if x.get("is_us") else ""
        ret = f"{x['ret_pct']:+.2f}%" if x.get("ret_pct") is not None else "n/a"
        print(f"  {i}. {x['wallet'][:12]}... ${x['usd']:>10.2f}  {ret}{tag}")
except FileNotFoundError:
    print("LEADERBOARD: no data yet (monitor writes data/leaderboard.json once running)")

# --- swaps ---------------------------------------------------------------
print("\nRECENT SWAPS (executed)")
rows = []
if os.path.exists(f"{D}/decisions.jsonl"):
    rows = [json.loads(l) for l in open(f"{D}/decisions.jsonl") if l.strip()]
ex   = [d for d in rows if d.get("event") == "trade_executed"]
fail = [d for d in rows if d.get("event") == "trade_failed"]
rej  = [d for d in rows if d.get("event") == "trade_rejected"]
for d in ex[-10:]:
    fields = {k: v for k, v in d.items() if k not in ("event", "ts")}
    print(f"  {d.get('ts','')}  {fields}")
print(f"  totals -> executed: {len(ex)} | failed: {len(fail)} | rejected: {len(rej)}")

# --- fees (latest ops report) -------------------------------------------
reps = sorted(glob.glob(f"{D}/reports/*.json"))
if reps:
    r = json.load(open(reps[-1]))
    f = r.get("fees") or {}
    print(f"\nFEES  (latest report: {os.path.basename(reps[-1])})")
    if f:
        print(f"  swap fees ~${f.get('fee_waiver_usd',0):.2f} (waiver) | "
              f"saved ~${f.get('waiver_saved_usd',0):.2f} vs standard 0.7%/leg")
    else:
        print("  (no fee block in this report yet)")
PY
