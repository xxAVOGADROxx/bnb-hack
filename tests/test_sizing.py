"""Volatility-targeted sizing (#2) and stop-loss entry tracking (#3)."""
from pytest import approx

from agent.signals.technical import vol_mult
from agent.state.store import StateStore


def test_vol_mult_caps_high_vol_and_never_exceeds_one():
    # at/below target -> full size (1.0); above target -> scaled down
    assert vol_mult(5.0, 5.0) == approx(1.0)
    assert vol_mult(3.0, 5.0) == approx(1.0)      # low-vol token not boosted above 1
    assert vol_mult(10.0, 5.0) == approx(0.5)     # 2x target -> half size
    assert vol_mult(20.0, 5.0) == approx(0.5)     # clamped at the floor (0.5)


def test_vol_mult_disabled_or_degenerate():
    assert vol_mult(10.0, 0.0) == 1.0             # vol_target<=0 disables
    assert vol_mult(0.0, 5.0) == 1.0              # no range info -> no scaling


def test_vol_mult_custom_floor():
    assert vol_mult(100.0, 5.0, vol_floor=0.25) == approx(0.25)


def test_entry_price_record_clear(tmp_path):
    s = StateStore(path=tmp_path / "state.json")
    assert s.entry_price("CAKE") is None
    s.record_entry("CAKE", 2.50)
    assert s.entry_price("CAKE") == approx(2.50)
    # survives a reload (persisted)
    assert StateStore(path=tmp_path / "state.json").entry_price("CAKE") == approx(2.50)
    s.clear_entry("CAKE")
    assert s.entry_price("CAKE") is None


def test_stop_loss_threshold_math():
    # an 8% stop fires at entry*0.92, not above
    entry, stop_pct = 100.0, 8.0
    trigger = entry * (1 - stop_pct / 100)
    assert trigger == approx(92.0)
    assert 91.9 <= trigger  # a price of 91 would stop; 93 would not


def test_token_exit_cooldown_clock(tmp_path):
    s = StateStore(path=tmp_path / "state.json")
    assert s.last_token_exit("CAKE") is None
    s.record_token_exit("CAKE", "2026-06-12T10:00:00+00:00")
    assert s.last_token_exit("CAKE") == "2026-06-12T10:00:00+00:00"
    # survives a reload (persisted)
    assert (StateStore(path=tmp_path / "state.json")
            .last_token_exit("CAKE")) == "2026-06-12T10:00:00+00:00"


def test_dry_run_and_live_trade_ledgers_are_separate(tmp_path):
    from datetime import datetime, timezone
    s = StateStore(path=tmp_path / "state.json")
    now = datetime(2026, 6, 22, 12, 0, tzinfo=timezone.utc)
    # a dry-run trade must NOT count toward the live compliance gate
    s.record_trade(now, dry_run=True)
    assert s.trades_today(now, dry_run=True) == 1
    assert s.trades_today(now, dry_run=False) == 0   # live ledger untouched
    s.record_trade(now, dry_run=False)
    assert s.trades_today(now, dry_run=False) == 1
    assert s.trades_today(now, dry_run=True) == 1     # independent
