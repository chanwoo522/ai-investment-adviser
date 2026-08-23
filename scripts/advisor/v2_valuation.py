from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from scripts.common.security_id import normalize_security_id_series


VALUATION_CONTRACT_VERSION = "ADVISOR_FULL_RESET_V2_VALUATION_V1"

# Valuation fields deliberately use exact XBRL concepts only.  Provider
# multiples, total profit/equity and liabilities-based EV proxies are not
# aliases under this contract.
EXACT_FLOW_ACCOUNT_IDS: Mapping[str, str] = {
    "revenue_quarter": "ifrs-full_Revenue",
    "operating_income_quarter": "dart_OperatingIncomeLoss",
    "parent_net_income_quarter": "ifrs-full_ProfitLossAttributableToOwnersOfParent",
    "cfo_quarter": "ifrs-full_CashFlowsFromUsedInOperatingActivities",
}

EXACT_STOCK_ACCOUNT_IDS: Mapping[str, str | None] = {
    "parent_equity_latest": "ifrs-full_EquityAttributableToOwnersOfParent",
    "interest_bearing_debt_latest": "dart_InterestBearingDebt",
    # There is no observed generic IFRS/DART concept that proves the entire
    # preferred-equity EV component.  It therefore remains NA unless a caller
    # supplies a separately certified exact value.
    "preferred_equity_latest": None,
    "noncontrolling_interest_latest": "ifrs-full_NoncontrollingInterests",
    "cash_and_cash_equivalents_latest": "ifrs-full_CashAndCashEquivalents",
}

_REPORT_TO_QUARTER = {
    "11013": 1,  # Q1
    "11012": 2,  # half-year cumulative
    "11014": 3,  # Q3 cumulative
    "11011": 4,  # annual cumulative
}
_REPORT_FLOW_AMOUNT_COLUMNS = {
    "11013": ("thstrm_amount", "thstrm_add_amount"),
    "11012": ("thstrm_add_amount", "thstrm_amount"),
    "11014": ("thstrm_add_amount", "thstrm_amount"),
    "11011": ("thstrm_amount", "thstrm_add_amount"),
}
_RAW_CACHE_PATTERN = re.compile(
    r"^finstate_all__corp=(?P<corp>\d{8})__year=(?P<year>\d{4})"
    r"__reprt=(?P<report>\d{5})__fs=(?P<scope>CFS|OFS)"
    r"__asof=(?P<asof>\d{4}-\d{2}-\d{2})\.parquet$"
)
_PLAIN_NUMBER = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$")


class ValuationContractError(ValueError):
    """Raised when an input cannot be interpreted without contract drift."""


@dataclass(frozen=True)
class ExactFinancialPanelResult:
    panel: pd.DataFrame
    qa: dict[str, Any]


@dataclass(frozen=True)
class ValuationArtifacts:
    valuation_qa: pd.DataFrame
    selected_security_financials: pd.DataFrame
    selected_security_diagnostics: pd.DataFrame
    qa: dict[str, Any]


def _canonical_date(value: object, field: str) -> str:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValuationContractError(f"{field} must be a valid date")
    return parsed.date().isoformat()


def _numeric(value: object) -> float | None:
    if value is None or value is pd.NA:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip()
    if not _PLAIN_NUMBER.fullmatch(text):
        return None
    number = float(text)
    return number if math.isfinite(number) else None


def _normalize_ticker_frame(frame: pd.DataFrame, label: str) -> pd.DataFrame:
    if "ticker" not in frame.columns:
        raise ValuationContractError(f"{label} must contain ticker")
    out = frame.copy()
    out["ticker"] = normalize_security_id_series(out["ticker"])
    if out["ticker"].isna().any():
        raise ValuationContractError(f"{label} contains an invalid security ID")
    return out


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _receipt_date(raw: pd.DataFrame) -> str | None:
    if "rcept_no" not in raw.columns:
        return None
    dates = raw["rcept_no"].astype("string").str.extract(r"^(\d{8})")[0]
    parsed = pd.to_datetime(dates, format="%Y%m%d", errors="coerce").dropna()
    if len(parsed) == 0:
        return None
    return parsed.max().date().isoformat()


