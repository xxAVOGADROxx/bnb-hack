"""Per-token / per-trade split of the live-config backtest.

Reuses the EXACT honest-backtest helpers (signals from the live code path,
asym regime gate with real F&G, live risk.yaml mechanisms) but instruments
the simulation to dump every round trip: token, entry/exit time, the two
swap fees, and the return in $ and %.

Two transactions per round trip (buy + sell); fee shown per swap = leg cost
(round_trip/2 of notional). Gas (~$0.10-0.30/swap on BSC) is separate and not
modeled here — it's a flat few cents, dwarfed by the leg cost.

Usage: .venv/bin/python scripts/trade_split.py [--bars 168]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.cmc.client import CMCClient, CMCError  # noqa: E402
from agent.config import DATA_DIR, load_config  # noqa: E402
from agent.signals.technical import DEFAULT_PARAMS, vol_mult  # noqa: E402
from agent.tokens import TokenRegistry  # noqa: E402
from scripts.backtest import (  # noqa: E402
    MAX_CONCURRENT, MAX_POSITION_PCT, START_EQUITY,
    fetch_aligned, fetch_fear_greed_by_day, precompute_signals, regime_overlay,
)

# Live config (mirrors risk.yaml + the live loop)
COST = 1.5            # round-trip %, half per leg
MIN_EDGE = 2.0
STOP_LOSS = 8.0
VOL_TARGET = 5.0
VOL_FLOOR = 0.5
COOLDOWN_BARS = 24


def simulate_traced(closes, signals, common_ts, n_bars, regime_scales, edge_floor):
    """Same mechanics as backtest.simulate but records full per-trade detail."""
    leg = COST / 2 / 100
    cash, positions = START_EQUITY, {}
    trades = []

    def open_trade(tok, t, usd, px):
        nonlocal cash
        qty = usd / px * (1 - leg)
        cash -= usd
        positions[tok] = {"qty": qty, "entry_px": px, "peak": px, "idx": len(trades)}
        trades.append({
            "token": tok, "entry_bar": t, "entry_time": common_ts[t][:16],
            "entry_usd": round(usd, 2), "entry_px": px,
            "fee_entry": round(usd * leg, 4),   # buy-leg swap fee
            "qty": qty, "exit_bar": None,
        })

    def close_trade(tok, t, px, reason):
        nonlocal cash
        pos = positions.pop(tok)
        gross = pos["qty"] * px
        proceeds = gross * (1 - leg)
        cash += proceeds
        tr = trades[pos["idx"]]
        tr["exit_bar"] = t
        tr["exit_time"] = common_ts[t][:16]
        tr["exit_px"] = px
        tr["fee_exit"] = round(gross * leg, 4)   # sell-leg swap fee
        tr["proceeds"] = round(proceeds, 2)
        tr["reason"] = reason
        tr["pnl_usd"] = round(proceeds - tr["entry_usd"], 2)
        tr["ret_pct"] = round((proceeds / tr["entry_usd"] - 1) * 100, 2)
        tr["fee_total"] = round(tr["fee_entry"] + tr["fee_exit"], 4)
        tr["hold_bars"] = t - tr["entry_bar"]

    last_exit: dict[str, int] = {}
    for t in range(n_bars):
        for tok in list(positions):
            _, exit_flag, _, _ = signals[tok][t]
            px = closes[tok][t]
            pos = positions[tok]
            pos["peak"] = max(pos["peak"], px)
            if px <= pos["entry_px"] * (1 - STOP_LOSS / 100):
                close_trade(tok, t, px, "stop_loss")
                last_exit[tok] = t
            elif exit_flag:
                close_trade(tok, t, px, "signal")
                last_exit[tok] = t
        equity = cash + sum(p["qty"] * closes[tok][t] for tok, p in positions.items())
        r_scale, r_floor = regime_scales[t] if regime_scales else (1.0, 0.0)
        for tok, series in closes.items():
            if tok in positions or len(positions) >= MAX_CONCURRENT or r_scale <= 0:
                continue
            if COOLDOWN_BARS and t - last_exit.get(tok, -10**9) < COOLDOWN_BARS:
                continue
            entry_conv, _, expected_move, daily_range = signals[tok][t]
            floor = max(MIN_EDGE, (edge_floor or {}).get(tok, 0.0))
            if entry_conv is None or expected_move < floor or entry_conv < r_floor:
                continue
            usd = equity * MAX_POSITION_PCT / 100 * entry_conv * r_scale * vol_mult(
                daily_range, VOL_TARGET, VOL_FLOOR)
            if usd < 10 or usd > cash:
                continue
            open_trade(tok, t, usd, series[t])

    final = cash + sum(p["qty"] * closes[tok][-1] * (1 - leg) for tok, p in positions.items())
    return trades, round(final, 2)


def report(bars: int, cmc, registry, tokens):
    common_ts, closes = fetch_aligned(cmc, registry, tokens, bars)
    n = len(common_ts)
    signals = precompute_signals(closes, DEFAULT_PARAMS)
    try:
        fg = fetch_fear_greed_by_day(cmc, days=max(30, bars // 24 + 2))
    except CMCError:
        fg = {}
    scales = regime_overlay(common_ts, fg, "asym") if fg else None
    edge_floor = {}
    try:
        liq = json.loads((DATA_DIR / "liquidity_report.json").read_text())
        edge_floor = {r["symbol"]: r["round_trip_cost_pct"] + 0.5 for r in liq.get("results", [])}
    except (OSError, ValueError, KeyError):
        pass

    trades, final = simulate_traced(closes, signals, common_ts, n, scales, edge_floor)
    days = sorted({ts[:10] for ts in common_ts})
    fear = sum(1 for d in days if fg.get(d, 50) <= 20)

    print(f"\n{'='*78}")
    print(f"  LIVE CONFIG — {bars}h window ({common_ts[0][:10]} .. {common_ts[-1][:10]}, "
          f"{len(days)} days, {fear} extreme-fear)  start ${START_EQUITY:,.0f}")
    print(f"{'='*78}")
    closed = [t for t in trades if t["exit_bar"] is not None]
    if not closed:
        print("  (no closed trades in window)")
    hdr = (f"  {'tok':<6}{'entry (UTC)':<13}{'exit (UTC)':<13}{'hold':>5}"
           f"{'entry$':>9}{'fee_buy':>9}{'fee_sell':>9}{'fee_tot':>9}{'pnl$':>9}{'ret%':>8}  reason")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    tot_fee = tot_pnl = tot_notional = 0.0
    for t in closed:
        print(f"  {t['token']:<6}{t['entry_time'][5:]:<13}{t['exit_time'][5:]:<13}"
              f"{t['hold_bars']:>4}h{t['entry_usd']:>9.2f}{t['fee_entry']:>9.3f}"
              f"{t['fee_exit']:>9.3f}{t['fee_total']:>9.3f}{t['pnl_usd']:>9.2f}"
              f"{t['ret_pct']:>7.2f}%  {t['reason']}")
        tot_fee += t["fee_total"]
        tot_pnl += t["pnl_usd"]
        tot_notional += t["entry_usd"]
    open_n = sum(1 for t in trades if t["exit_bar"] is None)

    # per-token rollup
    by_tok: dict[str, dict] = {}
    for t in closed:
        d = by_tok.setdefault(t["token"], {"n": 0, "pnl": 0.0, "fee": 0.0, "wins": 0})
        d["n"] += 1
        d["pnl"] += t["pnl_usd"]
        d["fee"] += t["fee_total"]
        d["wins"] += 1 if t["pnl_usd"] > 0 else 0
    print(f"\n  per-token: {'tok':<6}{'trades':>7}{'wins':>6}{'pnl$':>9}{'fee$':>9}")
    for tok, d in sorted(by_tok.items(), key=lambda kv: kv[1]["pnl"]):
        print(f"             {tok:<6}{d['n']:>7}{d['wins']:>6}{d['pnl']:>9.2f}{d['fee']:>9.3f}")

    print(f"\n  TOTAL: {len(closed)} round trips ({len(closed)*2} swaps), "
          f"{open_n} still open at end")
    print(f"  notional traded ${tot_notional:,.2f}  |  fees ${tot_fee:.3f} "
          f"({tot_fee/tot_notional*100 if tot_notional else 0:.3f}% of notional)")
    print(f"  avg fee / swap ${tot_fee/(len(closed)*2):.4f}" if closed else "")
    print(f"  realized trade PnL ${tot_pnl:.2f}")
    print(f"  final equity ${final:,.2f}  |  net return "
          f"{(final/START_EQUITY-1)*100:+.2f}%  (${final-START_EQUITY:+,.2f})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, nargs="+", default=[168, 480])
    args = ap.parse_args()
    cfg = load_config(dry_run=True)
    cmc = CMCClient(cfg.cmc_api_key)
    registry = TokenRegistry()
    tokens = list(cfg.tokens.watchlist)
    for b in args.bars:
        report(b, cmc, registry, tokens)


if __name__ == "__main__":
    main()
