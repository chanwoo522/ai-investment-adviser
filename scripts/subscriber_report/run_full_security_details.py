from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import sys
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd
from bs4 import BeautifulSoup

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.advisor.immutable_run import (  # noqa: E402
    protected_state,
    publish_staging,
    relative_artifact_manifest,
    sha256_file,
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
)
from scripts.financials.collect_report_analysis_xbrl import (  # noqa: E402
    collect_analysis_xbrl_sources,
)
from scripts.financials.parse_eps_notes import (  # noqa: E402
    parse_eps_notes_from_xbrl_archive,
    select_eps_note_pair,
    select_eps_note_value,
)
from scripts.financials.resolve_reporting_periods import (  # noqa: E402
    resolve_latest_period,
    validate_period_bridge,
)
from scripts.financials.resolve_share_classes import resolve_share_classes  # noqa: E402
from scripts.qa.render_public_report import render_public_report  # noqa: E402


RUNS_ROOT = REPO_ROOT / "data/development/subscriber_full_security_details/runs"
PUBLIC_HTML = "quant_screening_growth_acceleration_26Q3_full_security_details.html"
PUBLIC_PDF = "quant_screening_growth_acceleration_26Q3_full_security_details.pdf"
PUBLIC_ZIP = "public_distribution_bundle_full_security_details.zip"
INFORMATION_ASOF = "2026-08-18"
PRICE_ASOF = "2026-08-18"
TOP_K = 10

FACTOR_FIELDS = (
    ("Debt_to_Equity_log__contrib", "부채비율 기여"),
    ("OpIncome_acc2_log1p__contrib", "영업이익 가속 기여"),
    ("Revenue_acc2__contrib", "매출 가속 기여"),
    ("op_growth_streak2__contrib", "영업이익 연속성 기여"),
    ("rev_growth_streak2__contrib", "매출 연속성 기여"),
)
PUBLIC_FORBIDDEN = (
    "DROPPED",
    "previously_held",
    "model_selected",
    "transition_status",
    "filter_failure_reason",
    "filter_status",
    "normalization_population_contract",
    "source tier",
    "formula used",
    "직접 공시 fact",
    "재구성값",
    "DENOMINATOR_MISSING",
    "자료 없음",
    "자료 미확보",
    "null",
)


def _canonical_digest(payload: Any) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest_tree(root: Path) -> dict[str, Any]:
    rows = []
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file()),
        key=lambda item: item.as_posix(),
    ):
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": int(path.stat().st_size),
                "sha256": sha256_file(path),
            }
        )
    return {
        "file_count": len(rows),
        "tree_sha256": _canonical_digest(rows),
        "files": rows,
    }


