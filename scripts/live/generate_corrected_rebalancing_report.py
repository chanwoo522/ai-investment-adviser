from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
import sys
from pathlib import Path
from typing import Iterable

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.common.security_id import normalize_security_id_series


REPORT_SECTIONS = (
    "1. 실제 리밸런싱 실행 근거",
    "2. 직전 실제 집행 포트폴리오 성과",
    "3. KRX 300 대비 성과",
    "4. 종목별 성과·기여도",
    "5. 모델 권고 요약",
    "6. 사용자 최종 집행안",
    "7. 모델 권고와 최종 집행안의 차이",
    "8. 최종 거래계획",
    "9. 종목별 팩터 및 밸류에이션",
    "10. 데이터·계산 QA 요약",
)

REPORT_MACHINE_SCHEMA_VERSION = 2
REPORT_PROVENANCE_SCHEMA_VERSION = 1
REPORT_CONTRACT = "CORRECTED_REBALANCING_REPORT"
FORBIDDEN_BENCHMARK_IDENTIFIERS = {"229200", "292190", "304760"}
REPORT_INPUT_ARTIFACTS = {
    "model_portfolio": "model_portfolio",
    "final_execution": "final_execution",
    "valuation_qa": "valuation_qa",
    "scores": "scores",
    "performance_summary": "performance_summary",
    "performance_daily": "performance_daily",
    "performance_contribution": "performance_contribution",
    "performance_reconciliation": "performance_reconciliation",
    "execution_attribution": "execution_attribution",
    "execution_attribution_summary": "execution_attribution_summary",
    "benchmark_qa": "benchmark_qa",
    "benchmark_meta": "benchmark_meta",
    "benchmark_raw_index_master": "benchmark_raw_index_master",
    "benchmark_raw_index_series": "benchmark_raw_index_series",
    "industry_audit": "industry_audit",
}
EXECUTION_PROVENANCE_FIELDS = (
    "financial_data_cutoff_date", "pipeline_asof", "report_created_at", "first_fill_date",
    "last_fill_date", "post_trade_snapshot_date", "performance_start_date",
    "performance_start_basis", "performance_start_source_path",
    "performance_start_source_sha256", "actual_execution_proven", "opening_nav_basis",
    "model_target_date", "model_pipeline_asof",
)
EXECUTION_EVIDENCE_FIELDS = (
    "execution_evidence_source_type", "execution_evidence_path",
    "execution_evidence_sha256", "execution_evidence_certification_path",
    "execution_evidence_certification_sha256",
)
EXECUTION_ATTRIBUTION_COLUMNS = (
    "ticker", "name", "first_fill_date", "last_fill_date", "valuation_date",
    "fill_count", "buy_qty", "sell_qty", "gross_buy_value", "gross_sell_value",
    "net_execution_cashflow", "trading_cost", "gross_mark_to_last_fill_close_pnl",
    "net_mark_to_last_fill_close_pnl", "attribution_basis",
)

SCORELESS_MODEL_FIELDS = (
    "model_score", "model_score_rank", "model_score_adj", "model_score_adj_rank",
    "selection_bucket",
)
SCORELESS_EXIT_REASON_MARKERS = ("no_feature_history", "not_in_target_cohort")


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_csv(path: str | Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"ticker": "string"})
    if "ticker" in frame:
        frame["ticker"] = normalize_security_id_series(frame["ticker"])
    return frame


def _json(path: str | Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _number(value) -> float | None:
    parsed = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return None if pd.isna(parsed) else float(parsed)


def _krw(value) -> str:
    parsed = _number(value)
    return "NA" if parsed is None else f"₩{parsed:,.0f}"


def _qty(value) -> str:
    parsed = _number(value)
    return "NA" if parsed is None else f"{int(parsed):,}"


def _pct(value, digits: int = 2) -> str:
    parsed = _number(value)
    return "NA" if parsed is None else f"{parsed * 100:.{digits}f}%"


def _multiple(value) -> str:
    parsed = _number(value)
    return "NA" if parsed is None else f"{parsed:.2f}"


def _text(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)) or pd.isna(value):
        return "NA"
    return str(value)


def _records(frame: pd.DataFrame) -> list[dict]:
    return json.loads(frame.to_json(orient="records", force_ascii=False, date_format="iso"))


def _require_columns(frame: pd.DataFrame, required: set[str], label: str) -> None:
    missing = sorted(required - set(frame))
    if missing:
        raise ValueError(f"{label} schema missing: {missing}")


def _as_dates(frame: pd.DataFrame, label: str) -> pd.Series:
    dates = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    if dates.isna().any():
        raise ValueError(f"{label} contains invalid dates")
    if dates.duplicated().any():
        duplicates = dates.loc[dates.duplicated(False)].dt.strftime("%Y-%m-%d").tolist()
        raise ValueError(f"{label} contains duplicate dates: {duplicates}")
    if not dates.is_monotonic_increasing:
        raise ValueError(f"{label} dates must be sorted ascending")
    return dates


def _numeric(frame: pd.DataFrame, column: str, label: str) -> pd.Series:
    values = pd.to_numeric(frame[column], errors="coerce")
    if values.isna().any():
        raise ValueError(f"{label}.{column} contains missing/non-numeric values")
    return values.astype(float)


def _series_close(
    left: pd.Series,
    right: pd.Series,
    *,
    atol: float = 1e-10,
    rtol: float = 1e-9,
) -> bool:
    lhs = pd.to_numeric(left, errors="coerce")
    rhs = pd.to_numeric(right, errors="coerce")
    if lhs.isna().any() or rhs.isna().any() or len(lhs) != len(rhs):
        return False
    tolerance = atol + rtol * rhs.abs()
    return bool((lhs.sub(rhs).abs() <= tolerance).all())


def _scalar_close(left, right, *, atol: float = 1e-10, rtol: float = 1e-9) -> bool:
    lhs, rhs = _number(left), _number(right)
    if lhs is None or rhs is None:
        return False
    return abs(lhs - rhs) <= atol + rtol * abs(rhs)


def _require_sha256(value, label: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-fA-F]{64}", value) is None:
        raise ValueError(f"{label} must be a SHA-256 hex digest")


def _is_conditional_interval_performance(performance_summary: dict) -> bool:
    return (
        performance_summary.get("performance_production_ready") is False
        and performance_summary.get("performance_cost_basis_status")
        == "OPENING_BASIS_CERTIFIED_INTERVAL_ACTIVITY_UNKNOWN"
        and performance_summary.get("performance_calculation_method")
        == "CONDITIONAL_STATIC_SHADOW_LIABILITY"
        and performance_summary.get("performance_interval_activity_proven") is False
        and performance_summary.get("performance_blocking_failure")
        == "PERFORMANCE_INTERVAL_ACTIVITY_NOT_PROVEN"
        and performance_summary.get("interval_activity_status") == "UNKNOWN"
        and performance_summary.get("external_cash_flows") is None
    )


def _opening_balance_provenance_label(
    performance_summary: dict,
    opening_balance,
) -> str:
    if not _is_conditional_interval_performance(performance_summary):
        return "certified_opening_cash"
    return (
        "conditional_signed_settlement_liability"
        if float(opening_balance) < 0
        else "conditional_signed_settlement_balance"
    )


