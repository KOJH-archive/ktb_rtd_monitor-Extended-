"""Daily cash-yield risk proxy and chronological out-of-sample checks.

The proxy uses user-supplied futures DV01s and cash-yield changes. It is not
observed futures P&L, a trading signal, or an order quantity.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from app_settings import RiskSettings, dv01_package, load_settings

REQUIRED_COLUMNS = ("date", "cash_3y_yield_pct", "cash_10y_yield_pct")
LONG_WINDOW = 60
SHORT_WINDOW = 20
TRADING_DAYS = 252


@dataclass(frozen=True)
class CashDay:
    day: date
    three_bp: float
    ten_bp: float

    @property
    def spread_bp(self) -> float:
        return self.ten_bp - self.three_bp


def load_cash_days(path: Path) -> list[CashDay]:
    points: list[CashDay] = []
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not set(REQUIRED_COLUMNS) <= set(reader.fieldnames):
            raise ValueError(f"CSV requires columns: {', '.join(REQUIRED_COLUMNS)}")
        for line, row in enumerate(reader, 2):
            try:
                day = date.fromisoformat(row["date"])
                three = float(row["cash_3y_yield_pct"])
                ten = float(row["cash_10y_yield_pct"])
            except (TypeError, ValueError, KeyError) as exc:
                raise ValueError(f"Invalid cash yield at CSV line {line}") from exc
            if not (math.isfinite(three) and math.isfinite(ten)):
                raise ValueError(f"Nonfinite cash yield at CSV line {line}")
            if points and day <= points[-1].day:
                raise ValueError(f"Dates must be unique and ascending at CSV line {line}")
            points.append(CashDay(day, three * 100.0, ten * 100.0))
    return points


def _changes(points: list[CashDay], risk: RiskSettings | None = None) -> list[dict[str, object]]:
    package = dv01_package(risk) if risk is not None else None
    result = []
    for before, current in zip(points, points[1:]):
        three_change = current.three_bp - before.three_bp
        ten_change = current.ten_bp - before.ten_bp
        proxy = None
        if package is not None:
            proxy = (int(package["rounded_3y"]) * float(risk.dv01_3y_krw_per_bp) * three_change
                     - int(package["contracts_10y"]) * float(risk.dv01_10y_krw_per_bp) * ten_change)
        result.append({"date": current.day, "spread_change_bp": ten_change - three_change,
                       "package_proxy_pnl_krw": proxy})
    return result


def _forecast(values: list[float]) -> float | None:
    if len(values) < LONG_WINDOW:
        return None
    # The short window reacts to a volatility rise; neither window is annualised.
    return max(statistics.stdev(values[-LONG_WINDOW:]), statistics.stdev(values[-SHORT_WINDOW:]))


def target_packages(points: list[CashDay], risk: RiskSettings, as_of: date) -> dict[str, object]:
    required = {"risk.dv01_3y_krw_per_bp": risk.dv01_3y_krw_per_bp,
                "risk.dv01_10y_krw_per_bp": risk.dv01_10y_krw_per_bp,
                "risk.capital_krw": risk.capital_krw,
                "risk.annual_target_vol_fraction": risk.annual_target_vol_fraction,
                "risk.max_package_count": risk.max_package_count}
    missing = [name for name, value in required.items() if value is None]
    if missing:
        return {"status": "missing_settings", "missing": missing}
    past = [point for point in points if point.day <= as_of]
    changes = _changes(past, risk)
    values = [float(item["package_proxy_pnl_krw"]) for item in changes]
    forecast = _forecast(values)
    if forecast is None:
        return {"status": "insufficient_history", "observations": len(values),
                "required": LONG_WINDOW, "as_of": as_of.isoformat()}
    if forecast <= 0:
        return {"status": "zero_observed_volatility", "as_of": past[-1].day.isoformat()}
    package = dv01_package(risk)
    daily_budget = float(risk.capital_krw) * float(risk.annual_target_vol_fraction) / math.sqrt(TRADING_DAYS)
    # Cap before integer conversion so extreme but finite inputs cannot overflow.
    count = min(int(risk.max_package_count), max(0, math.floor(min(
        daily_budget / forecast, float(risk.max_package_count)))))
    gross_per = (int(package["rounded_3y"]) * float(risk.dv01_3y_krw_per_bp)
                 + int(package["contracts_10y"]) * float(risk.dv01_10y_krw_per_bp))
    residual_per = float(package["residual_dv01"])
    if risk.max_gross_dv01_krw_per_bp is not None:
        count = min(count, math.floor(risk.max_gross_dv01_krw_per_bp / gross_per))
    if risk.max_residual_dv01_krw_per_bp is not None and residual_per > 0:
        count = min(count, math.floor(risk.max_residual_dv01_krw_per_bp / residual_per))
    return {"status": "research_proxy", "as_of": past[-1].day.isoformat(),
            "observations": len(values), "daily_budget_krw": daily_budget,
            "unit_daily_sigma_krw": forecast,
            "target_packages": count, "contracts_3y": count * int(package["rounded_3y"]),
            "contracts_10y": count * int(package["contracts_10y"]),
            "gross_dv01_krw_per_bp": count * gross_per,
            "residual_dv01_krw_per_bp": count * residual_per,
            "limits_complete": (risk.max_gross_dv01_krw_per_bp is not None
                                and risk.max_residual_dv01_krw_per_bp is not None)}


def oos_check(points: list[CashDay], test_start: date, risk: RiskSettings | None = None,
              min_train: int = TRADING_DAYS, min_test: int = LONG_WINDOW) -> dict[str, object]:
    """One-step-ahead forecasts; today's move is appended only after prediction."""
    moves = _changes(points, risk)
    train = [move for move in moves if move["date"] < test_start]
    test = [move for move in moves if move["date"] >= test_start]
    result: dict[str, object] = {"metric": "cash_10y_minus_3y_daily_change_bp",
                                 "test_start": test_start.isoformat(), "train_days": len(train),
                                 "test_days": len(test), "required_train_days": min_train,
                                 "required_test_days": min_test}
    if len(train) < min_train or len(test) < min_test:
        return {**result, "status": "insufficient_history"}
    history = [float(move["spread_change_bp"]) for move in train]
    ratios = []
    covered = 0
    first_forecast = _forecast(history)
    for move in test:
        prediction = _forecast(history)
        actual = float(move["spread_change_bp"])
        if prediction is None or prediction <= 0:
            return {**result, "status": "zero_or_missing_forecast"}
        ratios.append(actual / prediction)
        covered += abs(actual) <= 2 * prediction
        history.append(actual)
    result.update(status="evaluated", first_test_day=test[0]["date"].isoformat(),
                  last_test_day=test[-1]["date"].isoformat(),
                  first_test_forecast_sigma_bp=first_forecast,
                  rms_realized_to_forecast=math.sqrt(sum(r * r for r in ratios) / len(ratios)),
                  within_two_sigma_fraction=covered / len(ratios))
    if risk is not None and dv01_package(risk) is not None:
        proxy_history = [float(move["package_proxy_pnl_krw"]) for move in train]
        proxy_ratios = []
        for move in test:
            prediction = _forecast(proxy_history)
            actual = float(move["package_proxy_pnl_krw"])
            if prediction is None or prediction <= 0:
                return {**result, "proxy_status": "zero_or_missing_forecast"}
            proxy_ratios.append(actual / prediction)
            proxy_history.append(actual)
        result["proxy_pnl_rms_realized_to_forecast"] = math.sqrt(
            sum(r * r for r in proxy_ratios) / len(proxy_ratios))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Chronological daily cash-curve risk check; no trade P&L")
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--test-start", type=date.fromisoformat, required=True,
                        help="Preselected first out-of-sample date (YYYY-MM-DD)")
    parser.add_argument("--settings", type=Path, default=Path("settings.json"))
    args = parser.parse_args()
    points = load_cash_days(args.csv_path)
    risk = load_settings(args.settings).risk
    print(json.dumps(oos_check(points, args.test_start, risk), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
