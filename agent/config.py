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
    vol_target_pct: float
    vol_floor: float
    stop_loss_pct: float
    max_trades_per_day: int
    max_slippage_pct: float
    min_expected_edge_pct: float
    daily_trade_deadline_utc: str
    min_portfolio_usd: float
    max_signal_age_min: int
    cycle_interval_s: int
    regime_cache_min: int
    # Asymmetric regime gate (#4): under F&G extreme fear, entries run at half
    # scale and must clear this conviction floor (0 disables the floor).
    fear_conviction_floor: float = 0.50
    # Liquidity sentinel (#7): exit when a held token's reference pool drains
    # this far below the entry-time baseline (0 disables).
    liquidity_exit_drop_pct: float = 40.0
    liquidity_min_ref_usd: float = 100_000.0
    # Anti-whipsaw (#9): hours before a closed token may be re-entered, and
    # the margin over each token's measured friction an entry must clear.
    reentry_cooldown_h: float = 24.0
    edge_floor_margin_pct: float = 0.5
    # Volume confirmation (#11): an entry needs volume_24h >= ratio x its own
    # trailing-lookback-bar mean (rising attention). Backtested to cut the
    # worst gross-negative entries and ~halve the fee-driven loss; tighter
    # ratios overshoot. ratio<=0 disables.
    vol_confirm_ratio: float = 1.0
    vol_confirm_lookback: int = 24
    # Trade-budget reserve (#12): before this UTC hour ("HH:MM"), keep this
    # many of the daily trades unspent so overnight whipsaws can't starve the
    # afternoon. "" or 0 disables. Exits are exempt (de-risking always runs).
    reserve_trades_until_utc: str = ""
    reserved_trades: int = 0


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
    strategy: str = "trend"  # active strategy plugin (agent/strategies/registry.py)


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
        vol_target_pct=float(r["position"].get("vol_target_pct", 0.0)),
        vol_floor=float(r["position"].get("vol_floor", 0.5)),
        stop_loss_pct=float((r.get("exits") or {}).get("stop_loss_pct", 0.0)),
        max_trades_per_day=int(r["limits"]["max_trades_per_day"]),
        max_slippage_pct=float(r["limits"]["max_slippage_pct"]),
        min_expected_edge_pct=float(r["limits"]["min_expected_edge_pct"]),
        daily_trade_deadline_utc=str(r["compliance"]["daily_trade_deadline_utc"]),
        min_portfolio_usd=float(r["compliance"]["min_portfolio_usd"]),
        max_signal_age_min=int(r["data"]["max_signal_age_min"]),
        cycle_interval_s=int(r["data"]["cycle_interval_s"]),
        regime_cache_min=int(r["data"]["regime_cache_min"]),
        fear_conviction_floor=float(
            (r.get("regime") or {}).get("fear_conviction_floor", 0.50)),
        liquidity_exit_drop_pct=float(
            (r.get("exits") or {}).get("liquidity_exit_drop_pct", 0.0)),
        liquidity_min_ref_usd=float(
            (r.get("exits") or {}).get("liquidity_min_ref_usd", 100_000.0)),
        reentry_cooldown_h=float(r["limits"].get("reentry_cooldown_h", 0.0)),
        edge_floor_margin_pct=float(r["limits"].get("edge_floor_margin_pct", 0.0)),
        vol_confirm_ratio=float((r.get("entry") or {}).get("vol_confirm_ratio", 1.0)),
        vol_confirm_lookback=int((r.get("entry") or {}).get("vol_confirm_lookback", 24)),
        reserve_trades_until_utc=str(
            r["limits"].get("reserve_trades_until_utc") or ""),
        reserved_trades=int(r["limits"].get("reserved_trades", 0)),
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

    # CMC is no longer required: signals/regime come from free public feeds
    # (agent/market/feed.py) and valuation is on-chain. Key kept as optional
    # legacy config (scripts/backtests may still use a CMC client directly).
    cmc_key = os.environ.get("CMC_PRO_API_KEY", "")

    return AppConfig(
        cmc_api_key=cmc_key,
        chain="bsc",
        dry_run=dry_run,
        risk=risk,
        tokens=tokens,
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN") or None,
        telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID") or None,
        strategy=str((r.get("strategy") or {}).get("active", "trend")),
    )