def _validate_provenance(
    provenance: dict,
    performance_summary: dict,
    *,
    asof: str,
    target: str,
) -> None:
    required = {
        "schema_version", "report_contract", "correction_run_id", "created_at_utc",
        "asof", "target", "source_run_id", "source_manifest_sha256",
        "source_run_fingerprint_sha256", "production_latest_sha256_before",
        "actual_execution_proven", "actual_execution_audit", "previous_execution",
        "actual_orders_submitted", "input_artifacts", "immutable_report_input_baseline",
    }
    required.update(EXECUTION_PROVENANCE_FIELDS)
    missing = sorted(required - set(provenance))
    if missing:
        raise ValueError(f"report provenance missing: {missing}")
    if provenance.get("schema_version") != REPORT_PROVENANCE_SCHEMA_VERSION:
        raise ValueError("unsupported report provenance schema_version")
    if provenance.get("report_contract") != REPORT_CONTRACT:
        raise ValueError("report provenance contract mismatch")
    if (str(provenance.get("asof")), str(provenance.get("target"))) != (asof, target):
        raise ValueError("report provenance ASOF/TARGET mismatch")
    if not str(provenance.get("correction_run_id", "")).strip() or not str(provenance.get("source_run_id", "")).strip():
        raise ValueError("report provenance run identifiers are required")
    created_at = pd.to_datetime(provenance.get("created_at_utc"), errors="coerce", utc=True)
    if pd.isna(created_at):
        raise ValueError("report provenance created_at_utc is invalid")
    for key in (
        "source_manifest_sha256", "source_run_fingerprint_sha256",
        "production_latest_sha256_before",
    ):
        _require_sha256(provenance.get(key), f"report provenance {key}")
    if provenance.get("actual_execution_proven") is not True:
        raise ValueError("ACTUAL_REBALANCE_DATE_NOT_PROVEN")
    if provenance.get("actual_orders_submitted") is not False:
        raise ValueError("report provenance must state actual_orders_submitted=false")
    chronology = provenance.get("actual_execution_chronology")
    chronology_override_required = provenance.get(
        "chronology_override_disclosure_required"
    ) is True
    if chronology_override_required:
        if not isinstance(chronology, dict):
            raise ValueError("user-confirmed chronology override provenance is required")
        expected_chronology = {
            "mode": "USER_CONFIRMED_BROKER_STATEMENT",
            "supersedes_prior_q1_after_filing_interpretation": True,
            "prior_interpretation_status": "RETRACTED_BY_NEWER_USER_CONFIRMATION",
            "unknown_date_policy": "EXPLICIT_UNKNOWN_NO_INFERENCE",
            "report_disclosure_required": True,
        }
        if any(chronology.get(key) != value for key, value in expected_chronology.items()):
            raise ValueError("user-confirmed chronology override contract mismatch")
        if str(chronology.get("user_confirmed_execution_date")) != str(
            provenance.get("performance_start_date")
        ):
            raise ValueError("user-confirmed execution date differs from performance start")
        for field in ("financial_data_cutoff_date", "pipeline_asof", "report_created_at"):
            if chronology.get(field) is not None or provenance.get(field) not in (None, "UNKNOWN"):
                raise ValueError(f"chronology override must preserve {field} as UNKNOWN")
        if not str(provenance.get("chronology_override_disclosure", "")).strip():
            raise ValueError("chronology override disclosure text is required")

    audit = provenance.get("actual_execution_audit")
    if not isinstance(audit, dict):
        raise ValueError("report provenance actual_execution_audit must be an object")
    audit_required = {"path", "sha256", "status", "proven_rebalance_date", *EXECUTION_EVIDENCE_FIELDS}
    if audit_required - set(audit):
        raise ValueError(f"actual execution audit provenance missing: {sorted(audit_required - set(audit))}")
    if audit.get("status") != "PROVEN":
        raise ValueError("ACTUAL_REBALANCE_DATE_NOT_PROVEN")
    _require_sha256(audit.get("sha256"), "actual execution audit sha256")
    if audit.get("execution_evidence_source_type") not in {
        "FILL_LEDGER", "POST_TRADE_ACCOUNT_SNAPSHOT", "CERTIFIED_MANIFEST",
        "USER_CONFIRMED_BROKER_STATEMENT_IMAGES",
    }:
        raise ValueError("actual execution evidence source type is not certified")
    if (
        not str(audit.get("execution_evidence_path", "")).strip()
        or not str(audit.get("execution_evidence_certification_path", "")).strip()
    ):
        raise ValueError("actual execution evidence source/certification paths are required")
    _require_sha256(audit.get("execution_evidence_sha256"), "actual execution evidence sha256")
    _require_sha256(
        audit.get("execution_evidence_certification_sha256"),
        "actual execution evidence certification sha256",
    )

    previous = provenance.get("previous_execution")
    if not isinstance(previous, dict):
        raise ValueError("report provenance previous_execution must be an object")
    previous_required = {
        "rebalance_date", "performance_source_type", "performance_source_path",
        "performance_source_sha256", "certification_path", "certification_sha256",
        "opening_nav", "opening_cash",
    }
    if previous_required - set(previous):
        raise ValueError(f"previous execution provenance missing: {sorted(previous_required - set(previous))}")
    if previous.get("performance_source_type") not in {
        "FILL_LEDGER", "POST_TRADE_SNAPSHOT", "CERTIFIED_MANIFEST",
        "USER_CONFIRMED_BROKER_STATEMENT_IMAGES",
    }:
        raise ValueError("previous execution must use a certified performance source")
    if not str(previous.get("performance_source_path", "")).strip() or not str(previous.get("certification_path", "")).strip():
        raise ValueError("certified performance source and certification paths are required")
    _require_sha256(previous.get("performance_source_sha256"), "performance source sha256")
    _require_sha256(previous.get("certification_sha256"), "performance certification sha256")
    opening_nav, opening_cash = _number(previous.get("opening_nav")), _number(previous.get("opening_cash"))
    signed_settlement_cash = (
        chronology_override_required
        and isinstance(chronology, dict)
        and chronology.get("opening_cash_basis") == "SIGNED_BROKER_SETTLEMENT_BALANCE"
        and chronology.get("signed_settlement_balance_verified") is True
        and _scalar_close(chronology.get("opening_cash"), opening_cash)
    )
    if (
        opening_nav is None
        or opening_nav <= 0
        or opening_cash is None
        or (opening_cash < 0 and not signed_settlement_cash)
        or opening_cash > opening_nav
    ):
        raise ValueError("certified opening_nav/opening_cash contract is invalid")

    proven_date = str(audit.get("proven_rebalance_date"))
    prior_date = str(previous.get("rebalance_date"))
    summary_start = str(performance_summary.get("start_date"))
    if proven_date != prior_date:
        raise ValueError("proven execution date does not match previous-execution provenance")
    if str(provenance.get("performance_start_date")) != summary_start:
        raise ValueError("report performance_start_date does not match performance summary")
    if str(provenance.get("performance_start_basis")) not in {
        "POST_TRADE_ACCOUNT_SNAPSHOT", "VERIFIED_FILL_LEDGER", "CERTIFIED_MANIFEST",
        "USER_CONFIRMED_BROKER_STATEMENT_LAST_FILL",
    }:
        raise ValueError("report performance_start_basis is not an allowed audited policy")
    if Path(str(provenance.get("performance_start_source_path"))).resolve() != Path(str(previous["performance_source_path"])).resolve():
        raise ValueError("report performance_start_source_path mismatch")
    if str(provenance.get("performance_start_source_sha256", "")).lower() != str(previous["performance_source_sha256"]).lower():
        raise ValueError("report performance_start_source_sha256 mismatch")
    if not str(provenance.get("opening_nav_basis", "")).strip():
        raise ValueError("report opening_nav_basis is required for a proven run")
    if performance_summary.get("source_type") != previous.get("performance_source_type"):
        raise ValueError("performance source_type does not match actual execution provenance")
    summary_path = str(performance_summary.get("source_path", "")).strip()
    if not summary_path or Path(summary_path).resolve() != Path(str(previous["performance_source_path"])).resolve():
        raise ValueError("performance source_path does not match certified execution provenance")
    conditional_interval = _is_conditional_interval_performance(performance_summary)
    certified_interval = (
        performance_summary.get("performance_production_ready") is True
        and performance_summary.get("performance_cost_basis_status") == "CERTIFIED"
    )
    if not (conditional_interval or certified_interval):
        raise ValueError("performance cost-basis publication state is unsupported")
    if conditional_interval:
        if (
            provenance.get("production_ready") is not False
            or provenance.get("performance_interval_activity_proven") is not False
            or provenance.get("blocking_failure")
            != "PERFORMANCE_INTERVAL_ACTIVITY_NOT_PROVEN"
        ):
            raise ValueError("conditional performance must fail the production publication gate")
    elif (
        provenance.get("production_ready", True) is not True
        or provenance.get("performance_interval_activity_proven", True) is not True
        or provenance.get("blocking_failure") is not None
    ):
        raise ValueError("certified performance publication provenance is inconsistent")
    if str(performance_summary.get("source_basis_date")) != summary_start:
        raise ValueError("certified performance source basis date mismatch")
    if str(performance_summary.get("source_sha256", "")).lower() != str(previous["performance_source_sha256"]).lower():
        raise ValueError("performance source SHA-256 mismatch")
    if Path(str(performance_summary.get("source_certification_path", ""))).resolve() != Path(str(previous["certification_path"])).resolve():
        raise ValueError("performance certification path mismatch")
    if str(performance_summary.get("source_certification_sha256", "")).lower() != str(previous["certification_sha256"]).lower():
        raise ValueError("performance certification SHA-256 mismatch")
    if not _scalar_close(performance_summary.get("certified_opening_nav"), opening_nav):
        raise ValueError("performance certified opening NAV mismatch")
    if not _scalar_close(performance_summary.get("certified_opening_cash"), opening_cash):
        raise ValueError("performance certified opening cash mismatch")

    artifacts = provenance.get("input_artifacts")
    if not isinstance(artifacts, dict):
        raise ValueError("report provenance input_artifacts must be an object")
    missing_artifacts = sorted(set(REPORT_INPUT_ARTIFACTS) - set(artifacts))
    if missing_artifacts:
        raise ValueError(f"report provenance input_artifacts missing: {missing_artifacts}")
    for name in REPORT_INPUT_ARTIFACTS:
        item = artifacts.get(name)
        if not isinstance(item, dict) or not str(item.get("path", "")).strip():
            raise ValueError(f"report provenance artifact is invalid: {name}")
        _require_sha256(item.get("sha256"), f"report provenance artifact {name}")
    immutable_baseline = provenance.get("immutable_report_input_baseline")
    baseline_mapping = {
        "model_portfolio": "model_portfolio",
        "final_execution": "final_execution",
        "valuation_csv": "valuation_qa",
    }
    if not isinstance(immutable_baseline, dict):
        raise ValueError("immutable model/overlay/valuation baseline is missing")
    for baseline_name, report_input_name in baseline_mapping.items():
        baseline_item = immutable_baseline.get(baseline_name)
        if not isinstance(baseline_item, dict):
            raise ValueError(f"immutable report baseline missing: {baseline_name}")
        if str(baseline_item.get("sha256", "")).lower() != str(artifacts[report_input_name]["sha256"]).lower():
            raise ValueError(f"immutable report input changed: {baseline_name}")