def _extract_exact_amount(
    raw: pd.DataFrame,
    *,
    statement_section: str | Iterable[str],
    account_id: str | None,
    amount_columns: Iterable[str],
) -> tuple[float | None, str]:
    if account_id is None:
        return None, "NO_APPROVED_EXACT_ACCOUNT_ID"
    required = {"sj_div", "account_id"}
    if not required.issubset(raw.columns):
        return None, "RAW_SCHEMA_MISSING"
    sections = (
        {statement_section}
        if isinstance(statement_section, str)
        else {str(value) for value in statement_section}
    )
    subset = raw.loc[
        raw["sj_div"].astype("string").isin(sections)
        & raw["account_id"].astype("string").eq(account_id)
    ]
    if len(subset) == 0:
        return None, "EXACT_ACCOUNT_ID_NOT_REPORTED"
    values: list[float] = []
    for _, row in subset.iterrows():
        value = None
        for column in amount_columns:
            if column in subset.columns:
                value = _numeric(row.get(column))
                if value is not None:
                    break
        if value is not None:
            values.append(value)
    unique_values = sorted(set(values))
    if len(unique_values) == 1:
        return unique_values[0], "PASS_EXACT_ACCOUNT_ID"
    if len(unique_values) == 0:
        return None, "EXACT_ACCOUNT_AMOUNT_MISSING"
    return None, "EXACT_ACCOUNT_AMOUNT_AMBIGUOUS"


def _load_raw_report(path: Path, *, corp_code: str, report_code: str, cutoff: str) -> dict[str, Any]:
    raw = pd.read_parquet(path)
    for column, expected in (
        ("corp_code", corp_code),
        ("reprt_code", report_code),
    ):
        if column not in raw.columns:
            raise ValuationContractError(f"raw DART report missing {column}: {path.name}")
        observed = set(raw[column].dropna().astype("string").str.strip())
        if observed and observed != {expected}:
            raise ValuationContractError(
                f"raw DART report {column} mismatch: {path.name} observed={sorted(observed)}"
            )
    received = _receipt_date(raw)
    if received is None:
        raise ValuationContractError(f"raw DART report lacks receipt lineage: {path.name}")
    if received > cutoff:
        raise ValuationContractError(f"future DART filing at cutoff {cutoff}: {path.name}")

    flow: dict[str, float | None] = {}
    flow_evidence: dict[str, str] = {}
    for target, account_id in EXACT_FLOW_ACCOUNT_IDS.items():
        value, status = _extract_exact_amount(
            raw,
            # DART legitimately publishes the exact income-statement concepts
            # under either IS or CIS.  This is a statement-section variation,
            # not a CFS/OFS scope fallback.  Conflicting exact values across
            # the two sections are rejected as ambiguous below.
            statement_section="CF" if target == "cfo_quarter" else ("IS", "CIS"),
            account_id=account_id,
            amount_columns=_REPORT_FLOW_AMOUNT_COLUMNS[report_code],
        )
        flow[target] = value
        flow_evidence[target] = status

    stock: dict[str, float | None] = {}
    stock_evidence: dict[str, str] = {}
    for target, account_id in EXACT_STOCK_ACCOUNT_IDS.items():
        value, status = _extract_exact_amount(
            raw,
            statement_section="BS",
            account_id=account_id,
            amount_columns=("thstrm_amount", "thstrm_add_amount"),
        )
        stock[target] = value
        stock_evidence[target] = status
    return {
        "flow": flow,
        "flow_evidence": flow_evidence,
        "stock": stock,
        "stock_evidence": stock_evidence,
        "received": received,
        "source_artifact": path.name,
        "source_sha256": _sha256(path),
    }


def _subtract_exact_flow(
    current: Mapping[str, float | None],
    dependency: Mapping[str, float | None],
) -> dict[str, float | None]:
    result: dict[str, float | None] = {}
    for field in EXACT_FLOW_ACCOUNT_IDS:
        current_value = current.get(field)
        dependency_value = dependency.get(field)
        result[field] = (
            float(current_value) - float(dependency_value)
            if current_value is not None and dependency_value is not None
            else None
        )
    return result


