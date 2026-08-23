#!/usr/bin/env python
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


BENCHMARK_NAME = "KRX 300"
BENCHMARK_ASSET_TYPE = "INDEX"
BENCHMARK_RETURN_TYPE = "PRICE"
BENCHMARK_SOURCE = "KRX_OFFICIAL_INDEX"
INDEX_MASTER_MARKET = "KRX"
INDEX_MASTER_ENDPOINT = "MDCSTAT00401"
INDEX_SERIES_ENDPOINT = "MDCSTAT00301"
KRX_JSON_ENDPOINT = "https://data.krx.co.kr/comm/bldAttendant/getJsonData.cmd"
NAV_DIVIDEND_TREATMENT = "EXCLUDED"

# These are exchange-traded products, not official KRX index identifiers.
FORBIDDEN_ETF_IDENTIFIERS = frozenset({"229200", "292190", "304760"})


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _redact_process_credentials(value: object) -> str:
    text = str(value)
    for name in ("KRX_ID", "KRX_PW"):
        secret = os.getenv(name)
        if secret:
            text = text.replace(secret, "<REDACTED>")
    return text


def _write_meta(output_csv: Path, payload: dict[str, Any]) -> Path:
    meta_path = output_csv.with_suffix(".meta.json")
    _write_new_text(meta_path, json.dumps(payload, ensure_ascii=False, indent=2))
    return meta_path


