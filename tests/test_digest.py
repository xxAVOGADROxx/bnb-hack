"""Ops digest (#10): aggregation, round-trip pairing, summary."""
import json
from datetime import datetime, timedelta, timezone

from agent.monitor.digest import build_digest, summary_line

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)


def write_log(path, rows):
    with open(path, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def ts(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def test_build_digest_counts_and_roundtrip_pnl(tmp_path):
    log = tmp_path / "decisions.jsonl"
    write_log(log, [
        {"ts": ts(50), "event": "regime", "regime": "conflicted"},
        {"ts": ts(50), "event": "signal", "token": "DOGE", "action": "buy", "conviction": 0.6},
        {"ts": ts(50), "event": "signal", "token": "ETH", "action": "hold"},
        {"ts": ts(49), "event": "entry_blocked", "token": "DOT", "rule": "regime_conviction_floor"},
        {"ts": ts(48), "event": "trade_rejected", "to": "FLOKI", "rule": "min_edge"},
        {"ts": ts(45), "event": "trade_executed", "from": "USDT", "to": "DOGE", "usd": 100.0},
        {"ts": ts(10), "event": "trade_executed", "from": "DOGE", "to": "USDT", "usd": 104.5},
        # outside the window -> ignored
        {"ts": ts(600), "event": "signal", "token": "XRP", "action": "buy", "conviction": 0.4},
    ])
    d = build_digest(NOW - timedelta(hours=1), NOW, decisions_path=log)
    assert d["cycles"] == 1
    assert d["signals"]["total"] == 2 and d["signals"]["by_action"]["buy"] == 1
    assert d["entries_blocked_by_rule"] == {"regime_conviction_floor": 1}
    assert d["trades_rejected_by_rule"] == {"min_edge": 1}
    assert len(d["trades_executed"]) == 2
    [rt] = d["round_trips_approx"]
    assert rt["token"] == "DOGE" and rt["pnl_usd"] == 4.5
    assert d["open_positions_unmatched"] == {}
    assert d["buy_signals_by_token"]["DOGE"]["count"] == 1


def test_unmatched_entry_reported_open(tmp_path):
    log = tmp_path / "decisions.jsonl"
    write_log(log, [
        {"ts": ts(30), "event": "trade_executed", "from": "USDT", "to": "ZEC", "usd": 50.0},
    ])
    d = build_digest(NOW - timedelta(hours=1), NOW, decisions_path=log)
    assert d["round_trips_approx"] == []
    assert d["open_positions_unmatched"] == {"ZEC": 1}


def test_summary_line_mentions_key_numbers(tmp_path):
    log = tmp_path / "decisions.jsonl"
    write_log(log, [
        {"ts": ts(45), "event": "trade_executed", "from": "USDT", "to": "DOGE", "usd": 100.0},
        {"ts": ts(10), "event": "trade_executed", "from": "DOGE", "to": "USDT", "usd": 95.0},
    ])
    d = build_digest(NOW - timedelta(hours=1), NOW, decisions_path=log)
    s = summary_line(d, portfolio={"total_usd": 5000.0})
    assert "trades 2" in s and "$-5.00" in s and "$5000.00" in s
