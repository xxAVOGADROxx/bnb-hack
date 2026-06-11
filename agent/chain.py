"""Shared on-chain access: an RPC pool with failover, Multicall3 ABI plumbing,
and batched ERC-20 balanceOf/decimals reads.

Used by reconcile (on-chain truth for our own positions) and the leaderboard
monitor (valuing the whole field). Pure-Python ABI encoding, no web3 dep.
"""
from __future__ import annotations

import json
import logging
import urllib.request

log = logging.getLogger(__name__)

MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
SEL_BALANCE_OF = "70a08231"
SEL_DECIMALS = "313ce567"
SEL_AGGREGATE3 = "82ad56cb"

# Free BSC endpoints verified 2026-06-11; only the first supports wide getLogs
# ranges (the dataseed family caps them hard). Order = preference.
RPC_POOL = (
    "https://bsc-mainnet.nodereal.io/v1/64a9df0874fb4a93b9d0a3849de012d3",
    "https://bnb.api.onfinality.io/public",
    "https://bsc.rpc.blxrbdn.com",
    "https://bsc-dataseed.binance.org",
)


# -- ABI plumbing (pure; tested) ----------------------------------------------
def _word(v: int) -> str:
    return f"{v:064x}"


def encode_aggregate3(calls: list[tuple[str, str]]) -> str:
    """aggregate3((address,bool,bytes)[]) with allowFailure=true per call.
    `calls` = [(target_address, calldata_hex)]."""
    tuples = []
    for target, data in calls:
        d = data[2:] if data.startswith("0x") else data
        padded = d + "0" * ((64 - len(d) % 64) % 64)
        tuples.append(
            _word(int(target, 16)) + _word(1) + _word(0x60)
            + _word(len(d) // 2) + padded
        )
    offsets, pos = [], 32 * len(calls)
    for t in tuples:
        offsets.append(pos)
        pos += len(t) // 2
    array = _word(len(calls)) + "".join(_word(o) for o in offsets) + "".join(tuples)
    return "0x" + SEL_AGGREGATE3 + _word(0x20) + array


def decode_aggregate3(hexstr: str) -> list[int | None]:
    """Decode Result(bool,bytes)[] into uint words (None on failed sub-call
    or empty return — e.g. a token address that is not actually a contract)."""
    b = hexstr[2:] if hexstr.startswith("0x") else hexstr

    def w(i: int) -> int:
        return int(b[i * 64:(i + 1) * 64] or "0", 16)

    base = w(0) // 32
    n = w(base)
    out: list[int | None] = []
    for k in range(n):
        tbase = base + 1 + w(base + 1 + k) // 32
        success, blen = w(tbase) == 1, w(tbase + 2)
        out.append(w(tbase + 3) if success and blen >= 32 else None)
    return out


def _balance_of_calldata(wallet: str) -> str:
    return "0x" + SEL_BALANCE_OF + wallet[2:].lower().rjust(64, "0")


# -- RPC pool with failover ----------------------------------------------------
class Rpc:
    def __init__(self, pool: tuple[str, ...] = RPC_POOL, timeout_s: int = 40):
        self.pool = pool
        self.timeout_s = timeout_s

    def _one(self, url: str, method: str, params: list):
        req = urllib.request.Request(
            url,
            json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
            {"Content-Type": "application/json", "User-Agent": "bnb-hack-agent/0.1"},
        )
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            r = json.load(resp)
        if "result" not in r:
            raise RuntimeError(str(r.get("error"))[:200])
        return r["result"]

    def call(self, method: str, params: list):
        last = None
        for url in self.pool:
            try:
                return self._one(url, method, params)
            except Exception as e:  # noqa: BLE001 — any node failure -> next node
                last = e
                log.debug("rpc %s failed on %s: %s", method, url.split("/")[2], e)
        raise RuntimeError(f"all RPC endpoints failed for {method}: {last}")

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def eth_call(self, to: str, data: str) -> str:
        return self.call("eth_call", [{"to": to, "data": data}, "latest"])

    def get_logs(self, address: str, topic0: str, from_block: int, to_block: int,
                 chunk: int = 50_000) -> list[dict]:
        logs, frm = [], from_block
        while frm <= to_block:
            to = min(frm + chunk - 1, to_block)
            try:
                logs += self.call("eth_getLogs", [{
                    "address": address, "topics": [topic0],
                    "fromBlock": hex(frm), "toBlock": hex(to),
                }])
            except RuntimeError:
                chunk //= 2
                if chunk < 1_000:
                    raise
                continue
            frm = to + 1
        return logs

    # -- ERC-20 reads (batched via Multicall3) --------------------------------
    def decimals(self, tokens: list[tuple[str, str]]) -> dict[str, int]:
        """symbol -> decimals for [(symbol, address)] in one multicall.
        Tokens that fail (not a contract) are omitted."""
        if not tokens:
            return {}
        raw = self.eth_call(MULTICALL3, encode_aggregate3(
            [(a, "0x" + SEL_DECIMALS) for _, a in tokens]))
        out: dict[str, int] = {}
        for (sym, _), dec in zip(tokens, decode_aggregate3(raw)):
            if dec is not None and dec <= 36:
                out[sym] = dec
        return out

    def balances(self, wallet: str, tokens: list[tuple[str, str]],
                 decimals: dict[str, int]) -> dict[str, float]:
        """symbol -> human-readable balance for a wallet across [(symbol,
        address)], one multicall. Zero balances are omitted."""
        tokens = [(s, a) for s, a in tokens if s in decimals]
        if not tokens:
            return {}
        raw = self.eth_call(MULTICALL3, encode_aggregate3(
            [(a, _balance_of_calldata(wallet)) for _, a in tokens]))
        out: dict[str, float] = {}
        for (sym, _), bal in zip(tokens, decode_aggregate3(raw)):
            if bal:
                out[sym] = bal / 10 ** decimals[sym]
        return out

    def holdings(self, wallet: str, tokens: list[tuple[str, str]]) -> dict[str, float]:
        """symbol -> human-readable balance for one wallet, fetching decimals
        and balanceOf together in a SINGLE multicall (no decimals cache). The
        authoritative on-chain truth for reconcile. Zero/failed are omitted."""
        if not tokens:
            return {}
        calls = []
        for _, a in tokens:
            calls.append((a, "0x" + SEL_DECIMALS))
            calls.append((a, _balance_of_calldata(wallet)))
        res = decode_aggregate3(self.eth_call(MULTICALL3, encode_aggregate3(calls)))
        out: dict[str, float] = {}
        for i, (sym, _) in enumerate(tokens):
            dec, bal = res[2 * i], res[2 * i + 1]
            if dec is not None and dec <= 36 and bal:
                out[sym] = bal / 10 ** dec
        return out
