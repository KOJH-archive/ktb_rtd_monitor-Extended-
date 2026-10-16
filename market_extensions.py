"""Price alerts and foreign-flow diagnostics. No order functionality."""
from __future__ import annotations

import ctypes
import io
import math
import struct
import threading
import wave
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from extra_settings import AlertRule, ExtraSettings

KST = timezone(timedelta(hours=9))


def optional_number(value: object) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def market_metrics(row: dict[str, object]) -> dict[str, float]:
    result: dict[str, float] = {}
    price3 = optional_number(row.get("3y_trade_price"))
    price10 = optional_number(row.get("10y_trade_price"))
    cash3 = optional_number(row.get("cash_3y_yield_pct"))
    cash10 = optional_number(row.get("cash_10y_yield_pct"))
    futures_yield3 = optional_number(row.get("3y_futures_yield_pct"))
    futures_yield10 = optional_number(row.get("10y_futures_yield_pct"))
    if price3 is not None:
        result["3y_trade_price"] = price3
    if price10 is not None:
        result["10y_trade_price"] = price10
    if price3 is not None and price10 is not None:
        result["futures_spread"] = price10 - price3
    if cash3 is not None and cash10 is not None:
        result["cash_spread_bp"] = (cash10 - cash3) * 100.0
    if futures_yield3 is not None and futures_yield10 is not None:
        result["futures_yield_spread_bp"] = (futures_yield10 - futures_yield3) * 100.0
    return result


def source_timestamp(value: object, collector_timestamp: datetime) -> datetime | None:
    """Parse ISO, Excel serial, or KST clock time without inventing missing times."""
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, (float, int)) and not isinstance(value, bool):
        stamp = datetime(1899, 12, 30, tzinfo=KST) + timedelta(days=float(value))
    else:
        text = str(value).strip()
        try:
            stamp = datetime.fromisoformat(text)
        except ValueError:
            parsed_time = None
            for pattern in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
                try:
                    parsed_time = datetime.strptime(text, pattern).time()
                    break
                except ValueError:
                    continue
            if parsed_time is None:
                return None
            local_date = collector_timestamp.astimezone(KST).date()
            stamp = datetime.combine(local_date, parsed_time, KST)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=KST)
    return stamp.astimezone(timezone.utc)


def metric_is_fresh(row: dict[str, object], metric: str, collector_timestamp: datetime,
                    settings: ExtraSettings) -> bool:
    keys = {"futures_spread": ("3y_trade_time", "10y_trade_time"),
            "cash_spread_bp": ("cash_3y_time", "cash_10y_time"),
            "3y_trade_price": ("3y_trade_time",), "10y_trade_price": ("10y_trade_time",)}[metric]
    raw_values = [row.get(key) for key in keys]
    if not any(value not in (None, "") for value in raw_values):
        return True  # Collector freshness only; UI explicitly discloses this limitation.
    stamps = [source_timestamp(value, collector_timestamp) for value in raw_values]
    if any(stamp is None for stamp in stamps):
        return False
    collector_utc = collector_timestamp.astimezone(timezone.utc)
    if any(abs((collector_utc - stamp).total_seconds()) > settings.max_source_age_seconds for stamp in stamps):
        return False
    return max(stamps) - min(stamps) <= timedelta(seconds=settings.max_leg_skew_seconds)


@dataclass
class _RuleState:
    armed: bool = True
    candidate_since: datetime | None = None
    last_trigger: datetime | None = None


@dataclass(frozen=True)
class AlertEvent:
    name: str
    metric: str
    value: float
    timestamp: datetime


class AlertEngine:
    """Transition alerts with confirmation, hysteresis and cooldown."""

    def __init__(self, rules: tuple[AlertRule, ...]) -> None:
        self.rules = rules
        self.states = {rule.name: _RuleState() for rule in rules}

    @staticmethod
    def _condition(rule: AlertRule, value: float) -> bool:
        if rule.mode == "above":
            return value >= rule.lower
        if rule.mode == "below":
            return value <= rule.lower
        return rule.lower <= value <= float(rule.upper)

    @staticmethod
    def _rearmed(rule: AlertRule, value: float) -> bool:
        if rule.mode == "above":
            return value < rule.lower - rule.hysteresis
        if rule.mode == "below":
            return value > rule.lower + rule.hysteresis
        return value < rule.lower - rule.hysteresis or value > float(rule.upper) + rule.hysteresis

    def update(self, metrics: dict[str, float], timestamp: datetime,
               fresh: bool | dict[str, bool]) -> list[AlertEvent]:
        events = []
        for rule in self.rules:
            if not rule.enabled or rule.metric not in metrics:
                continue
            state = self.states[rule.name]
            value = metrics[rule.metric]
            metric_fresh = fresh.get(rule.metric, False) if isinstance(fresh, dict) else fresh
            if not metric_fresh:
                state.candidate_since = None
                continue
            if not state.armed:
                if self._rearmed(rule, value):
                    state.armed = True
                else:
                    continue
            if not self._condition(rule, value):
                state.candidate_since = None
                continue
            state.candidate_since = state.candidate_since or timestamp
            confirmed = (timestamp - state.candidate_since).total_seconds() >= rule.confirmation_seconds
            cooled = state.last_trigger is None or (timestamp - state.last_trigger).total_seconds() >= rule.cooldown_seconds
            if confirmed and cooled:
                state.armed = False
                state.candidate_since = None
                state.last_trigger = timestamp
                events.append(AlertEvent(rule.name, rule.metric, value, timestamp))
        return events


