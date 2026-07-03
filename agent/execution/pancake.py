"""Direct PancakeSwap V3 execution — a drop-in replacement for the TWAK
quote/swap surface that removes the aggregator markup.

Why: every TWAK round-trip measured ~1.7% with price_impact 0 — pure fee, the
full standard rate, because the announced waiver never reached our 0x/LiquidMesh
routes. The same swaps priced directly against PancakeSwap V3 pools cost
~0.1-0.9% round-trip (LP fee only). That ~1% per round-trip is the difference
between the momentum bot's edge surviving friction or not.

Scope: this class overrides ONLY quote() and swap(); balances, competition
status and x402 all delegate to the wrapped TWAK client (`__getattr__`), so it
is a true drop-in at the single construction site in agent/loop.py. Signing is
local: the agent mnemonic is decrypted from the TWAK keystore (same scheme as
~/.twak/reveal-mnemonic.js) and never leaves the process.

Routing: for each swap we probe QuoterV2 across the V3 fee tiers for a direct
pool and for a 2-hop route via WBNB, and take the best output. Slippage is
enforced on-chain via amountOutMinimum; amountIn is capped at the real
balanceOf so an exit can never revert on "exceeds balance". Every real swap is
simulated with eth_call first and aborts before broadcast if it would revert.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from Crypto.Cipher import AES
from Crypto.Hash import SHA256
from Crypto.Protocol.KDF import PBKDF2
from eth_account import Account
from web3 import Web3

from agent.twak.client import Quote, TwakError

log = logging.getLogger(__name__)

# -- BSC / PancakeSwap V3 constants (all verified on-chain: QuoterV2.factory()
#    == FACTORY_V3 and QuoterV2.WETH9() == WBNB; see scratchpad/prove_quotes.py).
SMART_ROUTER = Web3.to_checksum_address("0x13f4EA83D0bd40E75C8222255bc855a974568Dd4")
QUOTER_V2 = Web3.to_checksum_address("0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997")
FACTORY_V3 = Web3.to_checksum_address("0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865")
WBNB = Web3.to_checksum_address("0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c")
STABLES = {
    "USDT": Web3.to_checksum_address("0x55d398326f99059fF775485246999027B3197955"),
    "USDC": Web3.to_checksum_address("0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"),
}
FEE_TIERS = (100, 500, 2500, 10000)
ZERO_ADDR = "0x" + "0" * 40
MAX_UINT = 2**256 - 1
CHAIN_ID = 56
HD_PATH = "m/44'/60'/0'/0/0"

DEFAULT_RPCS = (
    "https://bsc-dataseed.bnbchain.org",
    "https://bsc-dataseed1.binance.org",
    "https://bsc-dataseed2.binance.org",
    "https://rpc.ankr.com/bsc",
)

_QUOTER_ABI = json.loads("""[
 {"inputs":[],"name":"factory","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
 {"inputs":[],"name":"WETH9","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"},
 {"inputs":[{"components":[{"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},{"name":"amountIn","type":"uint256"},{"name":"fee","type":"uint24"},{"name":"sqrtPriceLimitX96","type":"uint160"}],"name":"params","type":"tuple"}],"name":"quoteExactInputSingle","outputs":[{"name":"amountOut","type":"uint256"},{"name":"sqrtPriceX96After","type":"uint160"},{"name":"initializedTicksCrossed","type":"uint32"},{"name":"gasEstimate","type":"uint256"}],"stateMutability":"nonpayable","type":"function"}
]""")
_FACTORY_ABI = json.loads(
    '[{"inputs":[{"type":"address"},{"type":"address"},{"type":"uint24"}],'
    '"name":"getPool","outputs":[{"type":"address"}],"stateMutability":"view","type":"function"}]')
_ERC20_ABI = json.loads("""[
 {"inputs":[],"name":"decimals","outputs":[{"type":"uint8"}],"stateMutability":"view","type":"function"},
 {"inputs":[{"type":"address"}],"name":"balanceOf","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
 {"inputs":[{"name":"owner","type":"address"},{"name":"spender","type":"address"}],"name":"allowance","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
 {"inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],"name":"approve","outputs":[{"type":"bool"}],"stateMutability":"nonpayable","type":"function"}
]""")
# PancakeSwap SmartRouter V3SwapRouter surface (exact-input, recipient in struct,
# no deadline field — the SmartRouter is deadline-less at the function level).
_ROUTER_ABI = json.loads("""[
 {"inputs":[{"components":[{"name":"tokenIn","type":"address"},{"name":"tokenOut","type":"address"},{"name":"fee","type":"uint24"},{"name":"recipient","type":"address"},{"name":"amountIn","type":"uint256"},{"name":"amountOutMinimum","type":"uint256"},{"name":"sqrtPriceLimitX96","type":"uint160"}],"name":"params","type":"tuple"}],"name":"exactInputSingle","outputs":[{"name":"amountOut","type":"uint256"}],"stateMutability":"payable","type":"function"},
 {"inputs":[{"components":[{"name":"path","type":"bytes"},{"name":"recipient","type":"address"},{"name":"amountIn","type":"uint256"},{"name":"amountOutMinimum","type":"uint256"}],"name":"params","type":"tuple"}],"name":"exactInput","outputs":[{"name":"amountOut","type":"uint256"}],"stateMutability":"payable","type":"function"}
]""")


class PancakeError(TwakError):
    """Raised for PancakeSwap execution failures. Subclasses TwakError so the
    Executor's existing `except TwakError` path logs and alerts unchanged."""


def _decrypt_mnemonic(keystore: Path, password: str) -> str:
    """Decrypt the agent mnemonic — PBKDF2-SHA256(600k, 32B) + AES-256-GCM,
    hex — matching ~/.twak/reveal-mnemonic.js exactly."""
    d = json.loads(keystore.read_text())
    key = PBKDF2(password, bytes.fromhex(d["salt"]), dkLen=32, count=600_000,
                 hmac_hash_module=SHA256)
    cipher = AES.new(key, AES.MODE_GCM, nonce=bytes.fromhex(d["iv"]))
    pt = cipher.decrypt_and_verify(
        bytes.fromhex(d["encryptedMnemonic"]), bytes.fromhex(d["authTag"]))
    return pt.decode()


class PancakeClient:
    """Drop-in for TwakClient/TwakRestClient: overrides quote()/swap() with
    direct PancakeSwap V3 execution; delegates every other attribute (balances,
    compete_*, x402_request, dry_run reads on the wrapped client, ...) to the
    wrapped TWAK client."""

    def __init__(self, twak, registry, *, rpc_urls=DEFAULT_RPCS, keystore=None,
                 password=None, chain="bsc", dry_run=True, timeout_s=180):
        self._twak = twak  # MUST be first: __getattr__ delegates to it
        self.registry = registry
        self.chain = chain
        self.dry_run = dry_run
        self.timeout_s = timeout_s
        self._rpc_urls = tuple(rpc_urls)
        self._keystore = Path(keystore or os.path.expanduser("~/.twak/wallet.json"))
        self._password = password if password is not None else os.environ.get(
            "TWAK_WALLET_PASSWORD", "")
        self._w3 = None
        self._acct = None
        self._dec_cache: dict[str, int] = {}

    def __getattr__(self, name):
        # Only fires for attributes not set on self — delegate to TWAK for the
        # read/compete/x402 surface we don't override. _twak is always set first
        # in __init__, so this cannot recurse on a normal instance.
        return getattr(self._twak, name)

    # -- lazy infra --------------------------------------------------------
    def _web3(self) -> Web3:
        if self._w3 is not None:
            return self._w3
        for url in self._rpc_urls:
            try:
                w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 15}))
                if w3.is_connected() and w3.eth.chain_id == CHAIN_ID:
                    log.info("PancakeClient RPC: %s (block %d)", url, w3.eth.block_number)
                    self._w3 = w3
                    return w3
            except Exception as e:  # noqa: BLE001
                log.warning("RPC %s unusable: %s", url, str(e)[:80])
        raise PancakeError("no BSC RPC reachable")

    def _account(self):
        if self._acct is None:
            if not self._password:
                raise PancakeError("TWAK_WALLET_PASSWORD not set — cannot sign")
            Account.enable_unaudited_hdwallet_features()
            mnemonic = _decrypt_mnemonic(self._keystore, self._password)
            self._acct = Account.from_mnemonic(mnemonic, account_path=HD_PATH)
            del mnemonic
            log.info("PancakeClient signer: %s", self._acct.address)
        return self._acct

    # -- token resolution --------------------------------------------------
    def _resolve(self, ref: str) -> str:
        """A swap side (address or symbol) -> checksum BSC address."""
        if isinstance(ref, str) and ref.startswith("0x") and len(ref) == 42:
            return Web3.to_checksum_address(ref)
        u = (ref or "").upper()
        if u in STABLES:
            return STABLES[u]
        if u in ("BNB", "WBNB"):
            return WBNB
        addr = self.registry.addresses.get(ref) or self.registry.addresses.get(u)
        if addr:
            return Web3.to_checksum_address(addr)
        raise PancakeError(f"no BSC address for {ref!r}")

    def _decimals(self, addr: str) -> int:
        if addr not in self._dec_cache:
            erc = self._web3().eth.contract(address=addr, abi=_ERC20_ABI)
            self._dec_cache[addr] = erc.functions.decimals().call()
        return self._dec_cache[addr]

    # -- quoting -----------------------------------------------------------
    def _q_single(self, tin: str, tout: str, amt: int):
        """Best (amountOut, fee) over fee tiers for a direct pool, or None."""
        w3 = self._web3()
        factory = w3.eth.contract(address=FACTORY_V3, abi=_FACTORY_ABI)
        quoter = w3.eth.contract(address=QUOTER_V2, abi=_QUOTER_ABI)
        best = None
        for fee in FEE_TIERS:
            if factory.functions.getPool(tin, tout, fee).call() == ZERO_ADDR:
                continue
            try:
                out = quoter.functions.quoteExactInputSingle(
                    (tin, tout, amt, fee, 0)).call()[0]
            except Exception:  # noqa: BLE001 — illiquid tick range etc.
                continue
            if out > 0 and (best is None or out > best[0]):
                best = (out, fee)
        return best

    def _best_route(self, tin: str, tout: str, amt: int):
        """Best of direct single-hop and 2-hop via WBNB. Returns a dict:
        {out, kind: 'single'|'multi', fee, path} or None."""
        routes = []
        d = self._q_single(tin, tout, amt)
        if d:
            routes.append({"out": d[0], "kind": "single", "fee": d[1],
                           "desc": f"direct fee{d[1]}"})
        if WBNB not in (tin, tout):
            h1 = self._q_single(tin, WBNB, amt)
            if h1:
                h2 = self._q_single(WBNB, tout, h1[0])
                if h2:
                    path = (bytes.fromhex(tin[2:]) + h1[1].to_bytes(3, "big")
                            + bytes.fromhex(WBNB[2:]) + h2[1].to_bytes(3, "big")
                            + bytes.fromhex(tout[2:]))
                    routes.append({"out": h2[0], "kind": "multi", "path": path,
                                   "desc": f"via WBNB {h1[1]}/{h2[1]}"})
        return max(routes, key=lambda r: r["out"]) if routes else None

    def _amount_in(self, tin: str, usd: float, amount: float | None) -> int:
        """Smallest-unit amountIn. Exits pass an exact token `amount`; entries
        pass a USD notional on a stable tokenIn (or a priced token, via spot)."""
        dec = self._decimals(tin)
        if amount is not None:
            return int(amount * 10**dec)
        if tin in STABLES.values():
            return int(usd * 10**dec)
        # Non-stable tokenIn priced by USD: spot via a 1-unit quote to USDT.
        spot = self._best_route(tin, STABLES["USDT"], 10**dec)
        if not spot or spot["out"] <= 0:
            raise PancakeError(f"no USDT price route for {tin}")
        price = spot["out"] / 10**self._decimals(STABLES["USDT"])
        return int(usd / price * 10**dec)

    def quote(self, from_token: str, to_token: str, usd: float,
              slippage_pct: float) -> Quote:
        """Return a TWAK-shaped quote dict (with priceImpact) so the Executor's
        up-front price-impact guard works unchanged. Price impact is measured as
        the effective-vs-marginal price gap at the real trade size."""
        tin, tout = self._resolve(from_token), self._resolve(to_token)
        amt = self._amount_in(tin, usd, None)
        full = self._best_route(tin, tout, amt)
        if not full:
            raise PancakeError(f"no route {from_token}->{to_token}")
        ref_amt = max(1, amt // 1000)
        ref = self._best_route(tin, tout, ref_amt)
        impact = 0.0
        if ref and ref["out"] > 0:
            marginal = ref["out"] / ref_amt
            effective = full["out"] / amt
            impact = max(0.0, (1 - effective / marginal) * 100)
        raw = {
            "priceImpact": f"{impact:.4f}",
            "output": str(full["out"]),
            "route": full["desc"],
            "provider": "pancakeswap-v3",
        }
        return Quote(from_token, to_token, usd, raw)

    def measure_round_trip(self, token_ref: str, size_usd: float) -> dict:
        """Quote-only USDT->token->USDT round-trip cost against live V3 pools —
        the REAL execution friction this client will pay. Same field shape as
        scripts/liquidity_filter.measure() so the report/edge-floor path is
        unchanged. Raises PancakeError if either leg has no route."""
        usdt = STABLES["USDT"]
        tok = self._resolve(token_ref)
        udec = self._decimals(usdt)
        amt_usdt = int(size_usd * 10**udec)
        buy = self._best_route(usdt, tok, amt_usdt)
        if not buy or buy["out"] <= 0:
            raise PancakeError(f"no buy route for {token_ref}")
        sell = self._best_route(tok, usdt, buy["out"])
        if not sell or sell["out"] <= 0:
            raise PancakeError(f"no sell route for {token_ref}")
        usd_back = sell["out"] / 10**udec
        return {
            "usd_back": round(usd_back, 2),
            "round_trip_cost_pct": round((1 - usd_back / size_usd) * 100, 3),
            "price_impact_1": None,
            "price_impact_2": None,
            "provider": "pancakeswap-v3",
            "route": f"{buy['desc']} / {sell['desc']}",
        }

    def price_usd(self, token_ref: str) -> float | None:
        """USD (USDT) spot price of ONE token via the best live V3 route — the
        valuation source for reconcile.py. Same venue as execution, so the mark
        equals what a sell would realize and a dead off-chain feed can never
        value the book at $0. Stables -> 1.0; no route -> None (caller values
        at $0 and logs). A 1-unit probe keeps price impact negligible."""
        if (token_ref or "").upper() in STABLES:
            return 1.0
        try:
            tok = self._resolve(token_ref)
        except PancakeError:
            return None
        usdt = STABLES["USDT"]
        if tok == usdt:
            return 1.0
        route = self._best_route(tok, usdt, 10 ** self._decimals(tok))
        if not route or route["out"] <= 0:
            return None
        return route["out"] / 10 ** self._decimals(usdt)

    # -- execution ---------------------------------------------------------
    def _send(self, w3, acct, tx) -> dict:
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = w3.eth.send_raw_transaction(raw)
        rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=self.timeout_s)
        return rcpt

    def _ensure_allowance(self, w3, acct, token: str, amount: int) -> None:
        erc = w3.eth.contract(address=token, abi=_ERC20_ABI)
        if erc.functions.allowance(acct.address, SMART_ROUTER).call() >= amount:
            return
        log.info("approving SmartRouter to spend %s", token)
        gas_price = w3.eth.gas_price
        tx = erc.functions.approve(SMART_ROUTER, MAX_UINT).build_transaction({
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address, "pending"),
            "gas": 80_000, "gasPrice": gas_price, "chainId": CHAIN_ID,
        })
        rcpt = self._send(w3, acct, tx)
        if rcpt.status != 1:
            raise PancakeError(f"approval reverted for {token}")

    def swap(self, from_token: str, to_token: str, usd: float,
             slippage_pct: float, amount: float | None = None) -> dict:
        if self.dry_run:
            log.info("[dry-run] pancake swap %s->%s $%.2f: quote only",
                     from_token, to_token, usd)
            return {"dry_run": True,
                    "quote": self.quote(from_token, to_token, usd, slippage_pct).raw}
        # Native BNB legs (gas balance, never a trading position) are out of
        # scope for the V3 ERC-20 path — delegate them to TWAK unchanged.
        if (from_token or "").upper() == "BNB" or (to_token or "").upper() == "BNB":
            log.info("native BNB leg -> delegating to TWAK")
            return self._twak.swap(from_token, to_token, usd, slippage_pct, amount=amount)

        w3 = self._web3()
        acct = self._account()
        tin, tout = self._resolve(from_token), self._resolve(to_token)
        to_dec = self._decimals(tout)

        amount_in = self._amount_in(tin, usd, amount)
        # Never ask to sell more than we actually hold (float round-trip can
        # exceed the wei balance and revert the whole tx).
        bal = w3.eth.contract(address=tin, abi=_ERC20_ABI).functions.balanceOf(
            acct.address).call()
        amount_in = min(amount_in, bal)
        if amount_in <= 0:
            raise PancakeError(f"zero balance/size for {from_token}")

        route = self._best_route(tin, tout, amount_in)
        if not route:
            raise PancakeError(f"no route {from_token}->{to_token}")
        min_out = int(route["out"] * (1 - slippage_pct / 100))

        self._ensure_allowance(w3, acct, tin, amount_in)

        router = w3.eth.contract(address=SMART_ROUTER, abi=_ROUTER_ABI)
        if route["kind"] == "single":
            fn = router.functions.exactInputSingle(
                (tin, tout, route["fee"], acct.address, amount_in, min_out, 0))
        else:
            fn = router.functions.exactInput(
                (route["path"], acct.address, amount_in, min_out))

        # Pre-flight simulation: eth_call the swap against current state. If the
        # ABI/route/allowance/min_out is wrong it reverts HERE, before we spend
        # gas or funds broadcasting.
        try:
            fn.call({"from": acct.address})
        except Exception as e:  # noqa: BLE001
            raise PancakeError(f"pre-flight revert {from_token}->{to_token}: "
                               f"{str(e)[:200]}") from e

        gas_price = w3.eth.gas_price
        try:
            gas = int(fn.estimate_gas({"from": acct.address}) * 1.25)
        except Exception:  # noqa: BLE001
            gas = 500_000
        tx = fn.build_transaction({
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address, "pending"),
            "gas": gas, "gasPrice": gas_price, "value": 0, "chainId": CHAIN_ID,
        })
        rcpt = self._send(w3, acct, tx)
        tx_hash = rcpt.transactionHash.hex()
        if not tx_hash.startswith("0x"):
            tx_hash = "0x" + tx_hash
        if rcpt.status != 1:
            raise PancakeError(f"swap reverted {from_token}->{to_token} "
                               f"(tx {tx_hash})")
        out_human = route["out"] / 10**to_dec
        log.info("pancake swap ok %s->%s: ~%.6g %s via %s (tx %s)",
                 from_token, to_token, out_human, to_token, route["desc"], tx_hash)
        return {
            "hash": tx_hash, "txHash": tx_hash,
            "output": f"{out_human:.6g} {to_token}",
            "explorer": f"https://bscscan.com/tx/{tx_hash}",
            "route": route["desc"], "amountOut": route["out"],
            "provider": "pancakeswap-v3", "dry_run": False,
        }


def make_pancake_client(twak, registry, chain="bsc", dry_run=True):
    """Wrap a TWAK client so quote/swap go direct to PancakeSwap V3 while every
    other surface (balances, compete, x402) keeps flowing through TWAK."""
    rpcs = os.environ.get("BSC_RPC_URLS")
    rpc_urls = tuple(u.strip() for u in rpcs.split(",") if u.strip()) if rpcs else DEFAULT_RPCS
    return PancakeClient(twak, registry, rpc_urls=rpc_urls, chain=chain, dry_run=dry_run)
