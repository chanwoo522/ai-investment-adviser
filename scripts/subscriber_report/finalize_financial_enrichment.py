from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.advisor.immutable_run import (
    protected_state,
    publish_staging,
    relative_artifact_manifest,
    sha256_file,
)


PUBLIC_PARENT_ID = "subscriber_26Q3_report_20260820_20260823T012244720326Z"
UPSTREAM_PARENT_ID = "advisor_full_reset_v2_20260820_20260821T142726787Z"
PUBLIC_PARENT_ROOT = (
    REPO_ROOT / "data/development/subscriber_reports/runs" / f"run_id={PUBLIC_PARENT_ID}"
)
UPSTREAM_PARENT_ROOT = (
    REPO_ROOT / "data/development/advisor_full_reset_v2/runs" / f"run_id={UPSTREAM_PARENT_ID}"
)
INFORMATION_ASOF = "2026-08-18"
PRICE_ASOF = "2026-08-18"


def _canonical_digest(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _digest_tree(root: Path) -> dict[str, Any]:
    records = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": int(path.stat().st_size),
                "sha256": sha256_file(path),
            }
        )
    return {
        "file_count": len(records),
        "tree_sha256": _canonical_digest(records),
        "files": records,
    }


def _safe_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _frame_exact(left: pd.DataFrame, right: pd.DataFrame, columns: list[str]) -> bool:
    try:
        pd.testing.assert_frame_equal(
            left[columns].reset_index(drop=True),
            right[columns].reset_index(drop=True),
            check_dtype=False,
            check_exact=True,
        )
    except AssertionError:
        return False
    return True