def _validate_performance_evidence(
    performance_summary: dict,
    performance_daily: pd.DataFrame,
    performance_contribution: pd.DataFrame,
    benchmark_qa: pd.DataFrame,
    benchmark_meta: dict,
    *,
    asof: str,
) -> None:
    required_summary = {
        "start_date", "end_date", "initial_nav", "invested_base", "cash_after_rebalance",
        "last_nav", "cum_return", "max_drawdown", "drawdown_peak_date", "max_drawdown_date", "num_positions",
        "start_cash", "end_cash", "total_trading_costs", "external_cash_flow_treatment",
        "external_cash_flows", "dividends",
        "nav_dividend_treatment", "cash_dividends_included", "source_type", "source_path",
        "source_sha256", "source_certification_path", "source_certification_sha256",
        "performance_production_ready", "performance_cost_basis_status",
        "certified_opening_nav", "certified_opening_cash",
        "contribution_reconciliation_status", "contribution_reconciliation_residual",
        "position_pnl_identity_status",
        "benchmark_cum_return", "active_return", "benchmark_alignment_policy",
        "benchmark_missing_dates", "benchmark_extra_dates",
        "benchmark_cum_return_filled_forward", "benchmark_cum_return_backfilled",
    }
    missing_summary = sorted(required_summary - set(performance_summary))
    if missing_summary:
        raise ValueError(f"performance summary contract missing: {missing_summary}")
    if performance_summary.get("nav_dividend_treatment") != "EXCLUDED" or performance_summary.get("cash_dividends_included") is not False:
        raise ValueError("portfolio dividend treatment is not proven; benchmark variant cannot be selected")
    if performance_summary.get("benchmark_alignment_policy") != "EXACT_PORTFOLIO_TRADING_DATES_NO_FILL":
        raise ValueError("benchmark alignment must be exact with no fill")
    if performance_summary.get("benchmark_cum_return_filled_forward") is not False or performance_summary.get("benchmark_cum_return_backfilled") is not False:
        raise ValueError("benchmark fill/backfill is forbidden")
    if int(performance_summary.get("benchmark_missing_dates")) != 0 or int(performance_summary.get("benchmark_extra_dates")) != 0:
        raise ValueError("benchmark and portfolio trading dates are not an exact match")
    conditional_interval = _is_conditional_interval_performance(performance_summary)
    certified_interval = (
        performance_summary.get("performance_production_ready") is True
        and performance_summary.get("performance_cost_basis_status") == "CERTIFIED"
    )
    if (
        not (conditional_interval or certified_interval)
        or performance_summary.get("contribution_reconciliation_status") != "PASS"
        or performance_summary.get("position_pnl_identity_status") != "PASS"
        or not _scalar_close(performance_summary.get("contribution_reconciliation_residual"), 0.0)
    ):
        raise ValueError("performance/contribution calculation contract is not internally reconciled")

    required_daily = {
        "date", "nav", "cash", "daily_return", "cum_return", "benchmark_daily_return",
        "benchmark_cum_return", "active_return",
    }
    required_benchmark = {
        "date", "index_level", "benchmark_name", "benchmark_identifier", "asset_type",
        "return_type", "source", "index_master_market",
    }
    required_contribution = {
        "ticker", "shares_at_start", "start_date", "start_price", "start_position_value",
        "end_date", "end_price", "end_position_value", "dividends",
        "net_intermediate_trade_cashflow", "trading_cost_allocated", "period_pnl",
        "position_period_return", "contribution_to_total_return", "weight_at_start_nav",
        "weight_at_end_nav", "performance_basis",
    }
    _require_columns(performance_daily, required_daily, "performance daily")
    _require_columns(benchmark_qa, required_benchmark, "benchmark QA")
    _require_columns(performance_contribution, required_contribution, "performance contribution")
    if performance_daily.empty or benchmark_qa.empty or performance_contribution.empty:
        raise ValueError("performance, benchmark, and contribution evidence must be non-empty")

    performance_dates = _as_dates(performance_daily, "performance daily")
    benchmark_dates = _as_dates(benchmark_qa, "benchmark QA")
    if not performance_dates.reset_index(drop=True).equals(benchmark_dates.reset_index(drop=True)):
        raise ValueError("performance and KRX 300 dates must match exactly")
    if performance_dates.iloc[0].strftime("%Y-%m-%d") != str(performance_summary["start_date"]):
        raise ValueError("performance summary start_date mismatch")
    if performance_dates.iloc[-1].strftime("%Y-%m-%d") != str(performance_summary["end_date"]):
        raise ValueError("performance summary end_date mismatch")
    if str(performance_summary["end_date"]) != asof:
        raise ValueError("performance evidence must end exactly on ASOF")
    peak_date = pd.to_datetime(performance_summary.get("drawdown_peak_date"), errors="coerce")
    max_drawdown_date = pd.to_datetime(performance_summary.get("max_drawdown_date"), errors="coerce")
    if (
        pd.isna(peak_date) or pd.isna(max_drawdown_date)
        or not (performance_dates.iloc[0] <= peak_date.normalize() <= performance_dates.iloc[-1])
        or not (performance_dates.iloc[0] <= max_drawdown_date.normalize() <= performance_dates.iloc[-1])
    ):
        raise ValueError("drawdown peak/MDD date lies outside the proven performance window")

    levels = _numeric(benchmark_qa, "index_level", "benchmark QA")
    if levels.le(0).any():
        raise ValueError("KRX 300 index levels must be positive")
    expected_benchmark = levels / float(levels.iloc[0]) - 1.0
    if "benchmark_cum_return" in benchmark_qa and not _series_close(
        benchmark_qa["benchmark_cum_return"], expected_benchmark
    ):
        raise ValueError("benchmark QA cumulative return does not reproduce from index levels")
    if not _series_close(performance_daily["benchmark_cum_return"], expected_benchmark):
        raise ValueError("performance benchmark return does not reproduce from official KRX 300 levels")
    portfolio_return = _numeric(performance_daily, "cum_return", "performance daily")
    active_return = _numeric(performance_daily, "active_return", "performance daily")
    if not _series_close(active_return, portfolio_return - expected_benchmark):
        raise ValueError("active return is inconsistent with portfolio minus KRX 300 return")
    if abs(float(portfolio_return.iloc[0])) > 1e-12 or abs(float(expected_benchmark.iloc[0])) > 1e-12:
        raise ValueError("portfolio and benchmark cumulative returns must start at zero")
    daily_return = _numeric(performance_daily, "daily_return", "performance daily")
    benchmark_daily_return = _numeric(performance_daily, "benchmark_daily_return", "performance daily")
    compounded_portfolio = (1.0 + daily_return).cumprod() - 1.0
    compounded_benchmark = (1.0 + benchmark_daily_return).cumprod() - 1.0
    if not _series_close(compounded_portfolio, portfolio_return):
        raise ValueError("portfolio daily-return compounding does not match endpoint return")
    if not _series_close(compounded_benchmark, expected_benchmark):
        raise ValueError("KRX 300 daily-return compounding does not match endpoint return")
    if "benchmark_price" in performance_daily and not _series_close(
        performance_daily["benchmark_price"], levels, atol=1e-8
    ):
        raise ValueError("performance benchmark_price differs from official KRX 300 index levels")
    if not _scalar_close(performance_summary["cum_return"], portfolio_return.iloc[-1]):
        raise ValueError("performance summary cumulative return mismatch")
    if not _scalar_close(performance_summary["benchmark_cum_return"], expected_benchmark.iloc[-1]):
        raise ValueError("performance summary benchmark return mismatch")
    if not _scalar_close(performance_summary["active_return"], active_return.iloc[-1]):
        raise ValueError("performance summary active return mismatch")
    external_flow_contract_ok = (
        performance_summary.get("external_cash_flows") is None
        and performance_summary.get("external_cash_flow_treatment")
        == "UNKNOWN_NOT_ASSUMED_ZERO"
        if conditional_interval
        else _scalar_close(performance_summary.get("external_cash_flows"), 0.0)
    )
    if (
        not _scalar_close(performance_summary["start_cash"], _numeric(performance_daily, "cash", "performance daily").iloc[0])
        or not _scalar_close(performance_summary["end_cash"], _numeric(performance_daily, "cash", "performance daily").iloc[-1])
        or not external_flow_contract_ok
        or not _scalar_close(performance_summary["dividends"], 0.0)
        or not str(performance_summary["external_cash_flow_treatment"]).strip()
    ):
        raise ValueError("performance cash/dividend/external-flow contract mismatch")

    identifier = str(benchmark_meta.get("benchmark_identifier", "")).strip()
    if identifier in FORBIDDEN_BENCHMARK_IDENTIFIERS or not identifier:
        raise ValueError("official KRX 300 index identifier is missing or is an ETF identifier")
    exact_metadata = {
        "benchmark_name": "KRX 300", "asset_type": "INDEX", "return_type": "PRICE",
        "source": "KRX_OFFICIAL_INDEX", "index_master_market": "KRX",
    }
    for column, expected in exact_metadata.items():
        values = benchmark_qa[column].astype(str).str.strip().drop_duplicates().tolist()
        if values != [expected] or str(benchmark_meta.get(column, "")).strip() != expected:
            raise ValueError(f"official KRX 300 metadata mismatch: {column}")
    benchmark_ids = benchmark_qa["benchmark_identifier"].astype(str).str.strip().drop_duplicates().tolist()
    if benchmark_ids != [identifier]:
        raise ValueError("KRX 300 identifier differs between series and metadata")
    if benchmark_meta.get("collection_status") != "OFFICIAL_INDEX_SUCCESS" or benchmark_meta.get("provisional") is not False:
        raise ValueError("KRX 300 primary official collection is not certified")
    if benchmark_meta.get("fallback_source_path") or benchmark_meta.get("fallback_source_detail"):
        raise ValueError("KRX 300 fallback source is forbidden")
    if str(benchmark_meta.get("first_date")) != str(performance_summary["start_date"]) or str(benchmark_meta.get("last_date")) != asof:
        raise ValueError("KRX 300 metadata date window mismatch")
    if int(benchmark_meta.get("rows", -1)) != len(benchmark_qa):
        raise ValueError("KRX 300 metadata row count mismatch")
    for key in (
        "source_endpoint", "source_endpoint_bld", "collection_timestamp",
        "normalized_artifact_path", "raw_index_master_artifact_path",
        "raw_index_series_artifact_path",
    ):
        if not str(benchmark_meta.get(key, "")).strip():
            raise ValueError(f"KRX 300 benchmark metadata missing: {key}")
    _require_sha256(
        benchmark_meta.get("normalized_artifact_sha256"),
        "KRX 300 normalized artifact sha256",
    )
    _require_sha256(
        benchmark_meta.get("raw_index_master_artifact_sha256"),
        "KRX 300 raw index master artifact sha256",
    )
    _require_sha256(
        benchmark_meta.get("raw_index_series_artifact_sha256"),
        "KRX 300 raw index series artifact sha256",
    )
    raw_artifacts = benchmark_meta.get("raw_artifacts")
    if not isinstance(raw_artifacts, dict) or set(raw_artifacts) != {"index_master", "index_series"}:
        raise ValueError("KRX 300 raw_artifacts contract is incomplete")
    for raw_name in ("index_master", "index_series"):
        raw_item = raw_artifacts.get(raw_name)
        if not isinstance(raw_item, dict) or not str(raw_item.get("path", "")).strip():
            raise ValueError(f"KRX 300 raw artifact metadata is invalid: {raw_name}")
        _require_sha256(raw_item.get("sha256"), f"KRX 300 raw artifact sha256: {raw_name}")
    if benchmark_meta.get("fill_used") is not False:
        raise ValueError("KRX 300 benchmark fill_used must be false")
    if pd.isna(pd.to_datetime(benchmark_meta.get("collection_timestamp"), errors="coerce", utc=True)):
        raise ValueError("KRX 300 collection_timestamp is invalid")

    contribution = performance_contribution.copy()
    contribution["ticker"] = normalize_security_id_series(contribution["ticker"])
    if contribution["ticker"].isna().any() or contribution["ticker"].duplicated().any():
        raise ValueError("performance contribution tickers must be valid and unique")
    start_dates = pd.to_datetime(contribution["start_date"], errors="coerce").dt.normalize()
    end_dates = pd.to_datetime(contribution["end_date"], errors="coerce").dt.normalize()
    if start_dates.isna().any() or not start_dates.eq(performance_dates.iloc[0]).all():
        raise ValueError("performance contribution start_date mismatch")
    if end_dates.isna().any() or not end_dates.eq(performance_dates.iloc[-1]).all():
        raise ValueError("performance contribution end_date mismatch")
    numeric_columns = required_contribution - {
        "ticker", "start_date", "end_date", "position_period_return", "performance_basis",
    }
    numeric = {
        column: _numeric(contribution, column, "performance contribution")
        for column in numeric_columns
    }
    if numeric["shares_at_start"].lt(0).any() or numeric["start_position_value"].lt(0).any() or numeric["end_position_value"].lt(0).any():
        raise ValueError("performance contribution contains negative shares/position values")
    if numeric["dividends"].lt(0).any() or numeric["trading_cost_allocated"].lt(0).any():
        raise ValueError("performance contribution dividends/cost must be non-negative")
    expected_pnl = (
        numeric["end_position_value"]
        - numeric["start_position_value"]
        - numeric["net_intermediate_trade_cashflow"]
        + numeric["dividends"]
        - numeric["trading_cost_allocated"]
    )
    if not _series_close(numeric["period_pnl"], expected_pnl, atol=1e-6):
        raise ValueError("performance contribution period PnL formula mismatch")
    if not _scalar_close(numeric["trading_cost_allocated"].sum(), performance_summary["total_trading_costs"], atol=1e-6):
        raise ValueError("allocated trading costs do not match performance summary")
    if not _scalar_close(numeric["dividends"].sum(), performance_summary["dividends"], atol=1e-6):
        raise ValueError("allocated dividends do not match performance summary")
    reported_position_return = pd.to_numeric(contribution["position_period_return"], errors="coerce")
    positive_start = numeric["start_position_value"].gt(0)
    if (
        not _series_close(
            reported_position_return.loc[positive_start],
            (numeric["period_pnl"] / numeric["start_position_value"]).loc[positive_start],
        )
        or reported_position_return.loc[~positive_start].notna().any()
    ):
        raise ValueError("performance contribution position return formula mismatch")
    initial_nav = float(performance_summary["initial_nav"])
    last_nav = float(performance_summary["last_nav"])
    if initial_nav <= 0 or last_nav <= 0:
        raise ValueError("performance NAV values must be positive")
    if not _series_close(numeric["contribution_to_total_return"], numeric["period_pnl"] / initial_nav):
        raise ValueError("performance contribution total-return formula mismatch")
    if not _series_close(numeric["weight_at_start_nav"], numeric["start_position_value"] / initial_nav):
        raise ValueError("performance contribution start weight formula mismatch")
    if not _series_close(numeric["weight_at_end_nav"], numeric["end_position_value"] / last_nav):
        raise ValueError("performance contribution end weight formula mismatch")
    if not _scalar_close(
        numeric["start_position_value"].sum() + float(performance_summary["certified_opening_cash"]),
        initial_nav,
        atol=1e-6,
    ):
        raise ValueError("performance contribution opening positions/cash do not reconcile to initial NAV")
    daily_nav = _numeric(performance_daily, "nav", "performance daily")
    daily_cash = _numeric(performance_daily, "cash", "performance daily")
    if not _scalar_close(daily_nav.iloc[0], initial_nav, atol=1e-6) or not _scalar_close(daily_nav.iloc[-1], last_nav, atol=1e-6):
        raise ValueError("performance daily NAV endpoints do not match summary")
    if not _scalar_close(
        numeric["end_position_value"].sum() + daily_cash.iloc[-1],
        last_nav,
        atol=1e-6,
    ):
        raise ValueError("performance contribution values do not reconcile to final NAV")
    if not _scalar_close(numeric["contribution_to_total_return"].sum(), performance_summary["cum_return"]):
        raise ValueError("performance contribution sum does not reconcile to portfolio return")
    if not contribution["performance_basis"].astype(str).str.strip().isin({
        "STATIC_POST_TRADE_SNAPSHOT", "TRANSACTION_AWARE_FILL_LEDGER",
    }).all():
        raise ValueError("performance contribution basis is not certified")


def _validate_cli_input_hashes(provenance: dict, input_paths: dict[str, Path]) -> None:
    artifacts = provenance.get("input_artifacts", {})
    if set(input_paths) != set(REPORT_INPUT_ARTIFACTS):
        raise ValueError("internal report input path mapping is incomplete")
    for name, path in input_paths.items():
        if not path.is_file():
            raise FileNotFoundError(path)
        item = artifacts.get(name)
        if not isinstance(item, dict):
            raise ValueError(f"report provenance input artifact missing: {name}")
        recorded_path = Path(str(item.get("path", ""))).resolve()
        if recorded_path != path.resolve():
            raise ValueError(f"report provenance input path mismatch: {name}")
        actual_hash = _sha256(path)
        if str(item.get("sha256", "")).lower() != actual_hash.lower():
            raise ValueError(f"report provenance input hash mismatch: {name}")


