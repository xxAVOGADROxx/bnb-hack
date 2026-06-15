"""Generate dashboard/tokens.json — the PUBLIC eligible-token list (symbol,
BSC address, decimals) the static dashboard uses to read any wallet's holdings.

Decimals are read on-chain once (Multicall3) and baked in, so the browser only
needs balanceOf calls. This is the public competition token universe — NOT the
private watchlist.

Usage: .venv/bin/python scripts/gen_token_list.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.chain import Rpc  # noqa: E402
from agent.config import DATA_DIR, ROOT, load_config  # noqa: E402

OUT = ROOT / "dashboard" / "tokens.json"


def main() -> None:
    cfg = load_config(dry_run=True)
    addrs = json.loads((DATA_DIR / "bsc_addresses.json").read_text())
    eligible = [(s, addrs[s]) for s in cfg.tokens.allowlist if s in addrs]
    rpc = Rpc()
    decs = rpc.decimals(eligible)
    out = []
    for sym, addr in eligible:
        d = decs.get(sym)
        if d is None:
            print(f"  {sym}: no decimals on-chain — skipping")
            continue
        out.append({"symbol": sym, "address": addr, "decimals": int(d)})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=1))
    print(f"wrote {len(out)} tokens -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
