from agent.x402 import ledger


def test_records_and_summarizes(tmp_path):
    p = tmp_path / "payments.jsonl"
    ledger.record_charge(0.01, "USD1", "0xabc", "/leaderboard", "0xtx1", path=p)
    ledger.record_charge(0.01, "USD1", "0xdef", "/report", "0xtx2", path=p)
    ledger.record_spend(0.01, "BSC", "cmc", "premium:ta", "0xtx3", path=p)

    s = ledger.summarize(path=p)
    assert s["charges"] == 2
    assert s["spends"] == 1
    assert round(s["charged"], 2) == 0.02
    assert round(s["spent"], 2) == 0.01
    assert round(s["net"], 2) == 0.01


def test_window_filter(tmp_path):
    p = tmp_path / "payments.jsonl"
    p.write_text(
        '{"ts": "2026-06-22T00:00:00+00:00", "dir": "in", "amount": 0.01}\n'
        '{"ts": "2026-06-28T00:00:00+00:00", "dir": "in", "amount": 0.01}\n'
    )
    s = ledger.summarize(since="2026-06-25T00:00:00+00:00", path=p)
    assert s["charges"] == 1


def test_missing_and_corrupt_lines_are_safe(tmp_path):
    p = tmp_path / "payments.jsonl"
    assert ledger.summarize(path=p) == {
        "charges": 0, "charged": 0.0, "spends": 0, "spent": 0.0, "net": 0.0}
    p.write_text('not json\n{"ts":"2026-06-22T00:00:00+00:00","dir":"out","amount":0.01}\n')
    assert ledger.summarize(path=p)["spends"] == 1
