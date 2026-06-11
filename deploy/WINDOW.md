# Bounded test window on the server (e.g. 6h, watched from Telegram)

Goal: run the agent unattended on the VPS for a fixed window, watch every
trade + an hourly balance line arrive in Telegram, and end with **no pending
transactions**. This is the rehearsal for the real 22–28 jun week.

## What you'll see in Telegram (`bnbTournamentBot`)
- `🤖 agent online (… , 6h window)` at start.
- `💰 $<value> (<return>) | dd <x>% | trades today N` once per UTC hour (heartbeat).
- `✅ swap FROM->TO $amount … <bscscan link>` on every executed trade.
- `⚠️ swap FAILED …` if a swap errors, and risk-state alerts if drawdown trips.
- `🛑 agent stopped cleanly — no pending transactions` at the end.

## Why "no pending tx at the end" is guaranteed
Swaps are **synchronous**: `twak swap` blocks until the tx confirms on-chain
before returning, so between cycles there is never an in-flight tx. The window
stop (timer, `docker stop`, or Ctrl-C) is handled by a signal that lets the
**current cycle finish** before exiting — it never interrupts a swap mid-flight.

## Run it

On the server, in `~/apps/dorahacks/bnb-hack-1337`, with `.env` +
`config/watchlist.local.yaml` in place (see DEPLOY.md prerequisites).

### Option A — dry-run window (zero risk, watch the decision-making)
Real signals, real quotes, **no transaction is ever signed**. Best first run.
```bash
python -m agent --max-hours 6
```
You'll get the hourly heartbeat and see in `data/decisions.jsonl` exactly what
it *would* trade and why. In the current Extreme-Fear / RISK_OFF regime the
correct behaviour is to **stay out** — disciplined non-trading is the story.

### Option B — live window (tiny real capital)
Signs and executes. With ~$45 and a RISK_OFF regime, the only trade you should
expect is the **daily compliance swap** (a minimal stable→stable trade) if the
window crosses 20:00 UTC — that alone produces one real on-chain tx + Telegram
notification to validate the live path end to end.
```bash
python -m agent --live --max-hours 6
```

### Under Docker (recommended on the server)
```bash
# dry-run window:
docker compose run --rm agent --max-hours 6
# live window:
docker compose run --rm agent --live --max-hours 6
```
`docker stop` during the window triggers the same clean shutdown.

## Before a *fresh* performance window
Return % and drawdown are measured from a baseline set on the first cycle. To
start the window from a clean baseline (not inheriting the dust-test numbers),
clear the run state first:
```bash
rm -f data/state.json data/snapshots.jsonl   # keeps id_map / addresses caches
```
Leave `data/decisions.jsonl` — it's the audit trail; it only ever appends.

## Faster feedback (optional)
Default cycle is 300 s. For a short window where you want to see more cycles,
drop `data.cycle_interval_s` in `config/risk.yaml` to e.g. 120. Put it back to
300 for the real week (fewer, higher-quality decisions; friction punishes
overtrading).
