"""Price-band volume conservation, cache and current-price boundary checks."""
import unittest
from datetime import datetime, timedelta, timezone

from extra_settings import ExtraSettings
from foreign_profile import band_for_price, group_price_profile
from market_extensions import ForeignFlowTracker

START = datetime(2026, 10, 29, tzinfo=timezone.utc)


def observe(tracker, seconds, price, buy, sell):
    tracker.update({"3y_trade_price": price, "3y_foreign_buy": buy, "3y_foreign_sell": sell},
                   START + timedelta(seconds=seconds), "today")


class ForeignProfileTests(unittest.TestCase):
    def test_profile_only_assigns_observed_volume_increments(self):
        tracker = ForeignFlowTracker("3y", ExtraSettings())
        observe(tracker, 0, 105.01, 1000, 900)
        self.assertEqual(tracker.price_profile()["bins"], ())
        observe(tracker, 5, 105.03, 1030, 910)
        observe(tracker, 10, 105.05, 1070, 960)
        profile = tracker.price_profile()
        self.assertEqual(profile["buy_volume"], 70)
        self.assertEqual(profile["sell_volume"], 60)
        bands = group_price_profile(profile)
        self.assertEqual(len(bands), 2)
        self.assertAlmostEqual(bands[0].low, 105.02)
        self.assertEqual((bands[0].buy, bands[0].sell, bands[0].net), (30, 10, 20))
        self.assertEqual((bands[1].buy, bands[1].sell, bands[1].net), (40, 50, -10))

    def test_cache_changes_only_with_volume_or_reset_and_old_snapshot_is_stable(self):
        tracker = ForeignFlowTracker("3y", ExtraSettings())
        observe(tracker, 0, 105, 100, 100)
        observe(tracker, 5, 105, 120, 110)
        before = tracker.price_profile()
        self.assertIs(before, tracker.price_profile())
        observe(tracker, 10, 106, 120, 110)
        self.assertIs(before, tracker.price_profile())
        observe(tracker, 15, 106, 130, 110)
        after = tracker.price_profile()
        self.assertIsNot(before, after)
        self.assertEqual(before["buy_volume"], 20)
        self.assertEqual(after["buy_volume"], 30)
        tracker.pause()
        self.assertIs(after, tracker.price_profile())
        observe(tracker, 20, 106, 1, 1)
        reset = tracker.price_profile()
        self.assertGreater(reset["revision"], after["revision"])
        self.assertEqual(reset["bins"], ())

    def test_gap_does_not_allocate_unknown_fills_to_current_price(self):
        tracker = ForeignFlowTracker("3y", ExtraSettings())
        observe(tracker, 0, 105, 100, 100)
        observe(tracker, 5, 105, 110, 100)
        observe(tracker, 40, 106, 500, 500)
        profile = tracker.price_profile()
        self.assertEqual(profile["buy_volume"], 10)
        self.assertEqual(profile["sell_volume"], 0)
        observe(tracker, 45, 106, 520, 505)
        profile = tracker.price_profile()
        self.assertEqual(profile["buy_volume"], 30)
        self.assertEqual(profile["sell_volume"], 5)

    def test_wide_sparse_range_groups_every_band_without_discarding_volume(self):
        profile = {"bin_size": .02, "bins": ((100.0, 300, 100), (101.0, 10, 50), (104.0, 200, 200))}
        bands = group_price_profile(profile)
        self.assertLessEqual(len(bands), 12)
        self.assertEqual(sum(band.buy for band in bands), 510)
        self.assertEqual(sum(band.sell for band in bands), 350)
        self.assertEqual(sum(band.net for band in bands), 160)
        self.assertTrue(any(band.buy == band.sell == 0 for band in bands))
        self.assertLessEqual(bands[0].low, 100)
        self.assertGreater(bands[-1].high, 104)
        for left, right in zip(bands, bands[1:]):
            self.assertAlmostEqual(left.high, right.low)
            self.assertAlmostEqual(left.high - left.low, right.high - right.low)

    def test_decimal_boundaries_are_half_open_and_outside_prices_remain_outside(self):
        profile = {"bin_size": .02, "bins": ((105.10, 10, 0), (105.14, 0, 20))}
        bands = group_price_profile(profile)
        self.assertEqual(band_for_price(bands, 105.10), 0)
        self.assertEqual(band_for_price(bands, 105.12), 1)
        self.assertEqual(band_for_price(bands, 105.14), 2)
        self.assertIsNone(band_for_price(bands, 105.16))
        self.assertIsNone(band_for_price(bands, 104.99))
        self.assertIsNone(band_for_price(bands, None))
        self.assertEqual(group_price_profile({"bin_size": .02, "bins": ()}), ())


if __name__ == "__main__":
    unittest.main()
