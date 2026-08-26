from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class PerResolution:
    per_ttm: float | None
    per_status: str
    method: str | None
    price_used: float
    price_observation_date: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def select_official_close_at_or_before(
    prices: pd.DataFrame,
    *,
    price_asof: str,
    maximum_lookback_days: int,
    date_column: str = "date",
    close_column: str = "close",
) -> tuple[str, float]:
    """Select the latest positive official close without crossing the run cutoff."""
    if maximum_lookback_days < 0:
        raise ValueError("maximum lookback days cannot be negative")
    if date_column not in prices or close_column not in prices:
        raise ValueError("official price history is missing required columns")
    cutoff = pd.Timestamp(date.fromisoformat(price_asof))
    eligible = prices[[date_column, close_column]].copy()
    eligible[date_column] = pd.to_datetime(eligible[date_column], errors="coerce")
    eligible[close_column] = pd.to_numeric(eligible[close_column], errors="coerce")
    eligible = eligible.loc[
        eligible[date_column].notna()
        & eligible[date_column].le(cutoff)
        & eligible[close_column].gt(0)
    ].sort_values(date_column)
    if eligible.empty:
        raise ValueError("no positive official close exists at or before the cutoff")
    selected = eligible.iloc[-1]
    age = int((cutoff - selected[date_column]).days)
    if age > maximum_lookback_days:
        raise ValueError("latest official close exceeds the permitted lookback")
    return selected[date_column].date().isoformat(), float(selected[close_column])


def calculate_per_ttm(
    *,
    official_close_price: float,
    price_observation_date: str,
    basic_eps_ttm: float | None,
    compatible_market_cap: float | None,
    compatible_net_income_ttm: float | None,
) -> PerResolution:
    if official_close_price <= 0:
        raise ValueError("official close price must be positive")
    if (basic_eps_ttm is not None and basic_eps_ttm <= 0) or (
        compatible_net_income_ttm is not None and compatible_net_income_ttm <= 0
    ):
        return PerResolution(None, "LOSS", None, official_close_price, price_observation_date)
    if basic_eps_ttm is not None:
        return PerResolution(
            official_close_price / basic_eps_ttm,
            "PASS",
            "PRICE_DIV_EPS",
            official_close_price,
            price_observation_date,
        )
    if compatible_market_cap is not None and compatible_net_income_ttm is not None:
        return PerResolution(
            compatible_market_cap / compatible_net_income_ttm,
            "PASS",
            "MCAP_DIV_NET_INCOME",
            official_close_price,
            price_observation_date,
        )
    return PerResolution(None, "NA", None, official_close_price, price_observation_date)
