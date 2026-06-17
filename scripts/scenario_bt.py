"""Two scenarios the standard backtest can't show on its own:

1. UNIVERSE — does a broader, higher-volatility token list beat the tight
   4-token watchlist? Runs the exact live engine on REAL recent hourly data for
   the 4-token watchlist vs the ~19 screened candidates (mixed friction), with
   PER-TOKEN measured edge floors so high-friction names self-exclude.

2. UPTREND — how does the model behave when the market actually rises? The last
   month of real hourly data is a downtrend, so we synthesize hourly uptrend
   series (positive drift + realistic noise) and run the SAME engine (correctly
   calibrated for hourly) to see if it captures the move net of fees.

Honest by construction: same simulate(), same signals; only the data/universe
changes. Usage: .venv/bin/python scripts/scenario_bt.py [--bars 480]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

import scripts.backtest as bt  # noqa: E402
from scripts.liquidity_filter import DEFAULT_CANDIDATES  # noqa: E402
from agent.cmc.client import CMCClient  # noqa: E402
from agent.config import DATA_DIR, load_config  # noqa: E402
from agent.signals.technical import DEFAULT_PARAMS  # noqa: E402
from agent.tokens import TokenRegistry  # noqa: E402

COSTS = [1.5, 3.0]  # round-trip %, bracketing liquid vs high-friction names


def _floors(margin: float) -> dict:
    try:
        liq = json.loads((DATA_DIR / "liquidity_report.json").read_text())
        return {r["symbol"]: r["round_trip_cost_pct"] + margin for r in liq.get("results", [])}
    except Exception:  # noqa: BLE001
        return {}


def _row(name, closes, signals, n, cost, edge_floor, cfg):
    bt.START_EQUITY = 5000.0
    r = bt.simulate(closes, signals, n, round_trip_pct=cost,
                    min_edge_pct=cfg.risk.min_expected_edge_pct,
                    stop_loss_pct=getattr(cfg.risk, "stop_loss_pct", 8.0),
                    vol_target=getattr(cfg.risk, "vol_target_pct", 5.0),
                    cooldown_bars=int(getattr(cfg.risk, "reentry_cooldown_h", 24)),
                    edge_floor=edge_floor)
    win = r["win_rate_pct"] if r["win_rate_pct"] is not None else 0.0
    print(f"{name:<28}{cost:>5.1f}{r['return_pct']:>+9.2f}{r['max_drawdown_pct']:>8.2f}"
          f"{r['trades_opened']:>8}{win:>7.1f}")
    return r


def universe_test(cfg, cmc, registry, bars):
    print("\n" + "=" * 66)
    print("1) UNIVERSE — broad/high-vol vs the tight watchlist (REAL hourly)")
    print("=" * 66)
    # Global 2% floor only (no per-token floor) so the comparison isolates
    # UNIVERSE BREADTH; the two cost scenarios bracket liquid vs high-friction.
    watch = list(cfg.tokens.watchlist)
    broad = list(dict.fromkeys(DEFAULT_CANDIDATES))  # 19 screened candidates
    print(f"{'universe':<28}{'cost':>5}{'ret%':>9}{'maxDD%':>8}{'trades':>8}{'win%':>7}")
    print("-" * 66)
    for label, toks in (("watchlist (4, <=1.5%)", watch), (f"broad ({len(broad)}, mixed)", broad)):
        common, closes = bt.fetch_aligned(cmc, registry, toks, bars)
        nb = len(next(iter(closes.values()))) if closes else 0
        sig = bt.precompute_signals(closes, DEFAULT_PARAMS)
        bh = bt.buy_and_hold(closes, 1.5)
        for c in COSTS:
            _row(label, closes, sig, nb, c, {}, cfg)
        print(f"   (buy&hold {bh:+.2f}% over {nb} bars, {len(closes)} tokens aligned)\n")


def uptrend_test(cfg, n_tokens=6, bars=300, drift_wk=0.08):
    print("=" * 66)
    print(f"2) UPTREND — synthetic hourly, +{drift_wk*100:.0f}%/week drift, "
          f"≈{bars/168:.1f} weeks")
    print("=" * 66)
    print(f"{'profile / cost':<28}{'cost':>5}{'ret%':>9}{'maxDD%':>8}{'trades':>8}{'win%':>7}")
    print("-" * 66)
    # smooth grind vs choppy bull — capture depends heavily on volatility (the
    # RSI<70 + 2% edge gate sits out smooth/overbought moves, enters on dips).
    for vol_hr in (0.008, 0.020):
        closes = {}
        for i in range(n_tokens):
            rng = np.random.default_rng(1000 + i + int(vol_hr * 1000))
            hr_drift = (1 + drift_wk) ** (1 / 168) - 1
            rets = hr_drift + rng.normal(0, vol_hr, bars)
            closes[f"SYN{i}"] = (100 * np.cumprod(1 + rets)).tolist()
        sig = bt.precompute_signals(closes, DEFAULT_PARAMS)
        bh = bt.buy_and_hold(closes, 1.5)
        tag = "smooth" if vol_hr < 0.015 else "choppy"
        for label, c in ((f"{tag} ({vol_hr*100:.1f}%/h) std", 1.5),
                         (f"{tag} ({vol_hr*100:.1f}%/h) waiver", 0.2)):
            _row(label, closes, sig, bars, c, {}, cfg)
        print(f"   (buy&hold over this uptrend: {bh:+.2f}%)\n")
    print("   NOTE: synthetic — tests whether the trend logic CAPTURES an up-move "
          "net of fees, not a market forecast.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=480)
    args = ap.parse_args()
    cfg = load_config(dry_run=True)
    cmc = CMCClient(cfg.cmc_api_key)
    registry = TokenRegistry()
    universe_test(cfg, cmc, registry, args.bars)
    uptrend_test(cfg)


if __name__ == "__main__":
    main()
