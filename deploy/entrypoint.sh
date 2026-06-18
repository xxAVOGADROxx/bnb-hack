#!/usr/bin/env bash
# Run the trading agent with the CLI transport (the proven-reliable execution
# path: the `twak swap` CLI sends the ERC-20/Permit2 approval AND waits for it
# to confirm before the swap, so first-time-token sells don't revert — unlike
# the REST `swap` action). The wallet password comes from TWAK_WALLET_PASSWORD
# in the environment (containers have no OS keychain).
#
# Signals: SIGTERM/SIGINT are handled by the Python agent itself (clean
# shutdown — finish the in-flight cycle, exit with no pending tx). exec makes
# the agent PID 1 so docker delivers the signal straight to it.
set -uo pipefail

# Optional pre-step (trading-week only): re-measure round-trip friction at the
# real per-position size so the edge floors reflect the live swap-fee waiver
# (0.7%->0.077%/leg, active only during the competition week). Gated on
# REFRESH_FILTER_ON_START so dry-run/test boots stay instant. Quotes only — no
# tx, no signing — so it can't contend with the trading nonce. It writes
# data/liquidity_report.json + config/watchlist.local.yaml (both bind-mounted
# rw). A failure here must NOT block trading: the loop degrades to the existing
# report, so we warn and carry on.
if [ -n "${REFRESH_FILTER_ON_START:-}" ]; then
    echo "entrypoint: re-measuring liquidity @ \$${FILTER_SIZE_USD:-750}/position, "\
"<=${WATCHLIST_MAX_COST_PCT:-1.5}% ceiling, before live loop"
    python scripts/liquidity_filter.py --size-usd "${FILTER_SIZE_USD:-750}" \
        --max-cost-pct "${WATCHLIST_MAX_COST_PCT:-1.5}" \
        || echo "entrypoint: WARNING liquidity filter failed; starting on the existing report"
fi

exec python -m agent "$@"