def build_exact_quarterly_financials_from_dart_cache(
    securities: pd.DataFrame | Sequence[Mapping[str, Any]],
    raw_dir: str | Path,
    *,
    information_asof: object,
    statement_scope: str = "CFS",
    lookback_years: int = 2,
) -> ExactFinancialPanelResult:
    """Rebuild a PIT quarterly panel from immutable exact-XBRL DART caches.

    The newest cache snapshot not later than ``information_asof`` is selected
    for each corp/year/report/scope tuple.  No network access and no writes are
    performed.  A missing report or exact concept remains missing.
    """

    cutoff = _canonical_date(information_asof, "information_asof")
    if isinstance(lookback_years, bool) or int(lookback_years) != lookback_years or lookback_years < 2:
        raise ValuationContractError("lookback_years must be an integer of at least two")
    cutoff_year = int(cutoff[:4])
    minimum_year = cutoff_year - int(lookback_years) + 1
    scope = str(statement_scope).upper()
    if scope not in {"CFS", "OFS"}:
        raise ValuationContractError("statement_scope must be CFS or OFS")
    selected = _normalize_ticker_frame(pd.DataFrame(securities), "securities")
    if "corp_code" not in selected.columns:
        raise ValuationContractError("securities must contain corp_code for exact DART joins")
    selected["corp_code"] = selected["corp_code"].astype("string").str.strip()
    valid_corp = selected["corp_code"].str.fullmatch(r"\d{8}", na=False)
    if not valid_corp.all():
        raise ValuationContractError("corp_code must be an eight-digit string")
    if selected["ticker"].duplicated().any() or selected["corp_code"].duplicated().any():
        raise ValuationContractError("ticker and corp_code must each be one-to-one")

    root = Path(raw_dir)
    if not root.is_dir():
        raise FileNotFoundError(root)
    rows: list[dict[str, Any]] = []
    source_files: list[str] = []
    missing_reports: list[dict[str, Any]] = []

    for security in selected.to_dict(orient="records"):
        ticker = str(security["ticker"])
        corp_code = str(security["corp_code"])
        candidates: dict[tuple[int, str], tuple[str, Path]] = {}
        for path in root.glob(f"finstate_all__corp={corp_code}__year=*__reprt=*__fs={scope}__asof=*.parquet"):
            match = _RAW_CACHE_PATTERN.fullmatch(path.name)
            if not match or match.group("scope") != scope or match.group("corp") != corp_code:
                continue
            artifact_asof = match.group("asof")
            report_code = match.group("report")
            report_year = int(match.group("year"))
            if (
                artifact_asof > cutoff
                or report_code not in _REPORT_TO_QUARTER
                or report_year < minimum_year
                or report_year > cutoff_year
            ):
                continue
            key = (report_year, report_code)
            if key not in candidates or artifact_asof > candidates[key][0]:
                candidates[key] = (artifact_asof, path)

        if not candidates:
            missing_reports.append({"ticker": ticker, "reason": "NO_RAW_DART_CACHE"})
            continue

        reports_by_year: dict[int, dict[str, dict[str, Any]]] = {}
        for (year, report_code), (artifact_asof, path) in sorted(candidates.items()):
            payload = _load_raw_report(
                path,
                corp_code=corp_code,
                report_code=report_code,
                cutoff=cutoff,
            )
            payload["artifact_asof"] = artifact_asof
            reports_by_year.setdefault(year, {})[report_code] = payload
            source_files.append(path.name)

        for year, reports in sorted(reports_by_year.items()):
            quarter_contracts = {
                1: ("11013", None),
                2: ("11012", "11013"),
                3: ("11014", "11012"),
                4: ("11011", "11014"),
            }
            for quarter, (current_code, dependency_code) in quarter_contracts.items():
                current = reports.get(current_code)
                dependency = reports.get(dependency_code) if dependency_code else None
                if current is None or (dependency_code is not None and dependency is None):
                    missing_reports.append(
                        {
                            "ticker": ticker,
                            "year": year,
                            "quarter": quarter,
                            "reason": "CURRENT_OR_DEPENDENCY_REPORT_MISSING",
                        }
                    )
                    continue
                flow = (
                    dict(current["flow"])
                    if dependency is None
                    else _subtract_exact_flow(current["flow"], dependency["flow"])
                )
                receipt_dates = [current["received"]]
                source_artifacts = [current["source_artifact"]]
                source_hashes = [current["source_sha256"]]
                if dependency is not None:
                    receipt_dates.append(dependency["received"])
                    source_artifacts.append(dependency["source_artifact"])
                    source_hashes.append(dependency["source_sha256"])
                row = {
                    "ticker": ticker,
                    "name": security.get("name"),
                    "corp_code": corp_code,
                    "year": year,
                    "quarter": quarter,
                    "financial_information_asof": cutoff,
                    "statement_scope": scope,
                    "source_received_date": max(receipt_dates),
                    "source_artifacts": "|".join(source_artifacts),
                    "source_sha256": "|".join(source_hashes),
                    **flow,
                    **current["stock"],
                }
                rows.append(row)

    panel = pd.DataFrame.from_records(rows)
    if len(panel):
        panel = panel.sort_values(["ticker", "year", "quarter"], kind="mergesort")
        if panel.duplicated(["ticker", "year", "quarter"]).any():
            raise ValuationContractError("exact DART panel contains duplicate security periods")
        panel = panel.reset_index(drop=True)
    contiguous_tickers: list[str] = []
    if len(panel):
        for ticker, group in panel.groupby("ticker"):
            ordinals = group.sort_values(["year", "quarter"])["year"].astype(int) * 4 + group.sort_values(
                ["year", "quarter"]
            )["quarter"].astype(int) - 1
            last_four = ordinals.tail(4).tolist()
            if len(last_four) == 4 and all(
                later - earlier == 1 for earlier, later in zip(last_four, last_four[1:])
            ):
                contiguous_tickers.append(str(ticker))
    all_tickers = selected["ticker"].astype(str).tolist()
    qa = {
        "contract_version": VALUATION_CONTRACT_VERSION,
        "status": "PASS" if len(missing_reports) == 0 else "PARTIAL",
        "information_asof": cutoff,
        "statement_scope": scope,
        "lookback_years": int(lookback_years),
        "years_considered": list(range(minimum_year, cutoff_year + 1)),
        "security_count": int(len(selected)),
        "security_count_with_rows": int(panel["ticker"].nunique()) if len(panel) else 0,
        "row_count": int(len(panel)),
        "source_file_count": len(set(source_files)),
        "source_files": sorted(set(source_files)),
        "security_count_with_contiguous_four_quarters": len(contiguous_tickers),
        "tickers_without_contiguous_four_quarters": sorted(set(all_tickers) - set(contiguous_tickers)),
        "missing_report_count": len(missing_reports),
        "missing_reports": missing_reports,
        "network_used": False,
        "raw_cache_modified": False,
    }
    return ExactFinancialPanelResult(panel=panel, qa=qa)


