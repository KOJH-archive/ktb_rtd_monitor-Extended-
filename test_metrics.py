import tempfile
import unittest
from pathlib import Path

from ktb_rtd_monitor import SnapshotThrottle, _metrics
from analyze_snapshots import ConfirmedConsensus, MinuteRVAnalyzer, RollingRVAnalyzer, analyze, consensus_decision
from app_settings import (AppSettings, BasisSettings, ExecutionSettings, FlowSettings, RVSettings, RiskSettings,
                          dv01_package, effective_beta, load_settings)
from decision_support import (OrderFlowAnalyzer, basis_metrics, executable_package_pnl,
                              execution_gate, risk_metrics)
from dashboard import IncrementalCsvTail


class MetricTests(unittest.TestCase):
    def test_metrics_reverse_excel_ask_order(self):
        row = _metrics("x", [104.75, 104.74, 104.73, 104.72, 104.71], [99, 103, 163, 106, 87],
                       [104.80, 104.79, 104.78, 104.77, 104.76], [26, 17, 14, 10, 1])
        self.assertEqual(row["x_best_bid"], 104.75)
        self.assertEqual(row["x_best_ask"], 104.76)
        self.assertAlmostEqual(row["x_mid"], 104.755)
        self.assertAlmostEqual(row["x_microprice"], 104.7599)
        self.assertGreater(row["x_obi"], 0)
        self.assertGreater(row["x_weighted_obi"], 0)

    def test_relative_value_analysis(self):
        rows = [{"timestamp_utc": "t1", "3y_mid": 100.0, "10y_mid": 100.0},
                {"timestamp_utc": "t2", "3y_mid": 100.1, "10y_mid": 100.4},
                {"timestamp_utc": "t3", "3y_mid": 100.2, "10y_mid": 100.8}]
        result = analyze(rows, window=2, beta=2.0)
        self.assertIsNotNone(result)
        self.assertAlmostEqual(result["latest_rv_change"], 0.2)

    def test_incremental_analysis_ignores_unchanged_books(self):
        analyzer = RollingRVAnalyzer(window=3, beta=2.0, min_samples=2)
        analyzer.update({"timestamp_utc": "t1", "3y_mid": 100.0, "10y_mid": 100.0})
        analyzer.update({"timestamp_utc": "t2", "3y_mid": 100.0, "10y_mid": 100.0})
        result = analyzer.update({"timestamp_utc": "t3", "3y_mid": 100.1, "10y_mid": 100.4})
        self.assertEqual(result["samples"], 2)
        self.assertAlmostEqual(result["latest_rv_change"], 0.2)

    def test_minute_analyzer_requires_full_time_window(self):
        analyzer = MinuteRVAnalyzer(minutes=3, beta=2.0)
        analyzer.update({"timestamp_utc": "2026-10-15T00:00:10+00:00", "3y_mid": 100.0, "10y_mid": 100.0})
        analyzer.update({"timestamp_utc": "2026-10-15T00:01:10+00:00", "3y_mid": 100.0, "10y_mid": 100.1})
        self.assertFalse(analyzer.result()["ready"])
        result = analyzer.update({"timestamp_utc": "2026-10-15T00:02:10+00:00", "3y_mid": 100.0, "10y_mid": 100.3})
        self.assertFalse(result["ready"])
        result = analyzer.update({"timestamp_utc": "2026-10-15T00:03:00+00:00", "3y_mid": 100.0, "10y_mid": 100.4})
        self.assertTrue(result["ready"])
        self.assertEqual(result["samples"], 3)

    def test_minute_analyzer_resets_at_kst_session_boundary(self):
        analyzer = MinuteRVAnalyzer(minutes=3, beta=2.0, min_samples=2)
        analyzer.update({"timestamp_utc": "2026-10-15T14:59:00+00:00", "3y_mid": 100.0, "10y_mid": 100.0})
        result = analyzer.update({"timestamp_utc": "2026-10-15T15:01:00+00:00", "3y_mid": 100.0, "10y_mid": 100.1})
        self.assertEqual(result["samples"], 0)
        self.assertFalse(result["ready"])

    def test_consensus_uses_entry_and_lower_exit_thresholds(self):
        state, _ = consensus_decision(2.1, 2.2, 0, entry_z=2.0, exit_z=1.5)
        self.assertEqual(state, 1)
        state, _ = consensus_decision(1.7, 1.8, state, entry_z=2.0, exit_z=1.5)
        self.assertEqual(state, 1)
        state, text = consensus_decision(1.4, 1.8, state, entry_z=2.0, exit_z=1.5)
        self.assertEqual(state, 0)
        self.assertEqual(text, "중립/관찰")

    def test_consensus_requires_market_time_confirmation(self):
        consensus = ConfirmedConsensus(entry_z=2.0, exit_z=1.5, confirmation_seconds=3.0)
        state, _ = consensus.update("2026-10-15T00:00:00+00:00", 2.2, 2.1)
        self.assertEqual(state, 0)
        state, _ = consensus.update("2026-10-15T00:00:02+00:00", 2.2, 2.1)
        self.assertEqual(state, 0)
        state, _ = consensus.update("2026-10-15T00:00:03+00:00", 2.2, 2.1)
        self.assertEqual(state, 1)

    def test_dv01_package_reports_integer_residual(self):
        risk = RiskSettings(dv01_3y_krw_per_bp=30_000,
                            dv01_10y_krw_per_bp=85_000,
                            package_10y_contracts=1)
        result = dv01_package(risk)
        self.assertAlmostEqual(result["exact_3y"], 85 / 30)
        self.assertEqual(result["rounded_3y"], 3)
        self.assertEqual(result["residual_dv01"], 5_000)
        self.assertAlmostEqual(effective_beta(AppSettings(rv=RVSettings(beta=2.88), risk=risk)), 85 / 30)

    def test_default_settings_file(self):
        settings = load_settings(Path(__file__).parent / "settings.json")
        self.assertEqual(settings.rv.fast_event_window, 120)
        self.assertEqual(settings.rv.core_minutes, 60)
        self.assertIsNone(settings.risk.dv01_3y_krw_per_bp)

    def test_order_flow_relative_score(self):
        row = {}
        for name, sign in (("3y", -1), ("10y", 1)):
            row.update({f"{name}_spread": 0.02, f"{name}_mid": 100.0,
                        f"{name}_microprice": 100.0 + sign * 0.005,
                        f"{name}_obi": sign * 0.4, f"{name}_weighted_obi": sign * 0.5})
        result = OrderFlowAnalyzer(FlowSettings(ema_alpha=1.0)).update(row)
        self.assertGreater(result["relative_score"], 0)
        self.assertEqual(result["state"], "10Y 상대 매수호가잔량 우위")

    def test_basis_deducts_crossing_and_extra_cost(self):
        row = {"3y_mid": 105.02, "3y_spread": 0.02}
        result = basis_metrics(row, BasisSettings(fair_3y_price=105.0,
                                                   extra_cost_3y_points=0.005,
                                                   min_net_edge_points=0.001))[0]
        self.assertAlmostEqual(result["basis"], 0.02)
        self.assertAlmostEqual(result["net_edge"], 0.005)
        self.assertEqual(result["state"], "입력 적정가 상회")

    def test_risk_metrics_enforces_configured_limits(self):
        risk = RiskSettings(dv01_3y_krw_per_bp=30_000, dv01_10y_krw_per_bp=85_000,
                            max_gross_dv01_krw_per_bp=170_000,
                            max_residual_dv01_krw_per_bp=4_000)
        result = risk_metrics(risk)
        self.assertIn("총 DV01 한도 초과", result["status"])
        self.assertIn("잔여 DV01 한도 초과", result["status"])

    def test_executable_package_pnl_includes_crossing(self):
        entry = {"10y_best_bid": 100.00, "10y_best_ask": 100.02,
                 "3y_best_bid": 100.00, "3y_best_ask": 100.01}
        exit_row = {"10y_best_bid": 99.96, "10y_best_ask": 99.98,
                    "3y_best_bid": 100.00, "3y_best_ask": 100.01}
        self.assertAlmostEqual(executable_package_pnl(entry, exit_row, signal_state=1, beta=2.0), 0.0)

    def test_execution_gate_requires_edge_above_roundtrip_cost(self):
        row = {"10y_spread": 0.01, "3y_spread": 0.01}
        core = {"latest_rv_level": 1.05, "level_mean": 1.00}
        result = execution_gate(row, core, beta=2.0,
                                settings=ExecutionSettings(min_edge_to_roundtrip_cost=1.25))
        self.assertAlmostEqual(result["roundtrip_cost"], 0.03)
        self.assertAlmostEqual(result["required_edge"], 0.0375)
        self.assertTrue(result["allowed"])

    def test_snapshot_throttle_keeps_changes_and_heartbeats(self):
        throttle = SnapshotThrottle(heartbeat_seconds=5.0)
        first = {"timestamp_utc": "t1", "3y_mid": 100.0}
        same = {"timestamp_utc": "t2", "3y_mid": 100.0}
        changed = {"timestamp_utc": "t3", "3y_mid": 100.1}
        self.assertTrue(throttle.should_write(first, now=10.0))
        self.assertFalse(throttle.should_write(same, now=11.0))
        self.assertTrue(throttle.should_write(same, now=15.0))
        self.assertTrue(throttle.should_write(changed, now=15.1))

    def test_csv_tail_skips_history_then_reads_only_appends(self):
        header = "timestamp_utc,3y_mid,10y_mid\n"
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            path = Path(directory) / "snapshots.csv"
            path.write_text(header + "t1,100,101\n" + "t2,100.1,101.2\n", encoding="utf-8")
            tail = IncrementalCsvTail()
            rows, reset = tail.read_new(path)
            self.assertTrue(reset)
            self.assertEqual(rows, [])
            with path.open("a", encoding="utf-8") as handle:
                handle.write("t3,100.2,101.4\n")
            rows, reset = tail.read_new(path)
            self.assertFalse(reset)
            self.assertEqual([row["timestamp_utc"] for row in rows], ["t3"])


if __name__ == "__main__":
    unittest.main()
