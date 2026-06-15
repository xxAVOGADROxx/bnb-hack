# Maintainers

This file lists the people responsible for reviewing contributions, triaging
issues, and stewarding releases.

## Current maintainers

| Maintainer    | GitHub                                          | Areas |
| ------------- | ----------------------------------------------- | ----- |
| Project lead  | [@xxAVOGADROxx](https://github.com/xxAVOGADROxx) | Architecture, strategy & risk engine, sponsor integrations, releases |

Contact for non-public matters: **jose.seraquive@gmail.com**
(security reports follow [`SECURITY.md`](SECURITY.md)).

## Responsibilities

- Review and merge pull requests against the standards in
  [`CONTRIBUTING.md`](CONTRIBUTING.md).
- Triage issues and security reports within a reasonable window.
- Guard the two non-negotiables: **deterministic runtime** and
  **evidence-backed strategy changes**.
- Cut releases following the process below.

## Decision process

Changes are accepted by maintainer review. Strategy and risk changes require
backtest evidence in the pull request. When maintainers disagree, the project
lead has the final decision.

## Release process

Releases follow [Semantic Versioning](https://semver.org/).

1. Ensure `pytest -q` is green and `CHANGELOG.md` is up to date.
2. Bump `version` in `pyproject.toml`.
3. Tag the release: `git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z`.
4. Publish the GitHub release with notes drawn from the changelog.

## Becoming a maintainer

Sustained, high-quality contributions and good judgement on the project's
principles are the path to maintainership. Existing maintainers extend the
invitation.
