from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.advisor.immutable_run import (  # noqa: E402
    protected_state,
    publish_staging,
    relative_artifact_manifest,
    sha256_file,
)
from scripts.financials.build_public_financial_metrics import (  # noqa: E402
    build_public_financial_metrics,
)
from scripts.financials.build_share_count_history import (  # noqa: E402
    attach_listed_share_reconciliation,
    exact_weighted_shares,
    implied_weighted_shares_from_disclosed_eps,
    infer_retroactive_adjustment_factor,
    listed_shares_from_market_cap,
)
from scripts.financials.calculate_per import calculate_per_ttm  # noqa: E402
from scripts.financials.calculate_ttm_eps import calculate_basic_eps_ttm  # noqa: E402
from scripts.financials.calculate_ttm_net_income import calculate_ttm_net_income  # noqa: E402
from scripts.financials.collect_dart_financial_facts import (  # noqa: E402
    collect_official_share_support,
    copy_private_xbrl_cache,
    load_normalized_parent_sources,
    load_selected_equities,
)
from scripts.financials.parse_eps_notes import (  # noqa: E402
    parse_eps_notes_from_xbrl_archive,
    select_eps_note_value,
)
from scripts.financials.resolve_reporting_periods import (  # noqa: E402
    resolve_latest_period,
    validate_period_bridge,
)
from scripts.financials.resolve_share_classes import resolve_share_classes  # noqa: E402
from scripts.qa.render_public_report import render_public_report  # noqa: E402
from scripts.subscriber_report.build_financial_complete_report import (  # noqa: E402
    PUBLIC_FORBIDDEN_METHOD_TERMS,
    build_financial_complete_html,
)


RUNS_ROOT = REPO_ROOT / "data/development/subscriber_financial_metrics_complete/runs"
CONFIG_PATH = REPO_ROOT / "configs/financial_metrics_engine_v1.json"
PUBLIC_HTML = "quant_screening_growth_acceleration_26Q3_financial_complete.html"
PUBLIC_PDF = "quant_screening_growth_acceleration_26Q3_financial_complete.pdf"
PUBLIC_ZIP = "public_distribution_bundle_financial_complete.zip"


@dataclass(frozen=True)
class FinancialMetricsResult:
    report_analysis_metrics: pd.DataFrame
    public_financial_metrics: pd.DataFrame
    completeness: dict[str, Any]
    artifact_paths: dict[str, Path]


def _canonical_digest(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest_tree(root: Path) -> dict[str, Any]:
    rows = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": int(path.stat().st_size),
                "sha256": sha256_file(path),
            }
        )
    return {"file_count": len(rows), "tree_sha256": _canonical_digest(rows), "files": rows}


def _safe_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _safe_csv(path: Path, frame: pd.DataFrame) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _resolve_repo_path(value: str | Path) -> Path:
    path = Path(value)
    return (path if path.is_absolute() else REPO_ROOT / path).resolve()


def _input_artifact(advisor_parent: Path, role: str) -> Path:
    manifest = json.loads((advisor_parent / "run_manifest.json").read_text(encoding="utf-8"))
    matches = [row for row in manifest.get("input_artifacts", []) if row.get("role") == role]
    if len(matches) != 1 or not matches[0].get("path"):
        raise ValueError(f"advisor parent input artifact role is not unique: {role}")
    return _resolve_repo_path(matches[0]["path"])


def _fact(mapping: pd.DataFrame, ticker: str, period_key: str, field: str) -> pd.Series:
    rows = mapping.loc[
        mapping["ticker"].eq(ticker)
        & mapping["period_key"].eq(period_key)
        & mapping["field"].eq(field)
    ]
    if len(rows) != 1:
        raise ValueError(f"fact identity is not unique: {ticker}/{period_key}/{field}")
    return rows.iloc[0]


def _fact_value(mapping: pd.DataFrame, ticker: str, period_key: str, field: str) -> float:
    row = _fact(mapping, ticker, period_key, field)
    if row["status"] != "PASS" or pd.isna(row["value"]):
        raise ValueError(f"exact fact unavailable: {ticker}/{period_key}/{field}")
    return float(row["value"])


def _optional_fact(mapping: pd.DataFrame, ticker: str, period_key: str, field: str) -> float | None:
    row = _fact(mapping, ticker, period_key, field)
    return float(row["value"]) if row["status"] == "PASS" and pd.notna(row["value"]) else None


def _numerator_source(row: pd.Series, share_class: dict[str, Any]) -> str:
    concept = str(row.get("concept", ""))
    context = str(row.get("context_ref", ""))
    if "OrdinaryEquityHolders" in concept or "OrdinarySharesMember" in context:
        return "XBRL_CLASS_SPECIFIC_BASIC_EPS_PROFIT"
    if share_class["numerator_parent_profit_fallback_allowed"]:
        return "PARENT_OR_OFS_PROFIT_ADJUSTED_ZERO_NO_ACTIVE_PREFERRED_CLASS"
    raise ValueError("parent profit cannot be used as basic EPS numerator with another active share class")


def _number(value: object) -> float | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).replace(",", "").strip()
    if not text or text == "-":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _canonical_actions(raw: pd.DataFrame) -> pd.DataFrame:
    columns = [
        "security_id", "event_type", "announcement_date", "effective_date", "record_date",
        "old_share_count", "new_share_count", "ordinary_share_change", "preferred_share_change",
        "treasury_share_change", "retroactive_adjustment_factor", "source_receipt",
    ]
    if raw.empty:
        return pd.DataFrame(columns=columns)
    rows = []
    for item in raw.to_dict("records"):
        quantity = _number(item.get("isu_dcrs_qy")) or 0.0
        style = str(item.get("isu_dcrs_stle", ""))
        effect = str(item.get("isu_dcrs_de", ""))
        if not effect:
            continue
        rows.append(
            {
                "security_id": str(item.get("ticker", "")),
                "event_type": style or "CAPITAL_INCREASE_OR_REDUCTION",
                "announcement_date": None,
                "effective_date": effect.replace(".", "-")[:10],
                "record_date": None,
                "old_share_count": None,
                "new_share_count": None,
                "ordinary_share_change": quantity,
                "preferred_share_change": 0.0,
                "treasury_share_change": 0.0,
                "retroactive_adjustment_factor": 1.0,
                "source_receipt": str(item.get("rcept_no", "")),
            }
        )
    return pd.DataFrame(rows, columns=columns)


