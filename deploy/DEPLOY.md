# Deployment (VPS, unattended trading week)

The agent must survive 7 days unattended. Two supported setups — **Docker
Compose is the recommended one**; systemd is the no-docker fallback.

In both setups the runtime is a single process: `python -m agent`, which
drives `twak` via the CLI. The CLI is the reliable execution path — `twak
swap` sends the ERC-20/Permit2 approval AND waits for it to confirm before
the swap, so first-time-token sells don't revert (the REST `swap` action
fires both together and reverts on the approval race; validated live with the
`--canary` round-trip). The wallet password comes from `TWAK_WALLET_PASSWORD`
in `.env` (containers have no OS keychain).

The agent reconciles positions from on-chain state on every start, so
restarts are always safe: crash -> restart -> reconcile -> continue.

Before the live week, validate the autonomous execution path end to end with
one small real round-trip (buys then sells a $10 position, ends flat):

```bash
docker compose run --rm agent --live --canary
```

## Prerequisites (on the server, once)

1. `twak` authenticated and the agent wallet created (`~/.twak` exists —
   already done on this server).
2. Docker + the compose plugin (option A) or python3.10+/node22 (option B).
3. Repo cloned; then provide the private files that are NOT in git (they
   reveal the strategy, so they live only on the server):
   - `.env` — from `.env.example`: `CMC_PRO_API_KEY`, `TW_ACCESS_ID`,
     `TW_HMAC_SECRET`, and `TWAK_WALLET_PASSWORD` (containers have no OS
     keychain, so the password must come from the environment).
   - `config/watchlist.local.yaml` — the private watchlist.
     ⚠️ Copy it BEFORE `docker compose up`: if the file is missing, docker
     creates a directory in its place and the mount breaks.
   - `data/liquidity_report.json` — the measured per-token friction that
     powers the edge floor (#9). Generate it ON the server (it's gitignored):
     `python scripts/liquidity_filter.py` (or it ships in `data/` if you
     `scp` the whole dir). **Without it the per-token edge floor is disabled**
     (the agent logs a warning and, in LIVE mode, Telegram-alerts) — the
     re-entry cooldown still applies, but regenerate it before the live week.

## Option A — Docker Compose (recommended)

```bash
docker compose up -d --build     # builds and starts in DRY-RUN
docker compose logs -f agent     # watch decisions live
```

- `restart: unless-stopped` = watchdog: if the agent dies, docker restarts it
  and it reconciles from chain. `stop_grace_period` gives an in-flight swap
  time to finish on `docker stop`.
- Logs rotate (10 MB x 5). The decision audit trail is in `./data/decisions.jsonl`.
- **Going live** (before the trading window): uncomment `command: ["--live"]`
  in docker-compose.yml, then `docker compose up -d`. Live mode is an
  explicit opt-in everywhere — dry-run is the default at every layer.

### Mandatory resilience test (do this once before the window)

```bash
docker kill bnb-hack-agent        # simulate a crash
docker ps                         # compose restarts it
docker compose logs --tail 20 agent   # must show: reconciled N holdings...
```

Also reboot the VPS once and confirm the container comes back
(`restart: unless-stopped` survives reboots as long as docker itself is
enabled: `systemctl enable docker`).

## Option B — systemd (no docker)

One unit: `deploy/trading-agent.service` (single agent process, CLI transport).
Install:

```bash
sudo cp deploy/trading-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trading-agent
journalctl -fu trading-agent
```

## Updating during the build window

```bash
git pull && docker compose up -d --build
```

Never update during the live window unless something is actually broken —
discipline is part of the score.

## Monitoring during the live week

- `docker compose logs -f agent` — every decision: signal -> rule -> action -> tx hash.
- `data/decisions.jsonl` — same, machine-readable (feeds the demo).
- Telegram alerts (if `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` set): drawdown
  ladder, repeated execution failures, dead data feed.
- `twak compete status --json` — registration/window state on-chain.
- Leaderboard (read-only side process, optional — trading never depends on it):
  `docker compose exec agent python scripts/leaderboard.py` for one refresh,
  or run it on the host with `--watch` for hourly refreshes. Output:
  `data/leaderboard.json` + console table with our rank and risk posture.
