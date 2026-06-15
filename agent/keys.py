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
# Dedicated x402 payments/settlement wallet — a SEPARATE keystore in twak's
# exact format, so the public-facing x402 server never decrypts the trading
# key and its settlement txs never contend for the trading wallet's nonce.
X402_WALLET_FILE = Path.home() / ".twak" / "x402-wallet.json"
PBKDF2_ITERS = 600_000


def _decrypt_mnemonic(keystore: Path, password: str) -> str:
    """TWAK keystore scheme: PBKDF2-SHA256(600k) -> AES-256-GCM. In memory."""
    d = json.loads(keystore.read_text())
    key = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(d["salt"]), PBKDF2_ITERS, 32)
    cipher = AES.new(key, AES.MODE_GCM, nonce=bytes.fromhex(d["iv"]))
    plain = cipher.decrypt_and_verify(
        bytes.fromhex(d["encryptedMnemonic"]), bytes.fromhex(d["authTag"]))
    return plain.decode()


def decrypt_twak_mnemonic(password: str | None = None) -> str:
    """Decrypt the TWAK trading wallet mnemonic, in memory."""
    password = password or os.environ.get("TWAK_WALLET_PASSWORD")
    if not password:
        raise RuntimeError("TWAK_WALLET_PASSWORD not set")
    return _decrypt_mnemonic(TWAK_WALLET_FILE, password)


def agent_account(password: str | None = None):
    """eth_account Account for the agent wallet, verified by address."""
    from eth_account import Account
    Account.enable_unaudited_hdwallet_features()
    acct = Account.from_mnemonic(decrypt_twak_mnemonic(password))
    if acct.address.lower() != AGENT_WALLET.lower():
        raise RuntimeError(
            f"derived {acct.address} != agent wallet {AGENT_WALLET} — abort")
    return acct


def x402_account(password: str | None = None):
    """eth_account Account for the dedicated x402 payments/settlement wallet.

    Loaded from its OWN keystore (never the trading wallet's), so the public
    x402 server settles from a separate nonce space and a compromise of that
    process can never touch the trading key. The keystore records its own
    address; we verify the derived key matches it. Password from
    X402_WALLET_PASSWORD (create the wallet with scripts/create_x402_wallet.py).
    """
    from eth_account import Account
    Account.enable_unaudited_hdwallet_features()
    password = password or os.environ.get("X402_WALLET_PASSWORD")
    if not password:
        raise RuntimeError("X402_WALLET_PASSWORD not set")
    if not X402_WALLET_FILE.exists():
        raise RuntimeError(
            f"{X402_WALLET_FILE} missing — run scripts/create_x402_wallet.py")
    expected = json.loads(X402_WALLET_FILE.read_text()).get("address")
    acct = Account.from_mnemonic(_decrypt_mnemonic(X402_WALLET_FILE, password))
    if expected and acct.address.lower() != expected.lower():
        raise RuntimeError(
            f"derived {acct.address} != recorded {expected} — abort")
    return acct
