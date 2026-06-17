"""Per-service fee breakdown + capital sweep over the LIVE decision logic.

Reuses the exact backtest engine (same signals, same simulate() — no drift) to
get the real trade list over the last N days, then decomposes realized friction
into the services that actually cause it:

  - TWAK swap fee  : per leg, two legs per round-trip. Standard 0.7%/leg vs the
                     competition-week waiver 0.077%/leg.
  - DEX price impact: SIZE-ACCURATE. We re-quote each traded token's round-trip
                     cost at a grid of sizes (live TWAK quotes, no tx, no fee),
                     build a per-token size->cost curve, and read impact at each
                     trade's ACTUAL notional minus the swap-fee portion.
  - BNB gas        : fixed per swap tx (BSC). ESTIMATE, flagged below.
  - CMC x402       : $0.01 per premium tie-break (grey-zone decisions only).

Sweeps starting capital (3k / 5k / 7k USDT). The strategy is %-based, so the
RETURN % is capital-neutral; the sweep exposes how the FIXED costs (gas + CMC)
dilute as a share of capital, and how impact grows with position size.

Usage: .venv/bin/python scripts/fee_sim.py [--bars 480] [--no-remeasure]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.backtest as bt  # noqa: E402  — reuse the live-faithful engine
from scripts.liquidity_filter import measure  # noqa: E402  — same round-trip quote
from agent.cmc.client import CMCClient  # noqa: E402
from agent.config import DATA_DIR, load_config  # noqa: E402
from agent.signals.technical import DEFAULT_PARAMS  # noqa: E402
from agent.tokens import TokenRegistry  # noqa: E402
from agent.twak.client import TwakClient, TwakError  # noqa: E402

# -- service cost model ------------------------------------------------------
SWAP_RATE = {"standard": 0.7, "waiver": 0.077}   # % PER LEG (TWAK)
STD_SWAP_RT = 2 * SWAP_RATE["standard"]          # 1.4% — to back impact out of the measurement
GAS_PER_SWAP_USD = 0.15   # BSC PancakeSwap swap, ~150k gas. ESTIMATE — flagged.
CMC_CALL_USD = 0.01       # x402 premium TA tie-break, per call
CAPITAL_LEVELS = [3_000.0, 5_000.0, 7_000.0]
# Size grid spanning realistic fills (entries are <=25% of capital, shrunk by
# conviction/vol). Round-trip cost is re-quoted at each; impact is interpolated.
SIZES = [250.0, 500.0, 750.0, 1250.0, 1750.0]
CURVE_PATH = DATA_DIR / "friction_by_size.json"


def build_curve(tokens: list[str], remeasure: bool) -> dict[str, dict[float, float]]:
    """Per-token {size_usd: round_trip_cost_pct}. Re-measures live via TWAK
    quotes (no tx, no fee) unless --no-remeasure, which reuses the cached file."""
    if not remeasure and CURVE_PATH.exists():
        raw = json.loads(CURVE_PATH.read_text()).get("round_trip_pct", {})
        return {t: {float(s): v for s, v in m.items()} for t, m in raw.items()}
    twak = TwakClient(chain="bsc", dry_run=True)
    addresses = json.loads((DATA_DIR / "bsc_addresses.json").read_text())
    curve: dict[str, dict[float, float]] = {}
    print(f"re-measuring round-trip cost at sizes {[int(s) for s in SIZES]} "
          f"(live quotes, no tx)...")
    for tok in tokens:
        curve[tok] = {}
        for size in SIZES:
            try:
                r = measure(twak, tok, size, addresses.get(tok) or tok)
                curve[tok][size] = r["round_trip_cost_pct"]
            except (TwakError, ValueError, KeyError) as e:
                print(f"  {tok}@${int(size)} measure failed: {str(e)[:80]}")
        if curve[tok]:
            pts = " ".join(f"${int(s)}:{c:.2f}%" for s, c in sorted(curve[tok].items()))
            print(f"  {tok:<6} {pts}")
    CURVE_PATH.write_text(json.dumps(
        {"sizes_usd": [int(s) for s in SIZES],
         "round_trip_pct": {t: {str(int(s)): v for s, v in m.items()}
                            for t, m in curve.items()}}, indent=1))
    return curve


def interp_rt(curve_tok: dict[float, float], n: float) -> float | None:
    """Round-trip cost % at notional n, linearly interpolated (clamped at ends)."""
    if not curve_tok:
        return None
    pts = sorted(curve_tok.items())
    if n <= pts[0][0]:
        return pts[0][1]
    if n >= pts[-1][0]:
        return pts[-1][1]
    for (s0, c0), (s1, c1) in zip(pts, pts[1:]):
        if s0 <= n <= s1:
            return c0 + (c1 - c0) * (n - s0) / (s1 - s0)
    return pts[-1][1]


def impact_pct(curve: dict[str, dict[float, float]], token: str, n: float,
               fallback_rt: float) -> float:
    """Size-accurate DEX impact % for a trade of notional n: the round-trip
    cost at that size minus the standard swap fee (the non-fee remainder)."""
    rt = interp_rt(curve.get(token, {}), n)
    if rt is None:
        rt = fallback_rt
    return max(0.0, rt - STD_SWAP_RT)


def decompose(trades, curve, regime, avg_rt, cmc_calls):
    """Attribute realized friction to each service for one fee regime."""
    swap_rt_pct = 2 * SWAP_RATE[regime]
    swap_usd = impact_usd = notional = 0.0
    for tr in trades:
        n = tr["entry_usd"]
        notional += n
        swap_usd += swap_rt_pct / 100 * n
        impact_usd += impact_pct(curve, tr["token"], n, avg_rt) / 100 * n
    gas_usd = GAS_PER_SWAP_USD * 2 * len(trades)
    cmc_usd = CMC_CALL_USD * cmc_calls
    return {
        "swap": round(swap_usd, 2), "impact": round(impact_usd, 2),
        "gas": round(gas_usd, 2), "cmc": round(cmc_usd, 2),
        "total": round(swap_usd + impact_usd + gas_usd + cmc_usd, 2),
        "notional": round(notional, 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bars", type=int, default=480, help="hourly bars (~20 days)")
    ap.add_argument("--no-remeasure", action="store_true",
                    help="reuse cached friction_by_size.json instead of re-quoting")
    args = ap.parse_args()

    cfg = load_config(dry_run=True)
    cmc = CMCClient(cfg.cmc_api_key)
    registry = TokenRegistry()
    tokens = list(cfg.tokens.watchlist)

    print(f"fetching {args.bars} hourly bars x {len(tokens)} tokens ({', '.join(tokens)})...")
    common, closes = bt.fetch_aligned(cmc, registry, tokens, args.bars)
    n = len(common)
    signals = bt.precompute_signals(closes, DEFAULT_PARAMS)

    curve = build_curve(tokens, remeasure=not args.no_remeasure)
    # blended fallback for any token missing a curve
    all_rt = [c for m in curve.values() for c in m.values()]
    avg_rt = (sum(all_rt) / len(all_rt)) if all_rt else STD_SWAP_RT
    edge_floor = {t: (interp_rt(m, 750.0) or avg_rt) + cfg.risk.edge_floor_margin_pct
                  for t, m in curve.items()}

    print(f"\nwindow: {n} bars  {common[0][:10]}..{common[-1][:10]}  "
          f"(swap fee: standard {SWAP_RATE['standard']}%/leg, waiver {SWAP_RATE['waiver']}%/leg; "
          f"gas est ${GAS_PER_SWAP_USD}/swap; impact size-accurate)\n")

    hdr = (f"{'capital':>8} {'regime':>9} {'trades':>7} {'avg$pos':>8} {'gross$':>9} "
           f"{'swap$':>8} {'impact$':>8} {'gas$':>7} {'cmc$':>6} "
           f"{'fees$':>8} {'net$':>9} {'net%':>7} {'fees%cap':>9}")
    print(hdr); print("-" * len(hdr))

    summary = []
    for cap in CAPITAL_LEVELS:
        bt.START_EQUITY = cap  # engine sizes off this; return% is capital-neutral
        res = bt.simulate(
            closes, signals, n, round_trip_pct=0.0,        # gross; costs applied below
            min_edge_pct=cfg.risk.min_expected_edge_pct,
            stop_loss_pct=getattr(cfg.risk, "stop_loss_pct", 8.0),
            vol_target=getattr(cfg.risk, "vol_target_pct", 5.0),
            vol_floor=getattr(cfg.risk, "vol_floor", 0.5),
            cooldown_bars=int(getattr(cfg.risk, "reentry_cooldown_h", 24)),
            edge_floor=edge_floor,
        )
        trades = res["trades"]
        gross = sum(tr["exit_usd"] - tr["entry_usd"] for tr in trades)
        avg_pos = (sum(tr["entry_usd"] for tr in trades) / len(trades)) if trades else 0.0
        for regime in ("standard", "waiver"):
            d = decompose(trades, curve, regime, avg_rt, cmc_calls=len(trades))
            net = gross - d["total"]
            print(f"{cap:>8.0f} {regime:>9} {len(trades):>7} {avg_pos:>8.0f} {gross:>+9.2f} "
                  f"{d['swap']:>8.2f} {d['impact']:>8.2f} {d['gas']:>7.2f} {d['cmc']:>6.2f} "
                  f"{d['total']:>8.2f} {net:>+9.2f} {net / cap * 100:>+7.2f} "
                  f"{d['total'] / cap * 100:>8.3f}%")
            summary.append({"capital": cap, "regime": regime, "trades": len(trades),
                            "avg_position_usd": round(avg_pos, 2),
                            "gross_usd": round(gross, 2), **d,
                            "net_usd": round(net, 2), "net_pct": round(net / cap * 100, 3)})
        print()

    out = DATA_DIR / "fee_sim_report.json"
    out.write_text(json.dumps({"bars": n, "tokens": tokens,
                               "swap_rate_pct_per_leg": SWAP_RATE,
                               "gas_per_swap_usd": GAS_PER_SWAP_USD,
                               "impact": "size-accurate (friction_by_size.json)",
                               "rows": summary}, indent=1))
    print(f"report -> {out}   curve -> {CURVE_PATH}")


if __name__ == "__main__":
    main()
