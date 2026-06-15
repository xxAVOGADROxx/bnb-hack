"""Volume-confirmation filter backtest + gross-vs-net (fee) decomposition.

Hypothesis: the strategy's GROSS edge is positive but fees flip it negative,
so cutting the marginal (low-edge) entries should help more than anything.
A volume gate — only enter when volume_24h is rising vs its own recent trend —
targets exactly those weak entries.

Reuses the live signal path (precompute_signals) and the live risk mechanics
(asym gate, stop 8%, vol-target 5%, cooldown 24h, edge floor). Adds an entry
gate on volume_24h pulled per bar from the same /quotes/historical payload.

Usage: .venv/bin/python scripts/vol_filter_bt.py [--bars 168 480]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.cmc.client import CMCClient, CMCError, usd_quote  # noqa: E402
from agent.config import DATA_DIR, load_config  # noqa: E402
from agent.signals.technical import DEFAULT_PARAMS, vol_mult  # noqa: E402
from agent.tokens import TokenRegistry  # noqa: E402
from scripts.backtest import (  # noqa: E402
    MAX_CONCURRENT, MAX_POSITION_PCT, START_EQUITY,
    fetch_fear_greed_by_day, precompute_signals, regime_overlay,
)

COST, MIN_EDGE, STOP_LOSS, VOL_TARGET, VOL_FLOOR, COOLDOWN = 1.5, 2.0, 8.0, 5.0, 0.5, 24


def fetch_price_vol(cmc, registry, tokens, bars):
    """Aligned (price, volume_24h) per token on common timestamps."""
    series = {}
    for tok in tokens:
        cid = registry.cmc_id(tok)
        if cid is None:
            continue
        try:
            raw = cmc._get("/v2/cryptocurrency/quotes/historical",
                           {"id": cid, "interval": "1h", "count": bars, "convert": "USD"},
                           ttl_s=0)
        except CMCError:
            continue
        pts = {}
        for p in raw.get("quotes", []):
            q = usd_quote(p)
            ts = p.get("timestamp") or q.get("timestamp")
            if q.get("price") is not None and ts:
                pts[ts] = (float(q["price"]), float(q.get("volume_24h") or 0.0))
        series[tok] = pts
    common = sorted(set.intersection(*(set(s) for s in series.values())))
    closes = {t: [series[t][ts][0] for ts in common] for t in series}
    vols = {t: [series[t][ts][1] for ts in common] for t in series}
    return common, closes, vols


def vol_ok(vol_series, t, lookback, ratio):
    """volume_24h at t must be >= ratio * trailing mean (rising attention)."""
    if t < lookback:
        return True
    window = vol_series[t - lookback:t]
    avg = sum(window) / len(window) if window else 0.0
    return avg <= 0 or vol_series[t] >= ratio * avg


def simulate(closes, vols, signals, n, scales, edge_floor,
             vol_ratio=0.0, vol_lookback=24):
    leg = COST / 2 / 100
    cash, positions, trades = START_EQUITY, {}, []
    last_exit = {}

    def close(tok, t, px, reason):
        nonlocal cash
        pos = positions.pop(tok)
        gross_out = pos["qty"] * px
        cash += gross_out * (1 - leg)
        tr = trades[pos["idx"]]
        tr.update(exit_bar=t, fee_exit=gross_out * leg, proceeds=gross_out * (1 - leg),
                  reason=reason)
        tr["pnl_net"] = tr["proceeds"] - tr["entry_usd"]
        tr["fee_total"] = tr["fee_entry"] + tr["fee_exit"]
        tr["pnl_gross"] = tr["pnl_net"] + tr["fee_total"]
        last_exit[tok] = t

    for t in range(n):
        for tok in list(positions):
            _, exit_flag, _, _ = signals[tok][t]
            px = closes[tok][t]
            if px <= positions[tok]["entry_px"] * (1 - STOP_LOSS / 100):
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
            entry_conv, _, exp_move, drange = signals[tok][t]
            floor = max(MIN_EDGE, (edge_floor or {}).get(tok, 0.0))
            if entry_conv is None or exp_move < floor or entry_conv < r_floor:
                continue
            if vol_ratio > 0 and not vol_ok(vols[tok], t, vol_lookback, vol_ratio):
                continue
            usd = equity * MAX_POSITION_PCT / 100 * entry_conv * r_scale * vol_mult(
                drange, VOL_TARGET, VOL_FLOOR)
            if usd < 10 or usd > cash:
                continue
            qty = usd / series[t] * (1 - leg)
            cash -= usd
            positions[tok] = {"qty": qty, "entry_px": series[t], "idx": len(trades)}
            trades.append({"token": tok, "entry_usd": usd, "fee_entry": usd * leg,
                           "exit_bar": None})
    final = cash + sum(p["qty"] * closes[tok][-1] * (1 - leg) for tok, p in positions.items())
    closed = [tr for tr in trades if tr["exit_bar"] is not None]
    fees = sum(tr["fee_total"] for tr in closed)
    gross = sum(tr["pnl_gross"] for tr in closed)
    net = sum(tr["pnl_net"] for tr in closed)
    wins = sum(1 for tr in closed if tr["pnl_net"] > 0)
    return {
        "trades": len(closed), "wins": wins,
        "ret_net_pct": (final / START_EQUITY - 1) * 100,
        "gross_pnl": gross, "fees": fees, "net_pnl": net,
        "final": final, "open": len(positions),
    }


def run(bars, cmc, registry, tokens, scales_cache):
    common, closes, vols = fetch_price_vol(cmc, registry, tokens, bars)
    n = len(common)
    signals = precompute_signals(closes, DEFAULT_PARAMS)
    fg = scales_cache
    scales = regime_overlay(common, fg, "asym") if fg else None
    edge_floor = {}
    try:
        liq = json.loads((DATA_DIR / "liquidity_report.json").read_text())
        edge_floor = {r["symbol"]: r["round_trip_cost_pct"] + 0.5 for r in liq.get("results", [])}
    except (OSError, ValueError, KeyError):
        pass
    print(f"\n{'='*82}")
    print(f"  {bars}h window ({common[0][:10]} .. {common[-1][:10]})  start ${START_EQUITY:,.0f}")
    print(f"{'='*82}")
    print(f"  {'config':<22}{'trades':>7}{'wins':>6}{'gross$':>9}{'fees$':>9}"
          f"{'net$':>9}{'ret_net':>9}")
    configs = [("no filter", 0.0), ("vol>=1.0x avg", 1.0),
               ("vol>=1.15x avg", 1.15), ("vol>=1.3x avg", 1.3)]
    for label, ratio in configs:
        r = simulate(closes, vols, signals, n, scales, edge_floor, vol_ratio=ratio)
        print(f"  {label:<22}{r['trades']:>7}{r['wins']:>6}{r['gross_pnl']:>9.2f}"
              f"{r['fees']:>9.2f}{r['net_pnl']:>9.2f}{r['ret_net_pct']:>8.2f}%")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, nargs="+", default=[168, 480])
    args = ap.parse_args()
    cfg = load_config(dry_run=True)
    cmc = CMCClient(cfg.cmc_api_key)
    registry = TokenRegistry()
    tokens = list(cfg.tokens.watchlist)
    registry.ensure_id_map(cmc, tokens)
    try:
        fg = fetch_fear_greed_by_day(cmc, days=60)
    except CMCError:
        fg = {}
    for b in args.bars:
        run(b, cmc, registry, tokens, fg)


if __name__ == "__main__":
    main()
