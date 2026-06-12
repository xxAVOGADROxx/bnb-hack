# BNB Hack — Autonomous Self-Custody Trading Agent on BSC

An autonomous trading agent for the **BNB Hack: AI Trading Agent Edition**
(CoinMarketCap × Trust Wallet × BNB Chain). It runs unattended for the
competition week, reading AI-processed market signals from CoinMarketCap,
deciding with a **deterministic, fully-auditable strategy + risk engine**, and
executing spot swaps **exclusively through the Trust Wallet Agent Kit (TWAK)**
with **local signing** — the key never leaves the machine.

```
  CoinMarketCap for Agent          deterministic Python loop            Trust Wallet Agent Kit
   AI-processed signals    ──►   strategy + regime + RISK ENGINE   ──►   local signing + swap
   (regime, F&G, TA,             (code, not an LLM per tick)             (self-custody, x402)
    macro events)                          │                                      │
                                    full decision audit  ◄───── on-chain truth (reconcile) ──┘
```

No LLM decides ticks: the live loop is deterministic code. The AI lives in the
*signal* (CMC's processed market intelligence + a paid premium TA tie-break via
x402), not in the *trigger*. For a 7-day unattended real-money run this is the
stronger design — **reproducible**, **auditable** (every decision logged with
the rule that fired), **fail-closed** (an unknown/hallucinated token is
rejected, not traded), and **cheap** (pay for AI only on the grey-zone branch).

## How the sponsor stack is used

- **CMC for Agent** — every signal: market regime (Global Metrics, Fear &
  Greed), per-token technicals, upcoming macro events. Read-only: CMC thinks,
  TWAK executes.
- **TWAK** — the *sole* execution layer, all signed locally: balances, quotes,
  swaps, competition registration. Production drives the **`twak` CLI** (it
  sends and waits for the token approval before the swap, so first-time-token
  sells don't revert) plus **x402** micropayments; a REST `twak serve` client
  is also implemented (`agent/twak/client.py`).
- **x402** — cost-aware premium data: in a grey-zone decision the agent pays
  per call for CMC's premium TA, settling in **USDC on BSC** (same chain it
  trades — no cross-chain bridge needed).

## Architecture

| Layer | Module | Responsibility |
|---|---|---|
| Signal / alpha | `agent/signals/technical.py` | EMA/MACD/RSI trend-following → BUY/HOLD/EXIT + conviction + grey-zone flag |
| Regime gate | `agent/signals/regime.py` | Global metrics + Fear&Greed → RISK_ON / CONFLICTED / RISK_OFF |
| Macro blackout | `agent/risk/macro.py` | Pause/halve entries around scheduled macro events (PCE, Fed) |
| Risk engine | `agent/risk/engine.py` | Fail-closed guardrails: allowlist, drawdown ladder, caps, slippage, min-edge |
| Execution | `agent/execution/executor.py` | Quote → price-impact check → swap via TWAK, by contract address |
| State / reconcile | `agent/state/` | Rebuilds positions from on-chain truth every cycle (restart-safe) |
| Self-monitor | `agent/monitor/snapshot.py` | Hourly snapshot + drawdown, measured like the judge does |
| Leaderboard | `agent/monitor/leaderboard.py` | Read-only competitor ranking via contract events + Multicall3 |
| x402 | `agent/x402/premium.py` | Pay-per-call CMC premium TA tie-break (BSC USDC) |
| Token registry | `agent/tokens.py` | Symbol → CMC id → **BSC contract address** (the only safe execution ref) |

## Guardrails (enforced in code, visible in logs)

Every decision — approved or rejected, with the rule that fired — is appended
to `data/decisions.jsonl`. The risk engine is **fail-closed**:

- drawdown ladder from the high-water mark: alert → pause entries → hard-stop
  and flatten to stables (well inside the official DQ cap)
- hard allowlist of the eligible tokens; anything else is rejected
- per-trade and per-day limits, max position size, max concurrent positions
- slippage + price-impact rejection on every swap; stale-data gate
- ≥1 trade/day compliance automation and a portfolio-floor check every cycle
- **restart-safe** (reconcile from chain) and **clean shutdown** (finishes the
  in-flight cycle, exits with no pending transaction)

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[ta,dev]"
cp .env.example .env       # add CMC + TWAK credentials (never commit .env)

python -m agent --once     # one DRY-RUN cycle (real signals, no tx signed)
python -m agent            # continuous dry-run
python -m agent --live     # real execution (explicit opt-in)
python -m agent --max-hours 6   # bounded, Telegram-watched window
pytest -q                  # 36 tests
```

Requires the [`twak` CLI](https://www.npmjs.com/package/@trustwallet/cli)
authenticated with an agent wallet (`twak wallet create`).

## Deployment

Built to survive the week unattended (Docker Compose with a restart watchdog,
or systemd). See [`deploy/DEPLOY.md`](deploy/DEPLOY.md) for the full server
setup and [`deploy/WINDOW.md`](deploy/WINDOW.md) for a bounded test window.

```bash
docker compose up -d --build              # starts the agent, dry-run
docker compose run --rm agent --max-hours 6   # bounded test window
```

## Evolution

See [`CHANGELOG.md`](CHANGELOG.md) for how the strategy and execution have
matured (with backtest evidence for each model change).

## Configuration

- `config/risk.yaml` — every guardrail threshold (enforced + logged).
- `config/tokens.yaml` — the eligible-token allowlist + stables.
- `config/watchlist.local.yaml` — the active watchlist (gitignored / private).
- `config/macro_events.yaml` — scheduled macro-event blackout windows.

## On-chain

- Chain: **BSC mainnet**. Execution: spot swaps via TWAK (non-custodial DEX).
- Agent wallet: `0x44dD4C2c353457fF68b164934870BB0391f9251C`
- Competition contract: `0x212c61b9b72c95d95bf29cf032f5e5635629aed5`

## Security

- No keys, seeds or credentials in this repo — ever. `.env`, keystores, the
  private watchlist and runtime data are gitignored.
- All signing is local (TWAK agent wallet, `~/.twak`, mounted read-only in
  Docker). No custodial component anywhere in the loop; spot execution on
  non-custodial DEX liquidity only.
- The wallet is treated as a hot wallet, funded with competition capital only.
