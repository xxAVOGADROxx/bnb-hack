"""Backtest exit hysteresis for the momentum strategy (whipsaw fix).

Motivation (2026-07-04): live round-trips of 11-60 min — entry on a marginal
breakout tick, exit fired by the hair-trigger momentum-break (price a hair
below EMA9 / one falling MACD bar) minutes later. Each round trip pays ~1%
friction, so these are structurally losing trades. Hypothesis: requiring the
break to clear a margin below the EMA and/or persist for N bars kills the
whipsaws without giving back much on real reversals (the fixed 8% stop still
backstops).

Honesty rules (same as backtest.py):
- Signals come from the ACTUAL MomentumStrategy plugin — the hysteresis knobs
  are constructor params whose defaults reproduce legacy behavior bit-for-bit.
- Data is Binance hourly klines via agent.market.feed.MarketFeed — the SAME
  feed the live loop reads since the CMC removal (2026-07-03), so no basis
  drift between backtest and production signals.
- simulate() is reused from atr_stop_bt (live config: vol filter, cooldown,
  edge floors, fixed 8% stop). Sizing patched to live risk.yaml
  (max_concurrent=1, max_position_pct=10).
- Hourly bars cannot see intra-hour whipsaw (live evaluates every 5 min on a
  partial candle), so the live benefit of hysteresis is UNDERSTATED here.
- Regime overlay off (free F&G history not wired); period was risk_on
  throughout, and the overlay would be identical across variants anyway.

Usage: .venv/bin/python scripts/exit_hysteresis_bt.py [--bars 720]
Writes data/exit_hysteresis_report.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.cmc.client import CMCError  # noqa: E402
from agent.config import DATA_DIR, load_config  # noqa: E402
from agent.market.feed import MarketFeed  # noqa: E402
from agent.strategies.momentum import MomentumStrategy  # noqa: E402
from agent.tokens import TokenRegistry  # noqa: E402
from scripts import atr_stop_bt  # noqa: E402
from scripts.atr_stop_bt import edge_floors, simulate  # noqa: E402
from scripts.strategy_bt import precompute_plugin  # noqa: E402

# Live sizing (risk.yaml), not the harness default 25%/3.
atr_stop_bt.MAX_CONCURRENT = 1
atr_stop_bt.MAX_POSITION_PCT = 10.0

# (label, exit_ema_buffer_pct, exit_confirm_bars, macd_confirm_bars)
VARIANTS = [
    ("base (live)", 0.0, 1, 1),
    ("buf0.5", 0.5, 1, 1),
    ("buf1.0", 1.0, 1, 1),
    ("buf1.5", 1.5, 1, 1),
    ("conf2", 0.0, 2, 1),
    ("macd2", 0.0, 1, 2),
    ("buf0.5+conf2", 0.5, 2, 1),
    ("buf1.0+conf2+macd2", 1.0, 2, 2),
]
COST_SCENARIOS = [1.0, 2.0]  # round-trip %, half per leg


def fetch_aligned(feed: MarketFeed, registry: TokenRegistry,
                  tokens: list[str], bars: int):
    """Aligned (closes, rolling-24h volumes) per token on common timestamps."""
    series = {}
    for tok in tokens:
        cid = registry.cmc_id(tok)
        if cid is None:
            print(f"  {tok}: sin cmc_id, omitido")
            continue
        try:
            rows = feed.series_with_volume(cid, "1h", bars, ttl_s=0)
        except CMCError as e:
            print(f"  {tok}: sin klines ({str(e)[:60]}), omitido")
            continue
        series[tok] = {ts: (px, vol) for ts, px, vol in rows}
    common = sorted(set.intersection(*(set(s) for s in series.values())))
    closes = {t: [series[t][ts][0] for ts in common] for t in series}
    vols = {t: [series[t][ts][1] for ts in common] for t in series}
    return common, closes, vols


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=720)
    args = ap.parse_args()

    cfg = load_config(dry_run=True)
    registry = TokenRegistry()
    feed = MarketFeed(registry)
    tokens = list(cfg.tokens.watchlist)
    ef = edge_floors()

    report = {}
    for bars in (240, args.bars):
        common, closes, vols = fetch_aligned(feed, registry, tokens, bars)
        n = len(common)
        print(f"\n{'=' * 78}\n  {bars}h window ({common[0][:10]} .. {common[-1][:10]}, "
              f"{len(closes)} tokens, {n} bars)\n{'=' * 78}")
        for cost in COST_SCENARIOS:
            atr_stop_bt.COST = cost
            print(f"\n  round-trip cost {cost:.1f}%  (live cfg: 10%/1pos/8%stop/"
                  f"cooldown24/vol-filter)")
            print(f"  {'variant':<20}{'trades':>7}{'wins':>6}{'stops':>7}"
                  f"{'gross$':>9}{'fees$':>8}{'net$':>9}{'ret':>8}")
            for label, buf, conf, macd_c in VARIANTS:
                strat = MomentumStrategy(exit_ema_buffer_pct=buf,
                                         exit_confirm_bars=conf,
                                         macd_confirm_bars=macd_c)
                sigs = precompute_plugin(closes, vols, strat)
                r = simulate(closes, sigs, n, None, ef, vols=vols, vol_ratio=1.0,
                             stop_mode="fixed", stop_pct=8)
                print(f"  {label:<20}{r['trades']:>7}{r['wins']:>6}{r['stops']:>7}"
                      f"{r['gross']:>9.2f}{r['fees']:>8.2f}{r['net']:>9.2f}"
                      f"{r['ret']:>8.2f}%")
                report[f"{bars}h/{cost}%/{label}"] = r

    out = DATA_DIR / "exit_hysteresis_report.json"
    out.write_text(json.dumps(report, indent=2))
    print(f"\nreport -> {out}")


if __name__ == "__main__":
    main()
