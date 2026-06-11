"""Competition leaderboard monitor (read-only, runs apart from the loop).

Replicates the official scoring approximately, for the whole field:
  participants  <- Registered(address) events on the competition contract
  holdings      <- balanceOf() of every eligible BSC token, via Multicall3
  prices        <- one batched CMC quotes call (same credits we already pay)
  return %      <- vs each wallet's baseline at the first snapshot of the
                   trading window, recomputed per refresh (sub-$1 flagged)

Strictly read-only: no keys, no twak, no writes outside data/. If this dies,
trading never notices. Honest caveat: it will not match the official board
exactly (price timing, simulated costs we do not know) — it only needs to say
approximately whether we are ahead or behind, which is all the §0 risk
policy consumes: ahead -> protect; behind -> higher-conviction setups only,
NEVER more size or frequency.

Contract surface (recovered from bytecode + live probing, no published ABI):
  register() / isRegistered(address) / registrationStart() /
  registrationDeadline() / event Registered(address indexed) — no list
  getter, hence the event scan.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from agent.chain import (  # shared on-chain primitives (also re-exported for tests)
    MULTICALL3, RPC_POOL, SEL_BALANCE_OF, SEL_DECIMALS, Rpc,
    decode_aggregate3, encode_aggregate3,
)
from agent.cmc.client import CMCClient, CMCError, usd_quote
from agent.config import DATA_DIR

log = logging.getLogger(__name__)

COMPETITION_CONTRACT = "0x212c61b9b72c95d95bf29cf032f5e5635629aed5"
DEPLOY_BLOCK = 101_943_185          # ~1h before registrationStart (2026-06-02)
REGISTERED_TOPIC = "0x2d3734a8e47ac8316e500ac231c90a6e1848ca2285f40d07eaa52005e4b3a0e9"
WINDOW_START_UTC = datetime(2026, 6, 22, 0, 0, tzinfo=timezone.utc)

SEL_IS_REGISTERED = "c3c5a547"
SEL_REG_DEADLINE = "11184392"

STATE_PATH = DATA_DIR / "leaderboard_state.json"
BOARD_PATH = DATA_DIR / "leaderboard.json"
SNAPSHOTS_PATH = DATA_DIR / "leaderboard_snapshots.jsonl"


# -- scoring helpers (pure; tested) -------------------------------------------
def return_pct(current_usd: float, baseline_usd: float | None) -> float | None:
    if not baseline_usd or baseline_usd <= 0:
        return None
    return (current_usd / baseline_usd - 1) * 100


def posture(our_return: float | None, field: list[float]) -> str:
    """§0 policy: ahead -> protect; behind -> conviction, never size."""
    if our_return is None or not field:
        return "no baseline yet — neutral"
    rank = 1 + sum(1 for r in field if r > our_return)
    if rank <= max(1, len(field) // 4):
        return f"rank {rank}/{len(field)}: AHEAD — protect, smaller sizes, cheap compliance trades"
    return (f"rank {rank}/{len(field)}: BEHIND — higher-conviction setups only, "
            f"NEVER more size/frequency (martingale = DQ)")


# -- the monitor ----------------------------------------------------------------
@dataclass(frozen=True)
class Standing:
    wallet: str
    usd: float
    ret_pct: float | None
    sub_dollar: bool
    is_us: bool


class LeaderboardMonitor:
    def __init__(self, cmc: CMCClient, registry, allowlist: tuple[str, ...],
                 our_wallet: str = "", rpc: Rpc | None = None):
        self.cmc = cmc
        self.registry = registry
        self.allowlist = allowlist
        self.our_wallet = our_wallet.lower()
        self.rpc = rpc or Rpc()
        self.state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {
            "participants": [], "last_block": DEPLOY_BLOCK, "baselines": {}, "decimals": {},
        }

    def _save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(self.state, indent=2))

    # participants — incremental event scan
    def refresh_participants(self) -> list[str]:
        head = self.rpc.block_number()
        logs = self.rpc.get_logs(
            COMPETITION_CONTRACT, REGISTERED_TOPIC, self.state["last_block"] + 1, head
        )
        known = set(self.state["participants"])
        for entry in logs:
            known.add("0x" + entry["topics"][1][-40:].lower())
        self.state["participants"] = sorted(known)
        self.state["last_block"] = head
        self._save()
        return self.state["participants"]

    # valuation universe: eligible tokens that exist as BSC contracts
    def _universe(self) -> list[tuple[str, str]]:
        # Self-bootstrap on a fresh clone: id map first (the agent only builds
        # ids for its watchlist; the board values the whole allowlist).
        self.registry.ensure_id_map(self.cmc, list(self.allowlist))
        self.registry.ensure_addresses(self.cmc, list(self.allowlist))
        return [(s, self.registry.addresses[s]) for s in self.allowlist
                if s in self.registry.addresses]

    def _decimals(self, tokens: list[tuple[str, str]]) -> dict[str, int]:
        cached = self.state["decimals"]
        missing = [(s, a) for s, a in tokens if s not in cached]
        if missing:
            raw = self.rpc.eth_call(MULTICALL3, encode_aggregate3(
                [(a, "0x" + SEL_DECIMALS) for _, a in missing]))
            for (sym, _), dec in zip(missing, decode_aggregate3(raw)):
                if dec is not None and dec <= 36:
                    cached[sym] = dec
            self._save()
        return cached

    def _prices(self, symbols: list[str]) -> dict[str, float]:
        ids = {s: self.registry.cmc_id(s) for s in symbols}
        id_list = [i for i in ids.values() if i]
        data = self.cmc.quotes_latest(id_list, ttl_s=120)
        prices = {}
        for sym, cid in ids.items():
            entry = data.get(str(cid)) or data.get(cid)
            if isinstance(entry, list):
                entry = entry[0] if entry else None
            if entry:
                try:
                    prices[sym] = float(usd_quote(entry)["price"])
                except (CMCError, KeyError, TypeError, ValueError):
                    pass
        return prices

    def value_wallets(self, wallets: list[str]) -> dict[str, float]:
        """USD value of eligible holdings per wallet (one multicall per
        wallet batch; 148 tokens x N wallets, allowFailure on every call)."""
        tokens = self._universe()
        decimals = self._decimals(tokens)
        tokens = [(s, a) for s, a in tokens if s in decimals]
        prices = self._prices([s for s, _ in tokens])

        values: dict[str, float] = {}
        per_batch = max(1, 600 // max(1, len(tokens))) if len(tokens) < 600 else 1
        for i in range(0, len(wallets), per_batch):
            batch = wallets[i:i + per_batch]
            calls = [(addr, "0x" + SEL_BALANCE_OF + w[2:].lower().rjust(64, "0"))
                     for w in batch for _, addr in tokens]
            results = decode_aggregate3(self.rpc.eth_call(MULTICALL3, encode_aggregate3(calls)))
            for j, w in enumerate(batch):
                total = 0.0
                for (sym, _), bal in zip(tokens, results[j * len(tokens):(j + 1) * len(tokens)]):
                    if bal:
                        total += bal / 10 ** decimals[sym] * prices.get(sym, 0.0)
                values[w] = total
        return values

    def refresh(self, now: datetime | None = None) -> list[Standing]:
        now = now or datetime.now(timezone.utc)
        wallets = self.refresh_participants()
        values = self.value_wallets(wallets)

        # Baseline = first observation at/after hour 0 of the trading window.
        if now >= WINDOW_START_UTC:
            for w, usd in values.items():
                self.state["baselines"].setdefault(w, {"ts": now.isoformat(), "usd": usd})
            self._save()

        board = sorted(
            (Standing(
                wallet=w, usd=usd,
                ret_pct=return_pct(usd, (self.state["baselines"].get(w) or {}).get("usd")),
                sub_dollar=usd <= 1.0,
                is_us=w == self.our_wallet,
            ) for w, usd in values.items()),
            key=lambda s: (s.ret_pct is None, -(s.ret_pct or 0), -s.usd),
        )

        BOARD_PATH.write_text(json.dumps(
            {"ts": now.isoformat(), "board": [s.__dict__ for s in board]}, indent=2))
        with open(SNAPSHOTS_PATH, "a", encoding="utf-8") as f:
            for s in board:
                f.write(json.dumps({"ts": now.isoformat(), **s.__dict__}) + "\n")
        return board
