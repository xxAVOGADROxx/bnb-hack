"""x402-gated leaderboard API — the agent doesn't just PAY x402, it CHARGES.

Sells the agent's read-only competition leaderboard (data/leaderboard.json,
produced by agent/monitor/leaderboard.py) at a flat price per call, speaking
canonical x402 V2 over plain HTTP — the same dialect CMC's x402 MCP uses, so
any compliant client (e.g. `twak x402 request`) can pay it out of the box:

  1. GET /leaderboard with no payment -> HTTP 402 + `payment-required` header
     (base64 x402 V2 requirements: exact / eip155:56 / USD1 / eip3009).
  2. Client signs an EIP-3009 TransferWithAuthorization for the price and
     retries with the X-PAYMENT (or PAYMENT-SIGNATURE) header.
  3. We verify the EIP-712 signature OFF-CHAIN (recover == from, to == us,
     value >= price, time window), then SETTLE ON-CHAIN: submit
     transferWithAuthorization to the token contract (we pay cents of BNB
     gas; the token's authorization nonce makes replays impossible).
  4. HTTP 200 + the leaderboard JSON + X-PAYMENT-RESPONSE (settlement tx).

USD1 (World Liberty Financial USD, 18 dp) is the asset because it is the
BSC stable that implements EIP-3009 — the simplest spec-compliant scheme
(no Permit2 spender contract needed). GET / is free and documents all this.

Usage: .venv/bin/python -m agent.x402.server [--port 8402] [--price-usd1 0.01]
Needs TWAK_WALLET_PASSWORD in .env (settlement signer — in-memory key, see
agent/keys.py) and a few cents of BNB for settlement gas.
"""
from __future__ import annotations

import argparse
import base64
import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from Crypto.Hash import keccak

from agent.alerts import Alerter
from agent.chain import Rpc
from agent.config import DATA_DIR, ROOT
from agent.keys import AGENT_WALLET, agent_account
from agent.x402 import ledger

log = logging.getLogger("x402.server")

USD1 = "0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0D"
USD1_DECIMALS = 18
CHAIN_ID = 56
BOARD_PATH = DATA_DIR / "leaderboard.json"

# transferWithAuthorization(address,address,uint256,uint256,uint256,bytes32,uint8,bytes32,bytes32)
_SEL = keccak.new(digest_bits=256, data=(
    b"transferWithAuthorization(address,address,uint256,uint256,uint256,"
    b"bytes32,uint8,bytes32,bytes32)")).hexdigest()[:8]

EIP712_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "TransferWithAuthorization": [
        {"name": "from", "type": "address"},
        {"name": "to", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "validAfter", "type": "uint256"},
        {"name": "validBefore", "type": "uint256"},
        {"name": "nonce", "type": "bytes32"},
    ],
}
DOMAIN = {"name": "World Liberty Financial USD", "version": "1",
          "chainId": CHAIN_ID, "verifyingContract": USD1}

REPORTS_DIR = DATA_DIR / "reports"
STATE_PATH = DATA_DIR / "state.json"


# -- priced products -------------------------------------------------------
# Each paid endpoint is one entry: a producer reading an artefact the agent
# already generates. Adding a product is one line here + a producer — the
# 402/verify/settle rail is shared, so the offering expands with no new signing
# surface (see docs/X402.md).
def _produce_leaderboard() -> dict:
    return (json.loads(BOARD_PATH.read_text())
            if BOARD_PATH.exists() else {"error": "no snapshot yet"})


def _produce_posture() -> dict:
    """Current risk posture: portfolio value, drawdown high-water mark, and
    the latest regime read — derived from state the agent already persists."""
    state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
    latest = _latest_report() or {}
    return {
        "high_water_mark_usd": state.get("high_water_mark_usd"),
        "baseline_usd": state.get("baseline_usd"),
        "snapshots": (state.get("snapshots") or [])[-3:],
        "recent_regimes": latest.get("digest", {}).get("regimes", {}),
        "as_of": latest.get("generated"),
    }


def _latest_report() -> dict | None:
    if not REPORTS_DIR.exists():
        return None
    files = sorted(REPORTS_DIR.glob("*.json"))
    if not files:
        return None
    try:
        return json.loads(files[-1].read_text())
    except (OSError, ValueError):
        return None


def _produce_report() -> dict:
    return _latest_report() or {"error": "no report yet"}


