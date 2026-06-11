"""TWAK clients — Trust Wallet Agent Kit as the SOLE execution layer.

Balances, quotes, swaps, x402 payments and competition registration all go
through TWAK, and every signature is produced locally by the TWAK agent
wallet (self-custody end to end).

Two interchangeable transports:
- TwakClient      — subprocess to the `twak` CLI (vertical slice, manual ops)
- TwakRestClient  — `twak serve --rest` HTTP API (long-running process; no
                    node startup cost per call). Auth: Bearer TW_HMAC_SECRET.

make_twak_client() picks REST when TWAK_SERVE_URL is set, else the CLI.
Both always pass the chain explicitly: the CLI default is `ethereum`, so an
omitted chain would execute on the wrong network.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)


class TwakError(RuntimeError):
    pass


@dataclass(frozen=True)
class Quote:
    from_token: str
    to_token: str
    amount_usd: float
    raw: dict


class TwakClient:
    def __init__(self, chain: str = "bsc", dry_run: bool = True, timeout_s: int = 180):
        self.chain = chain
        self.dry_run = dry_run
        self.timeout_s = timeout_s

    def _run(self, *args: str) -> dict:
        cmd = ["twak", *args, "--json"]
        log.debug("twak: %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=self.timeout_s
            )
        except FileNotFoundError as e:
            raise TwakError("twak CLI not found on PATH") from e
        except subprocess.TimeoutExpired as e:
            raise TwakError(f"twak timed out: {' '.join(args[:3])}") from e
        if proc.returncode != 0:
            raise TwakError(
                f"twak exited {proc.returncode}: {proc.stderr.strip() or proc.stdout.strip()[:500]}"
            )
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise TwakError(f"twak returned non-JSON: {proc.stdout[:500]}") from e

    # -- read surfaces -----------------------------------------------------
    def balances(self) -> dict:
        # TODO(server): confirm exact output shape of `twak wallet balance --json`
        return self._run("wallet", "balance", "--chain", self.chain)

    def compete_status(self) -> dict:
        return self._run("compete", "status")  # BSC-only command, no --chain flag

    def compete_register(self) -> dict:
        return self._run("compete", "register")

    # -- trading -------------------------------------------------------------
    def quote(self, from_token: str, to_token: str, usd: float, slippage_pct: float) -> Quote:
        raw = self._run(
            "swap", from_token, to_token,
            "--chain", self.chain,
            "--usd", f"{usd:.2f}",
            "--slippage", str(slippage_pct),
            "--quote-only",
        )
        return Quote(from_token, to_token, usd, raw)

    def quote_amount(self, amount: float, from_token: str, to_token: str,
                     slippage_pct: float = 1.0) -> dict:
        """Quote by token amount instead of USD value (e.g. the return leg
        of a round-trip cost measurement)."""
        return self._run(
            "swap", f"{amount:.18f}".rstrip("0").rstrip("."), from_token, to_token,
            "--chain", self.chain,
            "--slippage", str(slippage_pct),
            "--quote-only",
        )

    def swap(self, from_token: str, to_token: str, usd: float, slippage_pct: float) -> dict:
        if self.dry_run:
            log.info("[dry-run] swap %s->%s $%.2f: quote only, no tx", from_token, to_token, usd)
            return {"dry_run": True, "quote": self.quote(from_token, to_token, usd, slippage_pct).raw}
        return self._run(
            "swap", from_token, to_token,
            "--chain", self.chain,
            "--usd", f"{usd:.2f}",
            "--slippage", str(slippage_pct),
        )

    # -- x402 micropayments ---------------------------------------------------
    def x402_request(
        self,
        url: str,
        max_payment_atomic: int,
        method: str = "GET",
        body: str | None = None,
        prefer_network: str | None = None,
    ) -> dict:
        if self.dry_run:
            log.info("[dry-run] x402 request suppressed: %s", url)
            return {"dry_run": True, "url": url}
        args = ["x402", "request", url, "--max-payment", str(max_payment_atomic), "--yes"]
        if method != "GET":
            args += ["--method", method]
        if body is not None:
            args += ["--body", body]
        if prefer_network:
            args += ["--prefer-network", prefer_network]
        return self._run(*args)


class TwakRestClient:
    """Same interface as TwakClient over `twak serve --rest`.

    Surface (verified live): POST /actions/<name> with JSON body,
    `Authorization: Bearer <TW_HMAC_SECRET>`. Quote/swap payloads use
    fromToken/toToken/fromChain/toChain/amount(string)/slippage and return
    the same {input, output, provider, priceImpact} shape as the CLI.
    """

    STABLE_SYMBOLS = ("USDT", "USDC")

    def __init__(self, base_url: str, bearer: str, chain: str = "bsc",
                 dry_run: bool = True, timeout_s: int = 180):
        self.base_url = base_url.rstrip("/")
        self.chain = chain
        self.dry_run = dry_run
        self.timeout_s = timeout_s
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {bearer}"})

    def _post(self, action: str, **payload) -> dict:
        try:
            resp = self.session.post(
                f"{self.base_url}/actions/{action}", json=payload, timeout=self.timeout_s
            )
        except requests.RequestException as e:
            raise TwakError(f"twak serve unreachable ({action}): {e}") from e
        try:
            data = resp.json()
        except ValueError as e:
            raise TwakError(f"twak serve non-JSON ({action}): {resp.text[:300]}") from e
        if resp.status_code != 200 or data.get("success") is False or data.get("code"):
            raise TwakError(
                f"twak serve {action} -> {resp.status_code}: "
                f"{data.get('message') or data.get('error') or str(data)[:300]}"
            )
        return data

    # -- read surfaces -----------------------------------------------------
    def balances(self) -> dict:
        """Native + token holdings merged into the CLI `wallet balance` shape
        so the reconcile parser works unchanged."""
        native = self._post("wallet_balance", chain=self.chain)
        holdings = self._post(
            "get_token_holdings", chain=self.chain, address=native.get("address")
        )
        tokens = next((v for v in holdings.values() if isinstance(v, list)), [])
        bal = native.get("balance") or {}
        return {
            "chain": self.chain,
            "address": native.get("address"),
            "symbol": "BNB",
            "available": bal.get("available", "0"),
            "total": bal.get("total", "0"),
            "tokens": tokens,
        }

    def compete_status(self) -> dict:
        return self._post("competition_status")

    def compete_register(self) -> dict:
        return self._post("competition_register")

    # -- trading -------------------------------------------------------------
    def _amount_for_usd(self, token_ref: str, usd: float) -> float:
        """REST quotes take token amounts, not USD — convert via TWAK's own
        price action (stables short-circuit to 1:1)."""
        if token_ref.upper() in self.STABLE_SYMBOLS:
            return usd
        price = float(
            self._post("get_token_price", token=token_ref, chain=self.chain)["priceUsd"]
        )
        if price <= 0:
            raise TwakError(f"non-positive price for {token_ref}")
        return usd / price

    def quote(self, from_token: str, to_token: str, usd: float, slippage_pct: float) -> Quote:
        amount = self._amount_for_usd(from_token, usd)
        raw = self._post(
            "get_swap_quote",
            fromToken=from_token, toToken=to_token,
            fromChain=self.chain, toChain=self.chain,
            amount=f"{amount:.10f}".rstrip("0").rstrip("."),
            slippage=slippage_pct,
        )
        return Quote(from_token, to_token, usd, raw)

    def quote_amount(self, amount: float, from_token: str, to_token: str,
                     slippage_pct: float = 1.0) -> dict:
        return self._post(
            "get_swap_quote",
            fromToken=from_token, toToken=to_token,
            fromChain=self.chain, toChain=self.chain,
            amount=f"{amount:.10f}".rstrip("0").rstrip("."),
            slippage=slippage_pct,
        )

    def swap(self, from_token: str, to_token: str, usd: float, slippage_pct: float) -> dict:
        if self.dry_run:
            log.info("[dry-run] swap %s->%s $%.2f: quote only, no tx", from_token, to_token, usd)
            return {"dry_run": True, "quote": self.quote(from_token, to_token, usd, slippage_pct).raw}
        amount = self._amount_for_usd(from_token, usd)
        return self._post(
            "swap",
            fromToken=from_token, toToken=to_token,
            fromChain=self.chain, toChain=self.chain,
            amount=f"{amount:.10f}".rstrip("0").rstrip("."),
            slippage=slippage_pct,
        )

    # -- x402 micropayments ---------------------------------------------------
    def x402_request(self, url: str, max_payment_atomic: int, method: str = "GET",
                     body: str | None = None, prefer_network: str | None = None) -> dict:
        if self.dry_run:
            log.info("[dry-run] x402 request suppressed: %s", url)
            return {"dry_run": True, "url": url}
        payload: dict = {"url": url, "method": method, "maxPayment": str(max_payment_atomic)}
        if body is not None:
            payload["body"] = body
        if prefer_network:
            payload["preferNetwork"] = prefer_network
        # TODO(x402): CMC's MCP root answers 200 to plain POSTs — the 402
        # challenge lives inside MCP JSON-RPC tool calls; frame the body
        # accordingly when the premium branch goes live.
        return self._post("x402_request", **payload)


# Both transports expose the same surface (balances/quote/swap/x402/...).
AnyTwak = TwakClient | TwakRestClient


def make_twak_client(chain: str = "bsc", dry_run: bool = True) -> AnyTwak:
    """REST when TWAK_SERVE_URL is configured (long-running deployments),
    subprocess CLI otherwise (dev / manual ops)."""
    url = os.environ.get("TWAK_SERVE_URL")
    bearer = os.environ.get("TW_HMAC_SECRET")
    if url and bearer:
        log.info("TWAK transport: REST (%s)", url)
        return TwakRestClient(url, bearer, chain=chain, dry_run=dry_run)
    log.info("TWAK transport: CLI subprocess")
    return TwakClient(chain=chain, dry_run=dry_run)
