"""One-click supervisor for the collector and dashboard on Windows."""
from __future__ import annotations

import msvcrt
import subprocess
import time
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv32" / "Scripts" / "python.exe"
PYTHONW = ROOT / ".venv32" / "Scripts" / "pythonw.exe"
WORKBOOK = ROOT / "국채선물 호가창.xlsx"
DATA_DIR = ROOT / "data"
LOG_PATH = DATA_DIR / "launcher.log"
LOCK_PATH = DATA_DIR / "monitor_launcher.lock"
DASHBOARD_LOCK_PATH = DATA_DIR / "dashboard.lock"
MAX_LOG_BYTES = 2 * 1024 * 1024


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.handle.seek(0, 2) == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        try:
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            self.handle.close()
            self.handle = None
            raise AlreadyRunning("KTB 모니터가 이미 실행 중입니다.") from exc
        return self

    def __exit__(self, *_args) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def _notify(title: str, message: str) -> None:
    import tkinter as tk
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    messagebox.showinfo(title, message, parent=root)
    root.destroy()


def collector_command() -> list[str]:
    return [str(PYTHON), str(ROOT / "ktb_rtd_monitor.py"),
            "--interval-ms", "1000", "--output-dir", str(DATA_DIR),
            "--log", "both", "--flush-ms", "1000",
            "--heartbeat-seconds", "5", "--quiet"]


def _rotate_log() -> None:
    """Keep the launcher log small without touching market-data files."""
    if not LOG_PATH.is_file() or LOG_PATH.stat().st_size < MAX_LOG_BYTES:
        return
    backup = LOG_PATH.with_suffix(".log.old")
    backup.unlink(missing_ok=True)
    LOG_PATH.replace(backup)


def _stop(process: subprocess.Popen, timeout: float = 5.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def _run_dashboard(log) -> int:
    """Open one dashboard, attaching to an already-running collector if needed."""
    with InstanceLock(DASHBOARD_LOCK_PATH):
        dashboard = subprocess.Popen([str(PYTHONW), str(ROOT / "dashboard.py")],
                                     cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
        return dashboard.wait()


def main() -> int:
    if not PYTHON.is_file() or not PYTHONW.is_file():
        _notify("KTB 모니터", ".venv32 Python 실행 환경을 찾을 수 없습니다.")
        return 1
    if not WORKBOOK.is_file():
        _notify("KTB 모니터", "국채선물 호가창.xlsx 파일을 찾을 수 없습니다.")
        return 1
    try:
        with InstanceLock(LOCK_PATH):
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _rotate_log()
            with LOG_PATH.open("a", encoding="utf-8") as log:
                log.write(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] one-click launcher started\n")
                log.flush()
                hidden = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                collector = subprocess.Popen(collector_command(), cwd=ROOT, stdout=log,
                                             stderr=subprocess.STDOUT, creationflags=hidden)
                try:
                    time.sleep(1.0)
                    if collector.poll() is not None:
                        _notify("KTB 모니터", "수집기가 시작되지 않았습니다. data\\launcher.log를 확인하세요.")
                        return 1
                    return _run_dashboard(log)
                finally:
                    _stop(collector)
                    log.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] one-click launcher stopped\n")
                    log.flush()
    except AlreadyRunning as exc:
        # The standalone spread chart may own collector.lock. Attach the
        # normal dashboard to that collector instead of treating it as a
        # startup failure. A second dashboard is still refused separately.
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with LOG_PATH.open("a", encoding="utf-8") as log:
                return _run_dashboard(log)
        except AlreadyRunning:
            _notify("KTB 모니터", "대시보드가 이미 실행 중입니다.")
            return 0
    except Exception as exc:
        _notify("KTB 모니터", f"실행 중 오류가 발생했습니다.\n{exc}\n\ndata\\launcher.log를 확인하세요.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
