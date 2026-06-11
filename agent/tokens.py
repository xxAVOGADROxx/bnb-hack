"""Token registry: symbol -> CMC id + BSC contract address.

twak's symbol resolver on BSC is unreliable (it resolved TON/AVAX/LTC to
contracts whose quotes diverged wildly from the canonical Binance-Peg
tokens) — every twak call MUST use the BEP-20 contract address. Addresses
come from CMC metadata (/v2/cryptocurrency/info) and are persisted in
data/bsc_addresses.json; ids come from data/id_map.json (built once from
the allowlist).
"""
from __future__ import annotations

import json
import logging

from agent.cmc.client import CMCClient, CMCError
from agent.config import DATA_DIR

log = logging.getLogger(__name__)

ID_MAP_PATH = DATA_DIR / "id_map.json"
ADDRESSES_PATH = DATA_DIR / "bsc_addresses.json"


class TokenRegistry:
    def __init__(self) -> None:
        self.id_map: dict = _read(ID_MAP_PATH)
        self.addresses: dict = _read(ADDRESSES_PATH)

    def cmc_id(self, symbol: str) -> int | None:
        meta = self.id_map.get(symbol)
        return meta["id"] if meta else None

    def execution_ref(self, symbol: str) -> str:
        """What we hand to twak: the BSC contract address, never the symbol
        (except as a last resort, logged loudly)."""
        addr = self.addresses.get(symbol)
        if addr:
            return addr
        log.warning("no BSC address for %s — falling back to symbol resolution", symbol)
        return symbol

    def ensure_addresses(self, cmc: CMCClient, symbols: list[str]) -> None:
        """Fetch and persist BSC contract addresses for any symbol missing one."""
        missing = [s for s in symbols if s not in self.addresses and s in self.id_map]
        if not missing:
            return
        ids = {s: self.id_map[s]["id"] for s in missing}
        try:
            data = cmc._get(
                "/v2/cryptocurrency/info", {"id": ",".join(map(str, ids.values()))}
            )
        except CMCError as e:
            log.warning("could not fetch contract addresses for %s: %s", missing, e)
            return
        for sym, cid in ids.items():
            entry = data.get(str(cid)) or data.get(cid) or {}
            if isinstance(entry, list):
                entry = entry[0] if entry else {}
            addr = _bsc_address(entry)
            if addr:
                self.addresses[sym] = addr
            else:
                log.warning("CMC metadata has no BSC contract for %s (id %s)", sym, cid)
        ADDRESSES_PATH.parent.mkdir(parents=True, exist_ok=True)
        ADDRESSES_PATH.write_text(json.dumps(self.addresses, indent=2))


def _bsc_address(info_entry: dict) -> str | None:
    for ca in info_entry.get("contract_address", []):
        plat = ca.get("platform") or {}
        coin = plat.get("coin") or {}
        name = (plat.get("name") or "").lower()
        if "bnb" in name or "bsc" in name or coin.get("symbol") == "BNB":
            return ca.get("contract_address")
    return None


def _read(path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}
