# Deployment (VPS, unattended trading week)

The agent must survive 7 days unattended. Two supported setups — **Docker
Compose is the recommended one**; systemd is the no-docker fallback.

In both setups the runtime is the same pair of processes:

```
twak serve --rest (localhost:3000, local signing)  <--REST-->  python -m agent
```

The agent reconciles positions from on-chain state on every start, so
restarts are always safe: crash -> restart -> reconcile -> continue.

## Prerequisites (on the server, once)

1. `twak` authenticated and the agent wallet created (`~/.twak` exists —
   already done on this server).
2. Docker + the compose plugin (option A) or python3.10+/node22 (option B).
3. Repo cloned; then copy the two private files that are NOT in git:
   - `.env` — from `.env.example`: `CMC_PRO_API_KEY`, `TW_ACCESS_ID`,
     `TW_HMAC_SECRET`, and `TWAK_WALLET_PASSWORD` (containers have no OS
     keychain, so the password must come from the environment).
   - `config/watchlist.local.yaml` — the private watchlist.
     ⚠️ Copy it BEFORE `docker compose up`: if the file is missing, docker
     creates a directory in its place and the mount breaks.

## Option A — Docker Compose (recommended)

```bash
docker compose up -d --build     # builds and starts in DRY-RUN
docker compose logs -f agent     # watch decisions live
```

- `restart: unless-stopped` + the entrypoint supervisor = watchdog: if either
  twak serve or the loop dies, the container exits and docker restarts both.
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

Two units: `deploy/twak-serve.service` and `deploy/trading-agent.service`
(the agent unit depends on the serve unit). Install:

```bash
sudo cp deploy/twak-serve.service deploy/trading-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now twak-serve trading-agent
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