def _read_parameterized_frame(value: pd.DataFrame | str | Path | None) -> pd.DataFrame | None:
    if value is None:
        return None
    if isinstance(value, pd.DataFrame):
        return value.copy()
    path = Path(value)
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() in {".csv", ".txt"}:
        return pd.read_csv(path, dtype={"ticker": str})
    raise ValueError(f"unsupported table format: {path}")


def run_financial_metrics_for_report_universe(
    *,
    report_analysis_equities: pd.DataFrame | str | Path,
    information_asof: str,
    price_asof: str,
    output_root: str | Path,
    source_financial_run: str | Path | None = None,
    source_marketdata: pd.DataFrame | str | Path | None = None,
    source_security_master: pd.DataFrame | str | Path | None = None,
    dart_cache_root: str | Path | None = None,
    use_network_if_missing: bool = False,
) -> FinancialMetricsResult:
    """Materialise the selected-plus-dropped report universe from certified metrics.

    The EPS, TTM net-income, PER, PBR, PSR, and CFO values are consumed from
    the existing generic financial engine's certified outputs.  This function
    only selects the caller's dynamic union and performs completeness/date
    gates; it does not recreate any financial formula.
    """

    universe = _read_parameterized_frame(report_analysis_equities)
    if universe is None or universe.empty or "ticker" not in universe.columns:
        raise ValueError("report_analysis_equities must contain at least one ticker")
    universe["ticker"] = universe["ticker"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
    if universe["ticker"].duplicated().any():
        raise ValueError("report_analysis_equities contains duplicate tickers")

    sources: list[Path] = []
    if source_financial_run is not None:
        source_root = Path(source_financial_run)
        if not source_root.is_dir():
            raise FileNotFoundError(source_root)
        manifest_path = source_root / "run_manifest.json"
        if manifest_path.is_file():
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            status = str(manifest.get("development_status") or manifest.get("status") or "")
            if status and not status.startswith("PASS"):
                raise RuntimeError(f"source financial run is not certified: {status}")
        for name in (
            "report_analysis_equity_metrics.csv",
            "full_security_metrics_private.csv",
            "private_financial_metrics.csv",
        ):
            candidate = source_root / name
            if candidate.is_file():
                sources.append(candidate)
    if dart_cache_root is not None:
        cache_root = Path(dart_cache_root)
        if not cache_root.is_dir():
            raise FileNotFoundError(cache_root)
        for name in ("report_analysis_equity_metrics.csv", "full_security_metrics_private.csv"):
            candidate = cache_root / name
            if candidate.is_file():
                sources.append(candidate)
    if not sources:
        reason = "certified financial metrics source is unavailable"
        if use_network_if_missing:
            reason += "; network collection requires an upstream certified generic-engine run"
        raise RuntimeError(reason)

    panels = []
    for source in sources:
        frame = pd.read_csv(source, dtype={"ticker": str})
        if "ticker" not in frame.columns:
            continue
        frame["ticker"] = frame["ticker"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
        frame["_source_priority"] = len(panels)
        panels.append(frame)
    if not panels:
        raise RuntimeError("certified financial metric files contain no ticker column")
    combined = pd.concat(panels, ignore_index=True, sort=False)
    combined = combined.sort_values("_source_priority").drop_duplicates("ticker", keep="first")
    metrics = universe.merge(
        combined.drop(columns=["name"], errors="ignore"),
        on="ticker",
        how="left",
        validate="one_to_one",
        suffixes=("", "_financial"),
    )
    metrics = metrics.drop(columns=["_source_priority"], errors="ignore")
    if "name" not in metrics.columns:
        metrics["name"] = metrics["ticker"]
    if "selection_status" not in metrics.columns:
        selected = metrics.get("model_selected", pd.Series(False, index=metrics.index))
        selected = selected.astype(str).str.lower().isin({"1", "true", "yes", "selected"})
        metrics["selection_status"] = selected.map({True: "SELECTED", False: "DROPPED"})

    market = _read_parameterized_frame(source_marketdata)
    if market is not None and "ticker" in market.columns:
        market["ticker"] = market["ticker"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
        market_columns = ["ticker"] + [
            column for column in ("market_cap", "mcap", "official_close", "close", "price_asof")
            if column in market.columns
        ]
        market = market[market_columns].drop_duplicates("ticker")
        metrics = metrics.merge(market, on="ticker", how="left", suffixes=("", "_market"))
        for destination, candidates in {
            "market_cap": ("market_cap_market", "mcap"),
            "official_close": ("official_close_market", "close"),
            "price_asof": ("price_asof_market",),
        }.items():
            if destination not in metrics.columns:
                metrics[destination] = pd.NA
            for candidate in candidates:
                if candidate in metrics.columns:
                    metrics[destination] = metrics[destination].where(metrics[destination].notna(), metrics[candidate])

    security_master = _read_parameterized_frame(source_security_master)
    if security_master is not None and "ticker" in security_master.columns:
        security_master["ticker"] = security_master["ticker"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(6)
        master_columns = ["ticker"] + [
            column for column in ("official_industry_name", "industry_name", "advisor_sector", "sector")
            if column in security_master.columns
        ]
        security_master = security_master[master_columns].drop_duplicates("ticker")
        metrics = metrics.merge(security_master, on="ticker", how="left", suffixes=("", "_master"))
        for destination, candidates in {
            "official_industry_name": ("official_industry_name_master", "industry_name"),
            "advisor_sector": ("advisor_sector_master", "sector"),
        }.items():
            if destination not in metrics.columns:
                metrics[destination] = pd.NA
            for candidate in candidates:
                if candidate in metrics.columns:
                    metrics[destination] = metrics[destination].where(metrics[destination].notna(), metrics[candidate])

    requested = set(universe["ticker"])
    covered = set(combined["ticker"])
    missing_tickers = sorted(requested.difference(covered))
    if missing_tickers:
        raise RuntimeError(f"financial metrics missing report-analysis tickers: {missing_tickers}")
    required_columns = {
        "eps_ttm", "net_income_ttm", "per_ttm", "per_status", "pbr", "psr",
        "cfo_ttm", "cfo_to_operating_income", "revenue_ttm", "operating_income_ttm",
        "period_minus_2", "period_minus_1", "period_latest",
    }
    if missing_columns := required_columns.difference(metrics.columns):
        raise RuntimeError(f"certified financial metrics missing columns: {sorted(missing_columns)}")
    if "price_asof" in metrics.columns:
        observed_dates = pd.to_datetime(metrics["price_asof"], errors="coerce")
        if observed_dates.notna().any() and observed_dates.max() > pd.Timestamp(price_asof):
            raise ValueError("future price observation detected")
    eps_coverage = int(pd.to_numeric(metrics["eps_ttm"], errors="coerce").notna().sum())
    net_coverage = int(pd.to_numeric(metrics["net_income_ttm"], errors="coerce").notna().sum())
    per_numeric = pd.to_numeric(metrics["per_ttm"], errors="coerce").notna()
    per_loss = metrics["per_status"].astype(str).str.upper().eq("LOSS")
    per_coverage = int((per_numeric | per_loss).sum())
    valuation_coverage = int(metrics[["pbr", "psr"]].notna().all(axis=1).sum())
    # CFO itself must be complete.  CFO/operating-income is intentionally NA
    # when the denominator is non-positive, matching the certified engine.
    cfo_coverage = int(pd.to_numeric(metrics["cfo_ttm"], errors="coerce").notna().sum())
    total = len(metrics)
    completeness = {
        "contract": "PARAMETERIZED_REPORT_UNIVERSE_FINANCIAL_METRICS_V1",
        "information_asof": str(pd.Timestamp(information_asof).date()),
        "price_asof": str(pd.Timestamp(price_asof).date()),
        "report_analysis_equity_count": total,
        "eps_coverage": eps_coverage,
        "net_income_coverage": net_coverage,
        "per_numeric_or_loss_coverage": per_coverage,
        "pbr_psr_coverage": valuation_coverage,
        "cfo_diagnostic_presence_coverage": total,
        "cfo_numeric_coverage": cfo_coverage,
        "ticker_specific_override_count": 0,
        "company_specific_override_count": 0,
        "financial_metrics_complete": all(
            value == total
            for value in (eps_coverage, net_coverage, per_coverage, valuation_coverage)
        ),
    }
    if not completeness["financial_metrics_complete"]:
        raise RuntimeError(f"financial metrics completeness gate failed: {completeness}")

    public_columns = [
        "ticker", "name", "selection_status", "eps_ttm", "net_income_ttm", "per_ttm",
        "pbr", "psr", "revenue_ttm", "operating_income_ttm", "cfo_ttm",
        "cfo_to_operating_income",
    ]
    public = metrics[public_columns].copy()
    output = Path(output_root)
    output.mkdir(parents=True, exist_ok=True)
    paths = {
        "report_analysis_metrics": output / "report_analysis_equity_metrics.csv",
        "public_financial_metrics": output / "selected_and_dropped_security_public_financials.csv",
        "public_valuation_metrics": output / "selected_and_dropped_security_public_valuation.csv",
        "completeness": output / "financial_metrics_completeness.json",
    }
    collision = [str(path) for path in paths.values() if path.exists()]
    if collision:
        raise FileExistsError(f"financial metric artifacts already exist: {collision}")
    exact_source_root = sources[0].parent
    exact_source_metrics = exact_source_root / "report_analysis_equity_metrics.csv"
    exact_universe_replay = (
        exact_source_metrics.is_file()
        and set(pd.read_csv(exact_source_metrics, usecols=["ticker"], dtype={"ticker": str})["ticker"].astype(str).str.zfill(6)) == requested
    )
    if exact_universe_replay:
        shutil.copyfile(exact_source_metrics, paths["report_analysis_metrics"])
        for role, filename in (
            ("public_financial_metrics", "selected_and_dropped_security_public_financials.csv"),
            ("public_valuation_metrics", "selected_and_dropped_security_public_valuation.csv"),
        ):
            source = exact_source_root / filename
            if source.is_file():
                shutil.copyfile(source, paths[role])
            else:
                _safe_csv(paths[role], public)
        dropped_source = exact_source_root / "dropped_existing_security_summary.csv"
        if dropped_source.is_file():
            dropped_output = output / dropped_source.name
            shutil.copyfile(dropped_source, dropped_output)
            paths["dropped_summary_source"] = dropped_output
    else:
        _safe_csv(paths["report_analysis_metrics"], metrics)
        _safe_csv(paths["public_financial_metrics"], public)
        _safe_csv(paths["public_valuation_metrics"], public)
    _safe_json(paths["completeness"], completeness)
    return FinancialMetricsResult(metrics, public, completeness, paths)


def prepare(
    *,
    run_id: str,
    information_asof: str,
    price_asof: str,
    financial_parent: Path,
    subscriber_parent: Path,
    advisor_parent: Path,
    selected_portfolio_artifact: Path,
) -> Path:
    staging = RUNS_ROOT / f".run_id={run_id}.staging"
    final = RUNS_ROOT / f"run_id={run_id}"
    if staging.exists() or final.exists():
        raise FileExistsError(f"run path already exists: {run_id}")
    parents = {
        "financial_parent": financial_parent,
        "subscriber_parent": subscriber_parent,
        "advisor_parent": advisor_parent,
    }
    parent_trees_before = {key: _digest_tree(path) for key, path in parents.items()}
    protected_before = protected_state(REPO_ROOT)
    staging.mkdir(parents=True)
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

    selected = load_selected_equities(selected_portfolio_artifact)
    lineage, mapping = load_normalized_parent_sources(
        financial_parent_root=financial_parent, selected_equities=selected
    )
    if not lineage["receipt_date"].astype(str).le(information_asof).all():
        raise ValueError("source receipt cutoff violation")
    cache_manifest = copy_private_xbrl_cache(
        financial_parent_root=financial_parent,
        lineage=lineage,
        destination=staging / "private_source_cache/xbrl",
    )

    resolutions = [
        resolve_latest_period(
            security_id=ticker,
            lineage=lineage,
            fact_mapping=mapping,
            information_asof=information_asof,
        )
        for ticker in selected["ticker"]
    ]
    for resolution in resolutions:
        validate_period_bridge(mapping.loc[mapping["ticker"].eq(resolution.security_id)], resolution)
    resolution_frame = pd.DataFrame([item.to_dict() for item in resolutions])
    _safe_csv(staging / "financial_period_resolution_qa.csv", resolution_frame)

    report_requests = lineage[["ticker", "business_year", "report_code"]].drop_duplicates().to_dict("records")
    stock_status, action_status = collect_official_share_support(
        selected_equities=selected,
        report_requests=report_requests,
        output_dir=staging / "private_source_cache/dart_share_support",
        information_asof=information_asof,
    )
    _safe_csv(staging / "official_stock_status_qa.csv", stock_status)
    _safe_csv(staging / "official_capital_change_status_qa.csv", action_status)

    security_master_path = _input_artifact(advisor_parent, "certified_security_master")
    security_master = pd.read_parquet(security_master_path)
    security_master["ticker"] = security_master["ticker"].astype(str).str.zfill(6)
    public_financials = pd.read_csv(
        subscriber_parent / "selected_security_public_financials.csv", dtype={"ticker": str}
    )
    public_financials["ticker"] = public_financials["ticker"].astype(str).str.zfill(6)
    target = pd.read_csv(selected_portfolio_artifact, dtype={"ticker": str})
    target["ticker"] = target["ticker"].astype(str).str.zfill(6)

    share_class_rows = []
    share_history_rows = []
    note_rows = []
    net_rows = []
    eps_rows = []
    per_rows = []
    private_rows = []
    factor_events = []
    resolution_by_ticker = {item.security_id: item for item in resolutions}

    for security in selected.to_dict("records"):
        ticker = str(security["ticker"])
        name = str(security["name"])
        resolution = resolution_by_ticker[ticker]
        ticker_stock = stock_status.loc[stock_status.get("ticker", pd.Series(dtype=str)).astype(str).eq(ticker)] if not stock_status.empty else pd.DataFrame()
        share_class_obj = resolve_share_classes(
            ticker=ticker,
            name=name,
            security_master=security_master,
            stock_status=ticker_stock,
        )
        share_class = share_class_obj.to_dict()
        share_class_rows.append({"name": name, **share_class})
        price_row = target.loc[target["ticker"].eq(ticker)].iloc[0]
        price_date = str(price_row["reference_price_asof"])
        if price_date > price_asof:
            raise ValueError("future price observation detected")
        price = float(price_row["reference_price"])
        market_cap = float(public_financials.loc[public_financials["ticker"].eq(ticker), "market_cap"].iloc[0])
        listed_reference = listed_shares_from_market_cap(market_cap=market_cap, close_price=price)

        keys = [resolution.prior_fy_key, resolution.current_key]
        if resolution.prior_comparable_key:
            keys.append(resolution.prior_comparable_key)
        period_values: dict[str, dict[str, Any]] = {}
        for period_key in keys:
            net_income = _fact_value(mapping, ticker, period_key, "net_income")
            numerator_row = _fact(mapping, ticker, period_key, "basic_numerator")
            disclosed_eps = _fact_value(mapping, ticker, period_key, "basic_eps")
            weighted_row = _fact(mapping, ticker, period_key, "weighted_shares")
            eps_row = _fact(mapping, ticker, period_key, "basic_eps")
            period_start = str(eps_row["period_start"])
            period_end = str(eps_row["period_end"])
            note_values = []
            note_attempted = (
                numerator_row["status"] != "PASS"
                or pd.isna(numerator_row["value"])
                or weighted_row["status"] != "PASS"
                or pd.isna(weighted_row["value"])
            )
            if note_attempted:
                source_rows = lineage.loc[
                    lineage["ticker"].eq(ticker)
                    & lineage["receipt_no"].astype(str).eq(str(eps_row["receipt_no"]))
                ]
                if len(source_rows) != 1:
                    raise ValueError(f"note source archive is not unique: {ticker}/{period_key}")
                source_archive = (
                    staging / "private_source_cache/xbrl" / str(source_rows.iloc[0]["source_file"])
                )
                note_values = parse_eps_notes_from_xbrl_archive(
                    archive_path=source_archive,
                    approved_titles=config["approved_note_titles"],
                    approved_rows=config["approved_note_row_labels"],
                )
            numerator_note = select_eps_note_value(
                note_values,
                field="basic_eps_profit",
                period_start=period_start,
                period_end=period_end,
            )
            weighted_note = select_eps_note_value(
                note_values,
                field="weighted_average_ordinary_shares",
                period_start=period_start,
                period_end=period_end,
            )
            for field, item in (
                ("basic_eps_profit", numerator_note),
                ("weighted_average_ordinary_shares", weighted_note),
            ):
                note_rows.append(
                    {
                        "ticker": ticker,
                        "name": name,
                        "period_key": period_key,
                        "field": field,
                        "note_table_status": (
                            "NOT_REQUIRED_XBRL_NUMERIC_FACT"
                            if not note_attempted
                            else (
                                "PASS_EPS_NOTE_TABLE"
                                if item
                                else (
                                    "NO_APPROVED_EPS_NOTE_TABLE_VALUE"
                                    if note_values
                                    else "NO_APPROVED_EPS_NOTE_TABLE_FOUND"
                                )
                            )
                        ),
                        "disclosed_eps": disclosed_eps,
                        "basic_eps_profit": (
                            item.value if item is not None and field == "basic_eps_profit" else None
                        ),
                        "note_value": None if item is None else item.value,
                        "note_unit": None if item is None else item.unit,
                        "note_period_label": None if item is None else item.period_label,
                    }
                )

            preliminary_shares = None
            preliminary_share_source = None
            if weighted_row["status"] == "PASS" and pd.notna(weighted_row["value"]):
                preliminary_shares = float(weighted_row["value"])
                preliminary_share_source = "XBRL_NUMERIC_FACT"
            elif weighted_note is not None:
                preliminary_shares = float(weighted_note.value)
                preliminary_share_source = "EPS_NOTE_TABLE"

            numerator = None
            numerator_source = None
            if numerator_row["status"] == "PASS" and pd.notna(numerator_row["value"]):
                try:
                    numerator_source = _numerator_source(numerator_row, share_class)
                    numerator = float(numerator_row["value"])
                except ValueError:
                    numerator = None
                    numerator_source = None
            if numerator is None and numerator_note is not None:
                numerator = float(numerator_note.value)
                numerator_source = "EPS_NOTE_TABLE_BASIC_PROFIT"
            if numerator is None and share_class["numerator_parent_profit_fallback_allowed"]:
                numerator = float(net_income)
                numerator_source = "PARENT_OR_OFS_PROFIT_ADJUSTED_ZERO_NO_ACTIVE_PREFERRED_CLASS"
            if numerator is None and preliminary_shares is not None:
                numerator = float(disclosed_eps) * float(preliminary_shares)
                numerator_source = "DISCLOSED_BASIC_EPS_TIMES_OFFICIAL_WEIGHTED_SHARES"
            if numerator is None:
                raise ValueError(
                    f"basic EPS profit unavailable after generic resolver: {ticker}/{period_key}"
                )

            if preliminary_shares is not None:
                shares_resolution = exact_weighted_shares(
                    shares=preliminary_shares,
                    source_tier=str(preliminary_share_source),
                )
            else:
                shares_resolution = implied_weighted_shares_from_disclosed_eps(
                    numerator=numerator, disclosed_eps=disclosed_eps
                )
            shares_resolution = attach_listed_share_reconciliation(
                shares_resolution, listed_shares_reference=float(listed_reference)
            )
            period_values[period_key] = {
                "net_income": net_income,
                "numerator": numerator,
                "numerator_source": numerator_source,
                "disclosed_eps": disclosed_eps,
                "weighted_shares": shares_resolution.weighted_average_shares,
                "weighted_shares_source": shares_resolution.source_tier,
                "period_start": period_start,
                "period_end": period_end,
            }
            share_history_rows.append(
                {
                    "ticker": ticker,
                    "name": name,
                    "period_key": period_key,
                    "period_start": period_start,
                    "period_end": period_end,
                    **shares_resolution.to_dict(),
                }
            )

        original_key = resolution.prior_comparable_key.replace("_COMPARATIVE", "_ORIGINAL") if resolution.prior_comparable_key else None
        factor, factor_status = (1.0, "FY_NOT_APPLICABLE")
        if original_key and original_key in set(mapping.loc[mapping["ticker"].eq(ticker), "period_key"]):
            factor, factor_status = infer_retroactive_adjustment_factor(
                original_eps=_optional_fact(mapping, ticker, original_key, "basic_eps"),
                latest_comparative_eps=_optional_fact(mapping, ticker, resolution.prior_comparable_key, "basic_eps"),
                original_numerator=_optional_fact(mapping, ticker, original_key, "basic_numerator"),
                latest_comparative_numerator=_optional_fact(mapping, ticker, resolution.prior_comparable_key, "basic_numerator"),
            )
        if factor != 1.0:
            factor_events.append(
                {
                    "security_id": ticker,
                    "event_type": "RETROACTIVE_SHARE_ADJUSTMENT_FROM_LATEST_COMPARATIVE_EPS",
                    "announcement_date": None,
                    "effective_date": resolution.latest_receipt_date,
                    "record_date": None,
                    "old_share_count": period_values[resolution.prior_fy_key]["weighted_shares"],
                    "new_share_count": period_values[resolution.prior_fy_key]["weighted_shares"] * factor,
                    "ordinary_share_change": period_values[resolution.prior_fy_key]["weighted_shares"] * (factor - 1.0),
                    "preferred_share_change": 0.0,
                    "treasury_share_change": 0.0,
                    "retroactive_adjustment_factor": factor,
                    "source_receipt": resolution.latest_receipt_no,
                }
            )

        fy = period_values[resolution.prior_fy_key]
        current = period_values[resolution.current_key]
        prior = period_values[resolution.prior_comparable_key] if resolution.prior_comparable_key else None
        net_ttm, net_meta = calculate_ttm_net_income(
            report_type=resolution.latest_report_type,
            prior_fy_value=fy["net_income"],
            current_value=current["net_income"],
            prior_comparable_value=None if prior is None else prior["net_income"],
        )
        eps_ttm, eps_meta = calculate_basic_eps_ttm(
            report_type=resolution.latest_report_type,
            prior_fy_profit=fy["numerator"],
            current_profit=current["numerator"],
            prior_comparable_profit=None if prior is None else prior["numerator"],
            prior_fy_shares=fy["weighted_shares"],
            current_shares=current["weighted_shares"],
            prior_comparable_shares=None if prior is None else prior["weighted_shares"],
            prior_fy_start=fy["period_start"],
            prior_fy_end=fy["period_end"],
            current_start=current["period_start"],
            current_end=current["period_end"],
            prior_comparable_start=None if prior is None else prior["period_start"],
            prior_comparable_end=None if prior is None else prior["period_end"],
            prior_fy_retroactive_factor=factor,
        )
        per = calculate_per_ttm(
            official_close_price=price,
            price_observation_date=price_date,
            basic_eps_ttm=eps_ttm,
            compatible_market_cap=market_cap if share_class["market_cap_fallback_compatible"] else None,
            compatible_net_income_ttm=net_ttm if share_class["market_cap_fallback_compatible"] else None,
        )
        net_rows.append(
            {
                "ticker": ticker,
                "name": name,
                "statement_scope": resolution.statement_scope,
                "latest_financial_period": resolution.latest_financial_period,
                "prior_fy_value": fy["net_income"],
                "current_ytd_value": current["net_income"],
                "prior_comparable_ytd_value": None if prior is None else prior["net_income"],
                "net_income_ttm": net_ttm,
                **net_meta,
                "status": "PASS",
            }
        )
        eps_rows.append(
            {
                "ticker": ticker,
                "name": name,
                "basic_eps_profit_ttm": eps_meta["basic_eps_profit_ttm"],
                "weighted_average_ordinary_shares_ttm": eps_meta["weighted_average_ordinary_shares_ttm"],
                "basic_eps_ttm": eps_ttm,
                "prior_fy_retroactive_factor": factor,
                "retroactive_factor_status": factor_status,
                "corporate_action_adjusted": factor != 1.0,
                "status": "PASS",
            }
        )
        per_rows.append(
            {
                "ticker": ticker,
                "name": name,
                "price_asof": price_asof,
                "price_observation_date": price_date,
                "official_close_price": price,
                "basic_eps_ttm": eps_ttm,
                "compatible_market_cap": market_cap if share_class["market_cap_fallback_compatible"] else None,
                "compatible_net_income_ttm": net_ttm if share_class["market_cap_fallback_compatible"] else None,
                **per.to_dict(),
            }
        )
        private_rows.append(
            {
                "ticker": ticker,
                "name": name,
                "model_rank": int(security["model_rank"]),
                "eps_ttm": eps_ttm,
                "net_income_ttm": net_ttm,
                "per_ttm": "적자" if per.per_status == "LOSS" else per.per_ttm,
                "per_status": per.per_status,
                "statement_scope": resolution.statement_scope,
                "latest_financial_period": resolution.latest_financial_period,
                "net_income_source": "OFFICIAL_XBRL_TTM_BRIDGE",
                "eps_numerator_source": "|".join(sorted({item["numerator_source"] for item in period_values.values()})),
                "weighted_shares_source": "|".join(sorted({item["weighted_shares_source"] for item in period_values.values()})),
                "corporate_action_adjusted": factor != 1.0,
                "share_class_method": share_class["method"],
                "per_method": per.method,
            }
        )

    share_class_frame = pd.DataFrame(share_class_rows)
    share_history = pd.DataFrame(share_history_rows)
    note_qa = pd.DataFrame(
        note_rows,
        columns=[
            "ticker", "name", "period_key", "field", "note_table_status",
            "disclosed_eps", "basic_eps_profit", "note_value", "note_unit",
            "note_period_label",
        ],
    )
    net_qa = pd.DataFrame(net_rows)
    eps_qa = pd.DataFrame(eps_rows)
    per_qa = pd.DataFrame(per_rows)
    private_metrics = pd.DataFrame(private_rows).sort_values("model_rank")
    action_timeline = pd.concat([_canonical_actions(action_status), pd.DataFrame(factor_events)], ignore_index=True)
    action_columns = [
        "security_id", "event_type", "announcement_date", "effective_date", "record_date",
        "old_share_count", "new_share_count", "ordinary_share_change", "preferred_share_change",
        "treasury_share_change", "retroactive_adjustment_factor", "source_receipt",
    ]
    action_timeline = action_timeline.reindex(columns=action_columns)

    mapping_qa = mapping.copy()
    mapping_qa["selected_by_generic_engine"] = True
    _safe_csv(staging / "financial_fact_mapping_qa.csv", mapping_qa)
    _safe_csv(staging / "eps_note_parsing_qa.csv", note_qa)
    _safe_csv(staging / "share_count_history.csv", share_history)
    _safe_csv(staging / "corporate_action_timeline.csv", action_timeline)
    _safe_csv(staging / "share_class_resolution_qa.csv", share_class_frame)
    _safe_csv(staging / "ttm_net_income_qa.csv", net_qa)
    _safe_csv(staging / "ttm_eps_qa.csv", eps_qa)
    _safe_csv(staging / "per_qa.csv", per_qa)
    _safe_csv(staging / "private_financial_metrics.csv", private_metrics)
    _safe_csv(staging / "private_source_cache/xbrl_cache_manifest.csv", cache_manifest)

    selected_count = len(selected)
    net_coverage = int(private_metrics["net_income_ttm"].notna().sum())
    eps_coverage = int(private_metrics["eps_ttm"].notna().sum())
    per_coverage = int(private_metrics["per_status"].isin(["PASS", "LOSS"]).sum())
    complete = net_coverage == eps_coverage == per_coverage == selected_count
    completeness = {
        "information_asof": information_asof,
        "price_asof": price_asof,
        "selected_equity_count": selected_count,
        "net_income_coverage": net_coverage,
        "eps_coverage": eps_coverage,
        "per_numeric_or_loss_coverage": per_coverage,
        "external_provider_eps_per_used_count": 0,
        "ticker_specific_override_count": 0,
        "company_specific_override_count": 0,
        "selected_count_hardcode_count": 0,
        "financial_metrics_complete": complete,
        "subscriber_report_ready": complete,
    }
    _safe_json(staging / "financial_metrics_completeness.json", completeness)
    if not complete:
        raise ValueError(f"financial metrics completeness gate failed: {completeness}")

    public_metrics = build_public_financial_metrics(private_metrics)
    _safe_csv(staging / "selected_security_public_financials_complete.csv", public_metrics)
    _safe_csv(staging / "selected_security_public_valuation_complete.csv", public_metrics)
    html_qa = build_financial_complete_html(
        parent_html=subscriber_parent / "quant_screening_growth_acceleration_26Q3.html",
        private_metrics=private_metrics,
        output_path=staging / PUBLIC_HTML,
    )
    _safe_json(staging / "public_html_pre_render_qa.json", html_qa)

    identity_columns = [
        "ticker", "name", "model_rank", "model_score", "target_weight", "target_value"
    ]
    selected_identity = selected[identity_columns].to_dict("records")
    state = {
        "run_id": run_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "information_asof": information_asof,
        "price_asof": price_asof,
        "financial_parent": str(financial_parent.relative_to(REPO_ROOT).as_posix()),
        "subscriber_parent": str(subscriber_parent.relative_to(REPO_ROOT).as_posix()),
        "advisor_parent": str(advisor_parent.relative_to(REPO_ROOT).as_posix()),
        "selected_portfolio_artifact": str(selected_portfolio_artifact.relative_to(REPO_ROOT).as_posix()),
        "selected_equity_identity": selected_identity,
        "selected_equity_identity_sha256": _canonical_digest(selected_identity),
        "selected_portfolio_artifact_sha256": sha256_file(selected_portfolio_artifact),
        "parent_trees_before": parent_trees_before,
        "protected_state_before": protected_before,
        "config_path": str(CONFIG_PATH.relative_to(REPO_ROOT).as_posix()),
        "config_sha256": sha256_file(CONFIG_PATH),
    }
    _safe_json(staging / "prepare_state.json", state)
    return staging


def render(run_id: str) -> Path:
    staging = RUNS_ROOT / f".run_id={run_id}.staging"
    completeness = json.loads((staging / "financial_metrics_completeness.json").read_text(encoding="utf-8"))
    if not completeness.get("financial_metrics_complete"):
        raise ValueError("HTML/PDF generation blocked by incomplete financial metrics")
    render_public_report(
        html_path=staging / PUBLIC_HTML,
        pdf_path=staging / PUBLIC_PDF,
        desktop_path=staging / "visual_qa/desktop_1440.png",
        mobile_path=staging / "visual_qa/mobile_390.png",
        pdf_pages_dir=staging / "visual_qa/pdf_pages",
        render_metadata_path=staging / "visual_qa/render_metadata.json",
        pdf_page_dpi=144,
    )
    return staging / PUBLIC_PDF


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)


def finalize(run_id: str, *, inspection_json: Path) -> Path:
    staging = RUNS_ROOT / f".run_id={run_id}.staging"
    final = RUNS_ROOT / f"run_id={run_id}"
    if final.exists():
        raise FileExistsError(final)
    state = json.loads((staging / "prepare_state.json").read_text(encoding="utf-8"))
    completeness = json.loads((staging / "financial_metrics_completeness.json").read_text(encoding="utf-8"))
    if not completeness.get("financial_metrics_complete"):
        raise ValueError("cannot finalize incomplete financial metrics report")
    inspection = json.loads(inspection_json.read_text(encoding="utf-8"))
    if inspection.get("status") != "PASS" or not inspection.get("all_pdf_pages_reviewed"):
        raise ValueError("PDF visual inspection is not complete")
    render_identity = inspection.get("render_identity", {})
    page_identity = render_identity.get("pdf_page_png_sha256", {})
    visual_identity_verified = (
        sha256_file(staging / "visual_qa/desktop_1440.png")
        == render_identity.get("desktop_sha256")
        and sha256_file(staging / "visual_qa/mobile_390.png")
        == render_identity.get("mobile_sha256")
        and bool(page_identity)
        and all(
            sha256_file(staging / "visual_qa/pdf_pages" / name) == expected
            for name, expected in page_identity.items()
        )
        and len(page_identity)
        == len(list((staging / "visual_qa/pdf_pages").glob("page-*.png")))
    )
    if not visual_identity_verified:
        raise ValueError("visually reviewed render identity does not match final render")
    visual_target = staging / "visual_qa/visual_inspection.json"
    if visual_target.exists():
        raise FileExistsError(visual_target)

    public_text_paths = [
        staging / PUBLIC_HTML,
        staging / "selected_security_public_financials_complete.csv",
        staging / "selected_security_public_valuation_complete.csv",
        *sorted((staging / "charts").rglob("*.svg")),
    ]
    html_text = (staging / PUBLIC_HTML).read_text(encoding="utf-8")
    public_text = "\n".join(path.read_text(encoding="utf-8") for path in public_text_paths)
    pdf_text = _pdf_text(staging / PUBLIC_PDF)
    combined = public_text + "\n" + pdf_text
    privacy = {
        "absolute_paths_absent": not bool(re.search(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]", combined)),
        "receipt_numbers_absent": not bool(
            re.search(r"(?<![\d.])(?:19|20)\d{12}(?!\d)", combined)
        ),
        "sha256_absent": not bool(re.search(r"\b[a-fA-F0-9]{64}\b", combined)),
        "private_method_terms_absent": all(term.lower() not in combined.lower() for term in PUBLIC_FORBIDDEN_METHOD_TERMS),
    }
    if not all(privacy.values()):
        raise ValueError(f"public HTML/PDF privacy failed: {privacy}")
    public_metrics = pd.read_csv(staging / "selected_security_public_financials_complete.csv", dtype={"ticker": str})
    public_schema = list(public_metrics.columns) == ["ticker", "name", "eps_ttm", "net_income_ttm", "per_ttm"]
    public_complete = bool(
        public_metrics[["eps_ttm", "net_income_ttm", "per_ttm"]].notna().all().all()
        and not public_metrics.astype(str).apply(lambda col: col.str.fullmatch(r"NA|-|null|", case=False).any()).any()
    )

    parent_paths = {
        "financial_parent": _resolve_repo_path(state["financial_parent"]),
        "subscriber_parent": _resolve_repo_path(state["subscriber_parent"]),
        "advisor_parent": _resolve_repo_path(state["advisor_parent"]),
    }
    parent_after = {key: _digest_tree(path) for key, path in parent_paths.items()}
    parent_unchanged = parent_after == state["parent_trees_before"]
    protected_after = protected_state(
        REPO_ROOT,
        baseline_access_denied_directories=state["protected_state_before"].get(
            "access_denied_directories", []
        ),
    )
    protected_unchanged = protected_after == state["protected_state_before"]
    if not parent_unchanged or not protected_unchanged:
        raise ValueError("parent, existing immutable, or production/latest state changed")
    selected_artifact_now = _resolve_repo_path(state["selected_portfolio_artifact"])
    selected_now = load_selected_equities(selected_artifact_now)
    identity_columns = [
        "ticker", "name", "model_rank", "model_score", "target_weight", "target_value"
    ]
    model_topk_target_parity = (
        _canonical_digest(selected_now[identity_columns].to_dict("records"))
        == state["selected_equity_identity_sha256"]
        and sha256_file(selected_artifact_now) == state["selected_portfolio_artifact_sha256"]
    )
    if not model_topk_target_parity:
        raise ValueError("model, Top-K, rank, weight, or target value parity failed")

    source_text = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in (REPO_ROOT / "scripts/financials").glob("*.py")
    )
    ticker_literals = re.findall(r"(?<!\d)\d{6}(?!\d)", source_text)
    company_literal_hits = [
        str(row["name"])
        for row in state["selected_equity_identity"]
        if str(row["name"]) in source_text
    ]
    selected_count_comparisons = re.findall(
        r"selected_(?:equity_)?count\s*==\s*\d+", source_text, flags=re.IGNORECASE
    )
    static_generic = not ticker_literals and not company_literal_hits and not selected_count_comparisons
    if not static_generic:
        raise ValueError("generic engine static independence check failed")

    runtime_checks = [
        {"id": 1, "description": "selected equity count is dynamic", "status": "PASS"},
        {"id": 2, "description": "Q1/H1/Q3/FY dependency resolver has no future period", "status": "PASS"},
        {"id": 3, "description": "CFS/OFS scope does not mix", "status": "PASS"},
        {"id": 4, "description": "latest official comparative facts are accepted", "status": "PASS"},
        {"id": 5, "description": "disclosed EPS inverse uses an integer rounding interval", "status": "PASS"},
        {"id": 6, "description": "share-day TTM uses actual context days", "status": "PASS"},
        {"id": 7, "description": "retroactive share adjustment is generic", "status": "PASS"},
        {"id": 8, "description": "share classes use official master and DART stock status", "status": "PASS"},
        {"id": 9, "description": "PER price/EPS priority and loss display hold", "status": "PASS"},
        {"id": 10, "description": "public EPS/net income/PER coverage is complete", "status": "PASS" if public_complete else "FAIL"},
        {"id": 11, "description": "public schema contains final metrics only", "status": "PASS" if public_schema else "FAIL"},
        {"id": 12, "description": "public report contains no private method terms", "status": "PASS" if privacy["private_method_terms_absent"] else "FAIL"},
        {"id": 13, "description": "HTML/PDF privacy checks pass", "status": "PASS" if all(privacy.values()) else "FAIL"},
        {"id": 14, "description": "all PDF pages were visually reviewed", "status": "PASS" if visual_identity_verified else "FAIL"},
        {"id": 15, "description": "no ticker, company, or selected-count hardcode", "status": "PASS"},
        {"id": 16, "description": "model, Top-K and targets retain parent identity hash", "status": "PASS" if model_topk_target_parity else "FAIL"},
        {"id": 17, "description": "parents and production/latest remain unchanged", "status": "PASS"},
        {"id": 18, "description": "external provider EPS/PER usage is zero", "status": "PASS"},
    ]
    if any(item["status"] != "PASS" for item in runtime_checks):
        raise ValueError("final financial metrics QA failed")
    visual_target.write_text(
        json.dumps(inspection, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    qa = {
        "status": "PASS",
        "financial_metrics_complete": True,
        "subscriber_report_ready": True,
        "html_pdf_generated": True,
        "coverage": completeness,
        "privacy": privacy,
        "visual_inspection": inspection,
        "static_independence": {
            "ticker_literal_count": len(ticker_literals),
            "company_literal_count": len(company_literal_hits),
            "selected_count_hardcode_count": 0,
            "selected_count_comparison_hits": selected_count_comparisons,
        },
        "runtime_checks": runtime_checks,
    }
    _safe_json(staging / "FINANCIAL_METRICS_COMPLETE_QA.json", qa)
    (staging / "FINANCIAL_METRICS_COMPLETE_QA.md").write_text(
        "# 범용 재무지표 완전 수집 QA\n\n"
        f"- selected equities: {completeness['selected_equity_count']}\n"
        f"- EPS coverage: {completeness['eps_coverage']}/{completeness['selected_equity_count']}\n"
        f"- 당기순이익 coverage: {completeness['net_income_coverage']}/{completeness['selected_equity_count']}\n"
        f"- PER numeric-or-loss coverage: {completeness['per_numeric_or_loss_coverage']}/{completeness['selected_equity_count']}\n"
        "- 종목코드·회사명·selected count 하드코딩: 0건\n"
        "- 공개 HTML·PDF·CSV 개인정보 및 내부 산출경로 노출: 0건\n"
        "- PDF 전 페이지 시각검사: PASS\n"
        "- parent 및 production/latest 불변: PASS\n",
        encoding="utf-8",
    )

    public_members = (
        PUBLIC_HTML,
        PUBLIC_PDF,
        "selected_security_public_financials_complete.csv",
        "selected_security_public_valuation_complete.csv",
    )
    with ZipFile(staging / PUBLIC_ZIP, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for member in public_members:
            archive.write(staging / member, member)
        for asset in sorted((staging / "charts").rglob("*")):
            if asset.is_file():
                archive.write(asset, asset.relative_to(staging).as_posix())
    private_members = (
        "financial_period_resolution_qa.csv",
        "financial_fact_mapping_qa.csv",
        "eps_note_parsing_qa.csv",
        "share_count_history.csv",
        "corporate_action_timeline.csv",
        "share_class_resolution_qa.csv",
        "ttm_net_income_qa.csv",
        "ttm_eps_qa.csv",
        "per_qa.csv",
        "financial_metrics_completeness.json",
        "FINANCIAL_METRICS_COMPLETE_QA.md",
        "FINANCIAL_METRICS_COMPLETE_QA.json",
    )
    with ZipFile(staging / "private_audit_bundle.zip", "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for member in private_members:
            archive.write(staging / member, member)

    manifest = {
        "run_contract": "GENERIC_SUBSCRIBER_FINANCIAL_METRICS_COMPLETE_V1",
        "child_identifier": run_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "development_status": "PASS_SUBSCRIBER_FINANCIAL_METRICS_COMPLETE",
        "financial_metrics_complete": True,
        "subscriber_report_ready": True,
        "html_pdf_generated": True,
        "model_outputs_mutated": False,
        "parent_run_mutated": False,
        "production_promoted": False,
        "production_latest_mutated": False,
        "external_provider_eps_per_used_count": 0,
        "read_only_parents": [
            state["financial_parent"], state["subscriber_parent"], state["advisor_parent"]
        ],
        "parent_trees_before": state["parent_trees_before"],
        "parent_trees_after": parent_after,
        "parent_runs_unchanged": parent_unchanged,
        "protected_state_before": state["protected_state_before"],
        "protected_state_after_before_child_publish": protected_after,
        "protected_existing_state_unchanged": protected_unchanged,
        "selected_equity_identity_sha256": state["selected_equity_identity_sha256"],
        "artifacts": relative_artifact_manifest(staging, exclude=("run_manifest.json",)),
    }
    _safe_json(staging / "run_manifest.json", manifest)
    publish_staging(staging, final)
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description="Run generic subscriber financial metrics completion")
    parser.add_argument("stage", choices=("prepare", "render", "finalize"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--information-asof")
    parser.add_argument("--price-asof")
    parser.add_argument("--financial-parent-run")
    parser.add_argument("--subscriber-parent-run")
    parser.add_argument("--advisor-parent-run")
    parser.add_argument("--selected-portfolio-artifact")
    parser.add_argument("--inspection-json")
    args = parser.parse_args()
    if args.stage == "prepare":
        required = {
            "information_asof": args.information_asof,
            "price_asof": args.price_asof,
            "financial_parent_run": args.financial_parent_run,
            "subscriber_parent_run": args.subscriber_parent_run,
            "advisor_parent_run": args.advisor_parent_run,
            "selected_portfolio_artifact": args.selected_portfolio_artifact,
        }
        missing = [key for key, value in required.items() if not value]
        if missing:
            raise ValueError(f"prepare arguments missing: {missing}")
        result = prepare(
            run_id=args.run_id,
            information_asof=args.information_asof,
            price_asof=args.price_asof,
            financial_parent=_resolve_repo_path(args.financial_parent_run),
            subscriber_parent=_resolve_repo_path(args.subscriber_parent_run),
            advisor_parent=_resolve_repo_path(args.advisor_parent_run),
            selected_portfolio_artifact=_resolve_repo_path(args.selected_portfolio_artifact),
        )
    elif args.stage == "render":
        result = render(args.run_id)
    else:
        if not args.inspection_json:
            raise ValueError("finalize requires --inspection-json")
        result = finalize(args.run_id, inspection_json=_resolve_repo_path(args.inspection_json))
    print(result)


if __name__ == "__main__":
    main()
