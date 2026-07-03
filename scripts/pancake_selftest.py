"""One-off REAL self-test of the direct-PancakeSwap execution path.

Fires a tiny $2 USDT->USDC->USDT round-trip through the production PancakeClient
to prove the full sign -> approve -> exactInputSingle -> receipt path on-chain
BEFORE the live bot is trusted with position-sized swaps. Cost ≈ $0.01 (two
0.01% legs) + a few cents gas. Run with the live bot STOPPED (nonce safety).

Balance-neutral by design: sells back only the USDC received, capped at the
wallet's real balanceOf, so pre-existing USDC is untouched beyond the $2 unwind.
"""
import sys

from web3 import Web3

sys.path.insert(0, "/app")
from agent.execution.pancake import PancakeClient, STABLES  # noqa: E402
from agent.tokens import TokenRegistry  # noqa: E402

USDT = STABLES["USDT"]
USDC = STABLES["USDC"]
ERC20 = [{"inputs": [{"type": "address"}], "name": "balanceOf",
          "outputs": [{"type": "uint256"}], "stateMutability": "view",
          "type": "function"}]

reg = TokenRegistry()
# twak=None: a USDT/USDC swap never hits the native-BNB delegation path.
pc = PancakeClient(None, reg, dry_run=False)
w3 = pc._web3()
acct = pc._account()


def bal(tok):
    c = w3.eth.contract(address=tok, abi=ERC20)
    return c.functions.balanceOf(acct.address).call() / 1e18


print(f"signer {acct.address}")
print(f"before: USDT={bal(USDT):.4f}  USDC={bal(USDC):.4f}")

print("\nleg1: USDT -> USDC  $2.00")
r1 = pc.swap(USDT, USDC, 2.0, 1.0)
print(f"  tx {r1['txHash']}  out {r1['output']}  route {r1['route']}")

received_usdc = r1["amountOut"] / 1e18
print(f"\nleg2: USDC -> USDT  amount={received_usdc:.6f}")
r2 = pc.swap(USDC, USDT, 0.0, 1.0, amount=received_usdc)
print(f"  tx {r2['txHash']}  out {r2['output']}  route {r2['route']}")

print(f"\nafter:  USDT={bal(USDT):.4f}  USDC={bal(USDC):.4f}")
print("SELFTEST_OK")
