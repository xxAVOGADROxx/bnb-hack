"""Backtest: does the short-squeeze fingerprint (price up + OI down) predict
forward returns on our watchlist? Free data only — OKX 1H open-interest
history (~30d) aligned with Binance hourly closes.

Event definitions at bar t (24h lookbacks, events de-overlapped by 6h):
  SQUEEZE   px_chg_24h >= +PX%  and  oi_chg_24h <= -OI%   (shorts forced out)
  LONGBUILD px_chg_24h >= +PX%  and  oi_chg_24h >= +OI%   (fresh longs piling)
  PX-ONLY   px_chg_24h >= +PX%                            (control)
  CASCADE   px_chg_24h <= -PX%  and  oi_chg_24h <= -OI%   (longs liquidated)

For each: mean/median forward return at +6h/+12h/+24h and hit rate, vs the
all-bars baseline. SQUEEZE beating PX-ONLY = the OI leg adds information
(entry boost candidate); CASCADE strongly negative = veto candidate.

Usage: .venv/bin/python scripts/squeeze_bt.py [--px 2.0] [--oi 2.0] [--days 30]

VERDICT 2026-07-06 (30d, 13 tokens, 720h each; run at px2/oi2, px3/oi3, px2/oi4):
  - SQUEEZE beats PX-ONLY at the +24h horizon in 2 of 3 variants (px2/oi2:
    median +0.62% vs +0.29%, hit 59% vs 53%, n=98; px3/oi3: +0.71%/62%, n=50)
    but the edge is thin at +6/12h, n is small and it is not monotonic in the
    OI cut (px2/oi4 degrades). Directionally real, NOT strong enough to gate
    sizing for a sleeve whose holds are usually < 24h.
  - CASCADE (long-liquidation veto) REJECTED: cascades slightly BOUNCE at +6h
    (px3/oi3: +0.54% mean, 63% hit) — never turn this into an entry veto.
  => deriv_view stays SHADOW-ONLY. Re-run with more history before revisiting.
"""
from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.config import load_config  # noqa: E402

OKX = "https://www.okx.com"
BINANCE = "https://data-api.binance.vision"
HORIZONS = (6, 12, 24)
GAP_H = 6  # de-overlap: ignore a signal within this many hours of the last


def okx_oi_1h(session: requests.Session, inst_id: str, bars: int) -> dict[int, float]:
    """{hour_ts_ms: oi_usd} via paginated 1H OI history (newest first)."""
    out: dict[int, float] = {}
    end = ""
    for _ in range(bars // 90 + 2):
        params = {"instId": inst_id, "period": "1H", "limit": "100"}
        if end:
            params["end"] = end
        r = session.get(f"{OKX}/api/v5/rubik/stat/contracts/open-interest-history",
                        params=params, timeout=15)
        r.raise_for_status()
        rows = r.json().get("data") or []
        if not rows:
            break
        for row in rows:
            out[int(row[0])] = float(row[-1])
        end = rows[-1][0]  # oldest ts of this page -> next page older
        if len(out) >= bars:
            break
        time.sleep(0.25)  # public rate limit headroom
    return out


def binance_closes(session: requests.Session, symbol: str, bars: int) -> dict[int, float]:
    r = session.get(f"{BINANCE}/api/v3/klines",
                    {"symbol": f"{symbol}USDT", "interval": "1h",
                     "limit": min(bars, 1000)}, timeout=15)
    r.raise_for_status()
    return {int(k[0]): float(k[4]) for k in r.json()}


def fwd_returns(closes: list[float], i: int) -> dict[int, float] | None:
    if i + max(HORIZONS) >= len(closes):
        return None
    return {h: (closes[i + h] / closes[i] - 1) * 100 for h in HORIZONS}


def collect(tokens: list[str], px_cut: float, oi_cut: float, days: int):
    s = requests.Session()
    bars = days * 24
    events: dict[str, list[dict[int, float]]] = {
        "SQUEEZE": [], "LONGBUILD": [], "PX-ONLY": [], "CASCADE": [], "ALL": []}
    covered = []
    for tok in tokens:
        inst = f"{tok}-USDT-SWAP"
        try:
            oi = okx_oi_1h(s, inst, bars)
            if len(oi) < 100:
                print(f"  {tok}: sin OI OKX — omitido")
                continue
            px = binance_closes(s, tok, bars)
        except Exception as e:  # noqa: BLE001
            print(f"  {tok}: error de datos ({e}) — omitido")
            continue
        ts = sorted(set(oi) & set(px))
        closes = [px[t] for t in ts]
        ois = [oi[t] for t in ts]
        covered.append(f"{tok}({len(ts)}h)")
        last_sig = {k: -10**9 for k in events}
        for i in range(24, len(ts)):
            fwd = fwd_returns(closes, i)
            if fwd is None:
                continue
            events["ALL"].append(fwd)
            px_chg = (closes[i] / closes[i - 24] - 1) * 100
            oi_chg = (ois[i] / ois[i - 24] - 1) * 100 if ois[i - 24] > 0 else 0.0
            hits = {
                "SQUEEZE": px_chg >= px_cut and oi_chg <= -oi_cut,
                "LONGBUILD": px_chg >= px_cut and oi_chg >= oi_cut,
                "PX-ONLY": px_chg >= px_cut,
                "CASCADE": px_chg <= -px_cut and oi_chg <= -oi_cut,
            }
            for name, hit in hits.items():
                if hit and i - last_sig[name] >= GAP_H:
                    events[name].append(fwd)
                    last_sig[name] = i
    print("cobertura:", ", ".join(covered) or "ninguna")
    return events


def report(events) -> None:
    print(f"\n{'evento':10} {'n':>5}", end="")
    for h in HORIZONS:
        print(f" | {'+%dh' % h:>6} med {'hit%':>5}", end="")
    print()
    for name in ("ALL", "PX-ONLY", "SQUEEZE", "LONGBUILD", "CASCADE"):
        rows = events[name]
        print(f"{name:10} {len(rows):>5}", end="")
        for h in HORIZONS:
            vals = [r[h] for r in rows]
            if not vals:
                print(" | " + " " * 18, end="")
                continue
            mean = statistics.fmean(vals)
            med = statistics.median(vals)
            hit = 100 * sum(v > 0 for v in vals) / len(vals)
            print(f" | {mean:+6.2f} {med:+5.2f} {hit:5.0f}", end="")
        print()
    print("\nlectura: SQUEEZE > PX-ONLY -> la pata de OI añade señal (boost);")
    print("         CASCADE << ALL    -> veto de cuchillos cayendo con base.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--px", type=float, default=2.0)
    ap.add_argument("--oi", type=float, default=2.0)
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    cfg = load_config(dry_run=True)
    print(f"squeeze_bt: px>={args.px}% oi<=-{args.oi}% dias={args.days} "
          f"gap={GAP_H}h horizontes={HORIZONS}")
    report(collect(list(cfg.tokens.watchlist), args.px, args.oi, args.days))


if __name__ == "__main__":
    main()