def _first_present(frame: pd.DataFrame, names: Iterable[str]) -> pd.Series:
    for name in names:
        if name in frame.columns:
            return frame[name]
    return pd.Series(pd.NA, index=frame.index, dtype="object")


def _safe_ratio(numerator: object, denominator: object) -> tuple[float | None, str | None]:
    num = _numeric(numerator)
    den = _numeric(denominator)
    if num is None:
        return None, "NUMERATOR_MISSING"
    if den is None:
        return None, "DENOMINATOR_MISSING"
    if den <= 0:
        return None, "DENOMINATOR_NONPOSITIVE"
    return num / den, None


def _sum_complete(group: pd.DataFrame, column: str) -> float | None:
    if column not in group.columns or len(group) != 4:
        return None
    values = [_numeric(value) for value in group[column]]
    if any(value is None for value in values):
        return None
    return float(sum(value for value in values if value is not None))


def _market_snapshot(
    selected: pd.DataFrame,
    market_cap_snapshot: pd.DataFrame,
    *,
    valuation_asof: str,
) -> pd.DataFrame:
    market = _normalize_ticker_frame(market_cap_snapshot, "market_cap_snapshot")
    if market["ticker"].duplicated().any():
        raise ValuationContractError("market_cap_snapshot contains duplicate tickers")
    value = _first_present(market, ["market_cap_asof", "market_cap", "mcap"])
    date = _first_present(market, ["valuation_asof", "used_px_date", "asof_ymd", "date"])
    market = market[["ticker"]].copy()
    market["market_cap_asof"] = value.map(_numeric)
    market["valuation_asof"] = pd.to_datetime(date, errors="coerce").dt.date.astype("string")
    market["market_cap_status"] = "PASS"
    market.loc[market["market_cap_asof"].isna(), "market_cap_status"] = "MARKET_CAP_MISSING"
    market.loc[
        market["valuation_asof"].notna() & market["valuation_asof"].ne(valuation_asof),
        "market_cap_status",
    ] = "MARKET_CAP_DATE_MISMATCH"
    market.loc[market["valuation_asof"].isna(), "market_cap_status"] = "MARKET_CAP_DATE_MISSING"
    market.loc[market["market_cap_status"].ne("PASS"), "market_cap_asof"] = pd.NA
    return selected[["ticker"]].merge(market, on="ticker", how="left", validate="one_to_one")


