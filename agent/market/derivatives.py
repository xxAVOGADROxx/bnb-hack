"""Derivatives regime feed — free OKX public endpoints, no API key.

The spot book can only be long or flat, but perp positioning drives the
moves we trade: a short squeeze (price up while open interest collapses and
shorts get liquidated) is momentum fuel; a long-liquidation cascade is a
falling knife. This module reads that picture per token from OKX, the one
major derivatives venue whose public API is reachable from this host
(Binance fapi and Bybit are geo-blocked, verified 2026-07-05).

Endpoints (all public, TTL-cached here; OKX allows ~20 req/2s per path):
  /api/v5/public/funding-rate                current + next funding
  /api/v5/rubik/stat/contracts/open-interest-history   1H OI candles, ~30d
  /api/v5/rubik/stat/contracts/long-short-account-ratio
  /api/v5/public/liquidation-orders          recent liq orders w/ side
  /api/v5/public/instruments                 ctVal map (contract -> coin)

Coverage: 13/15 of the current watchlist has a {SYM}-USDT-SWAP on OKX
(missing: CAKE, TWT — BNB-ecosystem tokens). snapshot() returns None for
those and on ANY error: this is an information layer, strictly fail-open,
same contract as agent/market/dex.py. SHADOW-ONLY for now — loop.py logs
"deriv_view" records for calibration; no gate until a backtest earns one
(scripts/squeeze_bt.py is that backtest).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import requests

log = logging.getLogger(__name__)

OKX = "https://www.okx.com"


@dataclass(frozen=True)
class DerivView:
    """Perp positioning snapshot for one token (OKX USDT-margined swap)."""
    token: str
    funding_rate: float | None      # current period (8h) rate, e.g. -0.0001
    oi_usd: float | None            # open interest now, USD
    oi_chg_24h_pct: float | None    # OI now vs 24 hourly bars ago
    ls_ratio: float | None          # long/short ACCOUNT ratio (>1 = crowd long)
    long_liq_usd: float | None      # liquidated longs, observed recent window
    short_liq_usd: float | None     # liquidated shorts, observed recent window
    liq_window_h: float | None      # how many hours that window actually spans

    def squeeze_fingerprint(self, px_chg_24h_pct: float) -> bool:
        """Shorts being forced out: price up while OI bleeds. The px threshold
        matches the momentum sleeve's typical 24h entry move; both cuts are
        re-derived by scripts/squeeze_bt.py before this ever gates."""
        return (self.oi_chg_24h_pct is not None
                and px_chg_24h_pct >= 2.0 and self.oi_chg_24h_pct <= -2.0)


class DerivFeed:
    def __init__(self, timeout: int = 10):
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json"})
        self._cache: dict[str, tuple[float, Any]] = {}
        self._swaps: set[str] | None = None    # instIds with a USDT swap
        self._ct_val: dict[str, float] = {}    # instId -> contract size (coin)

    # -- plumbing ------------------------------------------------------------
    def _get(self, path: str, params: dict | None = None, ttl_s: int = 300) -> Any:
        """GET an OKX v5 path; returns the `data` payload. Raises on failure
        (public methods catch and fail open)."""
        key = path + repr(sorted((params or {}).items()))
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl_s:
            return hit[1]
        r = self.session.get(f"{OKX}{path}", params=params, timeout=self.timeout)
        r.raise_for_status()
        body = r.json()
        if body.get("code") not in ("0", 0):
            raise RuntimeError(f"okx {path} -> {body.get('code')}: {body.get('msg')}")
        data = body.get("data")
        self._cache[key] = (time.monotonic(), data)
        return data

    def inst_id(self, symbol: str) -> str | None:
        """{SYM}-USDT-SWAP when OKX lists it, else None. Instrument list is
        cached for the process lifetime (it changes on listing events only)."""
        if self._swaps is None:
            try:
                rows = self._get("/api/v5/public/instruments",
                                 {"instType": "SWAP"}, ttl_s=86_400)
                self._swaps = {r["instId"] for r in rows}
                self._ct_val.update(
                    {r["instId"]: float(r.get("ctVal") or 0) for r in rows})
            except Exception as e:  # noqa: BLE001
                log.debug("okx instruments unavailable: %s", e)
                return None
        iid = f"{symbol}-USDT-SWAP"
        return iid if iid in self._swaps else None

    # -- components (each fail-open to None) ----------------------------------
    def _funding(self, iid: str) -> float | None:
        try:
            rows = self._get("/api/v5/public/funding-rate",
                             {"instId": iid}, ttl_s=600)
            return float(rows[0]["fundingRate"])
        except Exception:  # noqa: BLE001
            return None

    def _oi(self, iid: str) -> tuple[float | None, float | None]:
        """(oi_usd_now, oi_chg_24h_pct) from the 1H OI history candles.
        Row shape: [ts_ms, oi_contracts, oi_coin, oi_usd], newest first."""
        try:
            rows = self._get("/api/v5/rubik/stat/contracts/open-interest-history",
                             {"instId": iid, "period": "1H", "limit": "25"},
                             ttl_s=300)
            if not rows:
                return None, None
            now = float(rows[0][-1])
            if len(rows) < 25 or float(rows[-1][-1]) <= 0:
                return now, None
            return now, (now / float(rows[24][-1]) - 1) * 100
        except Exception:  # noqa: BLE001
            return None, None

    def _ls_ratio(self, symbol: str) -> float | None:
        try:
            rows = self._get("/api/v5/rubik/stat/contracts/long-short-account-ratio",
                             {"ccy": symbol, "period": "1H"}, ttl_s=600)
            return float(rows[0][1])
        except Exception:  # noqa: BLE001
            return None

    def _liquidations(self, iid: str) -> tuple[float | None, float | None, float | None]:
        """(long_liq_usd, short_liq_usd, window_h) from the recent liq-order
        feed. OKX returns a bounded recent window (~100 orders/page), so the
        WINDOW VARIES — always read these next to liq_window_h. Notional =
        contracts x ctVal x bankruptcy price."""
        try:
            uly = iid.removesuffix("-SWAP")
            rows = self._get("/api/v5/public/liquidation-orders",
                             {"instType": "SWAP", "uly": uly, "state": "filled"},
                             ttl_s=300)
            details = (rows or [{}])[0].get("details") or []
            if not details:
                return 0.0, 0.0, None
            ct = self._ct_val.get(iid, 0.0)
            if ct <= 0:
                return None, None, None
            longs = shorts = 0.0
            oldest = newest = float(details[0]["ts"])
            for d in details:
                usd = float(d["sz"]) * ct * float(d["bkPx"])
                if d.get("posSide") == "long":
                    longs += usd
                else:
                    shorts += usd
                ts = float(d["ts"])
                oldest, newest = min(oldest, ts), max(newest, ts)
            return longs, shorts, round((newest - oldest) / 3_600_000, 1)
        except Exception:  # noqa: BLE001
            return None, None, None

    # -- the one call the loop makes ------------------------------------------
    def snapshot(self, symbol: str) -> DerivView | None:
        """Full positioning view, or None when OKX has no swap for the token
        or nothing could be read. Never raises."""
        try:
            iid = self.inst_id(symbol)
            if iid is None:
                return None
            funding = self._funding(iid)
            oi_usd, oi_chg = self._oi(iid)
            longs, shorts, window_h = self._liquidations(iid)
            view = DerivView(
                token=symbol, funding_rate=funding, oi_usd=oi_usd,
                oi_chg_24h_pct=round(oi_chg, 2) if oi_chg is not None else None,
                ls_ratio=self._ls_ratio(symbol),
                long_liq_usd=round(longs) if longs is not None else None,
                short_liq_usd=round(shorts) if shorts is not None else None,
                liq_window_h=window_h,
            )
            if all(v is None for v in (view.funding_rate, view.oi_usd,
                                       view.ls_ratio, view.long_liq_usd)):
                return None  # nothing readable this cycle
            return view
        except Exception as e:  # noqa: BLE001 — information layer: fail open
            log.debug("okx snapshot failed for %s: %s", symbol, e)
            return None
