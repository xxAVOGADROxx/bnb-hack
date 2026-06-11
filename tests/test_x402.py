"""x402 MCP framing + premium-response interpretation (pure parts)."""
import json

from agent.x402.premium import _mcp_body, _unwrap_mcp, confirms_entry


def test_mcp_body_is_jsonrpc_tools_call():
    body = json.loads(_mcp_body("get_crypto_technical_analysis", {"symbol": "CAKE"}))
    assert body["jsonrpc"] == "2.0"
    assert body["method"] == "tools/call"
    assert body["params"] == {
        "name": "get_crypto_technical_analysis",
        "arguments": {"symbol": "CAKE"},
    }


def test_unwrap_jsonrpc_content_text_with_nested_json():
    raw = {
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": '{"trend": "bullish"}'}]},
    }
    assert _unwrap_mcp(raw) == {"trend": "bullish"}


def test_unwrap_twak_envelope_then_sse_framing():
    inner = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": '{"summary": "buy"}'}]},
    })
    raw = {"body": f"event: message\ndata: {inner}\n\n"}
    assert _unwrap_mcp(raw) == {"summary": "buy"}


def test_unwrap_garbage_returns_payload_not_crash():
    assert _unwrap_mcp(None) is None
    assert _unwrap_mcp("") is None
    assert _unwrap_mcp("not json at all") == "not json at all"


def test_confirms_entry_bullish():
    assert confirms_entry({"analysis": {"trend": "Bullish", "rsi": 61}}) is True
    assert confirms_entry({"recommendation": "STRONG_BUY"}) is True


def test_confirms_entry_bearish():
    assert confirms_entry({"summary": "sell — momentum fading"}) is False


def test_confirms_entry_ambiguous_or_unknown_is_none():
    # conflict -> None; unrecognized shape -> None (callers treat as deny)
    assert confirms_entry({"signal": "buy", "trend": "bearish"}) is None
    assert confirms_entry({"rsi": 61, "macd": 0.2}) is None
    assert confirms_entry("plain text with no verdict keys") is None
