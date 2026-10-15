"""Validated user settings for the KTB decision-support dashboard."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class RVSettings:
    beta: float = 2.88
    fast_event_window: int = 120
    core_minutes: int = 60
    entry_z: float = 2.0
    exit_z: float = 1.5
    confirmation_seconds: float = 3.0


@dataclass(frozen=True)
class RiskSettings:
    dv01_3y_krw_per_bp: float | None = None
    dv01_10y_krw_per_bp: float | None = None
    package_10y_contracts: int = 1
    max_gross_dv01_krw_per_bp: float | None = None
    max_residual_dv01_krw_per_bp: float | None = None


@dataclass(frozen=True)
class FlowSettings:
    ema_alpha: float = 0.20
    obi_weight: float = 0.30
    weighted_obi_weight: float = 0.50
    microprice_weight: float = 0.20
    relative_threshold: float = 0.25


@dataclass(frozen=True)
class BasisSettings:
    fair_3y_price: float | None = None
    fair_10y_price: float | None = None
    extra_cost_3y_points: float = 0.0
    extra_cost_10y_points: float = 0.0
    min_net_edge_points: float = 0.01


@dataclass(frozen=True)
class ExecutionSettings:
    min_edge_to_roundtrip_cost: float = 1.25
    extra_roundtrip_cost_points: float = 0.0


@dataclass(frozen=True)
class DataSettings:
    max_gap_seconds: float = 10.0
    stale_seconds: float = 10.0


@dataclass(frozen=True)
class AppSettings:
    rv: RVSettings = RVSettings()
    risk: RiskSettings = RiskSettings()
    flow: FlowSettings = FlowSettings()
    basis: BasisSettings = BasisSettings()
    execution: ExecutionSettings = ExecutionSettings()
    data: DataSettings = DataSettings()


def _positive(value: object, label: str) -> float:
    number = _finite(value, label)
    if number <= 0:
        raise ValueError(f"{label} must be positive")
    return number


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a JSON number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be finite")
    return number


def _integer(value: object, label: str, minimum: int = 2) -> int:
    number = _positive(value, label)
    if not number.is_integer() or number < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return int(number)


def load_settings(path: Path) -> AppSettings:
    if not path.exists():
        raise FileNotFoundError(f"Settings file not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    sections = {"rv": RVSettings, "risk": RiskSettings, "flow": FlowSettings,
                "basis": BasisSettings, "execution": ExecutionSettings, "data": DataSettings}
    if not isinstance(raw, dict) or set(raw) - set(sections):
        raise ValueError("Settings must be an object with known section names")
    for name, cls in sections.items():
        section = raw.get(name, {})
        if not isinstance(section, dict) or set(section) - {field.name for field in fields(cls)}:
            raise ValueError(f"Unknown key or invalid object in {name}")
    rv_raw, risk_raw = raw.get("rv", {}), raw.get("risk", {})
    flow_raw, basis_raw = raw.get("flow", {}), raw.get("basis", {})
    execution_raw = raw.get("execution", {})
    defaults = AppSettings()
    rv = RVSettings(
        beta=_positive(rv_raw.get("beta", defaults.rv.beta), "rv.beta"),
        fast_event_window=_integer(rv_raw.get("fast_event_window", defaults.rv.fast_event_window),
                                   "rv.fast_event_window"),
        core_minutes=_integer(rv_raw.get("core_minutes", defaults.rv.core_minutes), "rv.core_minutes"),
        entry_z=_positive(rv_raw.get("entry_z", defaults.rv.entry_z), "rv.entry_z"),
        exit_z=_positive(rv_raw.get("exit_z", defaults.rv.exit_z), "rv.exit_z"),
        confirmation_seconds=_positive(rv_raw.get("confirmation_seconds", defaults.rv.confirmation_seconds),
                                       "rv.confirmation_seconds"),
    )
    if rv.exit_z >= rv.entry_z:
        raise ValueError("rv.exit_z must be smaller than rv.entry_z")

    def optional_positive(key: str) -> float | None:
        value = risk_raw.get(key)
        return None if value is None else _positive(value, f"risk.{key}")

    risk = RiskSettings(
        dv01_3y_krw_per_bp=optional_positive("dv01_3y_krw_per_bp"),
        dv01_10y_krw_per_bp=optional_positive("dv01_10y_krw_per_bp"),
        package_10y_contracts=_integer(risk_raw.get("package_10y_contracts", 1),
                                      "risk.package_10y_contracts", minimum=1),
        max_gross_dv01_krw_per_bp=optional_positive("max_gross_dv01_krw_per_bp"),
        max_residual_dv01_krw_per_bp=optional_positive("max_residual_dv01_krw_per_bp"),
    )
    if (risk.dv01_3y_krw_per_bp is None) != (risk.dv01_10y_krw_per_bp is None):
        raise ValueError("Both 3Y and 10Y DV01 values must be supplied together")

    flow = FlowSettings(
        ema_alpha=_positive(flow_raw.get("ema_alpha", defaults.flow.ema_alpha), "flow.ema_alpha"),
        obi_weight=_positive(flow_raw.get("obi_weight", defaults.flow.obi_weight), "flow.obi_weight"),
        weighted_obi_weight=_positive(flow_raw.get("weighted_obi_weight", defaults.flow.weighted_obi_weight),
                                      "flow.weighted_obi_weight"),
        microprice_weight=_positive(flow_raw.get("microprice_weight", defaults.flow.microprice_weight),
                                    "flow.microprice_weight"),
        relative_threshold=_positive(flow_raw.get("relative_threshold", defaults.flow.relative_threshold),
                                     "flow.relative_threshold"),
    )
    if flow.ema_alpha > 1:
        raise ValueError("flow.ema_alpha must not exceed 1")

    def optional_number(section: dict[str, object], key: str) -> float | None:
        value = section.get(key)
        return None if value is None else _positive(value, f"basis.{key}")

    basis = BasisSettings(
        fair_3y_price=optional_number(basis_raw, "fair_3y_price"),
        fair_10y_price=optional_number(basis_raw, "fair_10y_price"),
        extra_cost_3y_points=_finite(basis_raw.get("extra_cost_3y_points", 0.0), "basis.extra_cost_3y_points"),
        extra_cost_10y_points=_finite(basis_raw.get("extra_cost_10y_points", 0.0), "basis.extra_cost_10y_points"),
        min_net_edge_points=_positive(basis_raw.get("min_net_edge_points", defaults.basis.min_net_edge_points),
                                      "basis.min_net_edge_points"),
    )
    if basis.extra_cost_3y_points < 0 or basis.extra_cost_10y_points < 0:
        raise ValueError("basis extra costs cannot be negative")
    execution = ExecutionSettings(
        min_edge_to_roundtrip_cost=_positive(
            execution_raw.get("min_edge_to_roundtrip_cost", defaults.execution.min_edge_to_roundtrip_cost),
            "execution.min_edge_to_roundtrip_cost"),
        extra_roundtrip_cost_points=_finite(execution_raw.get("extra_roundtrip_cost_points", 0.0),
                                            "execution.extra_roundtrip_cost_points"),
    )
    if execution.extra_roundtrip_cost_points < 0:
        raise ValueError("execution.extra_roundtrip_cost_points cannot be negative")
    data_raw = raw.get("data", {})
    data = DataSettings(**{field.name: _positive(data_raw.get(field.name, field.default), f"data.{field.name}")
                           for field in fields(DataSettings)})
    return AppSettings(rv=rv, risk=risk, flow=flow, basis=basis, execution=execution, data=data)


def dv01_package(settings: RiskSettings) -> dict[str, float | int] | None:
    if settings.dv01_3y_krw_per_bp is None or settings.dv01_10y_krw_per_bp is None:
        return None
    exact_3y = settings.package_10y_contracts * settings.dv01_10y_krw_per_bp / settings.dv01_3y_krw_per_bp
    rounded_3y = max(1, round(exact_3y))
    residual = abs(rounded_3y * settings.dv01_3y_krw_per_bp
                   - settings.package_10y_contracts * settings.dv01_10y_krw_per_bp)
    return {"exact_3y": exact_3y, "rounded_3y": rounded_3y,
            "contracts_10y": settings.package_10y_contracts, "residual_dv01": residual}


def effective_beta(settings: AppSettings) -> float:
    """Use user-supplied DV01 ratio (NOT independently verified), else research beta.

    Assumes equal KRW/point multipliers for the two KTB futures.
    """
    package = dv01_package(settings.risk)
    if package is None:
        return settings.rv.beta
    return float(package["exact_3y"]) / int(package["contracts_10y"])
