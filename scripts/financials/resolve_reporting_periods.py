from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

import pandas as pd


REPORT_CODE_TO_TYPE = {
    "11013": "Q1",
    "11012": "H1",
    "11014": "Q3",
    "11011": "FY",
}


@dataclass(frozen=True)
class ReportingPeriodResolution:
    security_id: str
    statement_scope: str
    latest_financial_period: str
    fiscal_year_end: str
    latest_report_type: str
    current_ytd_period: str
    prior_comparable_ytd_period: str
    prior_fiscal_year: int
    prior_fy_key: str
    current_key: str
    prior_comparable_key: str | None
    latest_receipt_no: str
    latest_receipt_date: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def actual_days(start: date | str, end: date | str) -> int:
    start_date = date.fromisoformat(start) if isinstance(start, str) else start
    end_date = date.fromisoformat(end) if isinstance(end, str) else end
    if end_date < start_date:
        raise ValueError("period end precedes period start")
    return (end_date - start_date).days + 1


def ttm_dependency_keys(*, fiscal_year: int, report_type: str) -> tuple[str, str, str | None]:
    normalized = report_type.upper()
    if normalized == "FY":
        return f"FY{fiscal_year}", f"FY{fiscal_year}", None
    prefix = {"Q1": "Q1", "H1": "H1", "Q2": "H1", "Q3": "M9", "9M": "M9"}.get(normalized)
    if prefix is None:
        raise ValueError(f"unsupported report type: {report_type}")
    return (
        f"FY{fiscal_year - 1}",
        f"{prefix}_{fiscal_year}",
        f"{prefix}_{fiscal_year - 1}_COMPARATIVE",
    )


def resolve_latest_period(
    *, security_id: str, lineage: pd.DataFrame, fact_mapping: pd.DataFrame, information_asof: str
) -> ReportingPeriodResolution:
    rows = lineage.loc[
        lineage["ticker"].astype(str).eq(str(security_id))
        & lineage["receipt_date"].astype(str).le(information_asof)
    ].copy()
    if rows.empty:
        raise ValueError(f"no eligible periodic filing for security {security_id}")
    rows["report_type"] = rows["report_code"].astype(str).map(REPORT_CODE_TO_TYPE)
    rows = rows.loc[rows["report_type"].notna()].sort_values(["receipt_date", "receipt_no"])
    latest = rows.iloc[-1]
    fiscal_year = int(latest["business_year"])
    report_type = str(latest["report_type"])
    prior_fy_key, current_key, prior_key = ttm_dependency_keys(
        fiscal_year=fiscal_year, report_type=report_type
    )
    security_facts = fact_mapping.loc[fact_mapping["ticker"].astype(str).eq(str(security_id))]
    needed = {prior_fy_key, current_key}
    if prior_key:
        needed.add(prior_key)
    missing = needed - set(security_facts["period_key"].astype(str))
    if missing:
        raise ValueError(f"resolved period facts missing for {security_id}: {sorted(missing)}")
    periods = security_facts.loc[security_facts["period_key"].isin(needed)]
    starts = pd.to_datetime(periods["period_start"])
    ends = pd.to_datetime(periods["period_end"])
    current_rows = security_facts.loc[security_facts["period_key"].eq(current_key)]
    current_start = str(current_rows["period_start"].iloc[0])
    current_end = str(current_rows["period_end"].iloc[0])
    fiscal_year_end = str(
        security_facts.loc[security_facts["period_key"].eq(prior_fy_key), "period_end"].iloc[0]
    )
    latest_label = f"{fiscal_year}{report_type}"
    return ReportingPeriodResolution(
        security_id=str(security_id),
        statement_scope=str(latest["statement_scope"]),
        latest_financial_period=latest_label,
        fiscal_year_end=fiscal_year_end,
        latest_report_type=report_type,
        current_ytd_period=f"{current_start}/{current_end}",
        prior_comparable_ytd_period=(
            "" if prior_key is None else f"{periods.loc[periods['period_key'].eq(prior_key), 'period_start'].iloc[0]}/{periods.loc[periods['period_key'].eq(prior_key), 'period_end'].iloc[0]}"
        ),
        prior_fiscal_year=fiscal_year - 1 if report_type != "FY" else fiscal_year,
        prior_fy_key=prior_fy_key,
        current_key=current_key,
        prior_comparable_key=prior_key,
        latest_receipt_no=str(latest["receipt_no"]),
        latest_receipt_date=str(latest["receipt_date"]),
    )


def validate_period_bridge(periods: pd.DataFrame, resolution: ReportingPeriodResolution) -> None:
    keys = [resolution.prior_fy_key, resolution.current_key]
    if resolution.prior_comparable_key:
        keys.append(resolution.prior_comparable_key)
    rows = periods.loc[periods["period_key"].isin(keys)]
    if set(rows["statement_scope"]) != {resolution.statement_scope}:
        raise ValueError("CFS/OFS scope mixing detected")
    if not rows["period_end"].notna().all() or not rows["period_start"].notna().all():
        raise ValueError("period boundary missing")
    for row in rows.drop_duplicates("period_key").itertuples():
        actual_days(str(row.period_start), str(row.period_end))
