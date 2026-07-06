"""Backtest F3: true-ATR adaptive stop and ADX trend gate for momentum.

Now that OHLC is free (Binance klines via MarketFeed.ohlcv_series), test the
two indicators the stack lacked:

  ATR stop  — stop at entry_px x (1 - k x ATR14%/100) captured at ENTRY,
              vs the live fixed 8%. High-vol tokens get room, calm ones get
              a tight leash.
  ADX gate  — momentum entries only when ADX14 >= cut (chop filter: the
              breakout sleeve keeps paying friction on breakouts that die
              in ranging tape).

Honesty rules (same as exit_hysteresis_bt):
- Signals from the ACTUAL MomentumStrategy plugin at live hysteresis params.
- Same feed the live loop reads (Binance klines), aligned timestamps.
- simulate() forked from atr_stop_bt with two changes ONLY: the stop can use
  a real per-bar ATR%% series captured at entry, and entries can be masked by
  an ADX floor. Sizing/cooldown/vol-filter/edge-floors identical to live.
- Hourly bars understate intra-hour effects (live cycles every 5 min).

Usage: .venv/bin/python scripts/atr_adx_bt.py [--bars 720]
Writes data/atr_adx_report.json.

VERDICT 2026-07-06 (240h + 720h, 15 tokens, cost 1%/2%) — NOTHING WIRED:
  - ATR stop: NO-OP in this regime. With the hysteresis exits the mean hold
    is ~6h and MAE(closes) reaches -2% on 1 of 67 trades — no stop (fixed 8%
    or any ATR k) ever fires; results identical to the cent. On intra-bar
    LOWS, k=2 would have sold 6/67 wicks (realized whipsaw). Fixed 8% stays:
    it is a gap backstop, not a tuning knob.
  - ADX gate: REJECTED — inconsistent (720h: cut 25 trims net loss -18.96 ->
    -12.48 by cutting 17/55 trades; 240h: same cut makes it WORSE). Any
    trade-cutting filter flatters a bleeding sleeve; that is not selection.
  - Honest headline: the momentum sleeve itself is net-negative in both
    windows after friction. The lever is the strategy, not these knobs.
  atr_pct/adx stay in technical.py as library capability for future plugins.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import DATA_DIR, load_config  # noqa: E402
from agent.market.feed import FeedError, MarketFeed  # noqa: E402
from agent.signals.technical import adx, atr_pct, vol_mult  # noqa: E402
from agent.strategies.momentum import MomentumStrategy  # noqa: E402
from agent.tokens import TokenRegistry  # noqa: E402
from scripts.atr_stop_bt import edge_floors  # noqa: E402
from scripts.strategy_bt import precompute_plugin  # noqa: E402

# Live risk.yaml sizing.
START_EQUITY, MAX_POSITION_PCT, MAX_CONCURRENT = 1000.0, 10.0, 1
MIN_EDGE, VOL_TARGET, VOL_FLOOR, COOLDOWN = 2.0, 5.0, 0.5, 6
COST_SCENARIOS = [1.0, 2.0]

STOP_VARIANTS = [("fixed 8% (live)", "fixed", 8.0, 0.0),
                 ("ATR k=2.0", "atr", 8.0, 2.0),
                 ("ATR k=2.5", "atr", 8.0, 2.5),
                 ("ATR k=3.0", "atr", 8.0, 3.0),
                 ("ATR k=3.5", "atr", 8.0, 3.5)]
ADX_CUTS = [0, 15, 20, 25]


def simulate(closes, signals, n, edge_floor, vols, atrs, adxs, cost,
             stop_mode="fixed", stop_pct=8.0, atr_k=2.0, adx_min=0):
    leg = cost / 2 / 100
    cash, positions = START_EQUITY, {}
    tr_list, last_exit = [], {}

    def vol_ok(tok, t):
        if t <= 24:
            return True
        w = vols[tok][t - 24:t]
        avg = sum(w) / len(w) if w else 0.0
        return avg <= 0 or vols[tok][t] >= avg

    def close(tok, t, px, reason):
        nonlocal cash
        pos = positions.pop(tok)
        gross_out = pos["qty"] * px
        cash += gross_out * (1 - leg)
        tr = tr_list[pos["idx"]]
        tr.update(exit=t, reason=reason,
                  fee=pos["fee_in"] + gross_out * leg,
                  net=gross_out * (1 - leg) - pos["usd"])
        tr["gross"] = tr["net"] + tr["fee"]
        last_exit[tok] = t

    for t in range(n):
        for tok in list(positions):
            _, exit_flag, _, _ = signals[tok][t]
            px = closes[tok][t]
            pos = positions[tok]
            if px <= pos["stop_px"]:
                close(tok, t, px, "stop")
            elif exit_flag:
                close(tok, t, px, "signal")
        equity = cash + sum(p["qty"] * closes[tok][t] for tok, p in positions.items())
        for tok, series in closes.items():
            if tok in positions or len(positions) >= MAX_CONCURRENT:
                continue
            if COOLDOWN and t - last_exit.get(tok, -10**9) < COOLDOWN:
                continue
            entry_conv, _, exp_move, dr = signals[tok][t]
            floor = max(MIN_EDGE, (edge_floor or {}).get(tok, 0.0))
            if entry_conv is None or exp_move < floor:
                continue
            if adx_min and adxs[tok][t] < adx_min:
                continue
            if not vol_ok(tok, t):
                continue
            usd = equity * MAX_POSITION_PCT / 100 * entry_conv * vol_mult(
                dr, VOL_TARGET, VOL_FLOOR)
            if usd < 10 or usd > cash:
                continue
            px = series[t]
            if stop_mode == "atr" and atrs[tok][t] > 0:
                stop_px = px * (1 - atr_k * atrs[tok][t] / 100)
            else:
                stop_px = px * (1 - stop_pct / 100)
            qty = usd / px * (1 - leg)
            cash -= usd
            positions[tok] = {"qty": qty, "stop_px": stop_px, "usd": usd,
                              "fee_in": usd * leg, "idx": len(tr_list)}
            tr_list.append({"token": tok, "exit": None})
    final = cash + sum(p["qty"] * closes[tok][-1] * (1 - leg)
                       for tok, p in positions.items())
    closed = [t for t in tr_list if t["exit"] is not None]
    return {
        "trades": len(closed), "wins": sum(1 for t in closed if t["net"] > 0),
        "stops": sum(1 for t in closed if t["reason"] == "stop"),
        "gross": round(sum(t["gross"] for t in closed), 2),
        "fees": round(sum(t["fee"] for t in closed), 2),
        "net": round(sum(t["net"] for t in closed), 2),
        "ret": round((final / START_EQUITY - 1) * 100, 2),
    }


def fetch_ohlc(feed, registry, tokens, bars):
    """Aligned closes/vol24/highs/lows per token on common timestamps."""
    series = {}
    for tok in tokens:
        cid = registry.cmc_id(tok)
        if cid is None:
            continue
        try:
            rows = feed.ohlcv_series(cid, "1h", bars, ttl_s=0)
        except FeedError as e:
            print(f"  {tok}: sin klines ({str(e)[:60]}), omitido")
            continue
        series[tok] = {ts: (h, lo, c, v) for ts, h, lo, c, v in rows}
    common = sorted(set.intersection(*(set(s) for s in series.values())))
    highs = {t: [series[t][ts][0] for ts in common] for t in series}
    lows = {t: [series[t][ts][1] for ts in common] for t in series}
    closes = {t: [series[t][ts][2] for ts in common] for t in series}
    vols = {t: [series[t][ts][3] for ts in common] for t in series}
    return common, highs, lows, closes, vols


def row(label, r):
    print(f"  {label:<22}{r['trades']:>7}{r['wins']:>6}{r['stops']:>7}"
          f"{r['gross']:>9.2f}{r['fees']:>8.2f}{r['net']:>9.2f}{r['ret']:>8.2f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=720)
    args = ap.parse_args()
    cfg = load_config(dry_run=True)
    registry = TokenRegistry()
    feed = MarketFeed(registry)
    tokens = list(cfg.tokens.watchlist)
    ef = edge_floors()
    strat = MomentumStrategy()  # live hysteresis defaults

    report = {}
    for bars in (240, args.bars):
        common, highs, lows, closes, vols = fetch_ohlc(feed, registry, tokens, bars)
        n = len(common)
        atrs = {t: atr_pct(pd.Series(highs[t]), pd.Series(lows[t]),
                           pd.Series(closes[t])).tolist() for t in closes}
        adxs = {t: adx(pd.Series(highs[t]), pd.Series(lows[t]),
                       pd.Series(closes[t])).fillna(0).tolist() for t in closes}
        sigs = precompute_plugin(closes, vols, strat)
        print(f"\n{'=' * 80}\n  {bars}h ({common[0][:10]} .. {common[-1][:10]}, "
              f"{len(closes)} tokens)  momentum live-params\n{'=' * 80}")
        for cost in COST_SCENARIOS:
            print(f"\n  -- stops (ADX off) @ cost {cost:.0f}% --")
            print(f"  {'variant':<22}{'trades':>7}{'wins':>6}{'stops':>7}"
                  f"{'gross$':>9}{'fees$':>8}{'net$':>9}{'ret':>8}")
            for label, mode, pct, k in STOP_VARIANTS:
                r = simulate(closes, sigs, n, ef, vols, atrs, adxs, cost,
                             stop_mode=mode, stop_pct=pct, atr_k=k)
                row(label, r)
                report[f"{bars}h/{cost}%/stop/{label}"] = r
            print(f"\n  -- ADX gate (fixed 8% stop) @ cost {cost:.0f}% --")
            for cut in ADX_CUTS:
                r = simulate(closes, sigs, n, ef, vols, atrs, adxs, cost,
                             adx_min=cut)
                row(f"ADX >= {cut}" if cut else "ADX off (live)", r)
                report[f"{bars}h/{cost}%/adx/{cut}"] = r

    out = DATA_DIR / "atr_adx_report.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"\nreport -> {out}")


if __name__ == "__main__":
    main()
