"""Periodic operations report ("digest") — the agent explains its day.

Compiles everything the agent decided in a period (from data/decisions.jsonl:
signals, blocks by rule, rejections, executed trades with approximate
round-trip PnL, errors), adds the current portfolio + field standing
(leaderboard), writes it to a UNIQUELY-NAMED file under data/reports/ and
pushes a compact summary to Telegram.

Purpose: the human review loop. During the live week a report is generated
every N hours; each file can be handed to a reviewer (human or Claude) to
audit what fired, what was blocked and why, and whether a calibration is
justified — without touching the running agent. The LLM stays OUT of the
trading loop: this is oversight between days, never a tick decision.
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from agent.config import DATA_DIR

log = logging.getLogger(__name__)

REPORTS_DIR = DATA_DIR / "reports"
DECISIONS_PATH = DATA_DIR / "decisions.jsonl"


def build_digest(since: datetime, now: datetime | None = None,
                 decisions_path: Path = DECISIONS_PATH) -> dict:
    """Aggregate the decision log for [since, now] into plain-data stats."""
    now = now or datetime.now(timezone.utc)
    lo, hi = since.isoformat(), now.isoformat()
    rows = []
    if decisions_path.exists():
        with open(decisions_path, encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if lo <= r.get("ts", "") <= hi:
                    rows.append(r)

    regimes = Counter(r.get("regime") for r in rows if r["event"] == "regime")
    signals = [r for r in rows if r["event"] == "signal"]
    actions = Counter(r.get("action") for r in signals)
    buys_by_token: dict[str, list[float]] = defaultdict(list)
    for r in signals:
        if r.get("action") == "buy":
            buys_by_token[r["token"]].append(r.get("conviction", 0.0))
    blocked = Counter(r.get("rule") for r in rows if r["event"] == "entry_blocked")
    rejected = Counter(r.get("rule") for r in rows if r["event"] == "trade_rejected")
    skipped = sum(1 for r in rows if r["event"] == "entry_skipped")
    errors = sum(1 for r in rows if r["event"] == "cycle_error")

    trades = [r for r in rows if r["event"] == "trade_executed"]
    # Approximate round-trip PnL: pair each token's entries/exits in order.
    # usd is the proposal size (entry) / proceeds estimate (exit) — approx.
    open_entries: dict[str, list[dict]] = defaultdict(list)
    round_trips = []
    for t in trades:
        frm, to = t.get("from", ""), t.get("to", "")
        if to and to not in ("USDT", "USDC") and frm in ("USDT", "USDC"):
            open_entries[to].append(t)
        elif frm and frm not in ("USDT", "USDC") and open_entries.get(frm):
            e = open_entries[frm].pop(0)
            round_trips.append({
                "token": frm, "entry_usd": round(e.get("usd", 0.0), 2),
                "exit_usd": round(t.get("usd", 0.0), 2),
                "pnl_usd": round(t.get("usd", 0.0) - e.get("usd", 0.0), 2),
                "entry_ts": e.get("ts"), "exit_ts": t.get("ts"),
                "exit_reason": (t.get("signal_reason") or "")[:60],
            })

    return {
        "period": {"from": lo, "to": hi},
        "cycles": sum(regimes.values()),
        "regimes": dict(regimes),
        "signals": {"total": len(signals), "by_action": dict(actions)},
        "buy_signals_by_token": {
            k: {"count": len(v), "conv_min": round(min(v), 2),
                "conv_max": round(max(v), 2)}
            for k, v in sorted(buys_by_token.items())},
        "entries_blocked_by_rule": dict(blocked),
        "trades_rejected_by_rule": dict(rejected),
        "entries_skipped_below_min_size": skipped,
        "cycle_errors": errors,
        "trades_executed": [
            {k: t.get(k) for k in ("ts", "from", "to", "usd", "dry_run", "tx_hash")}
            for t in trades],
        "round_trips_approx": round_trips,
        "open_positions_unmatched": {k: len(v) for k, v in open_entries.items() if v},
    }


def write_report(digest: dict, board: list | None = None,
                 portfolio: dict | None = None, tag: str = "report") -> Path:
    """Write the digest (+ leaderboard + portfolio) to a uniquely-named file."""
    now = datetime.now(timezone.utc)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{tag}-{now:%Y%m%d-%H%M}Z.json"
    payload = {"generated": now.isoformat(), "digest": digest}
    if portfolio:
        payload["portfolio"] = portfolio
    if board is not None:
        payload["leaderboard"] = {
            "top": [s.__dict__ for s in board[:10]],
            "us": next((dict(s.__dict__, rank=i + 1)
                        for i, s in enumerate(board) if s.is_us), None),
            "field_size": len(board),
        }
    path.write_text(json.dumps(payload, indent=1))
    return path


def summary_line(digest: dict, board: list | None = None,
                 portfolio: dict | None = None) -> str:
    """Compact Telegram summary of a report."""
    sig = digest["signals"]
    pnl = sum(rt["pnl_usd"] for rt in digest["round_trips_approx"])
    parts = [
        f"📋 report {digest['period']['from'][11:16]}–{digest['period']['to'][11:16]} UTC",
        f"cycles {digest['cycles']} | signals {sig['total']} "
        f"(buy {sig['by_action'].get('buy', 0)})",
        f"blocked {sum(digest['entries_blocked_by_rule'].values())} "
        f"| rejected {sum(digest['trades_rejected_by_rule'].values())} "
        f"| trades {len(digest['trades_executed'])} "
        f"| rt-PnL ${pnl:+.2f}",
    ]
    if digest["cycle_errors"]:
        parts.append(f"⚠️ {digest['cycle_errors']} cycle errors")
    if portfolio:
        parts.append(f"💰 ${portfolio.get('total_usd', 0):.2f}")
    if board is not None:
        us = next(((i + 1, s) for i, s in enumerate(board) if s.is_us), None)
        if us:
            i, s = us
            ret = f"{s.ret_pct:+.2f}%" if s.ret_pct is not None else "—"
            parts.append(f"🏁 rank {i}/{len(board)} ({ret})")
    return "\n".join(parts)
