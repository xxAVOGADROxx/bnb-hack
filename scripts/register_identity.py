"""Register the agent's on-chain identity (ERC-8004) via the BNB AI Agent SDK.

One-shot, idempotent (re-running prints the existing registration). BSC
testnet, gas-free (MegaFuel paymaster sponsorship).

Self-custody, end to end — the same story as trading:
  1. Reads TWAK's locally-encrypted mnemonic (~/.twak/wallet.json,
     PBKDF2-SHA256 600k + AES-256-GCM) and decrypts it IN MEMORY with
     TWAK_WALLET_PASSWORD (.env). No prompt, no copy.
  2. Derives the agent key (BIP-44 m/44'/60'/0'/0/0) and VERIFIES it matches
     the competition wallet before doing anything.
  3. Hands it to the SDK with persist=False: in-memory only. The raw key is
     never written anywhere; ~/.twak stays the single source of truth.

The minted ERC-721 agentId goes to the SAME address that trades on mainnet —
one identity across the whole stack (CMC signal -> TWAK execution -> ERC-8004
identity).

Usage: .venv/bin/python scripts/register_identity.py [--dry-run]
Writes the public result (agentId, tx, uri) to data/identity.json.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from agent.config import DATA_DIR, ROOT  # noqa: E402
from agent.keys import AGENT_WALLET, agent_account  # noqa: E402

AGENT_NAME = "bnb-hack-1337"
AGENT_DESCRIPTION = (
    "Autonomous self-custody trading agent on BSC: CoinMarketCap AI signals "
    "-> deterministic strategy + fail-closed risk engine -> Trust Wallet "
    "Agent Kit local signing. BNB Hack: AI Trading Agent Edition."
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="decrypt + derive + verify the address, then stop")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    password = os.environ.get("TWAK_WALLET_PASSWORD")
    if not password:
        raise SystemExit("TWAK_WALLET_PASSWORD missing from .env")

    acct = agent_account(password)  # decrypts in memory + verifies the address
    print(f"key verified: derives the competition wallet {acct.address}")
    if args.dry_run:
        print("dry-run: stopping before any chain interaction")
        return

    from bnbagent import AgentEndpoint, ERC8004Agent, EVMWalletProvider

    wallet = EVMWalletProvider(password=password, private_key=acct.key.hex(),
                               persist=False)  # in-memory only, nothing written
    sdk = ERC8004Agent(wallet_provider=wallet, network="bsc-testnet")

    existing = None
    try:
        existing = sdk.get_local_agent_info()
    except Exception:
        pass  # not registered yet
    if existing and existing.get("agentId") is not None:
        print(f"already registered: agentId={existing.get('agentId')}")
        result = existing
    else:
        uri = sdk.generate_agent_uri(
            name=AGENT_NAME,
            description=AGENT_DESCRIPTION,
            endpoints=[AgentEndpoint(
                name="repository",
                endpoint="https://github.com/xxAVOGADROxx/bnb-hack",
                version="1.0.0",
            )],
        )
        result = sdk.register_agent(agent_uri=uri, metadata=[
            {"key": "competition", "value": "bnb-hack-ai-trading-2026"},
            {"key": "mainnet_wallet", "value": AGENT_WALLET},
        ])
        print(f"registered: {result}")

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "identity.json").write_text(json.dumps(
        {"network": "bsc-testnet", "wallet": AGENT_WALLET,
         "contract": str(getattr(sdk, "contract_address", "")), "result":
         {k: str(v) for k, v in (result or {}).items()}}, indent=2))
    print("saved -> data/identity.json")


if __name__ == "__main__":
    main()
