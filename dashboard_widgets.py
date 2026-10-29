"""Native Tk dashboard components; no browser, plotting or animation runtime."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from foreign_profile import ForeignPriceProfile

BG = "#eef2f6"
PANEL = "#ffffff"
INK = "#172b43"
MUTED = "#52657a"
LINE = "#dce4ec"
ACCENT = "#176b83"
WARN = "#9a5911"


class ValueLabel(tk.Label):
    """Avoid Tcl redraw/configure work when the displayed value is unchanged."""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self._displayed = dict(kwargs)

    def config(self, cnf=None, **kwargs):
        if cnf is not None:
            return super().configure(cnf, **kwargs)
        changed = {key: value for key, value in kwargs.items() if self._displayed.get(key) != value}
        if changed:
            super().configure(**changed)
            self._displayed.update(changed)


def text(parent, value="", size=10, color=INK, bold=False, **kwargs):
    label = ValueLabel(parent, text=value, bg=parent.cget("bg"), fg=color,
                       font=("맑은 고딕", size, "bold" if bold else "normal"),
                       anchor="w", justify="left", wraplength=500, **kwargs)
    # Follow the containing panel, not the label's own requested size. Binding
    # to the label creates a geometry feedback loop on shrink-to-fit controls.
    padding = parent.winfo_pixels(parent.cget("padx"))
    parent.bind("<Configure>", lambda event: label.config(
        wraplength=max(80, event.width - 2 * padding - 12)), add="+")
    return label


def panel(parent, title, subtitle=None):
    frame = tk.Frame(parent, bg=PANEL, highlightbackground=LINE, highlightthickness=1, padx=18, pady=12)
    text(frame, title, 11, bold=True).pack(fill="x")
    if subtitle:
        text(frame, subtitle, 9, MUTED).pack(fill="x", pady=(3, 8))
    return frame


def row_label(parent, value="", **kwargs):
    label = text(parent, value, **kwargs)
    label.pack(fill="x", pady=5)
    return label


def scroll_page(notebook, title):
    outer = tk.Frame(notebook, bg=BG)
    notebook.add(outer, text=title)
    canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
    scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
    scrollbar.pack(side="right", fill="y")
    canvas.pack(side="left", fill="both", expand=True)
    canvas.configure(yscrollcommand=scrollbar.set)
    page = tk.Frame(canvas, bg=BG, padx=1, pady=10)
    item = canvas.create_window((0, 0), window=page, anchor="nw")
    canvas.bind("<Configure>", lambda event: canvas.itemconfigure(item, width=event.width))
    page.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
    # Toplevel bind runs only for events belonging to this dashboard. Scope the
    # scroll to the page under the pointer, including labels nested inside it.
    def wheel(event):
        widget = event.widget
        while widget is not None:
            if widget is outer:
                if page.winfo_height() > canvas.winfo_height():
                    canvas.yview_scroll(-int(event.delta / 120), "units")
                break
            widget = getattr(widget, "master", None)
    notebook.winfo_toplevel().bind("<MouseWheel>", wheel, add="+")
    return page


def open_profile_window(view):
    popup = getattr(view, "foreign_profile_popup", None)
    if popup is not None and popup.winfo_exists():
        popup.lift()
        return
    popup = tk.Toplevel(view)
    popup.title("외국인 가격대 지도 · 당일 관측")
    popup.geometry("1100x640")
    popup.minsize(960, 560)
    popup.configure(bg=BG)
    view.foreign_profile_popup = popup
    body = tk.Frame(popup, bg=PANEL, padx=18, pady=14)
    body.pack(fill="both", expand=True, padx=12, pady=12)
    text(body, "외국인 가격대 지도 · 당일 관측 누적", 15, bold=True).pack(fill="x")
    controls = tk.Frame(body, bg=PANEL)
    controls.pack(fill="x", pady=(6, 10))
    for title, mode in (("매수·매도 관측량", "gross"), ("순매수 차이", "net")):
        tk.Radiobutton(controls, text=title, variable=view.profile_mode, value=mode,
                       bg=PANEL, selectcolor=PANEL, font=("맑은 고딕", 10)).pack(side="left", padx=(0, 12))
    text(body, "청록: 매수 · 주황: 매도 · 금색 점선: 현재가 · 마우스를 올리면 해당 가격대 수치가 표시됩니다.",
         9, MUTED).pack(fill="x")
    charts = tk.Frame(body, bg=PANEL)
    charts.pack(fill="both", expand=True, pady=8)
    charts.rowconfigure(0, weight=1)
    view.popup_foreign_profiles = {}
    for column, (name, title) in enumerate((("3y", "3년 선물"), ("10y", "10년 선물"))):
        charts.columnconfigure(column, weight=1, uniform="profile")
        chart = ForeignPriceProfile(charts, title)
        chart.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 12, 0))
        view.popup_foreign_profiles[name] = chart
    text(body, "관측량은 실제 외국인 체결가·잔고가 아닙니다. 최초 관측 이전 거래와 연결 공백은 가격대에 배정하지 않습니다.",
         9, MUTED).pack(fill="x")


def build_dashboard(view):
    view.title("KTB | 국채선물 모니터")
    view.geometry("1180x800")
    view.minsize(960, 720)
    view.configure(bg=BG)
    style = ttk.Style(view)
    style.theme_use("clam")
    style.configure("TNotebook", background=BG, borderwidth=0)
    style.configure("TNotebook.Tab", font=("맑은 고딕", 10), padding=(18, 9),
                    background="#e3eaf1", foreground=MUTED)
    style.map("TNotebook.Tab", background=[("selected", PANEL)], foreground=[("selected", ACCENT)])

    shell = tk.Frame(view, bg=BG, padx=22, pady=14)
    shell.pack(fill="both", expand=True)
    header = tk.Frame(shell, bg=BG)
    header.pack(fill="x")
    text(header, "KTB  /  국채선물 모니터", 19, bold=True).pack(side="left", fill="x", expand=True)
    tk.Button(header, text="가격 알림 설정", command=view.open_alert_editor,
              bg=ACCENT, fg="white", activebackground="#12566b", activeforeground="white",
              relief="flat", font=("맑은 고딕", 10), padx=16, pady=6, cursor="hand2").pack(side="right")
    view.status = text(shell, "수집 데이터 대기", 9, MUTED)
    view.status.pack(fill="x", pady=(6, 12))

    cards = tk.Frame(shell, bg=BG)
    cards.pack(fill="x")
    view.kpis = {}
    definitions = (("3y_trade_price", "3년 선물", "최근 체결가 · pt"),
                   ("10y_trade_price", "10년 선물", "최근 체결가 · pt"),
                   ("futures_yield_spread_bp", "선물 수익률 차", "10년 − 3년 · bp"),
                   ("cash_spread_bp", "현물 금리차", "10년 − 3년 · bp"))
    for column, (key, title, unit) in enumerate(definitions):
        cards.columnconfigure(column, weight=1, uniform="kpi")
        card = panel(cards, title)
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
        value = ValueLabel(card, text="—", bg=PANEL, fg=INK, anchor="w", font=("Segoe UI", 26, "bold"))
        value.pack(fill="x", pady=(5, 0))
        text(card, unit, 9, MUTED).pack(fill="x")
        view.kpis[key] = value

    briefs = tk.Frame(shell, bg=BG)
    briefs.pack(fill="x", pady=(10, 0))
    view.flow_briefs = {}
    for column, (name, title) in enumerate((("3y", "3년 선물 · 지금 수급과 가격"),
                                            ("10y", "10년 선물 · 지금 수급과 가격"))):
        briefs.columnconfigure(column, weight=1, uniform="brief")
        card = panel(briefs, title)
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 8, 0))
        headline = row_label(card, "수급·가격 데이터 대기", size=12, bold=True, color=MUTED)
        detail = row_label(card, "최근 5분 관측으로 요약합니다.", size=9, color=MUTED)
        view.flow_briefs[name] = (headline, detail)
    text(shell, "최근 5분 관측 요약 · 추정가는 외국인 실제 체결원가와 다릅니다 · 원천시각 미연결 시 수집시각 기준",
         9, MUTED).pack(fill="x", pady=(3, 0))

    view.decision = text(shell, "데이터 대기", 11, ACCENT, True, padx=12, pady=9)
    view.decision.pack(fill="x", pady=(10, 8))
    notebook = ttk.Notebook(shell)
    notebook.pack(fill="both", expand=True)
    view.notebook = notebook
    pages = []
    for title in ("금리차 위치", "상대가치", "외국인 수급", "변동성 · 위험", "호가 · 비용"):
        pages.append(scroll_page(notebook, title))
    curve_page, relative_page, foreign_page, risk_page, book_page = pages

    curve = panel(curve_page, "현물 10년 − 3년 금리차", "최근 체결수익률 차이 · bp · 수집시각 기준")
    curve.pack(fill="x")
    view.curve_position = row_label(curve, "현물 RTD 데이터 대기", size=11, color=ACCENT, bold=True)
    view.curve_trend = row_label(curve, "5·15분 변화: 기준 데이터 대기", size=10)
    row_label(curve, "5분·60분 위치는 완료된 각 분의 마지막 관측값과 비교합니다.\n"
              "추세는 최근 변화 방향만 표시하며, 미래 방향이나 적정가를 예측하지 않습니다.",
              size=9, color=MUTED)

    relative_curve = panel(relative_page, "01  현물 3−5−10년 곡률",
                           "5년 현물은 기준점이며 거래 가능한 5년 선물로 간주하지 않습니다.")
    relative_curve.pack(fill="x")
    view.relative_curve = row_label(relative_curve, "현물 5년 금리 연결 대기",
                                    size=11, color=ACCENT, bold=True)
    row_label(relative_curve, "양수는 5년 금리가 3년·10년 선형보간보다 높은 상태입니다.\n"
              "곡률은 상대가치 설명변수이며 독립적인 수익 예측이 아닙니다.", size=9, color=MUTED)
    relative_basis = panel(relative_page, "02  3년·10년 현선물 교차 베이시스",
                           "선물수익률 − 현물수익률 · bp")
    relative_basis.pack(fill="x", pady=8)
    view.relative_basis = row_label(relative_basis, "현선물 베이시스 데이터 대기",
                                    size=11, bold=True)
    relative_stats = panel(relative_page, "03  평균회귀 관측 · 실행비용",
                           "완료 분봉 통계와 현재 최우선 호가 간격 기준")
    relative_stats.pack(fill="x")
    view.relative_execution = row_label(relative_stats, "상대가치 통계·비용 계산 대기", size=10)
    row_label(relative_stats, "반감기는 최근 관측의 AR(1) 적합값이며 구조적 수렴을 보장하지 않습니다.\n"
              "손익분기 bp는 대칭적인 커브 변화와 양쪽 시장가 왕복을 가정한 연구용 근사치입니다.",
              size=9, color=MUTED)

    risk_page.columnconfigure(0, weight=1, uniform="risk")
    risk_page.columnconfigure(1, weight=1, uniform="risk")
    vol = panel(risk_page, "01  변동성 관측", "완료된 1분 가격 변화 기준")
    vol.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
    view.volatility = row_label(vol, "준비 중", size=13, color=ACCENT, bold=True)
    row_label(vol, "합성 RV = 10Y mid − β × 3Y mid\n가격 수준의 Z-score와 별도로 계산합니다.", size=9, color=MUTED)
    sizing = panel(risk_page, "02  위험예산 · 수량", "현재 구현 상태")
    sizing.grid(row=0, column=1, sticky="nsew")
    view.sizing = row_label(sizing, "일별 현물금리·위험예산 입력 대기", size=11, color=WARN, bold=True)
    row_label(sizing, "현물금리 변화와 입력 DV01로 만든 연구용 손익 대용치입니다.\n"
              "선물 실현손익·운용 권장 수량으로 사용하지 않습니다.", size=9, color=MUTED)
    hedge = panel(risk_page, "03  DV01 헤지 · 한도")
    hedge.grid(row=1, column=0, columnspan=2, sticky="ew", pady=8)
    view.risk = row_label(hedge, view._risk_text(), size=10)
    signal = panel(risk_page, "04  상대가격 이탈 관측", "빠른 창은 이벤트 수, 주 창은 완료된 분 수입니다.")
    signal.grid(row=2, column=0, columnspan=2, sticky="ew")
    view.fast_rv = row_label(signal, "빠른 RV: 대기", size=10)
    view.core_rv = row_label(signal, "주 RV: 대기", size=10, bold=True)
    view.window_note = row_label(signal, "60분 기준은 장중 관측용이며 수일·수주 적정가 기준은 없습니다.", size=9, color=MUTED)

    profile = panel(foreign_page, "외국인 가격대별 관측 분포 · 당일 누적",
                    "매수·매도 증가량을 관측 당시 선물 체결가에 배정합니다. 막대가 길수록 해당 가격대의 관측량이 많습니다.")
    profile.pack(fill="x", pady=(0, 8))
    controls = tk.Frame(profile, bg=PANEL)
    controls.pack(fill="x", pady=(0, 6))
    view.profile_mode = tk.StringVar(value="gross")
    view.foreign_profile_popup = None
    view.popup_foreign_profiles = {}
    for title, mode in (("매수·매도 관측량", "gross"), ("순매수 차이", "net")):
        tk.Radiobutton(controls, text=title, variable=view.profile_mode, value=mode,
                       bg=PANEL, fg=INK, selectcolor=PANEL, activebackground=PANEL,
                       font=("맑은 고딕", 9), cursor="hand2").pack(side="left", padx=(0, 12))
    tk.Button(controls, text="크게 보기", command=lambda: open_profile_window(view),
              bg=ACCENT, fg="white", relief="flat", font=("맑은 고딕", 9),
              padx=12, pady=3, cursor="hand2").pack(side="right")
    charts = tk.Frame(profile, bg=PANEL)
    charts.pack(fill="x")
    view.foreign_profiles = {}
    for column, (name, title) in enumerate((("3y", "3년 선물"), ("10y", "10년 선물"))):
        charts.columnconfigure(column, weight=1, uniform="profile")
        chart = ForeignPriceProfile(charts, title)
        chart.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 12, 0))
        view.foreign_profiles[name] = chart
    row_label(profile, "청록: 매수 / 주황: 매도 · 가격 구간은 하단 이상~상단 미만입니다. 넓은 가격 범위는 구간을 합쳐 전체를 표시합니다.\n"
              "순매수는 관측 매수−매도이며 실제 보유잔고가 아닙니다. 최초 관측 이전 거래·연결 공백은 가격대에 배정하지 않습니다.",
              size=9, color=MUTED)

    flow = panel(foreign_page, "외국인 관측 가격대 · 수치 상세", "누적 매수·매도 증가량에 당시 체결가를 결합한 추정치입니다.")
    flow.pack(fill="x")
    view.foreign_3y = row_label(flow, "3Y 외국인: 데이터 대기", size=11)
    tk.Frame(flow, bg=LINE, height=1).pack(fill="x", pady=10)
    view.foreign_10y = row_label(flow, "10Y 외국인: 데이터 대기", size=11)
    row_label(flow, "관측 시작 이전 거래는 평균에 포함되지 않습니다. 외국인별 체결·잔고 원천이 없어\n"
              "실제 평균체결가·보유원가·미결제 포지션은 산출할 수 없습니다.", size=9, color=MUTED)
    market = panel(foreign_page, "데이터 연결 · 가격차")
    market.pack(fill="x", pady=10)
    view.market_spreads = row_label(market, "추가 RTD 대기", size=10)

    history = panel(book_page, "호가 누적 관측 · 시간 기준",
                    "인접 관측 사이에는 직전 호가가 유지됐다고 가정해 시간을 가중합니다. 연결 공백은 제외합니다.")
    history.pack(fill="x", pady=(0, 8))
    columns = tk.Frame(history, bg=PANEL)
    columns.pack(fill="x")
    view.book_history = {}
    for column, (name, title) in enumerate((("3y", "3년 선물"), ("10y", "10년 선물"))):
        columns.columnconfigure(column, weight=1, uniform="book_history")
        contract = tk.Frame(columns, bg=PANEL, padx=6)
        contract.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 10, 0))
        row_label(contract, title, bold=True, color=ACCENT)
        headline = row_label(contract, "시간 누적 관측 대기", size=10, bold=True)
        detail = row_label(contract, "연속 호가 수집 중", size=9)
        view.book_history[name] = (headline, detail)
    row_label(history, "OBI = (5단계 매수잔량 − 매도잔량) / 합계. 우위시간은 OBI ≥ +0.10 / ≤ −0.10 기준입니다.\n"
              "비율은 유효 관측시간 기준이며, 관측률은 해당 창 중 집계 가능한 시간입니다. 당일은 기록 시작 이후 기준입니다.\n"
              "잔량은 취소될 수 있습니다. 체결수급·외국인 수급과 다르며 ±0.10은 표시 구분선입니다.", size=9, color=MUTED)

    book = panel(book_page, "현재 호가 상세 · 순간값", "위 누적 관측과 아래 현재 호가를 함께 비교합니다.")
    book.pack(fill="x")
    view.labels = {}
    for column, name in enumerate(("항목", "3년", "10년", "의미")):
        book.columnconfigure(column, weight=1 if column < 3 else 2)
    # Inner table keeps grid and pack geometry managers in separate containers.
    grid = tk.Frame(book, bg=PANEL)
    grid.pack(fill="x")
    for column, name in enumerate(("항목", "3년", "10년", "의미")):
        grid.columnconfigure(column, weight=1 if column < 3 else 2)
        text(grid, name, 9, MUTED, True).grid(row=0, column=column, sticky="ew", pady=(0, 6))
    entries = (("mid", "중간 호가", "최우선 매수·매도 중간값"),
               ("spread", "호가 간격", "최우선 매도 − 매수 · pt"),
               ("obi", "잔량 불균형", "+ 매수잔량 우위 / − 매도잔량 우위"),
               ("weighted_obi", "가중 불균형", "가까운 호가의 잔량에 높은 가중치"),
               ("microprice", "잔량 가중가격", "최우선 호가 잔량 기반 가격"),
               ("bid_depth", "매수 총잔량", "5단계 합계 · 계약"),
               ("ask_depth", "매도 총잔량", "5단계 합계 · 계약"))
    for index, (key, title, hint) in enumerate(entries, 1):
        text(grid, title, 10).grid(row=index, column=0, sticky="ew", pady=3)
        for column, name in enumerate(("3y", "10y"), 1):
            label = ValueLabel(grid, text="—", bg=PANEL, fg=INK, anchor="e", font=("Consolas", 11))
            label.grid(row=index, column=column, sticky="ew", padx=(0, 20), pady=3)
            view.labels[(name, key)] = label
        text(grid, hint, 9, MUTED).grid(row=index, column=3, sticky="ew")
    detail = panel(book_page, "호가 압력 · 비용 · 수동 적정가")
    detail.pack(fill="x", pady=(8, 0))
    view.flow = row_label(detail, "호가 압력: 대기", size=9)
    view.cost = row_label(detail, "비용필터: 대기", size=9)
    view.basis = row_label(detail, "수동 적정가: 미설정", size=9)

    footer = tk.Frame(shell, bg=BG)
    footer.pack(fill="x", pady=(8, 0))
    view.alert_status = text(footer, view._alert_summary(), 9, ACCENT)
    view.alert_status.pack(side="left", fill="x", expand=True)
    text(footer, "의사결정 지원  ·  자동 주문 없음", 9, MUTED).pack(side="right")
