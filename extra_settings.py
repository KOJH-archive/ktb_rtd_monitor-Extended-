"""Separate, validated configuration for non-ordering alerts and RTD extensions."""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

FEED_FIELDS = (
    "cash_3y_yield_pct", "cash_5y_yield_pct", "cash_10y_yield_pct",
    "cash_3y_time", "cash_5y_time", "cash_10y_time",
    "3y_foreign_buy", "3y_foreign_sell", "10y_foreign_buy", "10y_foreign_sell",
    "3y_futures_yield_pct", "10y_futures_yield_pct", "3y_trade_time", "10y_trade_time",
    "3y_foreign_time", "10y_foreign_time", "session_id", "3y_contract", "10y_contract",
    "cash_3y_bond", "cash_5y_bond", "cash_10y_bond",
)
METRICS = {"futures_spread": "10년−3년 선물 체결가차 (pt)",
           "futures_yield_spread_bp": "10년−3년 선물 수익률 차 (bp)",
           "cash_spread_bp": "현물 10년−3년 체결수익률차 (bp)",
           "cash_butterfly_bp": "현물 3−5−10년 곡률 (bp)",
           "basis_3y_bp": "3년 현선물 수익률 베이시스 (bp)",
           "basis_10y_bp": "10년 현선물 수익률 베이시스 (bp)",
           "cross_basis_bp": "10년−3년 교차 베이시스 (bp)",
           "3y_trade_price": "3년 선물 체결가", "10y_trade_price": "10년 선물 체결가"}


@dataclass(frozen=True)
class Binding:
    sheet: str
    cell: str


@dataclass(frozen=True)
class AlertRule:
    name: str
    metric: str
    mode: str
    lower: float
    upper: float | None = None
    hysteresis: float = 0.01
    confirmation_seconds: float = 1.0
    cooldown_seconds: float = 60.0
    enabled: bool = True


@dataclass(frozen=True)
class ExtraSettings:
    bindings: dict[str, Binding | None] = field(default_factory=lambda: dict.fromkeys(FEED_FIELDS))
    rules: tuple[AlertRule, ...] = ()
    sound_enabled: bool = True
    sound_gain: float = 0.06
    stale_seconds: float = 10.0
    max_source_age_seconds: float = 30.0
    max_leg_skew_seconds: float = 10.0
    flow_max_gap_seconds: float = 10.0
    price_bin_points: float = 0.02


def number(value, label, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError(f"{label}: 유한 숫자를 입력하세요")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label}: {minimum} 이상이어야 합니다")
    return float(value)


def parse_settings(raw: dict) -> ExtraSettings:
    defaults = ExtraSettings()
    if not isinstance(raw, dict) or set(raw) - set(asdict(defaults)):
        raise ValueError("알 수 없는 확장 설정 항목")
    mappings = raw.get("bindings", {})
    if not isinstance(mappings, dict) or set(mappings) - set(FEED_FIELDS):
        raise ValueError("알 수 없는 RTD 연결 항목")
    bindings = dict.fromkeys(FEED_FIELDS)
    for key, value in mappings.items():
        if value is None:
            continue
        if (not isinstance(value, dict) or set(value) != {"sheet", "cell"}
                or not isinstance(value["sheet"], str) or not value["sheet"].strip()
                or not isinstance(value["cell"], str)
                or not re.fullmatch(r"[A-Z]{1,3}[1-9][0-9]{0,6}", value["cell"])):
            raise ValueError(f"{key}: sheet와 단일 A1 cell 주소가 필요합니다")
        bindings[key] = Binding(**value)
    rules = []
    if not isinstance(raw.get("rules", []), list):
        raise ValueError("rules는 목록이어야 합니다")
    for item in raw.get("rules", []):
        if not isinstance(item, dict):
            raise ValueError("알림 규칙은 객체여야 합니다")
        try:
            rule = AlertRule(**item)
        except TypeError as exc:
            raise ValueError("알림 규칙 항목을 확인하세요") from exc
        if not isinstance(rule.name, str) or not rule.name.strip() or len(rule.name) > 80:
            raise ValueError("알림 이름은 1~80자입니다")
        if rule.metric not in METRICS or rule.mode not in ("above", "below", "between"):
            raise ValueError("알림 대상 또는 조건 오류")
        number(rule.lower, "기준값")
        if rule.upper is not None:
            number(rule.upper, "상단")
        if rule.mode == "between" and (rule.upper is None or rule.upper <= rule.lower):
            raise ValueError("구간 상단은 하단보다 커야 합니다")
        for key in ("hysteresis", "confirmation_seconds", "cooldown_seconds"):
            number(getattr(rule, key), key, 0)
        if not isinstance(rule.enabled, bool):
            raise ValueError("enabled는 true/false여야 합니다")
        rules.append(rule)
    if len(rules) > 100 or len({rule.name for rule in rules}) != len(rules):
        raise ValueError("알림 이름은 중복 없이 최대 100개입니다")
    kwargs = {}
    for key in ("sound_gain", "stale_seconds", "max_source_age_seconds", "max_leg_skew_seconds",
                "flow_max_gap_seconds", "price_bin_points"):
        kwargs[key] = number(raw.get(key, getattr(defaults, key)), key, 0)
        if kwargs[key] <= 0:
            raise ValueError(f"{key}: 0보다 커야 합니다")
    if kwargs["sound_gain"] > .15:
        raise ValueError("조용한 소리의 gain 상한은 0.15입니다")
    enabled = raw.get("sound_enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("sound_enabled는 true/false여야 합니다")
    return ExtraSettings(bindings=bindings, rules=tuple(rules), sound_enabled=enabled, **kwargs)


def load_extra_settings(path: Path) -> ExtraSettings:
    return parse_settings(json.loads(path.read_text(encoding="utf-8")))