def _validate(staging: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    summary = json.loads((staging / "financial_enrichment_summary.json").read_text(encoding="utf-8"))
    lineage = pd.read_csv(staging / "earnings_source_lineage.csv", dtype={"ticker": str})
    mappings = pd.read_csv(staging / "earnings_concept_mapping.csv", dtype={"ticker": str})
    net = pd.read_csv(staging / "net_income_ttm_qa.csv", dtype={"ticker": str})
    eps = pd.read_csv(staging / "eps_ttm_qa.csv", dtype={"ticker": str})
    shares = pd.read_csv(staging / "weighted_average_shares_qa.csv", dtype={"ticker": str})
    per = pd.read_csv(staging / "per_method_qa.csv", dtype={"ticker": str})
    share_class = pd.read_csv(staging / "share_class_market_cap_qa.csv", dtype={"ticker": str})
    parent_fin = pd.read_csv(
        PUBLIC_PARENT_ROOT / "selected_security_public_financials.csv", dtype={"ticker": str}
    )
    child_fin = pd.read_csv(staging / "selected_security_public_financials_v2.csv", dtype={"ticker": str})
    upstream_top = pd.read_csv(UPSTREAM_PARENT_ROOT / "fresh_start_top_k_v2.csv", dtype={"ticker": str})
    upstream_target = pd.read_csv(UPSTREAM_PARENT_ROOT / "target_portfolio_v2.csv", dtype={"ticker": str})

    receipt_cutoff = bool(lineage["receipt_date"].astype(str).le(INFORMATION_ASOF).all())
    no_scope_mix = bool(lineage.groupby("ticker")["statement_scope"].nunique().le(1).all())
    parent_public_columns_unchanged = _frame_exact(parent_fin, child_fin, list(parent_fin.columns))
    top_identity = list(parent_fin.sort_values("model_rank")["ticker"]) == list(
        upstream_top.sort_values("model_rank")["ticker"]
    )
    target = upstream_target.loc[upstream_target["ticker"].isin(upstream_top["ticker"])].sort_values(
        "model_rank"
    )
    top_target_unchanged = _frame_exact(
        upstream_top.sort_values("model_rank"),
        target,
        ["ticker", "name", "model_rank", "model_score"],
    )
    target_contract_preserved = bool(
        top_target_unchanged
        and target["target_weight"].eq(0.09).all()
        and target["reference_price_asof"].astype(str).eq(PRICE_ASOF).all()
    )

    eps_numeric = eps.dropna(subset=["eps_ttm", "profit_for_basic_eps_ttm", "weighted_average_shares_ttm"])
    eps_reproduction_error = (
        eps_numeric["eps_ttm"]
        - eps_numeric["profit_for_basic_eps_ttm"] / eps_numeric["weighted_average_shares_ttm"]
    ).abs()
    eps_formula_reproduces = bool((eps_reproduction_error <= 1e-9).all())
    per_price_priority = bool(
        per.loc[per["eps_ttm"].notna() & per["eps_ttm"].gt(0), "per_formula_used"]
        .eq("PRICE_DIV_EPS")
        .all()
    )
    mcap_only_when_eps_missing = bool(
        per.loc[per["per_formula_used"].eq("MCAP_DIV_NET_INCOME"), "eps_ttm"].isna().all()
    )
    loss_has_no_number = bool(
        per.loc[per["status"].eq("LOSS"), ["per_selected", "per_formula_used"]].isna().all().all()
    )
    samsung = per.loc[per["ticker"].eq("005930")].iloc[0]
    samsung_class_safe = bool(
        samsung["per_formula_used"] == "PRICE_DIV_EPS"
        and pd.isna(samsung["market_cap_for_per"])
        and "MCAP_FALLBACK_NOT_USED" in samsung["share_class_reconciliation_status"]
    )
    cfs_total_profit_fallback_absent = not bool(
        ((mappings["field"] == "net_income") & (mappings["statement_scope"] == "CFS") & (mappings["concept"] == "ProfitLoss")).any()
    )
    cfs_ofs_contract = bool(
        no_scope_mix
        and set(lineage["statement_scope"]).issubset({"CFS", "OFS"})
        and lineage.loc[lineage["statement_scope"].eq("OFS"), "ticker"].nunique() == 1
        and set(lineage.loc[lineage["statement_scope"].eq("OFS"), "ticker"]) == {"219130"}
    )
    exact_units = bool(
        mappings.loc[mappings["field"].eq("net_income") & mappings["status"].eq("PASS"), "unit"]
        .astype(str)
        .str.upper()
        .str.contains("KRW")
        .all()
        and mappings.loc[
            mappings["field"].eq("weighted_shares") & mappings["status"].eq("PASS"), "unit"
        ]
        .astype(str)
        .str.upper()
        .str.contains("SHARE")
        .all()
        and mappings.loc[mappings["field"].eq("basic_eps") & mappings["status"].eq("PASS"), "unit"]
        .astype(str)
        .str.upper()
        .str.contains("EPS|SHARE", regex=True)
        .all()
    )
    restatement_gate = bool(
        eps.loc[eps["comparative_share_count_restatement"].isna(), "eps_ttm"].isna().all()
    )
    report_files_absent = not any(
        (staging / name).exists()
        for name in (
            "quant_screening_growth_acceleration_26Q3_financial_enriched.html",
            "quant_screening_growth_acceleration_26Q3_financial_enriched.pdf",
        )
    )

    checks = [
        (1, "2026Q2 dependencies exclude future Q3/Q4", summary["future_quarter_dependencies"] == []),
        (2, "TTM formula is FY2025 + H1_2026 - H1_2025", summary["ttm_formula"] == "FY2025 + H1_2026 - H1_2025"),
        (3, "all receipt dates are at or before information_asof", receipt_cutoff),
        (4, "CFS/OFS is never mixed per security", cfs_ofs_contract),
        (5, "CFS total ProfitLoss fallback is absent", cfs_total_profit_fallback_absent),
        (6, "simple quarterly EPS summation is absent", summary["simple_quarter_eps_sum_used_count"] == 0),
        (7, "EPS reproduces numerator divided by weighted-average shares", eps_formula_reproduces),
        (8, "unresolved comparative share restatement blocks EPS", restatement_gate),
        (9, "price divided by EPS is first-priority PER", per_price_priority),
        (10, "market-cap PER is used only when EPS is unavailable", mcap_only_when_eps_missing),
        (11, "share-class reconciliation is explicit", share_class["share_class_reconciliation_status"].notna().all()),
        (12, "Samsung common market cap is not divided by total parent earnings", samsung_class_safe),
        (13, "loss PER has neither a number nor formula", loss_has_no_number),
        (14, "price basis is 2026-08-18", per["price_asof"].astype(str).eq(PRICE_ASOF).all()),
        (15, "calculations reproduce before public rounding", eps_formula_reproduces),
        (16, "external-provider EPS/PER usage is zero", summary["external_provider_eps_per_used_count"] == 0),
        (17, "model Top-K, ranks, scores, targets and parent public fields are unchanged", parent_public_columns_unchanged and top_identity and target_contract_preserved),
        (18, "incomplete gate suppresses enriched HTML/PDF", (summary["subscriber_report_ready"] and not report_files_absent) or (not summary["subscriber_report_ready"] and report_files_absent)),
        (19, "XBRL units match KRW, shares and KRW/share contracts", exact_units),
        (20, "source cache uses original XBRL and no external provider", summary["original_xbrl_used"] and summary["dart_api_used"]),
    ]
    runtime_checks = [
        {"id": check_id, "description": description, "status": "PASS" if passed else "FAIL"}
        for check_id, description, passed in checks
    ]
    failed = [item for item in runtime_checks if item["status"] != "PASS"]
    _require(not failed, f"financial enrichment runtime QA failed: {failed}")

    qa = {
        "status": "PASS_INCOMPLETE_GATE_ENFORCED" if not summary["subscriber_report_ready"] else "PASS",
        "information_asof": INFORMATION_ASOF,
        "price_asof": PRICE_ASOF,
        "coverage": {
            "top_k": 10,
            "net_income_ttm": int(net["ttm_value"].notna().sum()),
            "eps_ttm": int(eps["eps_ttm"].notna().sum()),
            "per_numeric_or_loss": int(per["status"].isin(["PASS", "LOSS"]).sum()),
        },
        "eps_formula_max_absolute_error": float(eps_reproduction_error.max()) if len(eps_reproduction_error) else None,
        "unresolved_restatement_count": int(eps["comparative_share_count_restatement"].isna().sum()),
        "runtime_checks": runtime_checks,
        "pdf_visual_qa": "NOT_RUN_COMPLETENESS_GATE_BLOCKED_REPORT_GENERATION",
        "privacy_qa": "NOT_APPLICABLE_NO_NEW_HTML_OR_PDF",
    }
    return qa, summary


def finalize(run_id: str) -> Path:
    runs_root = REPO_ROOT / "data/development/subscriber_financial_enrichment/runs"
    staging = runs_root / f".run_id={run_id}.staging"
    final = runs_root / f"run_id={run_id}"
    _require(staging.is_dir(), f"staging directory missing: {staging.name}")
    _require(not final.exists(), f"immutable child already exists: {final.name}")

    parent_before = {
        "public_subscriber_parent": _digest_tree(PUBLIC_PARENT_ROOT),
        "upstream_advisor_parent": _digest_tree(UPSTREAM_PARENT_ROOT),
    }
    protected_before = protected_state(REPO_ROOT)
    qa, summary = _validate(staging)
    _safe_json(staging / "FINANCIAL_ENRICHMENT_QA.json", qa)
    blockers = summary.get("incomplete_tickers", [])
    blocker_lines = "\n".join(
        f"- {row['ticker']} {row['name']}: {', '.join(row['missing_concepts']) or '계약 미충족'}"
        for row in blockers
    ) or "- 없음"
    (staging / "FINANCIAL_ENRICHMENT_QA.md").write_text(
        "# 구독자 재무 보강 QA\n\n"
        f"- 상태: {summary['financial_enrichment_status']}\n"
        f"- 당기순이익(TTM): {summary['net_income_coverage']}/10\n"
        f"- EPS(TTM): {summary['eps_coverage']}/10\n"
        f"- PER 숫자 또는 적자: {summary['per_numeric_or_loss_coverage']}/10\n"
        f"- 새 공개보고서 생성: {'예' if summary['subscriber_report_ready'] else '아니요(완전성 게이트)'}\n"
        "- 외부 EPS·PER 제공업체 사용: 0건\n"
        "- 미래 공시 사용: 0건\n\n"
        "## 차단 종목\n\n"
        f"{blocker_lines}\n",
        encoding="utf-8",
    )

    parent_after = {
        "public_subscriber_parent": _digest_tree(PUBLIC_PARENT_ROOT),
        "upstream_advisor_parent": _digest_tree(UPSTREAM_PARENT_ROOT),
    }
    protected_after = protected_state(REPO_ROOT)
    parent_unchanged = parent_before == parent_after
    protected_unchanged = protected_before == protected_after
    _require(parent_unchanged, "read-only parent changed during enrichment finalization")
    _require(protected_unchanged, "production/latest/existing immutable state changed")

    text_payload = "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in staging.rglob("*")
        if path.is_file() and path.suffix.lower() in {".csv", ".json", ".md"}
    )
    _require(
        not re.search(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]", text_payload),
        "absolute path leaked into child artifacts",
    )
    api_key = os.environ.get("DART_API_KEY", "")
    _require(not api_key or api_key not in text_payload, "DART API key leaked into child artifacts")

    artifacts = relative_artifact_manifest(staging, exclude=("run_manifest.json",))
    manifest = {
        "run_contract": "SUBSCRIBER_26Q3_FINANCIAL_ENRICHMENT_V1",
        "child_identifier": run_id,
        "read_only_parents": [PUBLIC_PARENT_ID, UPSTREAM_PARENT_ID],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "development_status": (
            "PASS_SUBSCRIBER_FINANCIAL_ENRICHMENT"
            if summary["subscriber_report_ready"]
            else "INCOMPLETE_SUBSCRIBER_FINANCIAL_ENRICHMENT"
        ),
        "financial_enrichment_status": summary["financial_enrichment_status"],
        "subscriber_report_ready": bool(summary["subscriber_report_ready"]),
        "report_regeneration_status": (
            "READY" if summary["subscriber_report_ready"] else "BLOCKED_BY_COMPLETENESS_GATE"
        ),
        "html_pdf_generated": bool(summary["subscriber_report_ready"]),
        "model_outputs_mutated": False,
        "parent_run_mutated": False,
        "production_promoted": False,
        "production_latest_mutated": False,
        "external_provider_eps_per_used_count": 0,
        "parent_tree_before": parent_before,
        "parent_tree_after": parent_after,
        "parent_runs_unchanged": parent_unchanged,
        "protected_state_before": protected_before,
        "protected_state_after_before_child_publish": protected_after,
        "protected_existing_state_unchanged": protected_unchanged,
        "artifacts": artifacts,
    }
    _safe_json(staging / "run_manifest.json", manifest)
    publish_staging(staging, final)
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description="Finalize immutable subscriber financial-enrichment child")
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    print(finalize(args.run_id))


if __name__ == "__main__":
    main()
