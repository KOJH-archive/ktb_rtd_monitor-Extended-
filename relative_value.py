"""Cash-curve anchored 3Y/10Y relative-value diagnostics.

The module deliberately produces observations, not orders or profit forecasts.
The optional 5Y cash yield is an anchor; it is not treated as a tradable futures leg.
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta

from analyze_snapshots import timestamp_kst
from app_settings import RiskSettings, dv01_package
from market_extensions import market_metrics, positive_optional_number


POINT_VALUE_KRW = 1_000_000.0  # 0.01 point = KRW 10,000 per KRX contract.


def estimate_half_life(values: list[float]) -> float | None:
    """Estimate AR(1) half-life in observations; reject non-mean-reverting fits."""
    if len(values) < 10 or not all(math.isfinite(value) for value in values):
        return None
    left, right = values[:-1], values[1:]
    left_mean, right_mean = statistics.fmean(left), statistics.fmean(right)
    denominator = sum((value - left_mean) ** 2 for value in left)
    if denominator <= 1e-12:
        return None
    phi = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right)) / denominator
    if not 0 < phi < 1:
        return None
    half_life = -math.log(2.0) / math.log(phi)
    return half_life if math.isfinite(half_life) else None


@dataclass(frozen=True)
class _MinutePoint:
    bucket: datetime
    cross_basis_bp: float
    segment: int


class RelativeValueTracker:
    """Completed-minute cross-basis reference window with explicit gap segments."""

    def __init__(self, window_minutes: int = 60, min_samples: int = 20) -> None:
        self.window_minutes = window_minutes
        self.min_samples = min_samples
        self.points: deque[_MinutePoint] = deque()
        self.current: _MinutePoint | None = None
        self.segment = 0

    def reset(self) -> None:
        self.points.clear()
        self.current = None
        self.segment = 0

    def pause(self) -> None:
        self.current = None
        self.segment += 1

    def update(self, row: dict[str, object]) -> dict[str, object] | None:
        cross = market_metrics(row).get("cross_basis_bp")
        if cross is None:
            self.pause()
            return self.summary()
        bucket = timestamp_kst(row["timestamp_utc"]).replace(second=0, microsecond=0)
        if self.current is not None:
            if bucket.date() != self.current.bucket.date() or bucket < self.current.bucket:
                self.reset()
            elif bucket > self.current.bucket:
                self.points.append(self.current)
                if bucket - self.current.bucket != timedelta(minutes=1):
                    self.segment += 1
        self.current = _MinutePoint(bucket, float(cross), self.segment)
        cutoff = bucket - timedelta(minutes=self.window_minutes)
        while self.points and self.points[0].bucket < cutoff:
            self.points.popleft()
        return self.summary()

    def summary(self) -> dict[str, object] | None:
        if self.current is None:
            return None
        values = [point.cross_basis_bp for point in self.points]
        count = len(values)
        mean = statistics.fmean(values) if values else None
        stdev = statistics.stdev(values) if count >= 2 else None
        zscore = ((self.current.cross_basis_bp - mean) / stdev
                  if count >= self.min_samples and stdev is not None and stdev > 1e-12 else None)
        consecutive: list[float] = []
        if self.points:
            # A gap increments the current segment before the first new point is
            # completed.  Key off the live point so the previous segment is not
            # briefly reused for the half-life estimate after reconnection.
            last_segment = self.current.segment
            consecutive = [point.cross_basis_bp for point in self.points
                           if point.segment == last_segment]
        return {"cross_basis_bp": self.current.cross_basis_bp,
                "reference_mean_bp": mean, "reference_stdev_bp": stdev,
                "zscore": zscore, "samples": count,
                "required_samples": self.min_samples,
                "half_life_minutes": estimate_half_life(consecutive),
                "continuous_samples": len(consecutive)}


def relative_execution_cost(row: dict[str, object], risk: RiskSettings) -> dict[str, float | int] | None:
    """Approximate top-of-book round-trip cost for the integer DV01 package.

    The bp conversion assumes a symmetric 3Y/10Y curve move around the average
    level.  It is a screening threshold, not a guaranteed executable cost.
    """
    package = dv01_package(risk)
    spread3 = positive_optional_number(row.get("3y_spread"))
    spread10 = positive_optional_number(row.get("10y_spread"))
    if package is None or spread3 is None or spread10 is None:
        return None
    contracts3 = int(package["rounded_3y"])
    contracts10 = int(package["contracts_10y"])
    dv3 = float(risk.dv01_3y_krw_per_bp)
    dv10 = float(risk.dv01_10y_krw_per_bp)
    roundtrip_krw = (contracts3 * spread3 + contracts10 * spread10) * POINT_VALUE_KRW
    symmetric_spread_dv01 = (contracts3 * dv3 + contracts10 * dv10) / 2.0
    return {"contracts_3y": contracts3, "contracts_10y": contracts10,
            "roundtrip_cost_krw": roundtrip_krw,
            "break_even_cross_basis_bp": roundtrip_krw / symmetric_spread_dv01,
            "residual_dv01_krw_per_bp": float(package["residual_dv01"])}
