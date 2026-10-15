"""Non-ordering decision-support metrics built from the live KTB book."""
from __future__ import annotations

from app_settings import BasisSettings, ExecutionSettings, FlowSettings, RiskSettings, dv01_package


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class OrderFlowAnalyzer:
    """Legacy name: book-pressure proxy, NOT trade/order flow."""

    def __init__(self, settings: FlowSettings) -> None:
        self.settings = settings
        self.ema: dict[str, float | None] = {"3y": None, "10y": None}
        self.signatures: dict[str, tuple] = {}

    def reset(self) -> None:
        self.ema = {"3y": None, "10y": None}
        self.signatures.clear()

    def update(self, row: dict[str, object]) -> dict[str, float | str]:
        result: dict[str, float | str] = {}
        weights = (self.settings.obi_weight, self.settings.weighted_obi_weight,
                   self.settings.microprice_weight)
        weight_sum = sum(weights)
        for name in ("3y", "10y"):
            spread = max(float(row[f"{name}_spread"]), 1e-12)
            micro_edge = _clamp((float(row[f"{name}_microprice"]) - float(row[f"{name}_mid"]))
                                / (spread / 2))
            raw = (weights[0] * float(row[f"{name}_obi"])
                   + weights[1] * float(row[f"{name}_weighted_obi"])
                   + weights[2] * micro_edge) / weight_sum
            previous = self.ema[name]
            signature = tuple(float(row[f"{name}_{key}"]) for key in
                              ("spread", "mid", "microprice", "obi", "weighted_obi"))
            smoothed = (raw if previous is None else previous if self.signatures.get(name) == signature
                        else self.settings.ema_alpha * raw + (1 - self.settings.ema_alpha) * previous)
            self.signatures[name] = signature
            self.ema[name] = smoothed
            result[f"{name}_raw"] = raw
            result[f"{name}_score"] = smoothed
        relative = float(result["10y_score"]) - float(result["3y_score"])
        result["relative_score"] = relative
        threshold = self.settings.relative_threshold
        result["state"] = ("10Y 상대 매수호가잔량 우위" if relative >= threshold else
                           "10Y 상대 매도호가잔량 우위" if relative <= -threshold else "중립")
        return result


def basis_metrics(row: dict[str, object], settings: BasisSettings) -> list[dict[str, float | str]]:
    results = []
    for name, fair, extra_cost in (("3Y", settings.fair_3y_price, settings.extra_cost_3y_points),
                                   ("10Y", settings.fair_10y_price, settings.extra_cost_10y_points)):
        if fair is None:
            continue
        key = name.lower()
        market = float(row[f"{key}_mid"])
        raw_basis = market - fair
        crossing_cost = float(row[f"{key}_spread"]) / 2 + extra_cost
        net_edge = max(0.0, abs(raw_basis) - crossing_cost)
        if net_edge < settings.min_net_edge_points:
            state = "비용 이내"
        else:
            state = "입력 적정가 상회" if raw_basis > 0 else "입력 적정가 하회"
        results.append({"contract": name, "market": market, "fair": fair, "basis": raw_basis,
                        "crossing_cost": crossing_cost, "net_edge": net_edge, "state": state})
    return results


def risk_metrics(settings: RiskSettings) -> dict[str, float | str | int] | None:
    package = dv01_package(settings)
    if package is None:
        return None
    gross = (int(package["rounded_3y"]) * float(settings.dv01_3y_krw_per_bp)
             + int(package["contracts_10y"]) * float(settings.dv01_10y_krw_per_bp))
    residual = float(package["residual_dv01"])
    violations = []
    if settings.max_gross_dv01_krw_per_bp is not None and gross > settings.max_gross_dv01_krw_per_bp:
        violations.append("총 DV01 한도 초과")
    if settings.max_residual_dv01_krw_per_bp is not None and residual > settings.max_residual_dv01_krw_per_bp:
        violations.append("잔여 DV01 한도 초과")
    complete = (settings.max_gross_dv01_krw_per_bp is not None
                and settings.max_residual_dv01_krw_per_bp is not None)
    return {**package, "gross_dv01": gross, "violations": violations,
            "status": (", ".join(violations) if violations else
                       "설정된 가상 패키지 한도 이내" if complete else "한도 미설정/부분 설정 — 운용 적합성 미판정")}


def execution_gate(row: dict[str, object], core_result: dict[str, object], beta: float,
                   settings: ExecutionSettings) -> dict[str, float | bool]:
    mean_distance = abs(float(core_result["latest_rv_level"]) - float(core_result["level_mean"]))
    roundtrip_cost = (float(row["10y_spread"]) + beta * float(row["3y_spread"])
                      + settings.extra_roundtrip_cost_points)
    required_edge = roundtrip_cost * settings.min_edge_to_roundtrip_cost
    return {"mean_distance": mean_distance, "roundtrip_cost": roundtrip_cost,
            "required_edge": required_edge, "allowed": mean_distance >= required_edge}


def executable_package_pnl(entry: dict[str, float], exit_row: dict[str, float],
                           signal_state: int, beta: float) -> float:
    """Hypothetical top-of-book P&L, synthetic points; not guaranteed executable."""
    if signal_state == 1:  # RV high: short 10Y, long beta units of 3Y.
        return ((entry["10y_best_bid"] - exit_row["10y_best_ask"])
                + beta * (exit_row["3y_best_bid"] - entry["3y_best_ask"]))
    if signal_state == -1:  # RV low: long 10Y, short beta units of 3Y.
        return ((exit_row["10y_best_bid"] - entry["10y_best_ask"])
                + beta * (entry["3y_best_bid"] - exit_row["3y_best_ask"]))
    raise ValueError("signal_state must be 1 or -1")
