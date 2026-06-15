# Documentation

Organized reference for the agent. Start with the [project README](../README.md)
for the overview.

## Contents

| Document | Purpose |
| -------- | ------- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | System design: signal → strategy → risk → execution, and how the sponsor stack is used. |
| [STRATEGY.md](STRATEGY.md) | The strategy and risk mechanisms, with the backtest evidence behind each. |
| [STRATEGIES.md](STRATEGIES.md) | The strategy plugin system — how to add a strategy and switch between them. |
| [BENCHMARKS.md](BENCHMARKS.md) | Honest backtest results (with caveats) and how to reproduce them. |
| [X402.md](X402.md) | The x402 micropayment integration — paying for premium data and charging for the leaderboard. |
| [../deploy/DEPLOY.md](../deploy/DEPLOY.md) | Server deployment for the unattended trading week (Docker / systemd). |
| [../deploy/WINDOW.md](../deploy/WINDOW.md) | Running a bounded, Telegram-watched test window. |
| [../CHANGELOG.md](../CHANGELOG.md) | How the strategy and execution have evolved, with evidence. |

## Project governance

- [Contributing](../CONTRIBUTING.md)
- [Code of Conduct](../CODE_OF_CONDUCT.md)
- [Security policy](../SECURITY.md)
- [Maintainers](../MAINTAINERS.md)
