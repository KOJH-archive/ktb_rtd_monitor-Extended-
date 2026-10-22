"""Start the collector only when needed, then open the independent spread chart."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from spread_chart import DATA_DIR, SpreadChart


ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv32" / "Scripts" / "python.exe"
LOG_PATH = DATA_DIR / "launcher.log"


def collector_command() -> list[str]:
    return [str(PYTHON), str(ROOT / "ktb_rtd_monitor.py"),
            "--interval-ms", "1000", "--output-dir", str(DATA_DIR),
            "--log", "both", "--flush-ms", "1000",
            "--heartbeat-seconds", "5", "--quiet"]


def main() -> int:
    if not PYTHON.is_file():
        raise SystemExit(".venv32 Python 실행 환경을 찾을 수 없습니다.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    hidden = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with LOG_PATH.open("a", encoding="utf-8") as log:
        collector = subprocess.Popen(collector_command(), cwd=ROOT, stdout=log,
                                     stderr=subprocess.STDOUT, creationflags=hidden)
    # A live collector exits quickly with code 2 when another collector owns
    # the output lock. In that case the chart still uses the existing writer.
    time.sleep(0.8)
    owns_collector = collector.poll() is None
    try:
        SpreadChart(DATA_DIR).mainloop()
    finally:
        if owns_collector and collector.poll() is None:
            collector.terminate()
            try:
                collector.wait(timeout=5)
            except subprocess.TimeoutExpired:
                collector.kill()
                collector.wait(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