def _financial_rows(
    quarterly_financials: pd.DataFrame,
    *,
    model_information_asof: str,
    statement_scope: str,
) -> pd.DataFrame:
    panel = _normalize_ticker_frame(quarterly_financials, "quarterly_financials")
    required = {"year", "quarter"}
    missing = sorted(required - set(panel.columns))
    if missing:
        raise ValuationContractError(f"quarterly_financials missing columns: {missing}")
    scope = _first_present(panel, ["statement_scope", "fs_div_used", "fs_div"])
    info_asof = _first_present(panel, ["financial_information_asof", "asof"])
    received = _first_present(panel, ["source_received_date", "financial_received_date"])
    panel["statement_scope"] = scope.astype("string").str.upper()
    panel["financial_information_asof"] = pd.to_datetime(info_asof, errors="coerce").dt.date.astype("string")
    panel["source_received_date"] = pd.to_datetime(received, errors="coerce").dt.date.astype("string")
    panel["year"] = pd.to_numeric(panel["year"], errors="coerce")
    panel["quarter"] = pd.to_numeric(panel["quarter"], errors="coerce")
    malformed = panel["year"].isna() | ~panel["quarter"].isin([1, 2, 3, 4])
    if malformed.any():
        raise ValuationContractError("quarterly_financials contains malformed periods")
    panel["period_ordinal"] = panel["year"].astype(int) * 4 + panel["quarter"].astype(int) - 1
    panel["pit_row_status"] = "PASS"
    panel.loc[panel["statement_scope"].ne(statement_scope), "pit_row_status"] = "STATEMENT_SCOPE_MISMATCH"
    panel.loc[
        panel["financial_information_asof"].notna()
        & panel["financial_information_asof"].gt(model_information_asof),
        "pit_row_status",
    ] = "FUTURE_INFORMATION_SNAPSHOT"
    panel.loc[
        panel["source_received_date"].notna()
        & panel["source_received_date"].gt(model_information_asof),
        "pit_row_status",
    ] = "FUTURE_FILING"
    # A dated immutable ASOF snapshot is accepted as PIT evidence even when a
    # legacy canonical panel lacks per-filing receipt dates.  The weaker
    # lineage is surfaced and never represented as exact receipt evidence.
    panel["pit_evidence_status"] = "RECEIPT_DATE_AND_ASOF"
    panel.loc[panel["source_received_date"].isna(), "pit_evidence_status"] = "ASOF_SNAPSHOT_ONLY"
    panel.loc[panel["financial_information_asof"].isna(), "pit_row_status"] = "INFORMATION_ASOF_MISSING"
    return panel


def _enterprise_value(row: Mapping[str, Any]) -> tuple[float | None, str | None]:
    components = (
        "market_cap_asof",
        "interest_bearing_debt_latest",
        "preferred_equity_latest",
        "noncontrolling_interest_latest",
        "cash_and_cash_equivalents_latest",
    )
    values = {component: _numeric(row.get(component)) for component in components}
    reason_labels = {
        "market_cap_asof": "MARKET_CAP_MISSING",
        "interest_bearing_debt_latest": "INTEREST_BEARING_DEBT_MISSING",
        "preferred_equity_latest": "PREFERRED_EQUITY_MISSING",
        "noncontrolling_interest_latest": "NONCONTROLLING_INTEREST_MISSING",
        "cash_and_cash_equivalents_latest": "CASH_AND_EQUIVALENTS_MISSING",
    }
    for component in components:
        if values[component] is None:
            return None, reason_labels[component]
    return (
        values["market_cap_asof"]
        + values["interest_bearing_debt_latest"]
        + values["preferred_equity_latest"]
        + values["noncontrolling_interest_latest"]
        - values["cash_and_cash_equivalents_latest"],
        None,
    )