def _write_new_text(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    """Atomically seal a new run artifact without overwriting existing evidence."""

    if path.exists():
        raise FileExistsError(f"refusing to overwrite immutable benchmark artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        raise FileExistsError(f"stale benchmark artifact temporary exists: {temporary}")
    temporary.write_text(content, encoding=encoding)
    temporary.replace(path)


def _write_new_csv(frame: pd.DataFrame, path: Path, *, index: bool) -> None:
    if path.exists():
        raise FileExistsError(f"refusing to overwrite immutable benchmark artifact: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    if temporary.exists():
        raise FileExistsError(f"stale benchmark artifact temporary exists: {temporary}")
    frame.to_csv(temporary, index=index, encoding="utf-8-sig")
    temporary.replace(path)


def default_raw_artifact_paths(output_csv: Path) -> tuple[Path, Path]:
    return (
        output_csv.with_name(f"{output_csv.stem}.raw.index_master.json"),
        output_csv.with_name(f"{output_csv.stem}.raw.index_series.csv"),
    )


def require_krx_credentials() -> None:
    missing = [name for name in ("KRX_ID", "KRX_PW") if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            f"production KRX index collection requires process credentials: missing={missing}"
        )


def collect_official_index_master_response(
    stock_api,
    lookup_date: str,
    benchmark_name: str = BENCHMARK_NAME,
) -> tuple[str, list[dict[str, str]], dict[str, Any]]:
    """Collect and resolve the official adapter's complete KRX index master response.

    KRX index identifiers are group-qualified index identifiers.  They are not
    six-digit listed-security tickers, so they must never be zero-padded or
    substituted with an ETF ticker.
    """

    ymd = pd.Timestamp(lookup_date).strftime("%Y%m%d")
    identifiers = stock_api.get_index_ticker_list(date=ymd, market=INDEX_MASTER_MARKET)
    if not identifiers:
        raise RuntimeError(f"official KRX index master returned no rows for {ymd}")

    master_rows: list[dict[str, str]] = []
    for raw_identifier in identifiers:
        identifier = str(raw_identifier).strip()
        name = str(stock_api.get_index_ticker_name(identifier)).strip()
        master_rows.append({"index_identifier": identifier, "index_name": name})

    matches = [row for row in master_rows if row["index_name"] == benchmark_name]

    if len(matches) != 1:
        raise RuntimeError(
            f"official KRX index master exact-name lookup must return one row: "
            f"name={benchmark_name!r} matches={len(matches)}"
        )

    identifier = matches[0]["index_identifier"]
    if identifier in FORBIDDEN_ETF_IDENTIFIERS:
        raise RuntimeError(
            f"ETF identifier is forbidden for an INDEX benchmark: identifier={identifier}"
        )
    if identifier not in {str(value).strip() for value in identifiers}:
        raise RuntimeError("resolved identifier is not a member of the official KRX index master")
    master_response = {
        "schema_version": 1,
        "artifact_type": "OFFICIAL_KRX_INDEX_MASTER_ADAPTER_RESPONSE",
        "source_provider": "Korea Exchange (KRX)",
        "source_endpoint": KRX_JSON_ENDPOINT,
        "source_endpoint_bld": f"dbms/MDC/STAT/standard/{INDEX_MASTER_ENDPOINT}",
        "adapter_method": "pykrx.stock.get_index_ticker_list/get_index_ticker_name",
        "lookup_date": pd.Timestamp(lookup_date).strftime("%Y-%m-%d"),
        "market": INDEX_MASTER_MARKET,
        "requested_exact_name": benchmark_name,
        "rows": master_rows,
        "exact_matches": matches,
        "resolved_index_identifier": identifier,
        "collected_at_utc": _now_utc(),
    }
    return identifier, matches, master_response


def resolve_official_index_identifier(
    stock_api,
    lookup_date: str,
    benchmark_name: str = BENCHMARK_NAME,
) -> tuple[str, list[dict[str, str]]]:
    identifier, matches, _ = collect_official_index_master_response(
        stock_api, lookup_date, benchmark_name
    )
    return identifier, matches


def normalize_official_index_frame(
    frame: pd.DataFrame,
    *,
    index_identifier: str,
    benchmark_name: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    if frame is None or len(frame) == 0:
        raise RuntimeError("official KRX index series returned no rows")

    raw = frame.reset_index().copy()
    date_col = next(
        (column for column in ("날짜", "date", "Date", "TRD_DD") if column in raw.columns),
        raw.columns[0] if len(raw.columns) else None,
    )
    level_col = next(
        (
            column
            for column in ("종가", "index_level", "close", "Close", "CLSPRC_IDX")
            if column in raw.columns
        ),
        None,
    )
    if date_col is None or level_col is None:
        raise ValueError(
            f"official KRX index frame lacks date/close columns: columns={list(raw.columns)}"
        )

    out = raw[[date_col, level_col]].rename(
        columns={date_col: "date", level_col: "index_level"}
    )
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["index_level"] = pd.to_numeric(out["index_level"], errors="coerce")
    if out[["date", "index_level"]].isna().any(axis=None):
        raise ValueError("official KRX index series contains invalid date or close values")
    if out["date"].duplicated().any():
        duplicates = out.loc[out["date"].duplicated(False), "date"].dt.strftime("%Y-%m-%d")
        raise ValueError(f"official KRX index series contains duplicate dates: {duplicates.tolist()}")
    if out["index_level"].le(0).any():
        raise ValueError("official KRX index series contains a non-positive close")

    start = pd.Timestamp(start_date).normalize()
    end = pd.Timestamp(end_date).normalize()
    out["date"] = out["date"].dt.normalize()
    if out["date"].lt(start).any() or out["date"].gt(end).any():
        raise ValueError("official KRX index series contains rows outside the requested window")

    out = out.sort_values("date").reset_index(drop=True)
    base_level = float(out["index_level"].iloc[0])
    out["benchmark_daily_return"] = out["index_level"].pct_change(fill_method=None).fillna(0.0)
    out["benchmark_cum_return"] = out["index_level"] / base_level - 1.0
    out["benchmark_cum_return_compounded"] = (
        1.0 + out["benchmark_daily_return"]
    ).cumprod() - 1.0
    if not (
        out["benchmark_cum_return"]
        .sub(out["benchmark_cum_return_compounded"])
        .abs()
        .le(1e-12)
        .all()
    ):
        raise ValueError("official KRX index endpoint and compounded returns disagree")
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    out["benchmark_name"] = benchmark_name
    out["benchmark_identifier"] = index_identifier
    out["asset_type"] = BENCHMARK_ASSET_TYPE
    out["return_type"] = BENCHMARK_RETURN_TYPE
    out["source"] = BENCHMARK_SOURCE
    out["index_master_market"] = INDEX_MASTER_MARKET
    return out


def request_official_index_series_response(
    stock_api,
    *,
    index_identifier: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    if str(index_identifier).strip() in FORBIDDEN_ETF_IDENTIFIERS:
        raise RuntimeError("ETF identifier is forbidden for an INDEX benchmark")
    start_ymd = pd.Timestamp(start_date).strftime("%Y%m%d")
    end_ymd = pd.Timestamp(end_date).strftime("%Y%m%d")
    return stock_api.get_index_ohlcv_by_date(
        start_ymd,
        end_ymd,
        str(index_identifier).strip(),
        name_display=False,
    )


def fetch_official_index_series(
    stock_api,
    *,
    index_identifier: str,
    benchmark_name: str,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    frame = request_official_index_series_response(
        stock_api,
        index_identifier=index_identifier,
        start_date=start_date,
        end_date=end_date,
    )
    return normalize_official_index_frame(
        frame,
        index_identifier=str(index_identifier).strip(),
        benchmark_name=benchmark_name,
        start_date=start_date,
        end_date=end_date,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Collect the official KRX 300 price-index series from KRX"
    )
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--benchmark_name", default=BENCHMARK_NAME)
    parser.add_argument("--output_csv", required=True)
    parser.add_argument("--raw-master-json")
    parser.add_argument("--raw-series-csv")
    parser.add_argument("--mode", choices=("production", "standard"), default="standard")
    args = parser.parse_args()

    start = pd.Timestamp(args.start).normalize()
    end = pd.Timestamp(args.end).normalize()
    if end < start:
        raise ValueError(f"end date must be >= start date: start={args.start} end={args.end}")
    if args.benchmark_name != BENCHMARK_NAME:
        raise ValueError(
            f"this adapter is fixed to the exact official index name {BENCHMARK_NAME!r}"
        )
    if args.mode == "production":
        require_krx_credentials()

    output_csv = Path(args.output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    default_raw_master, default_raw_series = default_raw_artifact_paths(output_csv)
    raw_master_json = Path(args.raw_master_json) if args.raw_master_json else default_raw_master
    raw_series_csv = Path(args.raw_series_csv) if args.raw_series_csv else default_raw_series
    meta_path = output_csv.with_suffix(".meta.json")
    artifact_paths = (output_csv, raw_master_json, raw_series_csv, meta_path)
    if len({path.resolve() for path in artifact_paths}) != len(artifact_paths):
        raise ValueError("normalized, metadata, and raw benchmark artifacts must use distinct paths")
    existing = [str(path) for path in artifact_paths if path.exists()]
    if existing:
        raise FileExistsError(f"refusing to overwrite immutable benchmark artifacts: {existing}")
    meta: dict[str, Any] = {
        "benchmark_name": BENCHMARK_NAME,
        "benchmark_identifier": None,
        "asset_type": BENCHMARK_ASSET_TYPE,
        "return_type": BENCHMARK_RETURN_TYPE,
        "nav_dividend_treatment": NAV_DIVIDEND_TREATMENT,
        "source": BENCHMARK_SOURCE,
        "source_provider": "Korea Exchange (KRX)",
        "index_master_market": INDEX_MASTER_MARKET,
        "index_master_endpoint": INDEX_MASTER_ENDPOINT,
        "index_series_endpoint": INDEX_SERIES_ENDPOINT,
        "source_endpoint": KRX_JSON_ENDPOINT,
        "source_endpoint_bld": f"dbms/MDC/STAT/standard/{INDEX_SERIES_ENDPOINT}",
        "index_master_lookup_date": end.strftime("%Y-%m-%d"),
        "requested_start": start.strftime("%Y-%m-%d"),
        "requested_end": end.strftime("%Y-%m-%d"),
        "normalized_artifact_path": str(output_csv.resolve()),
        "normalized_artifact_sha256": None,
        "provisional": False,
        "fallback_source_detail": None,
        "fallback_source_path": None,
        "collection_status": "RUNNING",
        "collection_started_at": _now_utc(),
        "collection_timestamp": None,
        "raw_index_master_artifact_path": str(raw_master_json.resolve()),
        "raw_index_master_artifact_sha256": None,
        "raw_index_series_artifact_path": str(raw_series_csv.resolve()),
        "raw_index_series_artifact_sha256": None,
        "raw_artifacts": {},
        "fill_used": False,
        "credentials_present": {
            "KRX_ID": bool(os.getenv("KRX_ID")),
            "KRX_PW": bool(os.getenv("KRX_PW")),
        },
    }

    try:
        # The locally installed 2026-session compatibility layer prints login
        # diagnostics (including the login identifier) while ``pykrx`` is
        # imported.  Credentials must never enter immutable run logs, so keep
        # third-party import chatter private and expose only our redacted
        # exception path below.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            from pykrx import stock

        index_identifier, matches, master_response = collect_official_index_master_response(
            stock, end.strftime("%Y-%m-%d"), BENCHMARK_NAME
        )
        _write_new_text(
            raw_master_json,
            json.dumps(master_response, ensure_ascii=False, indent=2),
        )
        raw_series = request_official_index_series_response(
            stock,
            index_identifier=index_identifier,
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
        )
        if raw_series is None or len(raw_series) == 0:
            raise RuntimeError("official KRX index series returned no rows")
        _write_new_csv(raw_series, raw_series_csv, index=True)
        out = normalize_official_index_frame(
            raw_series,
            index_identifier=index_identifier,
            benchmark_name=BENCHMARK_NAME,
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
        )
        meta.update(
            {
                "benchmark_identifier": index_identifier,
                "index_master_exact_matches": matches,
                "rows": int(len(out)),
                "first_date": str(out.iloc[0]["date"]),
                "last_date": str(out.iloc[-1]["date"]),
                "collection_status": "OFFICIAL_INDEX_SUCCESS",
            }
        )
        _write_new_csv(out, output_csv, index=False)
        raw_artifacts = {
            "index_master": {
                "path": str(raw_master_json.resolve()),
                "sha256": _sha256(raw_master_json),
                "format": "JSON",
                "adapter_method": "pykrx.stock.get_index_ticker_list/get_index_ticker_name",
                "source_endpoint_bld": f"dbms/MDC/STAT/standard/{INDEX_MASTER_ENDPOINT}",
            },
            "index_series": {
                "path": str(raw_series_csv.resolve()),
                "sha256": _sha256(raw_series_csv),
                "format": "CSV",
                "adapter_method": "pykrx.stock.get_index_ohlcv_by_date",
                "source_endpoint_bld": f"dbms/MDC/STAT/standard/{INDEX_SERIES_ENDPOINT}",
            },
        }
        meta.update(
            {
                "normalized_artifact_sha256": _sha256(output_csv),
                "raw_index_master_artifact_sha256": raw_artifacts["index_master"]["sha256"],
                "raw_index_series_artifact_sha256": raw_artifacts["index_series"]["sha256"],
                "raw_artifacts": raw_artifacts,
                "collection_timestamp": _now_utc(),
            }
        )
        meta_path = _write_meta(output_csv, meta)
    except Exception as exc:
        meta.update(
            {
                "collection_status": "FAILED_OFFICIAL_INDEX_REQUIRED",
                "error_type": type(exc).__name__,
                "error": _redact_process_credentials(exc),
                "collection_timestamp": _now_utc(),
            }
        )
        meta_path = _write_meta(output_csv, meta)
        raise RuntimeError(
            f"official KRX 300 index collection failed; no ETF or local fallback is allowed; "
            f"metadata={meta_path}"
        ) from exc

    print(f"[OK] official KRX 300 price index: {output_csv}")
    print(f"[OK] benchmark metadata: {meta_path}")
    print(f"[INFO] asset_type={BENCHMARK_ASSET_TYPE} return_type={BENCHMARK_RETURN_TYPE}")
    print(f"[INFO] rows={len(out)} first_date={out.iloc[0]['date']} last_date={out.iloc[-1]['date']}")


if __name__ == "__main__":
    main()
