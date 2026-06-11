#!/usr/bin/env bash
# Supervise `twak serve --rest` + the trading agent in one container.
#
# Two responsibilities:
#  1. If either process dies, exit so docker's restart policy revives the pair
#     (the agent reconciles from on-chain state on every start, so restarts are
#     safe by design).
#  2. On `docker stop` (SIGTERM), forward the signal to the AGENT so it shuts
#     down cleanly — it finishes the in-flight cycle (swaps are synchronous) and
#     exits with no pending transaction. Without this, SIGTERM hits PID 1 only
#     and docker SIGKILLs the agent after the grace period, possibly mid-swap.
set -uo pipefail

twak serve --rest --port 3000 --host 127.0.0.1 &
SERVE_PID=$!

# Give the wallet server a moment, then verify it actually came up.
sleep 3
if ! kill -0 "$SERVE_PID" 2>/dev/null; then
    echo "twak serve failed to start" >&2
    exit 1
fi

python -m agent "$@" &
AGENT_PID=$!

# Forward stop signals to the agent and wait for its clean shutdown.
terminate() {
    echo "signal received — forwarding to agent for clean shutdown" >&2
    kill -TERM "$AGENT_PID" 2>/dev/null
    wait "$AGENT_PID"
    kill -TERM "$SERVE_PID" 2>/dev/null
    exit 0
}
trap terminate SIGTERM SIGINT

# Wait for whichever exits first. If a process dies on its own, fall through and
# exit non-zero so docker restarts the pair; if a signal arrives, `terminate`
# handles it. `wait -n` returns on signal too, so re-check liveness after.
while kill -0 "$SERVE_PID" 2>/dev/null && kill -0 "$AGENT_PID" 2>/dev/null; do
    wait -n "$SERVE_PID" "$AGENT_PID" && break
done
echo "a supervised process exited — terminating container for restart" >&2
kill -TERM "$AGENT_PID" "$SERVE_PID" 2>/dev/null
exit 1
