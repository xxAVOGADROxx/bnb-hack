"""Housekeeping: 7-day retention on reports and JSONL logs, archives are
lossless and readable, malformed lines survive, always fail-open."""
import gzip
import json
import os
from datetime import datetime, timedelta, timezone

from agent.state.housekeep import housekeep, prune_reports, rotate_jsonl

NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


def jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def test_prune_reports_by_age(tmp_path):
    old, new = tmp_path / "old.json", tmp_path / "new.json"
    old.write_text("{}")
    new.write_text("{}")
    past = (NOW - timedelta(days=9)).timestamp()
    os.utime(old, (past, past))
    assert prune_reports(tmp_path, NOW) == 1
    assert new.exists() and not old.exists()


def test_rotate_moves_old_lines_to_gzip_archive(tmp_path):
    p = tmp_path / "decisions.jsonl"
    jsonl(p, [{"ts": iso(10), "event": "old1"}, {"ts": iso(8), "event": "old2"},
              {"ts": iso(1), "event": "recent"}])
    kept, archived = rotate_jsonl(p, NOW)
    assert (kept, archived) == (1, 2)
    live = [json.loads(x) for x in p.read_text().splitlines()]
    assert [r["event"] for r in live] == ["recent"]
    with gzip.open(tmp_path / "decisions.archive.jsonl.gz", "rt") as f:
        arch = [json.loads(x) for x in f.read().splitlines()]
    assert [r["event"] for r in arch] == ["old1", "old2"]


def test_rotate_appends_concatenated_gzip_members(tmp_path):
    p = tmp_path / "decisions.jsonl"
    jsonl(p, [{"ts": iso(10), "event": "a"}])
    rotate_jsonl(p, NOW)
    jsonl(p, [{"ts": iso(9), "event": "b"}, {"ts": iso(0), "event": "live"}])
    rotate_jsonl(p, NOW)
    with gzip.open(tmp_path / "decisions.archive.jsonl.gz", "rt") as f:
        events = [json.loads(x)["event"] for x in f.read().splitlines()]
    assert events == ["a", "b"]  # both members readable in order


def test_rotate_keeps_malformed_and_tsless_lines_live(tmp_path):
    p = tmp_path / "decisions.jsonl"
    p.write_text('not json at all\n{"event": "no_ts"}\n'
                 + json.dumps({"ts": iso(10), "event": "old"}) + "\n")
    kept, archived = rotate_jsonl(p, NOW)
    assert (kept, archived) == (2, 1)
    assert "not json at all" in p.read_text()


def test_rotate_noop_when_nothing_old(tmp_path):
    p = tmp_path / "decisions.jsonl"
    jsonl(p, [{"ts": iso(1), "event": "recent"}])
    assert rotate_jsonl(p, NOW) == (1, 0)
    assert not (tmp_path / "decisions.archive.jsonl.gz").exists()


def test_housekeep_full_pass_and_fail_open(tmp_path):
    (tmp_path / "reports").mkdir()
    jsonl(tmp_path / "decisions.jsonl", [{"ts": iso(30), "event": "ancient"}])
    housekeep(NOW, data_dir=tmp_path)
    assert (tmp_path / "decisions.archive.jsonl.gz").exists()
    # missing dir/files and even a file where a dir is expected: never raises
    housekeep(NOW, data_dir=tmp_path / "nonexistent")