def build_valuation_artifacts(
    selected_securities: pd.DataFrame | Sequence[Mapping[str, Any]],
    market_cap_snapshot: pd.DataFrame | Sequence[Mapping[str, Any]],
    quarterly_financials: pd.DataFrame | Sequence[Mapping[str, Any]],
    *,
    model_information_asof: object,
    valuation_asof: object,
    statement_scope: str = "CFS",
    industry_mapping: pd.DataFrame | Sequence[Mapping[str, Any]] | None = None,
) -> ValuationArtifacts:
    """Build V2 valuation and selected-security financial/diagnostic tables."""

    info_asof = _canonical_date(model_information_asof, "model_information_asof")
    value_asof = _canonical_date(valuation_asof, "valuation_asof")
    scope = str(statement_scope).upper()
    if scope not in {"CFS", "OFS"}:
        raise ValuationContractError("statement_scope must be CFS or OFS")
    selected = _normalize_ticker_frame(pd.DataFrame(selected_securities), "selected_securities")
    if selected["ticker"].duplicated().any():
        raise ValuationContractError("selected_securities contains duplicate tickers")
    if "name" not in selected.columns:
        selected["name"] = pd.NA
    if "model_rank" not in selected.columns:
        selected["model_rank"] = pd.NA
    market = _market_snapshot(selected, pd.DataFrame(market_cap_snapshot), valuation_asof=value_asof)
    panel = _financial_rows(
        pd.DataFrame(quarterly_financials),
        model_information_asof=info_asof,
        statement_scope=scope,
    )
    if panel.duplicated(["ticker", "year", "quarter"]).any():
        raise ValuationContractError("quarterly_financials periods must be unique per security")

    valuation_rows: list[dict[str, Any]] = []
    financial_rows: list[dict[str, Any]] = []
    for security in selected.to_dict(orient="records"):
        ticker = str(security["ticker"])
        market_row = market.loc[market["ticker"].eq(ticker)]
        market_payload = market_row.iloc[0].to_dict() if len(market_row) else {}
        security_panel_all = panel.loc[panel["ticker"].eq(ticker)].sort_values(
            "period_ordinal", kind="mergesort"
        )
        invalid_pit = security_panel_all.loc[security_panel_all["pit_row_status"].ne("PASS")]
        security_panel = security_panel_all.loc[security_panel_all["pit_row_status"].eq("PASS")]
        last_four = security_panel.tail(4)
        ordinals = last_four["period_ordinal"].astype(int).tolist()
        contiguous = len(ordinals) == 4 and all(
            later - earlier == 1 for earlier, later in zip(ordinals, ordinals[1:])
        )
        latest = security_panel.iloc[-1].to_dict() if len(security_panel) else {}

        revenue_ttm = _sum_complete(last_four, "revenue_quarter") if contiguous else None
        operating_income_ttm = (
            _sum_complete(last_four, "operating_income_quarter") if contiguous else None
        )
        parent_net_income_ttm = (
            _sum_complete(last_four, "parent_net_income_quarter") if contiguous else None
        )
        cfo_ttm = _sum_complete(last_four, "cfo_quarter") if contiguous else None
        payload: dict[str, Any] = {
            "ticker": ticker,
            "name": security.get("name"),
            "model_rank": security.get("model_rank"),
            "model_information_asof": info_asof,
            "valuation_asof": value_asof,
            "market_cap_asof": market_payload.get("market_cap_asof"),
            "market_cap_status": market_payload.get("market_cap_status", "MARKET_CAP_MISSING"),
            "statement_scope": scope,
            "financial_period_latest": (
                f"{int(latest['year'])}Q{int(latest['quarter'])}" if latest else pd.NA
            ),
            "financial_received_date": latest.get("source_received_date", pd.NA),
            "pit_evidence_status": latest.get("pit_evidence_status", "NO_FINANCIAL_ROWS"),
            "future_or_scope_invalid_row_count": int(len(invalid_pit)),
            "ttm_quarter_count": int(len(last_four)),
            "ttm_contiguous": bool(contiguous),
            "revenue_ttm": revenue_ttm,
            "operating_income_ttm": operating_income_ttm,
            "parent_net_income_ttm": parent_net_income_ttm,
            "cfo_ttm": cfo_ttm,
            "parent_equity_latest": _numeric(latest.get("parent_equity_latest")),
            "interest_bearing_debt_latest": _numeric(latest.get("interest_bearing_debt_latest")),
            "preferred_equity_latest": _numeric(latest.get("preferred_equity_latest")),
            "noncontrolling_interest_latest": _numeric(latest.get("noncontrolling_interest_latest")),
            "cash_and_cash_equivalents_latest": _numeric(
                latest.get("cash_and_cash_equivalents_latest")
            ),
        }

        payload["per_ttm"], payload["per_ttm_na_reason"] = _safe_ratio(
            payload["market_cap_asof"], payload["parent_net_income_ttm"]
        )
        payload["pbr"], payload["pbr_na_reason"] = _safe_ratio(
            payload["market_cap_asof"], payload["parent_equity_latest"]
        )
        payload["psr_ttm"], payload["psr_ttm_na_reason"] = _safe_ratio(
            payload["market_cap_asof"], payload["revenue_ttm"]
        )
        enterprise_value, ev_reason = _enterprise_value(payload)
        payload["enterprise_value"] = enterprise_value
        payload["enterprise_value_na_reason"] = ev_reason
        if enterprise_value is None:
            payload["ev_to_operating_income_ttm"] = None
            payload["ev_to_operating_income_ttm_na_reason"] = ev_reason
        else:
            (
                payload["ev_to_operating_income_ttm"],
                payload["ev_to_operating_income_ttm_na_reason"],
            ) = _safe_ratio(enterprise_value, payload["operating_income_ttm"])
        (
            payload["cfo_conversion_ttm"],
            payload["cfo_conversion_ttm_na_reason"],
        ) = _safe_ratio(payload["cfo_ttm"], payload["operating_income_ttm"])

        invalid_contract = bool(len(invalid_pit)) or payload["market_cap_status"] != "PASS"
        complete_core = all(
            payload[field] is not None
            for field in ("per_ttm", "pbr", "psr_ttm")
        )
        complete_all = complete_core and payload["ev_to_operating_income_ttm"] is not None
        payload["valuation_input_status"] = (
            "INVALID_PIT_OR_MARKET_DATE"
            if invalid_contract
            else "PASS_EXACT_SCOPE_AND_ASOF"
            if len(security_panel)
            else "CONTRACTUAL_NA_NO_EXACT_FINANCIAL_INPUT"
        )
        payload["valuation_status"] = (
            "INVALID_INPUT" if invalid_contract else "COMPLETE" if complete_all else "PARTIAL_NA"
        )
        payload["provider_multiples_used"] = False
        payload["ev_proxy_used"] = False
        payload["total_net_income_fallback_used"] = False
        payload["total_equity_fallback_used"] = False
        valuation_rows.append(payload)

        recent = security_panel.tail(3).reset_index(drop=True)
        financial_payload = dict(payload)
        labels = (
            ("quarter_minus_2", "q_minus_2"),
            ("quarter_minus_1", "q_minus_1"),
            ("latest_quarter", "latest_q"),
        )
        for index, (period_label, value_label) in enumerate(labels):
            if index < len(recent):
                period_row = recent.iloc[index]
                financial_payload[f"{period_label}_period"] = (
                    f"{int(period_row['year'])}Q{int(period_row['quarter'])}"
                )
                financial_payload[f"revenue_{value_label}"] = _numeric(
                    period_row.get("revenue_quarter")
                )
                financial_payload[f"operating_income_{value_label}"] = _numeric(
                    period_row.get("operating_income_quarter")
                )
            else:
                financial_payload[f"{period_label}_period"] = pd.NA
                financial_payload[f"revenue_{value_label}"] = pd.NA
                financial_payload[f"operating_income_{value_label}"] = pd.NA
        financial_rows.append(financial_payload)

    valuation = pd.DataFrame.from_records(valuation_rows)
    financials = pd.DataFrame.from_records(financial_rows)

    diagnostic_base_columns = [
        column
        for column in selected.columns
        if column in {
            "ticker",
            "name",
            "model_rank",
            "model_score",
            "quality_penalty",
            "quality_penalty_total",
            "quality_penalty_reason",
        }
        or column.endswith("__contrib")
    ]
    diagnostics = selected[diagnostic_base_columns].copy()
    if "quality_penalty" not in diagnostics.columns:
        diagnostics["quality_penalty"] = _first_present(
            diagnostics, ["quality_penalty_total"]
        )
    if "quality_penalty_reason" not in diagnostics.columns:
        diagnostics["quality_penalty_reason"] = pd.NA
    diagnostic_values = valuation[
        [
            "ticker",
            "cfo_conversion_ttm",
            "cfo_conversion_ttm_na_reason",
            "per_ttm",
            "pbr",
            "psr_ttm",
            "ev_to_operating_income_ttm",
            "valuation_status",
        ]
    ]
    diagnostics = diagnostics.merge(diagnostic_values, on="ticker", how="left", validate="one_to_one")
    if industry_mapping is not None:
        industry = _normalize_ticker_frame(pd.DataFrame(industry_mapping), "industry_mapping")
        industry_columns = [
            column
            for column in (
                "ticker",
                "official_industry_code",
                "official_industry_name",
                "official_industry_source",
                "advisor_sector",
                "advisor_sector_source",
            )
            if column in industry.columns
        ]
        industry = industry[industry_columns]
        if industry["ticker"].duplicated().any():
            raise ValuationContractError("industry_mapping contains duplicate tickers")
        diagnostics = diagnostics.merge(industry, on="ticker", how="left", validate="one_to_one")

    contractual_na = valuation.loc[valuation["valuation_status"].eq("PARTIAL_NA"), "ticker"].tolist()
    invalid = valuation.loc[valuation["valuation_status"].eq("INVALID_INPUT"), "ticker"].tolist()
    qa = {
        "contract_version": VALUATION_CONTRACT_VERSION,
        "status": (
            "FAIL_CONTRACT_VIOLATION"
            if invalid
            else "PASS_WITH_CONTRACTUAL_NA"
            if contractual_na
            else "PASS"
        ),
        "model_information_asof": info_asof,
        "valuation_asof": value_asof,
        "statement_scope": scope,
        "selected_security_count": int(len(selected)),
        "complete_valuation_count": int(valuation["valuation_status"].eq("COMPLETE").sum()),
        "contractual_na_count": int(len(contractual_na)),
        "contractual_na_tickers": contractual_na,
        "invalid_input_count": int(len(invalid)),
        "invalid_input_tickers": invalid,
        "provider_multiples_used": False,
        "ev_proxy_used": False,
        "total_net_income_fallback_used": False,
        "total_equity_fallback_used": False,
        "calculation_units": "KRW_UNROUNDED",
        "cfo_conversion_scored": False,
        "cfo_conversion_use": "NON_SCORING_ADVISOR_DIAGNOSTIC_AND_FUTURE_BACKTEST_CANDIDATE",
        # Properly reason-coded NA values (non-positive denominators or an
        # unmapped EV component) are a contract outcome, not a missing layer.
        "advisor_report_blocker": "VALUATION_INPUT_CONTRACT_VIOLATION" if invalid else None,
    }
    return ValuationArtifacts(
        valuation_qa=valuation,
        selected_security_financials=financials,
        selected_security_diagnostics=diagnostics,
        qa=qa,
    )


