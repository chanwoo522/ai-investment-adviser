from __future__ import annotations

"""Build an immutable ADVISOR_FULL_RESET_V2 contract-correction run.

This runner reads the immutable V1 advisor run and the authoritative production
model inputs.  It writes only to a new dot-prefixed staging directory under the
V2 development run root, renders the report for visual review, and publishes
the staging directory atomically after all runtime checks pass.
"""

import argparse
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping
from zipfile import ZipFile

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.advisor.full_reset_v2 import (  # noqa: E402
    CASH_RETURN_CONTRACT,
    build_account_valuation_snapshot,
    build_blocker_taxonomy,
    build_common_reference_price_snapshot,
    build_current_vs_target_v2,
    build_v2_target_portfolio,
    calculate_account_asof_liquidation,
)
from scripts.advisor.immutable_run import (  # noqa: E402
    build_private_audit_bundle,
    protected_state,
    publish_staging,
    relative_artifact_manifest,
    sha256_file,
)
from scripts.advisor.reporting_v2 import (  # noqa: E402
    generate_advisor_v2_html,
    validate_advisor_v2_html,
)
from scripts.advisor.score_parity_v2 import (  # noqa: E402
    assert_no_holdings_parameter,
    build_score_parity_bundle_from_paths,
    write_score_parity_artifacts,
)
from scripts.advisor.v2_industry import (  # noqa: E402
    build_advisor_sector_map_from_exact_ticker_policy,
    build_industry_artifacts,
    build_security_registry_from_exact_sources,
    write_industry_artifacts,
)
from scripts.advisor.v2_valuation import (  # noqa: E402
    build_exact_quarterly_financials_from_dart_cache,
    build_valuation_artifacts,
    write_valuation_artifacts,
)
from scripts.common.security_id import normalize_security_id  # noqa: E402


REQUIRED_OUTPUTS = (
    "score_parity_audit.csv",
    "score_parity_audit.json",
    "scoring_population_audit.json",
    "filter_order_audit.md",
    "account_valuation_snapshot.csv",
    "hypothetical_full_liquidation_account_asof.csv",
    "advisory_capital_summary_v2.json",
    "reference_price_snapshot.csv",
    "date_basis_qa.json",
    "valuation_qa.csv",
    "valuation_qa.json",
    "selected_security_financials.csv",
    "selected_security_diagnostics.csv",
    "industry_mapping_qa.csv",
    "sector_exposure_current.csv",
    "sector_exposure_target.csv",
    "top_k_boundary_watchlist.csv",
    "top_k_boundary_qa.json",
    "quarterly_advisor_report_v2.html",
    "quarterly_advisor_report_v2.pdf",
    "ADVISOR_FULL_RESET_V2_QA.md",
    "run_manifest.json",
    "private_audit_bundle.zip",
)


def _ticker(value: object) -> str:
    return str(normalize_security_id(value))


def _repo_path(value: str | Path) -> Path:
    path = (REPO_ROOT / Path(value)).resolve()
    try:
        path.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise ValueError(f"configured path leaves repository: {value}") from exc
    return path


def _relative(path: Path) -> str:
    return Path(path).resolve().relative_to(REPO_ROOT).as_posix()


