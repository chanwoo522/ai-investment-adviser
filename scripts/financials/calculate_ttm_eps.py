from __future__ import annotations

from typing import Any

from scripts.financials.calculate_ttm_net_income import calculate_ttm_bridge
from scripts.financials.resolve_reporting_periods import actual_days


def calculate_basic_eps_ttm(
    *,
    report_type: str,
    prior_fy_profit: float,
    current_profit: float,
    prior_comparable_profit: float | None,
    prior_fy_shares: float,
    current_shares: float,
    prior_comparable_shares: float | None,
    prior_fy_start: str,
    prior_fy_end: str,
    current_start: str,
    current_end: str,
    prior_comparable_start: str | None,
    prior_comparable_end: str | None,
    prior_fy_retroactive_factor: float = 1.0,
) -> tuple[float, dict[str, Any]]:
    if report_type.upper() == "FY":
        if current_shares <= 0:
            raise ValueError("current FY weighted shares must be positive")
        eps = float(current_profit) / float(current_shares)
        return eps, {
            "basic_eps_profit_ttm": float(current_profit),
            "weighted_average_ordinary_shares_ttm": float(current_shares),
            "share_days_ttm": float(current_shares) * actual_days(current_start, current_end),
            "days_ttm": actual_days(current_start, current_end),
            "prior_fy_retroactive_factor": 1.0,
        }
    if prior_comparable_profit is None or prior_comparable_shares is None:
        raise ValueError("interim TTM EPS requires prior comparable profit and shares")
    if prior_comparable_start is None or prior_comparable_end is None:
        raise ValueError("prior comparable period dates are required")
    fy_days = actual_days(prior_fy_start, prior_fy_end)
    current_days = actual_days(current_start, current_end)
    prior_days = actual_days(prior_comparable_start, prior_comparable_end)
    adjusted_fy_shares = float(prior_fy_shares) * float(prior_fy_retroactive_factor)
    share_days = adjusted_fy_shares * fy_days + float(current_shares) * current_days - float(prior_comparable_shares) * prior_days
    days_ttm = fy_days + current_days - prior_days
    if share_days <= 0 or days_ttm <= 0:
        raise ValueError("TTM share-day bridge is non-positive")
    weighted_shares = share_days / days_ttm
    profit_ttm = calculate_ttm_bridge(
        prior_fy_value=prior_fy_profit,
        current_ytd_value=current_profit,
        prior_comparable_ytd_value=prior_comparable_profit,
    )
    eps = profit_ttm / weighted_shares
    return eps, {
        "basic_eps_profit_ttm": profit_ttm,
        "weighted_average_ordinary_shares_ttm": weighted_shares,
        "share_days_ttm": share_days,
        "days_ttm": days_ttm,
        "prior_fy_retroactive_factor": float(prior_fy_retroactive_factor),
    }
