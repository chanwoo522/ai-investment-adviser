from __future__ import annotations

import json
import io
import hashlib
import os
import re
from pathlib import Path
from typing import Any

import pandas as pd
import requests
from zipfile import ZipFile

from scripts.data_pipeline.collect_selected_earnings_denominators import (
    REPORT_CODES,
    REPORT_PERIODS,
    EarningsContractError,
    _api_json,
    _canonical_report_name,
    _latest_receipt_with_xbrl,
    _period_facts,
    _scope_for_instances,
    parse_instance,
)


REPORT_NAME_PATTERNS = {
    "FY2025": re.compile(r"사업보고서\(2025\.12\)"),
    "H1_2025": re.compile(r"반기보고서\(2025\.06\)"),
    "H1_2026": re.compile(r"반기보고서\(2026\.06\)"),
}


def _official_document_zip(
    session: requests.Session, *, key: str, receipt_no: str
) -> bytes:
    try:
        response = session.get(
            "https://opendart.fss.or.kr/api/document.xml",
            params={"crtfc_key": key, "rcept_no": receipt_no},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.content
        with ZipFile(io.BytesIO(payload)) as archive:
            if not archive.namelist():
                raise EarningsContractError(
                    f"empty official document archive for receipt {receipt_no}"
                )
        return payload
    except EarningsContractError:
        raise
    except Exception as exc:
        raise EarningsContractError(
            f"OpenDART document request failed for receipt {receipt_no}: {type(exc).__name__}"
        ) from None


def _write_new_csv(path: Path, frame: pd.DataFrame) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _eligible_receipts(
    session: requests.Session,
    *,
    key: str,
    corp_code: str,
    information_asof: str,
) -> dict[str, list[dict[str, str]]]:
    payload = _api_json(
        session,
        "list.json",
        {
            "crtfc_key": key,
            "corp_code": corp_code,
            "bgn_de": "20250101",
            "end_de": information_asof.replace("-", ""),
            "pblntf_ty": "A",
            "page_no": "1",
            "page_count": "100",
        },
    )
    selected: dict[str, list[dict[str, str]]] = {}
    for report_key, pattern in REPORT_NAME_PATTERNS.items():
        candidates: list[dict[str, str]] = []
        for row in payload.get("list", []) or []:
            receipt = str(row.get("rcept_no", ""))
            if not re.fullmatch(r"\d{14}", receipt):
                continue
            receipt_date = f"{receipt[:4]}-{receipt[4:6]}-{receipt[6:8]}"
            if receipt_date > information_asof:
                continue
            if pattern.search(_canonical_report_name(row.get("report_nm"))):
                candidates.append(
                    {
                        "receipt_no": receipt,
                        "receipt_date": receipt_date,
                        "report_name": str(row.get("report_nm", "")),
                    }
                )
        if not candidates:
            raise EarningsContractError(
                f"required filing missing: corp={corp_code} report={report_key}"
            )
        selected[report_key] = sorted(
            candidates, key=lambda item: item["receipt_no"], reverse=True
        )
    return selected


def collect_analysis_xbrl_sources(
    *,
    equities: pd.DataFrame,
    output_dir: Path,
    information_asof: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Collect normalized official XBRL facts for an arbitrary equity set.

    The input controls the population.  No ticker, issuer name, or population
    size is embedded in this collector.
    """

    if output_dir.exists():
        raise FileExistsError(output_dir)
    required = {"ticker", "name", "corp_code"}
    missing = required - set(equities.columns)
    if missing:
        raise EarningsContractError(f"analysis equity columns missing: {sorted(missing)}")
    securities = equities.loc[:, ["ticker", "name", "corp_code"]].copy()
    securities["ticker"] = securities["ticker"].astype(str).str.zfill(6)
    securities["corp_code"] = (
        securities["corp_code"]
        .astype(str)
        .str.replace(r"\.0$", "", regex=True)
        .str.zfill(8)
    )
    if securities.empty or securities["ticker"].duplicated().any():
        raise EarningsContractError("analysis equity identities must be non-empty and unique")
    key = os.environ.get("DART_API_KEY")
    if not key:
        raise EarningsContractError("DART_API_KEY is not configured")

    output_dir.mkdir(parents=True)
    cache_dir = output_dir / "xbrl"
    cache_dir.mkdir()
    document_dir = output_dir / "documents"
    document_dir.mkdir()
    session = requests.Session()
    session.headers.update({"User-Agent": "report-analysis-equities/1.0"})
    lineage_rows: list[dict[str, Any]] = []
    mapping_rows: list[dict[str, Any]] = []

    for security in securities.to_dict("records"):
        ticker = str(security["ticker"])
        corp_code = str(security["corp_code"])
        receipts = _eligible_receipts(
            session,
            key=key,
            corp_code=corp_code,
            information_asof=information_asof,
        )
        instances = {}
        for report_key in REPORT_NAME_PATTERNS:
            receipt, payload, rejected = _latest_receipt_with_xbrl(
                session, key, receipts[report_key]
            )
            cache_path = (
                cache_dir
                / f"ticker={ticker}__receipt={receipt['receipt_no']}__xbrl.zip"
            )
            if cache_path.exists():
                raise FileExistsError(cache_path)
            cache_path.write_bytes(payload)
            document_payload = _official_document_zip(
                session, key=key, receipt_no=receipt["receipt_no"]
            )
            document_path = (
                document_dir
                / f"ticker={ticker}__receipt={receipt['receipt_no']}__document.zip"
            )
            if document_path.exists():
                raise FileExistsError(document_path)
            document_path.write_bytes(document_payload)
            business_year = 2025 if report_key != "H1_2026" else 2026
            instance = parse_instance(
                payload,
                receipt_no=receipt["receipt_no"],
                receipt_date=receipt["receipt_date"],
                report_key=report_key,
                business_year=business_year,
                report_code=REPORT_CODES[report_key],
                source_zip=cache_path,
            )
            instances[report_key] = instance
            lineage_rows.append(
                {
                    "ticker": ticker,
                    "name": str(security["name"]),
                    "corp_code": corp_code,
                    "report_key": report_key,
                    "statement_scope": None,
                    "receipt_no": instance.receipt_no,
                    "receipt_date": instance.receipt_date,
                    "business_year": business_year,
                    "report_code": instance.report_code,
                    "taxonomy_version": instance.taxonomy_version,
                    "source_file_sha256": instance.source_sha256,
                    "source_file": cache_path.name,
                    "document_file": document_path.name,
                    "document_file_sha256": hashlib.sha256(document_payload).hexdigest(),
                    "newer_receipts_without_xbrl": json.dumps(
                        rejected, ensure_ascii=False, separators=(",", ":")
                    ),
                }
            )

        scope, scope_status = _scope_for_instances(instances.values())
        if scope is None:
            raise EarningsContractError(
                f"statement scope unresolved for {ticker}: {scope_status}"
            )
        for row in lineage_rows[-len(REPORT_NAME_PATTERNS) :]:
            row["statement_scope"] = scope
        specs = (
            ("FY2025", instances["FY2025"], *REPORT_PERIODS["FY2025"]),
            ("H1_2025_ORIGINAL", instances["H1_2025"], *REPORT_PERIODS["H1_2025"]),
            ("H1_2026", instances["H1_2026"], *REPORT_PERIODS["H1_2026"]),
            (
                "H1_2025_COMPARATIVE",
                instances["H1_2026"],
                *REPORT_PERIODS["H1_2025_COMPARATIVE"],
            ),
        )
        for period_key, instance, period_start, period_end in specs:
            _, rows = _period_facts(
                instance,
                period_start=period_start,
                period_end=period_end,
                scope=scope,
            )
            mapping_rows.extend(
                {
                    "ticker": ticker,
                    "name": str(security["name"]),
                    "period_key": period_key,
                    **row,
                }
                for row in rows
            )

    lineage = pd.DataFrame(lineage_rows)
    mapping = pd.DataFrame(mapping_rows)
    _write_new_csv(output_dir / "earnings_source_lineage.csv", lineage)
    _write_new_csv(output_dir / "earnings_concept_mapping.csv", mapping)
    return lineage, mapping
