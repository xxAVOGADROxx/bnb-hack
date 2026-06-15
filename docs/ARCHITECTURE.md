# Architecture

The agent is a single deterministic process. No language model decides trades at
runtime; the AI lives in the *signal* (CoinMarketCap's processed market
intelligence) and at *design time* (backtesting and calibration). The runtime is
reproducible, auditable, and fail-closed.

```
  CoinMarketCap for Agent        deterministic Python loop          Trust Wallet Agent Kit
   AI-processed signals    ──►   strategy + regime + RISK ENGINE  ──►  local signing + swap
   (regime, F&G, TA,             (code, not an LLM per tick)            (self-custody, x402)
    macro events, DEX             │                                     │
    liquidity)            full decision audit ◄── on-chain truth (reconcile) ──┘
```

## Cycle

1. **Reconcile** holdings from on-chain state (the wallet is the source of
   truth; restarts are always safe).
2. **Snapshot + drawdown** against the high-water mark; the drawdown ladder may
   pause entries or hard-stop to stables.
3. **Regime gate** from Global Metrics + Fear & Greed (asymmetric: extreme greed
   blocks entries; extreme fear allows only top-conviction entries at half size).
4. **Per-token signals** from technicals on hourly closes, each producing a
   conviction and an expected edge.
5. **Risk verdicts** — every proposal passes the fail-closed risk engine
   (allowlist, sizing, slippage, edge floor, cooldown, volume confirmation).
6. **Execution** through TWAK with local signing; the result and its tx hash are
   logged.
7. **Compliance** — a forced minimal trade guarantees the ≥1-trade-per-day rule.

Every decision — approved or rejected, with the rule that fired — is appended to
`data/decisions.jsonl`.

## Sponsor stack

- **CoinMarketCap for Agent** — every signal: market regime (Global Metrics,
  Fear & Greed, live and historical), per-token technicals, live quotes for the
  stop-loss, scheduled macro events, and the **DEX API** for pool-level
  liquidity monitoring. Read-only: CMC informs, TWAK executes.
- **Trust Wallet Agent Kit (TWAK)** — the sole execution layer, all signed
  locally: balances, quotes, swaps, and competition registration. Production
  drives the `twak` CLI; a REST client is also implemented.
- **x402** — both sides of the protocol: the agent **pays** for premium TA in a
  grey-zone decision, and **charges** for its live leaderboard via a built-in
  x402 server. See [X402.md](X402.md).
- **BNB AI Agent SDK** — ERC-8004 on-chain identity, registered self-custody by
  decrypting the TWAK keystore in memory and verifying the address before use.

## Module map

| Area | Location |
| ---- | -------- |
| Signals (regime, technicals, macro) | `agent/signals/` |
| Risk engine + guardrails | `agent/risk/` |
| Execution (TWAK transport) | `agent/execution/`, `agent/twak/` |
| CMC client | `agent/cmc/` |
| x402 (pay + charge) | `agent/x402/` |
| State, logging, monitoring | `agent/state/`, `agent/monitor/` |
| Backtests & tooling | `scripts/` |
| Configuration | `config/` |
