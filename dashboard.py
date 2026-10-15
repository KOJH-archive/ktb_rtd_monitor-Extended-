from __future__ import annotations
import csv, json, math, os, tkinter as tk
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from analyze_snapshots import MinuteRVAnalyzer, RollingRVAnalyzer, timestamp_kst
from app_settings import AppSettings, effective_beta, load_settings
from decision_support import OrderFlowAnalyzer, basis_metrics, execution_gate, risk_metrics
from extra_settings import METRICS, AlertRule, load_extra_settings, parse_settings
from market_extensions import (AlertEngine, ForeignFlowTracker, WindowsNotifier,
                               market_metrics, metric_is_fresh, source_timestamp)
from signal_engine import SignalEngine

DATA_DIR = Path(__file__).parent / "data"
SETTINGS_PATH = Path(__file__).parent / "settings.json"
EXTRA_SETTINGS_PATH = Path(__file__).parent / "extra_settings.json"

def current_csv() -> Path:
    """Use the live trade-price log when it exists, otherwise the legacy log."""
    enhanced_csv = DATA_DIR / "snapshots_enhanced.csv"
    trade_csv = DATA_DIR / "snapshots_with_trades.csv"
    legacy_csv = DATA_DIR / "snapshots.csv"
    if enhanced_csv.exists() and enhanced_csv.stat().st_size > 0:
        return enhanced_csv
    if trade_csv.exists() and trade_csv.stat().st_size > 0:
        return trade_csv
    return legacy_csv


class IncrementalCsvTail:
    """Read only appended CSV rows after a bounded one-time warm-up."""

    def __init__(self, warmup_rows: int = 20_000) -> None:
        self.warmup_rows = warmup_rows
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
            # Same handle and fixed end offset: appends cannot race the warm-up.
            start = max(header_end, self.offset - 16 * 1024 * 1024)
            handle.seek(start)
            data = handle.read(self.offset - start)
            if start > header_end:
                data = data.partition(b"\n")[2]
        self.path = path
        self.header = next(csv.reader([header_line.decode("utf-8-sig").strip()]))
        lines = data.split(b"\n")
        self.pending = lines.pop()
        return self._parse([line.rstrip(b"\r") for line in lines[-self.warmup_rows:]])

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


