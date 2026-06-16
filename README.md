<div align="center">

<img src="assets/full_waso_lightpng.svg" alt="logo" width="320" />

# Autonomous Self-Custody Trading Agent on BSC

[![CI](https://github.com/xxAVOGADROxx/bnb-hack/actions/workflows/ci.yml/badge.svg)](https://github.com/xxAVOGADROxx/bnb-hack/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Release](https://img.shields.io/github/v/release/xxAVOGADROxx/bnb-hack?sort=semver)](https://github.com/xxAVOGADROxx/bnb-hack/releases)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Tests](https://img.shields.io/badge/tests-73%20passing-brightgreen.svg)](agent/tests)

[![Last commit](https://img.shields.io/github/last-commit/xxAVOGADROxx/bnb-hack)](https://github.com/xxAVOGADROxx/bnb-hack/commits/main)
[![Commit activity](https://img.shields.io/github/commit-activity/m/xxAVOGADROxx/bnb-hack)](https://github.com/xxAVOGADROxx/bnb-hack/pulse)
[![Downloads](https://img.shields.io/github/downloads/xxAVOGADROxx/bnb-hack/total)](https://github.com/xxAVOGADROxx/bnb-hack/releases)
[![Stars](https://img.shields.io/github/stars/xxAVOGADROxx/bnb-hack?style=flat)](https://github.com/xxAVOGADROxx/bnb-hack/stargazers)

</div>

An autonomous trading agent for the **BNB Hack: AI Trading Agent Edition**
(CoinMarketCap × Trust Wallet × BNB Chain). It runs unattended for the
competition week, reading AI-processed market signals from CoinMarketCap,
deciding with a **deterministic, fully-auditable strategy + risk engine**, and
executing spot swaps **exclusively through the Trust Wallet Agent Kit (TWAK)**
with **local signing** — the key never leaves the machine.

> **▶ Watch the demo (≈3 min):** https://youtu.be/2Hd6MHWRu2Y

```mermaid
flowchart LR
    CMC["<b>CoinMarketCap for Agent</b><br/>AI-processed signals<br/>regime · Fear/Greed · TA · macro"]:::brain
    LOOP["<b>Deterministic Python loop</b><br/>strategy + regime + RISK ENGINE<br/><i>code, not an LLM per tick</i>"]:::core
    TWAK["<b>Trust Wallet Agent Kit</b><br/>local signing + swap<br/>self-custody · x402"]:::exec
    CHAIN[("BSC — on-chain truth")]:::chain
    AUDIT[("Append-only<br/>decision audit")]:::log

    CMC -->|signal| LOOP
    LOOP -->|decision| TWAK
    TWAK -->|swap| CHAIN
    CHAIN -.->|reconcile| LOOP
    LOOP -.->|every rule that fired| AUDIT

    classDef brain fill:#eaf2ff,stroke:#3b82f6,color:#0b2545;
    classDef core  fill:#fff4e6,stroke:#f59e0b,color:#3a2a00;
    classDef exec  fill:#eafbf0,stroke:#10b981,color:#04321f;
    classDef chain fill:#f3e8ff,stroke:#a855f7,color:#2e1065;
    classDef log   fill:#f1f5f9,stroke:#64748b,color:#0f172a;
```

No LLM decides ticks: the live loop is deterministic code. The AI lives in the
*signal* (CMC's processed market intelligence + a paid premium TA tie-break via
x402), not in the *trigger*. For a 7-day unattended real-money run this is the
stronger design — **reproducible**, **auditable** (every decision logged with
the rule that fired), **fail-closed** (an unknown/hallucinated token is
rejected, not traded), and **cheap** (pay for AI only on the grey-zone branch).

## How the sponsor stack is used

- **CMC for Agent** — every signal: market regime (Global Metrics, Fear &
  Greed — live *and* historical, which calibrates the regime gate in the
  backtest), per-token technicals, live quotes for the stop-loss, upcoming
  macro events. The **CMC DEX API** adds pool-level liquidity monitoring (the
  liquidity sentinel below). Read-only: CMC thinks, TWAK executes.
- **TWAK** — the *sole* execution layer, all signed locally: balances, quotes,
  swaps, competition registration. Production drives the **`twak` CLI** (it
  sends and waits for the token approval before the swap, so first-time-token
  sells don't revert) plus **x402** micropayments; a REST `twak serve` client
  is also implemented (`agent/twak/client.py`).
- **x402** — both sides of the protocol. The agent **charges**, and it's
  **live and judge-payable right now** at a public endpoint: a built-in x402 V2
  server (`python -m agent.x402.server`, behind a Cloudflare tunnel at
  **`https://bnb-hack.broncano.io`**) sells a catalog of read-only data products
  (leaderboard, risk posture, reports) for **0.01 USD1 per call** — 402
  challenge → EIP-3009 signature verified off-chain → **settled on-chain on
  BSC** → each charge recorded to a payments ledger and Telegram-alerted. Proven
  end-to-end with a real settlement
  ([tx `0xc32f54aa…`](https://bscscan.com/tx/0xc32f54aaf8e5c0cf617da7928dcacc98271aa2f65def187566511798cf8f9f6a),
  block 104327666); any compliant client pays it out of the box (`twak x402
  request https://bnb-hack.broncano.io/leaderboard`). Settlement gas is paid by
  a **dedicated payments wallet**, isolated from the trading wallet so a charge
  can never consume a trade's nonce or stall an order. The agent also **pays**
  for a premium TA tie-break in grey-zone decisions. This was blocked by a TWAK
  transport bug (its paid retry sends only `Accept: application/json`, but CMC's
  MCP transport requires `application/json, text/event-stream` → HTTP 400 before
  settlement, no funds moved). We diagnosed it and shipped a small **local SSE
  proxy** that rewrites only that one header, forwarding the body and the x402
  payment headers unchanged — the exact request that 400s direct from CMC returns
  **200 through the proxy**, so the pay path is now open end-to-end without
  waiting on a TWAK fix. It stays **fail-safe**: if the proxy is unset or down,
  the agent falls back to "no confirmation → stay out" (it can never cause a bad
  trade). The first pay-side settlement lands when the agent hits a live
  grey-zone signal during trading. See [docs/X402.md](docs/X402.md).
- **BNB AI Agent SDK** — the agent has an **on-chain ERC-8004 identity**
  (agentId **1375**, BSC testnet registry `0x8004...BD9e`), minted to the same
  wallet that trades on mainnet. Registered self-custodially: the script
  decrypts TWAK's local keystore *in memory*, verifies the derived address,
  and signs — no raw key ever touches disk (`scripts/register_identity.py`).

## Architecture

| Layer | Module | Responsibility |
|---|---|---|
| Signal / alpha | `agent/strategies/` | Pluggable strategies (default `trend`: EMA/MACD/RSI → BUY/HOLD/EXIT + conviction + grey-zone); swappable via config or `--strategy` ([docs](docs/STRATEGIES.md)) |
| Regime gate | `agent/signals/regime.py` | Global metrics + Fear&Greed → RISK_ON / CONFLICTED / RISK_OFF |
| Macro blackout | `agent/risk/macro.py` | Pause/halve entries around scheduled macro events (PCE, Fed) |
| Risk engine | `agent/risk/engine.py` | Fail-closed guardrails: allowlist, drawdown ladder, caps, slippage, min-edge |
| Execution | `agent/execution/executor.py` | Quote → price-impact check → swap via TWAK, by contract address |
| State / reconcile | `agent/state/` | Rebuilds positions from on-chain truth every cycle (restart-safe) |
| Liquidity sentinel | `agent/risk/liquidity.py` | CMC DEX API pool monitoring → defensive exit on a liquidity drain |
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
- **liquidity sentinel**: a held token's DEX pool draining ≥40% below its
  entry baseline forces a defensive exit (rug/LP-migration protection,
  CMC DEX API + deterministic CREATE2 pool derivation)
- **anti-whipsaw**: a re-entry cooldown after each exit plus a per-token edge
  floor (an entry must clear that token's own *measured* round-trip friction,
  not just a global minimum) — friction is the dominant cost, so weak entries
  are cut, not taken
- **volume confirmation**: an entry needs `volume_24h` rising vs its own
  trailing average (attention confirming the setup); backtested to roughly
  halve the fee-driven loss by dropping the weakest entries
- ≥1 trade/day compliance automation and a portfolio-floor check every cycle
- **restart-safe** (reconcile from chain) and **clean shutdown** (finishes the
  in-flight cycle, exits with no pending transaction)

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env       # add CMC + TWAK credentials (never commit .env)

python -m agent --once     # one DRY-RUN cycle (real signals, no tx signed)
python -m agent            # continuous dry-run
python -m agent --live     # real execution (explicit opt-in)
python -m agent --max-hours 6   # bounded, Telegram-watched window
python -m agent --live --start-at 2026-06-22T00:00Z --stop-at 2026-06-28T23:59Z \
    --report-every-min 720      # scheduled window (exact UTC) + periodic ops reports
pytest -q                  # 73 tests
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

## Live dashboard

A static, keyless **[BSC Wallet Inspector](dashboard/)** (GitHub Pages) shows the
live on-chain holdings and USD value of any BSC wallet — prefilled with the
competition agent wallet. Balances come from a public RPC, prices from
DexScreener; nothing server-side, no keys. Deployed from [`dashboard/`](dashboard/)
via GitHub Actions.

## Benchmarks

Honest backtest results (with caveats) are in [`docs/BENCHMARKS.md`](docs/BENCHMARKS.md):
the `trend` strategy preserves capital in the current bear regime (7d −0.13%,
20d −0.16% net) and is net positive over the year at modeled cost; the binding
constraint is fees, not signal. Every number is reproducible from `scripts/`.

## Documentation

Full reference lives in [`docs/`](docs/README.md):

- [Architecture](docs/ARCHITECTURE.md) — signal → strategy → risk → execution.
- [Strategy & risk mechanisms](docs/STRATEGY.md) · [Strategy plugins](docs/STRATEGIES.md)
  · [Benchmarks](docs/BENCHMARKS.md)
- [x402 micropayments](docs/X402.md) — paying for data and charging for it.
- [Deployment](deploy/DEPLOY.md) · [Test window](deploy/WINDOW.md)
- Governance: [Contributing](CONTRIBUTING.md) · [Code of Conduct](CODE_OF_CONDUCT.md)
  · [Security](SECURITY.md) · [Maintainers](MAINTAINERS.md)

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
- ERC-8004 identity: agentId **1375** on BSC testnet
  (registry `0x8004A818BFB912233c491871b3d84c89A494BD9e`, tx
  `0xe544f78d748a170134ccd3402257597759c7fb480066ddd8d3f3e5f34e86b5ad`)
- x402 charge endpoint (**live**): `https://bnb-hack.broncano.io` — payments
  wallet `0x7DFAA1bd437C364491D8303AC47f7B0F56a8a363` (isolated from the trading
  wallet); on-chain settlement
  `0xc32f54aaf8e5c0cf617da7928dcacc98271aa2f65def187566511798cf8f9f6a`
  (EIP-3009 `transferWithAuthorization`, 0.01 USD1, block 104327666). USD1 asset:
  `0x8d0D000Ee44948FC98c9B98A4FA4921476f08B0D` (BSC, 18 dp).

## Security

- No keys, seeds or credentials in this repo — ever. `.env`, keystores, the
  private watchlist and runtime data are gitignored.
- All signing is local (TWAK agent wallet, `~/.twak`, mounted read-only in
  Docker). No custodial component anywhere in the loop; spot execution on
  non-custodial DEX liquidity only.
- The wallet is treated as a hot wallet, funded with competition capital only.

For private disclosure of vulnerabilities, see [`SECURITY.md`](SECURITY.md).

## Sponsors

Built for the **BNB Hack: AI Trading Agent Edition**, using all three sponsor
stacks:

- [**CoinMarketCap for Agent**](https://coinmarketcap.com/api/agent/) — signals: regime, Fear & Greed, technicals, DEX liquidity.
- [**Trust Wallet Agent Kit**](https://portal.trustwallet.com/) — sole execution layer, self-custody local signing, x402.
- [**BNB AI Agent SDK**](https://github.com/bnb-chain/bnbagent-sdk) — ERC-8004 on-chain agent identity.

## License

[MIT](LICENSE) © 2026 xxAVOGADROxx. Includes a trading-risk disclaimer — this
software moves real funds; use at your own risk.
