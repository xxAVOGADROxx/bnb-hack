"""Screen the full 148-token eligible list for upside potential the competition
week, grounded in historical data (no news-prediction — we can't and shouldn't).

Two stages to stay cheap on credits:
  Stage 1 (cheap): one quotes_latest batch -> liquidity (volume_24h, mcap) and
    momentum (7d/30d/90d % change) for ALL eligible tokens. Filters out the
    untradeable-thin and ranks the rest.
  Stage 2 (targeted): pull 365 daily bars for the top candidates and measure the
    UPSIDE TAIL that actually matters for a 1-week PnL sprint: p90/p95 weekly
    forward return, % positive weeks, annualized vol, and distance from the
    1-year high (room to run vs already-extended).

A high upside tail with enough liquidity to exit = a real satellite candidate.
A high tail with thin volume = a trap (you can't get out without giving it back
in slippage — exactly the fee problem we're fighting).

Usage: .venv/bin/python scripts/eligible_screen.py [--top 25] [--min-vol 2e6]
"""
from __future__ import annotations

import argparse
import sys
from statistics import median, pstdev
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.cmc.client import CMCClient, CMCError, usd_quote  # noqa: E402
from agent.config import load_config  # noqa: E402
from agent.tokens import TokenRegistry  # noqa: E402

DAYS, H = 365, 7


def pct(xs, q):
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--min-vol", type=float, default=2e6, help="min 24h volume to be tradeable")
    args = ap.parse_args()

    cfg = load_config(dry_run=True)
    cmc = CMCClient(cfg.cmc_api_key)
    reg = TokenRegistry()
    stables = set(cfg.tokens.stables) | {"USDT", "USDC", "DAI", "USD1", "USDe", "USDD",
                                         "XAUt", "FDUSD", "TUSD", "PYUSD"}
    watch = set(cfg.tokens.watchlist)
    eligible = [t for t in cfg.tokens.allowlist if t not in stables]
    reg.ensure_id_map(cmc, eligible)
    ids: dict[str, int] = {}
    for t in eligible:
        cid = reg.cmc_id(t)
        if cid is not None:
            ids[t] = cid

    # ---- stage 1: liquidity + momentum over ALL eligible ------------------
    rows = []
    id_list = list(ids.values())
    for i in range(0, len(id_list), 100):
        chunk = id_list[i:i + 100]
        try:
            data = cmc.quotes_latest(chunk, ttl_s=300)
        except CMCError as e:
            print(f"quotes batch failed: {e}")
            continue
        for tok, cid in ids.items():
            coin = data.get(cid) or data.get(str(cid))
            if not coin:
                continue
            q = usd_quote(coin)
            rows.append({
                "tok": tok, "cid": cid,
                "vol": float(q.get("volume_24h") or 0),
                "mcap": float(q.get("market_cap") or 0),
                "ch7": float(q.get("percent_change_7d") or 0),
                "ch30": float(q.get("percent_change_30d") or 0),
                "ch90": float(q.get("percent_change_90d") or 0),
            })
    # de-dup (a token only appears once across batches)
    seen, uniq = set(), []
    for r in rows:
        if r["tok"] in seen:
            continue
        seen.add(r["tok"])
        uniq.append(r)
    rows = uniq

    tradeable = [r for r in rows if r["vol"] >= args.min_vol]
    thin = [r for r in rows if r["vol"] < args.min_vol]
    # stage-1 score: recent momentum, tradeable only (7d weighted, 30d context)
    for r in tradeable:
        r["mom"] = 0.6 * r["ch7"] + 0.4 * r["ch30"]
    tradeable.sort(key=lambda r: r["mom"], reverse=True)

    print(f"\neligible non-stable: {len(rows)}  |  tradeable (vol>=${args.min_vol:,.0f}): "
          f"{len(tradeable)}  |  thin/untradeable: {len(thin)}")
    print(f"thin (skip — slippage trap): {', '.join(sorted(r['tok'] for r in thin))}\n")

    print(f"=== stage 1: top {args.top} tradeable by recent momentum ===")
    print(f"  {'tok':<7}{'vol24h':>13}{'mcap':>14}{'7d%':>8}{'30d%':>8}{'90d%':>8}  in-watch")
    cands = tradeable[:args.top]
    for r in cands:
        print(f"  {r['tok']:<7}{r['vol']:>13,.0f}{r['mcap']:>14,.0f}"
              f"{r['ch7']:>7.1f}%{r['ch30']:>7.1f}%{r['ch90']:>7.1f}%"
              f"  {'*' if r['tok'] in watch else ''}")

    # ---- stage 2: upside tail on the top candidates -----------------------
    print(f"\n=== stage 2: upside tail (365 daily bars) for top {len(cands)} ===")
    print(f"  {'tok':<7}{'1y%':>8}{'ann_vol':>8}{'wk_med':>8}{'wk_p90':>9}{'wk_p95':>9}"
          f"{'wk_win%':>8}{'frm_hi':>8}  note")
    scored = []
    for r in cands:
        try:
            ser = cmc.series_historical(r["cid"], "daily", DAYS, ttl_s=0)
        except CMCError:
            continue
        p = [px for _, px in ser]
        if len(p) < H + 30:
            continue
        wk = [p[i + H] / p[i] - 1 for i in range(len(p) - H)]
        daily = [p[i + 1] / p[i] - 1 for i in range(len(p) - 1)]
        ann_vol = pstdev(daily) * (365 ** 0.5) * 100
        hi = max(p)
        frm_hi = (p[-1] / hi - 1) * 100  # how far below the 1y high (negative)
        p90, p95 = pct(wk, 0.9) * 100, pct(wk, 0.95) * 100
        win = sum(1 for x in wk if x > 0) / len(wk) * 100
        note = ("MOONSHOT" if p95 > 25 else "high-beta" if p95 > 12 else "tame")
        scored.append({**r, "ann_vol": ann_vol, "wk_med": median(wk) * 100,
                       "p90": p90, "p95": p95, "win": win, "frm_hi": frm_hi, "note": note})
    # rank by upside tail (p95) among the momentum leaders
    scored.sort(key=lambda r: r["p95"], reverse=True)
    for r in scored:
        print(f"  {r['tok']:<7}{(r['ch90']):>7.0f}%{r['ann_vol']:>7.0f}%"
              f"{r['wk_med']:>7.1f}%{r['p90']:>8.1f}%{r['p95']:>8.1f}%"
              f"{r['win']:>7.0f}%{r['frm_hi']:>7.0f}%  {r['note']}"
              f"{'  <-IN WATCHLIST' if r['tok'] in watch else ''}")


if __name__ == "__main__":
    main()
