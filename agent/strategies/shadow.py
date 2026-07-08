"""Shadow books — paper-trade EVERY registered strategy alongside the live loop.

The live cycle already fetches closes/volumes per token; evaluating the four
inactive plugins on that same data is free CPU, so each live cycle grows a
track record for all five strategies at the cost of running one. That
multiplies learning per unit time: trend trades ~8x/month, so validating a
challenger from live fills alone would take months — the shadow books answer
"what would X have done here?" continuously instead.

Honesty rules (same spirit as the backtest harnesses):
- Entries pass the SAME gates as the live path, in the same order: regime
  scale, regime conviction floor, per-strategy re-entry cooldown, edge floor
  (global minimum AND the token's measured friction floor), volume confirm,
  max_concurrent. Same sizing formula on a virtual $1000 book per strategy.
- Fills at the cycle price, charged the token's MEASURED round-trip friction
  (data/liquidity_report.json) half per leg — shadow pays what the live
  executor measured, not zero.
- Exits mirror live: the plugin's SELL signal, plus the fixed stop-loss.
- Known gaps vs live: no slippage-vs-quote, no failed swaps, the stop sees
  the hourly close once per cycle (live checks a ~1min quote), and equity is
  marked at cost for tokens other than the one being cycled. Treat small
  edges between books as noise.
- CALIBRATION ONLY (house rule): shadow books never touch the live path.
  observe() is fail-open — any error is logged and swallowed.

State persists to data/shadow_books.json across restarts. Events land in
decisions.jsonl as shadow_open / shadow_close; scripts/shadow_race.py turns
them into a per-strategy scoreboard.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from agent.config import DATA_DIR
from agent.signals import technical
from agent.strategies import registry
from agent.strategies.base import MarketContext, Strategy

log = logging.getLogger(__name__)

START_EQUITY = 1000.0
DEFAULT_FRICTION_PCT = 1.0  # round trip, when a token has no measured cost
MIN_TICKET_USD = 10.0       # mirrors the live below_min_size skip


def _load_frictions() -> dict[str, float]:
    """Measured round-trip cost per token from the liquidity report (the same
    file the live edge floors come from). Missing file -> empty dict and every
    token falls back to DEFAULT_FRICTION_PCT."""
    try:
        liq = json.loads((DATA_DIR / "liquidity_report.json").read_text())
        return {r["symbol"]: float(r["round_trip_cost_pct"])
                for r in liq.get("results", []) if "round_trip_cost_pct" in r}
    except (OSError, ValueError, KeyError):
        log.warning("shadow books: no liquidity report — using %.1f%% friction",
                    DEFAULT_FRICTION_PCT)
        return {}


class ShadowBooks:
    def __init__(self, risk_cfg, floors, decisions,
                 strategies: dict[str, Strategy] | None = None,
                 path: Path | None = None,
                 frictions: dict[str, float] | None = None):
        """`floors` is a zero-arg callable returning the live per-token edge
        floors (margin included) — a callable because the loop rebinds the
        dict when friction is re-measured."""
        self.cfg = risk_cfg
        self.floors = floors
        self.decisions = decisions
        self.strategies = strategies or {
            name: registry.build(name) for name in registry.available()}
        self.path = path or DATA_DIR / "shadow_books.json"
        self.frictions = _load_frictions() if frictions is None else frictions
        self.state = self._load_state()

    # -- persistence -----------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            state = json.loads(self.path.read_text())
        except (OSError, ValueError):
            state = {}
        books = state.get("books", {})
        cash = state.get("cash", {})
        cooldowns = state.get("cooldowns", {})
        for name in self.strategies:
            books.setdefault(name, {})
            cash.setdefault(name, START_EQUITY)
            cooldowns.setdefault(name, {})
        return {"books": books, "cash": cash, "cooldowns": cooldowns}

    def _save_state(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1))
        os.replace(tmp, self.path)

    # -- cycle hook --------------------------------------------------------------
    def observe(self, token: str, closes: list[float], volumes: list[float],
                price: float, scale: float, conviction_floor: float) -> None:
        """Once per token per cycle, after the live signal. Never raises."""
        try:
            self._observe(token, closes, volumes, price, scale, conviction_floor)
        except Exception:  # noqa: BLE001 — shadow must never break the live cycle
            log.exception("shadow books: observe(%s) failed (ignored)", token)

    def _observe(self, token, closes, volumes, price, scale, conviction_floor):
        if price <= 0:
            return
        dirty = False
        for name, strat in self.strategies.items():
            book = self.state["books"][name]
            holding = token in book
            try:
                sig = strat.evaluate(MarketContext(token, closes, volumes, holding))
            except Exception:  # noqa: BLE001 — one bad plugin can't starve the rest
                log.exception("shadow books: %s.evaluate(%s) failed (ignored)",
                              name, token)
                continue
            if holding:
                pos = book[token]
                stop = self.cfg.stop_loss_pct
                if stop > 0 and price <= pos["entry_px"] * (1 - stop / 100):
                    loss = (price / pos["entry_px"] - 1) * 100
                    self._close(name, token, price, f"stop-loss {loss:.1f}%")
                    dirty = True
                elif sig.action == technical.Action.SELL:
                    self._close(name, token, price, sig.reason)
                    dirty = True
            elif (sig.action == technical.Action.BUY
                  and self._entry_clears_gates(name, token, sig, volumes,
                                               scale, conviction_floor)):
                self._open(name, token, price, sig, scale)
                dirty = True
        if dirty:
            self._save_state()

    # -- gates (live order: regime -> cooldown -> edge floor -> volume -> cap) ---
    def _entry_clears_gates(self, name, token, sig, volumes,
                            scale, conviction_floor) -> bool:
        if scale <= 0.0 or sig.conviction < conviction_floor:
            return False
        last = self.state["cooldowns"][name].get(token)
        if last and self.cfg.reentry_cooldown_h > 0:
            age_h = (datetime.now(timezone.utc)
                     - datetime.fromisoformat(last)).total_seconds() / 3600
            if age_h < self.cfg.reentry_cooldown_h:
                return False
        floor = max(self.cfg.min_expected_edge_pct,
                    (self.floors() or {}).get(token, 0.0))
        if sig.expected_move_pct < floor:
            return False
        if not technical.volume_confirms(volumes, self.cfg.vol_confirm_lookback,
                                         self.cfg.vol_confirm_ratio):
            return False
        return len(self.state["books"][name]) < self.cfg.max_concurrent

    # -- virtual fills -------------------------------------------------------------
    def _leg(self, token: str) -> float:
        return self.frictions.get(token, DEFAULT_FRICTION_PCT) / 2 / 100

    def _equity(self, name: str) -> float:
        """Cash + open positions at COST (this call has no fresh marks for
        other tokens; entry marks keep sizing stable and comparable)."""
        return (self.state["cash"][name]
                + sum(p["usd"] for p in self.state["books"][name].values()))

    def _open(self, name, token, price, sig, scale) -> None:
        vmult = technical.vol_mult(
            sig.daily_range_pct, self.cfg.vol_target_pct, self.cfg.vol_floor)
        usd = (self._equity(name) * self.cfg.max_position_pct / 100
               * scale * sig.conviction * vmult)
        cash = self.state["cash"][name]
        if usd < MIN_TICKET_USD or usd > cash:
            return
        leg = self._leg(token)
        self.state["cash"][name] = cash - usd
        self.state["books"][name][token] = {
            "entry_px": price, "usd": usd, "qty": usd / price * (1 - leg),
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        self.decisions.append(
            "shadow_open", strategy=name, token=token, px=price,
            usd=round(usd, 2), conviction=round(sig.conviction, 2),
            reason=sig.reason)

    def _close(self, name, token, price, reason) -> None:
        pos = self.state["books"][name].pop(token)
        leg = self._leg(token)
        out = pos["qty"] * price * (1 - leg)
        self.state["cash"][name] += out
        now = datetime.now(timezone.utc)
        self.state["cooldowns"][name][token] = now.isoformat()
        held_h = (now - datetime.fromisoformat(pos["ts"])).total_seconds() / 3600
        self.decisions.append(
            "shadow_close", strategy=name, token=token,
            entry_px=pos["entry_px"], exit_px=price,
            gross_pct=round((price / pos["entry_px"] - 1) * 100, 3),
            net_pct=round((out / pos["usd"] - 1) * 100, 3),
            net_usd=round(out - pos["usd"], 2),
            held_h=round(held_h, 1), reason=reason,
            equity=round(self._equity(name), 2))
