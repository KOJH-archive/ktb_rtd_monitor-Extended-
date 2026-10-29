"""Check that the plain-language cards describe observations, including gaps."""
import unittest
from datetime import datetime, timedelta, timezone

from extra_settings import ExtraSettings
from flow_brief import foreign_flow_brief
from market_extensions import ForeignFlowTracker


START = datetime(2026, 10, 29, tzinfo=timezone.utc)


class FlowBriefTests(unittest.TestCase):
    def observed(self, net_buy=True, direction=1):
        settings = ExtraSettings()
        tracker = ForeignFlowTracker("3y", settings)
        for step in range(61):
            stamp = START + timedelta(seconds=5 * step)
            row = {"timestamp_utc": stamp.isoformat(),
                   "3y_trade_price": 105 + direction * step * .001,
                   "3y_foreign_buy": 1000 + step * (2 if net_buy else 1),
                   "3y_foreign_sell": 2000 + step * (1 if net_buy else 2)}
            tracker.update(row, stamp, "today")
        return row, tracker, settings

    def test_recent_buying_and_daily_net_selling_are_distinct(self):
        row, tracker, settings = self.observed()
        brief = foreign_flow_brief("3y", row, tracker.summary(), settings, True)
        self.assertEqual(brief.headline, "외국인 순매수 · 가격 상승")
        self.assertIn("5분 순매수 +60계약", brief.detail)
        self.assertIn("당일 누적 순매수 -940계약", brief.detail)
        self.assertIn("현재 위", brief.detail)
        self.assertEqual(brief.tone, "buy")

    def test_selling_can_coexist_with_a_rising_price(self):
        row, tracker, settings = self.observed(net_buy=False)
        brief = foreign_flow_brief("3y", row, tracker.summary(), settings, True)
        self.assertEqual(brief.headline, "외국인 순매도 · 가격 상승")
        self.assertEqual(brief.tone, "sell")
        row, tracker, settings = self.observed(direction=-1)
        brief = foreign_flow_brief("3y", row, tracker.summary(), settings, True)
        self.assertEqual(brief.headline, "외국인 순매수 · 가격 하락")
        self.assertIn("현재 아래", brief.detail)

    def test_gap_preserves_history_but_withholds_direction_during_warmup(self):
        row, tracker, settings = self.observed()
        row = {**row, "timestamp_utc": (START + timedelta(seconds=330)).isoformat(),
               "3y_foreign_buy": 2000}
        tracker.update(row, START + timedelta(seconds=330), "today")
        brief = foreign_flow_brief("3y", row, tracker.summary(), settings, True)
        self.assertIn("관측 중", brief.headline)
        self.assertIn("5분창(부분)", brief.detail)
        self.assertNotIn("가격 상승", brief.headline)
        self.assertEqual(brief.tone, "muted")

    def test_stale_feed_or_source_does_not_describe_current_flow(self):
        row, tracker, settings = self.observed()
        summary = tracker.summary()
        brief = foreign_flow_brief("3y", row, summary, settings, False)
        self.assertEqual(brief.headline, "최신 데이터 대기")
        self.assertNotIn("+60", brief.detail)
        for key in ("3y_foreign_time", "3y_trade_time"):
            for value in (START.isoformat(), "invalid", float("inf")):
                with self.subTest(key=key, value=value):
                    brief = foreign_flow_brief("3y", {**row, key: value}, summary, settings, True)
                    self.assertIn("원천시각 확인", brief.headline)

    def test_missing_inputs_zero_volume_and_contracts_are_independent(self):
        row, tracker, settings = self.observed()
        summary = tracker.summary()
        other = foreign_flow_brief("10y", row, summary, settings, True)
        self.assertEqual(other.headline, "외국인 수급 미연결")
        for price in (0, "", None, float("nan")):
            brief = foreign_flow_brief("3y", {**row, "3y_trade_price": price}, summary, settings, True)
            self.assertEqual(brief.headline, "선물 체결가 대기")
        tracker = ForeignFlowTracker("3y", settings)
        row = {**row, "3y_foreign_buy": 0, "3y_foreign_sell": 0}
        tracker.update(row, START, "today")
        brief = foreign_flow_brief("3y", row, tracker.summary(), settings, True)
        self.assertIn("관측 중", brief.headline)
        self.assertIn("당일 누적 순매수 +0계약", brief.detail)
        tracker.pause()
        brief = foreign_flow_brief("3y", row, tracker.summary(), settings, True)
        self.assertIn("준비 중", brief.headline)


if __name__ == "__main__":
    unittest.main()