def _validate_performance_reconciliation(
    reconciliation: dict,
    performance_summary: dict,
    performance_contribution: pd.DataFrame,
) -> None:
    required = {"schema_version", "status", "start", "end", "pnl", "returns", "tolerance", "residuals", "source_artifacts"}
    if required - set(reconciliation):
        raise ValueError(f"performance reconciliation schema missing: {sorted(required - set(reconciliation))}")
    if reconciliation.get("schema_version") != 1 or reconciliation.get("status") != "PASS":
        raise ValueError("performance reconciliation artifact is not PASS")
    start, end = reconciliation.get("start", {}), reconciliation.get("end", {})
    if (
        str(start.get("date")) != str(performance_summary.get("start_date"))
        or str(end.get("date")) != str(performance_summary.get("end_date"))
        or not _scalar_close(start.get("nav"), performance_summary.get("initial_nav"))
        or not _scalar_close(end.get("nav"), performance_summary.get("last_nav"))
        or not _scalar_close(start.get("cash"), performance_summary.get("start_cash"))
        or not _scalar_close(end.get("cash"), performance_summary.get("end_cash"))
    ):
        raise ValueError("performance reconciliation endpoints mismatch")
    contribution_sum = pd.to_numeric(
        performance_contribution["contribution_to_total_return"], errors="coerce"
    ).sum()
    returns = reconciliation.get("returns", {})
    if (
        not _scalar_close(returns.get("reported_portfolio_return"), performance_summary.get("cum_return"))
        or not _scalar_close(returns.get("contribution_sum"), contribution_sum)
    ):
        raise ValueError("performance reconciliation return totals mismatch")
    residuals = reconciliation.get("residuals")
    tolerances = reconciliation.get("tolerance")
    if not isinstance(residuals, dict) or not isinstance(tolerances, dict):
        raise ValueError("performance reconciliation residual/tolerance contract is invalid")
    money_tolerance = _number(tolerances.get("money"))
    return_tolerance = _number(tolerances.get("return"))
    if money_tolerance is None or return_tolerance is None:
        raise ValueError("performance reconciliation tolerances are missing")
    for key, value in residuals.items():
        parsed = _number(value)
        tolerance = return_tolerance if "return" in key else money_tolerance
        if parsed is None or abs(parsed) > tolerance:
            raise ValueError(f"performance reconciliation residual failed: {key}")


def _validate_execution_attribution(
    attribution: pd.DataFrame,
    summary: dict,
    provenance: dict,
    performance_summary: dict,
) -> None:
    required_summary = {
        "schema_version", "status", "first_fill_date", "last_fill_date",
        "execution_trading_day_count", "fill_count", "trading_cost",
        "gross_mark_to_last_fill_close_pnl", "net_mark_to_last_fill_close_pnl",
        "cost_reconciliation_residual", "attribution_basis",
        "included_in_post_rebalance_performance", "source_type", "source_path",
        "source_sha256", "source_certification_path", "source_certification_sha256",
        "execution_window_type", "attribution_csv_path", "attribution_csv_sha256",
    }
    if required_summary - set(summary):
        raise ValueError(
            "execution attribution summary schema missing: "
            f"{sorted(required_summary - set(summary))}"
        )
    if summary.get("schema_version") != 1:
        raise ValueError("unsupported execution attribution summary schema")
    if summary.get("included_in_post_rebalance_performance") is not False:
        raise ValueError("execution-window attribution must remain separate from portfolio performance")
    paired_fields = {
        "execution_window_attribution_status": "status",
        "execution_window_type": "execution_window_type",
        "execution_window_first_fill_date": "first_fill_date",
        "execution_window_last_fill_date": "last_fill_date",
        "execution_window_trading_day_count": "execution_trading_day_count",
        "execution_window_source_path": "source_path",
        "execution_window_source_sha256": "source_sha256",
        "execution_window_source_certification_path": "source_certification_path",
        "execution_window_source_certification_sha256": "source_certification_sha256",
        "execution_window_attribution_csv_path": "attribution_csv_path",
        "execution_window_attribution_csv_sha256": "attribution_csv_sha256",
    }
    for performance_key, attribution_key in paired_fields.items():
        if performance_summary.get(performance_key) != summary.get(attribution_key):
            raise ValueError(
                f"performance/execution attribution summary mismatch: {performance_key}"
            )
    if performance_summary.get(
        "execution_window_attribution_included_in_post_rebalance_performance"
    ) is not False:
        raise ValueError("execution attribution was mixed into post-rebalance performance")
    evidence = provenance.get("actual_execution_audit", {})
    evidence_type = evidence.get("execution_evidence_source_type")
    status = str(summary.get("status"))
    if status == "NOT_AVAILABLE_NO_CERTIFIED_FILL_LEDGER":
        if summary.get("execution_window_type") != "NOT_APPLICABLE":
            raise ValueError("unavailable execution attribution must be NOT_APPLICABLE")
        if evidence_type == "FILL_LEDGER" or not attribution.empty:
            raise ValueError("certified fill execution evidence requires explicit execution attribution")
        if any(summary.get(key) is not None for key in (
            "source_type", "source_path", "source_sha256",
            "source_certification_path", "source_certification_sha256",
        )):
            raise ValueError("unavailable execution attribution cannot declare a source")
        return
    if status == "NOT_APPLICABLE_MARK_TO_CLOSE_CASH_EQ_UNPRICED":
        if evidence_type != "USER_CONFIRMED_BROKER_STATEMENT_IMAGES":
            raise ValueError("cash-equivalent attribution exception requires user-confirmed evidence")
        if not attribution.empty or summary.get("execution_window_type") != "SAME_DAY":
            raise ValueError("cash-equivalent mark-to-close exception must be same-day and row-empty")
        if (
            str(summary.get("first_fill_date")) != str(provenance.get("first_fill_date"))
            or str(summary.get("last_fill_date")) != str(provenance.get("last_fill_date"))
            or str(summary.get("last_fill_date"))
            != str(provenance.get("actual_execution_audit", {}).get("proven_rebalance_date"))
        ):
            raise ValueError("user-confirmed execution window differs from audited dates")
        if summary.get("cash_cost_reconciliation_status") != "PASS":
            raise ValueError("user-confirmed execution cash/cost reconciliation is not PASS")
        residual = _number(summary.get("cost_reconciliation_residual"))
        gross_buy = _number(summary.get("gross_buy_value"))
        gross_sell = _number(summary.get("gross_sell_value"))
        costs = _number(summary.get("trading_cost"))
        net_cashflow = _number(summary.get("net_execution_cashflow"))
        settlement_cash_effect = _number(summary.get("settlement_cash_effect"))
        final_balance = _number(summary.get("final_deposit_balance"))
        final_signed_balance = _number(summary.get("final_signed_settlement_balance"))
        signed_liability = _number(summary.get("signed_settlement_liability"))
        opening_cash = _number(provenance.get("previous_execution", {}).get("opening_cash"))
        if None in {
            residual, gross_buy, gross_sell, costs, net_cashflow,
            settlement_cash_effect, final_balance, final_signed_balance,
            signed_liability, opening_cash,
        }:
            raise ValueError("user-confirmed execution cash/cost totals are incomplete")
        if (
            abs(float(residual)) > 1e-8
            or not _scalar_close(net_cashflow, float(gross_sell) - float(gross_buy) - float(costs))
            or not _scalar_close(settlement_cash_effect, net_cashflow)
            or not _scalar_close(final_balance, opening_cash)
            or not _scalar_close(final_signed_balance, opening_cash)
            or not _scalar_close(signed_liability, max(-float(opening_cash), 0.0))
            or summary.get("gross_mark_to_last_fill_close_pnl") is not None
            or summary.get("net_mark_to_last_fill_close_pnl") is not None
        ):
            raise ValueError("user-confirmed execution cash/cost totals do not reconcile")
        for summary_key, evidence_key in (
            ("source_type", "execution_evidence_source_type"),
            ("source_path", "execution_evidence_path"),
            ("source_sha256", "execution_evidence_sha256"),
            ("source_certification_path", "execution_evidence_certification_path"),
            ("source_certification_sha256", "execution_evidence_certification_sha256"),
        ):
            expected = evidence.get(evidence_key)
            actual = summary.get(summary_key)
            if summary_key.endswith("path"):
                if Path(str(actual)).resolve() != Path(str(expected)).resolve():
                    raise ValueError("user-confirmed execution source path mismatch")
            elif str(actual).lower() != str(expected).lower():
                raise ValueError("user-confirmed execution source provenance mismatch")
        return
    if status != "PASS":
        raise ValueError(f"execution attribution is not PASS/explicitly unavailable: {status}")
    if summary.get("execution_window_type") not in {"SAME_DAY", "MULTI_DAY"}:
        raise ValueError("execution attribution window type is invalid")
    if evidence_type != "FILL_LEDGER":
        raise ValueError("execution attribution PASS requires certified FILL_LEDGER evidence")
    _require_columns(attribution, set(EXECUTION_ATTRIBUTION_COLUMNS), "execution attribution")
    if attribution.empty:
        raise ValueError("execution attribution PASS cannot be empty")
    frame = attribution.copy()
    frame["ticker"] = normalize_security_id_series(frame["ticker"])
    if frame["ticker"].isna().any() or frame["ticker"].duplicated().any():
        raise ValueError("execution attribution tickers must be valid and unique")
    for date_column in ("first_fill_date", "last_fill_date", "valuation_date"):
        dates = pd.to_datetime(frame[date_column], errors="coerce").dt.normalize()
        if dates.isna().any():
            raise ValueError(f"execution attribution has invalid {date_column}")
        if date_column == "valuation_date" and not dates.eq(
            pd.Timestamp(str(summary["last_fill_date"])).normalize()
        ).all():
            raise ValueError("execution attribution valuation date must equal last fill date")
    if (
        str(summary.get("first_fill_date")) != str(provenance.get("first_fill_date"))
        or str(summary.get("last_fill_date")) != str(provenance.get("last_fill_date"))
        or str(summary.get("last_fill_date"))
        != str(provenance.get("actual_execution_audit", {}).get("proven_rebalance_date"))
    ):
        raise ValueError("execution attribution fill window differs from audited execution provenance")
    day_count = int(summary.get("execution_trading_day_count", 0))
    if day_count < 1 or (
        (day_count == 1) != (summary.get("execution_window_type") == "SAME_DAY")
    ):
        raise ValueError("execution attribution day-count/type mismatch")
    numeric_columns = {
        "fill_count", "buy_qty", "sell_qty", "gross_buy_value", "gross_sell_value",
        "net_execution_cashflow", "trading_cost", "gross_mark_to_last_fill_close_pnl",
        "net_mark_to_last_fill_close_pnl",
    }
    numeric = {column: _numeric(frame, column, "execution attribution") for column in numeric_columns}
    if any(numeric[column].lt(0).any() for column in (
        "fill_count", "buy_qty", "sell_qty", "gross_buy_value", "gross_sell_value", "trading_cost",
    )):
        raise ValueError("execution attribution contains negative quantities/values/costs")
    if not _series_close(
        numeric["net_mark_to_last_fill_close_pnl"],
        numeric["gross_mark_to_last_fill_close_pnl"] - numeric["trading_cost"],
        atol=1e-6,
    ):
        raise ValueError("execution attribution gross/cost/net identity mismatch")
    for column, summary_key in (
        ("fill_count", "fill_count"),
        ("trading_cost", "trading_cost"),
        ("gross_mark_to_last_fill_close_pnl", "gross_mark_to_last_fill_close_pnl"),
        ("net_mark_to_last_fill_close_pnl", "net_mark_to_last_fill_close_pnl"),
    ):
        if not _scalar_close(numeric[column].sum(), summary.get(summary_key), atol=1e-6):
            raise ValueError(f"execution attribution summary total mismatch: {summary_key}")
    if not _scalar_close(summary.get("cost_reconciliation_residual"), 0.0, atol=1e-6):
        raise ValueError("execution attribution cost reconciliation residual is nonzero")
    if not frame["attribution_basis"].astype(str).eq("LAST_FILL_DATE_OFFICIAL_CLOSE").all():
        raise ValueError("execution attribution basis mismatch")
    if summary.get("attribution_basis") != "LAST_FILL_DATE_OFFICIAL_CLOSE":
        raise ValueError("execution attribution summary basis mismatch")
    if summary.get("source_type") != "FILL_LEDGER":
        raise ValueError("execution attribution source must be FILL_LEDGER")
    if not str(summary.get("attribution_csv_path", "")).strip():
        raise ValueError("execution attribution CSV path is required")
    _require_sha256(summary.get("attribution_csv_sha256"), "execution attribution CSV sha256")
    for summary_key, evidence_key in (
        ("source_path", "execution_evidence_path"),
        ("source_sha256", "execution_evidence_sha256"),
        ("source_certification_path", "execution_evidence_certification_path"),
        ("source_certification_sha256", "execution_evidence_certification_sha256"),
    ):
        actual, expected = summary.get(summary_key), evidence.get(evidence_key)
        if summary_key.endswith("path"):
            if Path(str(actual)).resolve() != Path(str(expected)).resolve():
                raise ValueError(f"execution attribution source provenance mismatch: {summary_key}")
        elif str(actual) != str(expected):
            raise ValueError(f"execution attribution source provenance mismatch: {summary_key}")


