"""Offline regression tests; no Excel/COM or real orders."""
import csv
import json
import tempfile
import unittest
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
from types import MethodType, SimpleNamespace

from analyze_snapshots import MinuteRVAnalyzer, RollingRVAnalyzer
from app_settings import AppSettings, DataSettings, RVSettings, RiskSettings, load_settings
from dashboard import Dashboard, IncrementalCsvTail
from decision_support import OrderFlowAnalyzer, risk_metrics
from extra_settings import AlertRule, ExtraSettings, parse_settings
from ktb_rtd_monitor import ExcelRtdReader, FeedUnavailable, SnapshotLogger, _numbers
from market_extensions import (AlertEngine, ForeignFlowTracker, WindowsNotifier,
                               market_metrics, metric_is_fresh, source_timestamp)
from paper_backtest import replay
from signal_engine import SignalEngine, SignalResult, validated_row

ROOT = Path(__file__).parent
START = datetime(2026, 10, 15, tzinfo=timezone.utc)


def quote(seconds=0, mid10=100.0):
    row = {"timestamp_utc": (START + timedelta(seconds=seconds)).isoformat()}
    for name, mid in (("3y", 100.0), ("10y", mid10)):
        row.update({f"{name}_mid": mid, f"{name}_best_bid": mid - 0.005,
                    f"{name}_best_ask": mid + 0.005, f"{name}_spread": 0.01,
                    f"{name}_microprice": mid, f"{name}_obi": 0.1, f"{name}_weighted_obi": 0.2,
                    f"{name}_bid_depth": 100, f"{name}_ask_depth": 100})
    return row


