"""Shadow books: paper-trades every plugin with live gates, measured
friction, persistent state and a fail-open observe()."""
from types import SimpleNamespace

from agent.signals.technical import Action, Signal
from agent.strategies.shadow import START_EQUITY, ShadowBooks


def sig(action, conviction=0.8, edge=5.0, dr=4.0):
    return Signal("TOK", action, conviction, False, edge, "scripted",
                  daily_range_pct=dr)


class Scripted:
    """Strategy stub returning a queue of signals (last one repeats)."""
    name = "scripted"

    def __init__(self, *signals):
        self.queue = list(signals)
        self.contexts = []

    def evaluate(self, ctx):
        self.contexts.append(ctx)
        return self.queue.pop(0) if len(self.queue) > 1 else self.queue[0]


class Exploding:
    name = "exploding"

    def evaluate(self, ctx):
        raise RuntimeError("boom")


class Log:
    def __init__(self):
        self.events = []

    def append(self, event, **fields):
        self.events.append({"event": event, **fields})


CFG = SimpleNamespace(
    max_position_pct=10.0, max_concurrent=1, vol_target_pct=5.0, vol_floor=0.5,
    stop_loss_pct=8.0, min_expected_edge_pct=2.0, reentry_cooldown_h=6.0,
    vol_confirm_ratio=0.0, vol_confirm_lookback=24,  # ratio 0 -> confirm off
)
CLOSES = [100.0] * 50
VOLS = [1000.0] * 50


def make(tmp_path, strategies, floors=None):
    log = Log()
    books = ShadowBooks(CFG, floors=lambda: floors or {}, decisions=log,
                        strategies=strategies,
                        path=tmp_path / "shadow.json", frictions={"TOK": 1.0})
    return books, log


def test_open_then_close_with_friction(tmp_path):
    strat = Scripted(sig(Action.BUY), sig(Action.HOLD), sig(Action.SELL))
    books, log = make(tmp_path, {"s": strat})
    books.observe("TOK", CLOSES, VOLS, 100.0, 1.0, 0.5)
    assert log.events[0]["event"] == "shadow_open"
    # sizing: 1000 * 10% * scale 1 * conv 0.8 * vmult(5/4->1.0) = $80
    assert log.events[0]["usd"] == 80.0
    assert strat.contexts[0].holding is False
    books.observe("TOK", CLOSES, VOLS, 100.0, 1.0, 0.5)  # HOLD while holding
    assert strat.contexts[1].holding is True
    books.observe("TOK", CLOSES, VOLS, 110.0, 1.0, 0.5)  # SELL at +10%
    close = log.events[-1]
    assert close["event"] == "shadow_close"
    assert close["gross_pct"] == 10.0
    # net = +10% minus 1.0% measured round trip (two 0.5% legs)
    assert 8.9 < close["net_pct"] < 9.1
    assert books.state["cash"]["s"] > START_EQUITY


def test_stop_loss_closes_before_signal(tmp_path):
    strat = Scripted(sig(Action.BUY), sig(Action.HOLD))
    books, log = make(tmp_path, {"s": strat})
    books.observe("TOK", CLOSES, VOLS, 100.0, 1.0, 0.5)
    books.observe("TOK", CLOSES, VOLS, 91.0, 1.0, 0.5)  # -9% < stop 8%
    assert log.events[-1]["event"] == "shadow_close"
    assert "stop-loss" in log.events[-1]["reason"]


def test_cooldown_blocks_reentry(tmp_path):
    strat = Scripted(sig(Action.BUY), sig(Action.SELL), sig(Action.BUY))
    books, log = make(tmp_path, {"s": strat})
    books.observe("TOK", CLOSES, VOLS, 100.0, 1.0, 0.5)
    books.observe("TOK", CLOSES, VOLS, 100.0, 1.0, 0.5)  # close -> cooldown
    books.observe("TOK", CLOSES, VOLS, 100.0, 1.0, 0.5)  # BUY blocked
    assert [e["event"] for e in log.events] == ["shadow_open", "shadow_close"]


def test_entry_gates(tmp_path):
    # conviction floor, regime scale 0, and the edge floor each block the BUY
    for kwargs, floors in (
        ({"scale": 1.0, "conviction_floor": 0.9}, {}),        # conv 0.8 < 0.9
        ({"scale": 0.0, "conviction_floor": 0.5}, {}),        # RISK_OFF
        ({"scale": 1.0, "conviction_floor": 0.5}, {"TOK": 9.0}),  # edge 5 < 9
    ):
        strat = Scripted(sig(Action.BUY))
        books, log = make(tmp_path, {"s": strat}, floors=floors)
        books.observe("TOK", CLOSES, VOLS, 100.0, **kwargs)
        assert log.events == []


def test_max_concurrent_across_tokens(tmp_path):
    strat = Scripted(sig(Action.BUY))
    books, log = make(tmp_path, {"s": strat})
    books.observe("TOK", CLOSES, VOLS, 100.0, 1.0, 0.5)
    books.observe("TOK2", CLOSES, VOLS, 100.0, 1.0, 0.5)  # cap 1 -> blocked
    assert len([e for e in log.events if e["event"] == "shadow_open"]) == 1


def test_state_survives_restart(tmp_path):
    strat = Scripted(sig(Action.BUY))
    books, _ = make(tmp_path, {"s": strat})
    books.observe("TOK", CLOSES, VOLS, 100.0, 1.0, 0.5)
    reborn, log2 = make(tmp_path, {"s": Scripted(sig(Action.SELL))})
    assert "TOK" in reborn.state["books"]["s"]
    reborn.observe("TOK", CLOSES, VOLS, 105.0, 1.0, 0.5)
    assert log2.events[-1]["event"] == "shadow_close"


def test_observe_is_fail_open_per_strategy(tmp_path):
    ok = Scripted(sig(Action.BUY))
    books, log = make(tmp_path, {"a_exploding": Exploding(), "ok": ok})
    books.observe("TOK", CLOSES, VOLS, 100.0, 1.0, 0.5)  # must not raise
    # the exploding plugin is isolated: 'ok' (later in dict order) still opens
    assert [e["event"] for e in log.events] == ["shadow_open"]
    assert log.events[0]["strategy"] == "ok"
