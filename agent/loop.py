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
import os
import time
from datetime import datetime, timedelta, timezone

from agent.alerts import Alerter
from agent.market.derivatives import DerivFeed
from agent.market.dex import DexFeed
from agent.market.feed import FeedError, MarketFeed, usd_quote
from agent.config import DATA_DIR, AppConfig
from agent.execution.executor import Executor
from agent.logger import DecisionLog
from agent.monitor import digest as digest_mod
from agent.monitor.snapshot import maybe_snapshot
from agent.risk.engine import RiskEngine, RiskState, TradeProposal
from agent.risk.liquidity import LiquiditySentinel
from agent.risk.macro import MacroCalendar
from agent.signals import regime as regime_mod
from agent.signals import technical
from agent.state.reconcile import reconcile
from agent.state import roundtrips
from agent.strategies import registry as strategy_registry
from agent.strategies.base import MarketContext
from agent.state.store import StateStore
from agent.tokens import TokenRegistry
from agent.twak.client import make_twak_client

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
        self.registry = TokenRegistry()
        # Market data (signals + regime) now comes from FREE keyless public
        # endpoints (Binance klines, alternative.me F&G, CoinGecko dominance)
        # instead of the paid CMC key that expired 2026-07-03 and, valuing the
        # whole book at $0, tripped a false HARD STOP. Holdings VALUATION is not
        # done here — it is on-chain via self._pricer (see below).
        self.feed = MarketFeed(self.registry)
        self.twak = make_twak_client(chain=cfg.chain, dry_run=cfg.dry_run)
        self.store = StateStore()
        self.decisions = DecisionLog()
        self.risk = RiskEngine(cfg.risk, cfg.tokens)
        self.alerter = Alerter(cfg.telegram_bot_token, cfg.telegram_chat_id)
        # Execution backend: quote/swap DIRECTLY through PancakeSwap V3 instead
        # of TWAK's 0x/LiquidMesh aggregation. Measured round-trip drops from
        # ~1.7% (pure aggregator markup) to ~0.1-0.9% (LP fee only) — the
        # difference between the momentum edge surviving friction or not.
        # Balances and native-BNB legs still delegate to TWAK. DEFAULT since
        # 2026-07-05 (it has been the live path all along); EXEC_BACKEND=twak
        # opts back out to the aggregator.
        if os.environ.get("EXEC_BACKEND", "pancake").lower() == "pancake":
            from agent.execution.pancake import make_pancake_client
            self.twak = make_pancake_client(
                self.twak, self.registry, chain=cfg.chain, dry_run=cfg.dry_run)
            log.info("execution backend: PancakeSwap V3 direct (quote/swap)")
        # Valuation source: on-chain via the execution client when it can price
        # (PancakeSwap). Keeps the mark equal to realizable value and immune to
        # any off-chain feed outage. None -> reconcile falls back to the free
        # feed's quotes (self.feed), used by scripts/dry-run without a pancake backend.
        self._pricer = self.twak if hasattr(self.twak, "price_usd") else None
        self.strategy = strategy_registry.build(cfg.strategy)  # active signal plugin
        log.info("strategy: %s (available: %s)",
                 self.strategy.name, ", ".join(strategy_registry.available()))
        self.macro = MacroCalendar()
        # On-chain venue eyes (F1): aggregated PancakeSwap pool depth + order
        # flow from free keyless APIs (DexScreener). Feeds the sentinel and
        # the shadow "dex_flow" decision records; strictly fail-open.
        self.dex = DexFeed()
        # Perp positioning eyes (F2): OKX funding/OI/liquidations per token,
        # shadow "deriv_view" records only — the squeeze fingerprint gates
        # nothing until scripts/squeeze_bt.py earns it a rule. Fail-open.
        self.derivs = DerivFeed()
        # Liquidity sentinel (#7): pool-drain / rug exit. Depth = aggregated
        # PancakeSwap V2+V3 liquidity (self.dex), falling back to the direct
        # on-chain v2 getReserves read (agent/chain.py) if the API is down.
        self.sentinel = LiquiditySentinel(
            self.store,
            min_ref_usd=cfg.risk.liquidity_min_ref_usd,
            exit_drop_pct=cfg.risk.liquidity_exit_drop_pct,
            dex=self.dex,
        ) if cfg.risk.liquidity_exit_drop_pct > 0 else None
        # Per-token edge floor (#9): an entry must clear the token's own
        # MEASURED round-trip friction + margin, not just the global min.
        self.edge_floors: dict[str, float] = {}
        self._friction_baseline: float | None = None
        if cfg.risk.edge_floor_margin_pct > 0:
            self._friction_baseline = self._load_edge_floors(announce_missing=not cfg.dry_run)
        # Self-correcting friction refresh (trading-week swap-fee waiver capture):
        # every FLOOR_REFRESH_H hours re-measure round-trip cost; on a material
        # drop vs this boot baseline (the waiver going live) exit cleanly so the
        # docker restart policy relaunches and the entrypoint re-measures at the
        # new pricing and reloads the FULL universe. 0/unset disables (default),
        # so bare `python -m agent` never self-exits — only the docker live path
        # sets it. See deploy/entrypoint.sh + docker-compose.yml.
        self._floor_refresh_h = float(os.environ.get("FLOOR_REFRESH_H", "0") or 0)
        self._next_floor_check: datetime | None = None
        self.executor = Executor(
            self.twak, self.risk, self.store, self.decisions,
            cfg.risk.max_slippage_pct, registry=self.registry, alerter=self.alerter,
        )
        self._stop = False           # set by SIGTERM/SIGINT for a clean shutdown
        self._last_heartbeat_hour = -1
        self._lb = None              # lazy read-only leaderboard monitor
        self._last_report: datetime | None = None

    # -- lifecycle -----------------------------------------------------------
    def request_stop(self, *_a) -> None:
        """Signal handler: finish the in-flight cycle, then exit cleanly.
        Swaps are synchronous (twak blocks until the tx confirms), so a stop
        between cycles never leaves a pending transaction behind."""
        log.info("stop requested — will exit after the current cycle")
        self._stop = True

    def _load_edge_floors(self, announce_missing: bool = False) -> float | None:
        """(Re)load per-token edge floors from data/liquidity_report.json and
        return the MEAN round-trip cost (for waiver detection), or None if the
        report is absent. data/liquidity_report.json is a private per-server
        file (it reveals the watchlist), generated by scripts/liquidity_filter.
        Missing it disables the per-token edge floor (#9) — the cooldown still
        applies; in LIVE mode that's worth shouting."""
        try:
            liq = json.loads((DATA_DIR / "liquidity_report.json").read_text())
            results = liq.get("results", [])
            self.edge_floors = {
                r["symbol"]: r["round_trip_cost_pct"] + self.cfg.risk.edge_floor_margin_pct
                for r in results if "round_trip_cost_pct" in r}
            costs = [r["round_trip_cost_pct"] for r in results if "round_trip_cost_pct" in r]
            return sum(costs) / len(costs) if costs else None
        except (OSError, ValueError, KeyError):
            msg = "no liquidity report — per-token edge floor DISABLED"
            log.warning(msg)
            if announce_missing:
                self.alerter.notify(f"⚠️ {msg} (run scripts/liquidity_filter.py)")
            return None

    def _maybe_refresh_floors(self) -> bool:
        """Trading-week waiver capture. Re-measure round-trip friction at the
        real per-position size; if the mean dropped materially vs the boot
        baseline (the swap-fee waiver went live), return True to request a clean
        exit -> docker restart -> entrypoint re-measure + FULL universe reload.
        Otherwise reload floors in place and return False. Never raises: a
        measurement hiccup keeps the existing floors and trading continues."""
        import subprocess
        import sys
        size = os.environ.get("FILTER_SIZE_USD", "750")
        max_cost = os.environ.get("WATCHLIST_MAX_COST_PCT", "1.5")
        try:
            subprocess.run(
                [sys.executable, "scripts/liquidity_filter.py", "--size-usd", size,
                 "--max-cost-pct", max_cost],
                check=True, capture_output=True, text=True, timeout=600)
        except Exception as e:  # noqa: BLE001 — re-measure must never kill trading
            log.warning("floor re-measure failed (keeping current floors): %s", e)
            return False
        new_mean = self._load_edge_floors()
        base = self._friction_baseline
        # ~60% cheaper at the waiver (0.7%->0.077%/leg); 0.7x clears quote noise.
        if new_mean is not None and base and new_mean < base * 0.7:
            self.alerter.notify(
                f"💸 swap-fee waiver detected (mean round-trip {base:.2f}%→{new_mean:.2f}%) "
                "— restarting to re-measure floors and widen the watchlist")
            return True
        if new_mean is not None:
            log.info("floors re-measured (mean round-trip %.2f%%, baseline %.2f%%)",
                     new_mean, base or 0.0)
        return False

    def run(self, once: bool = False, max_hours: float | None = None,
            start_at: datetime | None = None, stop_at: datetime | None = None,
            report_every_min: float = 0.0) -> None:
        mode = "LIVE" if not self.cfg.dry_run else "dry-run"
        log.info("agent starting (%s, chain=%s)", mode, self.cfg.chain)
        # Sanity-check the persisted symbol->id->address maps up front (they
        # are hand-maintained post-CMC; a missing entry means that token can't
        # signal or trade, so shout at boot rather than every cycle).
        universe = [*self.cfg.tokens.watchlist, *self.cfg.tokens.stables]
        self.registry.ensure_id_map(self.feed, universe)
        self.registry.ensure_addresses(self.feed, universe)

        # Scheduled window (exact UTC): sleep until start_at, stop at stop_at.
        # Everything in UTC — no local-timezone arithmetic, ever.
        now = datetime.now(timezone.utc)
        waited_for_window = False
        if stop_at and now >= stop_at:
            # Restarted after the window already ended (docker restart policy):
            # exit quietly — no trading, no Telegram spam.
            log.info("window already over (%s) — exiting", stop_at.isoformat())
            time.sleep(60)  # damp the docker restart loop
            return
        if start_at and now < start_at:
            waited_for_window = True
            self.alerter.notify(
                f"⏳ scheduled: trading starts {start_at:%Y-%m-%d %H:%M} UTC "
                f"(in {(start_at - now).total_seconds() / 3600:.1f}h) — waiting")
            while not self._stop and datetime.now(timezone.utc) < start_at:
                time.sleep(2)
            if self._stop:
                self.alerter.notify("🛑 stopped while waiting for window start")
                return
            self.alerter.notify("🚀 window open — trading loop starting NOW")

        deadlines = []
        window = ""
        if max_hours:
            deadlines.append(datetime.now(timezone.utc) + timedelta(hours=max_hours))
            window = f", {max_hours:g}h window"
        if stop_at:
            deadlines.append(stop_at)
            window = f", until {stop_at:%Y-%m-%d %H:%M} UTC"
        deadline = min(deadlines) if deadlines else None
        self.alerter.notify(
            f"🤖 agent online ({mode}, BSC{window}) — supervising "
            f"{len(self.cfg.tokens.watchlist)} tokens"
        )
        self._last_report = datetime.now(timezone.utc)
        if self._floor_refresh_h > 0:
            # If we waited for the window, check at once (catch a waiver that
            # went live during the wait); otherwise first check is one interval
            # out (the entrypoint already measured at this same boot).
            self._next_floor_check = (
                datetime.now(timezone.utc) if waited_for_window
                else datetime.now(timezone.utc) + timedelta(hours=self._floor_refresh_h))
        consecutive_errors = 0
        while not self._stop:
            if deadline and datetime.now(timezone.utc) >= deadline:
                log.info("window elapsed — stopping cleanly")
                break
            if self._next_floor_check and datetime.now(timezone.utc) >= self._next_floor_check:
                if self._maybe_refresh_floors():
                    log.info("exiting for supervised restart (waiver re-measure)")
                    return  # docker restart -> entrypoint re-measures + reloads universe
                self._next_floor_check = (datetime.now(timezone.utc)
                                          + timedelta(hours=self._floor_refresh_h))
            try:
                self.cycle()
                consecutive_errors = 0
            except Exception as e:  # never let one bad cycle kill the week
                consecutive_errors += 1
                log.exception("cycle failed (%d in a row)", consecutive_errors)
                self.decisions.append("cycle_error", error=str(e))
                if consecutive_errors >= 3:
                    self.alerter.notify(f"agent: {consecutive_errors} consecutive cycle errors: {e}")
            if report_every_min and (datetime.now(timezone.utc) - self._last_report
                                     ).total_seconds() >= report_every_min * 60:
                self._emit_report()
            if once:
                return
            slept = 0
            step = self.cfg.risk.cycle_interval_s * min(consecutive_errors + 1, 6)
            while slept < step and not self._stop:  # responsive to stop signals
                time.sleep(min(2, step - slept))
                slept += 2
        if report_every_min:
            self._emit_report(tag="final")  # end-of-window summary
        self.alerter.notify("🛑 agent stopped cleanly — no pending transactions")
        log.info("agent stopped cleanly")

    # -- periodic operations report (#10) -------------------------------------
    def _leaderboard(self):
        if self._lb is None:
            from agent.monitor.leaderboard import LeaderboardMonitor
            self._lb = LeaderboardMonitor(
                self.feed, self.registry, self.cfg.tokens.allowlist,
                our_wallet=os.environ.get("AGENT_WALLET_ADDRESS", ""))
        return self._lb

    def _emit_report(self, tag: str = "report") -> None:
        """Digest of the period since the last report + leaderboard standing,
        to a uniquely-named file + Telegram. Never breaks the trading loop."""
        period_start = self._last_report or datetime.now(timezone.utc)
        self._last_report = datetime.now(timezone.utc)
        try:
            digest = digest_mod.build_digest(period_start)
            board = None
            try:
                board = self._leaderboard().refresh()
            except Exception as e:  # noqa: BLE001 — board is best-effort
                log.warning("leaderboard refresh failed: %s", e)
            portfolio = None
            try:
                p = reconcile(self.twak, self.feed, self.cfg.tokens, registry=self.registry, pricer=self._pricer)
                portfolio = {"total_usd": round(p.total_usd, 2),
                             "holdings": {k: round(v, 2) for k, v in p.usd_values.items()}}
            except Exception as e:  # noqa: BLE001
                log.warning("report reconcile failed: %s", e)
            path = digest_mod.write_report(digest, board, portfolio, tag=tag)
            self.alerter.notify(
                digest_mod.summary_line(digest, board, portfolio) + f"\n📄 {path.name}")
            log.info("report written: %s", path)
        except Exception as e:  # noqa: BLE001 — reporting must never kill trading
            log.warning("report generation failed: %s", e)

    # -- canary (pre-week live validation) -----------------------------------
    def flatten(self) -> None:
        """One-shot: close every non-stable position into USDT and exit.
        For the end of the competition window (lock the measured result in
        stables) or any emergency de-risk. Honors dry-run."""
        self.registry.ensure_id_map(self.feed, [*self.cfg.tokens.watchlist, *self.cfg.tokens.stables])
        self.registry.ensure_addresses(self.feed, [*self.cfg.tokens.watchlist, *self.cfg.tokens.stables])
        portfolio = reconcile(self.twak, self.feed, self.cfg.tokens, registry=self.registry, pricer=self._pricer)
        non_stable = {s: v for s, v in portfolio.usd_values.items()
                      if s not in self.cfg.tokens.stables and s != "BNB" and v > 1.0}
        if not non_stable:
            log.info("flatten: nothing to close (already in stables)")
            self.alerter.notify("🏁 flatten: already flat — nothing to close")
            return
        log.info("flatten: closing %s", ", ".join(f"{s} ${v:.2f}" for s, v in non_stable.items()))
        self._flatten_to_stables(portfolio, RiskState.NORMAL)
        final = reconcile(self.twak, self.feed, self.cfg.tokens, registry=self.registry, pricer=self._pricer)
        self.alerter.notify(f"🏁 flattened to stables — final ${final.total_usd:.2f}")

    def canary_roundtrip(self, token: str = "CAKE", usd: float = 10.0) -> None:
        """One small REAL round-trip through the full executor path, to prove
        autonomous signing end-to-end before the live week. Buys then sells the
        same token so it ends flat. Bypasses the regime gate (TEST ONLY) but
        keeps every other guardrail; fully logged + alerted. Tiny by design —
        friction (~1.4% of $10 ≈ $0.15) is the cost of the validation."""
        self.registry.ensure_id_map(self.feed, [*self.cfg.tokens.watchlist, *self.cfg.tokens.stables])
        self.registry.ensure_addresses(self.feed, [*self.cfg.tokens.watchlist, *self.cfg.tokens.stables])
        stable = self.cfg.tokens.stables[0]
        self.alerter.notify(f"🐤 canary: real round-trip {stable}->{token}->{stable} ${usd:.0f} (live path test)")

        def snapshot():
            p = reconcile(self.twak, self.feed, self.cfg.tokens, registry=self.registry, pricer=self._pricer)
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
        portfolio = reconcile(self.twak, self.feed, self.cfg.tokens, registry=self.registry, pricer=self._pricer)
        # Freeze the return baseline only on the first LIVE cycle: a dry-run /
        # canary cycle must never seed it, or its tiny pre-funding value (e.g.
        # the ~$47 wallet during testing) sticks and inflates the reported
        # return for the whole competition.
        if (self.store.baseline_usd is None and portfolio.total_usd > 0
                and not self.cfg.dry_run):
            self.store.set_baseline(portfolio.total_usd)

        # 1b. Valuation sanity (2026-07-03): a dead price feed once valued the
        # whole book at $0 and tripped a false HARD STOP. A real wipeout is
        # impossible while any balance is held and priced, so treat an
        # implausible collapse as a data glitch — skip the risk ladder and the
        # snapshot this cycle, never flatten. Next good read resumes normally.
        hwm = self.store.high_water_mark_usd
        held = any(a > 0 for a in portfolio.holdings.values())
        if held and hwm > 0 and portfolio.total_usd < 0.10 * hwm:
            log.warning(
                "valuation implausibly low ($%.2f vs HWM $%.2f) with non-zero "
                "balances — skipping risk ladder this cycle (data glitch?)",
                portfolio.total_usd, hwm)
            self.decisions.append(
                "valuation_glitch", total_usd=round(portfolio.total_usd, 2),
                hwm=round(hwm, 2))
            return

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
                self.feed.global_metrics(ttl_s=self.cfg.risk.regime_cache_min * 60),
                self.feed.fear_greed_latest(ttl_s=self.cfg.risk.regime_cache_min * 60),
                fear_conviction_floor=self.cfg.risk.fear_conviction_floor,
            )
        except FeedError as e:
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
            f"trades today {self.store.trades_today(now, self.cfg.dry_run)}\n{held}"
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
        # Entries opened WITHIN this cycle: `portfolio` is the reconcile from
        # the cycle start, so without this counter two BUY signals in the same
        # pass would each see the pre-entry position count and max_concurrent
        # could be breached (it happened live 2026-07-05 23:10 UTC: ETH and
        # LTC entered 9s apart against max_concurrent=1 — the old 4/day cap
        # had been masking the race).
        opened_this_cycle = 0
        for token in self.cfg.tokens.watchlist:
            cmc_id = self.registry.cmc_id(token)
            if cmc_id is None:
                log.warning("no CMC id for %s — skipping", token)
                continue
            try:
                series = self.feed.series_with_volume(cmc_id, interval="1h", count=200)
                closes = [p for _, p, _ in series]
                volumes = [v for _, _, v in series]
            except FeedError as e:
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
                        self.feed.quotes_latest([cmc_id], ttl_s=60).get(cmc_id) or {}
                    ).get("price")
                    if live:
                        stop_px = float(live)
                except FeedError:
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
                        roundtrips.record(
                            "close", token, stop_px,
                            portfolio.usd_values.get(token, 0.0),
                            f"stop-loss {loss:.1f}%",
                            tx_hash=r.get("hash") or r.get("txHash"),
                            dry_run=bool(r.get("dry_run")))
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
                            roundtrips.record(
                                "close", token, price,
                                portfolio.usd_values.get(token, 0.0),
                                f"liquidity drain {verdict.drop_pct:.0f}%",
                                tx_hash=r.get("hash") or r.get("txHash"),
                                dry_run=bool(r.get("dry_run")))
                            self._position_closed(token)
                        continue
            elif self.store.entry_price(token) is not None:
                # Position left without us seeing the exit (manual move, missed
                # cycle): close the ledger leg too so pairs don't dangle.
                roundtrips.record(
                    "close", token, price, 0.0,
                    "position closed outside agent (reconciled)",
                    dry_run=self.cfg.dry_run)
                self._position_closed(token)

            sig = self.strategy.evaluate(
                MarketContext(token, closes, volumes, holding))
            self.decisions.append(
                "signal", token=token, strategy=self.strategy.name,
                action=sig.action.value,
                conviction=round(sig.conviction, 2), grey_zone=sig.grey_zone,
                expected_move_pct=round(sig.expected_move_pct, 2), detail=sig.reason,
            )
            # Venue shadow read (F1): on a BUY signal, record what PancakeSwap
            # itself is doing — aggregated liquidity, 1h taker buy/sell counts
            # and the pool-vs-CEX price basis. CALIBRATION ONLY: not a gate
            # until a backtest earns it one (house rule).
            if sig.action == technical.Action.BUY and not holding and addr:
                pv = self.dex.pool_view(token, addr)
                if pv is not None:
                    basis = ((pv.price_usd / price - 1) * 100
                             if pv.price_usd and price else None)
                    self.decisions.append(
                        "dex_flow", token=token,
                        liq_usd=round(pv.liquidity_usd),
                        buys_h1=pv.buys_h1, sells_h1=pv.sells_h1,
                        flow_ratio=round(pv.flow_ratio, 2),
                        vol_h24_usd=round(pv.vol_h24_usd),
                        basis_pct=round(basis, 3) if basis is not None else None,
                        main_pool=pv.main_pool, label=pv.main_pool_label,
                    )
                # Perp positioning shadow (F2): is this breakout a short
                # squeeze (OI down + price up = fuel) or fresh longs piling
                # in? Calibration only, same as dex_flow.
                dv = self.derivs.snapshot(token)
                if dv is not None:
                    px_chg = ((price / closes[-25] - 1) * 100
                              if len(closes) >= 25 and closes[-25] else None)
                    self.decisions.append(
                        "deriv_view", token=token,
                        funding=dv.funding_rate, oi_usd=dv.oi_usd,
                        oi_chg_24h_pct=dv.oi_chg_24h_pct,
                        ls_ratio=dv.ls_ratio,
                        long_liq_usd=dv.long_liq_usd,
                        short_liq_usd=dv.short_liq_usd,
                        liq_window_h=dv.liq_window_h,
                        px_chg_24h_pct=round(px_chg, 2) if px_chg is not None else None,
                        squeeze=(dv.squeeze_fingerprint(px_chg)
                                 if px_chg is not None else None),
                    )

            if sig.action == technical.Action.HOLD:
                continue

            is_entry = sig.action == technical.Action.BUY
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
            # Volume confirmation (#11): only enter when volume_24h is rising vs
            # its own trailing average. Gross alpha is positive but fees flip it
            # negative; this cuts the weakest (fee-margin) entries.
            if is_entry and not technical.volume_confirms(
                    volumes, self.cfg.risk.vol_confirm_lookback,
                    self.cfg.risk.vol_confirm_ratio):
                self.decisions.append(
                    "entry_blocked", token=token, rule="volume_confirm",
                    vol=round(volumes[-1], 0) if volumes else 0.0)
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
                open_positions=(portfolio.open_positions(self.cfg.tokens.stables)
                                + opened_this_cycle),
                signal_age_min=age_min,
            )
            if r is not None:  # track cost basis (stop-loss) + pool baseline (#7)
                roundtrips.record(
                    "open" if is_entry else "close", token, price, usd,
                    sig.reason, tx_hash=r.get("hash") or r.get("txHash"),
                    dry_run=bool(r.get("dry_run")))
                if is_entry:
                    opened_this_cycle += 1
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
        if nothing traded by the deadline, do a minimal stable<->stable swap.
        Post-competition this rule no longer applies — an empty
        daily_trade_deadline_utc disables the forced trade entirely (no reason
        to pay friction on a swap that captures no edge)."""
        if not self.cfg.risk.daily_trade_deadline_utc:
            return
        if self.store.trades_today(now, self.cfg.dry_run) > 0:
            return
        hh, mm = map(int, self.cfg.risk.daily_trade_deadline_utc.split(":"))
        if (now.hour, now.minute) < (hh, mm):
            return
        log.info("no trade yet today; executing compliance trade")
        s = self.cfg.tokens.stables
        if len(s) < 2:
            self.alerter.notify("compliance trade impossible: need two stables configured")
            return
        # Alternate direction (ping-pong): swap whichever stable currently holds
        # more into the other. One swap = one leg (qualifies under the >=1
        # swap/24h rule), but always draining the heavier side keeps the two
        # stables balanced instead of slowly bleeding the trading stable into
        # the counted-but-not-traded one over a quiet week.
        bal = portfolio.usd_values
        frm, to = (s[0], s[1]) if bal.get(s[0], 0.0) >= bal.get(s[1], 0.0) else (s[1], s[0])
        self.executor.execute(
            TradeProposal(frm, to, 10.0, 0.0, False, "daily compliance trade"),
            portfolio_usd=portfolio.total_usd,
            state=state,
            open_positions=portfolio.open_positions(s),
            signal_age_min=0.0,
        )
