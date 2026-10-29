import csv
import unittest
from datetime import date
from pathlib import Path

from spread_chart import minute_spreads
from spread_chart_feed import ChartFeed


class SpreadChartTests(unittest.TestCase):
    def test_incremental_append_partial_row_and_reload(self):
        path = Path(__file__).with_name('.test_chart_feed.csv')
        day = date(2026, 10, 22)
        header = 'timestamp_utc,cash_spread_bp,futures_yield_spread_bp\n'
        try:
            path.write_text(header + '2026-10-22T00:00:00+00:00,30,25\n', encoding='utf-8')
            feed = ChartFeed()
            original = feed.read(path, target_date=day)[0]
            read_bytes = feed.bytes_read
            self.assertFalse(feed.read(path, target_date=day)[2])
            self.assertEqual(feed.bytes_read, read_bytes)
            with path.open('a', encoding='utf-8') as handle:
                handle.write('2026-10-22T00:01:00+00:00,31,')
            self.assertEqual(feed.read(path, target_date=day)[0], original)
            with path.open('a', encoding='utf-8') as handle:
                handle.write('26\n')
            rows = feed.read(path, target_date=day)[0]
            self.assertEqual(rows, minute_spreads(path, day))
            self.assertEqual(len(rows), 2)
            self.assertEqual(feed.read(path, reload=True, target_date=day)[0], rows)
            path.write_text(header + '2026-10-22T00:02:00+00:00,32,27\n', encoding='utf-8')
            self.assertEqual(len(feed.read(path, target_date=day)[0]), 1)
        finally:
            path.unlink(missing_ok=True)

    def test_last_valid_observation_per_minute_and_kst_date(self):
        path = Path(__file__).with_name(".test_spread_chart_1.csv")
        try:
            fields = ["timestamp_utc", "cash_3y_yield_pct", "cash_10y_yield_pct",
                      "3y_futures_yield_pct", "10y_futures_yield_pct"]
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow(dict(zip(fields, ["2026-10-21T14:59:30+00:00", "3.0", "3.3", "3.1", "3.4"])))
                writer.writerow(dict(zip(fields, ["2026-10-21T15:00:05+00:00", "3.0", "3.4", "3.1", "3.5"])))
                writer.writerow(dict(zip(fields, ["2026-10-21T15:00:55+00:00", "3.0", "3.5", "3.1", "3.6"])))
            rows = minute_spreads(path, date(2026, 10, 22))
            self.assertEqual(rows, [("2026-10-22T00:00", 50.0, 50.0, None, None)])
        finally:
            path.unlink(missing_ok=True)

    def test_gap_is_not_interpolated(self):
        path = Path(__file__).with_name(".test_spread_chart_2.csv")
        try:
            path.write_text("timestamp_utc,cash_spread_bp,futures_yield_spread_bp\n"
                            "2026-10-22T00:00:05+00:00,30,25\n"
                            "2026-10-22T00:02:05+00:00,,27\n", encoding="utf-8")
            rows = minute_spreads(path, date(2026, 10, 22))
            self.assertEqual(rows, [("2026-10-22T09:00", 30.0, 25.0, None, None),
                                    ("2026-10-22T09:02", None, 27.0, None, None)])
        finally:
            path.unlink(missing_ok=True)

    def test_foreign_net_is_aggregated_from_both_contracts(self):
        path = Path(__file__).with_name(".test_spread_chart_3.csv")
        try:
            path.write_text("timestamp_utc,3y_foreign_buy,3y_foreign_sell,10y_foreign_buy,10y_foreign_sell\n"
                            "2026-10-22T00:00:05+00:00,100,90,50,45\n", encoding="utf-8")
            rows = minute_spreads(path, date(2026, 10, 22))
            self.assertEqual(rows[0][3], 10.0)
            self.assertEqual(rows[0][4], 5.0)
        finally:
            path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