def _validate_provenance_source_hashes(provenance: dict) -> None:
    audit = provenance.get("actual_execution_audit", {})
    previous = provenance.get("previous_execution", {})
    sources = (
        ("actual execution audit", audit, "path", "sha256"),
        (
            "actual execution evidence", audit,
            "execution_evidence_path", "execution_evidence_sha256",
        ),
        (
            "actual execution evidence certification", audit,
            "execution_evidence_certification_path",
            "execution_evidence_certification_sha256",
        ),
        ("certified performance source", previous, "performance_source_path", "performance_source_sha256"),
        ("performance source certification", previous, "certification_path", "certification_sha256"),
    )
    for label, item, path_key, hash_key in sources:
        path = Path(str(item.get(path_key, "")))
        if not path.is_file():
            raise FileNotFoundError(f"{label} provenance file not found: {path}")
        if _sha256(path).lower() != str(item.get(hash_key, "")).lower():
            raise ValueError(f"{label} provenance hash mismatch")
    audit_payload = _json(audit["path"])
    audit_publication_state_valid = (
        audit_payload.get("production_ready") is True
        or (
            audit_payload.get("production_ready") is False
            and audit_payload.get("performance_basis_proven") is True
            and audit_payload.get("interval_activity_proven") is False
            and audit_payload.get("blocking_failure")
            == "PERFORMANCE_INTERVAL_ACTIVITY_NOT_PROVEN"
        )
    )
    if (
        audit_payload.get("actual_execution_proven") is not True
        or audit_payload.get("status") != "PROVEN"
        or str(audit_payload.get("proven_rebalance_date")) != str(audit.get("proven_rebalance_date"))
        or not audit_publication_state_valid
    ):
        raise ValueError("ACTUAL_REBALANCE_DATE_NOT_PROVEN")
    for field in EXECUTION_PROVENANCE_FIELDS:
        if audit_payload.get(field) != provenance.get(field):
            raise ValueError(f"report execution provenance differs from audit: {field}")
    for field in EXECUTION_EVIDENCE_FIELDS:
        actual, expected = audit_payload.get(field), audit.get(field)
        if field.endswith("path"):
            actual_path = Path(str(actual))
            if not actual_path.is_absolute():
                actual_path = REPO_ROOT / actual_path
            if actual_path.resolve() != Path(str(expected)).resolve():
                raise ValueError(f"report execution evidence differs from audit: {field}")
        elif str(actual) != str(expected):
            raise ValueError(f"report execution evidence differs from audit: {field}")
    certified = audit_payload.get("certified_performance_source")
    if not isinstance(certified, dict):
        raise ValueError("actual execution audit lacks certified_performance_source")
    exact_fields = {
        "source_type": previous.get("performance_source_type"),
        "path": previous.get("performance_source_path"),
        "sha256": previous.get("performance_source_sha256"),
        "certification_path": previous.get("certification_path"),
        "certification_sha256": previous.get("certification_sha256"),
        "opening_nav": previous.get("opening_nav"),
        "opening_cash": previous.get("opening_cash"),
    }
    for field, expected in exact_fields.items():
        actual = certified.get(field)
        if field.endswith("path"):
            if Path(str(actual)).resolve() != Path(str(expected)).resolve():
                raise ValueError(f"certified performance audit provenance mismatch: {field}")
        elif field in {"opening_nav", "opening_cash"}:
            if not _scalar_close(actual, expected):
                raise ValueError(f"certified performance audit provenance mismatch: {field}")
        elif str(actual) != str(expected):
            raise ValueError(f"certified performance audit provenance mismatch: {field}")


def _validate_benchmark_artifact_hash(
    benchmark_meta: dict,
    benchmark_path: str | Path,
    raw_master_path: str | Path | None = None,
    raw_series_path: str | Path | None = None,
) -> None:
    """Verify normalized QA and both separately sealed official-adapter responses."""

    normalized = Path(benchmark_path).resolve()
    declared_normalized = Path(
        str(benchmark_meta.get("normalized_artifact_path", ""))
    ).resolve()
    if declared_normalized != normalized:
        raise ValueError("KRX 300 normalized_artifact_path does not identify benchmark QA input")
    if not normalized.is_file() or _sha256(normalized).lower() != str(
        benchmark_meta.get("normalized_artifact_sha256", "")
    ).lower():
        raise ValueError("KRX 300 normalized artifact SHA-256 mismatch")

    raw_artifacts = benchmark_meta.get("raw_artifacts")
    if not isinstance(raw_artifacts, dict) or set(raw_artifacts) != {"index_master", "index_series"}:
        raise ValueError("KRX 300 raw_artifacts contract is incomplete")
    requested_paths = {
        "index_master": raw_master_path,
        "index_series": raw_series_path,
    }
    top_level_fields = {
        "index_master": (
            "raw_index_master_artifact_path", "raw_index_master_artifact_sha256"
        ),
        "index_series": (
            "raw_index_series_artifact_path", "raw_index_series_artifact_sha256"
        ),
    }
    for name, requested in requested_paths.items():
        item = raw_artifacts.get(name)
        if not isinstance(item, dict):
            raise ValueError(f"KRX 300 raw artifact metadata is invalid: {name}")
        declared = Path(str(item.get("path", ""))).resolve()
        path_field, sha_field = top_level_fields[name]
        if declared != Path(str(benchmark_meta.get(path_field, ""))).resolve():
            raise ValueError(f"KRX 300 raw artifact path metadata mismatch: {name}")
        if requested is not None and declared != Path(requested).resolve():
            raise ValueError(f"KRX 300 raw artifact path differs from CLI input: {name}")
        if declared == normalized:
            raise ValueError(f"KRX 300 raw artifact must be separate from normalized QA: {name}")
        declared_sha = str(item.get("sha256", "")).lower()
        if declared_sha != str(benchmark_meta.get(sha_field, "")).lower():
            raise ValueError(f"KRX 300 raw artifact hash metadata mismatch: {name}")
        if not declared.is_file() or _sha256(declared).lower() != declared_sha:
            raise ValueError(f"KRX 300 raw artifact SHA-256 mismatch: {name}")


def _validate_reconciliation_source_hashes(
    reconciliation: dict,
    *,
    summary_path: str | Path,
    daily_path: str | Path,
    contribution_path: str | Path,
) -> None:
    expected = {
        "summary": Path(summary_path),
        "daily": Path(daily_path),
        "contribution": Path(contribution_path),
    }
    sources = reconciliation.get("source_artifacts")
    if not isinstance(sources, dict) or set(sources) != set(expected):
        raise ValueError("performance reconciliation source_artifacts contract mismatch")
    for name, path in expected.items():
        item = sources.get(name)
        if not isinstance(item, dict) or Path(str(item.get("path", ""))).resolve() != path.resolve():
            raise ValueError(f"performance reconciliation source path mismatch: {name}")
        if _sha256(path).lower() != str(item.get("sha256", "")).lower():
            raise ValueError(f"performance reconciliation source hash mismatch: {name}")


def _validate_execution_attribution_artifact_hashes(
    attribution_summary: dict,
    performance_summary: dict,
    *,
    attribution_path: str | Path,
    attribution_summary_path: str | Path,
) -> None:
    csv_path = Path(attribution_path).resolve()
    json_path = Path(attribution_summary_path).resolve()
    if Path(str(attribution_summary.get("attribution_csv_path", ""))).resolve() != csv_path:
        raise ValueError("execution attribution summary CSV path mismatch")
    if str(attribution_summary.get("attribution_csv_sha256", "")).lower() != _sha256(csv_path):
        raise ValueError("execution attribution summary CSV hash mismatch")
    expected_artifacts = {
        "execution_window_attribution_csv_path": csv_path,
        "execution_window_attribution_json_path": json_path,
    }
    for key, expected_path in expected_artifacts.items():
        if Path(str(performance_summary.get(key, ""))).resolve() != expected_path:
            raise ValueError(f"performance summary execution attribution path mismatch: {key}")
    expected_hashes = {
        "execution_window_attribution_csv_sha256": _sha256(csv_path),
        "execution_window_attribution_json_sha256": _sha256(json_path),
    }
    for key, expected_hash in expected_hashes.items():
        if str(performance_summary.get(key, "")).lower() != expected_hash:
            raise ValueError(f"performance summary execution attribution hash mismatch: {key}")
    exact_summary_fields = {
        "execution_window_attribution_status": "status",
        "execution_window_type": "execution_window_type",
        "execution_window_first_fill_date": "first_fill_date",
        "execution_window_last_fill_date": "last_fill_date",
        "execution_window_trading_day_count": "execution_trading_day_count",
        "execution_window_source_path": "source_path",
        "execution_window_source_sha256": "source_sha256",
        "execution_window_source_certification_path": "source_certification_path",
        "execution_window_source_certification_sha256": "source_certification_sha256",
    }
    for performance_key, attribution_key in exact_summary_fields.items():
        left, right = performance_summary.get(performance_key), attribution_summary.get(attribution_key)
        if performance_key.endswith("path") and left is not None and right is not None:
            if Path(str(left)).resolve() != Path(str(right)).resolve():
                raise ValueError(
                    f"performance/execution attribution provenance mismatch: {performance_key}"
                )
        elif left != right:
            raise ValueError(
                f"performance/execution attribution summary mismatch: {performance_key}"
            )
    if performance_summary.get(
        "execution_window_attribution_included_in_post_rebalance_performance"
    ) is not False:
        raise ValueError("execution attribution was mixed into post-rebalance performance")


def _html_table(frame: pd.DataFrame, columns: Iterable[str], formatters: dict[str, callable] | None = None) -> str:
    columns = [column for column in columns if column in frame]
    shown = frame[columns].copy()
    formatters = formatters or {}
    for column in columns:
        formatter = formatters.get(column, _text)
        shown[column] = shown[column].map(formatter)
    return shown.to_html(index=False, escape=True, border=0, classes=["data-table"])


def _polyline(values: pd.Series, width: int, height: int, lo: float, hi: float) -> str:
    numeric = pd.to_numeric(values, errors="coerce")
    if numeric.notna().sum() < 2:
        return ""
    span = hi - lo if hi > lo else 1.0
    count = len(numeric)
    points: list[str] = []
    for idx, value in enumerate(numeric):
        if pd.isna(value):
            continue
        x = 8 + idx * (width - 16) / max(count - 1, 1)
        y = 8 + (hi - float(value)) * (height - 16) / span
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def _performance_chart(daily: pd.DataFrame) -> str:
    columns = [column for column in ["cum_return", "benchmark_cum_return"] if column in daily]
    if len(columns) != 2 or daily[columns].apply(pd.to_numeric, errors="coerce").notna().sum().min() < 2:
        return '<p class="warning">성과 차트를 재현할 수 있는 일별 자료가 부족합니다.</p>'
    values = pd.concat([pd.to_numeric(daily[column], errors="coerce") for column in columns])
    lo, hi = float(values.min()), float(values.max())
    width, height = 920, 300
    portfolio = _polyline(daily["cum_return"], width, height, lo, hi)
    benchmark = _polyline(daily["benchmark_cum_return"], width, height, lo, hi)
    return f"""
    <div class="chart-legend"><span class="portfolio-dot"></span>포트폴리오 <span class="benchmark-dot"></span>KRX 300 가격지수</div>
    <svg class="performance-chart" viewBox="0 0 {width} {height}" role="img" aria-label="포트폴리오와 KRX 300 누적수익률 비교">
      <polyline class="portfolio-line" points="{portfolio}" />
      <polyline class="benchmark-line" points="{benchmark}" />
    </svg>
    """


def _weekly_performance(daily: pd.DataFrame) -> pd.DataFrame:
    frame = daily.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    frame = frame.dropna(subset=["date"]).sort_values("date")
    frame["week"] = frame["date"].dt.to_period("W-FRI")
    weekly = frame.groupby("week", as_index=False).tail(1).copy()
    weekly["date"] = weekly["date"].dt.strftime("%Y-%m-%d")
    columns = [column for column in ["date", "cum_return", "benchmark_cum_return", "active_return"] if column in weekly]
    return weekly[columns]


