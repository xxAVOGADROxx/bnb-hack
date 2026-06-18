"""Does the F&G regime gate (#4 asym) let the EXPANDED (waiver) watchlist
capture upside while capping downside? A/B the gate ON vs OFF on:
  - the REAL recent downtrend (expanded tokens, real historical F&G), and
  - a SYNTHETIC bull (expanded count, F&G rising 50->85 into euphoria).
Cost = waiver (the expanded watchlist only exists at waiver pricing).

asym gate: F&G>=80 -> no new entries (don't buy euphoria); <=20 -> half scale
+ conviction floor (cautious in capitulation); else full scale.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

import scripts.backtest as bt  # noqa: E402
from agent.cmc.client import CMCClient  # noqa: E402
from agent.config import DATA_DIR, load_config  # noqa: E402
from agent.signals.technical import DEFAULT_PARAMS  # noqa: E402
from agent.tokens import TokenRegistry  # noqa: E402

WAIVER = 0.2  # round-trip %, waiver pricing


def run(closes, n, scales, cfg):
    bt.START_EQUITY = 5000.0
    s = bt.precompute_signals(closes, DEFAULT_PARAMS)
    r = bt.simulate(closes, s, n, round_trip_pct=WAIVER,
                    min_edge_pct=cfg.risk.min_expected_edge_pct,
                    stop_loss_pct=8.0, vol_target=5.0, cooldown_bars=24,
                    edge_floor={}, regime_scales=scales)
    return r


def line(label, r):
    print(f"  {label:<12} ret {r['return_pct']:>+6.2f}%   maxDD {r['max_drawdown_pct']:>5.2f}%"
          f"   trades {r['trades_opened']:>3}   win {r['win_rate_pct'] or 0:>4.0f}%")


def main() -> None:
    cfg = load_config(dry_run=True)
    cmc = CMCClient(cfg.cmc_api_key)
    reg = TokenRegistry()
    toks = json.loads((DATA_DIR / "waiver_watchlist.json").read_text())["waiver"]
    reg.ensure_id_map(cmc, toks)

    # ---- REAL DOWNTREND ----
    common, closes = bt.fetch_aligned(cmc, reg, toks, 480)
    n = len(common)
    asym = bt.regime_overlay(common, bt.fetch_fear_greed_by_day(cmc, 60), "asym")
    print(f"REAL DOWNTREND — expanded watchlist ({len(closes)} aligned), waiver cost, "
          f"{n} bars, B&H {bt.buy_and_hold(closes, WAIVER):+.2f}%")
    line("gate OFF", run(closes, n, None, cfg))
    line("gate ASYM", run(closes, n, asym, cfg))

    # ---- SYNTHETIC BULL (F&G rising into euphoria) ----
    WARM, WEEK, N = 60, 168, 24
    syn = {}
    for i in range(N):
        rng = np.random.default_rng(9000 + i)
        d = 1.08 ** (1 / 168) - 1
        syn[f"T{i}"] = (100 * np.cumprod(1 + d + rng.normal(0, 0.015, WARM + WEEK))).tolist()
    nb = WARM + WEEK
    fg = [50 + 35 * max(0, t - WARM) / WEEK for t in range(nb)]  # 50 -> 85 over the week
    asym_syn = [(0.0, 0.0) if v >= 80 else (1.0, 0.0) for v in fg]  # block euphoria
    blocked = sum(1 for v in fg if v >= 80)
    print(f"\nSYNTHETIC BULL (+8%/wk) — expanded count ({N}), waiver cost, F&G 50->85 "
          f"(last {blocked} bars euphoric), B&H {bt.buy_and_hold(syn, WAIVER):+.2f}%")
    line("gate OFF", run(syn, nb, None, cfg))
    line("gate ASYM", run(syn, nb, asym_syn, cfg))


if __name__ == "__main__":
    main()