class WindowsNotifier:
    """Quiet short tone plus Windows taskbar-button flashing."""

    def __init__(self, settings: ExtraSettings) -> None:
        self.settings = settings

    @staticmethod
    def _wav(gain: float) -> bytes:
        rate, duration, frequency = 16_000, .09, 620.0
        count = int(rate * duration)
        samples = bytearray()
        for index in range(count):
            envelope = min(1.0, index / (rate * .012), (count - index) / (rate * .025))
            value = int(32767 * gain * max(0.0, envelope) * math.sin(2 * math.pi * frequency * index / rate))
            samples.extend(struct.pack("<h", value))
        target = io.BytesIO()
        with wave.open(target, "wb") as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(rate); wav.writeframes(bytes(samples))
        return target.getvalue()

    def notify(self, hwnd: int) -> None:
        if hasattr(ctypes, "windll"):
            class FLASHWINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("hwnd", ctypes.c_void_p),
                            ("dwFlags", ctypes.c_uint), ("uCount", ctypes.c_uint),
                            ("dwTimeout", ctypes.c_uint)]
            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
            user32.GetAncestor.restype = ctypes.c_void_p
            user32.FlashWindowEx.argtypes = (ctypes.POINTER(FLASHWINFO),)
            user32.FlashWindowEx.restype = ctypes.c_bool
            top = user32.GetAncestor(hwnd, 2) or hwnd
            info = FLASHWINFO(ctypes.sizeof(FLASHWINFO), top, 0x2, 5, 0)
            user32.FlashWindowEx(ctypes.byref(info))
        if self.settings.sound_enabled:
            sound = self._wav(self.settings.sound_gain)
            def play() -> None:
                try:
                    import winsound
                    winsound.PlaySound(sound, winsound.SND_MEMORY | winsound.SND_NODEFAULT)
                except (ImportError, RuntimeError):
                    pass
            threading.Thread(target=play, daemon=True).start()


@dataclass
class _Side:
    volume: float = 0.0
    price_value: float = 0.0
    yield_value: float = 0.0
    yield_volume: float = 0.0
    bins: dict[float, float] = field(default_factory=dict)

    def add(self, volume: float, price: float, yield_pct: float | None, bin_size: float) -> None:
        if volume <= 0:
            return
        self.volume += volume
        self.price_value += volume * price
        if yield_pct is not None:
            self.yield_value += volume * yield_pct
            self.yield_volume += volume
        bucket = math.floor(price / bin_size + 1e-9) * bin_size
        self.bins[bucket] = self.bins.get(bucket, 0.0) + volume

    def summary(self, bin_size: float) -> dict[str, float] | None:
        if self.volume <= 0:
            return None
        dominant = max(self.bins, key=self.bins.get)
        result = {"observed_volume": self.volume, "estimated_avg_price": self.price_value / self.volume,
                  "dominant_price_low": dominant, "dominant_price_high": dominant + bin_size,
                  "dominant_volume": self.bins[dominant]}
        if self.yield_volume:
            result["estimated_avg_yield_pct"] = self.yield_value / self.yield_volume
        return result


@dataclass
class _FlowSecond:
    timestamp: datetime
    first_price: float
    last_price: float
    buy: float = 0.0
    sell: float = 0.0
    buy_price_value: float = 0.0
    sell_price_value: float = 0.0


