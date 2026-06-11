"""Macro blackout calendar (STRATEGY §4.5)."""
from datetime import datetime, timezone
from pathlib import Path

from agent.risk.macro import MacroCalendar

CAL = """
defaults:
  high:   { pre_h: 2.0, post_h: 3.0 }
  medium: { pre_h: 1.0, post_h: 1.0 }
events:
  - name: "US PCE"
    level: high
    time_utc: "2026-06-26 12:30"
  - name: "Consumer Confidence"
    level: medium
    time_utc: "2026-06-23 14:00"
"""


def utc(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)


def calendar(tmp_path: Path, content: str = CAL) -> MacroCalendar:
    p = tmp_path / "macro_events.yaml"
    p.write_text(content, encoding="utf-8")
    return MacroCalendar(p)


def test_outside_any_window_is_clear(tmp_path):
    st = calendar(tmp_path).status(utc("2026-06-24 12:00"))
    assert st.entry_scale == 1.0 and not st.active and st.level == "none"


def test_high_window_blocks_entries_pre_and_post(tmp_path):
    cal = calendar(tmp_path)
    # T-2h .. T+3h around 12:30
    for t in ("2026-06-26 10:30", "2026-06-26 12:30", "2026-06-26 15:30"):
        st = cal.status(utc(t))
        assert st.entry_scale == 0.0 and st.level == "high"
        assert st.event and "PCE" in st.event
    # one minute outside either edge
    assert cal.status(utc("2026-06-26 10:29")).entry_scale == 1.0
    assert cal.status(utc("2026-06-26 15:31")).entry_scale == 1.0


def test_medium_window_halves_entries(tmp_path):
    st = calendar(tmp_path).status(utc("2026-06-23 14:30"))
    assert st.entry_scale == 0.5 and st.level == "medium"


def test_overlapping_windows_most_restrictive_wins(tmp_path):
    overlap = CAL + """
  - name: "Medium overlapping PCE"
    level: medium
    time_utc: "2026-06-26 12:00"
"""
    st = calendar(tmp_path, overlap).status(utc("2026-06-26 12:15"))
    assert st.entry_scale == 0.0 and st.level == "high"


def test_missing_calendar_fails_open(tmp_path):
    cal = MacroCalendar(tmp_path / "nope.yaml")
    assert cal.status(utc("2026-06-26 12:30")).entry_scale == 1.0


def test_malformed_event_skipped_others_kept(tmp_path):
    broken = """
events:
  - name: "bad"
    level: catastrophic
    time_utc: "2026-06-26 12:30"
  - name: "no time"
    level: high
  - name: "good"
    level: high
    time_utc: "2026-06-26 12:30"
"""
    st = calendar(tmp_path, broken).status(utc("2026-06-26 12:30"))
    assert st.entry_scale == 0.0 and st.event == "good"


def test_edit_picked_up_without_restart(tmp_path):
    import os

    p = tmp_path / "macro_events.yaml"
    p.write_text("events: []", encoding="utf-8")
    cal = MacroCalendar(p)
    assert cal.status(utc("2026-06-26 12:30")).entry_scale == 1.0
    p.write_text(CAL, encoding="utf-8")
    os.utime(p, (p.stat().st_atime, p.stat().st_mtime + 2))  # force mtime change
    assert cal.status(utc("2026-06-26 12:30")).entry_scale == 0.0
