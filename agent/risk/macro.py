"""Scheduled macro-event blackout (STRATEGY §4.5, v1).

A static-but-editable calendar of scheduled releases (PCE, Fed chair, GDP...)
mapped to entry restrictions: HIGH events block new entries around the
release, MEDIUM events halve entry size. Exits and the daily compliance
trade are never restricted — the blackout protects the drawdown gate, it
must not break qualification rules.

The YAML is re-read when its mtime changes, so the calendar can be refreshed
(by hand or by a daily MCP "Macro Events" pull) without restarting the agent.
A missing or broken calendar fails OPEN (no blackout): this layer is extra
protection on top of the regime gate and drawdown ladder, not a dependency.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from agent.config import CONFIG_DIR

log = logging.getLogger(__name__)

DEFAULT_WINDOWS_H = {"high": (2.0, 3.0), "medium": (1.0, 1.0)}
LEVEL_SCALE = {"high": 0.0, "medium": 0.5}


@dataclass(frozen=True)
class MacroStatus:
    entry_scale: float  # 1.0 normal, 0.5 medium window, 0.0 high blackout
    level: str          # "none" | "medium" | "high"
    event: str | None
    detail: str

    @property
    def active(self) -> bool:
        return self.entry_scale < 1.0


CLEAR = MacroStatus(1.0, "none", None, "no scheduled macro window active")


@dataclass(frozen=True)
class _Window:
    name: str
    level: str
    start: datetime
    end: datetime


class MacroCalendar:
    def __init__(self, path: Path | None = None):
        self.path = path or (CONFIG_DIR / "macro_events.yaml")
        self._mtime: float | None = None
        self._windows: list[_Window] = []

    def _load(self) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            if self._mtime is not None or self._windows:
                log.warning("macro calendar %s disappeared — blackout disabled", self.path)
            self._mtime, self._windows = None, []
            return
        if mtime == self._mtime:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError) as e:
            log.warning("macro calendar unreadable (%s) — keeping previous table", e)
            return

        defaults = raw.get("defaults") or {}
        windows: list[_Window] = []
        for ev in raw.get("events") or []:
            try:
                level = str(ev["level"]).lower()
                if level not in LEVEL_SCALE:
                    raise ValueError(f"unknown level {level!r}")
                t = datetime.strptime(str(ev["time_utc"]), "%Y-%m-%d %H:%M").replace(
                    tzinfo=timezone.utc
                )
                d = defaults.get(level) or {}
                pre_h = float(d.get("pre_h", DEFAULT_WINDOWS_H[level][0]))
                post_h = float(d.get("post_h", DEFAULT_WINDOWS_H[level][1]))
                windows.append(
                    _Window(
                        name=str(ev.get("name", "unnamed event")),
                        level=level,
                        start=t - timedelta(hours=pre_h),
                        end=t + timedelta(hours=post_h),
                    )
                )
            except (KeyError, ValueError, TypeError) as e:
                log.warning("skipping malformed macro event %r: %s", ev, e)
        self._mtime, self._windows = mtime, windows
        log.info("macro calendar loaded: %d blackout windows", len(windows))

    def status(self, now: datetime) -> MacroStatus:
        """Most restrictive active window wins (high beats medium)."""
        self._load()
        active = [w for w in self._windows if w.start <= now <= w.end]
        if not active:
            return CLEAR
        worst = min(active, key=lambda w: LEVEL_SCALE[w.level])
        return MacroStatus(
            entry_scale=LEVEL_SCALE[worst.level],
            level=worst.level,
            event=worst.name,
            detail=(
                f"{worst.name}: {worst.level} window "
                f"{worst.start:%m-%d %H:%M}..{worst.end:%H:%M} UTC"
            ),
        )
