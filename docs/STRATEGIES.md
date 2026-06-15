# Strategy plugins

Strategies are pluggable. A strategy generates a signal for one token; the
universal guardrails (regime gate, volume confirmation, edge floor, cooldown,
vol-targeted sizing, stop-loss) live in the loop and the risk engine and apply
to whatever strategy is active. So strategies stay small and swappable, and the
safety layer is shared and never bypassed.

## The contract

A strategy implements one method (`agent/strategies/base.py`):

```python
class Strategy(Protocol):
    name: str
    def evaluate(self, ctx: MarketContext) -> Signal: ...
```

`MarketContext` carries the market data for one token this cycle (`token`,
`closes`, `volumes`, `holding`). `Signal` is the standard action +
conviction + expected edge the loop already consumes.

## Adding a strategy

1. Create `agent/strategies/<name>.py` with a class implementing the protocol
   (set `name`, implement `evaluate`).
2. Register it — one line in `agent/strategies/registry.py`:

   ```python
   _BUILDERS = {
       TrendStrategy.name: TrendStrategy,
       MyStrategy.name: MyStrategy,   # <- add here
   }
   ```

3. Add a test, and **backtest it** before shipping — nothing becomes the active
   strategy without evidence (see [CONTRIBUTING.md](../CONTRIBUTING.md)).

That is the whole surface: the 402/sizing/risk machinery is untouched.

## Switching strategies

- **Config** — `config/risk.yaml`:

  ```yaml
  strategy:
    active: trend          # or mean_reversion
  ```

- **CLI** — override at launch: `python -m agent --strategy mean_reversion`.

The active strategy is logged at startup and written on every `signal` decision,
so the audit trail records which strategy produced each call.

## Bundled strategies

| Name | Status | Idea |
| ---- | ------ | ---- |
| `trend` | **default, validated** | EMA structure + MACD + RSI; all conditions align for entry, with dynamic conviction and the x402 grey-zone tie-break. |
| `mean_reversion` | experimental | Buy statistically stretched oversold dips, exit on reversion to the mean. On-thesis with the current regime, but not yet through the full backtest pipeline. |
