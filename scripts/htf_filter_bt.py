"""A/B backtest: does a HIGHER-TIMEFRAME trend filter improve the live logic?

Confluence idea: only act on a 1h entry if the bigger-picture trend agrees.
Entries are gated on:
  - 4h uptrend  : EMA(4h,10) > EMA(4h,30), using only COMPLETED 4h buckets
  - daily filter: price > EMA(daily,10), using only PRIOR completed days
Both higher-TF series are derived from the same hourly closes (no extra fetch,
no look-ahead). Everything else is the exact live engine (same simulate()), so
this is an honest A/B — the only change is suppressing entries whose higher-TF
trend disagrees.

Usage: .venv/bin/python scripts/htf_filter_bt.py [--bars 480]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

import scripts.backtest as bt  # noqa: E402
from agent.cmc.client import CMCClient  # noqa: E402
from agent.config import DATA_DIR, load_config  # noqa: E402
from agent.signals.technical import DEFAULT_PARAMS, ema  # noqa: E402
from agent.tokens import TokenRegistry  # noqa: E402


def htf_gates(common: list[str], closes: dict[str, list[float]]):
    """Per-token (ok_4h, ok_daily) boolean arrays per hourly bar — no look-ahead."""
    n = len(common)
    gates = {}
    for tok, series in closes.items():
        # --- 4h: one bucket per 4 bars; trend read from COMPLETED buckets ---
        bucket_end = list(range(3, n, 4))
        s4 = pd.Series([series[i] for i in bucket_end])
        ef, es = ema(s4, 10).tolist(), ema(s4, 30).tolist()
        up4 = [ef[b] > es[b] for b in range(len(s4))]
        ok4 = [(up4[t // 4 - 1] if t // 4 - 1 >= 0 else False) for t in range(n)]
        # --- daily: last close per date; price > PRIOR-day EMA(10) ---
        days, last_close = [], {}
        for i, ts in enumerate(common):
            d = ts[:10]
            if d not in last_close:
                days.append(d)
            last_close[d] = series[i]
        dema = ema(pd.Series([last_close[d] for d in days]), 10).tolist()
        di = {d: i for i, d in enumerate(days)}
        okd = [(series[t] > dema[di[common[t][:10]] - 1]
                if di[common[t][:10]] - 1 >= 0 else False) for t in range(n)]
        gates[tok] = (ok4, okd)
    return gates


def filter_signals(signals, gates, use4h: bool, useday: bool):
    """Suppress entries (conviction -> None) where the chosen higher-TF gate(s)
    disagree. Exit flags are left untouched — never block an exit."""
    out = {}
    for tok, flags in signals.items():
        ok4, okd = gates[tok]
        out[tok] = [((None if ((use4h and not ok4[t]) or (useday and not okd[t])) else entry),
                     ex, em, dr) for t, (entry, ex, em, dr) in enumerate(flags)]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=480)
    args = ap.parse_args()

    cfg = load_config(dry_run=True)
    cmc = CMCClient(cfg.cmc_api_key)
    registry = TokenRegistry()
    tokens = list(cfg.tokens.watchlist)

    print(f"fetching {args.bars} hourly bars x {len(tokens)} ({', '.join(tokens)})...")
    common, closes = bt.fetch_aligned(cmc, registry, tokens, args.bars)
    n = len(common)
    base = bt.precompute_signals(closes, DEFAULT_PARAMS)
    gates = htf_gates(common, closes)

    try:
        liq = json.loads((DATA_DIR / "liquidity_report.json").read_text())
        edge_floor = {r["symbol"]: r["round_trip_cost_pct"] + cfg.risk.edge_floor_margin_pct
                      for r in liq.get("results", [])}
    except Exception:  # noqa: BLE001
        edge_floor = {}

    def run(signals):
        bt.START_EQUITY = 5000.0
        return bt.simulate(
            closes, signals, n, round_trip_pct=1.5,
            min_edge_pct=cfg.risk.min_expected_edge_pct,
            stop_loss_pct=getattr(cfg.risk, "stop_loss_pct", 8.0),
            vol_target=getattr(cfg.risk, "vol_target_pct", 5.0),
            cooldown_bars=int(getattr(cfg.risk, "reentry_cooldown_h", 24)),
            edge_floor=edge_floor)

    variants = [
        ("baseline (1h only)", base),
        ("+ 4h uptrend", filter_signals(base, gates, True, False)),
        ("+ daily EMA10", filter_signals(base, gates, False, True)),
        ("+ both (4h & daily)", filter_signals(base, gates, True, True)),
    ]
    bh = bt.buy_and_hold(closes, 1.5)
    print(f"\nwindow {n} bars {common[0][:10]}..{common[-1][:10]}  "
          f"(cost 1.5% rt, edge>=2%; buy&hold {bh:+.2f}%)\n")
    print(f"{'variant':<24}{'ret%':>8}{'maxDD%':>8}{'trades':>8}{'win%':>7}")
    print("-" * 55)
    rows = []
    for name, sig in variants:
        r = run(sig)
        win = r["win_rate_pct"] if r["win_rate_pct"] is not None else 0.0
        print(f"{name:<24}{r['return_pct']:>+8.2f}{r['max_drawdown_pct']:>8.2f}"
              f"{r['trades_opened']:>8}{win:>7.1f}")
        rows.append({"variant": name, **{k: r[k] for k in
                    ("return_pct", "max_drawdown_pct", "trades_opened", "win_rate_pct")}})
    (DATA_DIR / "htf_filter_report.json").write_text(
        json.dumps({"bars": n, "buy_hold_pct": bh, "rows": rows}, indent=1))
    print("\nNOTE: 20-day window in a broad downtrend — tests whether the filter "
          "avoids bad entries, not upside capture. Re-run across regimes before trusting.")


if __name__ == "__main__":
    main()
