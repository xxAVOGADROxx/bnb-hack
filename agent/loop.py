"""Main deterministic loop.

One cycle:
  reconcile (on-chain truth) -> hourly snapshot + drawdown state -> regime
  -> per-token signals -> risk verdicts -> execution -> daily compliance.

The process must survive the week unattended: every cycle is wrapped, errors
alert and back off, and a restart reconciles from the chain before trading.
No LLM decides ticks — the strategy is code.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

from agent.alerts import Alerter
from agent.cmc.client import CMCClient, CMCError, usd_quote
from agent.config import DATA_DIR, AppConfig
from agent.execution.executor import Executor
from agent.logger import DecisionLog
from agent.monitor.snapshot import maybe_snapshot
from agent.risk.engine import RiskEngine, RiskState, TradeProposal
from agent.risk.liquidity import LiquiditySentinel
from agent.risk.macro import MacroCalendar
from agent.signals import regime as regime_mod
from agent.signals import technical
from agent.state.reconcile import reconcile
from agent.state.store import StateStore
from agent.tokens import TokenRegistry
from agent.twak.client import make_twak_client
from agent.x402 import premium as x402

log = logging.getLogger(__name__)

# Exposure scale per regime: governs how much of the position cap a new
# entry may use. TODO(tune): backtest.
REGIME_SCALE = {
    regime_mod.Regime.RISK_ON: 1.0,
    regime_mod.Regime.CONFLICTED: 0.5,
    regime_mod.Regime.RISK_OFF: 0.0,
}


class Agent:
    def __init__(self, cfg: AppConfig, paper_equity: float = 0.0):
        self.cfg = cfg
        # Dry-run only: pretend the portfolio is this big for entry SIZING, so
        # a test window exercises proposal -> risk engine -> quote end to end
        # (with tiny test capital, every entry dies at the $10 floor instead).
        # Drawdown, snapshots and exits always use the real on-chain value.
        self.paper_equity = paper_equity if cfg.dry_run else 0.0
        self.cmc = CMCClient(cfg.cmc_api_key)
        self.twak = make_twak_client(chain=cfg.chain, dry_run=cfg.dry_run)
        self.store = StateStore()
        self.decisions = DecisionLog()
        self.risk = RiskEngine(cfg.risk, cfg.tokens)
        self.alerter = Alerter(cfg.telegram_bot_token, cfg.telegram_chat_id)
        self.registry = TokenRegistry()
        self.macro = MacroCalendar()
        self.sentinel = LiquiditySentinel(
            self.cmc, self.store,
            min_ref_usd=cfg.risk.liquidity_min_ref_usd,
            exit_drop_pct=cfg.risk.liquidity_exit_drop_pct,
        ) if cfg.risk.liquidity_exit_drop_pct > 0 else None
        # Per-token edge floor (#9): an entry must clear the token's own
        # MEASURED round-trip friction + margin, not just the global min.
        self.edge_floors: dict[str, float] = {}
        if cfg.risk.edge_floor_margin_pct > 0:
            try:
                liq = json.loads((DATA_DIR / "liquidity_report.json").read_text())
                self.edge_floors = {
                    r["symbol"]: r["round_trip_cost_pct"] + cfg.risk.edge_floor_margin_pct
                    for r in liq.get("results", [])}
            except (OSError, ValueError, KeyError):
                log.warning("no liquidity report — per-token edge floor disabled")
        self.executor = Executor(
            self.twak, self.risk, self.store, self.decisions,
            cfg.risk.max_slippage_pct, registry=self.registry, alerter=self.alerter,
        )
        self._stop = False           # set by SIGTERM/SIGINT for a clean shutdown
        self._last_heartbeat_hour = -1

    # -- lifecycle -----------------------------------------------------------
    def request_stop(self, *_a) -> None:
        """Signal handler: finish the in-flight cycle, then exit cleanly.
        Swaps are synchronous (twak blocks until the tx confirms), so a stop
        between cycles never leaves a pending transaction behind."""
        log.info("stop requested — will exit after the current cycle")
        self._stop = True

    def run(self, once: bool = False, max_hours: float | None = None) -> None:
        mode = "LIVE" if not self.cfg.dry_run else "dry-run"
        log.info("agent starting (%s, chain=%s)", mode, self.cfg.chain)
        # Bootstrap the symbol->id->address caches if a fresh clone has none
        # (data/ is gitignored; these are public, regenerable reference data).
        universe = [*self.cfg.tokens.watchlist, *self.cfg.tokens.stables]
        self.registry.ensure_id_map(self.cmc, universe)
        # Contract addresses are mandatory for execution — resolve up front.
        self.registry.ensure_addresses(self.cmc, universe)
        deadline = None
        window = ""
        if max_hours:
            deadline = datetime.now(timezone.utc) + timedelta(hours=max_hours)
            window = f", {max_hours:g}h window"
        self.alerter.notify(
            f"🤖 agent online ({mode}, BSC{window}) — supervising "
            f"{len(self.cfg.tokens.watchlist)} tokens"
        )
        consecutive_errors = 0
        while not self._stop:
            if deadline and datetime.now(timezone.utc) >= deadline:
                log.info("window elapsed — stopping cleanly")
                break
            try:
                self.cycle()
                consecutive_errors = 0
            except Exception as e:  # never let one bad cycle kill the week
                consecutive_errors += 1
                log.exception("cycle failed (%d in a row)", consecutive_errors)
                self.decisions.append("cycle_error", error=str(e))
                if consecutive_errors >= 3:
                    self.alerter.notify(f"agent: {consecutive_errors} consecutive cycle errors: {e}")
            if once:
                return
            slept = 0
            step = self.cfg.risk.cycle_interval_s * min(consecutive_errors + 1, 6)
            while slept < step and not self._stop:  # responsive to stop signals
                time.sleep(min(2, step - slept))
                slept += 2
        self.alerter.notify("🛑 agent stopped cleanly — no pending transactions")
        log.info("agent stopped cleanly")

    # -- canary (pre-week live validation) -----------------------------------
    def flatten(self) -> None:
        """One-shot: close every non-stable position into USDT and exit.
        For the end of the competition window (lock the measured result in
        stables) or any emergency de-risk. Honors dry-run."""
        self.registry.ensure_id_map(self.cmc, [*self.cfg.tokens.watchlist, *self.cfg.tokens.stables])
        self.registry.ensure_addresses(self.cmc, [*self.cfg.tokens.watchlist, *self.cfg.tokens.stables])
        portfolio = reconcile(self.twak, self.cmc, self.cfg.tokens, registry=self.registry)
        non_stable = {s: v for s, v in portfolio.usd_values.items()
                      if s not in self.cfg.tokens.stables and s != "BNB" and v > 1.0}
        if not non_stable:
            log.info("flatten: nothing to close (already in stables)")
            self.alerter.notify("🏁 flatten: already flat — nothing to close")
            return
        log.info("flatten: closing %s", ", ".join(f"{s} ${v:.2f}" for s, v in non_stable.items()))
        self._flatten_to_stables(portfolio, RiskState.NORMAL)
        final = reconcile(self.twak, self.cmc, self.cfg.tokens, registry=self.registry)
        self.alerter.notify(f"🏁 flattened to stables — final ${final.total_usd:.2f}")

    def canary_roundtrip(self, token: str = "CAKE", usd: float = 10.0) -> None:
        """One small REAL round-trip through the full executor path, to prove
        autonomous signing end-to-end before the live week. Buys then sells the
        same token so it ends flat. Bypasses the regime gate (TEST ONLY) but
        keeps every other guardrail; fully logged + alerted. Tiny by design —
        friction (~1.4% of $10 ≈ $0.15) is the cost of the validation."""
        self.registry.ensure_id_map(self.cmc, [*self.cfg.tokens.watchlist, *self.cfg.tokens.stables])
        self.registry.ensure_addresses(self.cmc, [*self.cfg.tokens.watchlist, *self.cfg.tokens.stables])
        stable = self.cfg.tokens.stables[0]
        self.alerter.notify(f"🐤 canary: real round-trip {stable}->{token}->{stable} ${usd:.0f} (live path test)")

        def snapshot():
            p = reconcile(self.twak, self.cmc, self.cfg.tokens, registry=self.registry)
            st = self.risk.drawdown_state(p.total_usd, p.total_usd)
            return p, st

        portfolio, state = snapshot()
        # Entry: stable -> token. edge above the min-edge floor so risk passes.
        self.executor.execute(
            TradeProposal(stable, token, usd, 3.0, True, "canary entry (live path test)"),
            portfolio_usd=portfolio.total_usd, state=state,
            open_positions=portfolio.open_positions(self.cfg.tokens.stables), signal_age_min=0.0,
        )
        # Exit: sell whatever we just acquired, back to flat.
        portfolio, state = snapshot()
        held_usd = portfolio.usd_values.get(token, 0.0)
        held_amount = portfolio.holdings.get(token)
        if held_usd > 1.0:
            self.executor.execute(
                TradeProposal(token, stable, held_usd, 0.0, False,
                              "canary exit (back to flat)", amount=held_amount),
                portfolio_usd=portfolio.total_usd, state=state,
                open_positions=portfolio.open_positions(self.cfg.tokens.stables), signal_age_min=0.0,
            )
        else:
            log.warning("canary: no %s position to unwind (held $%.2f)", token, held_usd)
        portfolio, _ = snapshot()
        self.alerter.notify(f"🐤 canary done — portfolio ${portfolio.total_usd:.2f}, flat")

    # -- one cycle --------------------------------------------------------------
    def cycle(self) -> None:
        now = datetime.now(timezone.utc)

        # 1. On-chain truth first.
        portfolio = reconcile(self.twak, self.cmc, self.cfg.tokens, registry=self.registry)
        if self.store.baseline_usd is None and portfolio.total_usd > 0:
            self.store.set_baseline(portfolio.total_usd)

        # 2. Snapshot + drawdown ladder (measured like the judge measures it).
        metrics = maybe_snapshot(self.store, portfolio.total_usd, now)
        self._maybe_heartbeat(now, portfolio, metrics)
        state = self.risk.drawdown_state(portfolio.total_usd, metrics.high_water_mark_usd)
        if state in (RiskState.ALERT, RiskState.PAUSE_ENTRIES, RiskState.HARD_STOP):
            self.alerter.notify(
                f"risk state {state.value}: drawdown {metrics.drawdown_pct:.1f}%, "
                f"portfolio ${metrics.portfolio_usd:.2f}"
            )
        if state == RiskState.HARD_STOP:
            self._flatten_to_stables(portfolio, state)
            return

        # 3. Regime gate (cached upstream; recomputed every ~20 min).
        signal_ts = datetime.now(timezone.utc)
        try:
            view = regime_mod.classify(
                self.cmc.global_metrics(ttl_s=self.cfg.risk.regime_cache_min * 60),
                self.cmc.fear_greed_latest(ttl_s=self.cfg.risk.regime_cache_min * 60),
                fear_conviction_floor=self.cfg.risk.fear_conviction_floor,
            )
        except CMCError as e:
            # Freshness gate: no data -> no new entries this cycle.
            log.warning("regime data unavailable: %s", e)
            self.decisions.append("data_gate", detail=str(e))
            return
        self.decisions.append("regime", regime=view.regime.value, detail=view.detail)

        if view.regime == regime_mod.Regime.RISK_OFF:
            log.info("RISK_OFF: managing exits only, no new entries")

        # 3b. Scheduled macro blackout (STRATEGY §4.5): restricts entries
        # only — exits, hard-stop flatten and the compliance trade still run.
        macro = self.macro.status(now)
        if macro.active:
            log.info("macro window: %s", macro.detail)
            self.decisions.append(
                "macro_blackout", level=macro.level,
                entry_scale=macro.entry_scale, detail=macro.detail,
            )
        self._trade_watchlist(portfolio, state, view, signal_ts, macro.entry_scale)

        # 4. Compliance: >=1 trade per UTC day, forced before the deadline.
        self._ensure_daily_trade(now, portfolio, state)

        # 5. Sanity: never let the portfolio approach the $1 zero-hour rule.
        if 0 < portfolio.total_usd < self.cfg.risk.min_portfolio_usd:
            self.alerter.notify(f"portfolio ${portfolio.total_usd:.2f} near $1 floor!")

    def _maybe_heartbeat(self, now: datetime, portfolio, metrics) -> None:
        """Once per UTC hour, push a balance/return line to Telegram so the
        window can be watched from the phone without reading logs."""
        if now.hour == self._last_heartbeat_hour:
            return
        self._last_heartbeat_hour = now.hour
        ret = f"{metrics.return_pct:+.2f}%" if metrics.return_pct is not None else "—"
        held = ", ".join(
            f"{s} ${v:.0f}" for s, v in sorted(
                portfolio.usd_values.items(), key=lambda x: -x[1]) if v > 1.0
        ) or "empty"
        self.alerter.notify(
            f"💰 ${portfolio.total_usd:.2f} ({ret}) | dd {metrics.drawdown_pct:.1f}% | "
            f"trades today {self.store.trades_today(now)}\n{held}"
        )

    # -- helpers -------------------------------------------------------------------
    def _trade_watchlist(
        self, portfolio, state: RiskState, view, signal_ts: datetime,
        macro_scale: float = 1.0,
    ) -> None:
        scale = REGIME_SCALE[view.regime] * macro_scale
        stable = self.cfg.tokens.stables[0]
        # paper_equity is 0 outside dry-run: live always sizes on-chain truth.
        equity = max(portfolio.total_usd, self.paper_equity)
        for token in self.cfg.tokens.watchlist:
            cmc_id = self.registry.cmc_id(token)
            if cmc_id is None:
                log.warning("no CMC id for %s — skipping", token)
                continue
            try:
                closes = self.cmc.closes_historical(cmc_id, interval="1h", count=200)
            except CMCError as e:
                log.warning("no series for %s (%s) — skipping", token, e)
                continue
            holding = portfolio.usd_values.get(token, 0.0) > 1.0
            price = float(closes[-1])
            addr = self.registry.addresses.get(token)

            # Stop-loss (#3): a hard floor below the signal exit. Track the
            # entry price (the chain can't tell us our cost basis); if a holding
            # falls past the stop, cut it now — don't wait for the EMA signal.
            if holding:
                # #6: check the stop against the LIVE quote (~1 min fresh), not
                # the hourly close (up to an hour stale) — a fast dump inside
                # the hour is caught by the next 5-min cycle.
                stop_px = price
                try:
                    live = usd_quote(
                        self.cmc.quotes_latest([cmc_id], ttl_s=60).get(cmc_id) or {}
                    ).get("price")
                    if live:
                        stop_px = float(live)
                except CMCError:
                    pass  # degrade to the hourly close
                entry_px = self.store.entry_price(token)
                if entry_px is None:
                    self.store.record_entry(token, stop_px)  # restart: adopt, no spurious stop
                elif (self.cfg.risk.stop_loss_pct > 0
                      and stop_px <= entry_px * (1 - self.cfg.risk.stop_loss_pct / 100)):
                    loss = (stop_px / entry_px - 1) * 100
                    log.info("%s stop-loss: %.1f%% from entry", token, loss)
                    self.decisions.append("stop_loss", token=token, loss_pct=round(loss, 2))
                    r = self.executor.execute(
                        TradeProposal(token, stable, portfolio.usd_values.get(token, 0.0),
                                      0.0, False, f"stop-loss {loss:.1f}%",
                                      amount=portfolio.holdings.get(token)),
                        portfolio_usd=portfolio.total_usd, state=state,
                        open_positions=portfolio.open_positions(self.cfg.tokens.stables),
                        signal_age_min=0.0,
                    )
                    if r is not None:
                        self._position_closed(token)
                    continue
                # Liquidity sentinel (#7): the pool draining is the one tail
                # risk price-based exits lag — check it independently.
                if self.sentinel and addr:
                    verdict = self.sentinel.check(token, addr)
                    if verdict and verdict.exit_now:
                        log.warning("%s liquidity sentinel: %s", token, verdict.detail)
                        self.decisions.append(
                            "liquidity_exit", token=token,
                            drop_pct=verdict.drop_pct, detail=verdict.detail)
                        r = self.executor.execute(
                            TradeProposal(token, stable,
                                          portfolio.usd_values.get(token, 0.0),
                                          0.0, False,
                                          f"liquidity drain {verdict.drop_pct:.0f}%",
                                          amount=portfolio.holdings.get(token)),
                            portfolio_usd=portfolio.total_usd, state=state,
                            open_positions=portfolio.open_positions(self.cfg.tokens.stables),
                            signal_age_min=0.0,
                        )
                        if r is not None:
                            self._position_closed(token)
                        continue
            elif self.store.entry_price(token) is not None:
                self._position_closed(token)  # position left without us seeing the exit

            sig = technical.evaluate(token, closes, holding=holding)
            self.decisions.append(
                "signal", token=token, action=sig.action.value,
                conviction=round(sig.conviction, 2), grey_zone=sig.grey_zone,
                expected_move_pct=round(sig.expected_move_pct, 2), detail=sig.reason,
            )

            premium_entry = False
            if sig.action == technical.Action.HOLD:
                # x402 tie-break: in the grey zone (3/4 conditions) under a
                # CONFLICTED regime, pay for one premium TA pull and enter
                # only on a clear bullish confirmation. Don't pay when the
                # answer couldn't be used anyway (already holding, entries
                # blocked by macro blackout / RISK_OFF: scale == 0, or the
                # regime conviction floor would reject the entry regardless).
                if (sig.grey_zone and view.regime == regime_mod.Regime.CONFLICTED
                        and scale > 0 and not holding
                        and sig.conviction >= view.entry_conviction_floor):
                    premium_entry = x402.tie_break(self.twak, self.decisions, token, cmc_id)
                if not premium_entry:
                    continue
                log.info("%s grey zone: premium confirmed -> entry at half conviction", token)

            is_entry = sig.action == technical.Action.BUY or premium_entry
            if is_entry and (holding or scale == 0.0):
                continue
            # Asymmetric regime gate (#4): under extreme fear only top-
            # conviction setups may enter (at the halved CONFLICTED scale).
            if is_entry and sig.conviction < view.entry_conviction_floor:
                self.decisions.append(
                    "entry_blocked", token=token, rule="regime_conviction_floor",
                    conviction=round(sig.conviction, 2),
                    floor=view.entry_conviction_floor)
                continue
            # Anti-whipsaw (#9): a freshly closed token can't be re-entered
            # the same day, and the edge must clear the token's OWN measured
            # friction (+margin), not just the global minimum.
            if is_entry and self._in_cooldown(token):
                self.decisions.append(
                    "entry_blocked", token=token, rule="reentry_cooldown",
                    last_exit=self.store.last_token_exit(token))
                continue
            tok_floor = self.edge_floors.get(token, 0.0)
            if is_entry and sig.expected_move_pct < tok_floor:
                self.decisions.append(
                    "entry_blocked", token=token, rule="edge_floor",
                    edge=round(sig.expected_move_pct, 2), floor=round(tok_floor, 2))
                continue
            if is_entry:
                # Sizing = position cap x regime scale x conviction (#1) x the
                # volatility-targeting multiplier (#2, risk parity).
                vmult = technical.vol_mult(
                    sig.daily_range_pct, self.cfg.risk.vol_target_pct, self.cfg.risk.vol_floor)
                usd = (equity * self.cfg.risk.max_position_pct / 100
                       * scale * sig.conviction * vmult)
            else:
                usd = portfolio.usd_values.get(token, 0.0)
            if usd < 10.0:
                if is_entry:  # visible, not a silent skip (tiny capital lands here)
                    self.decisions.append(
                        "entry_skipped", token=token, rule="below_min_size",
                        usd=round(usd, 2))
                continue
            proposal = TradeProposal(
                from_token=stable if is_entry else token,
                to_token=token if is_entry else stable,
                usd=usd,
                expected_edge_pct=sig.expected_move_pct,
                is_entry=is_entry,
                reason=sig.reason,
                # Exits sell the exact on-chain token amount (avoids over-sell revert).
                amount=None if is_entry else portfolio.holdings.get(token),
            )
            age_min = (datetime.now(timezone.utc) - signal_ts).total_seconds() / 60
            r = self.executor.execute(
                proposal,
                portfolio_usd=equity,  # == real total outside dry-run
                state=state,
                open_positions=portfolio.open_positions(self.cfg.tokens.stables),
                signal_age_min=age_min,
            )
            if r is not None:  # track cost basis (stop-loss) + pool baseline (#7)
                if is_entry:
                    self.store.record_entry(token, price)
                    if self.sentinel and addr:
                        self.sentinel.on_entry(token, addr)
                else:
                    self._position_closed(token)

    def _position_closed(self, token: str) -> None:
        """Bookkeeping when a position closes: forget cost basis + pool
        baseline, start the re-entry cooldown clock (#9)."""
        self.store.clear_entry(token)
        self.store.record_token_exit(token, datetime.now(timezone.utc).isoformat())
        if self.sentinel:
            self.sentinel.clear(token)

    def _in_cooldown(self, token: str) -> bool:
        if self.cfg.risk.reentry_cooldown_h <= 0:
            return False
        last = self.store.last_token_exit(token)
        if not last:
            return False
        age_h = (datetime.now(timezone.utc)
                 - datetime.fromisoformat(last)).total_seconds() / 3600
        return age_h < self.cfg.risk.reentry_cooldown_h

    def _flatten_to_stables(self, portfolio, state: RiskState) -> None:
        """Hard stop: close every non-stable position into the primary stable."""
        self.alerter.notify("HARD STOP: flattening to stables")
        for sym, usd in portfolio.usd_values.items():
            # BNB is the gas reserve (valued, but not eligible/tradable):
            # never propose selling it — the allowlist would reject it anyway.
            if sym in self.cfg.tokens.stables or sym == "BNB" or usd <= 1.0:
                continue
            self.executor.execute(
                TradeProposal(sym, self.cfg.tokens.stables[0], usd, 0.0, False,
                              "hard_stop flatten", amount=portfolio.holdings.get(sym)),
                portfolio_usd=portfolio.total_usd,
                state=state,
                open_positions=portfolio.open_positions(self.cfg.tokens.stables),
                signal_age_min=0.0,
            )

    def _ensure_daily_trade(self, now: datetime, portfolio, state: RiskState) -> None:
        """>=1 trade per UTC day is a qualification constraint, not alpha:
        if nothing traded by the deadline, do a minimal stable<->stable swap."""
        if self.store.trades_today(now) > 0:
            return
        hh, mm = map(int, self.cfg.risk.daily_trade_deadline_utc.split(":"))
        if (now.hour, now.minute) < (hh, mm):
            return
        log.info("no trade yet today; executing compliance trade")
        s = self.cfg.tokens.stables
        if len(s) < 2:
            self.alerter.notify("compliance trade impossible: need two stables configured")
            return
        self.executor.execute(
            TradeProposal(s[0], s[1], 10.0, 0.0, False, "daily compliance trade"),
            portfolio_usd=portfolio.total_usd,
            state=state,
            open_positions=portfolio.open_positions(s),
            signal_age_min=0.0,
        )
