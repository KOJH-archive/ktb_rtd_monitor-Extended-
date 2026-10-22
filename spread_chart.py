"""Lightweight intraday 3Y-10Y spread chart.

Reads the current KST day's enhanced snapshot CSV and displays one-minute
last-observation values.  It is deliberately independent of the collector
and dashboard so it can be opened or closed without affecting either.
"""

from __future__ import annotations

import csv
import math
import sys
import time
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import tkinter as tk


DATA_DIR = Path(__file__).resolve().parent / "data"
REFRESH_MS = 5_000
def _kst_minute(timestamp: str) -> Optional[str]:
    try:
        value = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        # Avoid a third-party timezone dependency on the small chart utility.
        kst = value.astimezone(timezone.utc) + timedelta(hours=9)
        return kst.strftime("%Y-%m-%dT%H:%M")
    except (TypeError, ValueError, OverflowError):
        return None


def _number(row: dict, name: str) -> Optional[float]:
    try:
        value = float(row.get(name, ""))
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def minute_spreads(path: Path, target_date: Optional[date] = None) -> List[Tuple[str, Optional[float], Optional[float], Optional[float], Optional[float]]]:
    """Return minute spreads plus 3Y and 10Y foreign cumulative net.

    The last valid observation in each minute is used. Missing values remain
    missing; no interpolation is performed across an Excel/RTD gap.
    """
    target_date = target_date or datetime.now().date()
    buckets: Dict[str, Tuple[Optional[float], Optional[float], Optional[float], Optional[float]]] = {}
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = _kst_minute(row.get("timestamp_utc", ""))
            if not key or key[:10] != target_date.isoformat():
                continue
            cash = _number(row, "cash_spread_bp")
            futures = _number(row, "futures_yield_spread_bp")
            if cash is None:
                y3, y10 = _number(row, "cash_3y_yield_pct"), _number(row, "cash_10y_yield_pct")
                if y3 is not None and y10 is not None:
                    cash = (y10 - y3) * 100.0
            if futures is None:
                y3, y10 = _number(row, "3y_futures_yield_pct"), _number(row, "10y_futures_yield_pct")
                if y3 is not None and y10 is not None:
                    futures = (y10 - y3) * 100.0
            foreign_values = [_number(row, name) for name in
                              ("3y_foreign_buy", "3y_foreign_sell", "10y_foreign_buy", "10y_foreign_sell")]
            foreign_3y = (foreign_values[0] - foreign_values[1]) if all(value is not None for value in foreign_values[:2]) else None
            foreign_10y = (foreign_values[2] - foreign_values[3]) if all(value is not None for value in foreign_values[2:]) else None
            old = buckets.get(key, (None, None, None, None))
            buckets[key] = (cash if cash is not None else old[0],
                            futures if futures is not None else old[1],
                            foreign_3y if foreign_3y is not None else old[2],
                            foreign_10y if foreign_10y is not None else old[3])
    return [(key, *buckets[key]) for key in sorted(buckets)]


def current_csv(data_dir: Path = DATA_DIR, target_date: Optional[date] = None) -> Path:
    day = (target_date or datetime.now().date()).strftime("%Y%m%d")
    return data_dir / f"snapshots_enhanced_{day}.csv"


def latest_levels(path: Path, target_date: Optional[date] = None) -> dict:
    """Return the latest valid 3Y/10Y cash and futures yields for the day."""
    target_date = target_date or datetime.now().date()
    latest = {}
    if not path.exists():
        return latest
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            key = _kst_minute(row.get("timestamp_utc", ""))
            if not key or key[:10] != target_date.isoformat():
                continue
            for name in ("cash_3y_yield_pct", "cash_10y_yield_pct",
                         "3y_futures_yield_pct", "10y_futures_yield_pct"):
                value = _number(row, name)
                if value is not None and value > 0:
                    latest[name] = value
            for prefix in ("3y", "10y"):
                buy, sell = _number(row, f"{prefix}_foreign_buy"), _number(row, f"{prefix}_foreign_sell")
                if buy is not None and sell is not None:
                    latest[f"foreign_{prefix}_net"] = buy - sell
    if latest.get("3y_futures_yield_pct") is not None and latest.get("10y_futures_yield_pct") is not None:
        latest["futures_yield_spread_bp"] = (latest["10y_futures_yield_pct"] - latest["3y_futures_yield_pct"]) * 100.0
    return latest


