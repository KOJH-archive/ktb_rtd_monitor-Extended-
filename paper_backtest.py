"""Research replay with next-observation fills, not an execution simulator."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path

from analyze_snapshots import timestamp_kst
from app_settings import load_settings
from decision_support import executable_package_pnl
from signal_engine import NUMERIC_KEYS, SignalEngine, validated_row

FIELDS = ("entry_signal_utc", "entry_utc", "exit_signal_utc", "exit_utc", "mark_utc", "direction",
          "holding_minutes", "entry_rv_level", "exit_rv_level", "gross_mid_pnl_points",
          "crossing_cost_points", "extra_cost_points", "net_pnl_points", "mark_net_pnl_points",
          "status", "exit_reason")


@dataclass
class OpenTrade:
    state: int
    signal_timestamp: str
    row: dict[str, object]


def replay(path: Path, settings_path: Path, diagnostics: dict | None = None) -> list[dict[str, object]]:
    settings = load_settings(settings_path)
    engine = SignalEngine(settings)
    beta = engine.beta
    position: OpenTrade | None = None
    pending: tuple[int, str] | None = None
    previous: dict[str, object] | None = None
    records: list[dict[str, object]] = []
    stats = diagnostics if diagnostics is not None else {}
    stats.update(rows=0, invalid_rows=0, resets=0, ready_rows=0, max_fast_samples=0,
                 max_completed_minutes=0, entries=0, closed=0, unclosed=0)

    def finish(row: dict[str, object], reason: str, realized: bool, signal_time: str = "") -> None:
        nonlocal position
        if position is None:
            return
        entry = position.row
        entry_level = float(entry["10y_mid"]) - beta * float(entry["3y_mid"])
        exit_level = float(row["10y_mid"]) - beta * float(row["3y_mid"])
        gross = -position.state * (exit_level - entry_level)
        after_crossing = executable_package_pnl(entry, row, position.state, beta)
        extra = settings.execution.extra_roundtrip_cost_points
        net = after_crossing - extra
        records.append(dict(zip(FIELDS, (
            position.signal_timestamp, entry["timestamp_utc"], signal_time,
            row["timestamp_utc"] if realized else "", "" if realized else row["timestamp_utc"],
            "RV high" if position.state == 1 else "RV low",
            (timestamp_kst(row["timestamp_utc"]) - timestamp_kst(entry["timestamp_utc"])).total_seconds() / 60,
            entry_level, exit_level, gross, gross - after_crossing, extra,
            net if realized else "", "" if realized else net,
            "closed" if realized else "unclosed_mark_only", reason))))
        stats["closed" if realized else "unclosed"] += 1
        position = None

    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not set((*NUMERIC_KEYS, "timestamp_utc")) <= set(reader.fieldnames):
            raise ValueError("Missing required snapshot columns")
        for raw in reader:
            stats["rows"] += 1
            try:
                row = validated_row(raw)
                result = engine.update(row)
            except (ValueError, TypeError, KeyError):
                stats["invalid_rows"] += 1
                if position is not None and previous is not None:
                    finish(previous, "invalid_data", False)
                pending = None
                engine.reset()
                previous = None
                continue
            if result.reset_reason:
                stats["resets"] += 1
                if position is not None and previous is not None:
                    finish(previous, result.reset_reason, False)
                pending = None
            stats["ready_rows"] += int(result.ready)
            stats["max_fast_samples"] = max(stats["max_fast_samples"], (result.fast or {}).get("samples", 0))
            stats["max_completed_minutes"] = max(stats["max_completed_minutes"], (result.core or {}).get("samples", 0))
            # Fill only a decision from an earlier row, irrespective of this
            # row's newly calculated signal. A gap cancels the pending intent.
            if pending is not None:
                target, signal_time = pending
                if position is not None and target != position.state:
                    finish(row, "signal_exit_next_observation", True, signal_time)
                if target and position is None:
                    position = OpenTrade(target, signal_time, row)
                    stats["entries"] += 1
                pending = None
            if result.ready:
                target = result.state
                if position is not None and target != position.state:
                    pending = (target if result.entry_allowed else 0, str(row["timestamp_utc"]))
                elif position is None and target and result.entry_allowed:
                    pending = (target, str(row["timestamp_utc"]))
            elif position is not None:
                finish(row, "signal_not_ready", False)
            previous = row
    if position is not None and previous is not None:
        finish(previous, "end_of_data", False)
    stats["pending_at_end"] = pending is not None
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_path", nargs="?", type=Path, default=Path("data/snapshots_with_trades.csv"))
    parser.add_argument("--settings", type=Path, default=Path("settings.json"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output and args.output.resolve() in (args.csv_path.resolve(), args.settings.resolve()):
        parser.error("Output must not overwrite input or settings")
    diagnostics: dict = {}
    records = replay(args.csv_path, args.settings, diagnostics)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(records)
    closed = [record for record in records if record["status"] == "closed"]
    print(diagnostics)
    print(f"Closed hypothetical trades: {len(closed)}; net synthetic points: "
          f"{sum(float(record['net_pnl_points']) for record in closed):+.6f}")
    print("Unclosed marks excluded. Next recorded quote is not a guaranteed fill.")
    print("Fractional hedge, no depth/latency/slippage/margin/funding model; no alpha validation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
