"""Read-only UI preview and CPU sample. No Excel, notifications or settings writes.

Run with --demo for clearly marked synthetic values, otherwise read saved logs.
The preview closes automatically after --seconds (default 90).
"""
import argparse
import cProfile
import ctypes
import json
import math
import pstats
import time
import traceback
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from dashboard import Dashboard


def memory_mb():
    class Counters(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong)] + [
            (name, ctypes.c_size_t) for name in ("peak", "working", "paged_peak", "paged",
                                               "nonpaged_peak", "nonpaged", "pagefile", "pagefile_peak")]
    info = Counters()
    info.cb = ctypes.sizeof(info)
    kernel = ctypes.windll.kernel32
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    query = ctypes.windll.psapi.GetProcessMemoryInfo
    query.argtypes = [ctypes.c_void_p, ctypes.POINTER(Counters), ctypes.c_ulong]
    if not query(kernel.GetCurrentProcess(), ctypes.byref(info), info.cb):
        raise ctypes.WinError()
    return info.working / 1024 ** 2


def demo_row(stamp, index):
    row = {"timestamp_utc": stamp.isoformat(), "cash_3y_yield_pct": 3.861, "cash_10y_yield_pct": 4.188}
    for name, mid in (("3y", 103 + .04 * math.sin(index / 100)),
                      ("10y", 107 + .12 * math.sin(index / 110))):
        row.update({f"{name}_mid": mid, f"{name}_best_bid": mid - .005,
                    f"{name}_best_ask": mid + .005, f"{name}_spread": .01,
                    f"{name}_microprice": mid + .001, f"{name}_obi": .12,
                    f"{name}_weighted_obi": .18, f"{name}_bid_depth": 1234,
                    f"{name}_ask_depth": 1040, f"{name}_trade_price": mid,
                    f"{name}_foreign_buy": 40000 + index * 2,
                    f"{name}_foreign_sell": 39000 + index,
                    f"{name}_futures_yield_pct": 4.0})
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--seconds", type=int, default=90)
    parser.add_argument("--profile", action="store_true")
    parser.add_argument("--tab", type=int, choices=(0, 1, 2), default=0)
    parser.add_argument("--warmup-rows", type=int, default=20000)
    parser.add_argument("--withdraw", action="store_true")
    args = parser.parse_args()
    with patch("dashboard.WindowsNotifier.notify"), patch("dashboard.AlertEditor"):
        view = Dashboard()
        callback_errors = []
        def report_error(kind, value, trace):
            callback_errors.append(str(value))
            traceback.print_exception(kind, value, trace)
        view.report_callback_exception = report_error
        view.title("KTB 디자인 검수 · " + ("모의 데이터" if args.demo else "저장 로그"))
        view.notebook.select(args.tab)
        if args.withdraw:
            view.withdraw()
        if args.demo:
            # Fixed historical KST session for deterministic warm-up, then use
            # its same date/time clock during preview, with an explicit title.
            start = datetime(2026, 10, 16, 0, tzinfo=timezone.utc)
            count = max(1000, args.warmup_rows)
            records = [demo_row(start + timedelta(seconds=i * 5000 / count), i) for i in range(count)]
            def read_demo(path):
                if records:
                    batch = records[:]
                    records.clear()
                    return batch, True
                return [], False
            view.tail.read_new = read_demo
        baseline = {}
        profiler = cProfile.Profile() if args.profile else None
        def begin_sample():
            if view.pending_rows:
                view.after(500, begin_sample)
                return
            baseline.update(wall=time.perf_counter(), cpu=time.process_time(), memory=memory_mb())
            if profiler:
                profiler.enable()
        def finish():
            if profiler:
                profiler.disable()
            if baseline:
                elapsed = time.perf_counter() - baseline["wall"]
                cpu = time.process_time() - baseline["cpu"]
                print(json.dumps({"mode": "demo" if args.demo else "saved_logs", "sample_seconds": round(elapsed, 2),
                                  "cpu_seconds": round(cpu, 3), "one_core_cpu_percent": round(cpu / elapsed * 100, 2),
                                  "working_set_mb": round(memory_mb(), 1),
                                  "working_set_start_mb": round(baseline["memory"], 1)}, ensure_ascii=False), flush=True)
            if profiler:
                pstats.Stats(profiler).sort_stats("cumulative").print_stats(15)
            view.destroy()
        view.after(15000, begin_sample)
        view.after(max(20, args.seconds) * 1000, finish)
        view.mainloop()
        if callback_errors:
            raise RuntimeError(f"UI callback failures: {len(callback_errors)}")


if __name__ == "__main__":
    main()
