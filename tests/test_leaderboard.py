"""Leaderboard monitor — pure parts (ABI plumbing + scoring helpers)."""
from agent.monitor.leaderboard import (
    decode_aggregate3, encode_aggregate3, posture, return_pct,
)


def w(v: int) -> str:
    return f"{v:064x}"


def test_encode_aggregate3_layout():
    target = "0x" + "ab" * 20
    data = encode_aggregate3([(target, "0x70a08231" + "11" * 32)])
    assert data.startswith("0x82ad56cb")
    body = data[10:]
    words = [body[i:i + 64] for i in range(0, len(body), 64)]
    assert int(words[0], 16) == 0x20          # offset to array
    assert int(words[1], 16) == 1             # array length
    assert int(words[2], 16) == 32            # offset to tuple 0
    assert words[3][-40:] == "ab" * 20        # target address
    assert int(words[4], 16) == 1             # allowFailure
    assert int(words[5], 16) == 0x60          # offset to bytes
    assert int(words[6], 16) == 36            # calldata length (4 + 32)


def test_decode_aggregate3_success_and_failure():
    # Result[2]: (true, bytes32(5)) and (false, empty)
    payload = "0x" + "".join([
        w(0x20), w(2), w(64), w(192),
        w(1), w(0x40), w(32), w(5),       # tuple 0: success, value 5
        w(0), w(0x40), w(0),              # tuple 1: failed, no data
    ])
    assert decode_aggregate3(payload) == [5, None]


def test_encode_decode_word_alignment_multiple_calls():
    calls = [("0x" + f"{i:040x}", "0x313ce567") for i in range(1, 6)]
    data = encode_aggregate3(calls)
    body = data[10:]
    assert int(body[64:128], 16) == 5  # array length
    assert len(body) % 64 == 0         # word-aligned


def test_return_pct():
    from pytest import approx
    assert return_pct(110.0, 100.0) == approx(10.0)
    assert return_pct(95.0, 100.0) == approx(-5.0)
    assert return_pct(50.0, None) is None
    assert return_pct(50.0, 0.0) is None


def test_posture_policy():
    # ahead (top quartile) -> protect; behind -> conviction, never size
    assert "AHEAD" in posture(20.0, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
    behind = posture(-5.0, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0])
    assert "BEHIND" in behind and "NEVER more size" in behind
    assert "neutral" in posture(None, [1.0])