PRODUCTS: dict[str, dict] = {
    "/leaderboard": {
        "produce": _produce_leaderboard,
        "description": "field-wide ranking of registered competition wallets: "
                       "USD value + return% vs baseline, on-chain truth",
    },
    "/posture": {
        "produce": _produce_posture,
        "description": "the agent's current risk posture: high-water mark, "
                       "baseline, recent snapshots and regime reads",
    },
    "/report": {
        "produce": _produce_report,
        "description": "the latest periodic operations report (signals, blocks, "
                       "trades, approximate round-trip PnL)",
    },
}


def _match_product(path: str) -> str | None:
    base = "/" + path.lstrip("/").split("?", 1)[0].rstrip("/")
    return base if base in PRODUCTS else None


def payment_requirements(price_atomic: int) -> dict:
    return {
        "x402Version": 2,
        "error": "Payment required",
        "resource": {"url": "/leaderboard",
                     "description": "BNB Hack competition leaderboard (on-chain, refreshed)"},
        "accepts": [{
            "scheme": "exact",
            "network": f"eip155:{CHAIN_ID}",
            "asset": USD1,
            "amount": str(price_atomic),
            "payTo": AGENT_WALLET,
            "maxTimeoutSeconds": 60,
            "extra": {"name": DOMAIN["name"], "version": DOMAIN["version"],
                      "assetTransferMethod": "eip3009"},
        }],
    }


