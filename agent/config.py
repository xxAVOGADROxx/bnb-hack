"""Configuration loading: .env + YAML -> typed, frozen config objects."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"


@dataclass(frozen=True)
class DrawdownConfig:
    alert_pct: float
    pause_entries_pct: float
    hard_stop_pct: float


@dataclass(frozen=True)
class RiskConfig:
    drawdown: DrawdownConfig
    max_position_pct: float
    max_concurrent: int
    max_trades_per_day: int
    max_slippage_pct: float
    min_expected_edge_pct: float
    daily_trade_deadline_utc: str
    min_portfolio_usd: float
    max_signal_age_min: int
    cycle_interval_s: int
    regime_cache_min: int


@dataclass(frozen=True)
class TokensConfig:
    allowlist: tuple[str, ...]
    watchlist: tuple[str, ...]
    stables: tuple[str, ...]


@dataclass(frozen=True)
class AppConfig:
    cmc_api_key: str
    chain: str  # always "bsc"
    dry_run: bool
    risk: RiskConfig
    tokens: TokensConfig
    telegram_bot_token: str | None
    telegram_chat_id: str | None


def _load_yaml(name: str) -> dict:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_config(dry_run: bool = True) -> AppConfig:
    load_dotenv(ROOT / ".env")
    r = _load_yaml("risk.yaml")
    t = _load_yaml("tokens.yaml")

    risk = RiskConfig(
        drawdown=DrawdownConfig(
            alert_pct=float(r["drawdown"]["alert_pct"]),
            pause_entries_pct=float(r["drawdown"]["pause_entries_pct"]),
            hard_stop_pct=float(r["drawdown"]["hard_stop_pct"]),
        ),
        max_position_pct=float(r["position"]["max_position_pct"]),
        max_concurrent=int(r["position"]["max_concurrent"]),
        max_trades_per_day=int(r["limits"]["max_trades_per_day"]),
        max_slippage_pct=float(r["limits"]["max_slippage_pct"]),
        min_expected_edge_pct=float(r["limits"]["min_expected_edge_pct"]),
        daily_trade_deadline_utc=str(r["compliance"]["daily_trade_deadline_utc"]),
        min_portfolio_usd=float(r["compliance"]["min_portfolio_usd"]),
        max_signal_age_min=int(r["data"]["max_signal_age_min"]),
        cycle_interval_s=int(r["data"]["cycle_interval_s"]),
        regime_cache_min=int(r["data"]["regime_cache_min"]),
    )
    # The real watchlist is private: config/watchlist.local.yaml (gitignored)
    # overrides the empty placeholder committed to the public repo.
    watchlist = tuple(t.get("watchlist") or ())
    local = CONFIG_DIR / "watchlist.local.yaml"
    if local.exists():
        with open(local, encoding="utf-8") as f:
            watchlist = tuple((yaml.safe_load(f) or {}).get("watchlist") or watchlist)

    tokens = TokensConfig(
        allowlist=tuple(t.get("allowlist") or ()),
        watchlist=watchlist,
        stables=tuple(t.get("stables") or ()),
    )

    cmc_key = os.environ.get("CMC_PRO_API_KEY", "")
    if not cmc_key and not dry_run:
        raise RuntimeError("CMC_PRO_API_KEY missing from .env (required for live mode)")

    return AppConfig(
        cmc_api_key=cmc_key,
        chain="bsc",
        dry_run=dry_run,
        risk=risk,
        tokens=tokens,
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
    )
