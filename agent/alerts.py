"""Human alerts: circuit breaker, failing execution, dead data feed, drawdown.

Telegram if configured; always mirrored to the standard log. Alert failures
must never take down the trading loop.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class Alerter:
    def __init__(self, bot_token: str | None, chat_id: str | None):
        self.bot_token = bot_token
        self.chat_id = chat_id

    def notify(self, message: str) -> None:
        log.warning("ALERT: %s", message)
        if not (self.bot_token and self.chat_id):
            return
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": message},
                timeout=10,
            )
        except requests.RequestException as e:
            log.error("alert delivery failed: %s", e)