def _write_new_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(text)


def write_valuation_artifacts(artifacts: ValuationArtifacts, output_dir: str | Path) -> dict[str, Path]:
    """Write the four required V2 valuation artifacts without overwriting."""

    root = Path(output_dir)
    paths = {
        "valuation_qa_csv": root / "valuation_qa.csv",
        "valuation_qa_json": root / "valuation_qa.json",
        "selected_security_financials": root / "selected_security_financials.csv",
        "selected_security_diagnostics": root / "selected_security_diagnostics.csv",
    }
    _write_new_text(paths["valuation_qa_csv"], artifacts.valuation_qa.to_csv(index=False))
    _write_new_text(
        paths["valuation_qa_json"],
        json.dumps(artifacts.qa, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )
    _write_new_text(
        paths["selected_security_financials"],
        artifacts.selected_security_financials.to_csv(index=False),
    )
    _write_new_text(
        paths["selected_security_diagnostics"],
        artifacts.selected_security_diagnostics.to_csv(index=False),
    )
    return paths


__all__ = [
    "EXACT_FLOW_ACCOUNT_IDS",
    "EXACT_STOCK_ACCOUNT_IDS",
    "ExactFinancialPanelResult",
    "VALUATION_CONTRACT_VERSION",
    "ValuationArtifacts",
    "ValuationContractError",
    "build_exact_quarterly_financials_from_dart_cache",
    "build_valuation_artifacts",
    "write_valuation_artifacts",
]
