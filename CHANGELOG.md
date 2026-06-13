# Changelog — how the agent has evolved

Newest first. Each entry: what changed, why, and (for strategy changes) the
evidence. The backtest is one ~1-month window — treat absolute numbers as
directional, the *relative* comparisons as the signal.

## Strategy model

### Anti-whipsaw: re-entry cooldown + per-token edge floor (#9)
- **24h re-entry cooldown.** After closing a token, no re-entry for 24h. The
  same-day BUY→exit→BUY churn pays double friction for ~zero edge.
  *Evidence (720h, live cfg + asym gate, two independent windows):* return
  +0.5 to +0.6pp, win rate 29%→36-42%, fewer trades. Improved in both.
- **Per-token edge floor.** An entry's expected edge must clear the token's
  OWN measured round-trip friction + 0.5% (from `data/liquidity_report.json`),
  not just the global 2% minimum — ETH (1.3%) trades cheaper than TRX (2.0%).
  *Evidence:* +0.3pp return, −13% maxDD on top of the cooldown, both windows.
- **Trailing stop evaluated and REJECTED.** Locking winners with a peak-drop
  exit flipped sign between adjacent windows (+0.4% → −0.4%) — not robust;
  the EMA-loss exit already owns that role. Documented like the watchlist
  rejection: the backtest decides, both ways.

### Liquidity sentinel — CMC DEX API pool monitoring (#7)
- New guardrail for the tail risk every price-based exit lags: **liquidity
  draining out of the pool** (rug, LP migration, panic withdrawal). On entry
  the agent records the token's reference-pool liquidity (CMC **DEX API**,
  pool-level USD liquidity); every cycle a held token's pool is re-checked and
  a ≥40% drain below the entry baseline forces a defensive exit
  (`liquidity_exit` in the decision log).
- Reference pools are derived **deterministically** (PancakeSwap v2 CREATE2:
  factory + keccak256, unit-tested against the canonical CAKE/WBNB pool)
  because the DEX API's discovery endpoint ignores its documented filters.
  Tokens without a ≥$100k v2 reference pool (ZEC, BCH — their depth lives on
  other venues the execution aggregator routes through) are *uncovered*:
  fail-open, logged once, no forced action. 10/12 watchlist tokens covered.

### Asymmetric regime gate, calibrated on real Fear & Greed (#4)
- The v1 gate was a placeholder: F&G extreme (≤20 or ≥80) → no entries at all,
  symmetric. A 12h dry-run (12 jun) spent the **entire window in extreme fear**:
  108 buy signals cleared the edge gate and the regime rule blocked every one —
  correctly (the strongest blocked signal, ZEC, fell 4.3% in the window). But
  symmetric blocking means a fear-pinned live week degenerates to compliance
  trades only.
- v2 is **asymmetric**: extreme greed (≥80) still blocks all entries (chasing
  euphoria); extreme fear (≤20) allows entries at **half scale AND only above a
  conviction floor** — our entries already require trend=up + MACD bull, so in
  deep fear they are confirmed bounces, not falling knives.
- The backtest now simulates the gate with **real daily historical F&G**
  (previously it assumed RISK_ON throughout). *Evidence (720h window containing
  8 extreme-fear days, live cfg):* gate off −2.52% / DD 3.62% · v1 −0.87% /
  DD 1.92% · **asym(floor 0.50) −0.88% / DD 1.96% with 14 vs 11 trades** — the
  floor was calibrated by sweep (0.45/0.50/0.55); 0.50 matches the full block's
  protection while keeping entry optionality if fear persists all week.

### Watchlist expansion — evaluated and REJECTED (evidence)
- Hypothesis: only high-volatility tokens clear the fee-aware edge gate, so add
  more of them. Scanned all eligible candidates with ≥$20M volume (53), ranked
  by 7-day avg daily range, friction-tested the leaders with real round-trip
  quotes — 4 survived ≤2% (avg daily ranges 6.6–14%).
