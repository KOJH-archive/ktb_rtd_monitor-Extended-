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
from cash_risk import CashDay, _changes, load_cash_days, oos_check, target_packages
from curve_context import CashCurveTracker
from app_settings import AppSettings, DataSettings, RVSettings, RiskSettings, load_settings
from dashboard import AlertEditor, Dashboard, ForeignHistoryReplay, IncrementalCsvTail, current_csv
from decision_support import OrderFlowAnalyzer, risk_metrics
from extra_settings import AlertRule, ExtraSettings, parse_settings
from ktb_rtd_monitor import (ExcelConnectionUnavailable, ExcelRtdReader, FeedUnavailable,
                             PollSchedule, SnapshotLogger, _numbers)
from market_extensions import (AlertEngine, ForeignFlowTracker, WindowsNotifier,
                               market_metrics, metric_is_fresh, source_timestamp)
from monitor_launcher import (AlreadyRunning, DATA_DIR, PYTHON, InstanceLock,
                              _stop, collector_command)
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
    def test_one_click_launcher_command_lock_and_shutdown(self):
        command = collector_command()
        self.assertEqual(command[0], str(PYTHON))
        self.assertEqual(command[command.index("--interval-ms") + 1], "1000")
        self.assertEqual(command[command.index("--output-dir") + 1], str(DATA_DIR))
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            lock_path = Path(folder) / "launcher.lock"
            with InstanceLock(lock_path):
                with self.assertRaises(AlreadyRunning):
                    with InstanceLock(lock_path):
                        pass
            with InstanceLock(lock_path):
                pass
        process = MagicMock()
        process.poll.return_value = None
        _stop(process)
        process.terminate.assert_called_once()
        process.wait.assert_called_once_with(timeout=5.0)

    def test_excel_connection_backoff_and_quote_error_cadence(self):
        cadence = PollSchedule(1000)
        self.assertEqual(cadence.normal_seconds, 1)
        self.assertEqual([cadence.failure(True) for _ in range(4)], [3, 4, 5, 5])
        self.assertEqual(cadence.failure(False), 1)
        self.assertEqual(cadence.failure(True), 3)
        self.assertEqual(cadence.success(), 1)
        self.assertEqual(cadence.failure(True), 3)

    def test_excel_com_failure_is_distinct_from_invalid_quote(self):
        reader = ExcelRtdReader("unused", "Sheet1")
        sheet = MagicMock()
        sheet.range.side_effect = RuntimeError("COM unavailable")
        reader._sheet = sheet
        with self.assertRaises(ExcelConnectionUnavailable):
            reader.snapshot()
        self.assertIsNone(reader._sheet)
        self.assertFalse(isinstance(FeedUnavailable("bad quote"), ExcelConnectionUnavailable))

    def test_alert_editor_deletes_selected_rules_and_saves_remaining_settings(self):
        for selected, expected in (((0, 2), ["b"]), ((0, 1, 2), [])):
            with self.subTest(selected=selected), tempfile.TemporaryDirectory(dir=ROOT) as folder:
                path = Path(folder) / "extra_settings.json"
                raw = {"sound_enabled": False, "rules": [
                    {"name": name, "metric": "cash_spread_bp", "mode": "above", "lower": 30}
                    for name in ("a", "b", "c")]}
                path.write_text(json.dumps(raw), encoding="utf-8")
                editor = SimpleNamespace(rules=list(parse_settings(raw).rules), listbox=MagicMock(),
                                         refresh_list=MagicMock(), dashboard=MagicMock(), destroy=MagicMock())
                editor.listbox.curselection.return_value = selected
                AlertEditor.delete_rule(editor)
                with patch("dashboard.EXTRA_SETTINGS_PATH", path):
                    AlertEditor.save(editor)
                saved = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual([rule.name for rule in parse_settings(saved).rules], expected)
                self.assertFalse(saved["sound_enabled"])
                editor.dashboard.reload_alerts.assert_called_once()

    def _headless_dashboard(self, rows):
        view = SimpleNamespace(settings=AppSettings(), extra_settings=ExtraSettings(), rejected_rows=0,
                               tail=MagicMock(), after=MagicMock(), winfo_id=MagicMock(return_value=1),
                               pending_rows=deque(), replaying=False, kpis={
                                   key: MagicMock() for key in ("3y_trade_price", "10y_trade_price",
                                                               "futures_yield_spread_bp", "cash_spread_bp")})
        for method in ("_reset_analyzers", "_ingest_foreign_row", "_restore_foreign_history",
                       "_decision_text", "_flow_text", "_basis_text", "_cost_text",
                       "_market_text", "_foreign_text", "_curve_text", "_sizing_text", "_render_overview",
                       "_clear_market_display", "refresh"):
            setattr(view, method, MethodType(getattr(Dashboard, method), view))
        view._rv_text = Dashboard._rv_text
        view._reset_analyzers()
        for name in ("status", "decision", "fast_rv", "core_rv", "flow", "basis", "cost",
                     "market_spreads", "foreign_3y", "foreign_10y", "alert_status", "volatility",
                     "window_note", "curve_position", "curve_trend", "sizing"):
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
            self.assertEqual(view.kpis["3y_trade_price"].config.call_args.kwargs["text"], "—")

    def test_dashboard_bad_row_does_not_discard_rest_of_batch(self):
        view = self._headless_dashboard([{**quote(), "3y_spread": 0}, quote(5)])
        with patch("dashboard.current_csv", return_value=Path("test.csv")):
            view.refresh()
        self.assertEqual(view.rejected_rows, 1)
        self.assertEqual(view.latest_row["timestamp_utc"], quote(5)["timestamp_utc"])
        self.assertEqual(view.fast_result["samples"], 1)

    def test_fresh_dashboard_accepts_all_derived_metrics(self):
        row = {**quote(), "timestamp_utc": datetime.now(timezone.utc).isoformat(),
               "3y_trade_price": 103.06, "10y_trade_price": 106.60,
               "cash_3y_yield_pct": 3.5, "cash_10y_yield_pct": 3.83,
               "3y_futures_yield_pct": 3.5, "10y_futures_yield_pct": 3.776}
        view = self._headless_dashboard([row])
        with patch("dashboard.current_csv", return_value=Path("test.csv")):
            view.refresh()
        self.assertIsNotNone(view.latest_row)
        self.assertEqual(view.rejected_rows, 0)
        self.assertEqual(view.kpis["futures_yield_spread_bp"].config.call_args.kwargs["text"], "+27.6")
        stamp = datetime.fromisoformat(row["timestamp_utc"])
        self.assertTrue(metric_is_fresh(row, "futures_yield_spread_bp", stamp, ExtraSettings()))
        self.assertFalse(metric_is_fresh({**row, "3y_trade_time": (stamp - timedelta(minutes=1)).isoformat(),
                                        "10y_trade_time": stamp.isoformat()},
                                       "futures_yield_spread_bp", stamp, ExtraSettings()))
        self.assertFalse(metric_is_fresh(row, "unknown_metric", stamp, ExtraSettings()))

    def test_stale_foreign_source_discards_rolling_history(self):
        initial = {**quote(), "3y_trade_price": 105.0,
                   "3y_foreign_buy": 100, "3y_foreign_sell": 100}
        stale = {**quote(5), "3y_trade_price": 105.1,
                 "3y_foreign_buy": 110, "3y_foreign_sell": 100,
                 "3y_foreign_time": "2026-10-14T00:00:00+00:00"}
        view = self._headless_dashboard([initial, stale])
        with patch("dashboard.current_csv", return_value=Path("test.csv")):
            view.refresh()
        self.assertEqual(view.foreign_trackers["3y"].summary()["windows"], {})

    def test_dashboard_read_error_clears_cached_signal(self):
        view = self._headless_dashboard([])
        view.latest_row = quote()
        view.tail.read_new.side_effect = OSError("test failure")
        with patch("dashboard.current_csv", return_value=Path("test.csv")):
            view.refresh()
        self.assertIsNone(view.latest_row)
        self.assertIn("신호 차단", view.decision.config.call_args.kwargs["text"])
        self.assertEqual(view.kpis["futures_yield_spread_bp"].config.call_args.kwargs["text"], "—")

    def test_alert_popup_is_non_modal_and_replaces_previous_popup(self):
        view = self._headless_dashboard([])
        previous = MagicMock()
        view.alert_popup = previous
        popup = MagicMock()
        popup.winfo_exists.return_value = True
        popup.winfo_width.return_value = 380
        popup.winfo_height.return_value = 180
        popup.winfo_screenheight.return_value = 1080
        popup.after.return_value = "blink-job"
        with patch("dashboard.tk.Toplevel", return_value=popup), patch("dashboard.tk.Frame"), \
                patch("dashboard.tk.Label"), patch("dashboard.tk.Button"):
            Dashboard.show_alert_popup(view, SimpleNamespace(name="test", value=33.0))
        previous.destroy.assert_called_once()
        popup.geometry.assert_called_once_with("380x180+28+830")
        popup.grab_set.assert_not_called()
        popup.focus_force.assert_not_called()
        delay, callback = popup.after.call_args.args
        self.assertEqual(delay, 700)
        callback()
        self.assertEqual(popup.after.call_count, 2)
        popup.destroy.assert_not_called()
        cleanup = popup.bind.call_args.args[1]
        cleanup(SimpleNamespace(widget=popup))
        popup.after_cancel.assert_called_once_with("blink-job")
        self.assertIsNone(view.alert_popup)

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
                                  "cash_3y_yield_pct": 3.861, "cash_10y_yield_pct": 4.188,
                                  "3y_futures_yield_pct": 3.934, "10y_futures_yield_pct": 4.221})
        self.assertAlmostEqual(metrics["futures_spread"], 3.35)
        self.assertAlmostEqual(metrics["cash_spread_bp"], 32.7)
        self.assertAlmostEqual(metrics["futures_yield_spread_bp"], 28.7)
        self.assertNotIn("futures_yield_spread_bp", market_metrics({"3y_futures_yield_pct": 3.934}))
        self.assertNotIn("cash_spread_bp", market_metrics({"cash_3y_yield_pct": 0,
                                                           "cash_10y_yield_pct": 4.188}))
        self.assertNotIn("futures_yield_spread_bp", market_metrics({"3y_futures_yield_pct": 0,
                                                                    "10y_futures_yield_pct": 4.221}))
        view = self._headless_dashboard([])
        view._render_overview({"3y_futures_yield_pct": 3.934,
                               "10y_futures_yield_pct": 4.221}, True)
        self.assertEqual(view.kpis["futures_yield_spread_bp"].config.call_args.kwargs["text"], "+28.7")
        view._render_overview({"cash_3y_yield_pct": 0, "cash_10y_yield_pct": 4.188}, True)
        self.assertEqual(view.kpis["cash_spread_bp"].config.call_args.kwargs["text"], "N/A")
        self.assertEqual(view.kpis["futures_yield_spread_bp"].config.call_args.kwargs["text"], "N/A")
        with self.assertRaises(ValueError):
            parse_settings({"bindings": {"cash_3y_yield_pct": {"sheet": "x", "cell": "B0"}}})

    def test_alert_confirmation_hysteresis_and_stale_block(self):
        rule = AlertRule("test", "futures_spread", "above", 3.0,
                         hysteresis=.1, confirmation_seconds=1, cooldown_seconds=0)
        engine = AlertEngine((rule,))
        self.assertEqual(engine.update({"futures_spread": 3.1}, START, True), [])
        events = engine.update({"futures_spread": 3.1}, START + timedelta(seconds=1), True)
        self.assertEqual(len(events), 1)
        self.assertEqual(engine.update({"futures_spread": 3.2}, START + timedelta(seconds=2), True), [])
        engine.update({"futures_spread": 2.89}, START + timedelta(seconds=3), True)
        engine.update({"futures_spread": 3.2}, START + timedelta(seconds=4), False)
        self.assertEqual(engine.update({"futures_spread": 3.2}, START + timedelta(seconds=5), True), [])
        self.assertEqual(len(engine.update({"futures_spread": 3.2}, START + timedelta(seconds=6), True)), 1)

    def test_missing_yield_resets_alert_confirmation(self):
        rule = AlertRule("cash", "cash_spread_bp", "above", 30,
                         confirmation_seconds=1, cooldown_seconds=0)
        engine = AlertEngine((rule,))
        self.assertEqual(engine.update({"cash_spread_bp": 32}, START, True), [])
        self.assertEqual(engine.update({}, START + timedelta(seconds=1), True), [])
        self.assertEqual(engine.update({"cash_spread_bp": 32}, START + timedelta(seconds=2), True), [])
        self.assertEqual(len(engine.update({"cash_spread_bp": 32}, START + timedelta(seconds=3), True)), 1)

    def test_futures_yield_alert_configuration_and_bp_trigger(self):
        settings = parse_settings({"rules": [{"name": "yield curve", "metric": "futures_yield_spread_bp",
                                               "mode": "above", "lower": 28,
                                               "confirmation_seconds": 1, "cooldown_seconds": 0,
                                               "hysteresis": .5}]})
        engine = AlertEngine(settings.rules)
        metrics = market_metrics({"3y_futures_yield_pct": 3.934, "10y_futures_yield_pct": 4.221})
        self.assertEqual(engine.update(metrics, START, True), [])
        events = engine.update(metrics, START + timedelta(seconds=1), True)
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0].value, 28.7)
        engine.update({"futures_yield_spread_bp": 27.4}, START + timedelta(seconds=2), True)
        self.assertEqual(engine.update(metrics, START + timedelta(seconds=3), True), [])
        self.assertEqual(len(engine.update(metrics, START + timedelta(seconds=4), True)), 1)

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

    def test_foreign_gap_preserves_old_zones_but_skips_unpriced_volume(self):
        tracker = ForeignFlowTracker("3y", ExtraSettings(flow_max_gap_seconds=10))
        base = {"3y_foreign_buy": 100, "3y_foreign_sell": 100, "3y_trade_price": 105}
        tracker.update(base, START, START.date())
        tracker.update({**base, "3y_foreign_buy": 110}, START + timedelta(seconds=5), START.date())
        tracker.update({**base, "3y_foreign_buy": 160, "3y_trade_price": 106},
                       START + timedelta(seconds=35), START.date())
        self.assertEqual(tracker.summary()["buy"]["observed_volume"], 10)
        self.assertFalse(tracker.summary()["windows"][5]["complete"])
        tracker.update({**base, "3y_foreign_buy": 180, "3y_trade_price": 106},
                       START + timedelta(seconds=40), START.date())
        self.assertEqual(tracker.summary()["buy"]["observed_volume"], 30)
        self.assertIsNone(tracker.summary()["sell"])
        tracker.pause()
        tracker.update({**base, "3y_foreign_buy": 5}, START + timedelta(seconds=45), START.date())
        self.assertIsNone(tracker.summary()["buy"])

    def test_dashboard_restores_only_today_foreign_flow_without_alert_replay(self):
        start = datetime.now(timezone.utc) - timedelta(seconds=90)
        if (start + timedelta(seconds=50)).astimezone(timezone(timedelta(hours=9))).date() != \
                start.astimezone(timezone(timedelta(hours=9))).date():
            self.skipTest("KST day boundary")
        base = {**quote(), "timestamp_utc": start.isoformat(),
                "3y_trade_price": 105, "10y_trade_price": 106,
                "3y_foreign_buy": 100, "3y_foreign_sell": 100,
                "10y_foreign_buy": 200, "10y_foreign_sell": 200}
        rows = [base,
                {**base, "timestamp_utc": (start + timedelta(seconds=5)).isoformat(),
                 "3y_foreign_buy": 110},
                {**base, "timestamp_utc": (start + timedelta(seconds=40)).isoformat(),
                 "3y_foreign_buy": 160, "3y_trade_price": 106},
                {**base, "timestamp_utc": (start + timedelta(seconds=45)).isoformat(),
                 "3y_foreign_buy": 180, "3y_trade_price": 106}]
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            path = Path(folder) / "today.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(base))
                writer.writeheader()
                writer.writerows(rows)
            view = self._headless_dashboard([])
            view.tail = IncrementalCsvTail()
            with patch("dashboard.current_csv", return_value=path):
                view.refresh()
                self.assertIsInstance(view.flow_history, ForeignHistoryReplay)
                view.alert_engine.update = MagicMock(return_value=[])
                with path.open("a", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(base))
                    writer.writerow({**base, "timestamp_utc": (start + timedelta(seconds=50)).isoformat(),
                                     "3y_foreign_buy": 190, "3y_trade_price": 106})
                while view.flow_history is not None:
                    view.refresh()
                self.assertEqual(view.foreign_trackers["3y"].summary()["buy"]["observed_volume"], 40)
                view.alert_engine.update.assert_not_called()
                self.assertIsNotNone(view.latest_row)
                with path.open("a", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(base))
                    writer.writerow({**base, "timestamp_utc": (start + timedelta(seconds=55)).isoformat(),
                                     "3y_foreign_buy": 200, "3y_trade_price": 106})
                view.refresh()
                self.assertEqual(view.foreign_trackers["3y"].summary()["buy"]["observed_volume"], 50)
                self.assertIsNotNone(view.latest_row)
                view.alert_engine.update.assert_called_once()

    def test_foreign_rolling_windows_exclude_old_flow_and_flag_partial_history(self):
        tracker = ForeignFlowTracker("3y", ExtraSettings())
        base = {**quote(), "3y_foreign_buy": 100, "3y_foreign_sell": 100,
                "3y_trade_price": 105.0}
        tracker.update(base, START, "session")
        for step in range(1, 721):
            row = dict(base)
            row["3y_foreign_buy"] = 100 + (10 if step >= 1 else 0) + (20 if step >= 719 else 0)
            row["3y_foreign_sell"] = 100 + (50 if step >= 599 else 0)
            row["3y_trade_price"] = 105.0 if step < 600 else 105.5
            tracker.update(row, START + timedelta(seconds=step * 5), "session")
            if step == 60:
                partial = tracker.summary()["windows"]
                self.assertFalse(partial[15]["complete"])
        windows = tracker.summary()["windows"]
        self.assertTrue(windows[60]["complete"])
        self.assertEqual(windows[5]["net_volume"], 20)
        self.assertEqual(windows[15]["net_volume"], -30)
        self.assertEqual(windows[60]["net_volume"], -20)
        self.assertEqual(windows[5]["buy_avg_price"], 105.5)
        self.assertIsNone(windows[5]["sell_avg_price"])
        self.assertEqual(windows[15]["sell_avg_price"], 105.0)
        self.assertLessEqual(len(tracker.windows[60].buckets), 3601)
        view = self._headless_dashboard([])
        view.foreign_trackers["3y"] = tracker
        display = view._foreign_text("3y", row)
        self.assertIn("최근 5분", display)
        self.assertIn("최근 1분 수급 속도", display)
        self.assertIn("가격 정체", display)
        tracker.update({**row, "3y_foreign_buy": 2}, START + timedelta(seconds=3605), "session")
        self.assertEqual(tracker.summary()["windows"][5]["net_volume"], 0)
        tracker.update({**row, "3y_trade_price": ""}, START + timedelta(seconds=3610), "session")
        self.assertEqual(tracker.summary()["windows"], {})

    def test_cash_curve_uses_completed_minutes_and_resets_missing_feed(self):
        curve = CashCurveTracker()
        for minute in range(61):
            row = {"timestamp_utc": (START + timedelta(minutes=minute)).isoformat(),
                   "cash_3y_yield_pct": 3.0, "cash_10y_yield_pct": 3.30 + minute * .001}
            curve.update(row)
            if minute == 5:
                self.assertIsNotNone(curve.summary()["location_5m"])
                self.assertIsNone(curve.summary()["location_60m"])
        summary = curve.summary()
        self.assertAlmostEqual(summary["spread_bp"], 36.0)
        self.assertAlmostEqual(summary["change_5m_bp"], .5)
        self.assertAlmostEqual(summary["change_15m_bp"], 1.5)
        self.assertIsNotNone(summary["location_60m"])
        view = self._headless_dashboard([])
        view.curve_tracker = curve
        position, trend = view._curve_text()
        self.assertIn("60분 평균", position)
        self.assertIn("확대 동행", trend)
        curve.update({**row, "cash_10y_yield_pct": ""})
        self.assertIsNone(curve.summary())

    def test_cash_risk_proxy_uses_dv01_and_never_future_data_for_sizing(self):
        from datetime import date
        points = [CashDay(date(2024, 1, 1) + timedelta(days=i), 300.0,
                          330.0 + (i % 5) * .1) for i in range(80)]
        risk = RiskSettings(dv01_3y_krw_per_bp=30_000, dv01_10y_krw_per_bp=90_000,
                            capital_krw=1_000_000_000, annual_target_vol_fraction=.1,
                            max_package_count=4, max_gross_dv01_krw_per_bp=720_000,
                            max_residual_dv01_krw_per_bp=1)
        self.assertAlmostEqual(_changes(points[:2], risk)[0]["package_proxy_pnl_krw"], -9_000)
        baseline = target_packages(points, risk, points[-1].day)
        self.assertEqual(baseline["status"], "research_proxy")
        self.assertLessEqual(baseline["target_packages"], 4)
        self.assertLessEqual(target_packages(points, replace(risk, max_gross_dv01_krw_per_bp=360_000),
                                             points[-1].day)["target_packages"], 2)
        future = CashDay(points[-1].day + timedelta(days=1), 300.0, 900.0)
        self.assertEqual(target_packages(points + [future], risk, points[-1].day), baseline)
        self.assertEqual(target_packages(points, replace(risk, capital_krw=None), points[-1].day)
                         ["status"], "missing_settings")

    def test_cash_curve_oos_is_chronological_and_requires_long_history(self):
        from datetime import date
        points = [CashDay(date(2024, 1, 1) + timedelta(days=i), 300.0,
                          330.0 + (i % 7) * .1) for i in range(340)]
        cutoff = points[275].day
        result = oos_check(points, cutoff)
        self.assertEqual(result["status"], "evaluated")
        self.assertEqual(result["train_days"], 274)
        self.assertEqual(result["test_days"], 65)
        self.assertEqual(oos_check(points[:200], cutoff)["status"], "insufficient_history")
        self.assertGreater(result["first_test_forecast_sigma_bp"], 0)
        changed_future = points[:275] + [CashDay(item.day, item.three_bp, item.ten_bp + 30)
                                          for item in points[275:]]
        self.assertEqual(oos_check(changed_future, cutoff)["first_test_forecast_sigma_bp"],
                         result["first_test_forecast_sigma_bp"])

    def test_cash_daily_loader_rejects_duplicate_dates(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            path = Path(folder) / "cash.csv"
            path.write_text("date,cash_3y_yield_pct,cash_10y_yield_pct\n"
                            "2024-01-02,3.0,3.3\n2024-01-03,3.1,3.4\n", encoding="utf-8")
            self.assertAlmostEqual(load_cash_days(path)[-1].spread_bp, 30.0)
            path.write_text(path.read_text(encoding="utf-8") + "2024-01-03,3.1,3.4\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_cash_days(path)

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
            tail = IncrementalCsvTail()
            self.assertEqual(tail.read_new(path)[0], [])
            with path.open("ab") as handle:
                handle.write(b"4\r")
            self.assertEqual(tail.read_new(path)[0], [])
            with path.open("ab") as handle:
                handle.write(b"\n")
            self.assertEqual(tail.read_new(path)[0], [{"a": "3", "b": "4"}])
            path.write_bytes(b"a,b\n5,6\n")
            rows, reset = tail.read_new(path)
            self.assertTrue(reset)
            self.assertEqual(rows, [])

    def test_dashboard_ignores_undated_old_log(self):
        from analyze_snapshots import timestamp_kst
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            data_dir = Path(folder)
            (data_dir / "snapshots_enhanced.csv").write_text("old\n", encoding="utf-8")
            day = timestamp_kst(datetime.now(timezone.utc)).strftime("%Y%m%d")
            today = data_dir / f"snapshots_enhanced_{day}.csv"
            with patch("dashboard.DATA_DIR", data_dir):
                self.assertEqual(current_csv(), today)
                today.write_text("new\n", encoding="utf-8")
                self.assertEqual(current_csv(), today)

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
        reader._extra_snapshot = MagicMock(return_value={"3y_futures_yield_pct": "",
                                                         "10y_futures_yield_pct": ""})
        row = reader.snapshot()
        sheet.range.assert_called_once_with("C2:O11")
        self.assertAlmostEqual(row["3y_best_ask"], 100.01)
        self.assertAlmostEqual(row["10y_best_bid"], 100)
        self.assertIn("futures_yield_spread_bp", row)
        self.assertEqual(row["futures_yield_spread_bp"], "")
        reader._extra_snapshot = MagicMock(return_value={"3y_futures_yield_pct": 3.5,
                                                         "10y_futures_yield_pct": 3.8})
        with_yields = reader.snapshot()
        self.assertEqual(list(row), list(with_yields))
        self.assertAlmostEqual(with_yields["futures_yield_spread_bp"], 30.0)

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
        extra.range.return_value.options.return_value.value = [[0], [4.188], [0], [118204],
                                                                [102.95], [0], [78687], [78086],
                                                                [106.30], [4.221]]
        missing = reader.snapshot()
        self.assertEqual(list(row), list(missing))
        self.assertEqual(missing["cash_3y_yield_pct"], "")
        self.assertEqual(missing["3y_futures_yield_pct"], "")
        self.assertEqual(missing["3y_foreign_buy"], 0)
        self.assertEqual(missing["cash_spread_bp"], "")
        self.assertEqual(missing["futures_yield_spread_bp"], "")
        self.assertAlmostEqual(missing["3y_trade_price"], 100)
        extra.range.return_value.options.return_value.value = [[3.861], [4.188], [119111], [118204],
                                                                [102.95], [3.948], [78687], [78086],
                                                                [106.30], [4.221]]
        recovered = reader.snapshot()
        self.assertAlmostEqual(recovered["cash_spread_bp"], 32.7)
        self.assertAlmostEqual(recovered["futures_yield_spread_bp"], 27.3)
        block[0][4] = 0
        no_trade = reader.snapshot()
        self.assertEqual(no_trade["3y_trade_price"], "")
        self.assertEqual(no_trade["futures_spread"], "")
        self.assertGreater(no_trade["3y_mid"], 0)
        self.assertNotIn("3y_trade_price", market_metrics({"3y_trade_price": 0}))
        view = self._headless_dashboard([])
        view._render_overview(no_trade, True)
        self.assertEqual(view.kpis["3y_trade_price"].config.call_args.kwargs["text"], "N/A")
        with tempfile.TemporaryDirectory(dir=ROOT) as folder:
            logger = SnapshotLogger(Path(folder), csv_enabled=True, sqlite_enabled=False)
            for sample in (missing, recovered, no_trade):
                logger.write(sample)
            logger.close()
            with logger.csv_path.open(newline="", encoding="utf-8-sig") as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 3)

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