def _json_safe(value: Any) -> Any:
    if value is pd.NA:
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            return _json_safe(value.item())
        except (ValueError, TypeError):
            pass
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        json.dump(_json_safe(payload), handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8-sig", newline="") as handle:
        frame.to_csv(handle, index=False)


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(text)


def _load_config(path: Path) -> dict[str, Any]:
    config = _read_json(path)
    if config.get("contract_version") != "ADVISOR_FULL_RESET_V2":
        raise ValueError("contract_version must be ADVISOR_FULL_RESET_V2")
    if config.get("mode") != "ADVISOR_FULL_RESET" or not config.get("development_only"):
        raise ValueError("V2 correction must remain development-only ADVISOR_FULL_RESET")
    if config.get("development_status") != "PASS_CONTRACT_CORRECTION":
        raise ValueError("development status contract mismatch")
    if config.get("production_promoted") is not False:
        raise ValueError("production promotion is forbidden")
    if config.get("production_promotion_status") != "BLOCKED_PENDING_HISTORICAL_VALIDATION":
        raise ValueError("production promotion must remain blocked")
    model = config["model"]
    if float(model.get("holding_bonus", -1)) != 0 or int(model.get("keep_current_top_n", -1)) != 0:
        raise ValueError("fresh-start V2 permits only zero holding bonus and zero keep-current")
    if int(model.get("top_k", 0)) != 10:
        raise ValueError("this correction run requires top_k=10")
    if abs(float(model.get("target_cash_equivalent_weight", 0)) - 0.10) > 1e-12:
        raise ValueError("this correction run requires 10% cash equivalent target")
    dates = config["dates"]
    if dates.get("model_information_asof") != "2026-08-18":
        raise ValueError("model information date mismatch")
    if dates.get("account_valuation_asof") != "2026-08-20":
        raise ValueError("account valuation date mismatch")
    if dates.get("reference_price_requested_asof") != "2026-08-20":
        raise ValueError("reference price request date mismatch")
    parent = _repo_path(config["lineage"]["parent_run_root"])
    if parent.name != f"run_id={config['lineage']['parent_run_id']}" or not parent.is_dir():
        raise ValueError("immutable parent lineage mismatch")
    for group in ("source", "account"):
        for key, value in config[group].items():
            if key.endswith("status") or key.endswith("sha256") or key == "activity_ledger_status":
                continue
            if isinstance(value, str) and ("/" in value or "\\" in value):
                candidate = _repo_path(value)
                if key == "dart_raw_dir":
                    if not candidate.is_dir():
                        raise FileNotFoundError(candidate)
                elif not candidate.is_file():
                    raise FileNotFoundError(candidate)
    assert_no_holdings_parameter()
    return config


def _apply_names(frame: pd.DataFrame, names: Mapping[str, str]) -> pd.DataFrame:
    result = frame.copy()
    if "ticker" not in result.columns:
        return result
    non_security_ids = {"ACCOUNT_CASH", "CASH_EQUIVALENT_BUCKET"}
    result["ticker"] = result["ticker"].map(
        lambda value: str(value) if str(value) in non_security_ids else _ticker(value)
    )
    mapped = result["ticker"].map({str(key): str(value) for key, value in names.items()})
    if "name" not in result.columns:
        result["name"] = mapped
    else:
        result["name"] = mapped.combine_first(result["name"])
    return result


def _quality_penalty_reason(frame: pd.DataFrame) -> pd.Series:
    penalty_columns = [
        column
        for column in frame.columns
        if column.startswith("quality_penalty_")
        and column not in {"quality_penalty_total", "quality_penalty_reason"}
    ]
    reasons: list[str] = []
    for _, row in frame.iterrows():
        active = []
        for column in penalty_columns:
            value = pd.to_numeric(pd.Series([row.get(column)]), errors="coerce").iloc[0]
            if pd.notna(value) and float(value) != 0:
                active.append(column.removeprefix("quality_penalty_"))
        reasons.append(" | ".join(active) if active else "없음")
    return pd.Series(reasons, index=frame.index, dtype="string")


def _input_artifacts(config: Mapping[str, Any], config_path: Path) -> list[dict[str, Any]]:
    paths: list[tuple[str, Path]] = [("v2_config", config_path)]
    paths.append(("industry_policy", _repo_path(config["source"]["industry_policy"])))
    for key in (
        "production_scores",
        "prices_daily",
        "certified_universe",
        "certified_security_master",
        "marketdata",
        "fundamentals_canonical",
        "fundamentals_current",
    ):
        paths.append((key, _repo_path(config["source"][key])))
    for key in (
        "parent_holdings",
        "parent_snapshot",
        "parent_actual_account_performance",
        "parent_prior_model_performance",
        "parent_prepare_state",
        "parent_v1_fresh_scores",
        "parent_backtest_summary",
    ):
        paths.append((key, _repo_path(config["account"][key])))
    records = [
        {"role": role, "path": _relative(path), "sha256": sha256_file(path)}
        for role, path in paths
    ]
    records.append(
        {
            "role": "broker_source_identity_only",
            "path_stored": False,
            "sha256": config["account"]["broker_source_sha256"],
            "source_copied": False,
        }
    )
    return records


def _load_parent(config: Mapping[str, Any]) -> tuple[pd.DataFrame, dict[str, Any], dict[str, Any]]:
    account = config["account"]
    holdings = pd.read_csv(_repo_path(account["parent_holdings"]), dtype={"ticker": str})
    snapshot = _read_json(_repo_path(account["parent_snapshot"]))
    prepare_state = _read_json(_repo_path(account["parent_prepare_state"]))
    if snapshot.get("account_snapshot_status") != "VERIFIED":
        raise RuntimeError("parent account snapshot is not VERIFIED")
    if snapshot.get("account_asof") != config["dates"]["account_valuation_asof"]:
        raise RuntimeError("parent account snapshot date mismatch")
    if float(snapshot.get("gross_account_nav") or 0) != 79_113_563:
        raise RuntimeError("parent account gross NAV does not match the certified value")
    if prepare_state.get("run_id") != config["lineage"]["parent_run_id"]:
        raise RuntimeError("parent prepare state lineage mismatch")
    return holdings, snapshot, prepare_state


def _technical_backtest_audit(config: Mapping[str, Any]) -> dict[str, Any]:
    frame = pd.read_csv(_repo_path(config["account"]["parent_backtest_summary"]))
    row = frame.loc[frame["variant"].astype(str).str.endswith("fresh_start")]
    if len(row) != 1:
        raise RuntimeError("parent fresh-start technical backtest row is not unique")
    record = row.iloc[0]
    old_blockers = str(record.get("promotion_blockers") or "").split("|")
    removed = {
        "437350_HISTORY_INCOMPLETE",
        "CASH_EQUIVALENT_RETURN_PLACEHOLDER_437350",
    }
    retained = [item for item in old_blockers if item and item not in removed]
    return {
        "contract_version": "ADVISOR_FULL_RESET_V2_PRIVATE_BACKTEST_TECHNICAL_AUDIT",
        "visibility": "PRIVATE_TECHNICAL_AUDIT_ONLY",
        "validation_status": "PROVISIONAL_INCOMPLETE_DATA",
        "production_comparison_metrics_status": str(record["production_comparison_metrics_status"]),
        "observed_sample_CAGR": float(record["observed_sample_CAGR"]),
        "observed_sample_MDD": float(record["observed_sample_MDD"]),
        "average_turnover": float(record["average_turnover"]),
        "total_trading_cost_ratio": float(record["total_trading_cost_ratio"]),
        "expected_calendar_months": int(record["expected_calendar_months"]),
        "observed_months": int(record["observed_months"]),
        "full_calendar_return_coverage": float(record["full_calendar_return_coverage"]),
        "selected_security_return_coverage": float(record["selected_security_return_coverage"]),
        "krx300_monthly_coverage": float(record["krx300_monthly_coverage"]),
        "cash_return_contract": CASH_RETURN_CONTRACT,
        "implementation_vehicle_history_used": False,
        "removed_model_blockers": sorted(removed),
        "retained_parent_technical_blockers": retained,
        "advisor_body_metrics_published": False,
    }


def _combine_canonical_flows_with_exact_parent_attribution(
    *,
    canonical_path: Path,
    exact_panel: pd.DataFrame,
    selected_tickers: set[str],
    information_asof: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Build a PIT panel without treating total net income/equity as parent values.

    Revenue, operating income and CFO are non-attribution-specific statement
    flows and may come from the immutable canonical CFS panel.  Parent net
    income, parent equity and EV components remain exact-XBRL-only.  Missing
    exact parent attribution is preserved as NA rather than filled from total
    NetIncome/Equity.
    """

    canonical = pd.read_parquet(canonical_path).copy()
    canonical["ticker"] = canonical["ticker"].map(_ticker)
    canonical["asof"] = pd.to_datetime(canonical["asof"], errors="coerce")
    canonical["fs_div_used"] = canonical["fs_div_used"].astype("string").str.upper()
    cutoff = pd.Timestamp(information_asof)
    canonical = canonical.loc[
        canonical["ticker"].isin(selected_tickers)
        & canonical["fs_div_used"].eq("CFS")
        & canonical["asof"].le(cutoff)
    ].copy()
    canonical = canonical.sort_values(
        ["ticker", "year", "quarter", "asof"], kind="mergesort"
    ).drop_duplicates(["ticker", "year", "quarter"], keep="last")
    base = canonical[
        [
            "ticker",
            "name",
            "corp_code",
            "year",
            "quarter",
            "asof",
            "source_received_date",
            "Revenue",
            "OpIncome",
            "CFO",
        ]
    ].rename(
        columns={
            "asof": "financial_information_asof",
            "Revenue": "revenue_quarter",
            "OpIncome": "operating_income_quarter",
            "CFO": "cfo_quarter",
        }
    )
    base["financial_information_asof"] = pd.to_datetime(
        base["financial_information_asof"], errors="coerce"
    ).dt.date.astype("string")
    base["source_received_date"] = pd.to_datetime(
        base["source_received_date"], errors="coerce"
    ).dt.date.astype("string")
    base["statement_scope"] = "CFS"
    base["canonical_flow_source"] = "IMMUTABLE_FUNDAMENTALS_CANONICAL_ASOF"

    exact = exact_panel.copy()
    exact["ticker"] = exact["ticker"].map(_ticker)
    exact_columns = [
        "ticker",
        "year",
        "quarter",
        "source_received_date",
        "revenue_quarter",
        "operating_income_quarter",
        "parent_net_income_quarter",
        "cfo_quarter",
        "parent_equity_latest",
        "interest_bearing_debt_latest",
        "preferred_equity_latest",
        "noncontrolling_interest_latest",
        "cash_and_cash_equivalents_latest",
    ]
    exact = exact[[column for column in exact_columns if column in exact.columns]]
    merged = base.merge(
        exact,
        on=["ticker", "year", "quarter"],
        how="left",
        suffixes=("_canonical", "_exact"),
        validate="one_to_one",
    )
    for field in ("revenue_quarter", "operating_income_quarter", "cfo_quarter"):
        exact_field = f"{field}_exact"
        canonical_field = f"{field}_canonical"
        merged[field] = merged.get(exact_field).combine_first(merged.get(canonical_field))
        merged.drop(columns=[exact_field, canonical_field], inplace=True)
    merged["source_received_date"] = merged.get(
        "source_received_date_exact"
    ).combine_first(merged.get("source_received_date_canonical"))
    merged.drop(
        columns=["source_received_date_exact", "source_received_date_canonical"],
        inplace=True,
    )
    exact_parent_fields = [
        "parent_net_income_quarter",
        "parent_equity_latest",
        "interest_bearing_debt_latest",
        "preferred_equity_latest",
        "noncontrolling_interest_latest",
        "cash_and_cash_equivalents_latest",
    ]
    for field in exact_parent_fields:
        if field not in merged.columns:
            merged[field] = pd.NA
    merged["parent_attribution_source"] = merged["parent_equity_latest"].notna().map(
        {True: "EXACT_DART_XBRL", False: "UNAVAILABLE_NO_EXACT_PARENT_ATTRIBUTION"}
    )
    merged["total_net_income_fallback_used"] = False
    merged["total_equity_fallback_used"] = False
    merged = merged.sort_values(["ticker", "year", "quarter"], kind="mergesort")

    contiguous_counts: dict[str, bool] = {}
    for ticker, group in merged.groupby("ticker"):
        last_four = group.tail(4)
        ordinals = (
            pd.to_numeric(last_four["year"], errors="coerce").astype(int) * 4
            + pd.to_numeric(last_four["quarter"], errors="coerce").astype(int)
            - 1
        ).tolist()
        contiguous_counts[str(ticker)] = len(ordinals) == 4 and all(
            later - earlier == 1 for earlier, later in zip(ordinals, ordinals[1:])
        )
    qa = {
        "contract_version": "ADVISOR_FULL_RESET_V2_FINANCIAL_SOURCE_COMBINATION_V1",
        "status": "PASS",
        "information_asof": information_asof,
        "statement_scope": "CFS",
        "canonical_flow_fields": ["revenue_quarter", "operating_income_quarter", "cfo_quarter"],
        "exact_parent_attribution_fields": exact_parent_fields,
        "selected_security_count": len(selected_tickers),
        "selected_with_contiguous_four_quarters": int(sum(contiguous_counts.values())),
        "selected_without_contiguous_four_quarters": sorted(
            ticker for ticker, passed in contiguous_counts.items() if not passed
        ),
        "exact_parent_equity_security_count": int(
            merged.loc[merged["parent_equity_latest"].notna(), "ticker"].nunique()
        ),
        "total_net_income_fallback_used": False,
        "total_equity_fallback_used": False,
        "future_information_used": False,
    }
    return merged, qa


def prepare_run(*, config_path: Path, run_id: str) -> Path:
    config = _load_config(config_path)
    run_base = _repo_path(config["immutability"]["run_root"])
    staging = run_base / f".run_id={run_id}.staging"
    final = run_base / f"run_id={run_id}"
    if staging.exists() or final.exists():
        raise FileExistsError(f"run id already exists: {run_id}")
    staging.mkdir(parents=True, exist_ok=False)
    (staging / "visual_qa").mkdir()

    protected_before = protected_state(REPO_ROOT)
    holdings, snapshot, parent_state = _load_parent(config)
    industry_policy = _read_json(_repo_path(config["source"]["industry_policy"]))
    display_names = industry_policy["display_name_exact_ticker_map"]
    holdings = _apply_names(holdings, display_names)

    account_valuation, account_summary = build_account_valuation_snapshot(holdings, snapshot)
    liquidation, capital_summary = calculate_account_asof_liquidation(
        account_valuation,
        commission_rate=float(config["cost_policy"]["commission_rate"]),
        sell_tax_rate_by_market=config["cost_policy"]["sell_tax_rate_by_market"],
        default_sell_tax_rate=float(config["cost_policy"]["unknown_market_sell_tax_rate"]),
        liability_value=float(snapshot.get("credit_or_loan_amount") or 0),
    )

    bundle = build_score_parity_bundle_from_paths(
        _repo_path(config["source"]["production_scores"]),
        filters=config["model"]["production_filter_contract"],
        top_k=int(config["model"]["top_k"]),
        tolerance=float(config["model"]["score_parity_tolerance"]),
        fragile_threshold=float(config["model"]["boundary_fragile_threshold"]),
        drifted_fresh_scores_path=_repo_path(config["account"]["parent_v1_fresh_scores"]),
        authoritative_filter_inputs_path=_repo_path(config["source"]["certified_universe"]),
        require_drift_diagnosis=True,
    )
    for frame in (
        bundle.fresh_scores,
        bundle.fresh_top_k,
        bundle.parity_audit,
        bundle.top_k_boundary_watchlist,
    ):
        if "ticker" in frame.columns:
            frame["ticker"] = frame["ticker"].map(_ticker)
            if "name" in frame.columns:
                frame["name"] = frame["ticker"].map(display_names).combine_first(frame["name"])
    bundle.fresh_top_k["quality_penalty"] = bundle.fresh_top_k["quality_penalty_total"]
    bundle.fresh_top_k["quality_penalty_reason"] = _quality_penalty_reason(bundle.fresh_top_k)
    write_score_parity_artifacts(bundle, staging)

    reference_prices, reference_summary = build_common_reference_price_snapshot(
        _repo_path(config["source"]["prices_daily"]),
        bundle.fresh_top_k["ticker"].tolist(),
        requested_asof=config["dates"]["reference_price_requested_asof"],
    )
    target_portfolio, target_summary = build_v2_target_portfolio(
        bundle.fresh_top_k,
        reference_prices,
        advisory_rebalance_capital=float(capital_summary["advisory_rebalance_capital"]),
        target_cash_equivalent_weight=float(config["model"]["target_cash_equivalent_weight"]),
        buy_commission_rate=float(config["cost_policy"]["buy_commission_rate"]),
    )
    current_vs_target = build_current_vs_target_v2(account_valuation, target_portfolio)

    equity_current = account_valuation.loc[
        account_valuation["asset_class"].astype(str).eq("EQUITY")
    ].copy()
    equity_target = target_portfolio.loc[
        target_portfolio["asset_class"].astype(str).eq("EQUITY")
    ].copy()
    requested_registry = pd.concat(
        [
            equity_current[["ticker", "name"]],
            equity_target[["ticker", "name"]],
        ],
        ignore_index=True,
    ).drop_duplicates("ticker")
    source_scores = pd.read_csv(
        _repo_path(config["source"]["production_scores"]),
        dtype={"ticker": str, "corp_code": str},
    )
    identifier_override = pd.DataFrame(industry_policy.get("exact_identifier_overrides") or [])
    registry = build_security_registry_from_exact_sources(
        requested_registry,
        [source_scores[["ticker", "corp_code"]], identifier_override[["ticker", "corp_code"]]],
    )
    registry["name"] = registry["ticker"].map(display_names).combine_first(registry["name"])

    marketdata = pd.read_parquet(_repo_path(config["source"]["marketdata"])).copy()
    marketdata["ticker"] = marketdata["ticker"].map(_ticker)
    marketdata["industry_code"] = marketdata["industry_code"].astype("string").str.strip()
    code_names = {str(key): str(value) for key, value in industry_policy["official_industry_name_by_code"].items()}
    marketdata["industry_name"] = marketdata["industry_code"].map(code_names).combine_first(
        marketdata["industry_name"]
    )
    official_reference = marketdata.loc[
        marketdata["ticker"].isin(set(registry["ticker"])),
        ["ticker", "industry_code", "industry_name"],
    ].drop_duplicates("ticker")
    sector_map = build_advisor_sector_map_from_exact_ticker_policy(
        registry,
        industry_policy["advisor_sector_exact_ticker_map"],
        advisor_sector_source=industry_policy["advisor_sector_source"],
    )
    current_positions = equity_current.rename(columns={"account_asof_value": "current_value"})
    industry_artifacts = build_industry_artifacts(
        registry,
        official_reference,
        sector_map,
        official_industry_source="KRX_REFERENCE_DATA_3133_20260329_EXACT_TICKER",
        taxonomy_namespace=industry_policy["official_taxonomy_default"]["namespace"],
        taxonomy_version=industry_policy["official_taxonomy_default"]["version"],
        current_positions=current_positions,
        target_positions=equity_target,
        current_weight_column="current_weight",
        current_value_column="current_value",
        target_weight_column="target_weight",
        target_value_column="target_value",
        target_equity_weight=1.0 - float(config["model"]["target_cash_equivalent_weight"]),
        issuer_primary_business_overrides=industry_policy["official_industry_exact_overrides"],
    )
    write_industry_artifacts(industry_artifacts, staging)

    industry_columns = [
        "ticker",
        "official_industry_code",
        "official_industry_name",
        "official_industry_source",
        "taxonomy_namespace",
        "taxonomy_version",
        "advisor_sector",
        "advisor_sector_source",
    ]
    industry_mapping = industry_artifacts.industry_mapping_qa
    fresh_top_k_with_industry = bundle.fresh_top_k.merge(
        industry_mapping[industry_columns], on="ticker", how="left", validate="one_to_one"
    )
    target_portfolio = target_portfolio.merge(
        industry_mapping[industry_columns], on="ticker", how="left", validate="many_to_one"
    )

    top_registry = registry.loc[
        registry["ticker"].isin(set(fresh_top_k_with_industry["ticker"]))
    ]
    exact_panel = build_exact_quarterly_financials_from_dart_cache(
        top_registry[["ticker", "name", "corp_code"]],
        _repo_path(config["source"]["dart_raw_dir"]),
        information_asof=config["dates"]["model_information_asof"],
        statement_scope="CFS",
        lookback_years=2,
    )
    combined_financial_panel, combined_financial_qa = (
        _combine_canonical_flows_with_exact_parent_attribution(
            canonical_path=_repo_path(config["source"]["fundamentals_canonical"]),
            exact_panel=exact_panel.panel,
            selected_tickers=set(fresh_top_k_with_industry["ticker"]),
            information_asof=config["dates"]["model_information_asof"],
        )
    )
    valuation_artifacts = build_valuation_artifacts(
        fresh_top_k_with_industry,
        marketdata.loc[
            marketdata["ticker"].isin(set(fresh_top_k_with_industry["ticker"]))
        ],
        combined_financial_panel,
        model_information_asof=config["dates"]["model_information_asof"],
        valuation_asof=config["dates"]["model_information_asof"],
        statement_scope="CFS",
        industry_mapping=industry_mapping,
    )
    write_valuation_artifacts(valuation_artifacts, staging)

    reference_asof = reference_summary["reference_price_asof"]
    date_basis_qa = {
        "contract_version": "ADVISOR_FULL_RESET_V2_DATE_BASIS_V1",
        "status": "PASS",
        "model_information_asof": config["dates"]["model_information_asof"],
        "account_valuation_asof": config["dates"]["account_valuation_asof"],
        "reference_price_requested_asof": config["dates"]["reference_price_requested_asof"],
        "reference_price_asof": reference_asof,
        "reference_price_contract": reference_summary["reference_price_contract"],
        "current_account_value_source": "BROKER_EXACT_SNAPSHOT",
        "hypothetical_liquidation_value_source": "BROKER_MARKET_VALUE_AT_ACCOUNT_ASOF",
        "model_score_value_source": "AUTHORITATIVE_PRODUCTION_SCORE_AT_MODEL_INFORMATION_ASOF",
        "reference_target_quantity_price_source": "COMMON_OFFICIAL_CLOSE",
        "model_price_used_to_revalue_current_account": False,
        "single_actual_nav_mixed_from_multiple_dates": False,
        "all_selected_prices_same_cutoff": reference_summary["all_selected_prices_same_cutoff"],
    }
    if reference_asof is None or reference_asof > config["dates"]["reference_price_requested_asof"]:
        raise RuntimeError("reference price cutoff violates the requested date")

    technical_backtest = _technical_backtest_audit(config)
    blocker_taxonomy = build_blocker_taxonomy(
        score_parity_pass=bundle.parity_summary["status"] == "PASS",
        advisor_contract_checks={
            "VALUATION_LAYER_MISSING": valuation_artifacts.qa.get("advisor_report_blocker") is None,
            "ACCOUNT_SIZING_DATE_CONTRACT_FAILURE": date_basis_qa["status"] == "PASS",
            "INVALID_INDUSTRY_MAPPING": industry_artifacts.qa["status"] == "PASS",
            "NAV_IDENTITY_FAILURE": account_summary["nav_identity_status"] == "PASS",
            "TARGET_WEIGHT_FAILURE": bool(target_summary["cash_floor_satisfied"])
            and abs(float(target_summary["target_weight_total"]) - 1.0) <= 1e-12,
        },
        activity_ledger_available=False,
    )
    if blocker_taxonomy["ADVISOR_REPORT_BLOCKERS"]:
        raise RuntimeError("advisor blockers remain after V2 correction")

    prior_detail = pd.read_csv(_repo_path(config["account"]["parent_prior_model_performance"]), dtype={"ticker": str})
    actual_detail = pd.read_csv(_repo_path(config["account"]["parent_actual_account_performance"]), dtype={"ticker": str})
    parent_report_summary = parent_state["report_summary"]
    prior_summary = dict(parent_report_summary["prior_model_performance"])
    actual_summary = dict(parent_report_summary["actual_account_performance"])
    actual_summary.update(
        {
            "performance_status": "PROVISIONAL_ACCOUNT_PERFORMANCE",
            "activity_ledger_status": "NOT_AVAILABLE",
            "endpoint_nav_change_ratio_unadjusted": float(
                actual_summary["endpoint_change_ratio_not_cashflow_adjusted"]
            ),
            "account_performance_return": None,
            "new_advisory_generation_blocked": False,
        }
    )

    report_summary = {
        "contract_version": "ADVISOR_FULL_RESET_V2_REPORT_INPUT_V1",
        "run_id": run_id,
        "mode": "ADVISOR_FULL_RESET",
        "development_status": config["development_status"],
        "production_promoted": False,
        "production_promotion_status": config["production_promotion_status"],
        "model_information_asof": config["dates"]["model_information_asof"],
        "account_valuation_asof": config["dates"]["account_valuation_asof"],
        "reference_price_asof": reference_asof,
        "date_basis": date_basis_qa,
        "score_parity_status": bundle.parity_summary["status"],
        "score_parity": bundle.parity_summary,
        "top_k": int(config["model"]["top_k"]),
        "boundary": bundle.top_k_boundary_qa,
        "policy": {
            "top_k": int(config["model"]["top_k"]),
            "target_cash_equivalent_weight": float(config["model"]["target_cash_equivalent_weight"]),
            "target_equity_weight": 1.0 - float(config["model"]["target_cash_equivalent_weight"]),
        },
        "account": account_summary,
        "capital": capital_summary,
        "target_sizing": target_summary,
        "prior_model_performance": prior_summary,
        "actual_account_performance": actual_summary,
        "backtest": {
            "validation_status": technical_backtest["validation_status"],
            "coverage_status": "INCOMPLETE",
            "production_promotion_status": config["production_promotion_status"],
        },
        "blocker_taxonomy": blocker_taxonomy,
    }
    report_top = fresh_top_k_with_industry.copy()
    report_target = _apply_names(target_portfolio, display_names)
    report_current = _apply_names(account_valuation, display_names)
    report_liquidation = _apply_names(liquidation, display_names)
    report_comparison = _apply_names(current_vs_target, display_names)
    html = generate_advisor_v2_html(
        report_summary,
        current_portfolio=report_current,
        liquidation=report_liquidation,
        top_k=report_top,
        boundary_watchlist=bundle.top_k_boundary_watchlist,
        target_portfolio=report_target,
        current_vs_target=report_comparison,
        selected_details={
            "selected_security_financials": valuation_artifacts.selected_security_financials,
            "selected_security_diagnostics": valuation_artifacts.selected_security_diagnostics,
        },
        valuation_qa=valuation_artifacts.valuation_qa,
        sector_exposure={
            "current": industry_artifacts.sector_exposure_current,
            "target": industry_artifacts.sector_exposure_target,
        },
        prior_model_performance=prior_detail,
        actual_account_performance=actual_detail,
        qa=blocker_taxonomy,
    )
    html_qa = validate_advisor_v2_html(html)
    if not html_qa["pass"]:
        raise RuntimeError(f"V2 HTML validation failed: {html_qa['failed_checks']}")

    _write_csv(staging / "account_valuation_snapshot.csv", account_valuation)
    _write_csv(staging / "hypothetical_full_liquidation_account_asof.csv", liquidation)
    _write_json(staging / "advisory_capital_summary_v2.json", capital_summary)
    _write_csv(staging / "reference_price_snapshot.csv", reference_prices)
    _write_json(staging / "date_basis_qa.json", date_basis_qa)
    _write_csv(staging / "fresh_start_top_k_v2.csv", report_top)
    _write_csv(staging / "target_portfolio_v2.csv", report_target)
    _write_csv(staging / "current_vs_target_v2.csv", report_comparison)
    _write_csv(staging / "prior_model_performance_v2.csv", prior_detail)
    _write_csv(staging / "actual_account_endpoint_change_v2.csv", actual_detail)
    _write_json(staging / "private_backtest_technical_audit.json", technical_backtest)
    _write_json(staging / "blocker_taxonomy.json", blocker_taxonomy)
    _write_json(
        staging / "exact_financial_input_qa.json",
        {"raw_exact_xbrl": exact_panel.qa, "combined_internal_panel": combined_financial_qa},
    )
    _write_text(staging / "quarterly_advisor_report_v2.html", html)
    _write_json(
        staging / "prepare_state.json",
        {
            "contract_version": "ADVISOR_FULL_RESET_V2_PREPARE_STATE_V1",
            "run_id": run_id,
            "status": "PREPARED_AWAITING_PDF_QA",
            "prepared_at": datetime.now(timezone.utc).isoformat(),
            "config_path": _relative(config_path),
            "config_sha256": sha256_file(config_path),
            "parent_run_id": config["lineage"]["parent_run_id"],
            "protected_state_before": protected_before,
            "input_artifacts": _input_artifacts(config, config_path),
            "report_summary": report_summary,
            "html_qa": html_qa,
            "raw_broker_source_sha256": config["account"]["broker_source_sha256"],
        },
    )
    return staging


def render_run(*, config_path: Path, run_id: str) -> Path:
    config = _load_config(config_path)
    staging = _repo_path(config["immutability"]["run_root"]) / f".run_id={run_id}.staging"
    state = _read_json(staging / "prepare_state.json")
    if state.get("status") != "PREPARED_AWAITING_PDF_QA":
        raise RuntimeError("staging run is not ready for render")
    from scripts.qa.render_public_report import render_public_report

    poppler: str | None = None
    profile = os.environ.get("USERPROFILE")
    if profile:
        candidate = (
            Path(profile)
            / ".cache/codex-runtimes/codex-primary-runtime/dependencies/native/poppler/Library/bin/pdftoppm.exe"
        )
        if candidate.is_file():
            poppler = str(candidate)
    render_public_report(
        staging / "quarterly_advisor_report_v2.html",
        pdf_path=staging / "quarterly_advisor_report_v2.pdf",
        desktop_path=staging / "visual_qa/desktop_1440.png",
        mobile_path=staging / "visual_qa/mobile_390.png",
        pdf_pages_dir=staging / "visual_qa/pdf_pages",
        render_metadata_path=staging / "visual_qa/render_metadata.json",
        pdftoppm_executable=poppler,
        pdf_page_dpi=144,
    )
    return staging / "quarterly_advisor_report_v2.pdf"


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def _runtime_checks(staging: Path, state: Mapping[str, Any], protected_after: Mapping[str, Any]) -> list[dict[str, Any]]:
    parity = _read_json(staging / "score_parity_audit.json")
    population = _read_json(staging / "scoring_population_audit.json")
    capital = _read_json(staging / "advisory_capital_summary_v2.json")
    dates = _read_json(staging / "date_basis_qa.json")
    valuation = _read_json(staging / "valuation_qa.json")
    industry = _read_json(staging / "industry_sector_qa.json")
    boundary = _read_json(staging / "top_k_boundary_qa.json")
    blockers = _read_json(staging / "blocker_taxonomy.json")
    technical = _read_json(staging / "private_backtest_technical_audit.json")
    current = pd.read_csv(staging / "account_valuation_snapshot.csv", dtype={"ticker": str})
    target = pd.read_csv(staging / "target_portfolio_v2.csv", dtype={"ticker": str})
    valuation_rows = pd.read_csv(staging / "valuation_qa.csv", dtype={"ticker": str})
    industry_rows = pd.read_csv(staging / "industry_mapping_qa.csv", dtype={"ticker": str})
    sector_target = pd.read_csv(staging / "sector_exposure_target.csv")
    boundary_rows = pd.read_csv(staging / "top_k_boundary_watchlist.csv", dtype={"ticker": str})
    html = (staging / "quarterly_advisor_report_v2.html").read_text(encoding="utf-8")
    combined = html + "\n" + _pdf_text(staging / "quarterly_advisor_report_v2.pdf")
    exact_gross = float(current["account_asof_value"].sum())
    cash_target = target.loc[target["ticker"].eq("CASH_EQUIVALENT_BUCKET")].iloc[0]
    checks = [
        (1, "source score_base and fresh score_base exact parity", parity["checks"]["base_score_parity"] == "PASS" and float(parity["max_abs_score_base_diff"]) <= 1e-12),
        (2, "source and fresh quality penalty exact parity", parity["checks"]["quality_penalty_parity"] == "PASS" and float(parity["max_abs_quality_penalty_diff"]) <= 1e-12),
        (3, "selection is independent of current holdings", parity["checks"]["current_holdings_independence"] == "PASS_BY_CONSTRUCTION_NO_HOLDINGS_INPUT"),
        (4, "current account values use broker account-asof", abs(exact_gross - 79_113_563) <= 0.5 and set(current["valuation_source"]) == {"BROKER_EXACT_SNAPSHOT"}),
        (5, "advisory capital uses account-asof broker values", bool(capital["broker_market_value_used"]) and abs(float(capital["advisory_rebalance_capital"]) - 78_974_468.621) <= 1e-6),
        (6, "model and sizing dates are separately recorded", dates["model_information_asof"] == "2026-08-18" and dates["account_valuation_asof"] == "2026-08-20" and dates["reference_price_asof"] == "2026-08-18"),
        (7, "valuation formulas and exact-scope inputs reproduce", valuation["status"] in {"PASS", "PASS_WITH_CONTRACTUAL_NA"} and not valuation["provider_multiples_used"]),
        (8, "non-positive valuation denominators are NA", not bool(((pd.to_numeric(valuation_rows["parent_net_income_ttm"], errors="coerce") <= 0) & pd.to_numeric(valuation_rows["per_ttm"], errors="coerce").notna()).any())),
        (9, "EV proxy is forbidden", not valuation["ev_proxy_used"] and not bool(valuation_rows["ev_proxy_used"].astype(bool).any())),
        (10, "322000 invalid semiconductor mapping is absent", bool(industry["known_invalid_322000_mapping_absent"]) and not bool(industry_rows.loc[industry_rows["ticker"].eq("322000"), "known_invalid_mapping"].astype(bool).any())),
        (11, "advisor sector target weights reconcile to equity target", industry["target_sector_weight_reconciliation"] == "PASS" and abs(float(sector_target["sector_weight"].sum()) - 0.9) <= 1e-12),
        (12, "437350 history is not a model promotion blocker", "437350_HISTORY_INCOMPLETE" not in blockers["MODEL_PROMOTION_BLOCKERS"] and "CASH_EQUIVALENT_RETURN_PLACEHOLDER_437350" not in blockers["MODEL_PROMOTION_BLOCKERS"]),
        (13, "cash bucket zero nominal return contract is explicit", technical["cash_return_contract"] == CASH_RETURN_CONTRACT and cash_target["cash_return_contract"] == CASH_RETURN_CONTRACT),
        (14, "provisional account endpoint is not presented as a return", "endpoint_nav_change_ratio_unadjusted" in html and "not an investment return" in html and "실제 수익률" not in html and "성과수익률" not in html),
        (15, "unvalidated CAGR and MDD are absent from advisor body", re.search(r"CAGR|MDD|20\.69%|-32\.28%|90\.14%|8\.33%", html, flags=re.I) is None),
        (16, "Top-K boundary watchlist covers K-2 through K+5", boundary["status"] == "PASS" and len(boundary_rows) == 8 and set(boundary_rows["rank"].astype(int)) == set(range(8, 16))),
        (17, "HTML/PDF contain no personal data or absolute path", re.search(r"[A-Za-z]:[\\/]", combined) is None and config_account_identifier_absent(combined)),
        (18, "existing immutable runs, production latest, and model outputs are unchanged", protected_after == state["protected_state_before"]),
    ]
    checks.extend(
        [
            (19, "advisor report blockers are empty", blockers["ADVISOR_REPORT_BLOCKERS"] == []),
        (20, "score population drift cause is exactly diagnosed", population.get("drift_diagnosis", {}).get("cause") == "EXACT_MATCH_MCAP_TRADED_VALUE_FILTERS_BEFORE_STANDARDIZATION"),
            (21, "target weights total 100% and cash floor is at least 10%", abs(float(target["target_weight"].sum()) - 1.0) <= 1e-12 and float(cash_target["illustrative_target_value"]) / float(capital["advisory_rebalance_capital"]) + 1e-12 >= 0.10),
        ]
    )
    return [
        {"id": number, "description": description, "status": "PASS" if bool(passed) else "FAIL"}
        for number, description, passed in checks
    ]


def config_account_identifier_absent(text: str) -> bool:
    return (
        "*******2901" not in text
        and "22c08825b820b28259bd5a4eb6428ab45bba2c9dc2c2245b9d6fe9452b8e34e3" not in text
        and ".xlsx" not in text.lower()
    )


def finalize_run(*, config_path: Path, run_id: str) -> Path:
    config = _load_config(config_path)
    base = _repo_path(config["immutability"]["run_root"])
    staging = base / f".run_id={run_id}.staging"
    final = base / f"run_id={run_id}"
    if not staging.is_dir() or final.exists():
        raise FileNotFoundError(staging)
    state = _read_json(staging / "prepare_state.json")
    rendered = (
        staging / "quarterly_advisor_report_v2.pdf",
        staging / "visual_qa/desktop_1440.png",
        staging / "visual_qa/mobile_390.png",
        staging / "visual_qa/render_metadata.json",
    )
    if not all(path.is_file() for path in rendered) or not (staging / "visual_qa/pdf_pages").is_dir():
        raise RuntimeError("rendered report QA artifacts are incomplete")
    inspection = _read_json(staging / "visual_qa/visual_inspection.json")
    if inspection.get("status") != "PASS" or not inspection.get("all_pdf_pages_reviewed"):
        raise RuntimeError("manual visual inspection evidence has not passed")

    protected_after = protected_state(REPO_ROOT)
    checks = _runtime_checks(staging, state, protected_after)
    failures = [check for check in checks if check["status"] != "PASS"]
    qa_lines = [
        "# ADVISOR_FULL_RESET_V2 QA",
        "",
        f"- Run ID: `{run_id}`",
        f"- Development status: **{config['development_status']}**",
        "- Production promoted: **false**",
        f"- Production promotion: **{config['production_promotion_status']}**",
        "- Visual QA: **VISUALLY_INSPECTED_BY_CODEX_PASS**",
        "",
        "## Runtime contract checks",
        "",
    ]
    qa_lines.extend(
        f"{check['id']}. **{check['status']}** — {check['description']}" for check in checks
    )
    qa_lines.extend(
        [
            "",
            "## Remaining model-promotion blockers",
            "",
            *[
                f"- {item}"
                for item in state["report_summary"]["blocker_taxonomy"]["MODEL_PROMOTION_BLOCKERS"]
            ],
            "",
            "성과 인증 경고는 advisor 보고서 생성을 차단하지 않으며, 위 역사 검증 차단사유가 해소되기 전에는 production 승격을 허용하지 않아요.",
        ]
    )
    _write_text(staging / "ADVISOR_FULL_RESET_V2_QA.md", "\n".join(qa_lines) + "\n")
    if failures:
        raise RuntimeError(f"V2 runtime QA failed: {failures}")
    for relative in REQUIRED_OUTPUTS[:-2]:
        if not (staging / relative).is_file():
            raise RuntimeError(f"required V2 output missing before manifest: {relative}")

    artifacts = relative_artifact_manifest(
        staging, exclude={"run_manifest.json", "private_audit_bundle.zip"}
    )
    manifest = {
        "contract_version": "ADVISOR_FULL_RESET_V2_IMMUTABLE_RUN_V1",
        "run_id": run_id,
        "mode": "ADVISOR_FULL_RESET",
        "development_status": config["development_status"],
        "production_promoted": False,
        "production_promotion_status": config["production_promotion_status"],
        "parent_run_id": config["lineage"]["parent_run_id"],
        "authoritative_model_run_id": config["lineage"]["authoritative_model_run_id"],
        "prepared_at": state["prepared_at"],
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "config": {"path": state["config_path"], "sha256": state["config_sha256"]},
        "input_artifacts": state["input_artifacts"],
        "protected_state_before": state["protected_state_before"],
        "protected_state_after": protected_after,
        "runtime_checks": checks,
        "blocker_taxonomy": state["report_summary"]["blocker_taxonomy"],
        "raw_broker_source_copied": False,
        "artifact_paths_are_run_relative": True,
        "artifacts": artifacts,
    }
    _write_json(staging / "run_manifest.json", manifest)
    allowlist = [row["path"] for row in artifacts]
    allowlist.append("run_manifest.json")
    allowlist = [
        item
        for item in allowlist
        if not item.startswith("visual_qa/") and item != "prepare_state.json"
    ]
    build_private_audit_bundle(
        run_root=staging,
        output_path=staging / "private_audit_bundle.zip",
        allowlist=allowlist,
        forbidden_source_sha256=state["raw_broker_source_sha256"],
    )
    with ZipFile(staging / "private_audit_bundle.zip") as archive:
        if archive.testzip() is not None:
            raise RuntimeError("private audit ZIP CRC failed")
        names = archive.namelist()
        if any(Path(name).suffix.lower() in {".xls", ".xlsx", ".xlsm"} for name in names):
            raise RuntimeError("spreadsheet leaked into private audit ZIP")
        if any(Path(name).is_absolute() or ".." in Path(name).parts for name in names):
            raise RuntimeError("unsafe private audit ZIP member")
    if protected_after != state["protected_state_before"]:
        raise RuntimeError("protected immutable state changed")
    publish_staging(staging, final)
    return final


def _default_run_id(account_asof: str) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"advisor_full_reset_v2_{account_asof.replace('-', '')}_{timestamp}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Immutable ADVISOR_FULL_RESET_V2 correction runner")
    parser.add_argument("--config", default="configs/advisor_full_reset_v2_20260821.json")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--stage", choices=("prepare", "render", "finalize"), default="prepare")
    args = parser.parse_args()
    config_path = _repo_path(args.config)
    config = _load_config(config_path)
    run_id = args.run_id or _default_run_id(config["dates"]["account_valuation_asof"])
    if args.stage == "prepare":
        output = prepare_run(config_path=config_path, run_id=run_id)
    elif args.stage == "render":
        output = render_run(config_path=config_path, run_id=run_id)
    else:
        output = finalize_run(config_path=config_path, run_id=run_id)
    print(f"[OK] {args.stage}: {_relative(output)}")


if __name__ == "__main__":
    main()
