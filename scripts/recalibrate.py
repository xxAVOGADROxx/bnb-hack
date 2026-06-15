"""One-shot pre-competition re-calibration (run ~June 20, ON THE SERVER).

Runs the whole battery in order, safely, and prints a GO/NO-GO summary:

  0. code integrity      — pytest must pass (abort otherwise)
  1. backup              — snapshot the current watchlist + liquidity report
  2. friction (WRITES)   — re-measure real BSC round-trip cost and REGENERATE
                           config/watchlist.local.yaml + data/liquidity_report.json
                           (the live edge floors). Shows the old->new diff loud.
  3. satellites (read)   — measure friction + v2-pool sentinel coverage for any
                           candidate (default BEAT). REPORTED ONLY, never written
                           to the watchlist (uncovered tokens must be a human call).
  4. validation (read)   — backtest + volume-filter + year forecast on FRESH data
  5. summary             — new watchlist, flags, forecast, and the manual runbook

Nothing is committed and nothing goes live; this only regenerates the private,
gitignored server files the loop reads. Review the diff before flipping --live.

Usage: .venv/bin/python scripts/recalibrate.py [--size-usd 1500] [--max-cost-pct 2.0]
       [--satellites BEAT,GWEI] [--skip-tests]
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import CONFIG_DIR, DATA_DIR, load_config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
WATCHLIST = CONFIG_DIR / "watchlist.local.yaml"
REPORT = DATA_DIR / "liquidity_report.json"


def hr(title: str) -> None:
    print(f"\n{'='*78}\n  {title}\n{'='*78}")


def run(cmd: list[str], *, check: bool = True) -> int:
    print(f"\n$ {' '.join(str(c) for c in cmd)}\n")
    rc = subprocess.run(cmd, cwd=ROOT).returncode
    if check and rc != 0:
        print(f"\n!! step failed (exit {rc}) — STOPPING. Fix before going live.")
        sys.exit(rc)
    return rc


def read_watchlist() -> list[str]:
    import yaml
    if not WATCHLIST.exists():
        return []
    return list((yaml.safe_load(WATCHLIST.read_text()) or {}).get("watchlist") or [])


def satellite_check(symbols: list[str], size_usd: float) -> None:
    """Friction + PancakeSwap-v2 sentinel coverage for satellite candidates.
    Reported only — never auto-added (uncovered = unprotected = human decision)."""
    from agent.cmc.client import CMCClient
    from agent.risk.liquidity import USDT, WBNB, pancake_v2_pair
    from agent.twak.client import TwakClient, TwakError
    from scripts.liquidity_filter import measure

    cfg = load_config(dry_run=True)
    cmc = CMCClient(cfg.cmc_api_key)
    twak = TwakClient(chain="bsc", dry_run=True)
    addr = json.loads((DATA_DIR / "bsc_addresses.json").read_text())
    min_ref = cfg.risk.liquidity_min_ref_usd

    print(f"  {'sym':<7}{'round-trip':>11}{'deepest v2 pool':>18}  sentinel  verdict")
    for sym in symbols:
        token = addr.get(sym)
        if not token:
            print(f"  {sym:<7}{'—':>11}{'no BSC address':>18}  —         SKIP (unknown token)")
            continue
        try:
            r = measure(twak, sym, size_usd, token)
            rt = r["round_trip_cost_pct"]
        except (TwakError, ValueError, KeyError) as e:
            print(f"  {sym:<7}{'FAILED':>11}  {str(e)[:40]}")
            continue
        try:
            pools = [pancake_v2_pair(token, USDT), pancake_v2_pair(token, WBNB)]
            q = cmc.dex_pair_quotes_latest(pools)
            liq = max(float(q.get(p.lower(), {}).get("liquidity") or 0) for p in pools)
        except Exception:  # noqa: BLE001
            liq = 0.0
        covered = liq >= min_ref
        edge_floor = rt + cfg.risk.edge_floor_margin_pct
        ok_friction = rt <= 2.0
        verdict = ("CONSIDER" if (ok_friction and covered)
                   else "CAUTION: uncovered" if ok_friction
                   else "REJECT: friction")
        print(f"  {sym:<7}{rt:>10.2f}%{liq:>17,.0f}  {'YES' if covered else 'no ':>7}  "
              f"{verdict} (edge floor would be {edge_floor:.1f}%)")
    print("\n  Satellites are NOT written to the watchlist. To add one, edit "
          "config/watchlist.local.yaml by hand\n  AFTER confirming friction<=2% AND "
          "sentinel coverage (or accept the uncovered tail risk).")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size-usd", type=float, default=1500.0)
    ap.add_argument("--max-cost-pct", type=float, default=2.0,
                    help="watchlist friction ceiling (current list used 2.0)")
    ap.add_argument("--satellites", default="BEAT",
                    help="comma-separated candidates to evaluate (reported only)")
    ap.add_argument("--skip-tests", action="store_true")
    args = ap.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%MZ")
    hr(f"RE-CALIBRATION  {stamp}  (size ${args.size_usd:.0f}, ceiling {args.max_cost_pct}%)")
    print("  Regenerates the PRIVATE server files the live loop reads. Nothing is")
    print("  committed or set live. Review the watchlist diff before flipping --live.")

    # 0. code integrity ----------------------------------------------------
    hr("0. code integrity (pytest)")
    if args.skip_tests:
        print("  skipped (--skip-tests)")
    else:
        run([PY, "-m", "pytest", "-q"])

    # 0.5 CMC tier ---------------------------------------------------------
    # Record which CMC entitlement the calibration ran on. The Pro upgrade is
    # time-boxed, and the heavy historical re-measurement below is exactly what
    # the extra credits buy — so pin it to the record. Never fatal.
    hr("0.5 CMC key tier")
    try:
        from agent.cmc.client import CMCClient
        ks = CMCClient(load_config(dry_run=True).cmc_api_key).plan_summary()
        print(f"  tier: {ks['tier']}  ({'PAID' if ks['is_paid'] else 'free/Basic'})")
        print(f"  monthly credits: {ks['credits_monthly']} (left {ks['credits_left']}), "
              f"daily {ks['credits_daily']}, rate {ks['rate_limit_min']}/min")
        if not ks["is_paid"]:
            print("  note: free tier — historical OHLCV may be capped; the loop "
                  "still runs on standard endpoints (degrades safely).")
    except Exception as e:  # noqa: BLE001
        print(f"  skipped (key/info error): {e}")

    # 1. backup ------------------------------------------------------------
    hr("1. backup current watchlist + liquidity report")
    backup_dir = DATA_DIR / "backups" / stamp
    backup_dir.mkdir(parents=True, exist_ok=True)
    old_watchlist = read_watchlist()
    for f in (WATCHLIST, REPORT):
        if f.exists():
            shutil.copy2(f, backup_dir / f.name)
            print(f"  backed up {f.name} -> {backup_dir.relative_to(ROOT)}/")
    print(f"  old watchlist ({len(old_watchlist)}): {', '.join(old_watchlist) or '(none)'}")

    # 2. friction re-measure (WRITES watchlist + edge floors) --------------
    hr("2. friction re-measure  [WRITES watchlist.local.yaml + liquidity_report.json]")
    run([PY, "scripts/liquidity_filter.py",
         "--size-usd", str(args.size_usd), "--max-cost-pct", str(args.max_cost_pct)])
    new_watchlist = read_watchlist()
    added = [t for t in new_watchlist if t not in old_watchlist]
    dropped = [t for t in old_watchlist if t not in new_watchlist]
    print(f"\n  new watchlist ({len(new_watchlist)}): {', '.join(new_watchlist)}")
    if added:
        print(f"  ++ ADDED:   {', '.join(added)}")
    if dropped:
        print(f"  -- DROPPED: {', '.join(dropped)}  <-- liquidity worsened past "
              f"{args.max_cost_pct}%; confirm this is real before going live")
    if not added and not dropped:
        print("  no change vs the backed-up watchlist (stable liquidity) ✓")

    # 3. satellites (read-only) --------------------------------------------
    hr("3. satellite candidates (friction + sentinel coverage; reported only)")
    sats = [s.strip().upper() for s in args.satellites.split(",") if s.strip()]
    if sats:
        satellite_check(sats, args.size_usd)
    else:
        print("  (none)")

    # 4. validation battery on FRESH data (read-only) ----------------------
    hr("4. validation on fresh data — honest backtest (7d) + regime gate")
    run([PY, "scripts/backtest.py", "--bars", "168"], check=False)
    hr("4b. volume filter still helps? (7d + 20d, gross/fees/net)")
    run([PY, "scripts/vol_filter_bt.py"], check=False)
    hr("4c. competition-week forecast (regime-conditioned)")
    run([PY, "scripts/year_forecast.py"], check=False)

    # 5. summary + runbook -------------------------------------------------
    hr("5. GO / NO-GO summary")
    print(f"  watchlist now ({len(new_watchlist)}): {', '.join(new_watchlist)}")
    print(f"  edge floors regenerated -> data/liquidity_report.json")
    print(f"  backup of the previous config -> data/backups/{stamp}/")
    if dropped:
        print(f"  ⚠ REVIEW: {', '.join(dropped)} dropped — verify before live.")
    print("""
  Remaining manual steps toward the June 22 window:
    [ ] review the watchlist diff above (revert from the backup if it looks wrong)
    [ ] decide satellites by hand (only if CONSIDER above)
    [ ] confirm volume filter still net-positive vs no-filter (step 4b)
    [ ] git pull && docker compose up -d --build   (still DRY-RUN)
    [ ] crash/reboot resilience test (deploy/DEPLOY.md)
    [ ] FUND $5k USDT (BSC) + trim BNB to ~$5 gas      <- June 21
    [ ] rm data/state.json                              <- clean baseline
    [ ] twak compete register  (on-chain, before June 22)
    [ ] docker compose run --rm agent --live --canary   <- one real round-trip
    [ ] flip to scheduled live window:
        --live --start-at 2026-06-22T00:00Z --stop-at 2026-06-28T23:59Z
             --report-every-min 720
""")
    print("  re-calibration complete.")


if __name__ == "__main__":
    main()
