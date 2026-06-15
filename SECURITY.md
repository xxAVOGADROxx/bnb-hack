# Security Policy

This project signs blockchain transactions with a self-custody wallet. Security
is treated as a first-class concern, not an afterthought.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 1.0.x   | ✅        |
| < 1.0   | ❌        |

## Reporting a vulnerability

**Do not open a public issue for security vulnerabilities.**

Report privately through one of the following channels:

- GitHub's **[private vulnerability reporting](https://github.com/xxAVOGADROxx/bnb-hack/security/advisories/new)**
  (Security → Report a vulnerability), or
- email **jose.seraquive@gmail.com** with the subject line `SECURITY: bnb-hack`.

Please include a description, reproduction steps, and the potential impact. You
can expect an acknowledgement within 72 hours and a remediation plan once the
report is triaged. Responsible disclosure is appreciated; please allow a
reasonable window for a fix before any public disclosure.

## Key-handling guarantees

The agent is designed so that signing authority never leaves the machine:

- **Local signing only.** All transactions are signed by the Trust Wallet Agent
  Kit (TWAK) keystore on the host. No remote signer, MPC custodian, or exchange
  API is in the execution path.
- **Keys never touch the repository.** `.env`, any keystore, and
  `config/watchlist.local.yaml` are gitignored. A leaked key in git history is
  treated as a drained wallet — never commit secrets.
- **In-memory key use, verified.** Where a private key must be derived (ERC-8004
  identity, x402 settlement), the TWAK keystore is decrypted **in memory**
  (PBKDF2-SHA256 600k + AES-256-GCM), the derived address is checked against the
  expected wallet **before** any use, and the key is never persisted
  (`agent/keys.py`).
- **Non-custodial venues only.** Execution is restricted to on-chain DEX venues
  (PancakeSwap). No centralized-exchange custody at any step.

## Operational hardening (deployment)

- Fund with the minimum competition capital; treat the agent wallet as a hot
  wallet.
- Run on a hardened host: SSH key-only login, minimal open ports, patched OS.
- Back up the encrypted keystore offline and store its password separately in a
  password manager.
- The container mounts `~/.twak` **read-only**; the wallet password is supplied
  via an environment variable, never written to disk in plaintext.