class SpreadChart(tk.Tk):
    def __init__(self, data_dir: Path = DATA_DIR):
        super().__init__()
        self.data_dir = data_dir
        self.title("KTB 10Y-3Y Spread · 1분 추이")
        self.geometry("980x690")
        self.minsize(720, 520)
        self.configure(bg="#101722")
        self.window_minutes: Optional[int] = None
        self.last_rows = []
        self.last_read_ok = False
        self._refresh_job = None
        toolbar = tk.Frame(self, bg="#182333", height=36)
        toolbar.pack(fill="x", padx=12, pady=(10, 0))
        tk.Label(toolbar, text="10Y–3Y SPREAD", bg="#182333", fg="#e9eef5",
                 font=("Segoe UI", 10, "bold")).pack(side="left", padx=(10, 14))
        for label, minutes in (("60분", 60), ("120분", 120), ("전체", None)):
            button = tk.Button(toolbar, text=label, command=lambda value=minutes: self.set_window(value),
                               bg="#26364a", fg="#dbe7f3", activebackground="#38516d",
                               activeforeground="#ffffff", relief="flat", bd=0, padx=10, pady=4,
                               font=("Segoe UI", 9))
            button.pack(side="left", padx=2, pady=5)
        self.connection_badge = tk.Label(toolbar, text="● 대기", bg="#182333", fg="#f6ad55",
                                         font=("Segoe UI", 9, "bold"))
        self.connection_badge.pack(side="right", padx=10)
        levels = tk.Frame(self, bg="#101722")
        levels.pack(fill="x", padx=12, pady=(8, 0))
        self.level_labels = {}
        for key, title, color in (("cash_3y_yield_pct", "현물 3년 수익률", "#63b3ed"),
                                  ("cash_10y_yield_pct", "현물 10년 수익률", "#a78bfa"),
                                  ("futures_yield_spread_bp", "선물 10년–3년 차", "#f6ad55")):
            card = tk.Frame(levels, bg="#1a2738", highlightbackground="#2d4056", highlightthickness=1)
            card.pack(side="left", fill="x", expand=True, padx=3)
            tk.Label(card, text=title, bg="#1a2738", fg="#aab7c8", font=("Segoe UI", 9)).pack(anchor="w", padx=10, pady=(6, 0))
            value_label = tk.Label(card, text="N/A", bg="#1a2738", fg=color, font=("Segoe UI", 17, "bold"))
            value_label.pack(anchor="w", padx=10, pady=(0, 6))
            self.level_labels[key] = value_label
        flow_levels = tk.Frame(self, bg="#101722")
        flow_levels.pack(fill="x", padx=12, pady=(2, 0))
        self.flow_labels = {}
        for key, title in (("foreign_3y_net", "외인 3년 선물 누적 순매수"),
                           ("foreign_10y_net", "외인 10년 선물 누적 순매수")):
            card = tk.Frame(flow_levels, bg="#151f2d", highlightbackground="#29394d", highlightthickness=1)
            card.pack(side="left", fill="x", expand=True, padx=3)
            tk.Label(card, text=title, bg="#151f2d", fg="#93a4b8", font=("Segoe UI", 8)).pack(anchor="w", padx=9, pady=(3, 0))
            value_label = tk.Label(card, text="N/A", bg="#151f2d", fg="#d9e2ec", font=("Segoe UI", 11, "bold"))
            value_label.pack(anchor="w", padx=9, pady=(0, 3))
            self.flow_labels[key] = value_label
        self.canvas = tk.Canvas(self, bg="#101722", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=12, pady=(8, 0))
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda _event: self.canvas.delete("hover"))
        self._hover_points = []
        self.status = tk.Label(self, text="", anchor="w", bg="#101722", fg="#aab7c8", font=("Segoe UI", 9))
        self.status.pack(fill="x", padx=16, pady=6)
        self.bind("<Configure>", lambda _event: self._schedule_refresh())
        self._schedule_refresh(150)

    def _schedule_refresh(self, delay: int = REFRESH_MS) -> None:
        """Keep at most one pending refresh, including resize-triggered ones."""
        if self._refresh_job is None:
            self._refresh_job = self.after(delay, self.refresh)

    def refresh(self) -> None:
        self._refresh_job = None
        try:
            csv_path = current_csv(self.data_dir)
            rows = minute_spreads(csv_path)
            if not hasattr(self, "_last_levels_refresh") or time.monotonic() - self._last_levels_refresh >= 15:
                self._update_levels(latest_levels(csv_path))
                self._last_levels_refresh = time.monotonic()
            if rows:
                self.last_rows = rows
                self.last_read_ok = True
            elif not self.last_rows:
                self.last_read_ok = True
            self._draw(self._visible_rows())
            self.connection_badge.config(text="● 연결 정상" if rows else "● 데이터 대기",
                                         fg="#68d391" if rows else "#f6ad55")
        except (OSError, csv.Error, ValueError) as exc:
            # The collector can be flushing the CSV at this exact moment.
            # Keep the last complete frame instead of flashing an empty chart.
            self.last_read_ok = False
            self.connection_badge.config(text="● 재시도 중", fg="#fc8181")
            if self.last_rows:
                self._draw(self._visible_rows())
        except Exception:
            self.last_read_ok = False
            self.connection_badge.config(text="● 재시도 중", fg="#fc8181")
        self._schedule_refresh()

    def _update_levels(self, levels: dict) -> None:
        for name, label in self.level_labels.items():
            value = levels.get(name)
            if value is None:
                label.config(text="N/A")
            elif name == "futures_yield_spread_bp":
                label.config(text=f"{value:+.1f} bp")
            else:
                label.config(text=f"{value:.3f}%")
        for name, label in self.flow_labels.items():
            value = levels.get(name)
            label.config(text="N/A" if value is None else f"{value:+,.0f} 계약",
                         fg="#68d391" if value is not None and value >= 0 else "#fc8181")

    def set_window(self, minutes: Optional[int]) -> None:
        self.window_minutes = minutes
        self._draw(self._visible_rows())

    def _visible_rows(self):
        if self.window_minutes is None:
            return self.last_rows
        return self.last_rows[-self.window_minutes:]

    def _draw(self, rows: Iterable[Tuple[str, Optional[float], Optional[float]]]) -> None:
        rows = list(rows)
        self.canvas.delete("all")
        self._hover_points = []
        width, height = max(self.canvas.winfo_width(), 720), max(self.canvas.winfo_height(), 420)
        left, right, top, bottom = 72, 24, 40, 48
        plot_w, plot_h = width - left - right, height - top - bottom
        spread_bottom = top + plot_h * 0.68
        flow_top = spread_bottom + 30
        flow_bottom = height - bottom
        spread_height = spread_bottom - top
        values = [v for _, cash, fut, _foreign_3y, _foreign_10y in rows for v in (cash, fut) if v is not None]
        if not values:
            self.canvas.create_text(width / 2, height / 2, text="오늘의 유효한 10Y–3Y 데이터가 없습니다", fill="#aab7c8", font=("Segoe UI", 14))
            self.status.config(text=f"대상 파일: {current_csv(self.data_dir).name} · 데이터 대기 · 5초마다 갱신")
            return
        lo, hi = min(values), max(values)
        pad = max((hi - lo) * 0.12, 1.0)
        lo, hi = lo - pad, hi + pad
        self.canvas.create_text(left, 16, anchor="w", text="당일 1분 단위 10Y–3Y Spread (bp)", fill="#e9eef5", font=("Segoe UI", 14, "bold"))
        self.canvas.create_text(width - right, 16, anchor="e", text="● 현물   ● 선물수익률", fill="#aab7c8", font=("Segoe UI", 9))
        # Use 0.5bp grid spacing. On an unusually wide range, thin the grid
        # while retaining 0.5bp labels at ordinary intraday ranges.
        tick_start = math.floor(lo * 2) / 2
        tick_end = math.ceil(hi * 2) / 2
        tick_values = []
        tick = tick_start
        while tick <= tick_end + 1e-9:
            tick_values.append(tick)
            tick += 0.5
        stride = max(1, math.ceil(len(tick_values) / 36))
        for index, val in enumerate(tick_values):
            if index % stride:
                continue
            y = top + (hi - val) / (hi - lo) * spread_height
            self.canvas.create_line(left, y, width - right, y, fill="#263445")
            self.canvas.create_text(left - 8, y, anchor="e", text=f"{val:.1f}", fill="#8898ab", font=("Segoe UI", 8))
        self.canvas.create_rectangle(left, top, width - right, spread_bottom, outline="#3b4a5e")
        self.canvas.create_rectangle(left, flow_top, width - right, flow_bottom, outline="#3b4a5e")
        self.canvas.create_text(left, flow_top - 14, anchor="w", text="외국인 순매수 변화 (계약/분)", fill="#cbd5e1", font=("Segoe UI", 9, "bold"))
        self.canvas.create_text(width - right - 74, flow_top - 14, anchor="e", text="■ 3Y", fill="#edf2f7", font=("Segoe UI", 9, "bold"))
        self.canvas.create_text(width - right, flow_top - 14, anchor="e", text="■ 10Y", fill="#4f9ac6", font=("Segoe UI", 9, "bold"))
        n = max(len(rows) - 1, 1)

        def point(index: int, value: float) -> Tuple[float, float]:
            return left + plot_w * index / n, top + (hi - value) / (hi - lo) * spread_height

        for series_index, color in ((1, "#63b3ed"), (2, "#f6ad55")):
            previous = None
            for index, row in enumerate(rows):
                value = row[series_index]
                if value is None:
                    previous = None
                    continue
                current = point(index, value)
                if previous is not None:
                    self.canvas.create_line(*previous, *current, fill=color, width=2)
                previous = current
        previous_foreign = None
        flows = []
        for series_index in (3, 4):
            previous_foreign = None
            series_flows = []
            for row in rows:
                current_foreign = row[series_index]
                if current_foreign is None or previous_foreign is None:
                    series_flows.append(None)
                else:
                    series_flows.append(current_foreign - previous_foreign)
                if current_foreign is not None:
                    previous_foreign = current_foreign
            if series_index == 3:
                flows_3y = series_flows
            else:
                flows_10y = series_flows
        flows = [None if a is None and b is None else (a or 0.0) + (b or 0.0)
                 for a, b in zip(flows_3y, flows_10y)]
        valid_flows = [value for value in flows if value is not None]
        flow_limit = max(max((abs(value) for value in valid_flows), default=1.0), 1.0)
        zero_y = flow_top + (flow_bottom - flow_top) / 2
        self.canvas.create_line(left, zero_y, width - right, zero_y, fill="#657386")
        self.canvas.create_text(left - 8, zero_y, anchor="e", text="0", fill="#8898ab", font=("Segoe UI", 8))
        for index, (value_3y, value_10y) in enumerate(zip(flows_3y, flows_10y)):
            x = left + plot_w * index / n
            bar_width = max(1.5, plot_w / max(len(rows), 1) * 0.32)
            for offset, value, color in ((-bar_width / 2, value_3y, "#edf2f7"),
                                         (bar_width / 2, value_10y, "#4f9ac6")):
                if value is None:
                    continue
                bar_height = abs(value) / flow_limit * ((flow_bottom - flow_top) / 2 - 4)
                y1, y2 = (zero_y - bar_height, zero_y) if value >= 0 else (zero_y, zero_y + bar_height)
                self.canvas.create_rectangle(x + offset - bar_width / 2, y1, x + offset + bar_width / 2, y2,
                                             fill=color, outline=color)
        for index, row in enumerate(rows):
            x = left + plot_w * index / n
            self._hover_points.append((x, row[0], row[1], row[2]))
        # Label every 10 minutes in the actual KST time axis. If the day is
        # long enough to make labels collide, retain a regular 20/30-minute
        # subset while keeping the underlying point resolution at one minute.
        first_minute = datetime.strptime(rows[0][0], "%Y-%m-%dT%H:%M")
        last_minute = datetime.strptime(rows[-1][0], "%Y-%m-%dT%H:%M")
        total_minutes = max(int((last_minute - first_minute).total_seconds() / 60), 1)
        label_step = 10 * max(1, math.ceil(total_minutes / 180))
        for minute_offset in range(0, total_minutes + 1, 10):
            target = first_minute + timedelta(minutes=minute_offset)
            index = min(range(len(rows)), key=lambda candidate: abs(
                (datetime.strptime(rows[candidate][0], "%Y-%m-%dT%H:%M") - target).total_seconds()))
            x = left + plot_w * index / n
            self.canvas.create_line(x, top, x, height - bottom, fill="#1b2939")
            if minute_offset % label_step == 0:
                self.canvas.create_text(x, height - bottom + 18, text=target.strftime("%H:%M"), fill="#8898ab", font=("Segoe UI", 8))
        latest = rows[-1]
        latest_3y = flows_3y[-1] if flows_3y else None
        latest_10y = flows_10y[-1] if flows_10y else None
        window_label = "전체" if self.window_minutes is None else f"최근 {self.window_minutes}분"
        self.status.config(text=f"최근 {latest[0][11:16]} · 현물 {self._fmt(latest[1])} bp · 선물수익률 {self._fmt(latest[2])} bp · 외인 3Y {self._fmt(latest_3y)} / 10Y {self._fmt(latest_10y)} 계약/분 · {window_label} · 5초마다 갱신")

    @staticmethod
    def _fmt(value: Optional[float]) -> str:
        return "N/A" if value is None else f"{value:+.1f}"

    def _on_motion(self, event) -> None:
        if not self._hover_points:
            return
        nearest = min(self._hover_points, key=lambda point: abs(point[0] - event.x))
        if abs(nearest[0] - event.x) > 18:
            self.canvas.delete("hover")
            return
        _x, timestamp, cash, futures = nearest
        self.canvas.delete("hover")
        self.canvas.create_line(nearest[0], 40, nearest[0], self.canvas.winfo_height() - 48,
                               fill="#718096", dash=(3, 3), tags="hover")
        text = f"{timestamp[11:16]}  현물 {self._fmt(cash)} bp  선물수익률 {self._fmt(futures)} bp"
        tooltip_width = max(220, len(text) * 7)
        x = min(max(event.x + 12, 8), max(self.canvas.winfo_width() - tooltip_width - 8, 8))
        y = max(event.y - 30, 8)
        self.canvas.create_rectangle(x, y, x + tooltip_width, y + 24, fill="#243448", outline="#60758d", tags="hover")
        self.canvas.create_text(x + 8, y + 12, anchor="w", text=text, fill="#f1f5f9", font=("Segoe UI", 9), tags="hover")


def main() -> None:
    data_dir = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DATA_DIR
    SpreadChart(data_dir).mainloop()


if __name__ == "__main__":
    main()
