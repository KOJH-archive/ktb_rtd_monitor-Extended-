"""Small native price-volume profiles from existing foreign-flow observations."""
from __future__ import annotations

from dataclasses import dataclass
import math
from time import monotonic
import tkinter as tk

BUY = "#087f8c"
SELL = "#c76324"
CURRENT = "#9b7000"
INK = "#172b43"
MUTED = "#52657a"
LINE = "#dce4ec"
PANEL = "#ffffff"


@dataclass(frozen=True)
class PriceBand:
    low: float
    high: float
    buy: float
    sell: float

    @property
    def net(self) -> float:
        return self.buy - self.sell

    @property
    def label(self) -> str:
        return f"{self.low:.3f}–{self.high:.3f}"


def group_price_profile(profile: dict[str, object], max_rows: int = 12) -> tuple[PriceBand, ...]:
    """Cover the entire price span with equal-width bins, retaining all volume.

    Coarsen by an integer multiple of the native bin, including empty interior
    bins. Never hide volume by showing only the busiest prices.
    """
    if max_rows < 1:
        raise ValueError("max_rows must be positive")
    size = float(profile["bin_size"])
    if not math.isfinite(size) or size <= 0:
        raise ValueError("Invalid bin size")
    bins = profile["bins"]
    if not bins:
        return ()
    indexed = [(round(low / size), buy, sell) for low, buy, sell in bins]
    first = min(index for index, _, _ in indexed)
    last = max(index for index, _, _ in indexed)
    factor = max(1, math.ceil((last - first + 1) / max_rows))
    count = (last - first) // factor + 1
    buys, sells = [0.0] * count, [0.0] * count
    for index, buy, sell in indexed:
        group = (index - first) // factor
        buys[group] += buy
        sells[group] += sell
    return tuple(PriceBand((first + i * factor) * size, (first + (i + 1) * factor) * size,
                           buys[i], sells[i]) for i in range(count))


def band_for_price(bands: tuple[PriceBand, ...], price: float | None) -> int | None:
    if price is None or not bands:
        return None
    # Match the tracker's half-open bin convention, including decimal edges.
    step = bands[0].high - bands[0].low
    index = math.floor((price - bands[0].low) / step + 1e-9)
    return index if 0 <= index < len(bands) else None


