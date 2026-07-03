"""Watchlist builder: filter eligible-token candidates by REAL PancakeSwap/BSC
execution cost, not CMC rank.

CMC prices are CEX-aggregated; fills happen against on-chain DEX liquidity.
For each candidate we quote a full round-trip at the intended position size
(USDT -> TOKEN with --usd, then TOKEN -> USDT with the exact output amount)
and measure the total cost. Tokens above the cost ceiling are dropped.

Quotes only — no transaction is signed, no funds are needed.

Usage:
  .venv/bin/python scripts/liquidity_filter.py [--size-usd 1500] [--max-cost-pct 1.5]
  .venv/bin/python scripts/liquidity_filter.py --symbols ETH,CAKE,TWT

Writes survivors to config/watchlist.local.yaml (gitignored) and the full
measurement table to data/liquidity_report.json.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import CONFIG_DIR, DATA_DIR, load_config  # noqa: E402
from agent.twak.client import TwakClient, TwakError  # noqa: E402

# Default candidate pool: top-volume eligible tokens (data/candidates.json)
# minus stables/pegged, plus BSC-natives with guaranteed Pancake depth.
DEFAULT_CANDIDATES = [
    "ETH", "XRP", "ZEC", "DOGE", "ADA", "TRX", "AVAX", "LINK", "LTC", "BCH",
    "AAVE", "TON", "UNI", "DOT", "FET", "INJ",
    "CAKE", "TWT", "FLOKI",  # BSC-native
]


def parse_amount(s: str) -> float:
    m = re.match(r"([\d.]+)", s or "")
    if not m:
        raise ValueError(f"cannot parse amount from {s!r}")
    return float(m.group(1))


def build_client():
    """Measure friction with the SAME backend that executes swaps: PancakeSwap
    V3 direct when EXEC_BACKEND=pancake, else TWAK. Keeps the edge floors honest
    — an entry is gated against the cost it will actually pay, not a phantom."""
    if os.environ.get("EXEC_BACKEND", "").lower() == "pancake":
        from agent.execution.pancake import make_pancake_client
        from agent.tokens import TokenRegistry
        twak = TwakClient(chain="bsc", dry_run=True)  # delegate target (unused here)
        return make_pancake_client(twak, TokenRegistry(), chain="bsc", dry_run=True), "pancake"
    return TwakClient(chain="bsc", dry_run=True), "twak"


def measure(client, backend: str, symbol: str, size_usd: float, token_ref: str) -> dict:
    """token_ref: BSC contract address when known (the symbol resolver on BSC
    only covers a handful of tokens), else the symbol."""
    if backend == "pancake":
        return {"symbol": symbol, "size_usd": size_usd,
                **client.measure_round_trip(token_ref, size_usd)}
    leg1 = client.quote("USDT", token_ref, usd=size_usd, slippage_pct=1.0).raw
    token_amount = parse_amount(leg1["output"])
    leg2 = client.quote_amount(token_amount, token_ref, "USDT")
    usd_back = parse_amount(leg2["output"])
    cost_pct = (1 - usd_back / size_usd) * 100
    return {
        "symbol": symbol,
        "size_usd": size_usd,
        "usd_back": round(usd_back, 2),
        "round_trip_cost_pct": round(cost_pct, 3),
        "price_impact_1": leg1.get("priceImpact"),
        "price_impact_2": leg2.get("priceImpact"),
        "provider": leg1.get("provider"),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size-usd", type=float, default=1500.0)
    ap.add_argument("--max-cost-pct", type=float, default=1.5)
    ap.add_argument("--symbols", help="comma-separated override of the candidate pool")
    args = ap.parse_args()

    cfg = load_config(dry_run=True)  # loads .env (TWAK_WALLET_PASSWORD)
    client, backend = build_client()
    print(f"friction backend: {backend}")

    candidates = (
        [s.strip() for s in args.symbols.split(",")] if args.symbols else DEFAULT_CANDIDATES
    )
    bad = [s for s in candidates if s not in cfg.tokens.allowlist]
    if bad:
        raise SystemExit(f"not in the eligible-token allowlist: {bad}")

    addr_path = DATA_DIR / "bsc_addresses.json"
    addresses = json.loads(addr_path.read_text()) if addr_path.exists() else {}

    results, failures = [], []
    for sym in candidates:
        try:
            r = measure(client, backend, sym, args.size_usd, addresses.get(sym) or sym)
            results.append(r)
            print(f"{sym:<8} round-trip {r['round_trip_cost_pct']:>6.2f}%  "
                  f"(${r['usd_back']:.2f} back of ${args.size_usd:.0f})")
        except (TwakError, ValueError, KeyError) as e:
            failures.append({"symbol": sym, "error": str(e)[:300]})
            print(f"{sym:<8} FAILED: {str(e)[:120]}")

    survivors = [r["symbol"] for r in results
                 if r["round_trip_cost_pct"] <= args.max_cost_pct]

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "liquidity_report.json").write_text(
        json.dumps({"size_usd": args.size_usd, "max_cost_pct": args.max_cost_pct,
                    "results": results, "failures": failures}, indent=2)
    )

    out = CONFIG_DIR / "watchlist.local.yaml"
    lines = [
        "# Generated by scripts/liquidity_filter.py — PRIVATE, gitignored.",
        f"# Round-trip quoted at ${args.size_usd:.0f}, ceiling {args.max_cost_pct}%.",
        "watchlist:",
        *[f'  - "{s}"' for s in survivors],
    ]
    out.write_text("\n".join(lines) + "\n")
    print(f"\n{len(survivors)}/{len(candidates)} survived <= {args.max_cost_pct}% "
          f"-> {out.name}; full report in data/liquidity_report.json")


if __name__ == "__main__":
    main()
