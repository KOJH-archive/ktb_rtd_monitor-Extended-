"""Intraday cash-yield curve context using completed KST minute closes."""
from __future__ import annotations

import statistics
from collections import deque
from datetime import datetime, timedelta

from analyze_snapshots import timestamp_kst
from market_extensions import optional_number


class CashCurveTracker:
    """Observe the 10Y-minus-3Y cash yield spread in bp; no trading forecast."""

    def __init__(self) -> None:
        self.completed: deque[tuple[datetime, float]] = deque()
        self.current: tuple[datetime, float] | None = None

    def reset(self) -> None:
        self.completed.clear()
        self.current = None

    def update(self, row: dict[str, object]) -> None:
        three = optional_number(row.get("cash_3y_yield_pct"))
        ten = optional_number(row.get("cash_10y_yield_pct"))
        if three is None or ten is None or three <= 0 or ten <= 0:
            self.reset()
            return
        bucket = timestamp_kst(row["timestamp_utc"]).replace(second=0, microsecond=0)
        value = (ten - three) * 100.0
        if self.current is None:
            self.current = (bucket, value)
            return
        previous_bucket, _ = self.current
        if bucket < previous_bucket or bucket.date() != previous_bucket.date():
            self.reset()
        elif bucket > previous_bucket:
            if bucket - previous_bucket != timedelta(minutes=1):
                self.reset()
            else:
                self.completed.append(self.current)
                cutoff = bucket - timedelta(minutes=60)
                while self.completed and self.completed[0][0] < cutoff:
                    self.completed.popleft()
        self.current = (bucket, value)

    def summary(self) -> dict[str, object] | None:
        if self.current is None:
            return None
        bucket, latest = self.current
        result: dict[str, object] = {"spread_bp": latest}
        closes = dict(self.completed)
        for minutes in (5, 15):
            past = closes.get(bucket - timedelta(minutes=minutes))
            result[f"change_{minutes}m_bp"] = None if past is None else latest - past
        for minutes in (5, 60):
            values = [closes.get(bucket - timedelta(minutes=offset))
                      for offset in range(1, minutes + 1)]
            if any(value is None for value in values):
                result[f"location_{minutes}m"] = None
                continue
            mean = statistics.fmean(values)
            deviation = statistics.stdev(values)
            result[f"location_{minutes}m"] = {
                "reference_mean_bp": mean, "distance_bp": latest - mean,
                "zscore": (latest - mean) / deviation if deviation > 1e-12 else None,
                "reference_stdev_bp": deviation,
            }
        return result
