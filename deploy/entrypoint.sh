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

exec python -m agent "$@"
