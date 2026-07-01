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
LIQUIDITY_REPORT = DATA_DIR / "liquidity_report.json"

# Fallback per-leg swap fee (%) when no live measurement is available. The
# announced competition-week waiver (0.077%/leg) never applied to our routes:
# every measured round-trip stayed at ~1.3-1.8% with price_impact 0 (pure fee),
# i.e. the full ~0.7%/leg standard rate, and the waiver detector never fired.
# So we do NOT assume the waiver — we estimate the fee ACTUALLY paid from the
# measured round-trip cost, falling back to this standard rate.
SWAP_FEE_STANDARD_PCT = 0.7


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
        "x402": _x402_summary(lo, hi),
        "fees": _fee_summary(trades),
    }


def _measured_leg_pct() -> float:
    """Per-leg swap fee (%) inferred from the live liquidity measurement. The
    report's round-trips carry price_impact 0 (pure fee, no slippage), so the
    per-leg fee is half the mean round-trip cost. Falls back to the standard
    rate if the report is missing or unreadable."""
    try:
        results = json.loads(LIQUIDITY_REPORT.read_text()).get("results", [])
        costs = [r["round_trip_cost_pct"] for r in results
                 if isinstance(r.get("round_trip_cost_pct"), (int, float))]
        if costs:
            return (sum(costs) / len(costs)) / 2
    except (OSError, json.JSONDecodeError, KeyError):
        pass
    return SWAP_FEE_STANDARD_PCT


def _fee_summary(trades: list[dict]) -> dict:
    """Estimated swap-fee cost for the REAL (non-dry-run) swaps in the period,
    computed from the MEASURED round-trip friction (price_impact 0 = pure fee).
    The announced 0.077%/leg waiver never applied to our routes, so we do NOT
    assume it — we report what was actually paid at the measured rate.
    Estimate: fee = measured_leg_pct x notional per leg (TWAK returns no exact
    fee)."""
    real = [t for t in trades if not t.get("dry_run")]
    notional = sum(float(t.get("usd") or 0.0) for t in real)
    leg_pct = _measured_leg_pct()
    return {
        "swaps": len(real),
        "notional_usd": round(notional, 2),
        "fee_pct_per_leg": round(leg_pct, 3),
        "fee_usd": round(notional * leg_pct / 100, 4),
    }


def _x402_summary(lo: str, hi: str) -> dict:
    """Revenue (leaderboard charges) and spend (premium pulls) for the window,
    from the payments ledger. Best-effort: never breaks a report."""
    try:
        from agent.x402 import ledger
        return ledger.summarize(lo, hi)
    except Exception:  # noqa: BLE001
        return {"charges": 0, "charged": 0.0, "spends": 0, "spent": 0.0, "net": 0.0}


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
    x = digest.get("x402") or {}
    if x.get("charges") or x.get("spends"):
        parts.append(
            f"⚡ x402 +{x['charges']} charge ({x['charged']:.2f} USD1) "
            f"/ -{x['spends']} spend ({x['spent']:.2f})")
    f = digest.get("fees") or {}
    if f.get("swaps"):
        parts.append(
            f"💸 swap fees ~${f['fee_usd']:.2f} "
            f"(~{f['fee_pct_per_leg']:.2f}%/leg measured, no waiver)")
    if portfolio:
        parts.append(f"💰 ${portfolio.get('total_usd', 0):.2f}")
    if board is not None:
        us = next(((i + 1, s) for i, s in enumerate(board) if s.is_us), None)
        if us:
            i, s = us
            ret = f"{s.ret_pct:+.2f}%" if s.ret_pct is not None else "—"
            parts.append(f"🏁 rank {i}/{len(board)} ({ret})")
    return "\n".join(parts)
