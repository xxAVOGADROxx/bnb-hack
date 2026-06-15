"""Create a dedicated x402 payments/settlement wallet.

A SEPARATE keystore (~/.twak/x402-wallet.json) in twak's exact format
(PBKDF2-SHA256 600k -> AES-256-GCM), so the public-facing x402 server never
decrypts the trading wallet's key and its settlement txs never contend for the
trading wallet's nonce. `twak wallet create` can't make a second wallet (it
overwrites ~/.twak/wallet.json), so we mint one here in the same scheme.

The mnemonic is generated and encrypted IN MEMORY and is NEVER printed (so it
can't leak into a terminal capture or chat) — only the public address is shown.
To back up the seed offline later, decrypt the keystore on a trusted machine
with the same scheme as ~/.twak/reveal-mnemonic.js (point it at x402-wallet.json).

Fund the printed address with a little BNB for settlement gas; x402 revenue
(USD1) accrues there too.

Usage: X402_WALLET_PASSWORD=... .venv/bin/python scripts/create_x402_wallet.py
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path

from Crypto.Cipher import AES

KEYSTORE = Path.home() / ".twak" / "x402-wallet.json"
PBKDF2_ITERS = 600_000


def main() -> int:
    password = os.environ.get("X402_WALLET_PASSWORD")
    if not password:
        print("X402_WALLET_PASSWORD not set (export it, don't paste it in chat)",
              file=sys.stderr)
        return 1
    if KEYSTORE.exists():
        print(f"{KEYSTORE} already exists — refusing to overwrite", file=sys.stderr)
        return 1

    from eth_account import Account
    Account.enable_unaudited_hdwallet_features()
    acct, mnemonic = Account.create_with_mnemonic()  # default path m/44'/60'/0'/0/0

    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, PBKDF2_ITERS, 32)
    cipher = AES.new(key, AES.MODE_GCM, nonce=iv)
    ciphertext, tag = cipher.encrypt_and_digest(mnemonic.encode())
    del mnemonic  # don't keep the seed around longer than the encrypt

    KEYSTORE.parent.mkdir(parents=True, exist_ok=True)
    KEYSTORE.write_text(json.dumps({
        "encryptedMnemonic": ciphertext.hex(),
        "iv": iv.hex(),
        "authTag": tag.hex(),
        "salt": salt.hex(),
        "address": acct.address,
        "createdAt": int(time.time()),
        "chains": ["ethereum"],
    }, indent=2))
    KEYSTORE.chmod(0o600)

    print(f"x402 payments wallet created: {acct.address}")
    print(f"keystore: {KEYSTORE} (mode 600, twak format)")
    print("Next: fund this address with a little BNB for settlement gas,")
    print("and set X402_WALLET_PASSWORD in the server environment.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
