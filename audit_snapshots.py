"""Reproducible offline data/window audit; does not tune parameters."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import statistics
from collections import deque
from pathlib import Path

from analyze_snapshots import timestamp_kst
from app_settings import load_settings
from signal_engine import validated_row


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def audit(path: Path, settings_path: Path) -> dict:
    settings = load_settings(settings_path)
    result = dict(source=str(path.resolve()), source_sha256=digest(path),
                  settings_sha256=digest(settings_path), python=platform.python_version(),
                  rows=0, invalid_rows=0, invalid_reasons={}, nonincreasing_timestamps=0, gaps=0,
                  mid_observations=0, days={}, fast_event_window=settings.rv.fast_event_window,
                  core_completed_minutes=settings.rv.core_minutes)
    previous = None
    previous_mid = None
    events = deque(maxlen=settings.rv.fast_event_window)
    spans = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        for raw in csv.DictReader(handle):
            result["rows"] += 1
            try:
                row = validated_row(raw)
            except (KeyError, TypeError, ValueError) as exc:
                result["invalid_rows"] += 1
                reason = str(exc)
                result["invalid_reasons"][reason] = result["invalid_reasons"].get(reason, 0) + 1
                events.clear()
                previous = previous_mid = None
                continue
            stamp = timestamp_kst(row["timestamp_utc"])
            day = result["days"].setdefault(str(stamp.date()), {"first_kst": stamp.isoformat(), "rows": 0})
            day["last_kst"] = stamp.isoformat()
            day["rows"] += 1
            if previous is not None:
                elapsed = (stamp - previous).total_seconds()
                if elapsed <= 0:
                    result["nonincreasing_timestamps"] += 1
                if elapsed > settings.data.max_gap_seconds:
                    result["gaps"] += 1
                if elapsed <= 0 or elapsed > settings.data.max_gap_seconds or stamp.date() != previous.date():
                    events.clear()
                    previous_mid = None
            mid = row["3y_mid"], row["10y_mid"]
            if mid != previous_mid:
                result["mid_observations"] += 1
                events.append(stamp)
                if len(events) == settings.rv.fast_event_window:
                    spans.append((events[-1] - events[0]).total_seconds() / 60)
            previous, previous_mid = stamp, mid
    result["event_window_span_minutes"] = ({"count": len(spans), "min": min(spans),
                                             "median": statistics.median(spans), "max": max(spans)}
                                            if spans else {"count": 0})
    result["code_sha256"] = {name: digest(Path(__file__).parent / name) for name in
                              ("app_settings.py", "signal_engine.py", "analyze_snapshots.py",
                               "decision_support.py", "paper_backtest.py", "audit_snapshots.py")}
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--settings", type=Path, default=Path("settings.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = json.dumps(audit(args.csv_path, args.settings), ensure_ascii=False, indent=2)
    if args.output:
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(report + "\n")
    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
