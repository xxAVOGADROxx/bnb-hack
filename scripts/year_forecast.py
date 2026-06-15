"""1-year (daily) per-token analysis + an empirical forecast for the
competition week (Jun 22-28), conditioned on the current Fear regime.

Daily history reaches a full year on this plan (intraday is capped at 1 month),
so this is the ONLY long-horizon view we can build honestly.

Three parts:
  1. Per-token characterization over 365 daily bars (return, vol, drawdown,
     distribution of 7-day forward returns, % positive weeks).
  2. Regime conditioning: the equal-weight basket's 7-day-forward return on
     FEAR days (F&G <= 25, like today's 23) vs all days — the "water we swim
     in" during a week that looks like now.
  3. Our actual signal run on the 365 daily bars (same evaluate() code path,
     slower 20/50-DAY trend) as a longer-horizon sanity check on fee drag.

Usage: .venv/bin/python scripts/year_forecast.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, median, pstdev

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.cmc.client import CMCClient, CMCError  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.signals.technical import DEFAULT_PARAMS  # noqa: E402
from agent.tokens import TokenRegistry  # noqa: E402
from scripts.backtest import precompute_signals, simulate  # noqa: E402

DAYS = 365
H = 7  # forward horizon (competition week length)


def fetch_daily(cmc, registry, tokens):
    series = {}
    for tok in tokens:
        cid = registry.cmc_id(tok)
        if cid is None:
            continue
        try:
            series[tok] = dict(cmc.series_historical(cid, "daily", DAYS, ttl_s=0))
        except CMCError:
            continue
    common = sorted(set.intersection(*(set(s) for s in series.values())))
    closes = {t: [series[t][ts] for ts in common] for t in series}
    return [c[:10] for c in common], closes


def fwd_returns(prices, h):
    return [prices[i + h] / prices[i] - 1 for i in range(len(prices) - h)]


def max_dd(prices):
    hwm, dd = prices[0], 0.0
    for p in prices:
        hwm = max(hwm, p)
        dd = max(dd, (hwm - p) / hwm)
    return dd * 100


def pct(xs, q):
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def main():
    cfg = load_config(dry_run=True)
    cmc = CMCClient(cfg.cmc_api_key)
    registry = TokenRegistry()
    tokens = list(cfg.tokens.watchlist)
    registry.ensure_id_map(cmc, tokens)
    dates, closes = fetch_daily(cmc, registry, tokens)
    n = len(dates)
    print(f"1-year daily window: {dates[0]} .. {dates[-1]} ({n} days, {len(closes)} tokens)\n")

    # ---- 1. per-token characterization ------------------------------------
    print("=== per-token, trailing 1 year (daily) ===")
    print(f"  {'tok':<6}{'1y_ret':>9}{'ann_vol':>9}{'maxDD':>8}"
          f"{'wk_med':>9}{'wk_p10':>9}{'wk_p90':>9}{'wk_win%':>9}")
    basket_fwd_by_day = [[] for _ in range(n - H)]
    for tok in sorted(closes):
        p = closes[tok]
        ret1y = (p[-1] / p[0] - 1) * 100
        daily_ret = [p[i + 1] / p[i] - 1 for i in range(len(p) - 1)]
        ann_vol = pstdev(daily_ret) * (365 ** 0.5) * 100
        wk = fwd_returns(p, H)
        for i, r in enumerate(wk):
            basket_fwd_by_day[i].append(r)
        win = sum(1 for r in wk if r > 0) / len(wk) * 100
        print(f"  {tok:<6}{ret1y:>8.1f}%{ann_vol:>8.0f}%{max_dd(p):>7.0f}%"
              f"{median(wk)*100:>8.2f}%{pct(wk,0.1)*100:>8.2f}%{pct(wk,0.9)*100:>8.2f}%"
              f"{win:>8.0f}%")

    basket_wk = [mean(day) for day in basket_fwd_by_day if day]

    # ---- 2. regime conditioning -------------------------------------------
    try:
        raw = cmc._get("/v3/fear-and-greed/historical", {"limit": DAYS})
        fg = {datetime.fromtimestamp(int(x["timestamp"]), tz=timezone.utc).date().isoformat():
              float(x["value"]) for x in raw}
    except CMCError:
        fg = {}
    fear_wk = [basket_wk[i] for i in range(len(basket_wk))
               if fg.get(dates[i], 50) <= 25]
    print("\n=== equal-weight basket: 7-day forward return distribution ===")
    print(f"  all weeks    (n={len(basket_wk):>3}): "
          f"median {median(basket_wk)*100:+.2f}%  mean {mean(basket_wk)*100:+.2f}%  "
          f"p10 {pct(basket_wk,0.1)*100:+.2f}%  p90 {pct(basket_wk,0.9)*100:+.2f}%  "
          f"win {sum(1 for r in basket_wk if r>0)/len(basket_wk)*100:.0f}%")
    if fear_wk:
        print(f"  FEAR weeks   (n={len(fear_wk):>3}): "
              f"median {median(fear_wk)*100:+.2f}%  mean {mean(fear_wk)*100:+.2f}%  "
              f"p10 {pct(fear_wk,0.1)*100:+.2f}%  p90 {pct(fear_wk,0.9)*100:+.2f}%  "
              f"win {sum(1 for r in fear_wk if r>0)/len(fear_wk)*100:.0f}%")
        print("  (FEAR = F&G<=25; today's reading is 23, so this row is the relevant one)")

    # ---- 3. our signal on daily bars (longer-horizon fee-drag check) -------
    sigs = precompute_signals(closes, DEFAULT_PARAMS)
    print("\n=== our signal on 365 DAILY bars (20/50-DAY trend, slower, fewer trades) ===")
    print("  (caveat: params are tuned for 1h; this is a regime sanity check, not the live config)")
    for label, kw in (("edge2 only", dict(min_edge_pct=2.0)),
                      ("live (stop8+vol5)", dict(min_edge_pct=2.0, stop_loss_pct=8, vol_target=5))):
        for cost in (1.5, 3.0):  # 3.0% = realistic worse BSC fills
            r = simulate(closes, sigs, n, cost, **kw)
            print(f"  {label:<20} cost={cost}%  ret={r['return_pct']:>7.2f}%  "
                  f"maxDD={r['max_drawdown_pct']:>5.2f}%  trades={r['trades_closed']:>3}  "
                  f"win={r['win_rate_pct']}%")


if __name__ == "__main__":
    main()
