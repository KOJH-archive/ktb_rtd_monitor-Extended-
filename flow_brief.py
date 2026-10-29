"""Plain-language descriptions of observed foreign flow, with no new signals."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from extra_settings import ExtraSettings
from market_extensions import (metric_is_fresh, optional_number,
                               positive_optional_number, source_timestamp)


@dataclass(frozen=True)
class FlowBrief:
    headline: str
    detail: str
    tone: str = "muted"


def _price_zone(label: str, value: object, price: float) -> str:
    average = positive_optional_number(value)
    if average is None:
        return f"{label} 추정가 —"
    delta = price - average
    position = "위" if delta >= .0005 else "아래" if delta <= -.0005 else "부근"
    return f"{label} 추정 {average:.3f} (현재 {position})"


def foreign_flow_brief(name: str, row: dict[str, object] | None,
                       summary: dict[str, object], settings: ExtraSettings,
                       fresh: bool) -> FlowBrief:
    """Use the existing continuous 5-minute window; never extrapolate gaps."""
    if row is None:
        return FlowBrief("수급·가격 데이터 대기", "실시간 관측이 들어오면 요약합니다.")
    if not fresh:
        return FlowBrief("최신 데이터 대기", "연결 지연·기록 복원 중에는 현재 상황을 요약하지 않습니다.")
    buy = optional_number(row.get(f"{name}_foreign_buy"))
    sell = optional_number(row.get(f"{name}_foreign_sell"))
    price = positive_optional_number(row.get(f"{name}_trade_price"))
    if buy is None or sell is None or min(buy, sell) < 0:
        return FlowBrief("외국인 수급 미연결", "누적 매수·매도 수량이 들어오면 요약합니다.")
    if price is None:
        return FlowBrief("선물 체결가 대기", f"당일 누적 순매수 {buy-sell:+,.0f}계약 · 유효 체결가 필요")
    try:
        stamp = datetime.fromisoformat(str(row["timestamp_utc"]))
        if stamp.tzinfo is None:
            raise ValueError("Missing timestamp timezone")
        source = row.get(f"{name}_foreign_time")
        source_time = source_timestamp(source, stamp)
        flow_fresh = source in (None, "") or (source_time is not None and
            abs((stamp.astimezone(timezone.utc) - source_time).total_seconds())
            <= settings.max_source_age_seconds)
        price_fresh = metric_is_fresh(row, f"{name}_trade_price", stamp, settings)
    except (KeyError, ValueError, TypeError, OverflowError):
        flow_fresh = price_fresh = False
    if not flow_fresh or not price_fresh:
        return FlowBrief("수급·체결 원천시각 확인 필요", "원천시각이 지연되거나 유효하지 않아 요약을 보류합니다.")

    current = f"현재 {price:.3f}pt"
    daily = f"당일 누적 순매수 {buy-sell:+,.0f}계약"
    five = summary.get("windows", {}).get(5)
    if not five:
        return FlowBrief("최근 5분 관측 준비 중", f"{current} · {daily}\n연속 관측을 시작하면 수급과 가격 변화를 표시합니다.")

    complete = five["complete"]
    # After a gap, the tracker retains previously observed flow in the rolling
    # window. Label it as a partial 5-minute window, not as flow since resuming.
    period = "5분" if complete else "5분창(부분)"
    net = float(five["net_volume"])
    change = optional_number(five["price_change"])
    flow = "외국인 순매수" if net > 1e-9 else "외국인 순매도" if net < -1e-9 else "외국인 수급 균형"
    movement = ("가격 변화 대기" if change is None else
                "가격 상승" if change > 1e-9 else "가격 하락" if change < -1e-9 else "가격 보합")
    headline = f"{flow} · {movement}" if complete else f"최근 5분 관측 중 ({five['observed_seconds']/60:.1f}/5분)"
    price_change = "변화 대기" if change is None else f"{change:+.3f}pt"
    zones = " · ".join((_price_zone("매수", five["buy_avg_price"], price),
                        _price_zone("매도", five["sell_avg_price"], price)))
    detail = (f"{current} · {period} {price_change}\n"
              f"{period} 순매수 {net:+,.0f}계약 · {daily}\n{period} {zones}")
    tone = ("buy" if net > 1e-9 else "sell" if net < -1e-9 else "muted") if complete else "muted"
    return FlowBrief(headline, detail, tone)
