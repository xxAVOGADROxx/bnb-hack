# Strategy & risk mechanisms

The strategy is deterministic and signal-driven. Mechanisms are adopted — and
rejected — on backtest evidence; see [`CHANGELOG.md`](../CHANGELOG.md) for the
full history. The private token watchlist and its calibration are not part of
the public repository.

## Signal

Per-token technicals on hourly closes (EMA structure, RSI, MACD) produce a
conviction score and a conservative expected-edge estimate. An entry needs all
conditions to align; a grey-zone (near-miss) setup can trigger one paid premium
data pull via x402 before committing.

## Regime gate

An asymmetric gate over Fear & Greed and Global Metrics: extreme greed blocks
new entries; extreme fear allows only top-conviction entries, at half size.
Calibrated against real historical Fear & Greed.

## Risk engine (fail-closed)

| Mechanism | Behaviour |
| --------- | --------- |
| Drawdown ladder | From the high-water mark: alert → pause entries → hard-stop and flatten to stables, well inside the official cap. |
| Allowlist | Hard list of eligible tokens; anything else is rejected. |
| Position & day limits | Max size, max concurrent positions, per-day trade cap. |
| Slippage / price impact | Quote rejected above tolerance on every swap. |
| Vol-targeted sizing | Scale a position down when the token is more volatile than target, so each contributes a similar risk budget. |
| Stop-loss | A hard floor below the signal exit, checked against a live quote. |
| Liquidity sentinel | Exit a held token whose DEX pool drains past a threshold (rug / LP-migration protection). |
| Anti-whipsaw | Re-entry cooldown plus a per-token edge floor over each token's own measured friction. |
| Volume confirmation | An entry needs `volume_24h` rising versus its trailing average. |

## The fee insight

Backtesting showed the strategy's *gross* PnL is mildly positive while round-trip
fees flip it negative. The binding constraint is friction, not signal — so the
mechanisms that improved net results are those that trade **less and better**
(volume confirmation, edge floor, cooldown), not those that add size or
frequency. Several mechanisms (long-term trend filter, adaptive stop, trailing
stop, watchlist expansion) were tested and rejected because they did not improve
net results in the tested regime.