class _RollingForeignWindow:
    """Second-bucketed rolling totals; bounded to one session hour."""

    def __init__(self, minutes: int) -> None:
        self.minutes = minutes
        self.buckets: deque[_FlowSecond] = deque()
        self.buy = self.sell = self.buy_price_value = self.sell_price_value = 0.0

    def add(self, timestamp: datetime, buy: float, sell: float, price: float) -> None:
        second = timestamp.replace(microsecond=0)
        if not self.buckets or self.buckets[-1].timestamp != second:
            self.buckets.append(_FlowSecond(second, price, price))
        bucket = self.buckets[-1]
        bucket.last_price = price
        bucket.buy += buy
        bucket.sell += sell
        bucket.buy_price_value += buy * price
        bucket.sell_price_value += sell * price
        self.buy += buy
        self.sell += sell
        self.buy_price_value += buy * price
        self.sell_price_value += sell * price
        cutoff = timestamp - timedelta(minutes=self.minutes)
        while self.buckets and self.buckets[0].timestamp < cutoff:
            old = self.buckets.popleft()
            self.buy -= old.buy
            self.sell -= old.sell
            self.buy_price_value -= old.buy_price_value
            self.sell_price_value -= old.sell_price_value

    def summary(self, timestamp: datetime, started: datetime) -> dict[str, object]:
        elapsed = max(0.0, (timestamp - started).total_seconds())
        window_seconds = self.minutes * 60
        buy = max(0.0, self.buy)
        sell = max(0.0, self.sell)
        return {
            "minutes": self.minutes,
            "complete": elapsed >= window_seconds,
            "observed_seconds": min(elapsed, window_seconds),
            "buy_volume": buy,
            "sell_volume": sell,
            "net_volume": buy - sell,
            "buy_avg_price": self.buy_price_value / buy if buy > 0 else None,
            "sell_avg_price": self.sell_price_value / sell if sell > 0 else None,
            "price_change": (self.buckets[-1].last_price - self.buckets[0].first_price)
                            if self.buckets else None,
        }


class ForeignFlowTracker:
    """Estimate price/yield zones from changes in participant cumulative volumes.

    The snapshot trade price is a proxy for all participant-volume changes since
    the prior observation. This is not the participant's true fill price.
    """

    def __init__(self, contract: str, settings: ExtraSettings) -> None:
        self.contract, self.settings = contract, settings
        self.previous: tuple[float, float] | None = None
        self.previous_time: datetime | None = None
        self.started_time: datetime | None = None
        self.session = None
        self.buy, self.sell = _Side(), _Side()
        self.windows = {minutes: _RollingForeignWindow(minutes) for minutes in (1, 5, 15, 60)}
        self.resets = 0

    def reset(self, buy: float | None = None, sell: float | None = None,
              timestamp: datetime | None = None, session=None) -> None:
        self.previous = (buy, sell) if buy is not None and sell is not None else None
        self.previous_time, self.started_time, self.session = timestamp, timestamp, session
        self.buy, self.sell = _Side(), _Side()
        self.windows = {minutes: _RollingForeignWindow(minutes) for minutes in (1, 5, 15, 60)}
        self.resets += 1

    def update(self, row: dict[str, object], timestamp: datetime, session) -> None:
        buy = optional_number(row.get(f"{self.contract}_foreign_buy"))
        sell = optional_number(row.get(f"{self.contract}_foreign_sell"))
        price = optional_number(row.get(f"{self.contract}_trade_price"))
        yield_pct = optional_number(row.get(f"{self.contract}_futures_yield_pct"))
        if buy is None or sell is None or price is None or min(buy, sell) < 0:
            self.reset()
            return
        if (self.previous is None or session != self.session or self.previous_time is None
                or timestamp <= self.previous_time
                or (timestamp - self.previous_time).total_seconds() > self.settings.flow_max_gap_seconds
                or buy < self.previous[0] or sell < self.previous[1]):
            self.reset(buy, sell, timestamp, session)
            return
        buy_change, sell_change = buy - self.previous[0], sell - self.previous[1]
        self.buy.add(buy_change, price, yield_pct, self.settings.price_bin_points)
        self.sell.add(sell_change, price, yield_pct, self.settings.price_bin_points)
        for window in self.windows.values():
            window.add(timestamp, buy_change, sell_change, price)
        self.previous, self.previous_time, self.session = (buy, sell), timestamp, session

    def summary(self) -> dict[str, object]:
        windows = ({minutes: window.summary(self.previous_time, self.started_time)
                    for minutes, window in self.windows.items()}
                   if self.previous_time is not None and self.started_time is not None else {})
        return {"contract": self.contract, "buy": self.buy.summary(self.settings.price_bin_points),
                "sell": self.sell.summary(self.settings.price_bin_points), "resets": self.resets,
                "windows": windows}
