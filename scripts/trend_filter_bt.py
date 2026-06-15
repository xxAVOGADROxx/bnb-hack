"""Long-term trend filter backtest: only go long a token that is ABOVE its own
long EMA (don't catch falling knives). Layered ON TOP of the live config
(volume filter #11 already on), so this isolates the trend gate's marginal
effect. Run on 7d + 20d (1h, with the volume filter) and the year (daily).

The filter is applied at the signal level: entries whose bar is below the trend
EMA (optionally: and EMA not rising) are neutralized to None, then the existing
honest sims run unchanged. ratio/span 0 = off.

Usage: .venv/bin/python scripts/trend_filter_bt.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.cmc.client import CMCClient, CMCError  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.signals.technical import DEFAULT_PARAMS, ema  # noqa: E402
from agent.tokens import TokenRegistry  # noqa: E402
from scripts.backtest import (  # noqa: E402
    fetch_fear_greed_by_day, precompute_signals, regime_overlay, simulate as daily_sim,
)
from scripts.vol_filter_bt import fetch_price_vol, simulate as intraday_sim
from scripts.year_forecast import fetch_daily


def trend_mask(closes, signals, span, require_rising=False):
    """Return a copy of signals with entry_conv set to None where price is at/
    below the long EMA (and, if require_rising, where the EMA is falling)."""
    out = {}
    for tok, flags in signals.items():
        e = ema(pd.Series(closes[tok]), span).tolist()
        masked = []
        for t, (entry, ex, exp, drange) in enumerate(flags):
            ok = closes[tok][t] > e[t]
            if require_rising and t > 0:
                ok = ok and e[t] > e[t - 1]
            masked.append((entry if ok else None, ex, exp, drange))
        out[tok] = masked
    return out


def line(label, r):
    print(f"  {label:<26}{r['trades']:>7}{r['wins']:>6}{r['gross_pnl']:>9.2f}"
          f"{r['fees']:>9.2f}{r['net_pnl']:>9.2f}{r['ret_net_pct']:>8.2f}%")


def main():
    cfg = load_config(dry_run=True)
    cmc = CMCClient(cfg.cmc_api_key)
    reg = TokenRegistry()
    tokens = list(cfg.tokens.watchlist)
    reg.ensure_id_map(cmc, tokens)
    try:
        fg = fetch_fear_greed_by_day(cmc, days=60)
    except CMCError:
        fg = {}

    # ---- intraday windows (vol filter ON = current live config) -----------
    for bars in (168, 480):
        common, closes, vols = fetch_price_vol(cmc, reg, tokens, bars)
        n = len(common)
        sigs = precompute_signals(closes, DEFAULT_PARAMS)
        scales = regime_overlay(common, fg, "asym") if fg else None
        edge_floor = {}
        try:
            import json
            from agent.config import DATA_DIR
            liq = json.loads((DATA_DIR / "liquidity_report.json").read_text())
            edge_floor = {r["symbol"]: r["round_trip_cost_pct"] + 0.5
                          for r in liq.get("results", [])}
        except (OSError, ValueError, KeyError):
            pass
        print(f"\n{'='*80}\n  {bars}h window ({common[0][:10]} .. {common[-1][:10]})  "
              f"vol filter ON\n{'='*80}")
        print(f"  {'config':<26}{'trades':>7}{'wins':>6}{'gross$':>9}{'fees$':>9}"
              f"{'net$':>9}{'ret':>9}")
        base_kw = dict(scales=scales, edge_floor=edge_floor, vol_ratio=1.0)
        line("live (no trend)", intraday_sim(closes, vols, sigs, n, **base_kw))
        for span in (100, 200):
            line(f"+ trend ema{span}",
                 intraday_sim(closes, vols, trend_mask(closes, sigs, span), n, **base_kw))
        line("+ trend ema200 rising",
             intraday_sim(closes, vols, trend_mask(closes, sigs, 200, True), n, **base_kw))

    # ---- the year (daily) -------------------------------------------------
    dates, dcloses = fetch_daily(cmc, reg, tokens)
    n = len(dates)
    dsigs = precompute_signals(dcloses, DEFAULT_PARAMS)
    print(f"\n{'='*80}\n  1-YEAR daily ({dates[0]} .. {dates[-1]}), live cfg (stop8+vol5)"
          f"\n{'='*80}")
    print("  (caveat: 1h-tuned params on daily bars — regime sanity, not live config)")
    print(f"  {'config':<26}{'cost':>6}{'ret':>9}{'maxDD':>8}{'trades':>8}{'win':>7}")
    for label, sg in (("live (no trend)", dsigs),
                       ("+ trend ema30", trend_mask(dcloses, dsigs, 30)),
                       ("+ trend ema50", trend_mask(dcloses, dsigs, 50)),
                       ("+ trend ema50 rising", trend_mask(dcloses, dsigs, 50, True))):
        for cost in (1.5, 3.0):
            r = daily_sim(dcloses, sg, n, cost, 2.0, stop_loss_pct=8, vol_target=5)
            print(f"  {label:<26}{cost:>5.1f}%{r['return_pct']:>8.2f}%"
                  f"{r['max_drawdown_pct']:>7.2f}%{r['trades_closed']:>8}{str(r['win_rate_pct']):>7}")


if __name__ == "__main__":
    main()
