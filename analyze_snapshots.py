"""Analyze KTB RTD snapshots already collected by ktb_rtd_monitor.py."""
from __future__ import annotations
import argparse, csv, math, statistics
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

def read_rows(path: Path) -> list[dict[str, object]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        rows = []
        for raw in csv.DictReader(handle):
            try: rows.append({k: (v if k == "timestamp_utc" else float(v)) for k, v in raw.items()})
            except (TypeError, ValueError): pass
        return rows

class RollingRVAnalyzer:
    """Incremental, bounded-memory RV analysis for the live dashboard."""

    def __init__(self, window: int, beta: float, min_samples: int | None = None) -> None:
        self.window, self.beta = window, beta
        self.min_samples = window if min_samples is None else min_samples
        self.changes: deque[float] = deque(maxlen=window)
        self.levels: deque[float] = deque(maxlen=window)
        self.previous: tuple[float, float] | None = None
        self.timestamp: object = ""
        self.event_changed = False
        self.event_times: deque[datetime] = deque(maxlen=window)
        self._cached: dict[str, object] | None = None

    def reset(self) -> None:
        self.changes.clear()
        self.levels.clear()
        self.previous = None
        self.timestamp = ""
        self.event_changed = False
        self.event_times.clear()
        self._cached = None

    def pause(self) -> None:
        """Keep same-day observations but never create a change across a gap."""
        self.previous = None
        self.event_changed = False

    def update(self, row: dict[str, object]) -> dict[str, object] | None:
        self.event_changed = False
        mid3 = row.get("3y_mid", row.get("3y_trade_price"))
        mid10 = row.get("10y_mid", row.get("10y_trade_price"))
        if not isinstance(mid3, (float, int)) or not isinstance(mid10, (float, int)):
            return self.result()
        mid3, mid10 = float(mid3), float(mid10)
        if not all(math.isfinite(value) and value > 0 for value in (mid3, mid10)):
            raise ValueError("Invalid RV prices")
        self.timestamp = row.get("timestamp_utc", self.timestamp)
        level = mid10 - self.beta * mid3
        if self.previous is None:
            self.levels.append(level)
            self.event_changed = True
        elif (mid3, mid10) != self.previous:
            self.changes.append(level - (self.previous[1] - self.beta * self.previous[0]))
            self.levels.append(level)
            self.event_changed = True
        self.previous = (mid3, mid10)
        if self.event_changed:
            self._cached = None
            try:
                self.event_times.append(timestamp_kst(self.timestamp))
            except ValueError:
                pass  # Legacy pure-math callers may use labels instead of timestamps.
        return self.result()

    def result(self) -> dict[str, object] | None:
        if not self.levels:
            return None
        if self._cached is not None:
            return {**self._cached, "timestamp_utc": self.timestamp}
        level_sample = list(self.levels)
        change_sample = list(self.changes)
        latest_level = level_sample[-1]
        level_mean = statistics.fmean(level_sample)
        level_stdev = statistics.stdev(level_sample) if len(level_sample) >= 2 else 0.0
        latest_change = change_sample[-1] if change_sample else 0.0
        change_mean = statistics.fmean(change_sample) if change_sample else 0.0
        change_stdev = statistics.stdev(change_sample) if len(change_sample) >= 2 else 0.0
        level_zscore = ((latest_level - level_mean) / level_stdev
                        if len(level_sample) >= self.min_samples and level_stdev > 1e-12 else 0.0)
        change_zscore = ((latest_change - change_mean) / change_stdev
                         if len(change_sample) >= 2 and change_stdev > 1e-12 else 0.0)
        self._cached = {"timestamp_utc": self.timestamp, "samples": len(level_sample),
                "latest_rv_level": latest_level, "level_mean": level_mean, "level_stdev": level_stdev,
                "latest_rv_change": latest_change, "window_mean": change_mean, "window_stdev": change_stdev,
                "zscore": level_zscore, "level_zscore": level_zscore, "change_zscore": change_zscore,
                "beta": self.beta,
                "span_seconds": ((self.event_times[-1] - self.event_times[0]).total_seconds()
                                 if len(self.event_times) >= 2 else 0.0)}
        return dict(self._cached)


KST = timezone(timedelta(hours=9))


def timestamp_kst(value: object) -> datetime:
    timestamp = datetime.fromisoformat(str(value))
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(KST)


class MinuteRVAnalyzer:
    """Compare current RV with the last N completed minute closes; never fill gaps."""

    def __init__(self, minutes: int, beta: float, min_samples: int | None = None) -> None:
        self.minutes = minutes
        self.beta = beta
        self.min_samples = minutes if min_samples is None else min_samples
        self.points: deque[tuple[datetime, float, int]] = deque()
        self.session_date = None
        self.timestamp: object = ""
        self.event_changed = False
        self.current: tuple[datetime, float, int] | None = None
        self.segment = 0
        self._completed_stats: dict[str, object] | None = None

    def reset(self) -> None:
        self.points.clear()
        self.session_date = None
        self.timestamp = ""
        self.event_changed = False
        self.current = None
        self.segment = 0
        self._completed_stats = None

    def pause(self, next_timestamp: object | None = None) -> None:
        """Keep completed history while marking the next minute as a new segment."""
        self.event_changed = False
        if next_timestamp is not None and self.current is not None:
            next_bucket = timestamp_kst(next_timestamp).replace(second=0, microsecond=0)
            if next_bucket <= self.current[0]:
                return
            self.points.append(self.current)
        self.current = None
        self.segment += 1
        self._completed_stats = None

    def update(self, row: dict[str, object]) -> dict[str, object] | None:
        self.event_changed = False
        mid3 = row.get("3y_mid", row.get("3y_trade_price"))
        mid10 = row.get("10y_mid", row.get("10y_trade_price"))
        if not isinstance(mid3, (float, int)) or not isinstance(mid10, (float, int)):
            return self.result()
        stamp = timestamp_kst(row.get("timestamp_utc"))
        bucket = stamp.replace(second=0, microsecond=0)
        if self.session_date is not None and bucket.date() != self.session_date:
            self.reset()
        self.session_date = bucket.date()
        self.timestamp = row.get("timestamp_utc", self.timestamp)
        level = float(mid10) - self.beta * float(mid3)
        if not all(math.isfinite(float(value)) and float(value) > 0 for value in (mid3, mid10)):
            raise ValueError("Invalid RV prices")
        if self.current and bucket < self.current[0]:
            raise ValueError("Out-of-order minute")
        if self.current and bucket > self.current[0]:
            self.points.append(self.current)
            self._completed_stats = None
        candidate = (bucket, level, self.segment)
        self.event_changed = self.current != candidate
        self.current = candidate
        cutoff = bucket - timedelta(minutes=self.minutes)
        while self.points and self.points[0][0] < cutoff:
            self.points.popleft()
            self._completed_stats = None
        return self.result()

    def result(self) -> dict[str, object] | None:
        if self.current is None:
            return None
        latest = self.current[1]
        if self._completed_stats is None:
            points = list(self.points)
            levels = [level for _, level, _ in points]
            # Fixed-time changes, including unchanged minute closes. Never
            # confuse level dispersion or an event-time change with P&L risk.
            changes = [right[1] - left[1] for left, right in zip(points, points[1:])
                       if right[2] == left[2]
                       and right[0] - left[0] == timedelta(minutes=1)]
            self._completed_stats = {
                "samples": len(levels),
                "level_mean": statistics.fmean(levels) if levels else None,
                "level_stdev": statistics.stdev(levels) if len(levels) >= 2 else 0.0,
                "minute_change_samples": len(changes),
                "minute_change_stdev": statistics.stdev(changes) if len(changes) >= 2 else None,
                "minute_change_ready": len(levels) >= self.min_samples and len(changes) >= 2
                                       and len(changes) == len(levels) - 1,
            }
        stats = self._completed_stats
        mean = latest if stats["level_mean"] is None else stats["level_mean"]
        stdev = stats["level_stdev"]
        ready = stats["samples"] >= self.min_samples
        zscore = (latest - mean) / stdev if ready and stdev > 1e-12 else 0.0
        return {**stats, "timestamp_utc": self.timestamp, "required_samples": self.min_samples,
                "latest_rv_level": latest, "level_mean": mean, "level_stdev": stdev,
                "zscore": zscore, "level_zscore": zscore, "beta": self.beta,
                "window_minutes": self.minutes, "ready": ready}


def consensus_decision(fast_z: float, core_z: float, current_state: int,
                       entry_z: float, exit_z: float) -> tuple[int, str]:
    """Combine fast/core signals with a lower exit threshold to reduce chattering."""
    if current_state == 1 and fast_z > exit_z and core_z > exit_z:
        return 1, "10Y 상대강세 이탈 | 두 시간대 확인"
    if current_state == -1 and fast_z < -exit_z and core_z < -exit_z:
        return -1, "10Y 상대약세 이탈 | 두 시간대 확인"
    if fast_z >= entry_z and core_z >= entry_z:
        return 1, "10Y 상대강세 이탈 | 두 시간대 확인"
    if fast_z <= -entry_z and core_z <= -entry_z:
        return -1, "10Y 상대약세 이탈 | 두 시간대 확인"
    if abs(fast_z) >= entry_z and fast_z * core_z <= 0:
        return 0, "빠른·주 신호 불일치 | 진입 보류"
    if abs(fast_z) >= entry_z:
        return 0, "단기 이탈 | 주 신호 확인 대기"
    if abs(core_z) >= entry_z:
        return 0, "주 신호 이탈 | 단기 타이밍 확인 대기"
    return 0, "중립/관찰"


class ConfirmedConsensus:
    """Require a candidate state to persist in market time before changing state."""

    def __init__(self, entry_z: float, exit_z: float, confirmation_seconds: float) -> None:
        self.entry_z = entry_z
        self.exit_z = exit_z
        self.confirmation_seconds = confirmation_seconds
        self.state = 0
        self.candidate: int | None = None
        self.candidate_since: datetime | None = None
        self.text = "중립/관찰"

    def reset(self) -> None:
        self.state = 0
        self.candidate = None
        self.candidate_since = None
        self.text = "중립/관찰"

    def update(self, timestamp: object, fast_z: float, core_z: float) -> tuple[int, str]:
        target, target_text = consensus_decision(fast_z, core_z, self.state,
                                                 self.entry_z, self.exit_z)
        if target == self.state:
            self.candidate = None
            self.candidate_since = None
            self.text = target_text
            return self.state, self.text
        now = datetime.fromisoformat(str(timestamp))
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        if self.candidate != target or self.candidate_since is None:
            self.candidate = target
            self.candidate_since = now
        elapsed = max(0.0, (now - self.candidate_since).total_seconds())
        if elapsed >= self.confirmation_seconds:
            self.state = target
            self.candidate = None
            self.candidate_since = None
            self.text = target_text
        else:
            action = "진입" if target else "청산"
            self.text = f"{action} 확인 중 {elapsed:.1f}/{self.confirmation_seconds:.1f}초"
        return self.state, self.text


def analyze(rows: list[dict[str, object]], window: int, beta: float,
            min_samples: int | None = None) -> dict[str, object] | None:
    analyzer = RollingRVAnalyzer(window, beta, min_samples)
    result = None
    for row in rows:
        result = analyzer.update(row)
    return result

def main() -> int:
    p = argparse.ArgumentParser(description="Summarize KTB 3Y/10Y relative-value changes.")
    p.add_argument("csv_path", nargs="?", type=Path, default=Path("data/snapshots.csv")); p.add_argument("--window", type=int, default=120); p.add_argument("--beta", type=float, default=2.88)
    p.add_argument("--min-samples", type=int, help="Defaults to the full window")
    p.add_argument("--settings", type=Path, help="Use configured event window and effective beta")
    a = p.parse_args()
    if a.settings:
        from app_settings import effective_beta, load_settings
        settings = load_settings(a.settings)
        a.window, a.beta = settings.rv.fast_event_window, effective_beta(settings)
    if a.window < 2: p.error("--window must be at least 2")
    if a.min_samples is not None and not 2 <= a.min_samples <= a.window:
        p.error("--min-samples must be between 2 and --window")
    rows = read_rows(a.csv_path)
    if rows:
        latest_date = timestamp_kst(rows[-1]["timestamp_utc"]).date()
        rows = [row for row in rows if timestamp_kst(row["timestamp_utc"]).date() == latest_date]
    result = analyze(rows, a.window, a.beta, a.min_samples)
    if result is None: print("분석할 유효한 스냅샷이 없습니다."); return 1
    print(f"KTB RV summary | {result['timestamp_utc']}")
    print(f"samples {int(result['samples'])} | beta {result['beta']:.4f} | window {a.window}")
    print(f"RV level {result['latest_rv_level']:+.6f} | level mean {result['level_mean']:+.6f} | level stdev {result['level_stdev']:.6f} | level z-score {result['level_zscore']:+.3f}")
    print(f"latest RV change {result['latest_rv_change']:+.6f} | change z-score {result['change_zscore']:+.3f}")
    return 0

if __name__ == "__main__": raise SystemExit(main())
