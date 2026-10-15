"""Offline engine benchmark; does not connect to Excel or write market logs."""
import json
import math
import time
from datetime import datetime, timedelta, timezone

from app_settings import AppSettings
from signal_engine import SignalEngine


def run(count=20000):
    engine = SignalEngine(AppSettings())
    start = datetime(2026, 10, 16, tzinfo=timezone.utc)
    wall, cpu = time.perf_counter(), time.process_time()
    for index in range(count):
        row = {"timestamp_utc": (start + timedelta(seconds=index / 4)).isoformat()}
        for name, mid in (("3y", 103 + .04 * math.sin(index / 100)),
                          ("10y", 107 + .12 * math.sin(index / 110))):
            row.update({f"{name}_mid": mid, f"{name}_best_bid": mid - .005,
                        f"{name}_best_ask": mid + .005, f"{name}_spread": .01})
        engine.update(row)
    wall, cpu = time.perf_counter() - wall, time.process_time() - cpu
    return {"rows": count, "wall_seconds": round(wall, 3),
            "cpu_seconds": round(cpu, 3), "cpu_ms_per_row": round(cpu * 1000 / count, 4),
            "core_points": len(engine.core.points), "fast_points": len(engine.fast.levels),
            "scope": "synthetic engine only; excludes Excel, CSV I/O and UI"}


if __name__ == "__main__":
    print(json.dumps(run(), ensure_ascii=False))
