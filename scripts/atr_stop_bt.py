"""Volatility-adaptive stop backtest vs the fixed 8% stop.

True ATR needs OHLC (the OHLCV endpoint is 403 on this plan), so the volatility
unit is avg_daily_range_pct — the SAME per-token measure the live signal already
computes for vol-targeted sizing. The adaptive stop is placed at k x the token's
own daily range, captured at ENTRY (so a high-vol token like ZEC gets a wide
stop, a calm one like TRX a tight one — fixing the one-size-misfits-all 8%).

Layered on the current live config (volume filter #11 on, asym gate, cooldown,
edge floor, vol-target sizing). Reports gross/fees/net so the fee lens stays.

Usage: .venv/bin/python scripts/atr_stop_bt.py
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
from scripts.vol_filter_bt import fetch_price_vol
from scripts.year_forecast import fetch_daily

COST, MIN_EDGE, VOL_TARGET, VOL_FLOOR, COOLDOWN = 1.5, 2.0, 5.0, 0.5, 24


def simulate(closes, signals, n, scales, edge_floor, vols=None, vol_ratio=0.0,
             stop_mode="fixed", stop_pct=8.0, atr_k=2.0):
    leg = COST / 2 / 100
    cash, positions, trades = START_EQUITY, {}, {}
    tr_list, last_exit = [], {}

    def vol_ok(tok, t):
        if vol_ratio <= 0 or vols is None or t <= COOLDOWN:
            return True
        w = vols[tok][t - 24:t]
        avg = sum(w) / len(w) if w else 0.0
        return avg <= 0 or vols[tok][t] >= vol_ratio * avg

    def stop_level(entry_px, dr):
        if stop_mode == "atr":
            unit = dr if dr > 0 else stop_pct / atr_k
            return entry_px * (1 - atr_k * unit / 100)
        return entry_px * (1 - stop_pct / 100)

    def close(tok, t, px, reason):
        nonlocal cash
        pos = positions.pop(tok)
        gross_out = pos["qty"] * px
        cash += gross_out * (1 - leg)
        tr = tr_list[pos["idx"]]
        tr["exit"] = t
        tr["fee"] = pos["fee_in"] + gross_out * leg
        tr["net"] = gross_out * (1 - leg) - pos["usd"]
        tr["gross"] = tr["net"] + tr["fee"]
        tr["reason"] = reason
        last_exit[tok] = t

    for t in range(n):
        for tok in list(positions):
            _, exit_flag, _, _ = signals[tok][t]
            px = closes[tok][t]
            pos = positions[tok]
            if px <= stop_level(pos["entry_px"], pos["dr"]):
                close(tok, t, px, "stop")
            elif exit_flag:
                close(tok, t, px, "signal")
        equity = cash + sum(p["qty"] * closes[tok][t] for tok, p in positions.items())
        r_scale, r_floor = scales[t] if scales else (1.0, 0.0)
        for tok, series in closes.items():
            if tok in positions or len(positions) >= MAX_CONCURRENT or r_scale <= 0:
                continue
            if COOLDOWN and t - last_exit.get(tok, -10**9) < COOLDOWN:
                continue
            entry_conv, _, exp_move, dr = signals[tok][t]
            floor = max(MIN_EDGE, (edge_floor or {}).get(tok, 0.0))
            if entry_conv is None or exp_move < floor or entry_conv < r_floor:
                continue
            if not vol_ok(tok, t):
                continue
            usd = equity * MAX_POSITION_PCT / 100 * entry_conv * r_scale * vol_mult(
                dr, VOL_TARGET, VOL_FLOOR)
            if usd < 10 or usd > cash:
                continue
            qty = usd / series[t] * (1 - leg)
            cash -= usd
            positions[tok] = {"qty": qty, "entry_px": series[t], "dr": dr,
                              "usd": usd, "fee_in": usd * leg, "idx": len(tr_list)}
            tr_list.append({"token": tok, "exit": None})
    final = cash + sum(p["qty"] * closes[tok][-1] * (1 - leg) for tok, p in positions.items())
    closed = [t for t in tr_list if t["exit"] is not None]
    stops = sum(1 for t in closed if t["reason"] == "stop")
    return {
        "trades": len(closed), "wins": sum(1 for t in closed if t["net"] > 0),
        "stops": stops, "gross": sum(t["gross"] for t in closed),
        "fees": sum(t["fee"] for t in closed), "net": sum(t["net"] for t in closed),
        "ret": (final / START_EQUITY - 1) * 100,
    }


def edge_floors():
    try:
        liq = json.loads((DATA_DIR / "liquidity_report.json").read_text())
        return {r["symbol"]: r["round_trip_cost_pct"] + 0.5 for r in liq.get("results", [])}
    except (OSError, ValueError, KeyError):
        return {}


def row(label, r):
    print(f"  {label:<22}{r['trades']:>7}{r['wins']:>6}{r['stops']:>7}"
          f"{r['gross']:>9.2f}{r['fees']:>8.2f}{r['net']:>9.2f}{r['ret']:>8.2f}%")


def main():
    cfg = load_config(dry_run=True)
    cmc = CMCClient(cfg.cmc_api_key)
    reg = TokenRegistry()
    tokens = list(cfg.tokens.watchlist)
    reg.ensure_id_map(cmc, tokens)
    ef = edge_floors()
    try:
        fg = fetch_fear_greed_by_day(cmc, days=60)
    except CMCError:
        fg = {}

    for bars in (168, 480):
        common, closes, vols = fetch_price_vol(cmc, reg, tokens, bars)
        n = len(common)
        sigs = precompute_signals(closes, DEFAULT_PARAMS)
        scales = regime_overlay(common, fg, "asym") if fg else None
        kw = dict(scales=scales, edge_floor=ef, vols=vols, vol_ratio=1.0)
        print(f"\n{'='*82}\n  {bars}h window ({common[0][:10]} .. {common[-1][:10]})  "
              f"live cfg (vol filter on)\n{'='*82}")
        print(f"  {'stop':<22}{'trades':>7}{'wins':>6}{'stops':>7}{'gross$':>9}"
              f"{'fees$':>8}{'net$':>9}{'ret':>8}")
        row("fixed 8% (current)", simulate(closes, sigs, n, stop_mode="fixed", stop_pct=8, **kw))
        for k in (1.5, 2.0, 2.5, 3.0):
            row(f"ATR k={k}x range", simulate(closes, sigs, n, stop_mode="atr", atr_k=k, **kw))

    dates, dcloses = fetch_daily(cmc, reg, tokens)
    n = len(dates)
    dsigs = precompute_signals(dcloses, DEFAULT_PARAMS)
    print(f"\n{'='*82}\n  1-YEAR daily ({dates[0]} .. {dates[-1]})  "
          f"(1h-tuned params on daily — sanity)\n{'='*82}")
    print(f"  {'stop':<22}{'trades':>7}{'wins':>6}{'stops':>7}{'gross$':>9}"
          f"{'fees$':>8}{'net$':>9}{'ret':>8}")
    dkw = dict(scales=None, edge_floor=ef, vols=None, vol_ratio=0.0)
    row("fixed 8% (current)", simulate(dcloses, dsigs, n, stop_mode="fixed", stop_pct=8, **dkw))
    for k in (1.5, 2.0, 2.5, 3.0):
        row(f"ATR k={k}x range", simulate(dcloses, dsigs, n, stop_mode="atr", atr_k=k, **dkw))


if __name__ == "__main__":
    main()
