from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

import pandas as pd


PUBLIC_COLUMNS = ["ticker", "name", "eps_ttm", "net_income_ttm", "per_ttm"]


def round_half_up(value: float, decimals: int = 0) -> float | int:
    quantum = Decimal(1).scaleb(-decimals)
    rounded = Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    return int(rounded) if decimals == 0 else float(rounded)


def build_public_financial_metrics(private_metrics: pd.DataFrame) -> pd.DataFrame:
    required = set(PUBLIC_COLUMNS)
    missing = required - set(private_metrics.columns)
    if missing:
        raise ValueError(f"private metrics missing public fields: {sorted(missing)}")
    public = private_metrics[PUBLIC_COLUMNS].copy()
    if public[["eps_ttm", "net_income_ttm"]].isna().any().any():
        raise ValueError("public EPS and net income must be complete")
    if public["per_ttm"].isna().any():
        raise ValueError("public PER must be numeric or a loss display")
    return public
