# Benchmarks

Honest backtest results for the production `trend` strategy. The point of this
page is evidence, so the caveats come first — the numbers mean nothing without
them.

## Caveats (read first)

- **Prices are CEX-aggregated** (CoinMarketCap). Real fills against BSC DEX
  liquidity are worse, so these are an **upper bound**. Measured round-trip
  friction is applied in every run (1.5% baseline; a 3% stress case where shown).
- **Intraday history is capped at ~1 month** on our plan, so the 7-day and
  20-day windows test the **current regime only** — a broad, fearful drawdown.
  That mostly exercises capital preservation, not upside capture.
- **The 1-year view uses daily bars** with the 1h-tuned parameters, so it is
  **directional**, not the live config. It is included for a longer horizon, not
  as a precise live estimate.
- Reproduce everything yourself with the scripts below; nothing here is hidden.

## `trend` strategy — net return, live config

| Window | Net return | Max drawdown | Note |
| ------ | ---------- | ------------ | ---- |
| 7-day (intraday) | **−0.13%** | < 0.5% | fearful week; capital preserved |
| 20-day (intraday) | **−0.16%** | ~0.7% | with the volume filter (−0.65% without) |
| 1-year (daily, directional) | **+4.97%** @ 1.5% cost · **+0.55%** @ 3% cost | 13.6% | 1h params on daily — directional only |

**The core finding:** across the intraday windows the strategy's *gross* PnL is
mildly **positive**; ~1.5% round-trip fees flip it slightly negative. The binding
constraint is **friction, not signal** — which is why the mechanisms that help
are the ones that trade *less and better*, not more. The agent is built for
capital preservation in this bear regime, not upside capture: over the full year
the defensive approach was net positive at modeled cost while the average
watchlist token fell 40%+.

## What the volume filter (#11) bought

Adding the `volume_24h`-rising entry gate, same windows:

| Window | Without filter | With filter | Gross PnL |
| ------ | -------------- | ----------- | --------- |
| 7-day | −0.38% | **−0.13%** | −10.88 → −4.22 |
| 20-day | −0.65% | **−0.16%** | +9.65 → **+26.11** |

It cut the worst gross-negative entries and roughly **halved the fee-driven
loss**. Tighter ratios overshoot — 1.0× is the robust setting.

## Strategy comparison — `trend` vs `mean_reversion`

Head-to-head on the live config (`scripts/strategy_bt.py`):

| Window | `trend` net | `mean_reversion` net |
| ------ | ----------- | -------------------- |
| 7-day | −0.13% | +0.09% (1 trade — noise) |
| 20-day | **−0.41%** | **−5.49%** |
| 1-year | **−1.65%** | **−6.47%** |

`mean_reversion` is gross-negative on the meaningful windows — naive oversold
buying catches falling knives. It is kept only as a worked plugin example; the
production strategy is `trend`. See [STRATEGIES.md](STRATEGIES.md).

## Reproduce

```bash
.venv/bin/python scripts/backtest.py            # full grid + regime gate (#4)
.venv/bin/python scripts/vol_filter_bt.py       # volume-filter A/B (#11)
.venv/bin/python scripts/trade_split.py         # per-token, per-trade with fees
.venv/bin/python scripts/year_forecast.py       # 1-year daily + regime forecast
.venv/bin/python scripts/strategy_bt.py         # trend vs mean_reversion
```

Every change to strategy or risk behaviour is held to this bar — see
[CONTRIBUTING.md](../CONTRIBUTING.md) and [../CHANGELOG.md](../CHANGELOG.md) for
the mechanisms adopted *and rejected* on this evidence.
