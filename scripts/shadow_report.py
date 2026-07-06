"""Shadow-data report: what did the venue say at each live BUY signal, and
what happened next?

Reads dex_flow / deriv_view records from a decisions.jsonl (the shadow layer
logs one per BUY signal per cycle; repeats of the same signal are collapsed
to unique (token, hour)), then fetches Binance hourly closes and measures
forward returns at +6/+12/+24h from each signal bar. Splits by on-chain flow
(buys/sells 1h ratio) and by the squeeze flag, so the calibration question —
"does venue flow at signal time predict the outcome?" — gets a first, honest,
SMALL-N answer. Horizons that haven't elapsed yet are simply not counted.

Usage: .venv/bin/python scripts/shadow_report.py [path/to/decisions.jsonl]
       (default: data/decisions.jsonl; live file may need docker cp first)
"""
from __future__ import annotations

import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent.config import DATA_DIR  # noqa: E402

BINANCE = "https://data-api.binance.vision"
HORIZONS = (6, 12, 24)


def klines(session, symbol, bars=200):
    r = session.get(f"{BINANCE}/api/v3/klines",
                    {"symbol": f"{symbol}USDT", "interval": "1h",
                     "limit": bars}, timeout=15)
    r.raise_for_status()
    return {int(k[0]) // 3_600_000: float(k[4]) for k in r.json()}


def hour_key(ts_iso: str) -> int:
    dt = datetime.fromisoformat(ts_iso)
    return int(dt.timestamp()) // 3600


def load_signals(path: Path):
    """Unique (token, hour) -> latest dex_flow + deriv_view fields."""
    out: dict[tuple[str, int], dict] = {}
    for line in path.read_text().splitlines():
        if '"dex_flow"' not in line and '"deriv_view"' not in line:
            continue
        r = json.loads(line)
        key = (r["token"], hour_key(r["ts"]))
        out.setdefault(key, {"token": r["token"], "hour": key[1]}).update(
            {k: v for k, v in r.items() if k not in ("ts", "event")})
    return list(out.values())


def fmt_stats(vals):
    if not vals:
        return "        —"
    return (f"n={len(vals):>3} media {statistics.fmean(vals):+.2f}% "
            f"med {statistics.median(vals):+.2f}% "
            f"hit {100 * sum(v > 0 for v in vals) / len(vals):.0f}%")


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DATA_DIR / "decisions.jsonl"
    signals = load_signals(path)
    if not signals:
        sys.exit(f"sin registros sombra en {path}")
    print(f"{len(signals)} señales únicas (token,hora) en {path.name}")

    s = requests.Session()
    price_cache: dict[str, dict[int, float]] = {}
    now_h = int(datetime.now(timezone.utc).timestamp()) // 3600
    rows = []
    for sig in signals:
        tok = sig["token"]
        if tok not in price_cache:
            try:
                price_cache[tok] = klines(s, tok)
            except Exception as e:  # noqa: BLE001
                print(f"  {tok}: sin klines ({e})")
                price_cache[tok] = {}
        px = price_cache[tok]
        base = px.get(sig["hour"])
        if base is None:
            continue
        fwd = {h: (px[sig["hour"] + h] / base - 1) * 100
               for h in HORIZONS
               if sig["hour"] + h <= now_h and sig["hour"] + h in px}
        rows.append({**sig, "fwd": fwd})

    def bucket(name, rows_sel):
        print(f"\n  {name} ({len(rows_sel)} señales)")
        for h in HORIZONS:
            vals = [r["fwd"][h] for r in rows_sel if h in r["fwd"]]
            print(f"    +{h:>2}h  {fmt_stats(vals)}")

    bucket("TODAS", rows)
    with_flow = [r for r in rows if r.get("flow_ratio") is not None]
    bucket("flow_ratio >= 1.5 (presión compradora)",
           [r for r in with_flow if r["flow_ratio"] >= 1.5])
    bucket("flow_ratio 1.0-1.5", [r for r in with_flow if 1.0 <= r["flow_ratio"] < 1.5])
    bucket("flow_ratio < 1.0 (venta neta)", [r for r in with_flow if r["flow_ratio"] < 1.0])
    with_oi = [r for r in rows if r.get("oi_chg_24h_pct") is not None]
    if with_oi:
        bucket("OI 24h cayendo (squeeze-ish)", [r for r in with_oi if r["oi_chg_24h_pct"] < 0])
        bucket("OI 24h subiendo", [r for r in with_oi if r["oi_chg_24h_pct"] >= 0])
    print("\nCAVEAT: n pequeño y ventana corta — esto calibra hipótesis, no las valida.")


if __name__ == "__main__":
    main()
