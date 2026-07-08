"""Shadow race scoreboard: how is each strategy's paper book doing?

Reads shadow_open / shadow_close events from decisions.jsonl (written by
agent/strategies/shadow.py, which paper-trades every registered plugin with
live gates, live sizing and measured friction) and prints a per-strategy
table: closed trades, win rate, net P&L, mean/median net per trade, mean
hold, plus whatever is still open (marked at entry cost — this report
fetches no prices).

The live book itself is the ground truth for the ACTIVE strategy; its shadow
twin should track it minus execution noise — a persistent gap between the
two is an execution problem, not a signal problem.

Usage: .venv/bin/python scripts/shadow_race.py [path/to/decisions.jsonl]
       (default: data/decisions.jsonl; live file may need docker cp first)
"""
from __future__ import annotations

import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.config import DATA_DIR  # noqa: E402


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA_DIR / "decisions.jsonl"
    closes: dict[str, list[dict]] = defaultdict(list)
    opens: dict[str, dict[str, dict]] = defaultdict(dict)  # strategy -> token -> open
    first_ts = last_ts = None
    for line in path.read_text().splitlines():
        if '"shadow_' not in line:
            continue
        r = json.loads(line)
        first_ts = first_ts or r["ts"]
        last_ts = r["ts"]
        if r["event"] == "shadow_open":
            opens[r["strategy"]][r["token"]] = r
        elif r["event"] == "shadow_close":
            closes[r["strategy"]].append(r)
            opens[r["strategy"]].pop(r["token"], None)
    if not closes and not opens:
        sys.exit(f"sin eventos shadow_* en {path} — ¿el loop ya corre con shadow books?")
    print(f"ventana {first_ts[:16]} .. {last_ts[:16]} UTC ({path.name})\n")

    print(f"{'strategy':<16}{'closed':>7}{'win%':>6}{'net$':>8}{'avg%':>7}"
          f"{'med%':>7}{'hold_h':>8}  open")
    for name in sorted(set(closes) | set(opens)):
        cl = closes.get(name, [])
        nets = [c["net_pct"] for c in cl]
        open_s = ", ".join(f"{t} @{o['px']:g}" for t, o in
                           sorted(opens.get(name, {}).items())) or "—"
        if cl:
            win = 100 * sum(1 for c in cl if c["net_usd"] > 0) / len(cl)
            print(f"{name:<16}{len(cl):>7}{win:>6.0f}"
                  f"{sum(c['net_usd'] for c in cl):>8.2f}"
                  f"{statistics.fmean(nets):>7.2f}{statistics.median(nets):>7.2f}"
                  f"{statistics.fmean(c['held_h'] for c in cl):>8.1f}  {open_s}")
        else:
            print(f"{name:<16}{0:>7}{'—':>6}{'—':>8}{'—':>7}{'—':>7}{'—':>8}  {open_s}")

    print("\nCAVEAT: libro virtual — fricción medida pero sin slippage real ni "
          "fallos de swap; n chico las primeras semanas. El gemelo sombra de la "
          "estrategia ACTIVA debe parecerse al libro real: si divergen, el "
          "problema es de ejecución, no de señal.")


if __name__ == "__main__":
    main()