def _validate_score_coverage(scores: pd.DataFrame, final: pd.DataFrame) -> None:
    """Require score lineage except for an explicit, already-held scoreless exit.

    A current holding can legitimately be absent from the target-cohort score table.  The
    exception is deliberately narrow: it must be an actual position being exited, the
    immutable model recommendation must be SELL with an explicit cohort/history reason,
    and every score/rank/selection field must remain NA.  No synthetic score row is made.
    """
    if "ticker" not in scores:
        raise ValueError("scores schema missing ticker")
    if scores["ticker"].isna().any() or scores["ticker"].duplicated().any():
        raise ValueError("scores must contain unique, non-missing security IDs")

    required_final = {
        "ticker", "model_action", "model_reason", "final_action", "position_rule",
        "current_qty", "target_qty", *SCORELESS_MODEL_FIELDS,
    }
    missing_columns = required_final - set(final)
    if missing_columns:
        raise ValueError(
            f"score coverage validation schema missing: {sorted(missing_columns)}"
        )

    missing_tickers = set(final["ticker"]) - set(scores["ticker"])
    if not missing_tickers:
        return

    rows = final.loc[final["ticker"].isin(missing_tickers)].copy()
    reasons = rows["model_reason"].astype("string").str.lower()
    reason_is_explicit = pd.Series(False, index=rows.index)
    for marker in SCORELESS_EXIT_REASON_MARKERS:
        reason_is_explicit |= reasons.str.contains(marker, regex=False, na=False)
    current_qty = pd.to_numeric(rows["current_qty"], errors="coerce")
    target_qty = pd.to_numeric(rows["target_qty"], errors="coerce")
    allowed = (
        rows["model_action"].astype("string").str.upper().eq("SELL")
        & rows["final_action"].astype("string").str.upper().eq("EXIT")
        & rows["position_rule"].astype("string").str.upper().eq("EXIT")
        & current_qty.gt(0)
        & target_qty.eq(0)
        & reason_is_explicit
        & rows[list(SCORELESS_MODEL_FIELDS)].isna().all(axis=1)
    ).fillna(False)
    invalid = rows.loc[~allowed, "ticker"].astype(str).tolist()
    if invalid:
        raise ValueError(
            "scores are missing execution-overlay securities that are not explicit "
            f"scoreless holding exits: {sorted(invalid)}"
        )


def _validate_inputs(
    model: pd.DataFrame,
    final: pd.DataFrame,
    valuation: pd.DataFrame,
    scores: pd.DataFrame,
    performance_summary: dict,
    performance_daily: pd.DataFrame,
    performance_contribution: pd.DataFrame,
    performance_reconciliation: dict,
    execution_attribution: pd.DataFrame,
    execution_attribution_summary: dict,
    benchmark_qa: pd.DataFrame,
    benchmark_meta: dict,
    provenance: dict,
    *,
    asof: str,
    target: str,
) -> None:
    required_model = {
        "ticker", "model_action", "model_reason", "model_score", "model_score_rank",
        "model_score_adj", "model_score_adj_rank", "selection_bucket", "kept_from_previous",
    }
    required_final = {
        "ticker", "final_action", "override_flag", "override_reason", "position_rule",
        "current_qty", "target_qty", "delta_qty", "trade_side", "trade_qty",
        "current_value", "target_value", "order_value", "target_weight", "realized_target_weight",
        "model_action", "model_reason", "model_score", "model_score_rank",
        "model_score_adj", "model_score_adj_rank", "selection_bucket", "kept_from_previous",
    }
    missing_model = required_model - set(model)
    missing_final = required_final - set(final)
    if missing_model or missing_final:
        raise ValueError(f"report input schema missing: model={sorted(missing_model)} final={sorted(missing_final)}")
    if model["ticker"].duplicated().any() or final["ticker"].duplicated().any():
        raise ValueError("report inputs contain duplicate security IDs")
    if set(model["ticker"]) - set(final["ticker"]):
        raise ValueError("model target portfolio contains a security absent from the final overlay universe")
    if len(model) != 10:
        raise ValueError(f"model target portfolio must contain exactly 10 securities: rows={len(model)}")
    model_fields = sorted(required_model - {"ticker"})
    comparison = model[["ticker", *model_fields]].merge(
        final[["ticker", *model_fields]], on="ticker", how="left", validate="one_to_one",
        suffixes=("__target", "__overlay"),
    )
    for column in model_fields:
        left = comparison[f"{column}__target"].astype("string").fillna("<NA>")
        right = comparison[f"{column}__overlay"].astype("string").fillna("<NA>")
        if not left.eq(right).all():
            raise ValueError(f"model recommendation mutated in execution overlay: column={column}")
    required_valuation = {"ticker", "per_ttm", "pbr", "psr_ttm", "ev_to_opincome_ttm", "valuation_status", "na_reason"}
    if required_valuation - set(valuation):
        raise ValueError(f"valuation QA schema missing: {sorted(required_valuation - set(valuation))}")
    if valuation["ticker"].duplicated().any() or set(valuation["ticker"]) != set(final["ticker"]):
        raise ValueError("valuation QA must contain the exact unique execution-overlay universe")
    _validate_score_coverage(scores, final)
    if benchmark_meta.get("asset_type") != "INDEX" or benchmark_meta.get("benchmark_name") != "KRX 300":
        raise ValueError("benchmark metadata is not the official KRX 300 INDEX contract")
    return_type = benchmark_meta.get("index_variant", benchmark_meta.get("return_type"))
    if return_type != "PRICE":
        raise ValueError("portfolio NAV excludes dividends, so report benchmark must be the PRICE index")
    _validate_provenance(provenance, performance_summary, asof=asof, target=target)
    _validate_performance_evidence(
        performance_summary,
        performance_daily,
        performance_contribution,
        benchmark_qa,
        benchmark_meta,
        asof=asof,
    )
    _validate_performance_reconciliation(
        performance_reconciliation,
        performance_summary,
        performance_contribution,
    )
    _validate_execution_attribution(
        execution_attribution,
        execution_attribution_summary,
        provenance,
        performance_summary,
    )