- **Per-token marginal backtest said no**: each candidate made the portfolio
  worse on the same window/config (base −1.79%; +WLFI −2.22%, +NEX −2.54%,
  +ZAMA −2.64%, +XPL −4.30%). Fresh high-vol listings in post-listing
  downtrends chop a trend-follower to death. Breadth is not free alpha.
- Kept the **repeatable pipeline** instead: volatility scan → friction filter
  (`scripts/liquidity_filter.py`) → marginal A/B (`scripts/backtest.py
  --extra-tokens`). Re-run on fresh data before the live window; a candidate
  joins only with non-negative marginal evidence.

### Live-quote stop-loss check (#6)
- The stop-loss compared against the latest *hourly close* — up to an hour
  stale. It now checks CMC's live quote (~1 min fresh, one batched call for
  held tokens), so an intra-hour dump is caught by the next 5-minute cycle.
  Degrades to the hourly close if the quote call fails.

### Measure like the judge: value native BNB
- Reconcile valued only eligible BEP-20s; native BNB (gas) was logged at $0
  "by design". But scoring is % start→end of the wallet's capital — the judge
  counts BNB. Now valued (still never traded: the allowlist gates trading).
  Without this, the drawdown ladder and reported return drift from the judged
  number by the gas balance.

### Risk-managed sizing & exits — vol-targeting (#2) + stop-loss (#3)
- **Volatility-targeted sizing (risk parity).** Position size is now scaled down
  for tokens whose average daily range exceeds a target, so each position
  contributes a similar risk budget (`config/risk.yaml: position.vol_target_pct`).
  Since the strategy mostly trades the higher-volatility names (only they clear
  the edge gate), this stops them from dominating drawdown.
  *Evidence (same window, default params, cost 1.5%, edge 2%):* return
  −3.35% → **−2.19%**, **max drawdown 5.30% → 3.34% (−37%)**, same trades/win.
- **Hard stop-loss.** A position that falls `stop_loss_pct` from its entry is
  cut immediately — a backstop below the EMA-loss signal exit, protecting the
  drawdown (DQ) gate against a fast gap-down between 5-minute cycles. Needs the
  entry price, which the chain can't tell us, so it's tracked in state
  (restart-safe: a holding with no recorded entry adopts the current price).
  *Evidence:* in the backtest it never fired (the EMA-loss exit already cuts
  before −8%) — it's tail insurance, not a behaviour change. Kept as cheap
  protection for the constraint that matters most.

### Dynamic conviction, decoupled from the edge gate (#1)
- Conviction was pinned at 0.30 for every entry (no differentiation, and it
  accidentally acted as the edge filter). Replaced with a composite score
  (EMA trend spread + MACD histogram + RSI headroom) that scales position size
  with setup quality. The min-edge gate is now **decoupled**: it uses a fixed
  conservative fraction of the daily range, so stronger conviction can't loosen
  selectivity. Conviction drivers are logged in each signal (`conv(t/m/r)`).
  *Evidence (same window, default, cost 1.5%, edge 2%):* −3.39% / 13.3% win
  (flat 0.30) → **−2.08% / 29.4% win**, same ~20 trades. First coupling attempt
  loosened the gate (48 trades, −9.8%) and was rejected — the decoupling is the
  fix.

## Execution & deployment

### Test-window fidelity: dry-run mirrors live
- **Dry-run trades now count toward the daily ledger.** They didn't, so the
  20:00 UTC compliance trade retried every cycle in test windows (live was
  unaffected: real trades were always recorded). Dry-run now exercises the
  same cadence rules (compliance once, daily cap) as live.
- **`--paper-equity N` (dry-run only):** sizes entries as if the portfolio
  were $N, so a test window exercises the full entry path — proposal → risk
  engine verdict → quote. With tiny test capital every entry silently died at
  the $10 minimum (12 jun 2h window: DOGE/FLOKI cleared both regime gates and
  never reached the risk engine). That skip is now logged
  (`entry_skipped / below_min_size`). Live sizing always uses on-chain truth.

