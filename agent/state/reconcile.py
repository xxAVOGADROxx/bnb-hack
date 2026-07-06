"""Reconciliation: rebuild positions from on-chain truth.

Runs at startup and after every restart — internal state is never trusted
over the chain. Token balances are read DIRECTLY on-chain (balanceOf via
Multicall3) for the tradable universe, which is authoritative and
transport-independent; TWAK only supplies the wallet address and the native
BNB balance. (TWAK's REST get_token_holdings relies on the Trust Wallet
indexer, which is empty for an un-indexed wallet — direct balanceOf avoids
that dependency entirely.) USD valuation is on-chain via the execution
client's pricer; the fallback (no pricer, e.g. dry-run without the Pancake
backend) is the free feed's Binance-ticker quotes keyed by the id map.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from agent.chain import Rpc
from agent.config import DATA_DIR, TokensConfig
from agent.market.feed import FeedError, MarketFeed, usd_quote
from agent.tokens import TokenRegistry
from agent.twak.client import AnyTwak, TwakError

log = logging.getLogger(__name__)

ID_MAP_PATH = DATA_DIR / "id_map.json"


@dataclass(frozen=True)
class Portfolio:
    holdings: dict[str, float]        # symbol -> token amount
    usd_values: dict[str, float]      # symbol -> USD value
    total_usd: float

    def open_positions(self, stables: tuple[str, ...]) -> int:
        """Non-stable holdings above dust count as open positions. BNB is the
        native gas reserve, never a traded position — excluding it matches the
        holdings filter in loop.py and stops the gas balance from permanently
        pinning max_concurrent (which froze all entries pre-2026-07-02)."""
        return sum(
            1 for sym, usd in self.usd_values.items()
            if sym not in stables and sym != "BNB" and usd > 1.0
        )


def reconcile(
    twak: AnyTwak, feed: MarketFeed, tokens: TokensConfig,
    registry: TokenRegistry | None = None, rpc: Rpc | None = None,
    pricer=None,
) -> Portfolio:
    try:
        raw = twak.balances()
    except TwakError as e:
        if not twak.dry_run:
            raise  # live mode must fail loud, never trade on unknown positions
        log.warning("dry-run: balances unavailable (%s); using empty portfolio", e)
        raw = {"dry_run": True}
    holdings = _parse_balances(raw)  # native BNB (+ whatever the transport gave)

    # Authoritative on-chain token truth: balanceOf over the tradable universe.
    address = raw.get("address")
    registry = registry or TokenRegistry()
    universe = [
        (s, registry.addresses[s])
        for s in (*tokens.stables, *tokens.watchlist)
        if s in registry.addresses
    ]
    if address and universe:
        try:
            onchain = (rpc or Rpc()).holdings(address, universe)
            for sym, amt in onchain.items():  # on-chain overrides any transport value
                holdings[sym] = amt
        except Exception as e:  # noqa: BLE001 — RPC down must not crash the cycle
            if not twak.dry_run and not any(
                s in holdings for s in tokens.stables
            ):
                # Live + no token balances from any source = blind. Fail loud.
                raise TwakError(f"on-chain balance read failed and no fallback: {e}") from e
            log.warning("on-chain balance read failed (%s); using transport balances", e)

    if pricer is not None:
        usd_values = _value_onchain(holdings, pricer, live=not twak.dry_run)
    else:
        usd_values = _value_holdings(holdings, feed)
    total = sum(usd_values.values())
    log.info("reconciled %d holdings, total $%.2f", len(holdings), total)
    return Portfolio(holdings=holdings, usd_values=usd_values, total_usd=total)


def _value_onchain(holdings: dict[str, float], pricer, live: bool) -> dict[str, float]:
    """Value holdings via the execution venue itself (PancakeSwap spot). Only
    non-zero balances are quoted (one RPC route each). A pricing miss values
    that token at $0 but is logged — and if EVERY held token fails to price in
    live mode we raise rather than report a phantom $0 book (which once tripped
    a false hard-stop): the caller skips the cycle instead of flattening."""
    values: dict[str, float] = {}
    failed: list[str] = []
    for sym, amount in holdings.items():
        if amount <= 0:
            values[sym] = 0.0
            continue
        try:
            price = pricer.price_usd(sym)
        except Exception as e:  # noqa: BLE001 — one bad quote must not blind the rest
            log.warning("on-chain price failed for %s: %s", sym, e)
            price = None
        if price is None:
            failed.append(sym)
            values[sym] = 0.0
        else:
            values[sym] = amount * price
    if live and failed and all(v == 0.0 for v in values.values()):
        raise TwakError(f"on-chain valuation failed for all holdings: {failed}")
    if failed:
        log.warning("no on-chain price for %s — valued at $0", failed)
    return values


def _value_holdings(holdings: dict[str, float], feed: MarketFeed) -> dict[str, float]:
    if not holdings:
        return {}
    id_map: dict = {}
    if ID_MAP_PATH.exists():
        id_map = json.loads(ID_MAP_PATH.read_text())
    ids = {sym: id_map[sym]["id"] for sym in holdings if sym in id_map}
    # Native BNB is gas, never traded (allowlist gates trading) — but it IS
    # capital: value it, or the drawdown ladder and the return % drift from
    # the wallet's real worth. 1839 is BNB's id-map key (feed -> BNBUSDT).
    for sym, cid in {"BNB": 1839}.items():
        if sym in holdings and sym not in ids:
            ids[sym] = cid
    unmapped = [sym for sym in holdings if sym not in ids]
    if unmapped:
        log.warning("no id-map entry for %s — valued at $0", unmapped)
    if not ids:
        return {sym: 0.0 for sym in holdings}
    try:
        quotes = feed.quotes_latest(list(ids.values()))
    except FeedError as e:
        log.warning("valuation unavailable (%s); valuing at $0 this cycle", e)
        return {sym: 0.0 for sym in holdings}
    values = {}
    for sym, amount in holdings.items():
        price = usd_quote(quotes.get(ids.get(sym), {})).get("price") or 0.0
        values[sym] = amount * price
    return values


def _parse_balances(raw: dict) -> dict[str, float]:
    """Shape (verified live, both transports): {chain, address, symbol,
    available, total, totalUsd, tokens: [...]} — native coin at the top
    level with total/available; token entries use `balance` instead
    (CLI `wallet balance`) or total/available (REST holdings)."""
    if raw.get("dry_run"):
        return {}
    holdings: dict[str, float] = {}
    entries = [raw, *(raw.get("tokens") or [])]
    for e in entries:
        sym = e.get("symbol")
        try:
            amount = float(e.get("total") or e.get("available") or e.get("balance") or 0)
        except (TypeError, ValueError):
            log.warning("unparseable balance for %s: %r", sym, e.get("total"))
            continue
        if sym and amount > 0:
            holdings[sym] = holdings.get(sym, 0.0) + amount
    return holdings
