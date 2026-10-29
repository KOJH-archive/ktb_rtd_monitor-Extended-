"""Analytic checks for time weighting, missing intervals and rolling expiry."""
import unittest
from datetime import datetime, timedelta, timezone

from book_pressure import BookPressureTracker, book_pressure_text

START = datetime(2026, 10, 29, tzinfo=timezone.utc)


def book(second, obi=.4, spread=.01, **extra):
    return {"timestamp_utc": (START + timedelta(seconds=second)).isoformat(),
            "3y_obi": obi, "3y_weighted_obi": obi, "3y_spread": spread,
            "3y_bid_depth": 100 * (1 + obi), "3y_ask_depth": 100 * (1 - obi), **extra}


class BookPressureTests(unittest.TestCase):
    def test_time_weighting_uses_prior_book_not_row_counts_or_new_price(self):
        tracker = BookPressureTracker("3y")
        tracker.update(book(0, 1, .01))
        tracker.update(book(1, -1, .03))
        tracker.update(book(10, .8, .05))
        stats = tracker.summary()["windows"][5]
        self.assertEqual(stats["observed_seconds"], 10)
        self.assertAlmostEqual(stats["mean_obi"], -.8)
        self.assertAlmostEqual(stats["mean_spread"], .028)
        self.assertAlmostEqual(stats["buy_share"], .1)
        self.assertAlmostEqual(stats["sell_share"], .9)
        self.assertAlmostEqual(stats["coverage"], 10 / 300)
        self.assertFalse(stats["complete"])

    def test_identical_heartbeats_do_not_change_mean_or_duration(self):
        results = []
        for interval in (1, 5, 10):
            tracker = BookPressureTracker("3y")
            for second in range(0, 301, interval):
                tracker.update(book(second))
            stats = tracker.summary()
            results.append(stats)
            self.assertEqual(len(tracker.windows[5].segments), 1)
            self.assertTrue(stats["windows"][5]["complete"])
        for stats in results[1:]:
            for field in ("mean_obi", "mean_weighted_obi", "mean_spread", "buy_share", "coverage"):
                self.assertAlmostEqual(stats["windows"][5][field], results[0]["windows"][5][field])
            self.assertEqual(stats["day"]["observed_seconds"], 300)

    def test_rolling_boundary_trims_only_the_expired_fraction(self):
        tracker = BookPressureTracker("3y")
        for second in range(0, 311, 5):
            tracker.update(book(second, .8 if second < 100 else -.2))
        tracker.update(book(312.5, -.2))
        stats = tracker.summary()["windows"][5]
        self.assertAlmostEqual(stats["observed_seconds"], 300)
        self.assertAlmostEqual(stats["mean_obi"], (.8 * 87.5 - .2 * 212.5) / 300)
        self.assertAlmostEqual(stats["buy_share"], 87.5 / 300)
        self.assertAlmostEqual(stats["sell_share"], 212.5 / 300)
        self.assertAlmostEqual(tracker.summary()["day"]["observed_seconds"], 312.5)

    def test_gap_is_not_filled_and_day_history_survives_window_expiry(self):
        tracker = BookPressureTracker("3y")
        for second, obi in ((0, .8), (5, .8), (30, -.8), (35, -.8)):
            tracker.update(book(second, obi))
        stats = tracker.summary()
        self.assertEqual(stats["day"]["observed_seconds"], 10)
        self.assertAlmostEqual(stats["day"]["coverage"], 10 / 35)
        self.assertAlmostEqual(stats["day"]["mean_obi"], 0)
        self.assertFalse(stats["windows"][5]["complete"])
        tracker.update(book(4000))
        self.assertEqual(tracker.summary()["windows"][60]["observed_seconds"], 0)
        self.assertEqual(tracker.summary()["day"]["observed_seconds"], 10)
        self.assertTrue(all(not item.segments for item in tracker.windows.values()))

    def test_invalid_or_out_of_order_inputs_break_continuity_without_erasing_totals(self):
        invalid = ({"3y_obi": float("nan")}, {"3y_weighted_obi": 2},
                   {"3y_bid_depth": -1}, {"3y_spread": 0}, {"3y_obi": True},
                   {"3y_ask_depth": 0, "3y_bid_depth": 0})
        for bad in invalid:
            with self.subTest(bad=bad):
                tracker = BookPressureTracker("3y")
                tracker.update(book(0))
                tracker.update(book(5))
                tracker.update(book(10, **bad))
                self.assertFalse(tracker.summary()["active"])
                tracker.update(book(15))
                tracker.update(book(20))
                self.assertEqual(tracker.summary()["day"]["observed_seconds"], 10)
        tracker.update(book(19))
        tracker.update(book(25))
        self.assertEqual(tracker.summary()["day"]["observed_seconds"], 10)
        tracker.update(book(30))
        self.assertEqual(tracker.summary()["day"]["observed_seconds"], 15)

    def test_day_session_and_contract_changes_start_new_totals(self):
        for change in ({"timestamp_utc": (START + timedelta(days=1)).isoformat()},
                       {"session_id": "new-session"}, {"3y_contract": "new-contract"}):
            with self.subTest(change=change):
                tracker = BookPressureTracker("3y")
                tracker.update(book(0))
                tracker.update(book(5))
                tracker.update(book(10, **change))
                self.assertEqual(tracker.summary()["day"]["observed_seconds"], 0)
                self.assertEqual(tracker.summary()["windows"][5]["observed_seconds"], 0)

    def test_replay_matches_continuous_operation_and_summary_does_not_extend_time(self):
        records = [book(second, .4 if second < 100 else -.2) for second in range(0, 401, 5)]
        uninterrupted = BookPressureTracker("3y")
        for row in records:
            uninterrupted.update(row)
        restarted = BookPressureTracker("3y")
        for row in records[:50]:  # Restore the saved prefix, then resume live rows.
            restarted.update(row)
        for row in records[50:]:
            restarted.update(row)
        self.assertEqual(restarted.summary(), uninterrupted.summary())
        before = restarted.summary()
        for _ in range(10):
            self.assertEqual(restarted.summary(), before)
        title, detail, tone = book_pressure_text(before, False)
        self.assertIn("최신 관측 대기", title)
        self.assertEqual(tone, "muted")
        self.assertIn("당일", detail)

    def test_full_neutral_window_and_empty_contract_are_labelled(self):
        tracker = BookPressureTracker("3y")
        self.assertIn("관측 대기", book_pressure_text(tracker.summary(), True)[0])
        for second in range(0, 301, 5):
            tracker.update(book(second, 0))
        stats = tracker.summary()
        self.assertEqual(stats["windows"][5]["neutral_share"], 1)
        title, detail, tone = book_pressure_text(stats, True)
        self.assertIn("잔량 균형권", title)
        self.assertIn("전체", detail)
        self.assertIn("15분 [부분", detail)
        self.assertEqual(tone, "muted")
        other = BookPressureTracker("10y")
        other.update(book(300))
        self.assertEqual(other.summary()["day"]["observed_seconds"], 0)
        self.assertFalse(other.summary()["active"])


if __name__ == "__main__":
    unittest.main()
