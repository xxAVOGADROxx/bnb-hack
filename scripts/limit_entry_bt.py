"""Limit-at-level entry backtest vs market entry.

The Binacci idea, tested against our fee problem: instead of entering at market
when the signal fires (close[t]), park a limit at a dip below it — close[t] *
(1 - offset%) — and fill only if a later bar within a TTL touches it. A fill
gets a better entry price (offset% cheaper); the cost is MISSED trades — signals
whose dip never comes (including winners that just run up).

Honesty: we only have hourly CLOSES (no intra-bar low), so a limit at L fills
when a later close <= L. Real intra-bar lows touch L more often, so this
UNDERSTATES the fill rate — the miss cost here is an upper bound. Same live
config otherwise (trend signal, vol filter, asym regime, cooldown, edge floor,
fixed 8% stop, vol-target sizing). offset 0 = today's market entry (baseline).

Usage: .venv/bin/python scripts/limit_entry_bt.py
"""
from __future__ import annotations

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
    fetch_fear_greed_by_day, precompute_signals, regime_overlay,
)
from scripts.vol_filter_bt import fetch_price_vol  # noqa: E402

COST, MIN_EDGE, STOP, VOL_TARGET, VOL_FLOOR, COOLDOWN = 1.5, 2.0, 8.0, 5.0, 0.5, 24
LEG = COST / 2 / 100


def vol_ok(vols, tok, t, ratio):
    if ratio <= 0 or t <= 24:
        return True
    w = vols[tok][t - 24:t]
    avg = sum(w) / len(w) if w else 0.0
    return avg <= 0 or vols[tok][t] >= ratio * avg


def simulate(closes, vols, signals, n, scales, edge_floor, offset_pct, ttl, vol_ratio=1.0):
    cash, positions, pending, last_exit = START_EQUITY, {}, {}, {}
    trades, fills, misses = [], 0, 0

    def equity(t):
        return cash + sum(p["qty"] * closes[tok][t] for tok, p in positions.items())

    def open_pos(tok, t, px, conv, dr, scale):
        nonlocal cash, fills
        usd = equity(t) * MAX_POSITION_PCT / 100 * conv * scale * vol_mult(dr, VOL_TARGET, VOL_FLOOR)
        if usd < 10 or usd > cash:
            return False
        qty = usd / px * (1 - LEG)
        cash -= usd
        positions[tok] = {"qty": qty, "entry_px": px, "usd": usd,
                          "fee_in": usd * LEG, "idx": len(trades)}
        trades.append({"token": tok, "exit": None})
        fills += 1
        return True

    def close_pos(tok, t, px, reason):
        nonlocal cash
        pos = positions.pop(tok)
        gross_out = pos["qty"] * px
        cash += gross_out * (1 - LEG)
        tr = trades[pos["idx"]]
        tr["exit"] = t
        tr["fee"] = pos["fee_in"] + gross_out * LEG
        tr["net"] = gross_out * (1 - LEG) - pos["usd"]
        tr["gross"] = tr["net"] + tr["fee"]
        last_exit[tok] = t

    for t in range(n):
        # 1. exits on held positions
        for tok in list(positions):
            _, exit_flag, _, _ = signals[tok][t]
            px = closes[tok][t]
            if px <= positions[tok]["entry_px"] * (1 - STOP / 100):
                close_pos(tok, t, px, "stop")
            elif exit_flag:
                close_pos(tok, t, px, "signal")
        # 2. fill or expire pending limits
        for tok in list(pending):
            p = pending[tok]
            if closes[tok][t] <= p["limit"] and len(positions) < MAX_CONCURRENT:
                open_pos(tok, t, p["limit"], p["conv"], p["dr"], p["scale"])
                del pending[tok]
            elif t >= p["expiry"]:
                misses += 1
                del pending[tok]
        # 3. new entry signals
        r_scale, r_floor = scales[t] if scales else (1.0, 0.0)
        for tok, series in closes.items():
            if tok in positions or tok in pending or r_scale <= 0:
                continue
            if len(positions) + len(pending) >= MAX_CONCURRENT:
                break
            if COOLDOWN and t - last_exit.get(tok, -10**9) < COOLDOWN:
                continue
            entry_conv, _, exp_move, dr = signals[tok][t]
            floor = max(MIN_EDGE, (edge_floor or {}).get(tok, 0.0))
            if entry_conv is None or exp_move < floor or entry_conv < r_floor:
                continue
            if not vol_ok(vols, tok, t, vol_ratio):
                continue
            if offset_pct <= 0:  # market entry (baseline)
                open_pos(tok, t, series[t], entry_conv, dr, r_scale)
            else:
                pending[tok] = {"limit": series[t] * (1 - offset_pct / 100),
                                "expiry": t + ttl, "conv": entry_conv, "dr": dr,
                                "scale": r_scale}

    final = cash + sum(p["qty"] * closes[tok][-1] * (1 - LEG) for tok, p in positions.items())
    closed = [tr for tr in trades if tr["exit"] is not None]
    return {
        "fills": fills, "misses": misses, "trades": len(closed),
        "wins": sum(1 for t in closed if t["net"] > 0),
        "gross": sum(t["gross"] for t in closed),
        "fees": sum(t["fee"] for t in closed),
        "net": sum(t["net"] for t in closed),
        "ret": (final / START_EQUITY - 1) * 100,
    }


def main():
    cfg = load_config(dry_run=True)
    cmc = CMCClient(cfg.cmc_api_key)
    reg = TokenRegistry()
    tokens = list(cfg.tokens.watchlist)
    reg.ensure_id_map(cmc, tokens)
    try:
        ef = {r["symbol"]: r["round_trip_cost_pct"] + 0.5
              for r in json.loads((DATA_DIR / "liquidity_report.json").read_text()).get("results", [])}
    except (OSError, ValueError, KeyError):
        ef = {}
    try:
        fg = fetch_fear_greed_by_day(cmc, days=60)
    except CMCError:
        fg = {}

    configs = [("market (baseline)", 0.0, 0), ("limit -0.5% / 8h", 0.5, 8),
               ("limit -1.0% / 8h", 1.0, 8), ("limit -1.0% / 16h", 1.0, 16),
               ("limit -1.5% / 16h", 1.5, 16)]
    for bars in (168, 480):
        common, closes, vols = fetch_price_vol(cmc, reg, tokens, bars)
        n = len(common)
        sigs = precompute_signals(closes, DEFAULT_PARAMS)
        scales = regime_overlay(common, fg, "asym") if fg else None
        print(f"\n{'='*82}\n  {bars}h window ({common[0][:10]} .. {common[-1][:10]})  "
              f"live cfg\n{'='*82}")
        print(f"  {'entry':<20}{'fills':>6}{'miss':>6}{'trades':>7}{'wins':>5}"
              f"{'gross$':>9}{'fees$':>8}{'net$':>9}{'ret':>8}")
        for label, off, ttl in configs:
            r = simulate(closes, vols, sigs, n, scales, ef, off, ttl)
            print(f"  {label:<20}{r['fills']:>6}{r['misses']:>6}{r['trades']:>7}"
                  f"{r['wins']:>5}{r['gross']:>9.2f}{r['fees']:>8.2f}"
                  f"{r['net']:>9.2f}{r['ret']:>8.2f}%")


if __name__ == "__main__":
    main()
