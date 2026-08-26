from __future__ import annotations

from typing import Any


def calculate_ttm_bridge(
    *, prior_fy_value: float, current_ytd_value: float, prior_comparable_ytd_value: float
) -> float:
    return float(prior_fy_value) + float(current_ytd_value) - float(prior_comparable_ytd_value)


def calculate_ttm_net_income(
    *, report_type: str, prior_fy_value: float, current_value: float, prior_comparable_value: float | None
) -> tuple[float, dict[str, Any]]:
    if report_type.upper() == "FY":
        result = float(current_value)
        formula = "CURRENT_FY"
    else:
        if prior_comparable_value is None:
            raise ValueError("interim TTM requires a prior comparable YTD value")
        result = calculate_ttm_bridge(
            prior_fy_value=prior_fy_value,
            current_ytd_value=current_value,
            prior_comparable_ytd_value=prior_comparable_value,
        )
        formula = "PRIOR_FY_PLUS_CURRENT_YTD_MINUS_PRIOR_COMPARABLE_YTD"
    return result, {"formula": formula, "calculated_in_krw": True}
