"""Token registry: symbol -> CMC id + BSC contract address.

twak's symbol resolver on BSC is unreliable (it resolved TON/AVAX/LTC to
contracts whose quotes diverged wildly from the canonical Binance-Peg
tokens) — every twak call MUST use the BEP-20 contract address. Both maps
are persisted files (data/id_map.json, data/bsc_addresses.json), originally
built from CMC metadata; post-CMC (key removed 2026-07-03) they are
maintained BY HAND: adding a token to the watchlist means adding its id-map
entry (any unique int works — it is just the feed's internal key) and its
BEP-20 contract address, then re-running scripts/liquidity_filter.py.
"""
from __future__ import annotations

import json
import logging

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

    def ensure_id_map(self, _feed, symbols: list[str]) -> None:
        """Post-CMC this no longer fetches anything: it verifies the persisted
        map covers the universe and shouts (once, at boot) if it doesn't. The
        `_feed` param is kept so call sites read the same as before."""
        missing = [s for s in symbols if s not in self.id_map]
        if missing:
            log.error(
                "id map missing %s — add entries to %s by hand "
                "(shape: {\"SYM\": {\"id\": <unique int>, \"name\": ...}})",
                missing, ID_MAP_PATH)

    def ensure_addresses(self, _feed, symbols: list[str]) -> None:
        """Verify every symbol has a BEP-20 contract address persisted;
        execution refuses symbol resolution, so a missing address means the
        token cannot trade until it is added to the file by hand."""
        missing = [s for s in symbols if s not in self.addresses]
        if missing:
            log.error(
                "BSC addresses missing %s — add the BEP-20 contract addresses "
                "to %s by hand (verify on bscscan against the canonical "
                "Binance-Peg contract)", missing, ADDRESSES_PATH)


def _read(path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}