class ForeignPriceProfile(tk.Canvas):
    """At most 12 rows, no animations; one redraw job and one canvas tooltip."""

    def __init__(self, master, title: str):
        super().__init__(master, bg=PANEL, height=392, highlightthickness=0)
        self.title_text = title
        self._data: tuple | None = None
        self._key = None
        self._job = None
        self._last_draw = 0.0
        self._drawn_width = 0
        self._hits: list[tuple[float, float, PriceBand]] = []
        self._hover_band = None
        self._draw_count = 0
        self._tip_text = self._tip_box = None
        self.bind("<Configure>", self._resize)
        self.bind("<Map>", lambda event: self._schedule(0))
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda event: self._hide_tooltip())
        self.bind("<Destroy>", self._close, add="+")
        self._schedule(0)

    def set_data(self, profile: dict[str, object], current_price: float | None,
                 fresh: bool, mode: str, flow_active: bool) -> None:
        key = (id(profile), current_price, fresh, mode, flow_active)
        if key == self._key:
            return
        self._key = key
        self._data = (profile, current_price, fresh, mode, flow_active)
        wait = max(0, math.ceil((2.0 - (monotonic() - self._last_draw)) * 1000))
        # A deliberate mode switch or freshness change should be immediate.
        if getattr(self, "_display_mode", None) != mode or getattr(self, "_display_fresh", None) != fresh:
            if self._job is not None:
                self.after_cancel(self._job)
                self._job = None
            wait = 0
        self._schedule(wait)

    def _schedule(self, delay: int) -> None:
        if self._job is None:
            self._job = self.after(delay, self._draw)

    def _resize(self, event) -> None:
        if event.width != self._drawn_width:
            if self._job is not None:
                self.after_cancel(self._job)
                self._job = None
            self._schedule(120)

    def _close(self, event) -> None:
        if event.widget is self and self._job is not None:
            self.after_cancel(self._job)
            self._job = None

    def _hide_tooltip(self) -> None:
        self.delete("profile_tip")
        self._tip_text = self._tip_box = None
        self._hover_band = None

    def _motion(self, event) -> None:
        band = next((band for top, bottom, band in self._hits if top <= event.y < bottom), None)
        if band is None:
            self._hide_tooltip()
            return
        if band != self._hover_band:
            self._hide_tooltip()
            self._hover_band = band
            profile = self._data[0]
            share = band.buy / profile["buy_volume"] if profile["buy_volume"] > 0 else 0
            value = (f"{band.low:.3f} 이상 ~ {band.high:.3f} 미만\n"
                     f"매수 증가 관측 {band.buy:,.0f}계약 (관측 매수의 {share:.1%})\n"
                     f"매도 증가 관측 {band.sell:,.0f}계약\n"
                     f"관측 순매수 {band.net:+,.0f}계약")
            self._tip_text = self._text(0, 0, value, color="white", width=self.winfo_width() - 24,
                                       tags=("profile_tip",))
            self._tip_box = self.create_rectangle(0, 0, 0, 0, fill=INK, outline=INK, tags=("profile_tip",))
            self.tag_lower(self._tip_box, self._tip_text)
        bounds = self.bbox(self._tip_text)
        tip_width, tip_height = bounds[2] - bounds[0] + 16, bounds[3] - bounds[1] + 10
        x = max(0, min(event.x + 12, self.winfo_width() - tip_width))
        y = event.y + 14
        if y + tip_height > self.winfo_height():
            y = event.y - tip_height - 10
        y = max(0, y)
        self.coords(self._tip_text, x + 8, y + 5)
        self.coords(self._tip_box, x, y, x + tip_width, y + tip_height)

    def _text(self, x, y, value, *, color=INK, anchor="nw", bold=False, size=9, **kwargs):
        return self.create_text(x, y, text=value, fill=color, anchor=anchor,
                                font=("맑은 고딕", size, "bold" if bold else "normal"), **kwargs)

    def _draw(self) -> None:
        self._job = None
        if not self.winfo_ismapped() or self.winfo_width() < 250:
            return
        self._hide_tooltip()
        self.delete("all")
        self._hits = []
        self._last_draw = monotonic()
        self._drawn_width = width = self.winfo_width()
        self._draw_count += 1
        self._text(8, 5, self.title_text, bold=True, size=11)
        if self._data is None:
            self._text(8, 54, "당일 관측 데이터 대기", color=MUTED)
            return
        profile, price, fresh, mode, active = self._data
        self._display_mode, self._display_fresh = mode, fresh
        bands = group_price_profile(profile)
        if not bands:
            self._text(8, 56, "가격대별 외국인 관측량이 아직 없습니다.\n"
                       "첫 관측 이후 매수·매도 증가량부터 쌓입니다.", color=MUTED, width=width - 16)
            return
        buy_peak = max(bands, key=lambda band: band.buy)
        sell_peak = max(bands, key=lambda band: band.sell)
        self._text(8, 30, (f"매수 집중  {buy_peak.label}  ({buy_peak.buy:,.0f}계약)"
                           if buy_peak.buy else "매수 증가 관측 대기"), color=BUY, width=width - 16)
        self._text(8, 49, (f"매도 집중  {sell_peak.label}  ({sell_peak.sell:,.0f}계약)"
                           if sell_peak.sell else "매도 증가 관측 대기"), color=SELL, width=width - 16)
        current = band_for_price(bands, price) if fresh else None
        note = "현재가 갱신 대기 · 당일 관측 유지"
        if fresh and price is not None:
            location = (" · 표시범위 위" if price >= bands[-1].high else
                        " · 표시범위 아래" if price < bands[0].low else " · 금색 점선")
            note = f"현재 {price:.3f}pt{location}"
        if not active:
            note += " · 수급 갱신 대기"
        self._text(8, 71, note, color=CURRENT if fresh else MUTED, width=width - 16, size=8)

        plot_left, plot_right = 133, width - 68
        center = (plot_left + plot_right) / 2
        radius = max(1, (plot_right - plot_left) / 2 - 5)
        scale = max((max(band.buy, band.sell) if mode == "gross" else abs(band.net)) for band in bands)
        scale = max(1.0, scale)
        self._text(8, 99, "가격대(pt)", color=MUTED, size=8)
        self._text(center - 8, 99, "매도" if mode == "gross" else "순매도", color=SELL, anchor="ne", size=8)
        self._text(center + 8, 99, "매수" if mode == "gross" else "순매수", color=BUY, size=8)
        self._text(width - 5, 99, "순매수", color=MUTED, anchor="ne", size=8)
        top, bottom = 123, self.winfo_height() - 28
        height = min(28, (bottom - top) / len(bands))
        for row, band in enumerate(reversed(bands)):
            y = top + row * height
            original_index = len(bands) - row - 1
            fill = "#fff5d9" if original_index == current else ("#f3f6f9" if row % 2 == 0 else PANEL)
            self.create_rectangle(0, y, width, y + height, fill=fill, outline="")
            self._hits.append((y, y + height, band))
            self._text(8, y + height / 2, band.label, anchor="w", size=8)
            left = band.sell if mode == "gross" else max(0, -band.net)
            right = band.buy if mode == "gross" else max(0, band.net)
            for amount, direction, color in ((left, -1, SELL), (right, 1, BUY)):
                if amount > 0:
                    edge = center + direction * radius * amount / scale
                    self.create_rectangle(min(center, edge), y + height * .23,
                                          max(center, edge), y + height * .77, fill=color, outline="")
            self._text(width - 5, y + height / 2, f"{band.net:+,.0f}", anchor="e", size=8,
                       color=BUY if band.net > 0 else SELL if band.net < 0 else MUTED)
        self.create_line(center, top, center, top + len(bands) * height, fill=LINE)
        if current is not None:
            # Locate the current price within its equal-width price band.
            y = top + (len(bands) - 1 - current + (bands[current].high - price) /
                       (bands[current].high - bands[current].low)) * height
            self.create_line(plot_left, y, plot_right, y, fill=CURRENT, dash=(4, 3), width=2)
        step = bands[0].high - bands[0].low
        self._text(8, self.winfo_height() - 18,
                   f"구간 {step:.3f}pt · 계약 · 만기별 독립 축 · 마우스로 수치 확인", color=MUTED, size=8)
