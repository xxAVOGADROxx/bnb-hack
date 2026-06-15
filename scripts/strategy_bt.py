"""Backtest each pluggable strategy head-to-head, same windows, same live config.

Signals come from the ACTUAL strategy plugins (agent/strategies/registry.py) via
the same MarketContext the live loop builds — not a re-implementation. The
universal live config (volume filter #11, asym regime gate, cooldown, edge
floor, vol-target sizing, fixed 8% stop) is applied identically to every
strategy, so this isolates strategy quality. Gross/fees/net are reported so the
fee lens stays.

Usage: .venv/bin/python scripts/strategy_bt.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.cmc.client import CMCClient, CMCError  # noqa: E402
from agent.config import DATA_DIR, load_config  # noqa: E402
from agent.signals.technical import Action  # noqa: E402
from agent.strategies import registry  # noqa: E402
from agent.strategies.base import MarketContext  # noqa: E402
from agent.tokens import TokenRegistry  # noqa: E402
from scripts.atr_stop_bt import simulate  # noqa: E402
from scripts.backtest import fetch_fear_greed_by_day, regime_overlay  # noqa: E402
from scripts.vol_filter_bt import fetch_price_vol  # noqa: E402
from scripts.year_forecast import fetch_daily  # noqa: E402

WARMUP = 60


def precompute_plugin(closes, volumes, strategy):
    """Per token/bar: (entry_conv|None, exit_flag, expected_move, daily_range)
    from the strategy plugin — two evaluate() calls (flat/holding), mirroring
    the live loop's entry and exit reads."""
    out = {}
    for tok, series in closes.items():
        vols = volumes.get(tok, [0.0] * len(series)) if volumes else [0.0] * len(series)
        flags = []
        for t in range(len(series)):
            if t < WARMUP:
                flags.append((None, False, 0.0, 0.0))
                continue
            win, vw = series[: t + 1], vols[: t + 1]
            flat = strategy.evaluate(MarketContext(tok, win, vw, holding=False))
            held = strategy.evaluate(MarketContext(tok, win, vw, holding=True))
            entry = flat.conviction if flat.action == Action.BUY else None
            flags.append((entry, held.action == Action.SELL,
                          flat.expected_move_pct, flat.daily_range_pct))
        out[tok] = flags
    return out


def _edge_floors():
    try:
        liq = json.loads((DATA_DIR / "liquidity_report.json").read_text())
        return {r["symbol"]: r["round_trip_cost_pct"] + 0.5 for r in liq.get("results", [])}
    except (OSError, ValueError, KeyError):
        return {}


def row(name, r):
    print(f"  {name:<16}{r['trades']:>7}{r['wins']:>6}{r['stops']:>7}"
          f"{r['gross']:>9.2f}{r['fees']:>8.2f}{r['net']:>9.2f}{r['ret']:>8.2f}%")


def main():
    cfg = load_config(dry_run=True)
    cmc = CMCClient(cfg.cmc_api_key)
    reg = TokenRegistry()
    tokens = list(cfg.tokens.watchlist)
    reg.ensure_id_map(cmc, tokens)
    ef = _edge_floors()
    try:
        fg = fetch_fear_greed_by_day(cmc, days=60)
    except CMCError:
        fg = {}
    strategies = registry.available()

    for bars in (168, 480):
        common, closes, vols = fetch_price_vol(cmc, reg, tokens, bars)
        n = len(common)
        scales = regime_overlay(common, fg, "asym") if fg else None
        print(f"\n{'='*74}\n  {bars}h window ({common[0][:10]} .. {common[-1][:10]})  "
              f"live cfg\n{'='*74}")
        print(f"  {'strategy':<16}{'trades':>7}{'wins':>6}{'stops':>7}{'gross$':>9}"
              f"{'fees$':>8}{'net$':>9}{'ret':>8}")
        for name in strategies:
            sigs = precompute_plugin(closes, vols, registry.build(name))
            r = simulate(closes, sigs, n, scales, ef, vols=vols, vol_ratio=1.0,
                         stop_mode="fixed", stop_pct=8)
            row(name, r)

    dates, dcloses = fetch_daily(cmc, reg, tokens)
    n = len(dates)
    print(f"\n{'='*74}\n  1-YEAR daily ({dates[0]} .. {dates[-1]})  "
          f"(no intraday vol filter)\n{'='*74}")
    print(f"  {'strategy':<16}{'trades':>7}{'wins':>6}{'stops':>7}{'gross$':>9}"
          f"{'fees$':>8}{'net$':>9}{'ret':>8}")
    for name in strategies:
        sigs = precompute_plugin(dcloses, None, registry.build(name))
        r = simulate(dcloses, sigs, n, None, ef, vols=None, vol_ratio=0.0,
                     stop_mode="fixed", stop_pct=8)
        row(name, r)


if __name__ == "__main__":
    main()
