"""KRX standard-bond DV01 for 3Y and 10Y KTB futures.

This is a quoted standard-bond sensitivity, not a portfolio/CTD DV01.  KRX
defines a KRW 100 million face amount, 5% annual coupon and semiannual cash
flows for both contracts.
"""
from __future__ import annotations

import math


FACE_KRW = 100_000_000.0
QUOTE_FACE = 100.0
COUPON_RATE = 0.05
PAYMENTS_PER_YEAR = 2
ONE_BP = 0.0001


def standard_bond_price(yield_pct: float, maturity_years: int) -> float:
    """Return clean theoretical price per KRW 100 face under the KRX convention."""
    if isinstance(yield_pct, bool) or not math.isfinite(float(yield_pct)):
        raise ValueError("Futures yield must be finite")
    if maturity_years not in (3, 10):
        raise ValueError("Only KRX 3Y and 10Y standard bonds are supported")
    annual_yield = float(yield_pct) / 100.0
    discount = 1.0 + annual_yield / PAYMENTS_PER_YEAR
    if discount <= 0:
        raise ValueError("Futures yield is outside the bond-price domain")
    periods = maturity_years * PAYMENTS_PER_YEAR
    coupon = QUOTE_FACE * COUPON_RATE / PAYMENTS_PER_YEAR
    return sum(coupon / discount ** period for period in range(1, periods + 1)) + \
        QUOTE_FACE / discount ** periods


def standard_bond_dv01(yield_pct: float, maturity_years: int) -> float:
    """KRW price gain for a parallel 1bp yield decline, using central difference."""
    standard_bond_price(yield_pct, maturity_years)  # shared type/domain validation
    annual_yield = float(yield_pct) / 100.0
    lower_pct = (annual_yield - ONE_BP) * 100.0
    upper_pct = (annual_yield + ONE_BP) * 100.0
    point_dv01 = (standard_bond_price(lower_pct, maturity_years)
                  - standard_bond_price(upper_pct, maturity_years)) / 2.0
    return point_dv01 * FACE_KRW / QUOTE_FACE


def analytic_standard_bond_dv01(yield_pct: float, maturity_years: int) -> float:
    """Analytic derivative used to independently verify the finite difference."""
    # Validate with the same domain checks as the production price function.
    standard_bond_price(yield_pct, maturity_years)
    annual_yield = float(yield_pct) / 100.0
    discount = 1.0 + annual_yield / PAYMENTS_PER_YEAR
    periods = maturity_years * PAYMENTS_PER_YEAR
    coupon = QUOTE_FACE * COUPON_RATE / PAYMENTS_PER_YEAR
    derivative = 0.0
    for period in range(1, periods + 1):
        cashflow = coupon + (QUOTE_FACE if period == periods else 0.0)
        derivative -= (period / PAYMENTS_PER_YEAR) * cashflow / discount ** (period + 1)
    return -derivative * ONE_BP * FACE_KRW / QUOTE_FACE


def futures_dv01_pair(row: dict[str, object]) -> dict[str, float]:
    """Build execution-time standard-bond DV01s from connected RTD yields."""
    try:
        raw_3y = row["3y_futures_yield_pct"]
        raw_10y = row["10y_futures_yield_pct"]
        if isinstance(raw_3y, bool) or isinstance(raw_10y, bool):
            raise ValueError("Boolean futures yield")
        yield_3y = float(raw_3y)
        yield_10y = float(raw_10y)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Both futures yields are required for automatic DV01") from exc
    if not all(math.isfinite(value) and value > 0 for value in (yield_3y, yield_10y)):
        raise ValueError("Automatic DV01 requires positive finite futures yields")
    dv01_3y = standard_bond_dv01(yield_3y, 3)
    dv01_10y = standard_bond_dv01(yield_10y, 10)
    if not all(math.isfinite(value) and value > 0 for value in (dv01_3y, dv01_10y)):
        raise ValueError("Calculated DV01 is invalid")
    return {"yield_3y_pct": yield_3y, "yield_10y_pct": yield_10y,
            "dv01_3y_krw_per_bp": dv01_3y, "dv01_10y_krw_per_bp": dv01_10y,
            "beta": dv01_10y / dv01_3y}