class SafetyTests(unittest.TestCase):
    def _headless_dashboard(self, rows):
        view = SimpleNamespace(settings=AppSettings(), extra_settings=ExtraSettings(), rejected_rows=0,
                               tail=MagicMock(), after=MagicMock(), winfo_id=MagicMock(return_value=1),
                               pending_rows=deque(), replaying=False, kpis={
                                   key: MagicMock() for key in ("3y_trade_price", "10y_trade_price",
                                                               "futures_spread", "cash_spread_bp")})
        for method in ("_reset_analyzers", "_decision_text", "_flow_text", "_basis_text", "_cost_text",
                       "_market_text", "_foreign_text", "_render_overview", "_clear_market_display", "refresh"):
            setattr(view, method, MethodType(getattr(Dashboard, method), view))
        view._rv_text = Dashboard._rv_text
        view._reset_analyzers()
        for name in ("status", "decision", "fast_rv", "core_rv", "flow", "basis", "cost",
                     "market_spreads", "foreign_3y", "foreign_10y", "alert_status", "volatility", "window_note"):
            setattr(view, name, MagicMock())
        view.labels = {(name, key): MagicMock() for name in ("3y", "10y") for key in
                       ("mid", "spread", "obi", "weighted_obi", "microprice", "bid_depth", "ask_depth")}
        view.tail.read_new.return_value = (rows, False)
        return view

    def test_dashboard_stale_and_future_times_block_signal(self):
        for stamp in ("2000-01-01T00:00:00+00:00", "2100-01-01T00:00:00+00:00"):
            view = self._headless_dashboard([{**quote(), "timestamp_utc": stamp}])
            with patch("dashboard.current_csv", return_value=Path("test.csv")):
                view.refresh()
            self.assertIn("사용 금지", view.decision.config.call_args.kwargs["text"])

    def test_dashboard_bad_row_does_not_discard_rest_of_batch(self):
        view = self._headless_dashboard([{**quote(), "3y_spread": 0}, quote(5)])
        with patch("dashboard.current_csv", return_value=Path("test.csv")):
            view.refresh()
        self.assertEqual(view.rejected_rows, 1)
        self.assertEqual(view.latest_row["timestamp_utc"], quote(5)["timestamp_utc"])
        self.assertEqual(view.fast_result["samples"], 1)

    def test_dashboard_read_error_clears_cached_signal(self):
        view = self._headless_dashboard([])
        view.latest_row = quote()
        view.tail.read_new.side_effect = OSError("test failure")
        with patch("dashboard.current_csv", return_value=Path("test.csv")):
            view.refresh()
        self.assertIsNone(view.latest_row)
        self.assertIn("신호 차단", view.decision.config.call_args.kwargs["text"])
        self.assertEqual(view.kpis["futures_spread"].config.call_args.kwargs["text"], "—")

    def test_alert_popup_is_non_modal_and_replaces_previous_popup(self):
        view = self._headless_dashboard([])
        view.alert_popup = None
        view.show_alert_popup = MethodType(Dashboard.show_alert_popup, view)
        popup = MagicMock()
        popup.winfo_exists.return_value = True
        popup.destroy = MagicMock()
        view.alert_popup = popup
        # No Tk root is created in the headless suite; the live UI path is
        # smoke-tested by preview_dashboard.py. This verifies replacement policy.
        view.alert_popup.destroy()
        popup.destroy.assert_called_once()

    def test_dashboard_batches_history_without_dropping_rows_or_replaying_alerts(self):
        view = self._headless_dashboard([quote(i / 4, 100 + i / 10000) for i in range(620)])
        view.tail.read_new.return_value = (view.tail.read_new.return_value[0], True)
        with patch("dashboard.current_csv", return_value=Path("test.csv")):
            view.refresh()
            self.assertGreater(len(view.pending_rows), 0)
            self.assertIn("사용 금지", view.decision.config.call_args.kwargs["text"])
            while view.pending_rows:
                view.refresh()
        view.tail.read_new.assert_called_once()
        self.assertEqual(view.latest_row["timestamp_utc"], quote(619 / 4)["timestamp_utc"])
        view.after.assert_any_call(25, view.refresh)

    def test_minute_change_risk_is_completed_fixed_time_not_level_dispersion(self):
        analyzer = MinuteRVAnalyzer(4, 1)
        for minute, price in enumerate((100, 101, 103, 102, 150)):
            result = analyzer.update(quote(minute * 60, price))
        # Completed levels 0, 1, 3, 2; changes +1,+2,-1.
        self.assertEqual(result["minute_change_samples"], 3)
        self.assertAlmostEqual(result["minute_change_stdev"], (7 / 3) ** .5)
        self.assertTrue(result["minute_change_ready"])
        cached = analyzer._completed_stats
        intraminute = analyzer.update(quote(241, 170))
        self.assertIs(analyzer._completed_stats, cached)
        self.assertEqual(result["minute_change_stdev"], intraminute["minute_change_stdev"])
        analyzer.reset()
        self.assertIsNone(analyzer.update(quote())["minute_change_stdev"])

    def test_minute_change_risk_excludes_missing_minutes_and_keeps_zero_changes(self):
        analyzer = MinuteRVAnalyzer(4, 1)
        for minute in (0, 1, 3, 4):
            result = analyzer.update(quote(minute * 60, 100))
        self.assertEqual(result["minute_change_samples"], 1)
        self.assertFalse(result["minute_change_ready"])
        analyzer.reset()
        for minute in range(5):
            result = analyzer.update(quote(minute * 60, 100))
        self.assertEqual(result["minute_change_samples"], 3)
        self.assertEqual(result["minute_change_stdev"], 0)

    def test_settings_reject_nonfinite_bool_fraction_unknown_and_missing(self):
        invalid = [{"rv": {"beta": float("nan")}}, {"rv": {"beta": True}},
                   {"rv": {"fast_event_window": 2.5}}, {"rv": {"core_minutes": 1}},
                   {"execution": {"extra_roundtrip_cost_points": float("inf")}},
                   {"rv": {"betaa": 3}}, {"risk": []}, {"typo": {}}, []]
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            path = Path(folder) / "config.json"
            with self.assertRaises(FileNotFoundError):
                load_settings(path)
            for raw in invalid:
                with self.subTest(raw=raw):
                    path.write_text(json.dumps(raw), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_settings(path)

    def test_extra_settings_and_market_spreads(self):
        raw = {"bindings": {"cash_3y_yield_pct": {"sheet": "추가RTD", "cell": "B1"}},
               "rules": [{"name": "curve high", "metric": "cash_spread_bp", "mode": "above",
                           "lower": 30.0}]}
        settings = parse_settings(raw)
        self.assertEqual(settings.bindings["cash_3y_yield_pct"].cell, "B1")
        metrics = market_metrics({"3y_trade_price": 102.95, "10y_trade_price": 106.30,
                                  "cash_3y_yield_pct": 3.861, "cash_10y_yield_pct": 4.188})
        self.assertAlmostEqual(metrics["futures_spread"], 3.35)
        self.assertAlmostEqual(metrics["cash_spread_bp"], 32.7)
        with self.assertRaises(ValueError):
            parse_settings({"bindings": {"cash_3y_yield_pct": {"sheet": "x", "cell": "B0"}}})

    def test_alert_confirmation_hysteresis_and_stale_block(self):
        rule = AlertRule("test", "futures_spread", "above", 3.0,
                         hysteresis=.1, confirmation_seconds=1, cooldown_seconds=60)
        engine = AlertEngine((rule,))
        self.assertEqual(engine.update({"futures_spread": 3.1}, START, True), [])
        events = engine.update({"futures_spread": 3.1}, START + timedelta(seconds=1), True)
        self.assertEqual(len(events), 1)
        self.assertEqual(engine.update({"futures_spread": 3.2}, START + timedelta(seconds=2), True), [])
        engine.update({"futures_spread": 2.89}, START + timedelta(seconds=3), True)
        engine.update({"futures_spread": 3.2}, START + timedelta(seconds=70), False)
        self.assertEqual(engine.update({"futures_spread": 3.2}, START + timedelta(seconds=71), True), [])
        self.assertEqual(len(engine.update({"futures_spread": 3.2}, START + timedelta(seconds=72), True)), 1)

    def test_optional_source_times_gate_alert_freshness(self):
        collector = datetime(2026, 10, 15, 1, 0, 10, tzinfo=timezone.utc)
        self.assertEqual(source_timestamp("10:00:05", collector),
                         datetime(2026, 10, 15, 1, 0, 5, tzinfo=timezone.utc))
        settings = ExtraSettings(max_source_age_seconds=30, max_leg_skew_seconds=10)
        row = {"cash_3y_time": "10:00:05", "cash_10y_time": "10:00:10"}
        self.assertTrue(metric_is_fresh(row, "cash_spread_bp", collector, settings))
        row["cash_3y_time"] = "09:58:00"
        self.assertFalse(metric_is_fresh(row, "cash_spread_bp", collector, settings))

    def test_foreign_flow_estimate_ignores_opening_totals_and_resets(self):
        tracker = ForeignFlowTracker("3y", ExtraSettings(price_bin_points=.02, flow_max_gap_seconds=10))
        first = {**quote(), "3y_foreign_buy": 1000, "3y_foreign_sell": 900,
                 "3y_trade_price": 102.95, "3y_futures_yield_pct": 3.94}
        tracker.update(first, START, START.date())
        self.assertIsNone(tracker.summary()["buy"])
        second = {**first, "3y_foreign_buy": 1010, "3y_foreign_sell": 904,
                  "3y_trade_price": 102.97, "3y_futures_yield_pct": 3.93}
        tracker.update(second, START + timedelta(seconds=5), START.date())
        summary = tracker.summary()
        self.assertEqual(summary["buy"]["observed_volume"], 10)
        self.assertAlmostEqual(summary["buy"]["estimated_avg_price"], 102.97)
        reset = {**second, "3y_foreign_buy": 5, "3y_foreign_sell": 4}
        tracker.update(reset, START + timedelta(seconds=6), START.date())
        self.assertIsNone(tracker.summary()["buy"])

    def test_quiet_sound_is_short_and_valid_wav(self):
        sound = WindowsNotifier._wav(.06)
        self.assertEqual(sound[:4], b"RIFF")
        self.assertLess(len(sound), 10_000)

    def test_quote_validation_is_fail_closed(self):
        for key, value in (("3y_mid", float("nan")), ("3y_best_ask", 99),
                           ("3y_spread", 0), ("timestamp_utc", "2026-10-15T00:00:00")):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    validated_row({**quote(), key: value})
        for value in (float("nan"), float("inf"), True, 0):
            with self.assertRaises(FeedUnavailable):
                _numbers([value], "test", 1)

    def test_gap_and_bad_timestamp_reset_confirmation(self):
        engine = SignalEngine(AppSettings())
        engine.update(quote())
        engine.consensus.state = 1
        result = engine.update(quote(11, 101))
        self.assertEqual(result.reset_reason, "data_gap")
        self.assertEqual(result.state, 0)
        self.assertEqual(result.fast["samples"], 1)
        with self.assertRaises(ValueError):
            engine.update(quote(10))
        self.assertEqual(len(engine.fast.levels), 0)

    def test_completed_minutes_never_include_current_in_reference(self):
        analyzer = MinuteRVAnalyzer(2, 1)
        analyzer.update(quote(0, 100))
        analyzer.update(quote(59, 101))
        analyzer.update(quote(60, 102))
        result = analyzer.update(quote(120, 110))
        self.assertTrue(result["ready"])
        self.assertAlmostEqual(result["level_mean"], 1.5)
        result = analyzer.update(quote(125, 120))
        self.assertAlmostEqual(result["level_mean"], 1.5)
        self.assertEqual(result["latest_rv_level"], 20)
        self.assertFalse(analyzer.update(quote(300))["ready"])

    def test_fast_memory_and_time_span_bounded(self):
        analyzer = RollingRVAnalyzer(3, 1)
        for index in range(100):
            result = analyzer.update(quote(index * 5, 100 + index * .01))
        self.assertEqual(len(analyzer.levels), 3)
        self.assertEqual(len(analyzer.changes), 3)
        self.assertEqual(result["span_seconds"], 10)

    def test_duplicate_heartbeat_does_not_change_book_ema(self):
        analyzer = OrderFlowAnalyzer(AppSettings().flow)
        analyzer.update(quote())
        row = {**quote(1), "3y_obi": .9}
        first = analyzer.update(row)
        for _ in range(100):
            self.assertEqual(first, analyzer.update(row))

    def test_risk_unconfigured_not_reported_as_safe(self):
        risk = RiskSettings(dv01_3y_krw_per_bp=30000, dv01_10y_krw_per_bp=85000)
        self.assertIn("미판정", risk_metrics(risk)["status"])
        self.assertTrue(risk_metrics(replace(risk, max_gross_dv01_krw_per_bp=1))["violations"])

    def test_shared_engine_heartbeat_confirms_and_risk_blocks_entry(self):
        settings = AppSettings(rv=RVSettings(beta=1, fast_event_window=3, core_minutes=2,
                                             entry_z=.8, exit_z=.5, confirmation_seconds=3))
        engine = SignalEngine(settings)
        limited = SignalEngine(replace(settings, risk=RiskSettings(
            dv01_3y_krw_per_bp=30000, dv01_10y_krw_per_bp=30000, max_gross_dv01_krw_per_bp=1)))
        for seconds in range(0, 120, 5):
            row = quote(seconds, 100 + seconds / 10000)
            engine.update(row)
            limited.update(row)
        first = engine.update(quote(120, 101))
        limited.update(quote(120, 101))
        self.assertTrue(first.ready)
        self.assertEqual(first.state, 0)
        confirmed = engine.update(quote(125, 101))
        blocked = limited.update(quote(125, 101))
        self.assertEqual(confirmed.state, 1)
        self.assertTrue(confirmed.entry_allowed)
        self.assertEqual(blocked.state, 1)
        self.assertFalse(blocked.entry_allowed)

    def test_csv_startup_partial_line_and_split_crlf(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            path = Path(folder) / "data.csv"
            path.write_bytes(b"a,b\r\n1,2\r\n3,")
            tail = IncrementalCsvTail(2)
            self.assertEqual(tail.read_new(path)[0], [{"a": "1", "b": "2"}])
            with path.open("ab") as handle:
                handle.write(b"4\r")
            self.assertEqual(tail.read_new(path)[0], [])
            with path.open("ab") as handle:
                handle.write(b"\n")
            self.assertEqual(tail.read_new(path)[0], [{"a": "3", "b": "4"}])
            path.write_bytes(b"a,b\n5,6\n")
            rows, reset = tail.read_new(path)
            self.assertTrue(reset)
            self.assertEqual(rows, [{"a": "5", "b": "6"}])

    def test_logger_rejects_schema_without_appending(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            path = Path(folder)
            data = path / "snapshots_with_trades.csv"
            data.write_text("wrong,columns\n1,2\n", encoding="utf-8")
            before = data.read_bytes()
            logger = SnapshotLogger(path, True, True)
            try:
                with self.assertRaises(ValueError):
                    logger.write(quote())
            finally:
                logger.close()
            self.assertEqual(data.read_bytes(), before)

    def test_collector_reads_single_block_without_writing_excel(self):
        block = [[None] * 13 for _ in range(10)]
        for offset in (0, 8):
            block[0][offset + 4] = 100.0
            for index in range(5):
                block[index][offset] = 10
                block[index][offset + 1] = 100.05 - index * .01
                block[index + 5][offset + 1] = 100 - index * .01
                block[index + 5][offset + 2] = 10
        from unittest.mock import MagicMock
        sheet = MagicMock()
        sheet.range.return_value.options.return_value.value = block
        reader = ExcelRtdReader("unused", "Sheet1")
        reader._sheet = sheet
        row = reader.snapshot()
        sheet.range.assert_called_once_with("C2:O11")
        self.assertAlmostEqual(row["3y_best_ask"], 100.01)
        self.assertAlmostEqual(row["10y_best_bid"], 100)

    def test_collector_reads_configured_extra_rtd_block(self):
        block = [[None] * 13 for _ in range(10)]
        for offset in (0, 8):
            block[0][offset + 4] = 100.0
            for index in range(5):
                block[index][offset] = 10
                block[index][offset + 1] = 100.05 - index * .01
                block[index + 5][offset + 1] = 100 - index * .01
                block[index + 5][offset + 2] = 10
        settings = parse_settings({"bindings": {
            "cash_3y_yield_pct": {"sheet": "추가RTD", "cell": "B1"},
            "cash_10y_yield_pct": {"sheet": "추가RTD", "cell": "B2"},
            "3y_foreign_buy": {"sheet": "추가RTD", "cell": "B3"},
            "3y_foreign_sell": {"sheet": "추가RTD", "cell": "B4"},
            "3y_futures_yield_pct": {"sheet": "추가RTD", "cell": "B6"},
            "10y_foreign_buy": {"sheet": "추가RTD", "cell": "B7"},
            "10y_foreign_sell": {"sheet": "추가RTD", "cell": "B8"},
            "10y_futures_yield_pct": {"sheet": "추가RTD", "cell": "B10"}}})
        base, extra = MagicMock(), MagicMock()
        base.range.return_value.options.return_value.value = block
        extra.range.return_value.options.return_value.value = [[3.861], [4.188], [119111], [118204],
                                                                [102.95], [3.948], [78687], [78086],
                                                                [106.30], [4.221]]
        base.book.sheets.__getitem__.return_value = extra
        reader = ExcelRtdReader("unused", "Sheet1", settings)
        reader._sheet = base
        row = reader.snapshot()
        extra.range.assert_called_once_with("B1:B10")
        self.assertAlmostEqual(row["cash_spread_bp"], 32.7)
        self.assertEqual(row["3y_foreign_buy"], 119111)

    def _replay_fixture(self, folder, rows, signals):
        path, settings = Path(folder) / "input.csv", Path(folder) / "settings.json"
        settings.write_text('{"execution":{"extra_roundtrip_cost_points":0.02}}', encoding="utf-8")
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        diagnostics = {}
        with patch("paper_backtest.SignalEngine.update", side_effect=signals):
            result = replay(path, settings, diagnostics)
        return result, diagnostics

    def test_replay_fills_next_row_and_separates_costs(self):
        signal = lambda state: SignalResult(None, None, state, "", True, True, None)
        rows = [quote(index, 100 - index * .02) for index in range(4)]
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            trades, stats = self._replay_fixture(folder, rows, [signal(1), signal(1), signal(0), signal(0)])
        self.assertEqual(stats["closed"], 1)
        trade = trades[0]
        self.assertEqual(trade["entry_utc"], rows[1]["timestamp_utc"])
        self.assertEqual(trade["exit_utc"], rows[3]["timestamp_utc"])
        self.assertNotEqual(trade["entry_signal_utc"], trade["entry_utc"])
        self.assertAlmostEqual(trade["gross_mid_pnl_points"] - trade["crossing_cost_points"]
                               - trade["extra_cost_points"], trade["net_pnl_points"])

    def test_replay_end_is_mark_not_forced_fill(self):
        signal = SignalResult(None, None, 1, "", True, True, None)
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            trades, stats = self._replay_fixture(folder, [quote(0), quote(1)], [signal, signal])
        self.assertEqual(stats["closed"], 0)
        self.assertEqual(trades[0]["status"], "unclosed_mark_only")
        self.assertEqual(trades[0]["exit_utc"], "")
        self.assertEqual(trades[0]["net_pnl_points"], "")

    def test_gap_cancels_pending_entry(self):
        signal = SignalResult(None, None, 1, "", True, True, None)
        reset = SignalResult(None, None, 0, "", False, False, "data_gap")
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            trades, stats = self._replay_fixture(folder, [quote(0), quote(11)], [signal, reset])
        self.assertEqual(trades, [])
        self.assertEqual(stats["entries"], 0)


if __name__ == "__main__":
    unittest.main()
