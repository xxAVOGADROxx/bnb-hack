"""Thin pass-through proxy that fixes the CMC x402 MCP Accept-header bug.

The pay-side blocker (see agent/x402/premium.py): CMC's MCP uses the
StreamableHTTP transport, which per the MCP spec requires the client to send
`Accept: application/json, text/event-stream` on POST — the server may answer
with either a JSON body or an SSE stream. twak's paid x402 retry sends only
`Accept: application/json`, so CMC returns HTTP 400 *before* settling (no funds
move) and the paid TA tie-break never lands. The CLI exposes no header override.

This proxy sits between twak and CMC and does exactly one thing: it forwards
every request verbatim — method, path, body, and ALL headers including the
x402 payment headers (X-PAYMENT / PAYMENT-SIGNATURE) and the response's
`payment-required` / `X-PAYMENT-RESPONSE` headers — but rewrites the outgoing
`Accept` to `application/json, text/event-stream`. CMC then answers 402 on the
first call and 200 on the paid retry, and the existing premium.py path lights
up with no change to it.

Point the agent at the proxy by setting in .env:
    CMC_X402_MCP_URL=http://127.0.0.1:8403/x402/mcp
(premium.py reads that, defaulting to the direct CMC URL when unset, so the
proxy is fully opt-in and changes nothing until you enable it).

Usage: .venv/bin/python -m agent.x402.sse_proxy [--port 8403]
       [--upstream https://mcp.coinmarketcap.com]
"""
from __future__ import annotations

import argparse
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

log = logging.getLogger("x402.sse_proxy")

# The Accept value the StreamableHTTP transport requires (and twak omits).
ACCEPT = "application/json, text/event-stream"

# Hop-by-hop headers we must not relay; Accept is rewritten, Host/Content-Length
# are set by requests / our responder, so drop the inbound copies.
_DROP_REQUEST = {"accept", "host", "content-length", "connection",
                 "keep-alive", "proxy-authenticate", "proxy-authorization",
                 "te", "trailers", "transfer-encoding", "upgrade"}
_DROP_RESPONSE = {"content-length", "content-encoding", "transfer-encoding",
                  "connection", "keep-alive"}


def make_handler(upstream: str):
    base = upstream.rstrip("/")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info("%s %s", self.address_string(), fmt % args)

        def _proxy(self):
            if self.path in ("/healthz", "/health"):
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"ok")
                return

            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None

            # Forward all inbound headers except the ones we drop/rewrite —
            # crucially this preserves X-PAYMENT / PAYMENT-SIGNATURE so the
            # x402 settlement still happens at CMC.
            fwd = {k: v for k, v in self.headers.items()
                   if k.lower() not in _DROP_REQUEST}
            fwd["Accept"] = ACCEPT  # the one fix this proxy exists for

            url = base + self.path
            try:
                resp = requests.request(
                    self.command, url, headers=fwd, data=body,
                    timeout=60, stream=False)
            except requests.RequestException as e:  # upstream unreachable
                log.error("upstream error: %s", e)
                msg = f'{{"error":"proxy upstream failed: {e}"}}'.encode()
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
                return

            content = resp.content
            self.send_response(resp.status_code)
            # Relay CMC's headers verbatim (payment-required, X-PAYMENT-RESPONSE,
            # Content-Type, …) minus hop-by-hop; reset Content-Length ourselves.
            for k, v in resp.headers.items():
                if k.lower() not in _DROP_RESPONSE:
                    self.send_header(k, v)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        do_GET = _proxy
        do_POST = _proxy

    return Handler


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8403)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--upstream", default="https://mcp.coinmarketcap.com")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("x402 SSE proxy on %s:%d -> %s (injecting Accept: %s)",
             args.host, args.port, args.upstream, ACCEPT)
    ThreadingHTTPServer((args.host, args.port),
                        make_handler(args.upstream)).serve_forever()


if __name__ == "__main__":
    main()