def numeric_row(row: dict[str, str]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for key, value in row.items():
        try:
            parsed[key] = value if key == "timestamp_utc" else float(value)
        except (TypeError, ValueError):
            parsed[key] = value
    return parsed

class Dashboard(tk.Tk):
    def __init__(self):
        super().__init__(); self.title("KTB RTD Dashboard"); self.geometry("1120x880"); self.configure(bg="#101820")
        self.settings: AppSettings = load_settings(SETTINGS_PATH)
        self.extra_settings = load_extra_settings(EXTRA_SETTINGS_PATH)
        self.tail = IncrementalCsvTail()
        self.fast_analyzer: RollingRVAnalyzer
        self.core_analyzer: MinuteRVAnalyzer
        self._reset_analyzers()
        self.rejected_rows = 0
        self.decision_text = "중립/관찰"
        self.latest_row: dict[str, object] | None = None
        self.fast_result: dict[str, object] | None = None
        self.core_result: dict[str, object] | None = None
        self.flow_result: dict[str, object] | None = None
        self.labels = {}
        tk.Label(self, text="KTB RTD Dashboard  |  observation only", fg="white", bg="#101820", font=("Segoe UI", 16, "bold")).pack(pady=12)
        self.status = tk.Label(self, text="Waiting for snapshots...", fg="#9fb3c8", bg="#101820", font=("Consolas", 10)); self.status.pack()
        grid = tk.Frame(self, bg="#101820"); grid.pack(pady=18)
        for c, title in enumerate(("Metric", "3Y", "10Y", "Interpretation")): tk.Label(grid, text=title, width=24, fg="#8ed1fc", bg="#1b2a38", font=("Segoe UI", 11, "bold")).grid(row=0, column=c, padx=2, pady=2)
        for r, key, title in ((1,"mid","Mid"),(2,"spread","Spread"),(3,"obi","OBI"),(4,"weighted_obi","WOBI"),(5,"microprice","Microprice"),(6,"bid_depth","Bid depth"),(7,"ask_depth","Ask depth")):
            tk.Label(grid, text=title, width=24, fg="white", bg="#101820", anchor="w").grid(row=r,column=0,padx=2,pady=3)
            for c, name in enumerate(("3y","10y"),1):
                label=tk.Label(grid,text="-",width=24,fg="#e8f1f2",bg="#243746",font=("Consolas",11)); label.grid(row=r,column=c,padx=2,pady=3); self.labels[(name,key)]=label
            help_text = {"mid":"현재 최우선 호가의 단순 중간값", "spread":"매수-매도 간격; 작을수록 유동성 양호", "obi":"5단계 전체 잔량: + 매수 우세 / - 매도 우세", "weighted_obi":"WOBI: 가까운 호가에 높은 가중치; + 매수 압력 / - 매도 압력", "microprice":"최우선 호가 잔량으로 가중한 예상 중심가격", "bid_depth":"5단계 매수 총잔량", "ask_depth":"5단계 매도 총잔량"}[key]
            tk.Label(grid, text=help_text, width=38, fg="#b8c7d1", bg="#101820", anchor="w", wraplength=270, justify="left").grid(row=r,column=3,padx=4,pady=3)
        self.fast_rv = tk.Label(self, text="빠른 RV: -", fg="#ffd166", bg="#101820", font=("Consolas", 12)); self.fast_rv.pack(pady=(10, 2))
        self.core_rv = tk.Label(self, text="주 RV: -", fg="#8ed1fc", bg="#101820", font=("Consolas", 12, "bold")); self.core_rv.pack(pady=2)
        self.decision = tk.Label(self, text="종합 판단: 데이터 대기", fg="white", bg="#243746", font=("Segoe UI", 12, "bold"), width=100); self.decision.pack(pady=8)
        self.flow = tk.Label(self, text="호가잔량 압력: 데이터 대기", fg="#d6e6f2", bg="#101820", font=("Segoe UI", 10)); self.flow.pack(pady=2)
        self.basis = tk.Label(self, text="수동 적정가 비교: 입력 대기", fg="#d6e6f2", bg="#101820", font=("Segoe UI", 10)); self.basis.pack(pady=2)
        self.cost = tk.Label(self, text="체결비용: 신호 대기", fg="#d6e6f2", bg="#101820", font=("Segoe UI", 10)); self.cost.pack(pady=2)
        self.risk = tk.Label(self, text=self._risk_text(), fg="#b8c7d1", bg="#101820", font=("Segoe UI", 10)); self.risk.pack(pady=2)
        self.market_spreads = tk.Label(self, text="체결가차: 추가 RTD 대기", fg="#ffe08a", bg="#101820",
                                       font=("Segoe UI", 11, "bold")); self.market_spreads.pack(pady=(10, 2))
        self.foreign_3y = tk.Label(self, text="3Y 외국인 관측 가격대: 추가 RTD 대기", fg="#c9d8e4", bg="#101820",
                                  font=("Segoe UI", 10)); self.foreign_3y.pack(pady=2)
        self.foreign_10y = tk.Label(self, text="10Y 외국인 관측 가격대: 추가 RTD 대기", fg="#c9d8e4", bg="#101820",
                                   font=("Segoe UI", 10)); self.foreign_10y.pack(pady=2)
        alert_frame = tk.Frame(self, bg="#101820"); alert_frame.pack(pady=8)
        tk.Button(alert_frame, text="가격 알림 설정", command=self.open_alert_editor,
                  bg="#31495d", fg="white", activebackground="#46647b").pack(side="left", padx=5)
        self.alert_status = tk.Label(alert_frame, text=self._alert_summary(), fg="#8ed1fc", bg="#101820",
                                     font=("Segoe UI", 10)); self.alert_status.pack(side="left", padx=8)
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

    def _alert_summary(self) -> str:
        enabled = sum(rule.enabled for rule in self.extra_settings.rules)
        return f"알림 {enabled}개 | 조건 미설정" if not enabled else f"알림 {enabled}개 감시 중"

    def open_alert_editor(self) -> None:
        AlertEditor(self)

    def reload_alerts(self) -> None:
        self.extra_settings = load_extra_settings(EXTRA_SETTINGS_PATH)
        self.alert_engine = AlertEngine(self.extra_settings.rules)
        self.notifier = WindowsNotifier(self.extra_settings)
        self.alert_status.config(text=self._alert_summary())

    def _risk_text(self) -> str:
        metrics = risk_metrics(self.settings.risk)
        if metrics is None:
            return (f"DV01: 미설정 | 연구 beta {self.settings.rv.beta:.4f} 사용 | "
                    "settings.json에 계약별 원/bp 값을 입력하면 DV01 beta로 전환합니다.")
        return (f"DV01 헤지: 10Y {metrics['contracts_10y']}계약당 3Y {metrics['exact_3y']:.2f}계약 "
                f"| 정수 패키지 {metrics['rounded_3y']}:{metrics['contracts_10y']} "
                f"| 총 {metrics['gross_dv01']:,.0f}원/bp | 잔여 {metrics['residual_dv01']:,.0f}원/bp "
                f"| {metrics['status']} | 분석 beta {effective_beta(self.settings):.4f}")

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
        if "cash_spread_bp" in metrics:
            parts.append(f"현물 체결수익률 10Y−3Y {metrics['cash_spread_bp']:+.1f}bp")
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
        return prefix + (" | " + " | ".join(parts) if parts else " | 기준점 설정 중") + " | 실제 평균체결가 아님"

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
            rows, reset = self.tail.read_new(csv_path)
            if reset:
                self._reset_analyzers()
            alert_events = []
            for raw in rows:
                try:
                    row = numeric_row(raw)
                    # Validate display-only metrics as well as shared quotes.
                    for name in ("3y", "10y"):
                        for key in ("microprice", "obi", "weighted_obi", "bid_depth", "ask_depth"):
                            if not math.isfinite(float(row[f"{name}_{key}"])):
                                raise ValueError("Non-finite display metric")
                    result = self.engine.update(row)
                except (KeyError, ValueError, TypeError):
                    self.rejected_rows += 1
                    self._reset_analyzers()
                    continue
                if result.reset_reason:
                    self.flow_analyzer.reset()
                self.signal_result = result
                self.latest_row = row
                self.fast_result, self.core_result = result.fast, result.core
                self.flow_result = self.flow_analyzer.update(row)
                self.decision_text = result.text
                timestamp = datetime.fromisoformat(str(row["timestamp_utc"]))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                session = row.get("session_id") or timestamp_kst(timestamp).date()
                for name, tracker in self.foreign_trackers.items():
                    source_time = source_timestamp(row.get(f"{name}_foreign_time"), timestamp)
                    if (source_time is None or
                            abs((timestamp.astimezone(timezone.utc) - source_time).total_seconds())
                            <= self.extra_settings.max_source_age_seconds):
                        tracker.update(row, timestamp, session)
                age = (datetime.now(timezone.utc) - timestamp.astimezone(timezone.utc)).total_seconds()
                if not reset:
                    metrics = market_metrics(row)
                    freshness = {metric: 0 <= age <= self.extra_settings.stale_seconds
                                 and metric_is_fresh(row, metric, timestamp, self.extra_settings)
                                 for metric in metrics}
                    alert_events.extend(self.alert_engine.update(
                        metrics, timestamp, freshness))
            if alert_events:
                last = alert_events[-1]
                self.notifier.notify(self.winfo_id())
                self.alert_status.config(text=f"알림: {last.name} | {last.value:+.3f}", fg="#ffd166")
            if self.latest_row:
                row = self.latest_row
                timestamp = datetime.fromisoformat(str(row["timestamp_utc"]))
                age = (datetime.now(timezone.utc) - timestamp).total_seconds()
                fresh = 0 <= age <= self.settings.data.stale_seconds
                health = "수집시각 최신 (거래소 피드 신선도 미검증)" if fresh else f"과거/미래 시각 {age:.0f}초 — 신호 차단"
                self.status.config(text=f"UTC {row['timestamp_utc']} | {health} | 제외 {self.rejected_rows}행 | {csv_path.name}")
                for name in ("3y","10y"):
                    for key in ("mid","spread","obi","weighted_obi","microprice","bid_depth","ask_depth"):
                        value=float(row[f"{name}_{key}"]); text=f"{value:.4f}" if key in ("mid","microprice") else (f"{value:+.3f}" if "obi" in key else f"{value:.3f}")
                        self.labels[(name,key)].config(text=text)
                rv = self.settings.rv
                self.fast_rv.config(text=self._rv_text(f"빠른 RV ({rv.fast_event_window} 이벤트)", self.fast_result,
                                                       rv.fast_event_window, "이벤트"))
                self.core_rv.config(text=self._rv_text(f"주 RV (완료 {rv.core_minutes}분)", self.core_result,
                                                       rv.core_minutes, "분"))
                self.decision.config(text=self._decision_text() if fresh else "데이터 시각 이상/지연: 현재 신호 사용 금지")
                self.flow.config(text=self._flow_text())
                self.basis.config(text=self._basis_text(row))
                self.cost.config(text=self._cost_text(row))
                self.market_spreads.config(text=self._market_text(row))
                self.foreign_3y.config(text=self._foreign_text("3y", row))
                self.foreign_10y.config(text=self._foreign_text("10y", row))
            else:
                self.status.config(text=f"유효 데이터 대기 | 제외 {self.rejected_rows}행")
                self.decision.config(text="유효 데이터 없음 — 신호 차단")
                self.flow.config(text="호가잔량 압력: 데이터 대기")
                self.basis.config(text="수동 적정가 비교: 데이터 대기")
                self.cost.config(text="비용필터: 데이터 대기")
        except (OSError, KeyError, ValueError, TypeError, csv.Error) as exc:
            self._reset_analyzers()
            self.status.config(text=f"데이터 오류: {exc}")
            self.decision.config(text="오류로 신호 차단 — 새 데이터로 재준비")
            self.flow.config(text="호가잔량 압력: 오류로 중단")
            self.basis.config(text="수동 적정가 비교: 오류로 중단")
            self.cost.config(text="비용필터: 오류로 중단")
        self.after(500, self.refresh)


class AlertEditor(tk.Toplevel):
    """Small non-technical editor for threshold alerts."""

    MODE_NAMES = {"이상": "above", "이하": "below", "구간": "between"}

    def __init__(self, dashboard: Dashboard) -> None:
        super().__init__(dashboard)
        self.dashboard = dashboard
        self.title("가격 알림 설정")
        self.geometry("760x430")
        self.transient(dashboard)
        self.metric_name = tk.StringVar(value=next(iter(METRICS.values())))
        self.mode_name = tk.StringVar(value="이상")
        self.lower = tk.StringVar()
        self.upper = tk.StringVar()
        self.rules = list(dashboard.extra_settings.rules)
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
        self.listbox = tk.Listbox(self, height=12, font=("Consolas", 10)); self.listbox.pack(fill="both", expand=True, padx=12)
        buttons = tk.Frame(self); buttons.pack(pady=10)
        tk.Button(buttons, text="선택 삭제", command=self.delete_rule).pack(side="left", padx=4)
        tk.Button(buttons, text="저장", command=self.save).pack(side="left", padx=4)
        tk.Button(buttons, text="닫기", command=self.destroy).pack(side="left", padx=4)
        tk.Label(self, text="조건 진입 1초 후 1회 알림. 기준에서 1틱 이상 이탈해야 다시 무장됩니다.",
                 fg="#555555").pack(pady=(0, 8))
        self.refresh_list()

    def refresh_list(self) -> None:
        self.listbox.delete(0, tk.END)
        for rule in self.rules:
            upper = f" ~ {rule.upper:g}" if rule.mode == "between" else ""
            self.listbox.insert(tk.END, f"{rule.name}: {METRICS[rule.metric]} {rule.mode} {rule.lower:g}{upper}")

    def add_rule(self) -> None:
        try:
            metric = next(key for key, value in METRICS.items() if value == self.metric_name.get())
            mode = self.MODE_NAMES[self.mode_name.get()]
            lower = float(self.lower.get())
            upper = float(self.upper.get()) if self.upper.get().strip() else None
            hysteresis = .5 if metric == "cash_spread_bp" else .01
            name = f"{self.metric_name.get()} {self.mode_name.get()} {lower:g}"
            if mode == "between":
                name += f"~{upper:g}" if upper is not None else ""
            candidate = AlertRule(name=name, metric=metric, mode=mode, lower=lower, upper=upper,
                                  hysteresis=hysteresis, confirmation_seconds=1, cooldown_seconds=60)
            raw = json.loads(EXTRA_SETTINGS_PATH.read_text(encoding="utf-8"))
            raw["rules"] = [asdict(rule) for rule in (*self.rules, candidate)]
            validated = parse_settings(raw)
            self.rules = list(validated.rules)
            self.refresh_list()
        except (ValueError, StopIteration, TypeError) as exc:
            self.title(f"가격 알림 설정 — 입력 오류: {exc}")

    def delete_rule(self) -> None:
        selection = self.listbox.curselection()
        if selection:
            self.rules.pop(selection[0]); self.refresh_list()

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
