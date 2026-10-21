"""Shared research signal path for UI and replay; never places orders."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from analyze_snapshots import ConfirmedConsensus, MinuteRVAnalyzer, RollingRVAnalyzer, timestamp_kst
from app_settings import AppSettings, effective_beta
from decision_support import execution_gate, risk_metrics

NUMERIC_KEYS = tuple(f"{name}_{key}" for name in ("3y", "10y")
                     for key in ("mid", "best_bid", "best_ask", "spread"))


def validated_row(raw: dict[str, object]) -> dict[str, object]:
    row = dict(raw)
    stamp = datetime.fromisoformat(str(row["timestamp_utc"]))
    if stamp.tzinfo is None:
        raise ValueError("Snapshot timestamp must include timezone")
    for key in NUMERIC_KEYS:
        if isinstance(row[key], bool):
            raise ValueError(f"Boolean quote: {key}")
        value = float(row[key])
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"Invalid quote: {key}")
        row[key] = value
    for name in ("3y", "10y"):
        bid, ask = row[f"{name}_best_bid"], row[f"{name}_best_ask"]
        if bid >= ask:
            raise ValueError(f"Locked/crossed book: {name}")
        if not math.isclose(row[f"{name}_mid"], (bid + ask) / 2, abs_tol=1e-8):
            raise ValueError(f"Inconsistent mid: {name}")
        if not math.isclose(row[f"{name}_spread"], ask - bid, abs_tol=1e-8):
            raise ValueError(f"Inconsistent spread: {name}")
    return row


@dataclass(frozen=True)
class SignalResult:
    fast: dict[str, object] | None
    core: dict[str, object] | None
    state: int
    text: str
    ready: bool
    entry_allowed: bool
    reset_reason: str | None


class SignalEngine:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.beta = effective_beta(settings)
        self.fast = RollingRVAnalyzer(settings.rv.fast_event_window, self.beta)
        self.core = MinuteRVAnalyzer(settings.rv.core_minutes, self.beta)
        self.consensus = ConfirmedConsensus(settings.rv.entry_z, settings.rv.exit_z,
                                            settings.rv.confirmation_seconds)
        self.previous: datetime | None = None

    def reset(self) -> None:
        self.fast.reset()
        self.core.reset()
        self.consensus.reset()
        self.previous = None

    def update(self, raw: dict[str, object], preserve_history_on_gap: bool = False) -> SignalResult:
        try:
            row = validated_row(raw)
            stamp = timestamp_kst(row["timestamp_utc"])
            if self.previous is not None and stamp <= self.previous:
                raise ValueError("Duplicate or out-of-order snapshot timestamp")
        except (ValueError, TypeError, KeyError):
            self.reset()
            raise
        reason = None
        if self.previous is not None:
            if stamp.date() != self.previous.date():
                reason = "calendar_day_change"
            elif (stamp - self.previous).total_seconds() > self.settings.data.max_gap_seconds:
                reason = "data_gap"
        if reason:
            if preserve_history_on_gap and reason == "data_gap":
                # A dashboard restart must not erase today's observations.  Break
                # return/confirmation continuity so no move during the outage is
                # treated as an observed one-period change or a confirmed signal.
                self.fast.pause()
                self.core.pause(row["timestamp_utc"])
                self.consensus.reset()
                reason = "data_gap_resumed"
            else:
                self.reset()
        self.previous = stamp
        fast, core = self.fast.update(row), self.core.update(row)
        ready = bool(fast and core and fast["samples"] >= self.settings.rv.fast_event_window
                     and core["ready"] and fast["level_stdev"] > 1e-12 and core["level_stdev"] > 1e-12)
        state, text = 0, "연구 신호 준비 중 / 분산 부족"
        allowed = False
        if ready:
            state, text = self.consensus.update(row["timestamp_utc"], fast["zscore"], core["zscore"])
            risk = risk_metrics(self.settings.risk)
            allowed = bool(execution_gate(row, core, self.beta, self.settings.execution)["allowed"]
                           and (risk is None or not risk["violations"]))
            if state and not allowed:
                text += " | 비용/설정 위험한도 미충족"
        else:
            self.consensus.reset()
        return SignalResult(fast, core, state, text, ready, allowed, reason)