def build_report(
    *,
    model: pd.DataFrame,
    final: pd.DataFrame,
    valuation: pd.DataFrame,
    scores: pd.DataFrame,
    performance_summary: dict,
    performance_daily: pd.DataFrame,
    performance_contribution: pd.DataFrame,
    performance_reconciliation: dict,
    execution_attribution: pd.DataFrame,
    execution_attribution_summary: dict,
    benchmark_qa: pd.DataFrame,
    benchmark_meta: dict,
    industry_audit: dict,
    provenance: dict,
    asof: str,
    target: str,
) -> str:
    model = model.copy()
    final = final.copy()
    valuation = valuation.copy()
    scores = scores.copy()
    performance_daily = performance_daily.copy()
    performance_contribution = performance_contribution.copy()
    execution_attribution = execution_attribution.copy()
    benchmark_qa = benchmark_qa.copy()
    for frame in (
        model, final, valuation, scores, performance_contribution, execution_attribution,
    ):
        if "ticker" in frame:
            frame["ticker"] = normalize_security_id_series(frame["ticker"])
    _validate_inputs(
        model,
        final,
        valuation,
        scores,
        performance_summary,
        performance_daily,
        performance_contribution,
        performance_reconciliation,
        execution_attribution,
        execution_attribution_summary,
        benchmark_qa,
        benchmark_meta,
        provenance,
        asof=asof,
        target=target,
    )
    conditional_interval = _is_conditional_interval_performance(performance_summary)
    publication_notice = (
        '<p class="notice"><strong>비운영 조건부 성과:</strong> 2026-04-01 체결일과 '
        '당일 시작 NAV는 입증됐지만, 이후 ASOF까지의 외부 입출금 및 중간 매매 부재는 '
        '입증되지 않았습니다. 아래 성과는 고정 보유·signed settlement liability를 둔 '
        '조건부 shadow 계산이며 production PASS가 아닙니다. '
        '<code>PERFORMANCE_INTERVAL_ACTIVITY_NOT_PROVEN</code></p>'
        if conditional_interval else ""
    )
    recommendations = final[[
        column for column in [
            "ticker", "name", "model_action", "model_reason", "model_score", "model_score_rank",
            "model_score_adj", "model_score_adj_rank", "selection_bucket", "kept_from_previous",
        ] if column in final
    ]].copy()
    merged = final.copy()
    scores_small = scores.copy()
    factor_columns = [
        column for column in scores_small
        if column.endswith("__contrib") and not column.endswith("__contrib_base")
    ]
    score_keep = [column for column in ["ticker", "name", *factor_columns] if column in scores_small]
    scores_small = scores_small[score_keep].drop_duplicates("ticker", keep="last")
    merged = merged.merge(scores_small, on="ticker", how="left", validate="one_to_one", suffixes=("", "__score"))
    merged = merged.merge(valuation, on="ticker", how="left", validate="one_to_one", suffixes=("", "__valuation"))

    model_counts = recommendations["model_action"].value_counts().to_dict()
    positive = pd.to_numeric(final["target_qty"], errors="coerce").fillna(0).gt(0)
    final_count = int(positive.sum())
    nav = _number(final["nav"].iloc[0]) if "nav" in final and len(final) else None
    final_cash = _number(final["final_cash"].iloc[0]) if "final_cash" in final and len(final) else None
    final_cash_weight = _number(final["final_cash_weight"].iloc[0]) if "final_cash_weight" in final and len(final) else None
    total_cost = _number(final["estimated_total_cost"].iloc[0]) if "estimated_total_cost" in final and len(final) else None

    summary_rows = pd.DataFrame([
        {"metric": "시작일", "value": performance_summary.get("start_date")},
        {"metric": "종료일", "value": performance_summary.get("end_date")},
        {"metric": "초기 NAV", "value": _krw(performance_summary.get("initial_nav"))},
        {"metric": "현재 NAV", "value": _krw(performance_summary.get("last_nav"))},
        {"metric": "포트폴리오 누적수익률", "value": _pct(performance_summary.get("cum_return"))},
        {"metric": "MDD", "value": _pct(performance_summary.get("max_drawdown"))},
        {"metric": "MDD 기준 고점일", "value": performance_summary.get("drawdown_peak_date")},
        {"metric": "MDD 발생일", "value": performance_summary.get("max_drawdown_date")},
        {
            "metric": (
                "시작 서명 결제잔액(조건부 shadow liability)"
                if conditional_interval else "시작 현금"
            ),
            "value": _krw(performance_summary.get("start_cash")),
        },
        {
            "metric": (
                "종료 서명 결제잔액(조건부 shadow liability)"
                if conditional_interval else "종료 현금"
            ),
            "value": _krw(performance_summary.get("end_cash")),
        },
        {"metric": "총 거래비용", "value": _krw(performance_summary.get("total_trading_costs"))},
        {"metric": "배당 처리", "value": performance_summary.get("nav_dividend_treatment")},
        {"metric": "반영 배당", "value": _krw(performance_summary.get("dividends"))},
        {"metric": "외부 현금흐름 처리", "value": performance_summary.get("external_cash_flow_treatment")},
        {"metric": "외부 현금흐름", "value": _krw(performance_summary.get("external_cash_flows"))},
    ])
    execution_attribution_rows = pd.DataFrame([
        {"metric": "상태", "value": execution_attribution_summary.get("status")},
        {"metric": "실행구간 유형", "value": execution_attribution_summary.get("execution_window_type")},
        {"metric": "최초 체결일", "value": execution_attribution_summary.get("first_fill_date")},
        {"metric": "최종 체결일", "value": execution_attribution_summary.get("last_fill_date")},
        {"metric": "체결 거래일 수", "value": execution_attribution_summary.get("execution_trading_day_count")},
        {"metric": "체결 건수", "value": execution_attribution_summary.get("fill_count")},
        {"metric": "총 체결비용", "value": _krw(execution_attribution_summary.get("trading_cost"))},
        {
            "metric": "결제 현금효과(SELL-BUY-비용)",
            "value": _krw(execution_attribution_summary.get("settlement_cash_effect")),
        },
        {
            "metric": "최종 서명 결제잔액",
            "value": _krw(
                execution_attribution_summary.get("final_signed_settlement_balance")
            ),
        },
        {
            "metric": "서명 결제부채",
            "value": _krw(execution_attribution_summary.get("signed_settlement_liability")),
        },
        {
            "metric": "최종 체결일 종가 기준 총손익",
            "value": _krw(execution_attribution_summary.get("gross_mark_to_last_fill_close_pnl")),
        },
        {
            "metric": "최종 체결일 종가 기준 순손익",
            "value": _krw(execution_attribution_summary.get("net_mark_to_last_fill_close_pnl")),
        },
        {"metric": "귀속 기준", "value": execution_attribution_summary.get("attribution_basis")},
        {
            "metric": "사후 포트폴리오 성과 포함 여부",
            "value": execution_attribution_summary.get("included_in_post_rebalance_performance"),
        },
    ])
    benchmark_rows = pd.DataFrame([
        {"metric": "벤치마크", "value": benchmark_meta.get("benchmark_name")},
        {"metric": "자산유형", "value": benchmark_meta.get("asset_type")},
        {"metric": "지수 유형", "value": benchmark_meta.get("index_variant", benchmark_meta.get("return_type"))},
        {"metric": "공식 식별자", "value": benchmark_meta.get("benchmark_identifier")},
        {"metric": "출처", "value": benchmark_meta.get("source")},
        {"metric": "실제 성과 시작일", "value": performance_summary.get("start_date")},
        {"metric": "지수 시작일", "value": benchmark_meta.get("first_date")},
        {"metric": "지수 시작값", "value": _multiple(benchmark_qa["index_level"].iloc[0])},
        {"metric": "지수 종료일", "value": benchmark_meta.get("last_date")},
        {"metric": "지수 종료값", "value": _multiple(benchmark_qa["index_level"].iloc[-1])},
        {"metric": "정확 정렬 행 수", "value": len(benchmark_qa)},
        {"metric": "source endpoint", "value": benchmark_meta.get("source_endpoint")},
        {"metric": "source endpoint BLD", "value": benchmark_meta.get("source_endpoint_bld")},
        {"metric": "수집 시각", "value": benchmark_meta.get("collection_timestamp")},
        {"metric": "정규화 QA SHA-256", "value": benchmark_meta.get("normalized_artifact_sha256")},
        {"metric": "원본 index master SHA-256", "value": benchmark_meta.get("raw_index_master_artifact_sha256")},
        {"metric": "원본 index series SHA-256", "value": benchmark_meta.get("raw_index_series_artifact_sha256")},
        {"metric": "fill 사용", "value": benchmark_meta.get("fill_used")},
        {"metric": "벤치마크 누적수익률", "value": _pct(performance_summary.get("benchmark_cum_return"))},
        {"metric": "초과수익률", "value": _pct(performance_summary.get("active_return"))},
    ])
    benchmark_series_rows = pd.DataFrame([
        {
            "date": str(pd.to_datetime(benchmark_qa["date"].iloc[0]).date()),
            "index_level": benchmark_qa["index_level"].iloc[0],
            "benchmark_cum_return": 0.0,
        },
        {
            "date": str(pd.to_datetime(benchmark_qa["date"].iloc[-1]).date()),
            "index_level": benchmark_qa["index_level"].iloc[-1],
            "benchmark_cum_return": performance_daily["benchmark_cum_return"].iloc[-1],
        },
    ])
    model_summary = pd.DataFrame([
        {"model_action": action, "count": int(model_counts.get(action, 0))}
        for action in ["BUY", "HOLD", "SELL"]
    ])
    final_summary = pd.DataFrame([
        {"position_rule": "KEEP_QTY", "count": int(final["position_rule"].eq("KEEP_QTY").sum())},
        {"position_rule": "TARGET_WEIGHT / ADD", "count": int(final["position_rule"].eq("TARGET_WEIGHT").sum())},
        {"position_rule": "EXIT", "count": int(final["position_rule"].eq("EXIT").sum())},
        {"position_rule": "RESIDUAL_EQUAL_WEIGHT_BUY", "count": int(final["position_rule"].eq("RESIDUAL_EQUAL_WEIGHT_BUY").sum())},
        {"position_rule": "최종 주식 종목 수", "count": final_count},
        {"position_rule": "최소 현금비중", "count": "10%"},
    ])
    difference = merged.loc[merged["override_flag"].astype(str).str.lower().isin(["true", "1", "yes"])].copy()
    trade = merged.loc[merged["trade_side"].ne("NONE") & pd.to_numeric(merged["trade_qty"], errors="coerce").gt(0)].copy()
    weekly = _weekly_performance(performance_daily)
    contribution = performance_contribution.sort_values(
        "contribution_to_total_return", ascending=False
    ).copy()

    actual_audit = provenance["actual_execution_audit"]
    previous_execution = provenance["previous_execution"]
    chronology = provenance.get("actual_execution_chronology", {})
    provenance_rows = pd.DataFrame([
        {"field": "correction_run_id", "value": provenance["correction_run_id"]},
        {"field": "source_run_id", "value": provenance["source_run_id"]},
        {"field": "report_contract", "value": provenance["report_contract"]},
        {"field": "created_at_utc", "value": provenance["created_at_utc"]},
        {"field": "actual_execution_proven", "value": provenance["actual_execution_proven"]},
        {"field": "production_ready", "value": provenance.get("production_ready")},
        {"field": "blocking_failure", "value": provenance.get("blocking_failure")},
        {
            "field": "performance_interval_activity_proven",
            "value": provenance.get("performance_interval_activity_proven"),
        },
        {
            "field": "performance_calculation_method",
            "value": performance_summary.get("performance_calculation_method"),
        },
        {"field": "proven_rebalance_date", "value": actual_audit["proven_rebalance_date"]},
        {"field": "actual_execution_audit_sha256", "value": actual_audit["sha256"]},
        {"field": "execution_evidence_source_type", "value": actual_audit["execution_evidence_source_type"]},
        {"field": "execution_evidence_path", "value": actual_audit["execution_evidence_path"]},
        {"field": "execution_evidence_sha256", "value": actual_audit["execution_evidence_sha256"]},
        {
            "field": "execution_evidence_certification_path",
            "value": actual_audit["execution_evidence_certification_path"],
        },
        {
            "field": "execution_evidence_certification_sha256",
            "value": actual_audit["execution_evidence_certification_sha256"],
        },
        {"field": "pipeline_asof", "value": provenance.get("pipeline_asof")},
        {"field": "model_pipeline_asof", "value": provenance.get("model_pipeline_asof")},
        {"field": "model_target_date", "value": provenance.get("model_target_date")},
        {"field": "prior_report_created_at", "value": provenance.get("report_created_at")},
        {"field": "first_fill_date", "value": provenance.get("first_fill_date")},
        {"field": "last_fill_date", "value": provenance.get("last_fill_date")},
        {"field": "post_trade_snapshot_date", "value": provenance.get("post_trade_snapshot_date")},
        {"field": "performance_start_date", "value": provenance.get("performance_start_date")},
        {"field": "performance_start_basis", "value": provenance.get("performance_start_basis")},
        {"field": "performance_start_source_path", "value": provenance.get("performance_start_source_path")},
        {"field": "performance_start_source_sha256", "value": provenance.get("performance_start_source_sha256")},
        {"field": "performance_source_type", "value": previous_execution["performance_source_type"]},
        {"field": "performance_source_sha256", "value": previous_execution["performance_source_sha256"]},
        {"field": "performance_certification_sha256", "value": previous_execution["certification_sha256"]},
        {"field": "opening_nav_basis", "value": provenance.get("opening_nav_basis")},
        {"field": "certified_opening_nav", "value": _krw(previous_execution["opening_nav"])},
        {"field": "opening_cash_basis", "value": chronology.get("opening_cash_basis", "CERTIFIED_CASH_BALANCE")},
        {
            "field": _opening_balance_provenance_label(
                performance_summary,
                previous_execution["opening_cash"],
            ),
            "value": _krw(previous_execution["opening_cash"]),
        },
        {"field": "financial_data_cutoff_date", "value": provenance.get("financial_data_cutoff_date")},
        {"field": "chronology_mode", "value": chronology.get("mode")},
        {"field": "user_confirmed_execution_date", "value": chronology.get("user_confirmed_execution_date")},
        {
            "field": "prior_q1_after_filing_interpretation",
            "value": chronology.get("prior_interpretation_status"),
        },
        {"field": "unknown_date_policy", "value": chronology.get("unknown_date_policy")},
        {"field": "chronology_override_disclosure", "value": provenance.get("chronology_override_disclosure")},
        {"field": "actual_orders_submitted", "value": provenance["actual_orders_submitted"]},
    ])

    valuation_formatters = {
        "mcap_asof": _krw,
        "revenue_ttm": _krw,
        "op_income_ttm": _krw,
        "parent_net_income_ttm": _krw,
        "parent_equity_latest": _krw,
        "per_ttm": _multiple,
        "pbr": _multiple,
        "psr_ttm": _multiple,
        "ev_to_opincome_ttm": _multiple,
    }
    execution_formatters = {
        "current_qty": _qty,
        "target_qty": _qty,
        "delta_qty": _qty,
        "trade_qty": _qty,
        "current_value": _krw,
        "target_value": _krw,
        "order_value": _krw,
        "target_weight": _pct,
        "realized_target_weight": _pct,
    }
    weekly_formatters = {"cum_return": _pct, "benchmark_cum_return": _pct, "active_return": _pct}
    contribution_formatters = {
        "shares_at_start": _qty,
        "start_price": _krw,
        "start_position_value": _krw,
        "end_price": _krw,
        "end_position_value": _krw,
        "dividends": _krw,
        "net_intermediate_trade_cashflow": _krw,
        "trading_cost_allocated": _krw,
        "period_pnl": _krw,
        "position_period_return": _pct,
        "contribution_to_total_return": _pct,
        "weight_at_start_nav": _pct,
        "weight_at_end_nav": _pct,
    }
    execution_attribution_formatters = {
        "buy_qty": _qty,
        "sell_qty": _qty,
        "gross_buy_value": _krw,
        "gross_sell_value": _krw,
        "net_execution_cashflow": _krw,
        "trading_cost": _krw,
        "gross_mark_to_last_fill_close_pnl": _krw,
        "net_mark_to_last_fill_close_pnl": _krw,
    }

    qa_rows = pd.DataFrame([
        {"check": "official_index_asset_type", "result": benchmark_meta.get("asset_type") == "INDEX"},
        {"check": "official_krx300_exact_name", "result": benchmark_meta.get("benchmark_name") == "KRX 300"},
        {"check": "price_index_matches_no_dividend_nav", "result": benchmark_meta.get("index_variant", benchmark_meta.get("return_type")) == "PRICE"},
        {"check": "benchmark_no_fallback", "result": not bool(benchmark_meta.get("fallback_source_path"))},
        {"check": "actual_execution_date_proven", "result": provenance.get("actual_execution_proven") is True},
        {"check": "performance_dates_exact_krx300", "result": True},
        {"check": "performance_contribution_reconciled", "result": True},
        {"check": "performance_reconciliation_artifact", "result": performance_reconciliation.get("status") == "PASS"},
        {
            "check": "execution_window_attribution_separate",
            "result": execution_attribution_summary.get("status") in {
                "PASS", "NOT_AVAILABLE_NO_CERTIFIED_FILL_LEDGER",
                "NOT_APPLICABLE_MARK_TO_CLOSE_CASH_EQ_UNPRICED",
            }
            and execution_attribution_summary.get("included_in_post_rebalance_performance") is False,
        },
        {"check": "input_hashes_sealed", "result": True},
        {"check": "industry_lineage", "result": industry_audit.get("status") == "PASS"},
        {"check": "final_positions", "result": final_count == 10},
        {"check": "cash_floor", "result": final_cash_weight is not None and final_cash_weight >= 0.10},
        {"check": "nonnegative_cash", "result": final_cash is not None and final_cash >= 0},
        {"check": "valuation_rows_reproducible", "result": not valuation["valuation_status"].eq("ERROR").any()},
    ])

    machine_payload = {
        "schema_version": REPORT_MACHINE_SCHEMA_VERSION,
        "report_contract": REPORT_CONTRACT,
        "asof": asof,
        "target": target,
        "provenance": provenance,
        "benchmark": benchmark_meta,
        "benchmark_metadata": benchmark_meta,
        "benchmark_rows": _records(benchmark_qa),
        "performance": performance_summary,
        "performance_summary": performance_summary,
        "performance_daily_rows": _records(performance_daily),
        "performance_contribution_rows": _records(performance_contribution),
        "performance_reconciliation": performance_reconciliation,
        "execution_attribution_rows": _records(execution_attribution),
        "execution_attribution_columns": list(execution_attribution.columns),
        "execution_attribution_summary": execution_attribution_summary,
        "model_action_counts": {key: int(value) for key, value in model_counts.items()},
        "final": {
            "nav": nav,
            "cash": final_cash,
            "cash_weight": final_cash_weight,
            "estimated_cost": total_cost,
            "position_count": final_count,
        },
        "model_target_rows": _records(model),
        "model_rows": _records(recommendations),
        "final_rows": _records(final),
        "factor_contribution_rows": _records(scores_small),
        "valuation_rows": _records(valuation),
        "industry_audit": industry_audit,
    }
    machine_json = json.dumps(machine_payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    model_columns = [
        "ticker", "name", "model_action", "model_reason", "model_score", "model_score_rank",
        "model_score_adj", "model_score_adj_rank", "selection_bucket", "kept_from_previous",
    ]
    final_columns = [
        "ticker", "name", "final_action", "position_rule", "current_qty", "target_qty", "delta_qty",
        "trade_side", "trade_qty", "target_value", "target_weight", "realized_target_weight",
        "override_flag", "override_reason",
    ]
    factor_valuation_columns = [
        "ticker", "name", *factor_columns, "valuation_asof", "financial_period", "statement_scope",
        "mcap_asof", "revenue_ttm", "op_income_ttm", "parent_net_income_ttm", "parent_equity_latest",
        "per_ttm", "pbr", "psr_ttm", "ev_to_opincome_ttm", "valuation_status", "na_reason",
    ]

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Investment Adviser 리밸런싱 보고서 — {html.escape(asof)}</title>
<style>
:root{{--ink:#172033;--muted:#667085;--line:#dfe3ea;--panel:#f8fafc;--blue:#2457d6;--orange:#e67e22;--good:#087443;--warn:#9a6700}}
body{{font-family:"Noto Sans KR","Malgun Gothic",sans-serif;color:var(--ink);max-width:1180px;margin:0 auto;padding:28px;line-height:1.55}}
h1{{font-size:28px;margin-bottom:4px}} h2{{margin-top:40px;border-bottom:2px solid var(--ink);padding-bottom:8px}}
.subtitle,.note{{color:var(--muted)}} .notice{{background:#fff8e6;border-left:4px solid var(--warn);padding:12px 16px}}
.data-table{{width:100%;border-collapse:collapse;font-size:13px;margin:12px 0 24px}} .data-table th,.data-table td{{border:1px solid var(--line);padding:7px 8px;text-align:right}}
.data-table th:first-child,.data-table td:first-child,.data-table th:nth-child(2),.data-table td:nth-child(2){{text-align:left}}
.data-table th{{background:var(--panel);position:sticky;top:0}} .performance-chart{{width:100%;height:auto;background:#fff;border:1px solid var(--line)}}
.performance-chart polyline{{fill:none;stroke-width:3}} .portfolio-line{{stroke:var(--blue)}} .benchmark-line{{stroke:var(--orange)}}
.chart-legend{{font-size:13px;margin:8px 0}} .portfolio-dot,.benchmark-dot{{display:inline-block;width:10px;height:10px;border-radius:50%;margin:0 5px 0 14px}}
.portfolio-dot{{background:var(--blue)}} .benchmark-dot{{background:var(--orange)}} .qa-pass{{color:var(--good);font-weight:700}}
code{{background:var(--panel);padding:2px 4px}} .scroll{{overflow-x:auto}}
</style></head><body>
<h1>AI Investment Adviser 리밸런싱 보고서</h1>
<p class="subtitle">ASOF {html.escape(asof)} · TARGET {html.escape(target)} · 모델 권고와 사용자 집행 오버레이 분리본</p>
{publication_notice}
<p class="notice">모델 팩터·점수·필터·랭킹은 변경하지 않았습니다. 아래 사용자 최종 집행안은 별도 execution overlay이며 실제 주문은 제출하지 않습니다.</p>

<h2>{REPORT_SECTIONS[0]}</h2>
<p>성과 시작일은 파일명이나 설정값으로 추정하지 않고, 실제 집행일 감사에서 입증된 날짜만 사용합니다. 입력 원본은 SHA-256으로 봉인되어 있습니다.</p>
<p class="notice">{html.escape(str(provenance.get('chronology_override_disclosure', '')))}</p>
{_html_table(provenance_rows, ['field','value'])}
<h3>실행구간 손익·비용 (사후 포트폴리오 성과와 분리)</h3>
<p>인증된 체결원장이 있을 때만 최초~최종 체결일 구간을 마지막 체결일 공식 종가로 평가합니다. 이 손익은 아래 사후 포트폴리오 수익률에 포함하지 않습니다.</p>
{_html_table(execution_attribution_rows, ['metric','value'])}
<div class="scroll">{_html_table(execution_attribution, EXECUTION_ATTRIBUTION_COLUMNS, execution_attribution_formatters)}</div>

<h2>{REPORT_SECTIONS[1]}</h2>
{_html_table(summary_rows, ['metric','value'])}

<h2>{REPORT_SECTIONS[2]}</h2>
{_html_table(benchmark_rows, ['metric','value'])}
<h3>공식 지수 시작·종료값</h3>
{_html_table(benchmark_series_rows, ['date','index_level','benchmark_cum_return'], {'index_level':_multiple,'benchmark_cum_return':_pct})}
{_performance_chart(performance_daily)}
<h3>주간 스냅샷</h3><div class="scroll">{_html_table(weekly, weekly.columns, weekly_formatters)}</div>

<h2>{REPORT_SECTIONS[3]}</h2>
<p>각 기여도는 종목 손익을 성과 시작 NAV로 나눈 값이며, 합계는 포트폴리오 누적수익률과 일치합니다.</p>
<div class="scroll">{_html_table(contribution, ['ticker','name','shares_at_start','start_date','start_price','start_position_value','end_date','end_price','end_position_value','dividends','net_intermediate_trade_cashflow','trading_cost_allocated','period_pnl','position_period_return','contribution_to_total_return','weight_at_start_nav','weight_at_end_nav','performance_basis'], contribution_formatters)}</div>

<h2>{REPORT_SECTIONS[4]}</h2>
<p>원본 모델 결과: BUY {model_counts.get('BUY',0)}, HOLD {model_counts.get('HOLD',0)}, SELL {model_counts.get('SELL',0)}</p>
{_html_table(model_summary, ['model_action','count'])}
<div class="scroll">{_html_table(recommendations, model_columns)}</div>

<h2>{REPORT_SECTIONS[5]}</h2>
{_html_table(final_summary, ['position_rule','count'])}
<p>NAV {_krw(nav)} · 예상 거래비용 {_krw(total_cost)} · 최종 현금 {_krw(final_cash)} ({_pct(final_cash_weight)})</p>
<div class="scroll">{_html_table(merged, final_columns, execution_formatters)}</div>

<h2>{REPORT_SECTIONS[6]}</h2>
<div class="scroll">{_html_table(difference, ['ticker','name','model_action','final_action','position_rule','override_reason'])}</div>

<h2>{REPORT_SECTIONS[7]}</h2>
<p><code>target_qty</code>는 목표 보유수량, <code>trade_qty</code>는 실제 주문수량입니다. 두 값은 서로 대체되지 않습니다.</p>
<div class="scroll">{_html_table(trade, ['ticker','name','trade_side','trade_qty','current_qty','target_qty','order_value','estimated_total_cost_row'], execution_formatters | {'estimated_total_cost_row':_krw})}</div>

<h2>{REPORT_SECTIONS[8]}</h2>
<p>밸류에이션은 공식 ASOF 시가총액과 동일 scope의 내부 DART 원자료만 사용합니다. 외부 provider PER/PBR은 표시하지 않습니다.</p>
<div class="scroll">{_html_table(merged, factor_valuation_columns, valuation_formatters)}</div>

<h2>{REPORT_SECTIONS[9]}</h2>
{_html_table(qa_rows, ['check','result'])}
<p>EV 구성요소가 하나라도 신뢰성 있게 매핑되지 않으면 <code>EV/영업이익(TTM)</code>은 NA입니다. 총순이익·총자본·외부 배수를 대체값으로 사용하지 않습니다.</p>

<script type="application/json" id="report-machine-data">{machine_json}</script>
</body></html>"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-portfolio", required=True)
    parser.add_argument("--final-execution", required=True)
    parser.add_argument("--valuation-qa", required=True)
    parser.add_argument("--scores", required=True)
    parser.add_argument("--performance-summary", required=True)
    parser.add_argument("--performance-daily", required=True)
    parser.add_argument("--performance-contribution", required=True)
    parser.add_argument("--performance-reconciliation", required=True)
    parser.add_argument("--execution-attribution", required=True)
    parser.add_argument("--execution-attribution-summary", required=True)
    parser.add_argument("--benchmark-qa", required=True)
    parser.add_argument("--benchmark-meta", required=True)
    parser.add_argument("--benchmark-raw-index-master", required=True)
    parser.add_argument("--benchmark-raw-index-series", required=True)
    parser.add_argument("--industry-audit", required=True)
    parser.add_argument("--report-provenance", required=True)
    parser.add_argument("--asof", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    input_paths = {
        "model_portfolio": Path(args.model_portfolio),
        "final_execution": Path(args.final_execution),
        "valuation_qa": Path(args.valuation_qa),
        "scores": Path(args.scores),
        "performance_summary": Path(args.performance_summary),
        "performance_daily": Path(args.performance_daily),
        "performance_contribution": Path(args.performance_contribution),
        "performance_reconciliation": Path(args.performance_reconciliation),
        "execution_attribution": Path(args.execution_attribution),
        "execution_attribution_summary": Path(args.execution_attribution_summary),
        "benchmark_qa": Path(args.benchmark_qa),
        "benchmark_meta": Path(args.benchmark_meta),
        "benchmark_raw_index_master": Path(args.benchmark_raw_index_master),
        "benchmark_raw_index_series": Path(args.benchmark_raw_index_series),
        "industry_audit": Path(args.industry_audit),
    }
    provenance = _json(args.report_provenance)
    _validate_cli_input_hashes(provenance, input_paths)
    _validate_provenance_source_hashes(provenance)
    benchmark_meta = _json(args.benchmark_meta)
    _validate_benchmark_artifact_hash(
        benchmark_meta,
        args.benchmark_qa,
        args.benchmark_raw_index_master,
        args.benchmark_raw_index_series,
    )
    performance_reconciliation = _json(args.performance_reconciliation)
    _validate_reconciliation_source_hashes(
        performance_reconciliation,
        summary_path=args.performance_summary,
        daily_path=args.performance_daily,
        contribution_path=args.performance_contribution,
    )
    execution_attribution_summary = _json(args.execution_attribution_summary)
    performance_summary = _json(args.performance_summary)
    _validate_execution_attribution_artifact_hashes(
        execution_attribution_summary,
        performance_summary,
        attribution_path=args.execution_attribution,
        attribution_summary_path=args.execution_attribution_summary,
    )

    report = build_report(
        model=_read_csv(args.model_portfolio),
        final=_read_csv(args.final_execution),
        valuation=_read_csv(args.valuation_qa),
        scores=_read_csv(args.scores),
        performance_summary=performance_summary,
        performance_daily=_read_csv(args.performance_daily),
        performance_contribution=_read_csv(args.performance_contribution),
        performance_reconciliation=performance_reconciliation,
        execution_attribution=_read_csv(args.execution_attribution),
        execution_attribution_summary=execution_attribution_summary,
        benchmark_qa=_read_csv(args.benchmark_qa),
        benchmark_meta=benchmark_meta,
        industry_audit=_json(args.industry_audit),
        provenance=provenance,
        asof=args.asof,
        target=args.target,
    )
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(f"refusing to overwrite immutable corrected report: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + ".tmp")
    if temporary.exists():
        raise FileExistsError(f"stale temporary report already exists: {temporary}")
    temporary.write_text(report, encoding="utf-8")
    temporary.replace(output)
    print(f"[OK] corrected report: {output}")


if __name__ == "__main__":
    main()
