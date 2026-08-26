from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Iterable

import pandas as pd
import requests


class OfficialSourceError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_selected_equities(selected_portfolio_artifact: Path) -> pd.DataFrame:
    frame = pd.read_csv(selected_portfolio_artifact, dtype={"ticker": str, "corp_code": str})
    required = {"ticker", "name", "model_selected", "asset_class", "model_rank", "model_score", "target_weight", "target_value"}
    missing = required - set(frame.columns)
    if missing:
        raise OfficialSourceError(f"selected portfolio columns missing: {sorted(missing)}")
    selected = frame.loc[
        frame["model_selected"].astype(str).str.lower().eq("true")
        & frame["asset_class"].astype(str).eq("EQUITY")
    ].copy()
    if selected.empty:
        raise OfficialSourceError("selected equity set is empty")
    selected["ticker"] = selected["ticker"].astype(str).str.zfill(6)
    if "corp_code" in selected:
        selected["corp_code"] = selected["corp_code"].astype(str).str.replace(r"\.0$", "", regex=True).str.zfill(8)
    if selected["ticker"].duplicated().any() or selected["model_rank"].duplicated().any():
        raise OfficialSourceError("selected equity identities or ranks are duplicated")
    return selected.sort_values("model_rank").reset_index(drop=True)


def load_normalized_parent_sources(
    *, financial_parent_root: Path, selected_equities: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    lineage = pd.read_csv(
        financial_parent_root / "earnings_source_lineage.csv", dtype={"ticker": str, "corp_code": str, "receipt_no": str}
    )
    mapping = pd.read_csv(
        financial_parent_root / "earnings_concept_mapping.csv", dtype={"ticker": str, "receipt_no": str}
    )
    selected_ids = set(selected_equities["ticker"])
    lineage["ticker"] = lineage["ticker"].astype(str).str.zfill(6)
    mapping["ticker"] = mapping["ticker"].astype(str).str.zfill(6)
    lineage = lineage.loc[lineage["ticker"].isin(selected_ids)].copy()
    mapping = mapping.loc[mapping["ticker"].isin(selected_ids)].copy()
    if set(lineage["ticker"]) != selected_ids or set(mapping["ticker"]) != selected_ids:
        raise OfficialSourceError("normalized parent source coverage does not match selected equities")
    return lineage, mapping


def copy_private_xbrl_cache(
    *, financial_parent_root: Path, lineage: pd.DataFrame, destination: Path
) -> pd.DataFrame:
    if destination.exists():
        raise FileExistsError(destination)
    destination.mkdir(parents=True)
    source_root = financial_parent_root / "immutable_xbrl_cache"
    rows = []
    for row in lineage.itertuples():
        source = source_root / str(row.source_file)
        if not source.is_file():
            raise FileNotFoundError(source)
        expected = str(row.source_file_sha256)
        if sha256_file(source) != expected:
            raise OfficialSourceError(f"source hash mismatch: {source.name}")
        target = destination / source.name
        shutil.copyfile(source, target)
        if sha256_file(target) != expected:
            raise OfficialSourceError(f"copied source hash mismatch: {target.name}")
        rows.append(
            {
                "ticker": str(row.ticker),
                "receipt_no": str(row.receipt_no),
                "private_cache_file": target.name,
                "source_sha256": expected,
            }
        )
    return pd.DataFrame(rows)


def _api_json(
    session: requests.Session, endpoint: str, params: dict[str, str], *, allow_no_data: bool = True
) -> dict[str, Any]:
    try:
        response = session.get(f"https://opendart.fss.or.kr/api/{endpoint}", params=params, timeout=40)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise OfficialSourceError(f"OpenDART {endpoint} request failed: {type(exc).__name__}") from None
    status = str(payload.get("status", ""))
    if status == "013" and allow_no_data:
        return {"status": status, "message": payload.get("message"), "list": []}
    if status != "000":
        raise OfficialSourceError(f"OpenDART {endpoint} status={status} message={payload.get('message')}")
    return payload


def collect_official_share_support(
    *,
    selected_equities: pd.DataFrame,
    report_requests: Iterable[dict[str, Any]],
    output_dir: Path,
    information_asof: str,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if output_dir.exists():
        raise FileExistsError(output_dir)
    output_dir.mkdir(parents=True)
    api_key = os.environ.get("DART_API_KEY")
    if not api_key:
        raise OfficialSourceError("DART_API_KEY is not configured")
    security_by_ticker = selected_equities.set_index("ticker")
    session = requests.Session()
    session.headers.update({"User-Agent": "generic-financial-metrics-engine/1.0"})
    stock_rows: list[dict[str, Any]] = []
    action_rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for request in report_requests:
        ticker = str(request["ticker"])
        business_year = str(request["business_year"])
        report_code = str(request["report_code"])
        dedupe_key = (ticker, business_year, report_code)
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        corp_code = str(security_by_ticker.loc[ticker, "corp_code"])
        params = {
            "crtfc_key": api_key,
            "corp_code": corp_code,
            "bsns_year": business_year,
            "reprt_code": report_code,
        }
        for endpoint, sink in (("stockTotqySttus.json", stock_rows), ("irdsSttus.json", action_rows)):
            payload = _api_json(session, endpoint, params)
            safe_payload = {"status": payload.get("status"), "message": payload.get("message"), "list": payload.get("list", [])}
            cache_name = f"ticker={ticker}__year={business_year}__report={report_code}__endpoint={endpoint}"
            (output_dir / cache_name).write_text(
                json.dumps(safe_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            for row in safe_payload["list"] or []:
                receipt = str(row.get("rcept_no", ""))
                if len(receipt) >= 8:
                    receipt_date = f"{receipt[:4]}-{receipt[4:6]}-{receipt[6:8]}"
                    if receipt_date > information_asof:
                        continue
                sink.append(
                    {
                        "ticker": ticker,
                        "business_year": business_year,
                        "report_code": report_code,
                        "endpoint": endpoint,
                        **row,
                    }
                )
    return pd.DataFrame(stock_rows), pd.DataFrame(action_rows)
