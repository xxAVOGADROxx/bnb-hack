# Contributing

Thank you for your interest in contributing. This document describes how to
propose changes, the standards the project holds, and the workflow for getting
a change merged.

By participating you agree to abide by the [Code of Conduct](CODE_OF_CONDUCT.md).

## Guiding principles

This is an autonomous, self-custody trading agent. Two principles govern every
change:

1. **Determinism over cleverness.** The live loop is deterministic code, not an
   LLM deciding per tick. Contributions must keep the trading path reproducible
   and fully auditable. The AI lives in the *signal* and at *design time*, never
   in the runtime trigger.
2. **Evidence over opinion.** Any change to strategy or risk behaviour must be
   accompanied by backtest evidence (`scripts/backtest.py` and the focused
   experiment scripts). We adopt and *reject* mechanisms based on the data;
   "it feels better" is not sufficient. See [`CHANGELOG.md`](CHANGELOG.md) for
   the standard a strategy entry is held to.

## Development setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[ta,dev]"
cp .env.example .env        # add CMC + TWAK credentials (never commit .env)
pytest -q                   # the suite must pass before you start
```

## Workflow

1. **Open an issue first** for anything non-trivial, so the approach can be
   agreed before code is written.
2. **Branch** from `main` (`feature/<short-name>` or `fix/<short-name>`).
3. **Keep changes focused.** One logical change per pull request.
4. **Add tests.** New logic needs unit tests; risk and strategy changes need a
   backtest run quoted in the PR description.
5. **Run the suite** (`pytest -q`) and confirm it is green.
6. **Open a pull request** against `main` using the template, describing what
   changed, why, and the evidence.

## Coding standards

- **Python ≥ 3.10**, type hints on public functions, module-level docstrings
  that explain *why*, not just *what*.
- **Match the surrounding style** — naming, comment density, and idioms should
  read like the existing code.
- **No secrets, ever.** Keys, seeds, credentials, and the real watchlist never
  appear in code, tests, logs, or commit history.
- **Fail closed.** New guardrails default to the safe state; dry-run is the
  default at every layer and live execution is an explicit opt-in.

## Commit and PR conventions

- Write imperative, descriptive commit subjects (e.g. "Add volume-confirmation
  entry gate"). Explain the reasoning in the body.
- Reference the issue the PR closes.
- Update [`CHANGELOG.md`](CHANGELOG.md) for any user-visible or strategy change.

## Security

Never report a vulnerability in a public issue or PR. Follow
[`SECURITY.md`](SECURITY.md) for private disclosure.
