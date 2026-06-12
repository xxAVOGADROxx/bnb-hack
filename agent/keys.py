"""In-memory access to the agent key, from TWAK's locally-encrypted keystore.

Single source of truth stays ~/.twak/wallet.json (PBKDF2-SHA256 600k +
AES-256-GCM). The mnemonic is decrypted in memory, the BIP-44 key derived in
memory, and the derived address is ALWAYS verified against the expected agent
wallet before the key is used. Nothing is ever written to disk.

Used by the one-shot ERC-8004 registration and the x402 server's settlement
signer — never by the trading loop (TWAK signs all trades itself).
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from Crypto.Cipher import AES

AGENT_WALLET = "0x44dD4C2c353457fF68b164934870BB0391f9251C"
TWAK_WALLET_FILE = Path.home() / ".twak" / "wallet.json"
PBKDF2_ITERS = 600_000


def decrypt_twak_mnemonic(password: str | None = None) -> str:
    """TWAK keystore scheme: PBKDF2-SHA256(600k) -> AES-256-GCM. In memory."""
    password = password or os.environ.get("TWAK_WALLET_PASSWORD")
    if not password:
        raise RuntimeError("TWAK_WALLET_PASSWORD not set")
    d = json.loads(TWAK_WALLET_FILE.read_text())
    key = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(d["salt"]), PBKDF2_ITERS, 32)
    cipher = AES.new(key, AES.MODE_GCM, nonce=bytes.fromhex(d["iv"]))
    plain = cipher.decrypt_and_verify(
        bytes.fromhex(d["encryptedMnemonic"]), bytes.fromhex(d["authTag"]))
    return plain.decode()


def agent_account(password: str | None = None):
    """eth_account Account for the agent wallet, verified by address."""
    from eth_account import Account
    Account.enable_unaudited_hdwallet_features()
    acct = Account.from_mnemonic(decrypt_twak_mnemonic(password))
    if acct.address.lower() != AGENT_WALLET.lower():
        raise RuntimeError(
            f"derived {acct.address} != agent wallet {AGENT_WALLET} — abort")
    return acct
