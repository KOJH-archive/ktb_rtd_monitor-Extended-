from __future__ import annotations
import csv, json, math, os, tkinter as tk
from collections import deque
from time import perf_counter
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from analyze_snapshots import MinuteRVAnalyzer, RollingRVAnalyzer, timestamp_kst
from app_settings import AppSettings, effective_beta, load_settings
from decision_support import OrderFlowAnalyzer, basis_metrics, execution_gate, risk_metrics
from extra_settings import METRICS, AlertRule, load_extra_settings, parse_settings
from market_extensions import (AlertEngine, ForeignFlowTracker, WindowsNotifier,
                               market_metrics, metric_is_fresh, source_timestamp)
from signal_engine import SignalEngine, validated_row
from curve_context import CashCurveTracker
from cash_risk import load_cash_days, target_packages
from futures_dv01 import futures_dv01_pair
from relative_value import RelativeValueTracker, relative_execution_cost
from dashboard_widgets import ACCENT, INK, MUTED, WARN, build_dashboard

DATA_DIR = Path(__file__).parent / "data"
SETTINGS_PATH = Path(__file__).parent / "settings.json"
EXTRA_SETTINGS_PATH = Path(__file__).parent / "extra_settings.json"

def current_csv() -> Path:
    """Select only today's KST log; never fall back to a prior session."""
    day = timestamp_kst(datetime.now(timezone.utc)).strftime("%Y%m%d")
    enhanced_csv = DATA_DIR / f"snapshots_enhanced_{day}.csv"
    trade_csv = DATA_DIR / f"snapshots_with_trades_{day}.csv"
    if enhanced_csv.exists():
        return enhanced_csv
    if trade_csv.exists():
        return trade_csv
    return enhanced_csv


class IncrementalCsvTail:
    """Read only rows appended after attachment, without historical replay."""

    def __init__(self) -> None:
        self.path: Path | None = None
        self.offset = 0
        self.header: list[str] = []
        self.pending = b""
        self.identity = None

    def _parse(self, lines: list[bytes]) -> list[dict[str, str]]:
        rows = []
        for raw in lines:
            if not raw.strip():
                continue
            values = next(csv.reader([raw.decode("utf-8")]))
            if values == self.header:
                continue
            if len(values) != len(self.header):
                raise ValueError("CSV column count mismatch")
            rows.append(dict(zip(self.header, values)))
        return rows

    def _reset(self, path: Path) -> list[dict[str, str]]:
        with path.open("rb") as handle:
            header_line = handle.readline()
            if not header_line.endswith(b"\n"):
                raise ValueError("Incomplete CSV header")
            header_end = handle.tell()
            handle.seek(0, 2)
            self.offset = handle.tell()
            self.identity = os.fstat(handle.fileno()).st_ino
            handle.seek(max(header_end, self.offset - 65536))
            tail_end = handle.read(self.offset - handle.tell())
        self.path = path
        self.header = next(csv.reader([header_line.decode("utf-8-sig").strip()]))
        # Preserve only an in-flight partial line, not completed old records.
        self.pending = tail_end.rpartition(b"\n")[2] if b"\n" in tail_end else b""
        return []

    def read_new(self, path: Path) -> tuple[list[dict[str, str]], bool]:
        stat = path.stat()
        size = stat.st_size
        if path != self.path or size < self.offset or stat.st_ino != self.identity:
            return self._reset(path), True
        if size == self.offset:
            return [], False
        with path.open("rb") as handle:
            handle.seek(self.offset)
            data = self.pending + handle.read(1024 * 1024)
            self.offset = handle.tell()
        lines = data.splitlines(keepends=True)
        self.pending = b""
        if lines and not lines[-1].endswith(b"\n"):
            self.pending = lines.pop()
        return self._parse([line.rstrip(b"\r\n") for line in lines]), False


class ForeignHistoryReplay:
    """Read a fixed startup slice of today's CSV without replaying signals."""

    def __init__(self, path: Path, limit: int) -> None:
        self.path, self.limit = path, limit
        with path.open("rb") as handle:
            header = handle.readline()
            if not header.endswith(b"\n"):
                raise ValueError("Incomplete CSV header")
            self.offset = handle.tell()
            self.identity = os.fstat(handle.fileno()).st_ino
        self.header = next(csv.reader([header.decode("utf-8-sig").strip()]))
        self.rows_read = 0

    def read_batch(self, maximum: int = 500) -> list[dict[str, str]]:
        rows = []
        with self.path.open("rb") as handle:
            if os.fstat(handle.fileno()).st_ino != self.identity or os.fstat(handle.fileno()).st_size < self.limit:
                raise OSError("Today's CSV changed during foreign-flow restoration")
            handle.seek(self.offset)
            while len(rows) < maximum and handle.tell() < self.limit:
                raw = handle.readline(self.limit - handle.tell())
                if not raw.endswith(b"\n"):
                    self.offset = self.limit
                    break
                self.offset = handle.tell()
                values = next(csv.reader([raw.decode("utf-8")]))
                if values == self.header:
                    continue
                rows.append(dict(zip(self.header, values)) if len(values) == len(self.header) else {})
        self.rows_read += len(rows)
        return rows

    @property
    def done(self) -> bool:
        return self.offset >= self.limit


