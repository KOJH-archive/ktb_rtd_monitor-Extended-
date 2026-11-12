"""Time-weighted book observations. No trade-flow inference or order signals."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import math

from market_extensions import optional_number

KST = timezone(timedelta(hours=9))
DOMINANCE_BAND = .10


class _Totals:
    def __init__(self) -> None:
        self.seconds = 0.0
        # OBI, weighted OBI, spread, buy-dominant time, sell-dominant time.
        self.values = [0.0] * 5

    def add(self, rates: tuple[float, ...], seconds: float) -> None:
        self.seconds += seconds
        for index, rate in enumerate(rates):
            self.values[index] += rate * seconds

    def summary(self, expected_seconds: float) -> dict[str, float | bool | None]:
        seconds = max(0.0, self.seconds)
        if seconds < 1e-8:
            return {"observed_seconds": 0.0, "coverage": 0.0, "complete": False,
                    "mean_obi": None, "mean_weighted_obi": None, "mean_spread": None,
                    "buy_share": None, "sell_share": None, "neutral_share": None}
        buy = min(1.0, max(0.0, self.values[3] / seconds))
        sell = min(1.0 - buy, max(0.0, self.values[4] / seconds))
        return {"observed_seconds": seconds,
                "coverage": min(1.0, seconds / expected_seconds) if expected_seconds > 0 else 0.0,
                "complete": expected_seconds > 0 and seconds >= expected_seconds - 1e-6,
                "mean_obi": self.values[0] / seconds,
                "mean_weighted_obi": self.values[1] / seconds,
                "mean_spread": self.values[2] / seconds,
                "buy_share": buy, "sell_share": sell, "neutral_share": max(0.0, 1 - buy - sell)}


@dataclass(slots=True)
class _Segment:
    start: datetime
    end: datetime
    rates: tuple[float, ...]


class _Window:
    def __init__(self, minutes: int) -> None:
        self.minutes = minutes
        self.segments: deque[_Segment] = deque()
        self.totals = _Totals()

    def add(self, start: datetime, end: datetime, rates: tuple[float, ...]) -> None:
        if self.segments and self.segments[-1].end == start and self.segments[-1].rates == rates:
            self.segments[-1].end = end
        else:
            self.segments.append(_Segment(start, end, rates))
        self.totals.add(rates, (end - start).total_seconds())

    def advance(self, timestamp: datetime) -> None:
        cutoff = timestamp - timedelta(minutes=self.minutes)
        while self.segments and self.segments[0].start < cutoff:
            segment = self.segments[0]
            end = min(segment.end, cutoff)
            self.totals.add(segment.rates, -(end - segment.start).total_seconds())
            if segment.end <= cutoff:
                self.segments.popleft()
            else:
                segment.start = cutoff
                break
        if not self.segments:
            self.totals = _Totals()


class BookPressureTracker:
    """Integrate the prior observed book until the next valid observation.

    No extension to wall time, and no integration across invalid data, reversed
    timestamps, session/contract changes or gaps exceeding max_gap_seconds.
    """

    def __init__(self, contract: str, max_gap_seconds: float = 10.0) -> None:
        if not math.isfinite(max_gap_seconds) or max_gap_seconds <= 0:
            raise ValueError("max_gap_seconds must be positive and finite")
        self.contract = contract
        self.max_gap_seconds = max_gap_seconds
        self.reset()

    def reset(self) -> None:
        self.windows = {minutes: _Window(minutes) for minutes in (5, 15, 60)}
        self.day = _Totals()
        self.identity = None
        self.started: datetime | None = None
        self.timestamp: datetime | None = None
        self.previous: tuple[datetime, tuple[float, ...]] | None = None
        self.active = False
        self.clear_ofi()

    def clear_ofi(self) -> None:
        self.ofi_previous = None
        self.ofi_events = deque()
        self.ofi_total = 0.0
        self.ofi_started = None
        self.ofi_alignment = None

    def pause(self) -> None:
        self.previous = None
        self.active = False
        self.clear_ofi()

    def update_ofi(self, row, stamp, weighted):
        fields = ("best_bid", "best_ask", "bid1_qty", "ask1_qty")
        quote = tuple(optional_number(row.get(f"{self.contract}_{field}")) for field in fields)
        if any(value is None or value <= 0 for value in quote) or quote[0] >= quote[1]:
            self.clear_ofi()
            return
        if self.ofi_previous is not None:
            previous_stamp, (old_bid, old_ask, old_bq, old_aq) = self.ofi_previous
            if (stamp - previous_stamp).total_seconds() > self.max_gap_seconds:
                self.clear_ofi()
            else:
                bid, ask, bq, aq = quote
                change = ((bq if bid >= old_bid else 0) - (old_bq if bid <= old_bid else 0)
                          - (aq if ask <= old_ask else 0) + (old_aq if ask >= old_ask else 0))
                self.ofi_events.append((stamp, change))
                self.ofi_total += change
        if self.ofi_started is None:
            self.ofi_started = stamp
        cutoff = stamp - timedelta(seconds=60)
        while self.ofi_events and self.ofi_events[0][0] <= cutoff:
            self.ofi_total -= self.ofi_events.popleft()[1]
        self.ofi_previous = stamp, quote
        if self.ofi_started <= cutoff:
            if abs(weighted) < DOMINANCE_BAND or abs(self.ofi_total) < 1e-9:
                self.ofi_alignment = "중립"
            else:
                self.ofi_alignment = "방향 일치" if weighted * self.ofi_total > 0 else "방향 엇갈림"

    def update(self, row: dict[str, object], timestamp: datetime | None = None) -> None:
        try:
            stamp = timestamp or datetime.fromisoformat(str(row["timestamp_utc"]))
            if stamp.tzinfo is None:
                raise ValueError("Timezone required")
            stamp = stamp.astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError, OverflowError):
            self.pause()
            return
        if self.timestamp is not None and stamp <= self.timestamp:
            self.pause()
            return
        identity = (stamp.astimezone(KST).date(), row.get("session_id") or "",
                    row.get(f"{self.contract}_contract") or "")
        if self.identity is not None and identity != self.identity:
            self.reset()
        self.identity = identity
        self.timestamp = stamp
        for window in self.windows.values():
            window.advance(stamp)

        keys = ("obi", "weighted_obi", "spread", "bid_depth", "ask_depth")
        values = [optional_number(row.get(f"{self.contract}_{key}")) for key in keys]
        if any(value is None for value in values):
            self.pause()
            return
        obi, weighted, spread, bid, ask = values
        if abs(obi) > 1 or abs(weighted) > 1 or spread <= 0 or min(bid, ask) < 0 or bid + ask <= 0:
            self.pause()
            return
        rates = (obi, weighted, spread, float(obi >= DOMINANCE_BAND), float(obi <= -DOMINANCE_BAND))
        if self.started is None:
            self.started = stamp
        if self.previous is not None:
            previous_time, previous_rates = self.previous
            seconds = (stamp - previous_time).total_seconds()
            if seconds <= self.max_gap_seconds:
                self.day.add(previous_rates, seconds)
                for window in self.windows.values():
                    window.add(previous_time, stamp, previous_rates)
                    window.advance(stamp)
        self.previous = (stamp, rates)
        self.active = True
        self.update_ofi(row, stamp, weighted)

    def summary(self) -> dict[str, object]:
        elapsed = ((self.timestamp - self.started).total_seconds()
                   if self.timestamp is not None and self.started is not None else 0.0)
        return {"timestamp": self.timestamp, "started": self.started, "active": self.active,
                "day": self.day.summary(elapsed),
                "ofi": self.ofi_total if self.ofi_alignment is not None else None,
                "ofi_alignment": self.ofi_alignment,
                "current_weighted_obi": self.previous[1][1] if self.previous else None,
                "windows": {minutes: window.totals.summary(minutes * 60)
                            for minutes, window in self.windows.items()}}


def book_pressure_text(summary: dict[str, object], fresh: bool) -> tuple[str, str, str]:
    """Return headline, evidence and tone; completeness is actual time coverage."""
    stamp = summary["timestamp"]
    if stamp is None:
        return "시간 누적 관측 대기", "유효한 연속 호가가 들어오면 집계합니다.", "muted"
    as_of = stamp.astimezone(KST).strftime("%H:%M:%S")
    five = summary["windows"][5]
    tone = "muted"
    if not fresh or not summary["active"]:
        headline = f"최신 관측 대기 · 마지막 집계 {as_of}"
    elif not five["complete"]:
        headline = f"5분 관측 {five['observed_seconds']:.1f}/300초 · 공백 제외"
    else:
        mean = five["mean_obi"]
        state = "매수잔량 우위" if mean >= DOMINANCE_BAND else "매도잔량 우위" if mean <= -DOMINANCE_BAND else "잔량 균형권"
        headline = f"5분 평균: {state} · OBI {mean:+.3f}"
        tone = "buy" if mean >= DOMINANCE_BAND else "sell" if mean <= -DOMINANCE_BAND else "muted"
    lines = []
    for label, item in [(f"{minutes}분", summary["windows"][minutes]) for minutes in (5, 15, 60)] + [("당일", summary["day"])]:
        if item["mean_obi"] is None:
            lines.append(f"{label}: 유효 관측 대기")
            continue
        coverage = item["coverage"] * 100
        status = ("전체" if item["complete"] else "부분") if label != "당일" else "관측 시작 이후"
        lines.append(f"{label} [{status} · 관측 {coverage:.1f}%]  평균 OBI {item['mean_obi']:+.3f} / 가중 {item['mean_weighted_obi']:+.3f}\n"
                     f"우위시간 매수 {item['buy_share']:.0%} · 중립 {item['neutral_share']:.0%} · 매도 {item['sell_share']:.0%}"
                     f" | 평균 호가간격 {item['mean_spread']:.3f}pt")
    started = summary["started"]
    if started is not None:
        lines.append(f"당일 기록 {started.astimezone(KST):%H:%M:%S}~{as_of} · 유효 {summary['day']['observed_seconds']/60:.1f}분")
    ofi = summary.get("ofi") if fresh else None
    ofi_text = (f"60초 관측 OFI {ofi:+,.0f}계약 · 현재 가중 OBI와 {summary['ofi_alignment']}"
                if ofi is not None else "60초 관측 OFI · 연속 호가 대기")
    if ofi is not None:
        weighted = summary["current_weighted_obi"]
        book_side = "매수 잔량 우세" if weighted >= DOMINANCE_BAND else (
            "매도 잔량 우세" if weighted <= -DOMINANCE_BAND else "잔량 균형권")
        flow_side = "매수 쪽 변화 우세" if ofi > 1e-9 else (
            "매도 쪽 변화 우세" if ofi < -1e-9 else "양쪽 변화 균형")
        ofi_text = f"현재 {book_side} · 최근 60초 {flow_side}\n{ofi_text}"
    return headline, "\n\n".join([ofi_text, *lines]), tone
