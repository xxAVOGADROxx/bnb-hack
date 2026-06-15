"""x402 branch — cost-aware premium data fetch via CMC's x402 MCP.

By design this is the ONLY code path that pays per call: when the
deterministic signal lands in the grey zone (3/4 entry conditions in a
CONFLICTED regime), the agent pays one x402 micropayment for a premium
technical-analysis pull and enters only on a clear bullish confirmation.
Real on-chain payments, logged with their hash — not a README mention.

Protocol, verified live against the endpoint (2026-06-11):
- initialize / tools/list are free, plain JSON-RPC over POST (no session).
- tools/call returns HTTP 402 with a base64 x402 v2 challenge in the
  `payment-required` header; retry with a PAYMENT-SIGNATURE header pays.
- The `accepts` list includes BSC (eip155:56) — USDC BEP-20 (18 decimals),
  USD1 and United Stables — alongside Base USDC (6 decimals), 0.01 USD/call.
  So the payment settles on BSC with the same stable the agent already
  trades: one chain, self-custody intact, no Base balance required. We hold a
  small USDC balance on BSC for exactly this.
- get_crypto_technical_analysis takes the numeric CMC `id` (not a symbol).

KNOWN BLOCKER (twak 0.18.0, verified 2026-06-11): the payment rail is proven
end to end up to the signature — twak detects the 402, selects the BSC USDC
route, broadcasts the one-time Permit2 approval and signs the payment. But
CMC's MCP transport (StreamableHTTP) requires
`Accept: application/json, text/event-stream`; twak's paid retry omits the
SSE part, so CMC returns HTTP 400 *before settling* (no funds move). Proven
with a controlled curl: same body returns 400 with `Accept: application/json`
and 200 with both types. The CLI exposes no header override. This is genuine
sponsor-tooling feedback. The branch below degrades safely (a failed/blocked
call == "no confirmation" == stay out), so the agent is correct either way;
when twak fixes the header (or we self-host a thin SSE-adding proxy) the paid
entry path lights up with no code change here.

twak's x402 client handles the challenge/sign/retry dance; this module only
frames the MCP JSON-RPC body and interprets the answer. Anything it cannot
interpret counts as "no confirmation" — the branch is conservative.
"""
from __future__ import annotations

import json
import logging
import os

from agent.logger import DecisionLog
from agent.twak.client import AnyTwak, TwakError

log = logging.getLogger(__name__)

# Direct CMC endpoint by default. Set CMC_X402_MCP_URL to the local SSE proxy
# (agent/x402/sse_proxy.py, e.g. http://127.0.0.1:8403/x402/mcp) to work around
# twak's missing `Accept: text/event-stream` on the paid retry — opt-in, so the
# default behaviour is unchanged.
CMC_X402_MCP = os.environ.get(
    "CMC_X402_MCP_URL", "https://mcp.coinmarketcap.com/x402/mcp")
PREFERRED_NETWORK = "bsc"
# Hard per-call cap in atomic units of the payment asset (CMC charges 0.01
# USD/call; we cap at 0.02). Decimals differ per network: 18 on BSC, 6 on Base.
MAX_PAYMENT_ATOMIC = {"bsc": 2 * 10**16, "base": 20_000}

TIE_BREAK_TOOL = "get_crypto_technical_analysis"

_BULLISH = ("buy", "bullish", "strong_buy", "strong buy", "uptrend")
_BEARISH = ("sell", "bearish", "strong_sell", "strong sell", "downtrend")
_VERDICT_KEYS = {"summary", "recommendation", "signal", "trend", "rating", "action", "sentiment"}


def _mcp_body(tool: str, arguments: dict) -> str:
    return json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    })