### Reliable live execution (canary-validated)
- Exits sell the **exact on-chain token amount** (not a USD-derived amount that
  could oversell and revert). Execution uses the **`twak` CLI transport**, which
  sends *and waits for* the token approval before the swap (the REST `swap`
  action fires both together and reverts on the approval race). Validated with a
  real `--canary` round-trip (USDT→CAKE→USDT) that ends flat. See the tooling
  findings in the private defense notes.
- Added `--canary` (one small real round-trip to validate live signing) and
  `--max-hours N` (bounded, self-stopping windows). Clean shutdown on SIGTERM
  (finish the in-flight cycle, exit with no pending tx).

### On-chain truth & bootstrap
- Reconcile reads token balances **on-chain** (`balanceOf` via Multicall3,
  `agent/chain.py`) instead of TWAK's indexer (empty for an un-indexed wallet).
- `id_map` / address caches **auto-bootstrap** on a fresh clone (gitignored,
  regenerable reference data).

## Infrastructure

### Scheduled window + periodic ops reports (#10)
- `--start-at` / `--stop-at` (exact UTC, e.g. `2026-06-22T00:00Z`): the agent
  waits for the window, trades it, and stops cleanly at the end — no human
  timezone arithmetic. Crash-restart safe: re-reads the bounds (waits again /
  resumes / exits quietly if the window is over).
- `--report-every-min N`: every N minutes the agent writes a uniquely-named
  ops report to `data/reports/` (decision digest: signals, blocks by rule,
  trades with approximate round-trip PnL — plus portfolio and the live
  leaderboard standing) and pushes a summary to Telegram. A final report is
  emitted on shutdown. `scripts/daily_digest.py` produces the same report on
  demand. This is the human/LLM review loop: audit each period, calibrate
  between days — the LLM stays out of the tick loop.


### The agent charges x402, not just pays it (#8)
- New x402 V2 **server** (`agent/x402/server.py`): sells the read-only
  competition leaderboard at 0.01 USD1/call. Speaks the same dialect as CMC's
  x402 MCP (402 + base64 `payment-required`, `exact`/eip155:56/eip3009), so
  `twak x402 request` pays it with zero client work. EIP-712 signature
  verified off-chain; settlement submitted on-chain by the agent
  (`transferWithAuthorization` — the token's authorization nonce makes
  replays impossible). Validated end-to-end with a REAL self-payment:
  twak signed, the server verified + settled on BSC, and returned the data.
- Settlement signer reuses the in-memory key pattern (`agent/keys.py`,
  shared with the ERC-8004 registration): TWAK's keystore stays the single
  source of truth, address always verified before use, nothing on disk.

### On-chain agent identity (ERC-8004, BNB AI Agent SDK)
- The agent is registered in the ERC-8004 identity registry on BSC testnet:
  **agentId 1375**, minted to the same wallet that trades on mainnet, with
  competition + mainnet-wallet metadata on-chain. Gas-free via the MegaFuel
  paymaster. Registration is self-custody-consistent:
  `scripts/register_identity.py` decrypts TWAK's local keystore **in memory**
  (PBKDF2-600k + AES-GCM), verifies the derived address equals the competition
  wallet before any chain interaction, and hands the key to the SDK with
  `persist=False` — `~/.twak` remains the single source of truth. All three
  sponsor stacks are now in use: CMC (signal) + TWAK (execution) + BNB SDK
  (identity).
- `--flatten` one-shot: close every position into USDT (end of the
  competition window locks the judged result; also an emergency de-risk).
- Docker deploy (single agent process, CLI transport, restart watchdog,
  `stop_grace_period`); read-only leaderboard monitor; macro-event blackout
  calendar; Telegram tx + hourly balance notifications; competition registered
  on-chain.

---
*Tunables (conviction weights, edge ref, vol target, stop %) live in
`config/risk.yaml` and `SignalParams` — calibrate via `scripts/backtest.py`.*