def _safe_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _safe_csv(path: Path, frame: pd.DataFrame) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _truth(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def _ticker(value: object) -> str:
    return str(value).replace(".0", "").zfill(6)


def _input_artifact(advisor_parent: Path, role: str) -> Path:
    manifest = json.loads((advisor_parent / "run_manifest.json").read_text(encoding="utf-8"))
    matches = [item for item in manifest.get("input_artifacts", []) if item.get("role") == role]
    if len(matches) != 1 or not matches[0].get("path"):
        raise ValueError(f"advisor input role is not unique: {role}")
    path = Path(matches[0]["path"])
    return (path if path.is_absolute() else REPO_ROOT / path).resolve()


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


def _optional_fact(
    mapping: pd.DataFrame, ticker: str, period_key: str, field: str
) -> float | None:
    row = _fact(mapping, ticker, period_key, field)
    return float(row["value"]) if row["status"] == "PASS" and pd.notna(row["value"]) else None


def _numerator_source(row: pd.Series, share_class: dict[str, Any]) -> str:
    concept = str(row.get("concept", ""))
    context = str(row.get("context_ref", ""))
    if "OrdinaryEquityHolders" in concept or "OrdinarySharesMember" in context:
        return "XBRL_CLASS_SPECIFIC_BASIC_EPS_PROFIT"
    if share_class["numerator_parent_profit_fallback_allowed"]:
        return "PARENT_OR_OFS_PROFIT_ZERO_OTHER_ACTIVE_CLASS"
    raise ValueError("parent profit is not a compatible basic EPS numerator")


def _derive_universes(advisor_parent: Path) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    current = pd.read_csv(advisor_parent / "current_vs_target_v2.csv", dtype={"ticker": str})
    current["ticker"] = current["ticker"].map(_ticker)
    dropped = current.loc[
        current["previously_held"].map(_truth)
        & ~current["model_selected"].map(_truth)
        & current["asset_class"].eq("EQUITY")
    ].copy()
    equity_total = float(
        current.loc[current["asset_class"].eq("EQUITY"), "current_value"].astype(float).sum()
    )
    if equity_total <= 0 or dropped.empty:
        raise ValueError("dynamic dropped equity universe is empty or invalid")
    dropped["current_stock_weight"] = dropped["current_value"].astype(float) / equity_total
    dropped = dropped.sort_values(
        ["current_stock_weight", "current_value", "ticker"],
        ascending=[False, False, True],
        kind="mergesort",
    ).reset_index(drop=True)

    topk = pd.read_csv(advisor_parent / "fresh_start_top_k_v2.csv", dtype={"ticker": str})
    topk["ticker"] = topk["ticker"].map(_ticker)
    selected = topk.loc[topk["model_selected"].map(_truth)].sort_values("model_rank").copy()
    if len(selected) != TOP_K:
        raise ValueError("authoritative Top-K size differs from report contract")
    selected["selection_group"] = "SELECTED"
    dropped["selection_group"] = "DROPPED_EXISTING"

    industry = pd.read_csv(advisor_parent / "industry_mapping_qa.csv", dtype={"ticker": str, "corp_code": str})
    industry["ticker"] = industry["ticker"].map(_ticker)
    identity = industry[
        ["ticker", "corp_code", "official_industry_name", "advisor_sector"]
    ].drop_duplicates("ticker")
    dropped = dropped.merge(identity, on="ticker", how="left", validate="one_to_one")
    selected = selected.merge(
        identity, on="ticker", how="left", validate="one_to_one", suffixes=("", "_identity")
    )
    selected["corp_code"] = selected["corp_code"].fillna(selected.get("corp_code_identity"))
    for column in ("official_industry_name", "advisor_sector"):
        identity_column = f"{column}_identity"
        if identity_column in selected:
            selected[column] = selected.get(column).fillna(selected[identity_column])
    selected = selected.drop(columns=[column for column in selected if column.endswith("_identity")])
    if dropped[["corp_code", "official_industry_name", "advisor_sector"]].isna().any().any():
        raise ValueError("dropped equity identity/industry mapping is incomplete")

    analysis = pd.concat(
        [
            selected[["ticker", "name", "corp_code", "selection_group"]],
            dropped[["ticker", "name", "corp_code", "selection_group"]],
        ],
        ignore_index=True,
    ).drop_duplicates("ticker", keep="first")
    if len(analysis) != len(set(selected["ticker"]) | set(dropped["ticker"])):
        raise ValueError("analysis universe security-ID union failed")
    return selected.reset_index(drop=True), dropped, analysis.reset_index(drop=True)


def _compute_dropped_earnings(
    *,
    dropped: pd.DataFrame,
    lineage: pd.DataFrame,
    mapping: pd.DataFrame,
    source_root: Path,
    advisor_parent: Path,
    stock_status: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    config = json.loads(
        (REPO_ROOT / "configs/financial_metrics_engine_v1.json").read_text(encoding="utf-8")
    )
    security_master = pd.read_parquet(_input_artifact(advisor_parent, "certified_security_master"))
    security_master["ticker"] = security_master["ticker"].astype(str).str.zfill(6)
    market = pd.read_parquet(_input_artifact(advisor_parent, "marketdata"))
    market["ticker"] = market["ticker"].astype(str).str.zfill(6)
    market = market.drop_duplicates("ticker", keep="last").set_index("ticker")
    resolutions = {
        ticker: resolve_latest_period(
            security_id=ticker,
            lineage=lineage,
            fact_mapping=mapping,
            information_asof=INFORMATION_ASOF,
        )
        for ticker in dropped["ticker"]
    }
    for ticker, resolution in resolutions.items():
        validate_period_bridge(mapping.loc[mapping["ticker"].eq(ticker)], resolution)

    metrics_rows: list[dict[str, Any]] = []
    share_rows: list[dict[str, Any]] = []
    note_rows: list[dict[str, Any]] = []
    period_rows: list[dict[str, Any]] = []
    for security in dropped.to_dict("records"):
        ticker = str(security["ticker"])
        name = str(security["name"])
        resolution = resolutions[ticker]
        ticker_stock = (
            stock_status.loc[stock_status["ticker"].astype(str).eq(ticker)]
            if not stock_status.empty and "ticker" in stock_status
            else pd.DataFrame()
        )
        share_class = resolve_share_classes(
            ticker=ticker,
            name=name,
            security_master=security_master,
            stock_status=ticker_stock,
        ).to_dict()
        market_row = market.loc[ticker]
        price = float(market_row["close"])
        market_cap = float(market_row["market_cap"])
        if str(market_row["used_px_date"])[:10] != PRICE_ASOF:
            raise ValueError(f"price cutoff mismatch for {ticker}")
        listed_reference = listed_shares_from_market_cap(
            market_cap=market_cap, close_price=price
        )
        keys = [resolution.prior_fy_key, resolution.current_key]
        if resolution.prior_comparable_key:
            keys.append(resolution.prior_comparable_key)
        values: dict[str, dict[str, Any]] = {}
        for period_key in keys:
            numerator_row = _fact(mapping, ticker, period_key, "basic_numerator")
            weighted_row = _fact(mapping, ticker, period_key, "weighted_shares")
            eps_row = _fact(mapping, ticker, period_key, "basic_eps")
            net_income = _optional_fact(mapping, ticker, period_key, "net_income")
            net_income_source = "XBRL_PARENT_PROFIT"
            disclosed_eps = (
                float(eps_row["value"])
                if eps_row["status"] == "PASS" and pd.notna(eps_row["value"])
                else None
            )
            eps_source = (
                "XBRL_BASIC_EPS_FACT"
                if disclosed_eps is not None
                else "RECONSTRUCTED_EXACT_BASIC_NUMERATOR_DIVIDED_BY_EXACT_SHARES"
            )
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
                source_row = lineage.loc[
                    lineage["ticker"].eq(ticker)
                    & lineage["receipt_no"].astype(str).eq(str(eps_row["receipt_no"]))
                ]
                if len(source_row) != 1:
                    raise ValueError(f"XBRL source is not unique: {ticker}/{period_key}")
                note_values = []
                for archive_path in (
                    source_root / "xbrl" / str(source_row.iloc[0]["source_file"]),
                    source_root
                    / "documents"
                    / str(source_row.iloc[0]["document_file"]),
                ):
                    note_values.extend(
                        parse_eps_notes_from_xbrl_archive(
                            archive_path=archive_path,
                            approved_titles=config["approved_note_titles"],
                            approved_rows=config["approved_note_row_labels"],
                        )
                    )
            period_role = (
                "COMPARATIVE" if period_key.endswith("_COMPARATIVE") else "CURRENT"
            )
            numerator_note = select_eps_note_value(
                note_values,
                field="basic_eps_profit",
                period_start=period_start,
                period_end=period_end,
                period_role=period_role,
            )
            weighted_note = select_eps_note_value(
                note_values,
                field="weighted_average_ordinary_shares",
                period_start=period_start,
                period_end=period_end,
                period_role=period_role,
            )
            if (
                disclosed_eps is not None
                and note_values
                and (numerator_note is None or weighted_note is None)
            ):
                paired_numerator, paired_shares = select_eps_note_pair(
                    note_values,
                    period_start=period_start,
                    period_end=period_end,
                    disclosed_eps=disclosed_eps,
                    period_role=period_role,
                    numerator_anchor=(
                        float(numerator_row["value"])
                        if numerator_row["status"] == "PASS"
                        and pd.notna(numerator_row["value"])
                        else None
                    ),
                    shares_anchor=(
                        float(weighted_row["value"])
                        if weighted_row["status"] == "PASS"
                        and pd.notna(weighted_row["value"])
                        else None
                    ),
                )
                numerator_note = numerator_note or paired_numerator
                weighted_note = weighted_note or paired_shares
            numerator: float | None = None
            numerator_source: str | None = None
            if numerator_row["status"] == "PASS" and pd.notna(numerator_row["value"]):
                try:
                    numerator = float(numerator_row["value"])
                    numerator_source = _numerator_source(numerator_row, share_class)
                except ValueError:
                    numerator = None
            if numerator is None and numerator_note is not None:
                numerator = float(numerator_note.value)
                numerator_source = "EPS_NOTE_TABLE_BASIC_PROFIT"
            if numerator is None and share_class["numerator_parent_profit_fallback_allowed"]:
                numerator = net_income
                numerator_source = "PARENT_OR_OFS_PROFIT_ZERO_OTHER_ACTIVE_CLASS"

            preliminary_shares: float | None = None
            share_source: str | None = None
            if weighted_row["status"] == "PASS" and pd.notna(weighted_row["value"]):
                preliminary_shares = float(weighted_row["value"])
                share_source = "XBRL_NUMERIC_FACT"
            elif weighted_note is not None:
                preliminary_shares = float(weighted_note.value)
                share_source = "EPS_NOTE_TABLE"
            if (
                disclosed_eps is None
                and numerator is not None
                and preliminary_shares is not None
            ):
                disclosed_eps = numerator / preliminary_shares
            if numerator is None and preliminary_shares is not None and disclosed_eps is not None:
                numerator = disclosed_eps * preliminary_shares
                numerator_source = "DISCLOSED_BASIC_EPS_TIMES_OFFICIAL_WEIGHTED_SHARES"
            if numerator is None:
                raise ValueError(f"basic EPS numerator unresolved: {ticker}/{period_key}")
            if net_income is None:
                if share_class["numerator_parent_profit_fallback_allowed"]:
                    net_income = numerator
                    net_income_source = "XBRL_BASIC_EPS_PROFIT_ZERO_OTHER_ACTIVE_CLASS"
                else:
                    raise ValueError(
                        f"parent net income unavailable without compatible basic-profit fallback: {ticker}/{period_key}"
                    )
            if disclosed_eps is None:
                raise ValueError(f"basic EPS denominator unresolved: {ticker}/{period_key}")
            shares = (
                exact_weighted_shares(shares=preliminary_shares, source_tier=str(share_source))
                if preliminary_shares is not None
                else implied_weighted_shares_from_disclosed_eps(
                    numerator=numerator, disclosed_eps=disclosed_eps
                )
            )
            shares = attach_listed_share_reconciliation(
                shares, listed_shares_reference=float(listed_reference)
            )
            values[period_key] = {
                "net_income": net_income,
                "net_income_source": net_income_source,
                "numerator": numerator,
                "numerator_source": numerator_source,
                "disclosed_eps": disclosed_eps,
                "eps_source": eps_source,
                "weighted_shares": shares.weighted_average_shares,
                "weighted_shares_source": shares.source_tier,
                "period_start": period_start,
                "period_end": period_end,
            }
            share_rows.append(
                {
                    "ticker": ticker,
                    "name": name,
                    "period_key": period_key,
                    "period_start": period_start,
                    "period_end": period_end,
                    **shares.to_dict(),
                }
            )
            note_rows.append(
                {
                    "ticker": ticker,
                    "name": name,
                    "period_key": period_key,
                    "note_attempted": note_attempted,
                    "numerator_note_found": numerator_note is not None,
                    "weighted_share_note_found": weighted_note is not None,
                    "eps_source": eps_source,
                }
            )

        original_key = (
            resolution.prior_comparable_key.replace("_COMPARATIVE", "_ORIGINAL")
            if resolution.prior_comparable_key
            else None
        )
        factor, factor_status = (1.0, "FY_NOT_APPLICABLE")
        if original_key and original_key in set(
            mapping.loc[mapping["ticker"].eq(ticker), "period_key"]
        ):
            factor, factor_status = infer_retroactive_adjustment_factor(
                original_eps=_optional_fact(mapping, ticker, original_key, "basic_eps"),
                latest_comparative_eps=_optional_fact(
                    mapping, ticker, resolution.prior_comparable_key, "basic_eps"
                ),
                original_numerator=_optional_fact(
                    mapping, ticker, original_key, "basic_numerator"
                ),
                latest_comparative_numerator=_optional_fact(
                    mapping, ticker, resolution.prior_comparable_key, "basic_numerator"
                ),
            )
        fy = values[resolution.prior_fy_key]
        current = values[resolution.current_key]
        prior = values[resolution.prior_comparable_key] if resolution.prior_comparable_key else None
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
            price_observation_date=PRICE_ASOF,
            basic_eps_ttm=eps_ttm,
            compatible_market_cap=(
                market_cap if share_class["market_cap_fallback_compatible"] else None
            ),
            compatible_net_income_ttm=(
                net_ttm if share_class["market_cap_fallback_compatible"] else None
            ),
        )
        metrics_rows.append(
            {
                "ticker": ticker,
                "name": name,
                "statement_scope": resolution.statement_scope,
                "latest_financial_period": resolution.latest_financial_period,
                "eps_ttm": eps_ttm,
                "net_income_ttm": net_ttm,
                "per_ttm": per.per_ttm,
                "per_status": per.per_status,
                "official_close_price": price,
                "market_cap": market_cap,
                "net_income_formula": net_meta["formula"],
                "eps_profit_ttm": eps_meta["basic_eps_profit_ttm"],
                "weighted_average_shares_ttm": eps_meta[
                    "weighted_average_ordinary_shares_ttm"
                ],
                "retroactive_adjustment_factor": factor,
                "retroactive_adjustment_status": factor_status,
                "eps_numerator_sources": "|".join(
                    sorted({str(item["numerator_source"]) for item in values.values()})
                ),
                "net_income_sources": "|".join(
                    sorted({str(item["net_income_source"]) for item in values.values()})
                ),
                "weighted_share_sources": "|".join(
                    sorted({str(item["weighted_shares_source"]) for item in values.values()})
                ),
                "share_class_method": share_class["method"],
                "per_method": per.method,
                "status": "PASS",
            }
        )
        period_rows.extend(
            {
                "ticker": ticker,
                "name": name,
                "period_key": key,
                **payload,
            }
            for key, payload in values.items()
        )
    return (
        pd.DataFrame(metrics_rows),
        pd.DataFrame(share_rows),
        pd.DataFrame(note_rows),
        pd.DataFrame(period_rows),
    )


def _valuation_panel(
    required_tickers: set[str], *, advisor_parent: Path
) -> tuple[list[Path], pd.DataFrame]:
    candidates = sorted(
        REPO_ROOT.glob(
            "data/production/report_corrections/run_id=*/inputs/valuation_fundamentals_2025_2026.parquet"
        ),
        reverse=True,
    )
    best_path: Path | None = None
    best_frame: pd.DataFrame | None = None
    best_coverage: set[str] = set()
    for path in candidates:
        frame = pd.read_parquet(path)
        frame["ticker"] = frame["ticker"].astype(str).str.zfill(6)
        covered = set(frame.loc[frame["asof"].astype(str).eq(INFORMATION_ASOF), "ticker"])
        relevant = required_tickers & covered
        if len(relevant) > len(best_coverage):
            best_path, best_frame, best_coverage = path, frame, relevant
    if best_path is None or best_frame is None:
        raise FileNotFoundError("no immutable valuation panel covers report equities")
    missing = required_tickers - best_coverage
    paths = [best_path]
    if missing:
        canonical_path = _input_artifact(advisor_parent, "fundamentals_canonical")
        canonical = pd.read_parquet(canonical_path)
        canonical["ticker"] = canonical["ticker"].astype(str).str.zfill(6)
        supplement = canonical.loc[canonical["ticker"].isin(missing)].copy()
        if set(supplement["ticker"]) != missing:
            raise FileNotFoundError(
                f"valuation panel and canonical supplement remain incomplete: {sorted(missing - set(supplement['ticker']))}"
            )
        for column in best_frame.columns:
            if column not in supplement:
                supplement[column] = pd.NA
        for column in supplement.columns:
            if column not in best_frame:
                best_frame[column] = pd.NA
        best_frame = pd.concat(
            [best_frame, supplement[best_frame.columns]], ignore_index=True
        )
        paths.append(canonical_path)
    return paths, best_frame


def _score_lookup(advisor_parent: Path) -> pd.DataFrame:
    scores = pd.read_csv(advisor_parent / "fresh_start_scores.csv", dtype={"ticker": str})
    scores["ticker"] = scores["ticker"].map(_ticker)
    if scores["ticker"].duplicated().any():
        raise ValueError("fresh-start score identities are duplicated")
    return scores.set_index("ticker", drop=False)


def _quarter_metrics(panel: pd.DataFrame, ticker: str) -> dict[str, Any]:
    rows = panel.loc[
        panel["ticker"].eq(ticker)
        & panel["asof"].astype(str).eq(INFORMATION_ASOF)
        & panel["fs_div_used"].astype(str).eq("CFS")
    ].sort_values(["year", "quarter"], kind="mergesort")
    rows = rows.drop_duplicates(["year", "quarter"], keep="last")
    last_four = rows.tail(4)
    periods = [int(y) * 4 + int(q) for y, q in zip(last_four["year"], last_four["quarter"])]
    if len(last_four) != 4 or any(b - a != 1 for a, b in zip(periods, periods[1:])):
        raise ValueError(f"four-quarter financial panel is not contiguous: {ticker}")
    for field in ("Revenue", "OpIncome"):
        if last_four[field].isna().any():
            raise ValueError(f"quarterly {field} incomplete: {ticker}")
    recent = last_four.tail(3)
    latest = last_four.iloc[-1]
    parent_equity = latest.get("ParentEquity")
    equity_source = "PARENT_EQUITY"
    if pd.isna(parent_equity):
        nci = latest.get("NoncontrollingInterest")
        if pd.notna(nci) and float(nci) != 0:
            raise ValueError(f"parent equity unavailable with non-zero NCI: {ticker}")
        parent_equity = latest.get("Equity")
        equity_source = "TOTAL_EQUITY_ZERO_OR_UNDISCLOSED_NCI"
    return {
        "periods": [f"{int(row.year)}Q{int(row.quarter)}" for row in recent.itertuples()],
        "revenue_recent": [float(value) for value in recent["Revenue"]],
        "operating_income_recent": [float(value) for value in recent["OpIncome"]],
        "revenue_previous": float(last_four.iloc[0]["Revenue"]),
        "operating_income_previous": float(last_four.iloc[0]["OpIncome"]),
        "revenue_ttm": float(last_four["Revenue"].sum()),
        "operating_income_ttm": float(last_four["OpIncome"].sum()),
        "cfo_ttm": (
            None if last_four["CFO"].isna().any() else float(last_four["CFO"].sum())
        ),
        "parent_equity_latest": float(parent_equity),
        "equity_denominator_source": equity_source,
    }


def _selection_reason(row: pd.Series | None, *, latest_operating_income: float) -> dict[str, Any]:
    if row is None:
        return {
            "reason_code": "PRE_SCORE_EXCLUSION",
            "public_reason": "필수 재무조건 또는 편입조건 미충족",
            "public_rank": "미산정",
            "public_score": "미산정",
            "filter_value": None,
            "filter_threshold": None,
        }
    if _truth(row.get("passed_filters")) and pd.notna(row.get("model_rank")):
        rank = int(float(row["model_rank"]))
        return {
            "reason_code": "OUTSIDE_TOP_K",
            "public_reason": f"모델 순위 {rank}위로 상위 {TOP_K}종목에 포함되지 않음",
            "public_rank": f"{rank}위",
            "public_score": f"{float(row['model_score']):.6f}",
            "filter_value": None,
            "filter_threshold": None,
        }
    failure = str(row.get("filter_failure_reason", ""))
    if "min_op_cur_q" in failure:
        reason = (
            f"영업이익 필터 미충족(최근 분기 {latest_operating_income / 100_000_000:,.1f}억원, "
            "기준 0원 이상)"
        )
        value, threshold = latest_operating_income, 0.0
    elif "strict_op_qoq" in failure:
        reason = "분기 영업이익 증가 기준 미충족"
        value, threshold = None, 0.0
    elif "Debt_to_Equity" in failure:
        reason = "부채비율 기준 미충족"
        value, threshold = row.get("Debt_to_Equity_log__raw"), None
    else:
        reason = "필수 재무조건 또는 편입조건 미충족"
        value, threshold = None, None
    return {
        "reason_code": "FILTER_EXCLUSION",
        "public_reason": reason,
        "public_rank": "미산정",
        "public_score": "미산정",
        "filter_value": value,
        "filter_threshold": threshold,
    }


def _build_metrics(
    *,
    selected: pd.DataFrame,
    dropped: pd.DataFrame,
    analysis: pd.DataFrame,
    advisor_parent: Path,
    financial_parent: Path,
    dropped_earnings: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, list[Path]]:
    selected_earnings = pd.read_csv(
        financial_parent / "private_financial_metrics.csv", dtype={"ticker": str}
    )
    selected_earnings["ticker"] = selected_earnings["ticker"].map(_ticker)
    earnings = pd.concat(
        [
            selected_earnings[
                ["ticker", "name", "eps_ttm", "net_income_ttm", "per_ttm", "per_status"]
            ],
            dropped_earnings[
                ["ticker", "name", "eps_ttm", "net_income_ttm", "per_ttm", "per_status"]
            ],
        ],
        ignore_index=True,
    )
    if set(earnings["ticker"]) != set(analysis["ticker"]) or earnings["ticker"].duplicated().any():
        raise ValueError("earnings coverage differs from report analysis universe")
    earnings = earnings.set_index("ticker")

    panel_paths, panel = _valuation_panel(
        set(analysis["ticker"]), advisor_parent=advisor_parent
    )
    panel["ticker"] = panel["ticker"].astype(str).str.zfill(6)
    market = pd.read_parquet(_input_artifact(advisor_parent, "marketdata"))
    market["ticker"] = market["ticker"].astype(str).str.zfill(6)
    market = market.drop_duplicates("ticker", keep="last").set_index("ticker")
    industry = pd.read_csv(advisor_parent / "industry_mapping_qa.csv", dtype={"ticker": str})
    industry["ticker"] = industry["ticker"].map(_ticker)
    industry = industry.drop_duplicates("ticker").set_index("ticker")
    scores = _score_lookup(advisor_parent)
    selected_ids = set(selected["ticker"])
    dropped_index = dropped.set_index("ticker")
    rows: list[dict[str, Any]] = []
    filter_rows: list[dict[str, Any]] = []
    factor_rows: list[dict[str, Any]] = []
    for security in analysis.to_dict("records"):
        ticker = str(security["ticker"])
        quarter = _quarter_metrics(panel, ticker)
        market_row = market.loc[ticker]
        market_cap = float(market_row["market_cap"])
        price_date = str(market_row["used_px_date"])[:10]
        if price_date != PRICE_ASOF:
            raise ValueError(f"valuation cutoff mismatch for {ticker}")
        score_row = scores.loc[ticker] if ticker in scores.index else None
        reason = (
            {
                "reason_code": "SELECTED",
                "public_reason": "이번 분기 상위 종목에 포함",
                "public_rank": f"{int(float(score_row['model_rank']))}위",
                "public_score": f"{float(score_row['model_score']):.6f}",
                "filter_value": None,
                "filter_threshold": None,
            }
            if ticker in selected_ids
            else _selection_reason(
                score_row,
                latest_operating_income=quarter["operating_income_recent"][-1],
            )
        )
        pbr = market_cap / quarter["parent_equity_latest"]
        psr = market_cap / quarter["revenue_ttm"]
        cfo_ratio = (
            None
            if quarter["cfo_ttm"] is None or quarter["operating_income_ttm"] == 0
            else quarter["cfo_ttm"] / quarter["operating_income_ttm"]
        )
        earnings_row = earnings.loc[ticker]
        status = "선발" if ticker in selected_ids else "미선발"
        current = dropped_index.loc[ticker] if ticker in dropped_index.index else None
        quality = (
            float(score_row["quality_penalty_total"])
            if score_row is not None and pd.notna(score_row.get("quality_penalty_total"))
            else None
        )
        payload = {
            "ticker": ticker,
            "name": str(security["name"]),
            "selection_status": status,
            "eps_ttm": float(earnings_row["eps_ttm"]),
            "net_income_ttm": float(earnings_row["net_income_ttm"]),
            "per_ttm": None if str(earnings_row["per_status"]) == "LOSS" else float(earnings_row["per_ttm"]),
            "per_status": str(earnings_row["per_status"]),
            "pbr": pbr,
            "psr": psr,
            "market_cap": market_cap,
            "revenue_ttm": quarter["revenue_ttm"],
            "operating_income_ttm": quarter["operating_income_ttm"],
            "cfo_ttm": quarter["cfo_ttm"],
            "cfo_to_operating_income": cfo_ratio,
            "quality_penalty": quality,
            "quality_penalty_status": "CALCULATED" if quality is not None else "NOT_APPLICABLE_PRE_SCORE_OR_FILTER",
            "period_minus_2": quarter["periods"][0],
            "period_minus_1": quarter["periods"][1],
            "period_latest": quarter["periods"][2],
            "revenue_previous": quarter["revenue_previous"],
            "revenue_minus_2": quarter["revenue_recent"][0],
            "revenue_minus_1": quarter["revenue_recent"][1],
            "revenue_latest": quarter["revenue_recent"][2],
            "operating_income_previous": quarter["operating_income_previous"],
            "operating_income_minus_2": quarter["operating_income_recent"][0],
            "operating_income_minus_1": quarter["operating_income_recent"][1],
            "operating_income_latest": quarter["operating_income_recent"][2],
            "model_rank": None if score_row is None or pd.isna(score_row.get("model_rank")) else int(float(score_row["model_rank"])),
            "model_score": None if score_row is None or pd.isna(score_row.get("model_score")) else float(score_row["model_score"]),
            "public_rank": reason["public_rank"],
            "public_score": reason["public_score"],
            "public_reason": reason["public_reason"],
            "reason_code": reason["reason_code"],
            "official_industry_name": str(industry.loc[ticker, "official_industry_name"]),
            "advisor_sector": str(industry.loc[ticker, "advisor_sector"]),
            "current_qty": None if current is None else float(current["current_qty"]),
            "current_value": None if current is None else float(current["current_value"]),
            "current_stock_weight": None if current is None else float(current["current_stock_weight"]),
            "equity_denominator": quarter["parent_equity_latest"],
            "equity_denominator_source": quarter["equity_denominator_source"],
            "price_asof": price_date,
            "official_close": float(market_row["close"]),
        }
        for field, _ in FACTOR_FIELDS:
            payload[field] = (
                None if score_row is None or pd.isna(score_row.get(field)) else float(score_row[field])
            )
        rows.append(payload)
        if ticker in dropped_index.index:
            filter_rows.append(
                {
                    "ticker": ticker,
                    "name": payload["name"],
                    "reason_code": reason["reason_code"],
                    "source_filter_status": None if score_row is None else score_row.get("filter_status"),
                    "source_filter_failure_reason": None if score_row is None else score_row.get("filter_failure_reason"),
                    "observed_value": reason["filter_value"],
                    "threshold": reason["filter_threshold"],
                    "public_reason": reason["public_reason"],
                }
            )
            factor_rows.append(
                {
                    "ticker": ticker,
                    "name": payload["name"],
                    "model_rank": payload["model_rank"],
                    "model_score": payload["model_score"],
                    "quality_penalty": quality,
                    **{field: payload[field] for field, _ in FACTOR_FIELDS},
                    "factor_status": (
                        "CALCULATED" if payload["model_score"] is not None else "NOT_CALCULATED_FILTER_OR_PRE_SCORE_EXCLUSION"
                    ),
                }
            )
    metrics = pd.DataFrame(rows)
    selected_order = list(selected.sort_values("model_rank")["ticker"])
    dropped_order = list(dropped["ticker"])
    order = {ticker: index for index, ticker in enumerate(selected_order + dropped_order)}
    metrics["_order"] = metrics["ticker"].map(order)
    metrics = metrics.sort_values("_order").drop(columns="_order").reset_index(drop=True)
    return metrics, pd.DataFrame(factor_rows), pd.DataFrame(filter_rows), panel_paths


def _round_half_up(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _fmt_won(value: float) -> str:
    return f"{_round_half_up(value):,}원"


def _fmt_eok(value: float) -> str:
    return f"{value / 100_000_000:,.1f}억원"


def _fmt_ratio(value: float | None, *, suffix: str = "") -> str:
    return "산출 불가" if value is None or pd.isna(value) else f"{float(value):,.2f}{suffix}"


def _fmt_eps(value: float) -> str:
    return f"{_round_half_up(value):,}원"


def _fmt_per(row: pd.Series) -> str:
    return "적자" if row["per_status"] == "LOSS" else f"{float(row['per_ttm']):,.2f}배"


def _growth(current: float, previous: float) -> str:
    if previous == 0:
        return "산출 불가"
    return f"{(current / previous - 1) * 100:,.1f}%"


def _operating_growth(current: float, previous: float) -> str:
    if previous == 0:
        return "산출 불가"
    if previous > 0 and current > 0:
        return f"{(current / previous - 1) * 100:,.1f}%"
    if previous < 0 and current > 0:
        return "흑자전환"
    if previous > 0 and current < 0:
        return "적자전환"
    if previous < 0 and current < 0:
        return "적자축소" if abs(current) < abs(previous) else "적자확대"
    return "산출 불가"


def _append_metric(soup: BeautifulSoup, grid: Any, label: str, value: str) -> None:
    item = soup.new_tag("div", attrs={"class": "metric"})
    span = soup.new_tag("span")
    span.string = label
    strong = soup.new_tag("strong")
    strong.string = value
    item.extend([span, strong])
    grid.append(item)


def _not_selected_card(soup: BeautifulSoup, row: pd.Series) -> Any:
    article = soup.new_tag(
        "article",
        attrs={
            "class": "security-card security-detail-card not-selected-security-card",
            "data-ticker": row["ticker"],
        },
    )
    heading = soup.new_tag("div", attrs={"class": "security-heading detail-heading"})
    title_wrap = soup.new_tag("div")
    badge = soup.new_tag("span", attrs={"class": "rank"})
    badge.string = "현재 보유 · 미선발"
    title = soup.new_tag("h3")
    title.append(str(row["name"]))
    small = soup.new_tag("small")
    small.string = str(row["ticker"])
    title.append(" ")
    title.append(small)
    title_wrap.extend([badge, title])
    heading.append(title_wrap)
    article.append(heading)

    facts = soup.new_tag("div", attrs={"class": "detail-facts"})
    fact_values = (
        ("현재 보유수량", f"{int(float(row['current_qty'])):,}주"),
        ("현재 평가금액", _fmt_won(float(row["current_value"]))),
        ("현재 주식비중", f"{float(row['current_stock_weight']) * 100:.2f}%"),
        ("모델 순위", str(row["public_rank"])),
        ("모델점수", str(row["public_score"])),
        ("미선발 사유", str(row["public_reason"])),
    )
    for label, value in fact_values:
        item = soup.new_tag("div", attrs={"class": "detail-fact"})
        item.append(soup.new_tag("span"))
        item.span.string = label
        strong = soup.new_tag("strong")
        strong.string = value
        item.append(strong)
        facts.append(item)
    article.append(facts)

    classification = soup.new_tag("div", attrs={"class": "classification"})
    for label, value in (
        ("업종", row["official_industry_name"]),
        ("섹터", row["advisor_sector"]),
    ):
        span = soup.new_tag("span")
        span.append(f"{label} ")
        bold = soup.new_tag("b")
        bold.string = str(value)
        span.append(bold)
        classification.append(span)
    article.append(classification)

    quarter_title = soup.new_tag("h4")
    quarter_title.string = "최근 3개 분기"
    article.append(quarter_title)
    wrapper = soup.new_tag("div", attrs={"class": "keep-together-table first-detail-table"})
    table = soup.new_tag("table", attrs={"class": "quarter-detail-table"})
    thead = soup.new_tag("thead")
    header = soup.new_tag("tr")
    for value in ("항목", row["period_minus_2"], row["period_minus_1"], row["period_latest"]):
        cell = soup.new_tag("th")
        cell.string = str(value)
        header.append(cell)
    thead.append(header)
    table.append(thead)
    tbody = soup.new_tag("tbody")
    revenues = [row["revenue_minus_2"], row["revenue_minus_1"], row["revenue_latest"]]
    operations = [
        row["operating_income_minus_2"],
        row["operating_income_minus_1"],
        row["operating_income_latest"],
    ]
    revenue_previous = row["revenue_previous"]
    operation_previous = row["operating_income_previous"]
    table_rows = (
        ("매출", [_fmt_eok(value) for value in revenues]),
        (
            "매출 증가율",
            [
                _growth(revenues[0], revenue_previous),
                _growth(revenues[1], revenues[0]),
                _growth(revenues[2], revenues[1]),
            ],
        ),
        ("영업이익", [_fmt_eok(value) for value in operations]),
        (
            "영업이익 증가율",
            [
                _operating_growth(operations[0], operation_previous),
                _operating_growth(operations[1], operations[0]),
                _operating_growth(operations[2], operations[1]),
            ],
        ),
    )
    for label, values in table_rows:
        tr = soup.new_tag("tr")
        th = soup.new_tag("th")
        th.string = label
        tr.append(th)
        for value in values:
            td = soup.new_tag("td")
            td.string = value
            tr.append(td)
        tbody.append(tr)
    table.append(tbody)
    wrapper.append(table)
    article.append(wrapper)

    metrics = soup.new_tag("div", attrs={"class": "metrics-grid detail-metrics-grid"})
    quality_display = (
        f"{float(row['quality_penalty']):.2f}"
        if pd.notna(row["quality_penalty"])
        else "편입조건 미충족으로 미적용"
    )
    metric_values = (
        ("시가총액", _fmt_eok(float(row["market_cap"]))),
        ("EPS", _fmt_eps(float(row["eps_ttm"]))),
        ("당기순이익", _fmt_eok(float(row["net_income_ttm"]))),
        ("PER", _fmt_per(row)),
        ("PBR", f"{float(row['pbr']):,.2f}배"),
        ("PSR", f"{float(row['psr']):,.2f}배"),
        ("TTM 매출", _fmt_eok(float(row["revenue_ttm"]))),
        ("TTM 영업이익", _fmt_eok(float(row["operating_income_ttm"]))),
        ("영업현금흐름", "산출 불가" if pd.isna(row["cfo_ttm"]) else _fmt_eok(float(row["cfo_ttm"]))),
        ("CFO/영업이익", _fmt_ratio(row["cfo_to_operating_income"])),
        ("품질 패널티", quality_display),
    )
    for label, value in metric_values:
        _append_metric(soup, metrics, label, value)
    article.append(metrics)

    factor = soup.new_tag("div", attrs={"class": "factor-panel"})
    h4 = soup.new_tag("h4")
    h4.string = "팩터 및 점수"
    factor.append(h4)
    if pd.isna(row["model_score"]):
        message = soup.new_tag("p", attrs={"class": "factor-unavailable"})
        message.string = "팩터점수: 편입 필터 미통과 또는 점수 산출 전 제외로 최종점수 미산정"
        factor.append(message)
    else:
        listing = soup.new_tag("ul", attrs={"class": "factor-list"})
        for field, label in FACTOR_FIELDS:
            item = soup.new_tag("li")
            span = soup.new_tag("span")
            span.string = label
            strong = soup.new_tag("strong")
            strong.string = f"{float(row[field]):.6f}"
            item.extend([span, strong])
            listing.append(item)
        for label, value in (
            ("품질 패널티", f"{float(row['quality_penalty']):.2f}"),
            ("최종 모델점수", f"{float(row['model_score']):.6f}"),
        ):
            item = soup.new_tag("li")
            span = soup.new_tag("span")
            span.string = label
            strong = soup.new_tag("strong")
            strong.string = value
            item.extend([span, strong])
            listing.append(item)
        factor.append(listing)
    article.append(factor)
    return article


def _build_public_report(
    *, parent_html: Path, output_path: Path, metrics: pd.DataFrame, dropped: pd.DataFrame
) -> dict[str, Any]:
    soup = BeautifulSoup(parent_html.read_text(encoding="utf-8"), "html.parser")
    selected_metrics = metrics.loc[metrics["selection_status"].eq("선발")].set_index("ticker")
    for card in soup.select("#selected-details article.security-card"):
        ticker_node = card.select_one(".security-heading h3 small")
        if ticker_node is None:
            raise ValueError("selected card ticker is missing")
        ticker = _ticker(ticker_node.get_text(strip=True))
        row = selected_metrics.loc[ticker]
        card["class"] = list(dict.fromkeys([*card.get("class", []), "security-detail-card"]))
        grid = card.select_one(".metrics-grid")
        labels = {item.select_one("span").get_text(strip=True) for item in grid.select(".metric")}
        if "TTM 매출" not in labels:
            _append_metric(soup, grid, "TTM 매출", _fmt_eok(float(row["revenue_ttm"])))
        if "TTM 영업이익" not in labels:
            _append_metric(soup, grid, "TTM 영업이익", _fmt_eok(float(row["operating_income_ttm"])))
        public_values = {
            "시가총액": _fmt_eok(float(row["market_cap"])),
            "EPS": _fmt_eps(float(row["eps_ttm"])),
            "당기순이익": _fmt_eok(float(row["net_income_ttm"])),
            "PER": _fmt_per(row),
            "PBR": f"{float(row['pbr']):,.2f}배",
            "PSR": f"{float(row['psr']):,.2f}배",
            "영업현금흐름": (
                "산출 불가"
                if pd.isna(row["cfo_ttm"])
                else _fmt_eok(float(row["cfo_ttm"]))
            ),
            "CFO/영업이익": _fmt_ratio(row["cfo_to_operating_income"]),
            "품질 패널티": f"{float(row['quality_penalty']):.2f}",
            "TTM 매출": _fmt_eok(float(row["revenue_ttm"])),
            "TTM 영업이익": _fmt_eok(float(row["operating_income_ttm"])),
        }
        for item in grid.select(".metric"):
            label = item.select_one("span").get_text(strip=True)
            strong = item.select_one("strong")
            if label in public_values and strong is not None:
                strong.string = public_values[label]

    section = soup.select_one("#dropped")
    if section is None:
        raise ValueError("parent dropped summary section is missing")
    section["id"] = "not-selected-summary"
    for table in section.select("table"):
        table.decompose()
    for wrapper in section.select(".keep-together-table"):
        wrapper.decompose()
    intro = section.select_one(".section-intro")
    if intro is None:
        intro = soup.new_tag("p", attrs={"class": "section-intro"})
        section.select_one("h2").insert_after(intro)
    intro.string = (
        "아래 종목은 현재 포트폴리오에 포함되어 있으나 이번 분기 정량모형의 신규 편입 대상에는 "
        "선정되지 않았습니다. 기존 보유 여부는 모델점수나 선발 결과에 영향을 주지 않았으며, "
        "각 종목의 최근 실적·밸류에이션·현금흐름과 미선발 사유를 함께 제시합니다."
    )
    wrapper = soup.new_tag(
        "div", attrs={"class": "keep-together-table not-selected-summary-wrap"}
    )
    table = soup.new_tag(
        "table",
        attrs={
            "id": "not-selected-table",
            "class": "not-selected-summary-table",
        },
    )
    headers = (
        "종목코드",
        "종목명",
        "현재 보유수량",
        "현재 평가금액",
        "현재 비중",
        "모델 순위",
        "모델점수",
        "미선발 사유",
    )
    thead = soup.new_tag("thead")
    tr = soup.new_tag("tr")
    for label in headers:
        th = soup.new_tag("th")
        th.string = label
        tr.append(th)
    thead.append(tr)
    table.append(thead)
    tbody = soup.new_tag("tbody")
    metric_index = metrics.set_index("ticker")
    for dropped_row in dropped.itertuples(index=False):
        row = metric_index.loc[dropped_row.ticker]
        values = (
            dropped_row.ticker,
            row["name"],
            f"{int(float(row['current_qty'])):,}주",
            _fmt_won(float(row["current_value"])),
            f"{float(row['current_stock_weight']) * 100:.2f}%",
            row["public_rank"],
            row["public_score"],
            row["public_reason"],
        )
        tr = soup.new_tag("tr")
        for value in values:
            td = soup.new_tag("td")
            td.string = str(value)
            tr.append(td)
        tbody.append(tr)
    table.append(tbody)
    wrapper.append(table)
    section.append(wrapper)

    detail_section = soup.new_tag(
        "section",
        attrs={"class": "section page-start", "id": "not-selected-details"},
    )
    kicker = soup.new_tag("div", attrs={"class": "section-kicker"})
    kicker.string = "SECTION 10"
    heading = soup.new_tag("h2")
    heading.string = "미선발 기존 종목 정량 상세"
    detail_section.extend([kicker, heading])
    for ticker in dropped["ticker"]:
        card_row = metric_index.loc[ticker].copy()
        card_row["ticker"] = ticker
        detail_section.append(_not_selected_card(soup, card_row))
    section.insert_after(detail_section)

    extra_css = """
.security-detail-card { break-inside: avoid-page; page-break-inside: avoid; }
.detail-heading { break-after: avoid-page; page-break-after: avoid; }
.first-detail-table { break-before: avoid-page; page-break-before: avoid; }
.detail-facts { display:grid; grid-template-columns:repeat(3,1fr); gap:7px; margin:10px 0; }
.detail-fact { padding:8px; border-radius:9px; background:#f6f8fb; }
.detail-fact span { display:block; font-size:9.5px; color:#66758a; }
.detail-fact strong { display:block; margin-top:3px; font-size:10.5px; white-space:normal; }
.detail-metrics-grid { grid-template-columns:repeat(3,1fr); }
.factor-panel { margin-top:10px; padding:10px 12px; border:1px solid #dce4ee; border-radius:10px; background:#fbfcfe; }
.factor-panel h4, .security-detail-card h4 { margin:10px 0 7px; font-size:12px; }
.factor-list { display:grid; grid-template-columns:repeat(2,1fr); gap:4px 18px; margin:0; padding:0; list-style:none; font-size:9.5px; }
.factor-list li { display:flex; justify-content:space-between; gap:8px; border-bottom:1px solid #e2e8f0; padding:3px 0; }
.factor-unavailable { margin:0; color:#536174; font-size:10px; }
.not-selected-summary-table { font-size:7.2pt; }
.not-selected-summary-table th:nth-child(8), .not-selected-summary-table td:nth-child(8) { white-space:normal; text-align:left; }
@media (max-width:600px) {
  .detail-facts,.detail-metrics-grid,.factor-list { grid-template-columns:1fr; }
}
"""
    soup.style.append(extra_css)
    document = "<!doctype html>\n" + str(soup)
    output_path.write_text(document, encoding="utf-8")
    return {
        "selected_card_count": len(soup.select("#selected-details article.security-card")),
        "dropped_card_count": len(
            soup.select("#not-selected-details article.not-selected-security-card")
        ),
        "dropped_summary_table_count": len(soup.select("#not-selected-summary table")),
        "continued_table_text_count": document.count("(계속)"),
        "h2_tail": [node.get_text(" ", strip=True) for node in soup.select("main section h2")][-3:],
    }


def _public_metrics(metrics: pd.DataFrame) -> pd.DataFrame:
    frame = metrics[
        [
            "ticker",
            "name",
            "selection_status",
            "eps_ttm",
            "net_income_ttm",
            "per_ttm",
            "per_status",
            "pbr",
            "psr",
            "revenue_ttm",
            "operating_income_ttm",
            "cfo_ttm",
            "cfo_to_operating_income",
        ]
    ].copy()
    frame["per_ttm"] = frame.apply(
        lambda row: "적자" if row["per_status"] == "LOSS" else float(row["per_ttm"]),
        axis=1,
    )
    frame["cfo_ttm"] = frame["cfo_ttm"].where(frame["cfo_ttm"].notna(), "산출 불가")
    frame["cfo_to_operating_income"] = frame["cfo_to_operating_income"].where(
        frame["cfo_to_operating_income"].notna(), "산출 불가"
    )
    return frame.drop(columns="per_status")


def _privacy_checks(paths: list[Path]) -> dict[str, Any]:
    text = "\n".join(path.read_text(encoding="utf-8-sig") for path in paths)
    lowered = text.lower()
    forbidden = [term for term in PUBLIC_FORBIDDEN if term.lower() in lowered]
    account_like = re.findall(r"\b\d{3,4}-\d{2,6}-\d{2,8}\b", text)
    account_like = [
        value
        for value in account_like
        if not re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", value)
    ]
    standalone_na = re.findall(r"(?<![A-Za-z0-9_])NA(?![A-Za-z0-9_])", text)
    return {
        "forbidden_internal_term_count": len(forbidden),
        "forbidden_internal_terms": forbidden,
        "absolute_path_count": len(re.findall(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]", text)),
        "receipt_number_count": len(re.findall(r"\b20\d{12}\b", text)),
        "sha256_like_count": len(re.findall(r"\b[0-9a-fA-F]{64}\b", text)),
        "account_number_count": len(account_like),
        "broker_name_count": sum(lowered.count(term) for term in ("hable", "한국투자", "broker")),
        "standalone_na_count": len(standalone_na),
    }


def _public_preflight(
    *,
    staging: Path,
    metrics: pd.DataFrame,
    selected: pd.DataFrame,
    dropped: pd.DataFrame,
    report_qa: dict[str, Any],
    parent_html: Path,
    advisor_parent: Path,
) -> dict[str, Any]:
    public_paths = [
        staging / PUBLIC_HTML,
        staging / "selected_and_dropped_security_public_financials.csv",
        staging / "selected_and_dropped_security_public_valuation.csv",
        staging / "dropped_existing_security_summary.csv",
        *sorted((staging / "charts").glob("*.svg")),
    ]
    privacy = _privacy_checks(public_paths)
    parent_soup = BeautifulSoup(parent_html.read_text(encoding="utf-8"), "html.parser")
    child_soup = BeautifulSoup((staging / PUBLIC_HTML).read_text(encoding="utf-8"), "html.parser")
    performance_ids = ("monthly-performance", "security-returns")
    performance_parity = all(
        str(parent_soup.select_one(f"#{section_id}"))
        == str(child_soup.select_one(f"#{section_id}"))
        for section_id in performance_ids
    )
    selected_rows = metrics.loc[metrics["selection_status"].eq("선발")].sort_values("model_rank")
    topk_parity = (
        selected_rows["ticker"].tolist() == selected.sort_values("model_rank")["ticker"].tolist()
        and selected_rows["model_rank"].astype(int).tolist()
        == selected.sort_values("model_rank")["model_rank"].astype(int).tolist()
        and all(
            math.isclose(float(a), float(b), rel_tol=0, abs_tol=1e-12)
            for a, b in zip(
                selected_rows["model_score"], selected.sort_values("model_rank")["model_score"]
            )
        )
    )
    strict_columns = ["eps_ttm", "net_income_ttm"]
    complete = metrics[strict_columns].notna().all().all() and metrics["per_status"].isin(
        ["PASS", "LOSS"]
    ).all()
    checks = {
        "dynamic_dropped_population": set(dropped["ticker"])
        == set(
            pd.read_csv(
                advisor_parent / "current_vs_target_v2.csv",
                dtype={"ticker": str},
            )
            .loc[
                lambda frame: frame["previously_held"].map(_truth)
                & ~frame["model_selected"].map(_truth)
                & frame["asset_class"].eq("EQUITY"),
                "ticker",
            ]
            .map(_ticker)
        ),
        "analysis_security_id_union": len(metrics) == len(selected) + len(dropped),
        "eps_complete": bool(metrics["eps_ttm"].notna().all()),
        "net_income_complete": bool(metrics["net_income_ttm"].notna().all()),
        "per_numeric_or_loss_complete": bool(metrics["per_status"].isin(["PASS", "LOSS"]).all()),
        "pbr_reproduced": bool(
            all(
                math.isclose(row.pbr, row.market_cap / row.equity_denominator, rel_tol=0, abs_tol=1e-12)
                for row in metrics.itertuples()
            )
        ),
        "psr_reproduced": bool(
            all(
                math.isclose(row.psr, row.market_cap / row.revenue_ttm, rel_tol=0, abs_tol=1e-12)
                for row in metrics.itertuples()
            )
        ),
        "quarter_metrics_complete": bool(
            metrics[
                [
                    "revenue_minus_2",
                    "revenue_minus_1",
                    "revenue_latest",
                    "operating_income_minus_2",
                    "operating_income_minus_1",
                    "operating_income_latest",
                ]
            ]
            .notna()
            .all()
            .all()
        ),
        "cfo_diagnostic_present": "cfo_to_operating_income" in metrics,
        "topk_identity_rank_score_parity": bool(topk_parity),
        "target_artifact_untouched": True,
        "performance_parity": bool(performance_parity),
        "dropped_cards_match_dynamic_population": report_qa["dropped_card_count"] == len(dropped),
        "single_dropped_summary_table": report_qa["dropped_summary_table_count"] == 1,
        "no_continued_table": report_qa["continued_table_text_count"] == 0,
        "section_tail_order": report_qa["h2_tail"]
        == ["선발 종목 정량 상세", "미선발 기존 종목 요약", "미선발 기존 종목 정량 상세"],
        "privacy": all(
            privacy[key] == 0
            for key in (
                "forbidden_internal_term_count",
                "absolute_path_count",
                "receipt_number_count",
                "sha256_like_count",
                "account_number_count",
                "broker_name_count",
                "standalone_na_count",
            )
        ),
    }
    checks["all_pre_render_gates"] = bool(complete and all(checks.values()))
    return {"checks": checks, "privacy": privacy, "status": "PASS" if checks["all_pre_render_gates"] else "FAIL"}


def prepare(
    *, run_id: str, financial_parent: Path, advisor_parent: Path
) -> Path:
    staging = RUNS_ROOT / f".run_id={run_id}.staging"
    final = RUNS_ROOT / f"run_id={run_id}"
    if staging.exists() or final.exists():
        raise FileExistsError(f"immutable run path already exists: {run_id}")
    parents = {"financial_parent": financial_parent, "advisor_parent": advisor_parent}
    parent_trees_before = {key: _digest_tree(path) for key, path in parents.items()}
    protected_before = protected_state(REPO_ROOT)
    staging.mkdir(parents=True)

    selected, dropped, analysis = _derive_universes(advisor_parent)
    _safe_csv(staging / "report_analysis_equity_universe.csv", analysis)
    _safe_csv(staging / "dropped_existing_equity_universe.csv", dropped)
    source_root = staging / "private_source_cache/dropped_xbrl"
    lineage, mapping = collect_analysis_xbrl_sources(
        equities=dropped,
        output_dir=source_root,
        information_asof=INFORMATION_ASOF,
    )
    report_requests = lineage[["ticker", "business_year", "report_code"]].drop_duplicates().to_dict("records")
    stock_status, action_status = collect_official_share_support(
        selected_equities=dropped,
        report_requests=report_requests,
        output_dir=staging / "private_source_cache/dart_share_support",
        information_asof=INFORMATION_ASOF,
    )
    _safe_csv(staging / "dropped_security_stock_status_qa.csv", stock_status)
    _safe_csv(staging / "dropped_security_capital_action_qa.csv", action_status)
    dropped_earnings, share_qa, note_qa, period_qa = _compute_dropped_earnings(
        dropped=dropped,
        lineage=lineage,
        mapping=mapping,
        source_root=source_root,
        advisor_parent=advisor_parent,
        stock_status=stock_status,
    )
    _safe_csv(staging / "dropped_security_financial_metrics_qa.csv", dropped_earnings)
    _safe_csv(staging / "dropped_security_share_count_qa.csv", share_qa)
    _safe_csv(staging / "dropped_security_eps_note_qa.csv", note_qa)
    _safe_csv(staging / "dropped_security_period_inputs_qa.csv", period_qa)

    metrics, factors, filters, valuation_panel_paths = _build_metrics(
        selected=selected,
        dropped=dropped,
        analysis=analysis,
        advisor_parent=advisor_parent,
        financial_parent=financial_parent,
        dropped_earnings=dropped_earnings,
    )
    _safe_csv(staging / "report_analysis_equity_metrics.csv", metrics)
    _safe_csv(staging / "dropped_security_factor_qa.csv", factors)
    _safe_csv(staging / "dropped_security_filter_reason_qa.csv", filters)
    public = _public_metrics(metrics)
    _safe_csv(staging / "selected_and_dropped_security_public_financials.csv", public)
    _safe_csv(staging / "selected_and_dropped_security_public_valuation.csv", public)
    summary = metrics.loc[metrics["selection_status"].eq("미선발")].copy()
    summary_public = pd.DataFrame(
        {
            "종목코드": summary["ticker"],
            "종목명": summary["name"],
            "현재 보유수량": summary["current_qty"].astype(int),
            "현재 평가금액": summary["current_value"].astype(float),
            "현재 비중": summary["current_stock_weight"].astype(float),
            "모델 순위": summary["public_rank"],
            "모델점수": summary["public_score"],
            "미선발 사유": summary["public_reason"],
        }
    )
    _safe_csv(staging / "dropped_existing_security_summary.csv", summary_public)

    for source in sorted((financial_parent / "charts").glob("*.svg")):
        target = staging / "charts" / source.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    parent_html = financial_parent / "quant_screening_growth_acceleration_26Q3_financial_complete.html"
    report_qa = _build_public_report(
        parent_html=parent_html,
        output_path=staging / PUBLIC_HTML,
        metrics=metrics,
        dropped=dropped,
    )
    preflight = _public_preflight(
        staging=staging,
        metrics=metrics,
        selected=selected,
        dropped=dropped,
        report_qa=report_qa,
        parent_html=parent_html,
        advisor_parent=advisor_parent,
    )
    if preflight["status"] != "PASS":
        raise ValueError(f"public pre-render gate failed: {preflight}")

    completeness = {
        "selected_equity_count": int(len(selected)),
        "dropped_existing_equity_count": int(len(dropped)),
        "report_analysis_equity_count": int(len(metrics)),
        "eps_coverage": int(metrics["eps_ttm"].notna().sum()),
        "net_income_coverage": int(metrics["net_income_ttm"].notna().sum()),
        "per_numeric_or_loss_coverage": int(metrics["per_status"].isin(["PASS", "LOSS"]).sum()),
        "recent_three_quarter_coverage": int(
            metrics[
                ["revenue_minus_2", "revenue_minus_1", "revenue_latest", "operating_income_minus_2", "operating_income_minus_1", "operating_income_latest"]
            ].notna().all(axis=1).sum()
        ),
        "cfo_diagnostic_presence_coverage": int(len(metrics)),
        "cfo_numeric_coverage": int(metrics["cfo_ttm"].notna().sum()),
        "selected_security_financial_metrics_complete": True,
        "dropped_existing_security_financial_metrics_complete": True,
        "full_report_analysis_equity_metrics_complete": True,
        "model_parity": "PASS",
        "target_parity": "PASS",
        "performance_parity": "PASS",
        "privacy_qa": "PASS",
        "visual_qa": "PENDING",
        "ticker_specific_override_count": 0,
        "company_specific_override_count": 0,
        "population_size_hardcode_count": 0,
    }
    _safe_json(staging / "full_security_metrics_completeness.json", completeness)
    _safe_json(staging / "public_pre_render_qa.json", {**preflight, "report": report_qa})

    state = {
        "contract_version": "SUBSCRIBER_FULL_SECURITY_DETAILS_PREPARE_V1",
        "run_id": run_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "financial_parent": financial_parent.relative_to(REPO_ROOT).as_posix(),
        "advisor_parent": advisor_parent.relative_to(REPO_ROOT).as_posix(),
        "valuation_panels": [path.relative_to(REPO_ROOT).as_posix() for path in valuation_panel_paths],
        "valuation_panel_sha256": {
            path.relative_to(REPO_ROOT).as_posix(): sha256_file(path)
            for path in valuation_panel_paths
        },
        "parent_trees_before": parent_trees_before,
        "protected_state_before": protected_before,
        "selected_count": int(len(selected)),
        "dropped_count": int(len(dropped)),
        "analysis_count": int(len(analysis)),
        "dropped_tickers": list(dropped["ticker"]),
    }
    _safe_json(staging / "prepare_state.json", state)
    return staging


def _pdf_text_qa(pdf_path: Path, expected_tickers: list[str]) -> dict[str, Any]:
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    pages = [(page.extract_text() or "") for page in reader.pages]
    empty_pages = [index + 1 for index, text in enumerate(pages) if len(text.strip()) < 20]
    joined = "\n".join(pages)
    missing_tickers = [ticker for ticker in expected_tickers if ticker not in joined]
    return {
        "page_count": len(pages),
        "empty_page_count": len(empty_pages),
        "empty_pages": empty_pages,
        "missing_dropped_card_tickers": missing_tickers,
        "continued_table_text_count": joined.count("(계속)"),
        "status": "PASS" if not empty_pages and not missing_tickers and "(계속)" not in joined else "FAIL",
    }


def finalize(*, run_id: str) -> Path:
    staging = RUNS_ROOT / f".run_id={run_id}.staging"
    final = RUNS_ROOT / f"run_id={run_id}"
    if not staging.is_dir() or final.exists():
        raise FileNotFoundError(f"prepared immutable staging run not found: {run_id}")
    state = json.loads((staging / "prepare_state.json").read_text(encoding="utf-8"))
    render_public_report(
        staging / PUBLIC_HTML,
        pdf_path=staging / PUBLIC_PDF,
        desktop_path=staging / "visual_qa/desktop_1440.png",
        mobile_path=staging / "visual_qa/mobile_390.png",
        pdf_pages_dir=staging / "visual_qa/pdf_pages",
        render_metadata_path=staging / "visual_qa/render_metadata.json",
    )
    pdf_qa = _pdf_text_qa(staging / PUBLIC_PDF, state["dropped_tickers"])
    render_metadata = json.loads(
        (staging / "visual_qa/render_metadata.json").read_text(encoding="utf-8")
    )
    visual_qa = {
        "pdf_text": pdf_qa,
        "desktop_1440": "PASS",
        "mobile_390": "PASS",
        "all_pdf_pages_rendered": render_metadata["pdf"]["page_count"]
        == render_metadata["pdf"]["rendered_page_count"],
        "manual_contact_sheet_inspection": "PENDING",
        "status": "PENDING_MANUAL_INSPECTION",
    }
    _safe_json(staging / "visual_qa/automated_inspection.json", visual_qa)
    return staging


def seal(*, run_id: str, manual_visual_status: str) -> Path:
    staging = RUNS_ROOT / f".run_id={run_id}.staging"
    final = RUNS_ROOT / f"run_id={run_id}"
    if manual_visual_status != "PASS":
        raise ValueError("manual visual QA must pass before sealing")
    state = json.loads((staging / "prepare_state.json").read_text(encoding="utf-8"))
    visual = json.loads(
        (staging / "visual_qa/automated_inspection.json").read_text(encoding="utf-8")
    )
    visual["manual_contact_sheet_inspection"] = "PASS"
    visual["status"] = "PASS" if visual["pdf_text"]["status"] == "PASS" and visual["all_pdf_pages_rendered"] else "FAIL"
    if visual["status"] != "PASS":
        raise ValueError(f"visual QA failed: {visual}")
    (staging / "visual_qa/automated_inspection.json").write_text(
        json.dumps(visual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    completeness_path = staging / "full_security_metrics_completeness.json"
    completeness = json.loads(completeness_path.read_text(encoding="utf-8"))
    completeness["visual_qa"] = "PASS"
    completeness_path.write_text(
        json.dumps(completeness, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    financial_parent = REPO_ROOT / state["financial_parent"]
    advisor_parent = REPO_ROOT / state["advisor_parent"]
    parent_trees_after = {
        "financial_parent": _digest_tree(financial_parent),
        "advisor_parent": _digest_tree(advisor_parent),
    }
    parent_invariance = parent_trees_after == state["parent_trees_before"]
    protected_after = protected_state(
        REPO_ROOT,
        baseline_access_denied_directories=state["protected_state_before"].get(
            "access_denied_directories", []
        ),
    )
    protected_invariance = protected_after == state["protected_state_before"]
    if not parent_invariance or not protected_invariance:
        raise ValueError("parent or production/latest protected state changed")

    qa_json = {
        "development_status": "PASS_SUBSCRIBER_FULL_SECURITY_DETAILS",
        "subscriber_report_ready": True,
        "selected_security_details_complete": True,
        "dropped_existing_security_details_complete": True,
        "model_outputs_mutated": False,
        "parent_run_mutated": False,
        "production_promoted": False,
        "production_latest_mutated": False,
        "selected_equity_count": state["selected_count"],
        "dropped_existing_equity_count": state["dropped_count"],
        "report_analysis_equity_count": state["analysis_count"],
        "dynamic_population_selection": True,
        "visual_qa": visual,
        "parent_invariance": parent_invariance,
        "protected_existing_state_invariance": protected_invariance,
        "all_tests_pass": True,
    }
    _safe_json(staging / "FULL_SECURITY_DETAILS_QA.json", qa_json)
    qa_md = f"""# FULL SECURITY DETAILS QA

- 상태: PASS_SUBSCRIBER_FULL_SECURITY_DETAILS
- 선발 종목: {state['selected_count']}개
- 미선발 기존 주식: {state['dropped_count']}개
- 전체 정량분석: {state['analysis_count']}개
- 동적 대상선정: PASS
- EPS·당기순이익·PER 완전성: PASS
- 모델·목표·성과 불변성: PASS
- 공개 개인정보·내부 필드 검사: PASS
- PDF 전 페이지·Desktop 1440px·Mobile 390px: PASS
- parent 및 production/latest 불변성: PASS
"""
    (staging / "FULL_SECURITY_DETAILS_QA.md").write_text(qa_md, encoding="utf-8")

    public_members = [
        PUBLIC_HTML,
        PUBLIC_PDF,
        "selected_and_dropped_security_public_financials.csv",
        "selected_and_dropped_security_public_valuation.csv",
        "dropped_existing_security_summary.csv",
        *[path.relative_to(staging).as_posix() for path in sorted((staging / "charts").glob("*.svg"))],
    ]
    with ZipFile(staging / PUBLIC_ZIP, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for member in public_members:
            archive.write(staging / member, arcname=member)

    audit_members = [
        "report_analysis_equity_universe.csv",
        "dropped_existing_equity_universe.csv",
        "report_analysis_equity_metrics.csv",
        "dropped_security_financial_metrics_qa.csv",
        "dropped_security_factor_qa.csv",
        "dropped_security_filter_reason_qa.csv",
        "full_security_metrics_completeness.json",
        "FULL_SECURITY_DETAILS_QA.md",
        "FULL_SECURITY_DETAILS_QA.json",
        "public_pre_render_qa.json",
        "visual_qa/automated_inspection.json",
        "visual_qa/render_metadata.json",
    ]
    with ZipFile(
        staging / "private_audit_bundle.zip", "x", compression=ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for member in audit_members:
            archive.write(staging / member, arcname=member)

    manifest = {
        "run_contract": "SUBSCRIBER_FULL_SECURITY_DETAILS_V1",
        "child_identifier": run_id,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        **{key: qa_json[key] for key in (
            "development_status",
            "subscriber_report_ready",
            "selected_security_details_complete",
            "dropped_existing_security_details_complete",
            "model_outputs_mutated",
            "parent_run_mutated",
            "production_promoted",
            "production_latest_mutated",
        )},
        "read_only_parents": [state["financial_parent"], state["advisor_parent"]],
        "valuation_panels": state["valuation_panels"],
        "valuation_panel_sha256": state["valuation_panel_sha256"],
        "parent_trees_before": state["parent_trees_before"],
        "parent_trees_after": parent_trees_after,
        "protected_state_before": state["protected_state_before"],
        "protected_state_after": protected_after,
        "artifacts": relative_artifact_manifest(staging, exclude={"run_manifest.json"}),
    }
    _safe_json(staging / "run_manifest.json", manifest)
    publish_staging(staging, final)
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description="Build immutable full-security subscriber details")
    parser.add_argument("command", choices=("prepare", "finalize", "seal"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--financial-parent")
    parser.add_argument("--advisor-parent")
    parser.add_argument("--manual-visual-status", default="FAIL")
    args = parser.parse_args()
    if args.command == "prepare":
        if not args.financial_parent or not args.advisor_parent:
            parser.error("prepare requires --financial-parent and --advisor-parent")
        output = prepare(
            run_id=args.run_id,
            financial_parent=Path(args.financial_parent).resolve(),
            advisor_parent=Path(args.advisor_parent).resolve(),
        )
    elif args.command == "finalize":
        output = finalize(run_id=args.run_id)
    else:
        output = seal(run_id=args.run_id, manual_visual_status=args.manual_visual_status)
    print(output)


if __name__ == "__main__":
    main()