def verify_payment(header_b64: str, price_atomic: int) -> dict:
    """Decode + verify the X-PAYMENT payload OFF-CHAIN. Returns the
    authorization dict (with signature) or raises ValueError."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    p = json.loads(base64.b64decode(header_b64))
    payload = p.get("payload") or p
    auth = payload.get("authorization") or payload
    sig = payload.get("signature") or p.get("signature")
    if not sig:
        raise ValueError("no signature in payment payload")
    for k in ("from", "to", "value", "validAfter", "validBefore", "nonce"):
        if k not in auth:
            raise ValueError(f"authorization missing {k}")

    message = {
        "from": auth["from"], "to": auth["to"], "value": int(auth["value"]),
        "validAfter": int(auth["validAfter"]),
        "validBefore": int(auth["validBefore"]), "nonce": auth["nonce"],
    }
    signable = encode_typed_data(full_message={
        "types": EIP712_TYPES, "primaryType": "TransferWithAuthorization",
        "domain": DOMAIN, "message": message,
    })
    recovered = Account.recover_message(signable, signature=sig)

    if recovered.lower() != str(auth["from"]).lower():
        raise ValueError(f"signature recovers {recovered}, not {auth['from']}")
    if str(auth["to"]).lower() != AGENT_WALLET.lower():
        raise ValueError("payment not addressed to this agent")
    if int(auth["value"]) < price_atomic:
        raise ValueError(f"value {auth['value']} below price {price_atomic}")
    now = int(time.time())
    if not (int(auth["validAfter"]) <= now <= int(auth["validBefore"])):
        raise ValueError("authorization outside its validity window")
    return {**message, "signature": sig}


def settle(rpc: Rpc, acct, auth: dict) -> str:
    """Submit transferWithAuthorization on-chain. Returns the tx hash.
    The token contract enforces the authorization nonce -> no replays."""
    sig = bytes.fromhex(auth["signature"].removeprefix("0x"))
    r, s, v = sig[:32], sig[32:64], sig[64]
    if v < 27:
        v += 27
    word = lambda x: x.to_bytes(32, "big")  # noqa: E731
    addr = lambda a: bytes(12) + bytes.fromhex(a.removeprefix("0x"))  # noqa: E731
    nonce32 = bytes.fromhex(str(auth["nonce"]).removeprefix("0x"))
    data = "0x" + _SEL + (
        addr(auth["from"]) + addr(auth["to"]) + word(int(auth["value"]))
        + word(int(auth["validAfter"])) + word(int(auth["validBefore"]))
        + nonce32 + word(v) + r + s
    ).hex()

    tx_nonce = int(rpc.call("eth_getTransactionCount", [acct.address, "pending"]), 16)
    gas_price = int(rpc.call("eth_gasPrice", []), 16)
    from eth_utils import to_checksum_address  # eth_account requires EIP-55
    tx = {"nonce": tx_nonce, "gasPrice": max(gas_price, 10 ** 8), "gas": 150_000,
          "to": to_checksum_address(USD1), "value": 0, "data": data,
          "chainId": CHAIN_ID}
    raw = acct.sign_transaction(tx).raw_transaction
    tx_hash = rpc.call("eth_sendRawTransaction", ["0x" + raw.hex()])
    for _ in range(30):  # ~60s: BSC confirms in seconds
        time.sleep(2)
        rec = rpc.call("eth_getTransactionReceipt", [tx_hash])
        if rec:
            if int(rec.get("status", "0x0"), 16) != 1:
                raise RuntimeError(f"settlement reverted: {tx_hash}")
            return tx_hash
    raise RuntimeError(f"settlement not confirmed in time: {tx_hash}")


def make_handler(price_atomic: int, rpc: Rpc, acct, alerter: Alerter | None = None):
    requirements_b64 = base64.b64encode(
        json.dumps(payment_requirements(price_atomic)).encode()).decode()

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict, headers: dict | None = None):
            data = json.dumps(body, indent=1).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):  # route to our logger
            log.info("%s %s", self.address_string(), fmt % args)

        def do_GET(self):  # noqa: N802
            price_usd1 = price_atomic / 10 ** USD1_DECIMALS
            if self.path.rstrip("/") in ("", "/index.html"):
                return self._send(200, {
                    "service": "bnb-hack-1337 — x402-gated agent data API",
                    "agent": AGENT_WALLET,
                    "erc8004_agent_id": 1375,
                    "catalog": "/catalog (free)",
                    "paid_endpoints": sorted(PRODUCTS),
                    "price": f"{price_usd1} USD1 each (BSC, eip3009)",
                    "how_to_pay": "x402 V2 — e.g.: twak x402 request "
                                  "<this-url>/leaderboard --prefer-network bsc --yes",
                })
            if self.path.rstrip("/") == "/catalog":  # free product discovery
                return self._send(200, {
                    "price_each": f"{price_usd1} USD1",
                    "asset": f"USD1 eip155:{CHAIN_ID} (eip3009)",
                    "products": [{"path": p, "description": d["description"]}
                                 for p, d in sorted(PRODUCTS.items())],
                })

            product = _match_product(self.path)
            if product is None:
                return self._send(404, {"error": "unknown path",
                                        "see": "/catalog"})

            header = (self.headers.get("X-PAYMENT")
                      or self.headers.get("PAYMENT-SIGNATURE"))
            if not header:
                return self._send(
                    402, {"error": "Payment required — see payment-required header "
                                   "(x402 V2). Pay with: twak x402 request",
                          "product": product},
                    {"payment-required": requirements_b64})
            try:
                auth = verify_payment(header, price_atomic)
            except (ValueError, KeyError, json.JSONDecodeError) as e:
                return self._send(402, {"error": f"invalid payment: {e}"},
                                  {"payment-required": requirements_b64})
            try:
                tx_hash = settle(rpc, acct, auth)
            except Exception as e:  # noqa: BLE001 — report settlement failure
                log.error("settlement failed: %s", e)
                return self._send(502, {"error": f"settlement failed: {e}"})

            log.info("paid by %s for %s -> %s", auth["from"], product, tx_hash)
            # Record revenue + alert (both best-effort: never fail the response).
            try:
                ledger.record_charge(price_usd1, "USD1", auth["from"], product, tx_hash)
            except Exception as e:  # noqa: BLE001
                log.warning("ledger write failed: %s", e)
            if alerter:
                alerter.notify(
                    f"💰 x402 charge {price_usd1} USD1 for {product}\n"
                    f"from {auth['from'][:10]}…  tx {tx_hash[:14]}…")

            data = PRODUCTS[product]["produce"]()
            response_b64 = base64.b64encode(json.dumps({
                "success": True, "network": f"eip155:{CHAIN_ID}",
                "transaction": tx_hash}).encode()).decode()
            return self._send(200, {"paid_by": auth["from"], "product": product,
                                    "settlement_tx": tx_hash, "data": data},
                              {"X-PAYMENT-RESPONSE": response_b64})

    return Handler


def main() -> None:
    from dotenv import load_dotenv
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8402)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--price-usd1", type=float, default=0.01)
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    load_dotenv(ROOT / ".env")
    acct = agent_account()  # verifies the derived address == agent wallet
    price_atomic = int(args.price_usd1 * 10 ** USD1_DECIMALS)
    alerter = Alerter(os.environ.get("TELEGRAM_BOT_TOKEN"),
                      os.environ.get("TELEGRAM_CHAT_ID"))
    log.info("x402 server on %s:%d — selling %s at %.4f USD1 each to %s",
             args.host, args.port, ",".join(sorted(PRODUCTS)), args.price_usd1,
             acct.address)
    ThreadingHTTPServer((args.host, args.port),
                        make_handler(price_atomic, Rpc(), acct, alerter)).serve_forever()


if __name__ == "__main__":
    main()
