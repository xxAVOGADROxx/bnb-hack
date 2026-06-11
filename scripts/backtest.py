"""Honest backtest of the live v1 signal rules over CMC historical closes.

Honesty rules (non-negotiable):
- Signals come from agent.signals.technical.evaluate — the EXACT code path
  the live loop runs, not a vectorized re-implementation that could drift.
- Round-trip costs are always applied (1.5% and 2.0% scenarios, half per leg).
- Basis mismatch caveat: these are CEX-aggregated prices; real fills against
  BSC liquidity will be worse. Results are an upper bound.
- Plan limitation: Hobbyist history is capped at ~1 month (720 hourly bars),
  so this validates behavior in the CURRENT regime only — right now a broad
  drawdown, which mostly tests capital preservation, not upside capture.

Simulation mirrors the live loop: entries need a BUY with all conditions,
sized equity * max_position_pct * conviction, max 3 concurrent positions;
exits on the SELL signal. Regime gate is NOT simulated (assumes RISK_ON
scale=1.0 throughout) — flagged in the report.

Usage: .venv/bin/python scripts/backtest.py [--bars 720]
Writes data/backtest_report.json.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.cmc.client import CMCClient, CMCError  # noqa: E402
from agent.config import DATA_DIR, load_config  # noqa: E402
from agent.signals.technical import (  # noqa: E402
    DEFAULT_PARAMS, Action, SignalParams, evaluate,
)
from agent.tokens import TokenRegistry  # noqa: E402

START_EQUITY = 5_000.0
MAX_POSITION_PCT = 25.0
MAX_CONCURRENT = 3
WARMUP = 60

PARAM_GRID: dict[str, SignalParams] = {
    "default(20/50,rsi70)": DEFAULT_PARAMS,
    "fast(12/26,rsi70)": replace(DEFAULT_PARAMS, ema_fast=12, ema_slow=26),
    "rsi65": replace(DEFAULT_PARAMS, rsi_overbought=65.0),
    "rsi75": replace(DEFAULT_PARAMS, rsi_overbought=75.0),
}
COST_SCENARIOS = [1.5, 2.0]  # round-trip %, half applied per leg
MIN_EDGE_SCENARIOS = [0.0, 2.0]  # live risk engine enforces 2.0% (risk.yaml)


def fetch_aligned(cmc: CMCClient, registry: TokenRegistry,
                  tokens: list[str], bars: int) -> tuple[list[str], dict[str, list[float]]]:
    """Fetch hourly series per token and align on common timestamps."""
    series = {}
    for tok in tokens:
        cid = registry.cmc_id(tok)
        if cid is None:
            continue
        try:
            series[tok] = dict(cmc.series_historical(cid, "1h", bars, ttl_s=0))
        except CMCError as e:
            print(f"  {tok}: sin serie ({str(e)[:80]})")
    common = sorted(set.intersection(*(set(s) for s in series.values())))
    aligned = {tok: [s[ts] for ts in common] for tok, s in series.items()}
    return common, aligned


def precompute_signals(closes: dict[str, list[float]], params: SignalParams):
    """For each token and bar: (entry_conviction|None, exit_flag).
    evaluate() is called twice (holding False/True) so entry and exit flags
    come from the identical live code path."""
    out = {}
    for tok, series in closes.items():
        flags = []
        for t in range(len(series)):
            if t < WARMUP:
                flags.append((None, False, 0.0))
                continue
            window = series[: t + 1]
            flat = evaluate(tok, window, holding=False, params=params)
            held = evaluate(tok, window, holding=True, params=params)
            entry = flat.conviction if flat.action == Action.BUY else None
            flags.append((entry, held.action == Action.SELL, flat.expected_move_pct))
        out[tok] = flags
    return out


def simulate(closes: dict[str, list[float]], signals, n_bars: int,
             round_trip_pct: float, min_edge_pct: float = 0.0):
    leg = round_trip_pct / 2 / 100
    cash, positions = START_EQUITY, {}  # tok -> qty
    trades, equity_curve = [], []
    hwm, max_dd = 0.0, 0.0

    for t in range(n_bars):
        # exits first
        for tok in list(positions):
            entry_conv, exit_flag, _ = signals[tok][t]
            if exit_flag:
                px = closes[tok][t]
                proceeds = positions.pop(tok) * px * (1 - leg)
                cash += proceeds
                trades[-_open_idx(trades, tok)]["exit_usd"] = proceeds  # close last open
        equity = cash + sum(q * closes[tok][t] for tok, q in positions.items())
        # entries
        for tok, series in closes.items():
            if tok in positions or len(positions) >= MAX_CONCURRENT:
                continue
            entry_conv, _, expected_move = signals[tok][t]
            if entry_conv is None or expected_move < min_edge_pct:
                continue
            usd = equity * MAX_POSITION_PCT / 100 * entry_conv
            if usd < 10 or usd > cash:
                continue
            px = series[t]
            qty = usd / px * (1 - leg)
            cash -= usd
            positions[tok] = qty
            trades.append({"token": tok, "bar": t, "entry_usd": usd, "exit_usd": None})

        equity = cash + sum(q * closes[tok][t] for tok, q in positions.items())
        equity_curve.append(equity)
        hwm = max(hwm, equity)
        max_dd = max(max_dd, (hwm - equity) / hwm * 100 if hwm else 0.0)

    # liquidate remainder at last bar for final equity
    final = cash + sum(q * closes[tok][-1] * (1 - leg) for tok, q in positions.items())
    closed = [tr for tr in trades if tr["exit_usd"] is not None]
    wins = sum(1 for tr in closed if tr["exit_usd"] > tr["entry_usd"])
    return {
        "round_trip_cost_pct": round_trip_pct,
        "min_edge_pct": min_edge_pct,
        "final_equity": round(final, 2),
        "return_pct": round((final / START_EQUITY - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "trades_opened": len(trades),
        "trades_closed": len(closed),
        "win_rate_pct": round(wins / len(closed) * 100, 1) if closed else None,
        "open_at_end": len(positions),
    }


def _open_idx(trades, tok):
    for i, tr in enumerate(reversed(trades), 1):
        if tr["token"] == tok and tr["exit_usd"] is None:
            return i
    raise RuntimeError("no open trade found")


def buy_and_hold(closes: dict[str, list[float]], round_trip_pct: float) -> float:
    leg = round_trip_pct / 2 / 100
    rets = [series[-1] / series[WARMUP] for series in closes.values()]
    return round(((sum(rets) / len(rets)) * (1 - leg) ** 2 - 1) * 100, 2)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=720)
    args = ap.parse_args()

    cfg = load_config(dry_run=True)
    cmc = CMCClient(cfg.cmc_api_key)
    registry = TokenRegistry()
    tokens = list(cfg.tokens.watchlist)

    print(f"fetching {args.bars} hourly bars x {len(tokens)} tokens...")
    common_ts, closes = fetch_aligned(cmc, registry, tokens, args.bars)
    n = len(common_ts)
    print(f"aligned window: {n} bars ({common_ts[0]} .. {common_ts[-1]})\n")

    results = []
    for name, params in PARAM_GRID.items():
        signals = precompute_signals(closes, params)
        for cost in COST_SCENARIOS:
            for edge in MIN_EDGE_SCENARIOS:
                r = {"params": name, **simulate(closes, signals, n, cost, edge),
                     "benchmark_buyhold_pct": buy_and_hold(closes, cost)}
                results.append(r)
                print(f"{name:<22} cost={cost}% edge>={edge}%  ret={r['return_pct']:>7.2f}%  "
                      f"maxDD={r['max_drawdown_pct']:>5.2f}%  trades={r['trades_closed']:>3}  "
                      f"win={r['win_rate_pct']}%  B&H={r['benchmark_buyhold_pct']}%")

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "backtest_report.json").write_text(json.dumps({
        "window_bars": n, "window_start": common_ts[0], "window_end": common_ts[-1],
        "start_equity": START_EQUITY, "max_position_pct": MAX_POSITION_PCT,
        "max_concurrent": MAX_CONCURRENT,
        "caveats": [
            "CEX-aggregated prices; real BSC fills will be worse",
            "regime gate not simulated (RISK_ON scale=1.0 throughout)",
            "history capped at ~1 month by plan — current regime only",
        ],
        "results": results,
    }, indent=2))
    print("\nreport -> data/backtest_report.json")


if __name__ == "__main__":
    main()