def numeric_row(row: dict[str, str]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for key, value in row.items():
        try:
            parsed[key] = value if key == "timestamp_utc" else float(value)
        except (TypeError, ValueError):
            parsed[key] = value
    return parsed


def recent_complete_rows(path: Path, limit: int, header: list[str],
                         maximum_bytes: int = 1024 * 1024):
    """Yield recent complete CSV rows newest-first without changing tail state."""
    start = max(0, limit - maximum_bytes)
    with path.open("rb") as handle:
        handle.seek(start)
        data = handle.read(limit - start)
    if start:
        _, separator, data = data.partition(b"\n")
        if not separator:
            return
    lines = data.splitlines(keepends=True)
    if lines and not lines[-1].endswith(b"\n"):
        lines.pop()
    for raw in reversed(lines):
        values = next(csv.reader([raw.rstrip(b"\r\n").decode("utf-8-sig")]))
        if values != header and len(values) == len(header):
            yield dict(zip(header, values))

class Dashboard(tk.Tk):
    def __init__(self):
        super().__init__()
        self.base_settings: AppSettings = load_settings(SETTINGS_PATH)
        self.settings: AppSettings = self.base_settings
        self.auto_dv01: dict[str, float | str] | None = None
        self.extra_settings = load_extra_settings(EXTRA_SETTINGS_PATH)
        self.tail = IncrementalCsvTail()
        self.fast_analyzer: RollingRVAnalyzer
        self.core_analyzer: MinuteRVAnalyzer
        self._reset_analyzers()
        self.cash_risk_report = self._load_cash_risk()
        self.rejected_rows = 0
        self.decision_text = "중립/관찰"
        self.latest_row: dict[str, object] | None = None
        self.fast_result: dict[str, object] | None = None
        self.core_result: dict[str, object] | None = None
        self.flow_result: dict[str, object] | None = None
        self.pending_rows = deque()
        self.replaying = False
        self.alert_popup = None
        build_dashboard(self)
        self.after(500, self.refresh)

    def _reset_analyzers(self) -> None:
        self.engine = SignalEngine(self.settings)
        self.fast_analyzer = self.engine.fast
        self.core_analyzer = self.engine.core
        self.flow_analyzer = OrderFlowAnalyzer(self.settings.flow)
        self.consensus = self.engine.consensus
        self.decision_text = "중립/관찰"
        self.latest_row = self.fast_result = self.core_result = self.flow_result = None
        self.signal_result = None
        self.alert_engine = AlertEngine(self.extra_settings.rules)
        self.notifier = WindowsNotifier(self.extra_settings)
        self.foreign_trackers = {name: ForeignFlowTracker(name, self.extra_settings) for name in ("3y", "10y")}
        self.curve_tracker = CashCurveTracker()
        self.relative_tracker = RelativeValueTracker()
        self.relative_result: dict[str, object] | None = None
        self.flow_history: ForeignHistoryReplay | None = None
        self.flow_catchup_end: int | None = None
        self.risk_window_started_at: datetime | None = None
        self.risk_reset_reason: str | None = None

    def _record_risk_progress(self, result, row: dict[str, object]) -> None:
        timestamp = datetime.fromisoformat(str(row["timestamp_utc"]))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        if self.risk_window_started_at is None:
            self.risk_window_started_at = timestamp
            if self.risk_reset_reason is None:
                self.risk_reset_reason = "startup"
        if result.reset_reason and result.reset_reason != "data_gap_resumed":
            self.risk_window_started_at = timestamp
            self.risk_reset_reason = result.reset_reason
        elif result.reset_reason == "data_gap_resumed":
            self.risk_reset_reason = result.reset_reason

    def _configure_auto_dv01(self, row: dict[str, object]) -> bool:
        """Freeze standard-bond DV01 at launch; manual configured values win."""
        base_risk = self.base_settings.risk
        if base_risk.dv01_3y_krw_per_bp is not None or self.auto_dv01 is not None:
            return False
        try:
            calculated = futures_dv01_pair(row)
            timestamp = datetime.fromisoformat(str(row["timestamp_utc"]))
            if timestamp.tzinfo is None:
                return False
            age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
            if not 0 <= age <= self.base_settings.data.stale_seconds:
                return False
        except (KeyError, TypeError, ValueError, OverflowError):
            return False
        risk = replace(base_risk,
                       dv01_3y_krw_per_bp=calculated["dv01_3y_krw_per_bp"],
                       dv01_10y_krw_per_bp=calculated["dv01_10y_krw_per_bp"])
        self.settings = replace(self.base_settings, risk=risk)
        self.auto_dv01 = {**calculated, "timestamp_utc": timestamp.isoformat()}
        self.engine = SignalEngine(self.settings)
        self.fast_analyzer = self.engine.fast
        self.core_analyzer = self.engine.core
        self.consensus = self.engine.consensus
        self.fast_result = self.core_result = self.signal_result = None
        self.risk_window_started_at = None
        self.risk_reset_reason = None
        self.cash_risk_report = self._load_cash_risk()
        return True

    def _ingest_foreign_row(self, row: dict[str, object], timestamp: datetime) -> None:
        session = row.get("session_id") or timestamp_kst(timestamp).date()
        for name, tracker in self.foreign_trackers.items():
            raw_source_time = row.get(f"{name}_foreign_time")
            source_time = source_timestamp(raw_source_time, timestamp)
            if raw_source_time in (None, "") or (source_time is not None and
                    abs((timestamp.astimezone(timezone.utc) - source_time).total_seconds())
                    <= self.extra_settings.max_source_age_seconds):
                tracker.update(row, timestamp, session)
            else:
                tracker.pause()

    def _update_relative_value(self, row: dict[str, object], timestamp: datetime) -> None:
        metrics = market_metrics(row)
        if ("cross_basis_bp" in metrics
                and metric_is_fresh(row, "cross_basis_bp", timestamp, self.extra_settings)):
            self.relative_result = self.relative_tracker.update(row)
        else:
            self.relative_tracker.pause()
            self.relative_result = None

    def _restore_foreign_history(self) -> None:
        history = self.flow_history
        if history is None:
            return
        today = timestamp_kst(datetime.now(timezone.utc)).date()
        for raw in history.read_batch():
            try:
                row = validated_row(numeric_row(raw))
                timestamp = datetime.fromisoformat(str(row["timestamp_utc"]))
                if timestamp_kst(timestamp).date() != today or timestamp > datetime.now(timezone.utc):
                    raise ValueError("Outside current trading day")
                result = self.engine.update(row, preserve_history_on_gap=True)
                self.fast_result, self.core_result = result.fast, result.core
                self._record_risk_progress(result, row)
                self._ingest_foreign_row(row, timestamp)
                self._update_relative_value(row, timestamp)
            except (KeyError, ValueError, TypeError, OverflowError):
                # A malformed historical row breaks continuity but should not
                # erase valid same-day observations recovered before it.
                self.engine.fast.pause()
                self.engine.core.pause()
                self.engine.consensus.reset()
                self.engine.previous = None
                self.fast_result = self.core_result = None
                self.fast_result = self.engine.fast.result()
                self.core_result = self.engine.core.result()
                self.risk_reset_reason = "invalid_row"
                for tracker in self.foreign_trackers.values():
                    tracker.pause()
                self.relative_tracker.pause()
                self.relative_result = None
        if history.done:
            # Historical observations warm only the volatility/RV windows. A
            # historical consensus state is never carried into live decisions.
            self.engine.consensus.reset()
            self.decision_text = "복원 완료 · 새 관측부터 판단"
            self.flow_history = None
            self.flow_catchup_end = history.path.stat().st_size

    def _alert_summary(self) -> str:
        enabled = sum(rule.enabled for rule in self.extra_settings.rules)
        return f"알림 {enabled}개 | 조건 미설정" if not enabled else f"알림 {enabled}개 감시 중"

    def open_alert_editor(self) -> None:
        AlertEditor(self)

    def reload_alerts(self) -> None:
        self.extra_settings = load_extra_settings(EXTRA_SETTINGS_PATH)
        self.alert_engine = AlertEngine(self.extra_settings.rules)
        self.notifier = WindowsNotifier(self.extra_settings)
        self.alert_status.config(text=self._alert_summary(), fg=ACCENT)

    def show_alert_popup(self, event) -> None:
        """Show a visible, non-modal alert without stealing input focus."""
        if self.alert_popup is not None and self.alert_popup.winfo_exists():
            self.alert_popup.destroy()
        popup = tk.Toplevel(self)
        self.alert_popup = popup
        popup.title("KTB 가격 알림")
        popup.configure(bg="#fff7df")
        popup.resizable(False, False)
        popup.attributes("-topmost", True)
        popup.protocol("WM_DELETE_WINDOW", popup.destroy)
        frame = tk.Frame(popup, bg="#fff7df", padx=18, pady=13,
                         highlightthickness=3, highlightbackground="#d69a20")
        frame.pack(fill="both", expand=True)
        heading = tk.Label(frame, text="KTB 가격 알림 · 확인할 때까지 표시", bg="#fff7df", fg="#7a4300",
                           font=("맑은 고딕", 12, "bold"), anchor="w")
        heading.pack(fill="x")
        tk.Label(frame, text=event.name, bg="#fff7df", fg="#241b10",
                 font=("맑은 고딕", 11), anchor="w", justify="left", wraplength=330).pack(fill="x", pady=(5, 0))
        tk.Label(frame, text=f"알림 발생값  {event.value:+.3f}", bg="#fff7df", fg="#241b10",
                 font=("Consolas", 15, "bold"), anchor="w").pack(fill="x", pady=(4, 8))
        tk.Button(frame, text="확인 · 닫기", command=popup.destroy, relief="flat",
                  bg="#e6c76a", activebackground="#d7b453", fg="#241b10",
                  font=("맑은 고딕", 9), padx=10).pack(anchor="e")
        popup.update_idletasks()
        width, height = popup.winfo_width(), popup.winfo_height()
        x = 28
        y = popup.winfo_screenheight() - height - 70
        popup.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        blink_job = None
        bright = False

        def blink():
            nonlocal blink_job, bright
            if not popup.winfo_exists():
                return
            bright = not bright
            frame.configure(highlightbackground="#d69a20" if bright else "#fff7df")
            heading.configure(bg="#ffe5a0" if bright else "#fff7df")
            blink_job = popup.after(700, blink)

        def cleanup(event):
            nonlocal blink_job
            if event.widget is not popup:
                return
            if blink_job is not None:
                try:
                    popup.after_cancel(blink_job)
                except tk.TclError:
                    pass
                blink_job = None
            if self.alert_popup is popup:
                self.alert_popup = None

        popup.bind("<Destroy>", cleanup, add="+")
        blink()

    def _risk_text(self) -> str:
        metrics = risk_metrics(self.settings.risk)
        if metrics is None:
            return (f"DV01: 미설정 | 연구 beta {self.settings.rv.beta:.4f} 사용 | "
                    "계약별 DV01(원/bp)을 입력하면 헤지 비율을 계산합니다.")
        source = ""
        if self.auto_dv01:
            stamp = timestamp_kst(self.auto_dv01["timestamp_utc"])
            source = (f"자동 표준채권 DV01 · 3Y {self.auto_dv01['dv01_3y_krw_per_bp']:,.0f} / "
                      f"10Y {self.auto_dv01['dv01_10y_krw_per_bp']:,.0f}원/bp "
                      f"· 수익률 {self.auto_dv01['yield_3y_pct']:.3f}%/{self.auto_dv01['yield_10y_pct']:.3f}% "
                      f"· {stamp:%H:%M:%S} KST 수집값으로 실행 중 고정\n")
        return (source + f"DV01 헤지: 10Y {metrics['contracts_10y']}계약당 3Y {metrics['exact_3y']:.2f}계약 "
                f"| 정수 패키지 {metrics['rounded_3y']}:{metrics['contracts_10y']} "
                f"| 총 {metrics['gross_dv01']:,.0f}원/bp | 잔여 {metrics['residual_dv01']:,.0f}원/bp "
                f"| {metrics['status']} | 분석 beta {effective_beta(self.settings):.4f}")

    def _load_cash_risk(self) -> dict[str, object]:
        source = self.settings.risk.cash_history_csv
        if source is None:
            return {"status": "missing_history"}
        path = Path(source)
        if not path.is_absolute():
            path = Path(__file__).parent / path
        try:
            today_kst = timestamp_kst(datetime.now(timezone.utc)).date()
            return target_packages(load_cash_days(path), self.settings.risk,
                                   today_kst - timedelta(days=1))
        except (OSError, ValueError) as exc:
            return {"status": "invalid_history", "reason": str(exc)}

    def _sizing_text(self) -> str:
        report = getattr(self, "cash_risk_report", {"status": "missing_history"})
        status = report["status"]
        if status == "missing_history":
            return "일별 현물금리 CSV 미연결 · settings.json의 risk.cash_history_csv 입력 필요"
        if status == "missing_settings":
            stale = (f"\n현물금리 {report['history_as_of']} 기준 · {report['history_age_days']}일 경과 · 갱신 필요"
                     if report.get("history_stale") else "")
            return "위험예산·DV01 입력 필요 · " + ", ".join(report["missing"]) + stale
        if status == "stale_history":
            return (f"수량 차단 · 현물금리 자료가 {report['history_as_of']} 기준으로 "
                    f"{report['history_age_days']}일 경과 (허용 {report['max_age_days']}일)")
        if status == "insufficient_history":
            return f"일별 위험 표본 {report['observations']}/{report['required']}개 · 수량 준비 중"
        if status != "research_proxy":
            return f"현물금리 위험 계산 불가 · {report.get('reason', status)}"
        limits = "한도 일부 미설정" if not report["limits_complete"] else "설정 한도 반영"
        return (f"현물금리 기반 연구 수량 {report['target_packages']}패키지 "
                f"(10Y {report['contracts_10y']} / 3Y {report['contracts_3y']}) · {limits}\n"
                f"기준 {report['as_of']} · 1패키지 일간 σ {report['unit_daily_sigma_krw']:,.0f}원 "
                f"/ 목표 위험 {report['daily_budget_krw']:,.0f}원")

    def _flow_text(self) -> str:
        if not self.flow_result:
            return "호가잔량 압력: 데이터 대기"
        return (f"호가잔량 압력(체결흐름 아님): 3Y {float(self.flow_result['3y_score']):+.3f} | "
                f"10Y {float(self.flow_result['10y_score']):+.3f} | "
                f"상대 {float(self.flow_result['relative_score']):+.3f} | {self.flow_result['state']}")

    def _basis_text(self, row: dict[str, object]) -> str:
        metrics = basis_metrics(row, self.settings.basis)
        if not metrics:
            return "수동 적정가 비교: 미설정 | 현물·선물 베이시스 엔진 미구현"
        parts = [f"{item['contract']} {float(item['basis']):+.4f} ({item['state']}, 순괴리 {float(item['net_edge']):.4f})"
                 for item in metrics]
        return "수동 적정가 비교(시점/출처 미검증): " + " | ".join(parts)

    def _cost_text(self, row: dict[str, object]) -> str:
        if not self.core_result or not bool(self.core_result.get("ready")):
            return "체결비용: 주 신호 준비 중"
        gate = execution_gate(row, self.core_result, effective_beta(self.settings), self.settings.execution)
        state = "통과" if gate["allowed"] else "진입 보류"
        return (f"연구 비용필터: 평균과의 거리 {float(gate['mean_distance']):.4f} (기대수익 아님) | "
                f"왕복비용 {float(gate['roundtrip_cost']):.4f} | 필요 {float(gate['required_edge']):.4f} | {state}")

    def _market_text(self, row: dict[str, object]) -> str:
        metrics = market_metrics(row)
        parts = []
        if "futures_spread" in metrics:
            parts.append(f"선물 체결가 10Y−3Y {metrics['futures_spread']:+.3f}pt")
        else:
            parts.append("선물 체결가 10Y−3Y N/A")
        if "cash_spread_bp" in metrics:
            parts.append(f"현물 체결수익률 10Y−3Y {metrics['cash_spread_bp']:+.1f}bp")
        else:
            parts.append("현물 체결수익률 10Y−3Y N/A")
        if not parts:
            return "체결가차: 추가 RTD 미연결"
        source_time = "원천시각 미연결, 수집시각 기준"
        if row.get("cash_3y_time") and row.get("cash_10y_time"):
            source_time = f"현물시각 {row['cash_3y_time']}/{row['cash_10y_time']}"
        return " | ".join(parts) + f" | {source_time}"

    def _foreign_text(self, name: str, row: dict[str, object]) -> str:
        buy_total = row.get(f"{name}_foreign_buy")
        sell_total = row.get(f"{name}_foreign_sell")
        if buy_total in (None, "") or sell_total in (None, ""):
            return f"{name.upper()} 외국인 관측 가격대: 추가 RTD 미연결"
        summary = self.foreign_trackers[name].summary()
        prefix = f"{name.upper()} 외국인 누적 순매수 {float(buy_total)-float(sell_total):+,.0f}"
        parts = []
        for side, label in (("buy", "매수"), ("sell", "매도")):
            item = summary[side]
            if item:
                yld = (f"/{item['estimated_avg_yield_pct']:.3f}%" if "estimated_avg_yield_pct" in item else "")
                parts.append(f"{label} 추정 {item['estimated_avg_price']:.3f}{yld} "
                             f"| 주구간 {item['dominant_price_low']:.2f}~{item['dominant_price_high']:.2f} "
                             f"(관측증가 {item['observed_volume']:,.0f})")
        windows = summary["windows"]
        recent = []
        for minutes in (5, 15, 60):
            item = windows.get(minutes)
            if item is None:
                continue
            partial = "" if item["complete"] else f" (부분 {item['observed_seconds']/60:.1f}/{minutes}분)"
            buy_price = "—" if item["buy_avg_price"] is None else f"{item['buy_avg_price']:.3f}"
            sell_price = "—" if item["sell_avg_price"] is None else f"{item['sell_avg_price']:.3f}"
            recent.append(f"최근 {minutes}분{partial}: 순매수 {item['net_volume']:+,.0f}계약 "
                          f"| 매수 추정가 {buy_price} / 매도 추정가 {sell_price}")
        pace = windows.get(1)
        if pace:
            recent.append((f"최근 1분 수급 속도 {pace['net_volume']:+,.0f}계약/분"
                           if pace["complete"] else "최근 1분 수급 속도 준비 중"))
        five = windows.get(5)
        if five and five["complete"] and five["price_change"] is not None:
            net, change = five["net_volume"], five["price_change"]
            if abs(net) < 1e-9:
                response = "순매수 중립"
            elif abs(change) < 1e-9:
                response = "수급 증가 대비 가격 정체"
            elif net * change < 0:
                response = "수급·가격 방향 엇갈림"
            else:
                response = "수급·가격 같은 방향"
            recent.append(f"최근 5분 체결가 변화 {change:+.3f}pt | {response} (인과관계 아님)")
        return (prefix + ("\n" + "\n".join(parts) if parts else " | 기준점 설정 중")
                + ("\n" + "\n".join(recent) if recent else ""))

    def _curve_text(self) -> tuple[str, str]:
        summary = self.curve_tracker.summary()
        if summary is None:
            return "현물 10Y−3Y: N/A · RTD 금리 대기", "5·15분 변화: 기준 데이터 대기"
        positions = []
        for minutes in (5, 60):
            point = summary[f"location_{minutes}m"]
            if point is None:
                positions.append(f"{minutes}분 위치 준비 중")
            else:
                z = "σ=0" if point["zscore"] is None else f"Z {point['zscore']:+.2f}"
                positions.append(f"{minutes}분 평균 {point['reference_mean_bp']:+.1f}bp 대비 "
                                 f"{point['distance_bp']:+.1f}bp ({z})")
        changes = {minutes: summary[f"change_{minutes}m_bp"] for minutes in (5, 15)}
        trend = " | ".join(f"{minutes}분 변화 " + (f"{value:+.1f}bp" if value is not None else "준비 중")
                           for minutes, value in changes.items())
        if all(value is not None for value in changes.values()):
            five, fifteen = changes[5], changes[15]
            direction = ("확대 동행" if five > 0 and fifteen > 0 else
                         "축소 동행" if five < 0 and fifteen < 0 else
                         "혼조/정체")
            trend += f" | {direction}"
        return (f"현재 {summary['spread_bp']:+.1f}bp\n" + "\n".join(positions), trend)

    def _relative_value_texts(self, row: dict[str, object], timestamp: datetime) -> tuple[str, str, str]:
        metrics = market_metrics(row)
        butterfly = metrics.get("cash_butterfly_bp")
        butterfly_fresh = (butterfly is not None and
                           metric_is_fresh(row, "cash_butterfly_bp", timestamp, self.extra_settings))
        if butterfly_fresh:
            curve = (f"현물 5Y 곡률 {float(butterfly):+.2f}bp\n"
                     "5Y − (5/7×3Y + 2/7×10Y) · 선형 만기보간 기준")
            if not any(row.get(key) not in (None, "")
                       for key in ("cash_3y_time", "cash_5y_time", "cash_10y_time")):
                curve += "\n원천시각 미연결 · 수집시각 기준"
        elif butterfly is None:
            curve = "현물 5Y 금리 미연결 · cash_5y_yield_pct 입력 필요"
        else:
            curve = "현물 3·5·10년 원천시각 불일치/지연 · 곡률 사용 중지"

        fresh = {key: key in metrics and metric_is_fresh(row, key, timestamp, self.extra_settings)
                 for key in ("basis_3y_bp", "basis_10y_bp", "cross_basis_bp")}
        if fresh["basis_3y_bp"] and fresh["basis_10y_bp"] and fresh["cross_basis_bp"]:
            basis = (f"3Y 현선물 {metrics['basis_3y_bp']:+.2f}bp | "
                     f"10Y 현선물 {metrics['basis_10y_bp']:+.2f}bp\n"
                     f"교차 베이시스 {metrics['cross_basis_bp']:+.2f}bp "
                     "= (10Y 선물−현물) − (3Y 선물−현물)")
            contracts = (f"3Y {row.get('3y_contract')} / 10Y {row.get('10y_contract')}"
                         if row.get("3y_contract") and row.get("10y_contract") else "계약월 미연결")
            timing = ("원천시각 미연결 · 수집시각 기준"
                      if not any(row.get(key) not in (None, "") for key in
                                 ("3y_trade_time", "10y_trade_time", "cash_3y_time", "cash_10y_time"))
                      else "연결된 원천시각 검사 통과")
            basis += f"\n{contracts} | {timing}"
        elif "cross_basis_bp" not in metrics:
            basis = "3Y·10Y 현물금리와 선물수익률 연결 대기"
        else:
            basis = "현물·선물 원천시각 불일치/지연 · 교차 베이시스 사용 중지"

        result = self.relative_result
        if not result or result.get("reference_mean_bp") is None:
            stats = "60분 기준 분봉 관측 준비 중"
        else:
            samples = int(result["samples"])
            zscore = result.get("zscore")
            half_life = result.get("half_life_minutes")
            ztext = "준비 중" if zscore is None else f"{float(zscore):+.2f}"
            half = "추정 불가" if half_life is None else f"{float(half_life):.1f}분"
            stats = (f"완료 분 기준 평균 {float(result['reference_mean_bp']):+.2f}bp | "
                     f"표준편차 {float(result['reference_stdev_bp'] or 0):.2f}bp | z {ztext}\n"
                     f"관측 반감기 {half} | 표본 {samples}/{int(result['required_samples'])} "
                     f"| 최근 연속 {int(result['continuous_samples'])}분")

        cost = relative_execution_cost(row, self.settings.risk)
        if cost is None:
            execution = stats + "\nDV01 또는 호가 간격 대기 · 비용 판정 불가"
        else:
            status = "통계 기준 준비 중"
            if result and result.get("reference_mean_bp") is not None and result.get("zscore") is not None:
                deviation = abs(float(result["cross_basis_bp"]) - float(result["reference_mean_bp"]))
                required = (float(cost["break_even_cross_basis_bp"])
                            * self.settings.execution.min_edge_to_roundtrip_cost)
                status = ("비용 초과 상대가치 괴리 관측 · 매매 신호 아님"
                          if abs(float(result["zscore"])) >= self.settings.rv.entry_z and deviation >= required
                          else "호가비용·통계 기준 미충족")
            execution = (stats + f"\nDV01 중립 정수 패키지 3Y {cost['contracts_3y']} / 10Y {cost['contracts_10y']} "
                         f"| 왕복 호가비용 약 {float(cost['roundtrip_cost_krw']):,.0f}원 "
                         f"| 손익분기 약 {float(cost['break_even_cross_basis_bp']):.2f}bp\n{status}")
        return curve, basis, execution

    def _render_overview(self, row: dict[str, object] | None, fresh: bool) -> None:
        metrics = market_metrics(row) if row else {}
        for key, label in self.kpis.items():
            value = metrics.get(key)
            rendered = ("N/A" if row else "—") if value is None else (
                f"{value:+.1f}" if key.endswith("_spread_bp") else f"{value:.3f}")
            label.config(text=rendered, fg=INK if fresh else MUTED)
        self.sizing.config(text=self._sizing_text())
        self.risk.config(text=self._risk_text())
        core = self.core_result or {}
        samples = int(core.get("minute_change_samples", 0))
        if core.get("minute_change_ready"):
            value = float(core["minute_change_stdev"])
            self.volatility.config(text=f"1분 변화 σ  {value:.5f} pt\n{samples}개 변화 표본 · 연율 환산 없음",
                                   fg=ACCENT if fresh else MUTED)
        else:
            required_changes = max(1, self.settings.rv.core_minutes - 1)
            self.volatility.config(text=f"준비 중 · 연속 1분 변화 {samples}/{required_changes}개\n"
                                   f"완료 분 {int(core.get('samples', 0))}/{self.settings.rv.core_minutes}개",
                                   fg=MUTED)
        span = float((self.fast_result or {}).get("span_seconds", 0))
        fast_samples = int((self.fast_result or {}).get("samples", 0))
        core_samples = int(core.get("samples", 0))
        start = (timestamp_kst(self.risk_window_started_at).strftime("%H:%M:%S")
                 if self.risk_window_started_at else "대기")
        reason = {"data_gap": "실시간 데이터 공백 후", "data_gap_resumed": "데이터 공백 연결 제외·당일 이력 유지",
                  "calendar_day_change": "날짜 변경 후",
                  "invalid_row": "불량 데이터 후", "startup": "당일 기록 복원/시작"}.get(
                      self.risk_reset_reason, "현재")
        self.window_note.config(
            text=f"당일 관측 기준 {start} KST · {reason}\n"
                 f"빠른 {fast_samples}/{self.settings.rv.fast_event_window} 이벤트 "
                 f"({max(0, self.settings.rv.fast_event_window-fast_samples)}개 남음, 실제 {span/60:.1f}분) · "
                 f"주 {core_samples}/{self.settings.rv.core_minutes}분 "
                 f"({max(0, self.settings.rv.core_minutes-core_samples)}분 남음)")

    def _decision_text(self) -> str:
        rv = self.settings.rv
        fast, core = self.fast_result, self.core_result
        if not fast or int(fast["samples"]) < rv.fast_event_window:
            samples = 0 if not fast else int(fast["samples"])
            return f"종합 판단: 빠른 신호 준비 중 ({samples}/{rv.fast_event_window} 이벤트)"
        if not core or not bool(core.get("ready")):
            samples = 0 if not core else int(core["samples"])
            return f"종합 판단: 주 신호 준비 중 ({samples}/{rv.core_minutes} 분)"
        return f"연구 관찰: {self.decision_text} | 실거래 승인 아님"

    @staticmethod
    def _rv_text(name: str, result: dict[str, object] | None, required: int, unit: str) -> str:
        if not result:
            return f"{name}: 데이터 대기"
        samples = int(result["samples"])
        if samples < required:
            return f"{name}: 준비 중 {samples}/{required} {unit} | level {float(result['latest_rv_level']):+.4f}"
        return (f"{name}: level {float(result['latest_rv_level']):+.4f} | 평균 {float(result['level_mean']):+.4f} "
                f"| z-score {float(result['zscore']):+.3f} | 표본 {samples}")

    def refresh(self):
        try:
            csv_path = current_csv()
            if self.flow_history is not None:
                self._restore_foreign_history()
                if self.flow_history is not None:
                    self.status.config(text=f"오늘 수급·변동성 복원 중 · {self.flow_history.rows_read:,}행", fg=MUTED)
                    self.after(25, self.refresh)
                    return
            if not self.pending_rows:
                start_offset = self.tail.offset
                rows, reset = self.tail.read_new(csv_path)
                if reset:
                    self.settings = self.base_settings
                    self.auto_dv01 = None
                    self._reset_analyzers()
                    self.cash_risk_report = self._load_cash_risk()
                    if csv_path.is_file():
                        history = ForeignHistoryReplay(csv_path, self.tail.offset)
                        for recent in recent_complete_rows(csv_path, self.tail.offset, history.header):
                            if self._configure_auto_dv01(numeric_row(recent)):
                                break
                        self.flow_history = history
                self.pending_rows.extend(rows)
                self.replaying = reset or (self.flow_catchup_end is not None
                                           and start_offset < self.flow_catchup_end)
            alert_events = []
            deadline = perf_counter() + .020
            processed = 0
            while self.pending_rows and processed < 250 and perf_counter() < deadline:
                raw = self.pending_rows.popleft()
                processed += 1
                try:
                    row = validated_row(numeric_row(raw))
                    # Validate display-only metrics as well as shared quotes.
                    for name in ("3y", "10y"):
                        for key in ("microprice", "obi", "weighted_obi", "bid_depth", "ask_depth"):
                            if not math.isfinite(float(row[f"{name}_{key}"])):
                                raise ValueError("Non-finite display metric")
                    self._configure_auto_dv01(row)
                    # Block while the feed is stale, but do not throw away
                    # valid same-day minute history after a transient outage.
                    # The engine still breaks return/confirmation continuity.
                    result = self.engine.update(row, preserve_history_on_gap=True)
                except (KeyError, ValueError, TypeError):
                    self.rejected_rows += 1
                    self._reset_analyzers()
                    continue
                if result.reset_reason:
                    self.flow_analyzer.reset()
                    self.curve_tracker.reset()
                self.signal_result = result
                self.latest_row = row
                self.fast_result, self.core_result = result.fast, result.core
                self._record_risk_progress(result, row)
                self.flow_result = self.flow_analyzer.update(row)
                self.decision_text = result.text
                timestamp = datetime.fromisoformat(str(row["timestamp_utc"]))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                if metric_is_fresh(row, "cash_spread_bp", timestamp, self.extra_settings):
                    self.curve_tracker.update(row)
                else:
                    self.curve_tracker.reset()
                self._update_relative_value(row, timestamp)
                self._ingest_foreign_row(row, timestamp)
                age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
                if not self.replaying:
                    metrics = market_metrics(row)
                    freshness = {metric: 0 <= age <= self.extra_settings.stale_seconds
                                 and metric_is_fresh(row, metric, timestamp, self.extra_settings)
                                 for metric in metrics}
                    alert_events.extend(self.alert_engine.update(
                        metrics, timestamp, freshness))
            if self.flow_catchup_end is not None and self.tail.offset >= self.flow_catchup_end and not self.pending_rows:
                self.flow_catchup_end = None
            if alert_events:
                last = alert_events[-1]
                self.notifier.notify(self.winfo_id())
                self.show_alert_popup(last)
                self.alert_status.config(text=f"알림: {last.name} | {last.value:+.3f}", fg=WARN)
            if self.latest_row:
                row = self.latest_row
                timestamp = datetime.fromisoformat(str(row["timestamp_utc"]))
                age = (datetime.now(timezone.utc) - timestamp).total_seconds()
                fresh = 0 <= age <= self.settings.data.stale_seconds and not self.pending_rows
                health = "수집 최신 · 원천시각 별도 확인" if fresh else f"지연/시각 이상 ({age:.0f}초) · 신호 차단"
                if self.pending_rows:
                    health = f"기록 읽는 중 · {len(self.pending_rows):,}행 남음"
                self.status.config(text=f"{timestamp_kst(timestamp):%m/%d %H:%M:%S} KST  |  {health}  |  제외 {self.rejected_rows}행  |  {csv_path.name}",
                                   fg=MUTED if fresh else WARN)
                for name in ("3y","10y"):
                    for key in ("mid","spread","obi","weighted_obi","microprice","bid_depth","ask_depth"):
                        value=float(row[f"{name}_{key}"]); text=f"{value:,.0f}" if "depth" in key else (f"{value:+.3f}" if "obi" in key else f"{value:.3f}")
                        self.labels[(name,key)].config(text=text)
                rv = self.settings.rv
                self.fast_rv.config(text=self._rv_text(f"빠른 RV ({rv.fast_event_window} 이벤트)", self.fast_result,
                                                       rv.fast_event_window, "이벤트"))
                self.core_rv.config(text=self._rv_text(f"주 RV (완료 {rv.core_minutes}분)", self.core_result,
                                                       rv.core_minutes, "분"))
                self.decision.config(text=self._decision_text() if fresh else "데이터 준비/지연: 현재 신호 사용 금지",
                                     fg=ACCENT if fresh else WARN)
                self.flow.config(text=self._flow_text())
                self.basis.config(text=self._basis_text(row))
                self.cost.config(text=self._cost_text(row))
                self.market_spreads.config(text=self._market_text(row))
                self.foreign_3y.config(text=self._foreign_text("3y", row))
                self.foreign_10y.config(text=self._foreign_text("10y", row))
                curve_position, curve_trend = self._curve_text()
                self.curve_position.config(text=curve_position)
                self.curve_trend.config(text=curve_trend)
                relative_curve, relative_basis, relative_execution = self._relative_value_texts(row, timestamp)
                self.relative_curve.config(text=relative_curve)
                self.relative_basis.config(text=relative_basis)
                self.relative_execution.config(text=relative_execution)
                self._render_overview(row, fresh)
                if not fresh and not self.pending_rows:
                    self.flow.config(text="호가잔량 압력: 최신 데이터 대기")
                    self.basis.config(text="수동 적정가 비교: 최신 데이터 대기")
                    self.cost.config(text="비용필터: 최신 데이터 대기")
                    self._clear_market_display()
            else:
                self.status.config(text=f"유효 데이터 대기 | 제외 {self.rejected_rows}행")
                self.decision.config(text="유효 데이터 없음 — 신호 차단")
                self.flow.config(text="호가잔량 압력: 데이터 대기")
                self.basis.config(text="수동 적정가 비교: 데이터 대기")
                self.cost.config(text="비용필터: 데이터 대기")
                self._clear_market_display()
        except FileNotFoundError:
            if self.tail.path is not None or self.latest_row is not None:
                self._reset_analyzers()
                self.pending_rows.clear()
                self.tail = IncrementalCsvTail()
            self.status.config(text="오늘 실시간 데이터 대기 · Excel RTD와 수집기 연결 확인", fg=WARN)
            self.decision.config(text="오늘 데이터 없음 — 신호 차단", fg=WARN)
            self.flow.config(text="호가잔량 압력: 오늘 데이터 대기")
            self.basis.config(text="수동 적정가 비교: 오늘 데이터 대기")
            self.cost.config(text="비용필터: 오늘 데이터 대기")
            self.alert_status.config(text="오늘 데이터 대기 · 알림 중지", fg=MUTED)
            self._clear_market_display()
        except (OSError, KeyError, ValueError, TypeError, csv.Error) as exc:
            self._reset_analyzers()
            self.pending_rows.clear()
            self.status.config(text=f"데이터 오류: {exc}")
            self.decision.config(text="오류로 신호 차단 — 새 데이터로 재준비")
            self.flow.config(text="호가잔량 압력: 오류로 중단")
            self.basis.config(text="수동 적정가 비교: 오류로 중단")
            self.cost.config(text="비용필터: 오류로 중단")
            self._clear_market_display()
        self.after(25 if self.pending_rows or self.flow_history is not None else 500, self.refresh)

    def _clear_market_display(self) -> None:
        for label in self.labels.values():
            label.config(text="—")
        for label in (self.fast_rv, self.core_rv, self.market_spreads, self.foreign_3y, self.foreign_10y):
            label.config(text="유효 데이터 대기")
        self.curve_position.config(text="현물 RTD 데이터 대기")
        self.curve_trend.config(text="5·15분 변화: 기준 데이터 대기")
        self.relative_curve.config(text="현물 5년 금리 연결 대기")
        self.relative_basis.config(text="현선물 베이시스 데이터 대기")
        self.relative_execution.config(text="상대가치 통계·비용 계산 대기")
        self._render_overview(None, False)


class AlertEditor(tk.Toplevel):
    """Small non-technical editor for threshold alerts."""

    MODE_NAMES = {"이상": "above", "이하": "below", "구간": "between"}

    def __init__(self, dashboard: Dashboard) -> None:
        super().__init__(dashboard)
        self.dashboard = dashboard
        self.title("가격 알림 설정")
        self.geometry("800x480")
        self.minsize(760, 400)
        self.transient(dashboard)
        self.metric_name = tk.StringVar(value=next(iter(METRICS.values())))
        self.mode_name = tk.StringVar(value="이상")
        self.lower = tk.StringVar()
        self.upper = tk.StringVar()
        self.rules = list(dashboard.extra_settings.rules)
        # Reserve the footer before the expanding list, including at high DPI.
        footer = tk.Frame(self)
        footer.pack(side="bottom", fill="x", padx=12, pady=10)
        tk.Button(footer, text="저장·적용", command=self.save).pack(side="right", padx=4)
        tk.Button(footer, text="취소", command=self.destroy).pack(side="right", padx=4)
        tk.Label(footer, text="추가·삭제한 조건은 ‘저장·적용’을 눌러야 반영됩니다.",
                 fg="#555555").pack(side="left")
        form = tk.Frame(self); form.pack(fill="x", padx=12, pady=10)
        for column, (label, widget) in enumerate((
            ("대상", tk.OptionMenu(form, self.metric_name, *METRICS.values())),
            ("조건", tk.OptionMenu(form, self.mode_name, *self.MODE_NAMES)),
            ("기준/하단", tk.Entry(form, textvariable=self.lower, width=12)),
            ("상단(구간)", tk.Entry(form, textvariable=self.upper, width=12)),
        )):
            tk.Label(form, text=label).grid(row=0, column=column, padx=5)
            widget.grid(row=1, column=column, padx=5)
        tk.Button(form, text="추가", command=self.add_rule).grid(row=1, column=4, padx=8)
        toolbar = tk.Frame(self)
        toolbar.pack(fill="x", padx=12, pady=(0, 6))
        tk.Label(toolbar, text="등록된 알림 조건 · Ctrl/Shift로 여러 개 선택").pack(side="left")
        self.delete_button = tk.Button(toolbar, text="선택한 조건 삭제", command=self.delete_rule,
                                       state="disabled")
        self.delete_button.pack(side="right")
        self.listbox = tk.Listbox(self, height=8, font=("맑은 고딕", 10),
                                 selectmode=tk.EXTENDED, exportselection=False)
        self.listbox.pack(fill="both", expand=True, padx=12)
        self.listbox.bind("<<ListboxSelect>>", self.selection_changed)
        self.listbox.bind("<Delete>", self.delete_rule)
        self.refresh_list()

    def refresh_list(self) -> None:
        self.listbox.delete(0, tk.END)
        for rule in self.rules:
            upper = f" ~ {rule.upper:g}" if rule.mode == "between" else ""
            self.listbox.insert(tk.END, f"{rule.name}: {METRICS[rule.metric]} {rule.mode} {rule.lower:g}{upper}")
        self.selection_changed()

    def selection_changed(self, event=None) -> None:
        self.delete_button.config(state="normal" if self.listbox.curselection() else "disabled")

    def add_rule(self) -> None:
        try:
            metric = next(key for key, value in METRICS.items() if value == self.metric_name.get())
            mode = self.MODE_NAMES[self.mode_name.get()]
            lower = float(self.lower.get())
            upper = float(self.upper.get()) if self.upper.get().strip() else None
            hysteresis = .5 if metric in ("cash_spread_bp", "futures_yield_spread_bp") else .01
            name = f"{self.metric_name.get()} {self.mode_name.get()} {lower:g}"
            if mode == "between":
                name += f"~{upper:g}" if upper is not None else ""
            candidate = AlertRule(name=name, metric=metric, mode=mode, lower=lower, upper=upper,
                                  hysteresis=hysteresis, confirmation_seconds=1, cooldown_seconds=0)
            raw = json.loads(EXTRA_SETTINGS_PATH.read_text(encoding="utf-8"))
            raw["rules"] = [asdict(rule) for rule in (*self.rules, candidate)]
            validated = parse_settings(raw)
            self.rules = list(validated.rules)
            self.refresh_list()
        except (ValueError, StopIteration, TypeError) as exc:
            self.title(f"가격 알림 설정 — 입력 오류: {exc}")

    def delete_rule(self, event=None) -> str:
        selection = self.listbox.curselection()
        if selection:
            for index in sorted(selection, reverse=True):
                self.rules.pop(index)
            self.refresh_list()
        return "break"

    def save(self) -> None:
        raw = json.loads(EXTRA_SETTINGS_PATH.read_text(encoding="utf-8"))
        raw["rules"] = [asdict(rule) for rule in self.rules]
        parse_settings(raw)
        temporary = EXTRA_SETTINGS_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(EXTRA_SETTINGS_PATH)
        self.dashboard.reload_alerts()
        self.destroy()

if __name__ == "__main__": Dashboard().mainloop()
