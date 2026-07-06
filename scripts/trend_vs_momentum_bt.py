"""Head-to-head: trend (the validated default) vs momentum (the navaja),
post-competition — which plugin should the live testbed run?

Same honesty rules as every harness here: actual plugins, the same free feed
the loop reads, live risk.yaml sizing/filters (10%/1pos, 6h cooldown, vol
confirm, edge floors, fixed 8% stop), fees at 1%/2% round trip. simulate()
is reused from scripts/atr_adx_bt (stop/ADX extensions left at live: fixed
8%, ADX off).

Usage: .venv/bin/python scripts/trend_vs_momentum_bt.py [--bars 720]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import load_config  # noqa: E402
from agent.market.feed import MarketFeed  # noqa: E402
from agent.strategies.registry import build  # noqa: E402
from agent.tokens import TokenRegistry  # noqa: E402
from scripts.atr_adx_bt import fetch_ohlc, row, simulate  # noqa: E402
from scripts.atr_stop_bt import edge_floors  # noqa: E402
from scripts.strategy_bt import precompute_plugin  # noqa: E402

STRATEGIES = ("trend", "momentum")
COST_SCENARIOS = [1.0, 2.0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=720)
    args = ap.parse_args()
    cfg = load_config(dry_run=True)
    registry = TokenRegistry()
    feed = MarketFeed(registry)
    tokens = list(cfg.tokens.watchlist)
    ef = edge_floors()

    for bars in (240, args.bars):
        common, highs, lows, closes, vols = fetch_ohlc(feed, registry, tokens, bars)
        n = len(common)
        zeros = {t: [0.0] * n for t in closes}
        print(f"\n{'=' * 78}\n  {bars}h ({common[0][:10]} .. {common[-1][:10]}, "
              f"{len(closes)} tokens)  live cfg\n{'=' * 78}")
        sigs = {name: precompute_plugin(closes, vols, build(name))
                for name in STRATEGIES}
        for cost in COST_SCENARIOS:
            print(f"\n  round-trip cost {cost:.0f}%")
            print(f"  {'strategy':<22}{'trades':>7}{'wins':>6}{'stops':>7}"
                  f"{'gross$':>9}{'fees$':>8}{'net$':>9}{'ret':>8}")
            for name in STRATEGIES:
                r = simulate(closes, sigs[name], n, ef, vols, zeros, zeros, cost)
                row(name, r)


if __name__ == "__main__":
    main()