def _unwrap_mcp(raw) -> dict | list | str | None:
    """Tolerant unwrap: twak response -> HTTP body -> JSON-RPC result ->
    MCP content. Returns None when no payload can be extracted."""
    if raw is None:
        return None
    if isinstance(raw, str):
        # SSE framing ("data: {...}") or plain JSON text.
        for line in raw.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                raw = line[5:].strip()
                break
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return raw or None
    if not isinstance(raw, dict):
        return raw
    for key in ("body", "response", "data", "text", "content"):
        if key in raw and not ("jsonrpc" in raw or "result" in raw):
            return _unwrap_mcp(raw[key])
    if "result" in raw:  # JSON-RPC envelope
        result = raw["result"]
        content = result.get("content") if isinstance(result, dict) else None
        if isinstance(content, list) and content:
            texts = [str(c["text"]) for c in content if isinstance(c, dict) and c.get("text")]
            if texts:
                return _unwrap_mcp("\n".join(texts))
        return result
    return raw


def _verdict_strings(data, out: list[str], depth: int = 0) -> None:
    """Collect values of verdict-shaped keys anywhere in the payload."""
    if depth > 6:
        return
    if isinstance(data, dict):
        for k, v in data.items():
            if str(k).lower() in _VERDICT_KEYS and isinstance(v, str):
                out.append(v.lower())
            else:
                _verdict_strings(v, out, depth + 1)
    elif isinstance(data, list):
        for v in data:
            _verdict_strings(v, out, depth + 1)


def confirms_entry(data) -> bool | None:
    """True on a clear bullish read, False on bearish, None when ambiguous
    or unrecognized (callers must treat None as deny)."""
    found: list[str] = []
    _verdict_strings(data, found)
    bull = any(b in s for s in found for b in _BULLISH)
    bear = any(b in s for s in found for b in _BEARISH)
    if bull and not bear:
        return True
    if bear and not bull:
        return False
    return None


def mcp_paid_call(twak: AnyTwak, decisions: DecisionLog, tool: str, arguments: dict):
    """One x402-paid MCP tool call. Returns the unwrapped payload or None
    (callers must treat None as 'no extra signal')."""
    try:
        result = twak.x402_request(
            CMC_X402_MCP,
            MAX_PAYMENT_ATOMIC[PREFERRED_NETWORK],
            method="POST",
            body=_mcp_body(tool, arguments),
            prefer_network=PREFERRED_NETWORK,
        )
    except TwakError as e:
        log.warning("x402 call %s failed: %s", tool, e)
        decisions.append("x402_failed", tool=tool, arguments=arguments, error=str(e))
        return None

    if result.get("dry_run"):
        decisions.append("x402_skipped_dry_run", tool=tool, arguments=arguments)
        return None
    payment_hash = result.get("paymentHash") or result.get("hash")
    decisions.append(
        "x402_paid", tool=tool, arguments=arguments, payment_hash=payment_hash,
    )
    # Record the spend so the report shows x402 cost next to revenue. Cost-aware
    # by design: this path only fires on a grey-zone tie-break (<= one call).
    try:
        from agent.x402 import ledger
        ledger.record_spend(0.01, PREFERRED_NETWORK.upper(), CMC_X402_MCP,
                            f"premium:{tool}", str(payment_hash or ""))
    except Exception as e:  # noqa: BLE001 — accounting must never break trading
        log.warning("spend ledger write failed: %s", e)
    payload = _unwrap_mcp(result)
    if payload is None:
        # Keep the raw response: first paid calls calibrate the parser.
        decisions.append("x402_unparsed", tool=tool, raw=str(result)[:2000])
    return payload


def tie_break(twak: AnyTwak, decisions: DecisionLog, token: str, cmc_id: int) -> bool:
    """Grey-zone tie-break: pay for one premium TA pull; enter only on a
    clear bullish confirmation. The TA tool keys on the numeric CMC id."""
    payload = mcp_paid_call(twak, decisions, TIE_BREAK_TOOL, {"id": str(cmc_id)})
    if payload is None:
        return False
    verdict = confirms_entry(payload)
    decisions.append("x402_tie_break", token=token, verdict=verdict)
    return verdict is True
