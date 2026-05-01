from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import argparse
import json
import os
import time
from datetime import datetime
from typing import Optional

import OpenDartReader as ODR
import pandas as pd
import requests

from common.industry_map import load_industry_reference

PRO = Path("data/processed")


def _tag(asof: str) -> str:
    return asof.replace("-", "")


def _normalize_ticker_series(s: pd.Series) -> pd.Series:
    return s.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)


def _load_universe(asof: str, metric: Optional[str] = None) -> pd.DataFrame:
    """
    Load universe tickers. Prefer metric-tagged universe, else any universe for that asof.
    """
    metric_cands: list[Path] = []
    generic_cands: list[Path] = []
    if metric:
        metric_cands += sorted(PRO.glob(f"universe__asof={asof}__metric={metric}__v=*.csv"))
        metric_cands += sorted(PRO.glob(f"universe__asof={asof}__metric={metric}__v=*.parquet"))
    generic_cands += sorted(PRO.glob(f"universe__asof={asof}__*.csv"))
    generic_cands += sorted(PRO.glob(f"universe__asof={asof}__*.parquet"))
    cands = metric_cands if metric_cands else generic_cands
    if not cands:
        raise FileNotFoundError(f"universe not found for asof={asof} in {PRO}")

    p = cands[-1]
    df = pd.read_csv(p) if p.suffix.lower() == ".csv" else pd.read_parquet(p)
    if "ticker" not in df.columns:
        raise ValueError(f"universe missing ticker col. file={p} cols={list(df.columns)}")
    df["ticker"] = _normalize_ticker_series(df["ticker"])
    return df[["ticker"]].drop_duplicates()


def _corp_map_from_odr(dart: ODR) -> pd.DataFrame:
    corp_obj = getattr(dart, "corp_codes", None)
    if corp_obj is None:
        raise RuntimeError("OpenDartReader has no corp_codes attribute/function.")

    corp_df = corp_obj() if callable(corp_obj) else corp_obj
    if not isinstance(corp_df, pd.DataFrame):
        raise RuntimeError(f"corp_codes returned non-DataFrame: {type(corp_df)}")

    need = {"corp_code", "stock_code"}
    if not need.issubset(set(corp_df.columns)):
        raise ValueError(f"Unexpected corp_codes schema: cols={list(corp_df.columns)}")

    out = corp_df[["corp_code", "stock_code"]].copy()
    out["name"] = corp_df["corp_name"] if "corp_name" in corp_df.columns else None
    out = out.rename(columns={"stock_code": "ticker"})
    out["ticker"] = _normalize_ticker_series(out["ticker"])
    out["corp_code"] = out["corp_code"].astype(str)
    return out


def _retry_get_json(
    url: str,
    params: dict,
    *,
    tries: int,
    sleep_s: float,
    timeout_s: float,
) -> dict:
    last = None
    for i in range(max(1, tries)):
        try:
            r = requests.get(url, params=params, timeout=timeout_s)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if i < max(1, tries) - 1:
                time.sleep(sleep_s * (2**i))
    raise RuntimeError(f"GET failed: {url} last_err={last}")


def _is_network_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    if isinstance(exc, requests.RequestException):
        return True
    return "winerror 10013" in msg or "failed to establish a new connection" in msg


def _meta_path(outp: Path) -> Path:
    return outp.with_suffix(".meta.json")


def _failed_path(outp: Path) -> Path:
    return outp.with_suffix(".failed_tickers.csv")


def _partial_path(outp: Path) -> Path:
    return outp.with_name(outp.stem + "__partial" + outp.suffix)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_failed_csv(path: Path, rows: list[dict]) -> None:
    cols = ["ticker", "corp_code", "name", "stage", "error_type", "error_message", "is_network_error", "logged_at"]
    df = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    df.to_csv(path, index=False, encoding="utf-8-sig")


def _load_resume_rows(outp: Path) -> pd.DataFrame:
    partial_path = _partial_path(outp)
    if not partial_path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(partial_path)
    if "ticker" in df.columns:
        df["ticker"] = _normalize_ticker_series(df["ticker"])
    return df


def _apply_reference_industry(out: pd.DataFrame, industry_ref_csv: Optional[str]) -> pd.DataFrame:
    out2 = out.copy()
    ref = load_industry_reference(industry_ref_csv)
    if len(ref) > 0:
        out2 = out2.drop(columns=["industry_code", "industry_name", "industry4", "industry_source"], errors="ignore")
        out2 = out2.merge(ref, on="ticker", how="left")
        out2["industry_source"] = out2["industry_code"].notna().map(
            {True: "reference_by_ticker", False: "missing_reference"}
        )
    else:
        out2["industry_code"] = pd.NA
        out2["industry_name"] = pd.NA
        out2["industry_source"] = "reference_missing"

    if "industry_code" in out2.columns:
        out2["industry_code"] = out2["industry_code"].astype("string").str.extract(r"(\d+)")[0]
        bad = out2["industry_code"].notna() & ~out2["industry_code"].astype(str).str.len().eq(6)
        if bad.any():
            print(f"[WARN] non-6-digit industry_code rows dropped to NA: {int(bad.sum())}")
            out2.loc[bad, "industry_code"] = pd.NA
            out2.loc[bad, "industry_name"] = pd.NA
            out2.loc[bad, "industry_source"] = "invalid_reference_code"

    return out2


def _materialize_output(rows: list[dict], asof: str, with_industry: bool, industry_ref_csv: Optional[str]) -> pd.DataFrame:
    cols = ["ticker", "name", "corp_code", "bsns_year", "reprt_code", "se", "shares", "dart_induty_code", "induty_code"]
    out = pd.DataFrame(rows, columns=cols) if rows else pd.DataFrame(columns=cols)
    if len(out):
        out["ticker"] = _normalize_ticker_series(out["ticker"])
    out["asof"] = asof
    out["created_at"] = datetime.now().isoformat(timespec="seconds")
    if with_industry:
        out = _apply_reference_industry(out, industry_ref_csv)
    return out


def _build_meta(
    *,
    args: argparse.Namespace,
    outp: Path,
    requested_universe: int,
    rows_written: int,
    success_tickers: int,
    failed_rows: list[dict],
    status: str,
    endpoint_access_ok: Optional[bool],
    first_network_error: Optional[str],
    provisional: bool,
) -> dict:
    return {
        "asof": args.asof,
        "metric": args.metric,
        "output_path": str(outp),
        "partial_output_path": str(_partial_path(outp)),
        "failed_tickers_path": str(_failed_path(outp)),
        "collection_status": status,
        "requested_universe": int(requested_universe),
        "rows_written": int(rows_written),
        "success_tickers": int(success_tickers),
        "failed_ticker_records": int(len(failed_rows)),
        "failed_tickers_unique": int(len({r["ticker"] for r in failed_rows})) if failed_rows else 0,
        "request_timeout": args.request_timeout,
        "max_retries": args.max_retries,
        "backoff_base": args.backoff_base,
        "partial_save_every": args.partial_save_every,
        "resume_from_existing": bool(args.resume_from_existing),
        "endpoint_access_ok": endpoint_access_ok,
        "first_network_error": first_network_error,
        "provisional": bool(provisional),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }


def _persist_state(
    *,
    outp: Path,
    out_df: pd.DataFrame,
    failed_rows: list[dict],
    meta_payload: dict,
    final_write: bool,
) -> None:
    _write_failed_csv(_failed_path(outp), failed_rows)
    _write_json(_meta_path(outp), meta_payload)
    _partial_path(outp).parent.mkdir(parents=True, exist_ok=True)
    out_df.to_parquet(_partial_path(outp), index=False)
    if final_write:
        out_df.to_parquet(outp, index=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--bsns_year", type=int, default=None)
    ap.add_argument("--reprt_code", default="11011")
    ap.add_argument("--sleep", type=float, default=0.12)
    ap.add_argument("--out_v", type=int, required=True)
    ap.add_argument("--with_industry", action="store_true")
    ap.add_argument("--industry_ref_csv", default=None)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--request-timeout", type=float, default=10.0)
    ap.add_argument("--max-retries", type=int, default=2)
    ap.add_argument("--backoff-base", type=float, default=0.5)
    ap.add_argument("--partial-save-every", type=int, default=10)
    ap.add_argument("--resume-from-existing", action="store_true")
    args = ap.parse_args()

    key = os.getenv("DART_API_KEY")
    if not key:
        raise RuntimeError("DART_API_KEY env var not set. (PowerShell) $env:DART_API_KEY='...'; then rerun")

    asof = args.asof
    asof_year = int(_tag(asof)[:4])
    bsns_year = args.bsns_year if args.bsns_year is not None else (asof_year - 1)

    PRO.mkdir(parents=True, exist_ok=True)

    outp = PRO / f"shares_industry__asof={asof}__src=dart__y={bsns_year}__reprt={args.reprt_code}__v={args.out_v}.parquet"
    dart = ODR(key)
    uni = _load_universe(asof, metric=args.metric)
    cmap = _corp_map_from_odr(dart)
    df = uni.merge(cmap, on="ticker", how="left")
    df = df.dropna(subset=["corp_code"]).copy()

    if args.limit:
        df = df.head(args.limit)

    rows: list[dict] = []
    if args.resume_from_existing:
        resume_df = _load_resume_rows(outp)
        if len(resume_df):
            rows = resume_df[
                ["ticker", "name", "corp_code", "bsns_year", "reprt_code", "se", "shares", "dart_induty_code", "induty_code"]
            ].to_dict("records")
            done = set(resume_df["ticker"].astype(str).str.zfill(6))
            before = len(df)
            df = df.loc[~df["ticker"].astype(str).str.zfill(6).isin(done)].copy()
            print(f"[INFO] resume_from_existing enabled: loaded={len(resume_df)} remaining={len(df)} skipped={before - len(df)}")

    url_stock = "https://opendart.fss.or.kr/api/stockTotqySttus.json"
    url_comp = "https://opendart.fss.or.kr/api/company.json"
    failed_rows: list[dict] = []
    endpoint_access_ok: Optional[bool] = None
    first_network_error: Optional[str] = None
    status = "completed"
    total = len(df)

    for idx, (_, r) in enumerate(df.iterrows(), start=1):
        corp_code = str(r["corp_code"])
        ticker = str(r["ticker"]).zfill(6)
        name = r.get("name")
        print(f"[INFO] ticker_progress={idx}/{total} ticker={ticker} corp_code={corp_code} name={name}")

        shares = None
        se_used = None
        dart_induty_code = None
        network_error_seen = False

        try:
            js = _retry_get_json(
                url_stock,
                params={
                    "crtfc_key": key,
                    "corp_code": corp_code,
                    "bsns_year": str(bsns_year),
                    "reprt_code": args.reprt_code,
                },
                tries=args.max_retries,
                sleep_s=args.backoff_base,
                timeout_s=args.request_timeout,
            )
            endpoint_access_ok = True
            if js.get("status") == "000":
                lst = js.get("list", []) or []
                for item in lst:
                    v = item.get("istc_totqy")
                    if v is None:
                        continue
                    try:
                        shares = int(str(v).replace(",", ""))
                        se_used = item.get("se")
                        break
                    except Exception:
                        continue
            else:
                failed_rows.append(
                    {
                        "ticker": ticker,
                        "corp_code": corp_code,
                        "name": name,
                        "stage": "shares_status",
                        "error_type": "dart_status_nonzero",
                        "error_message": f"status={js.get('status')} message={js.get('message')}",
                        "is_network_error": False,
                        "logged_at": datetime.now().isoformat(timespec="seconds"),
                    }
                )
        except Exception as e:
            network_error_seen = _is_network_error(e)
            if network_error_seen:
                endpoint_access_ok = False
                if first_network_error is None:
                    first_network_error = str(e)
            failed_rows.append(
                {
                    "ticker": ticker,
                    "corp_code": corp_code,
                    "name": name,
                    "stage": "shares_request",
                    "error_type": type(e).__name__,
                    "error_message": str(e),
                    "is_network_error": network_error_seen,
                    "logged_at": datetime.now().isoformat(timespec="seconds"),
                }
            )
            print(f"[WARN] ticker={ticker} stage=shares_request err={type(e).__name__}: {e}")

        if args.with_industry:
            try:
                jc = _retry_get_json(
                    url_comp,
                    params={"crtfc_key": key, "corp_code": corp_code},
                    tries=args.max_retries,
                    sleep_s=args.backoff_base,
                    timeout_s=args.request_timeout,
                )
                if endpoint_access_ok is None:
                    endpoint_access_ok = True
                if jc.get("status") == "000":
                    dart_induty_code = jc.get("induty_code") or jc.get("industy_code")
                else:
                    failed_rows.append(
                        {
                            "ticker": ticker,
                            "corp_code": corp_code,
                            "name": name,
                            "stage": "company_status",
                            "error_type": "dart_status_nonzero",
                            "error_message": f"status={jc.get('status')} message={jc.get('message')}",
                            "is_network_error": False,
                            "logged_at": datetime.now().isoformat(timespec="seconds"),
                        }
                    )
            except Exception as e:
                is_net = _is_network_error(e)
                network_error_seen = network_error_seen or is_net
                if is_net:
                    endpoint_access_ok = False
                    if first_network_error is None:
                        first_network_error = str(e)
                failed_rows.append(
                    {
                        "ticker": ticker,
                        "corp_code": corp_code,
                        "name": name,
                        "stage": "company_request",
                        "error_type": type(e).__name__,
                        "error_message": str(e),
                        "is_network_error": is_net,
                        "logged_at": datetime.now().isoformat(timespec="seconds"),
                    }
                )
                print(f"[WARN] ticker={ticker} stage=company_request err={type(e).__name__}: {e}")

        rows.append(
            {
                "ticker": ticker,
                "name": name,
                "corp_code": corp_code,
                "bsns_year": bsns_year,
                "reprt_code": args.reprt_code,
                "se": se_used,
                "shares": shares,
                "dart_induty_code": dart_induty_code,
                "induty_code": dart_induty_code,
            }
        )

        out_df = _materialize_output(rows, asof, args.with_industry, args.industry_ref_csv)
        success_count = int(out_df["shares"].notna().sum()) if "shares" in out_df.columns else 0
        provisional = bool(network_error_seen or out_df["shares"].isna().any())
        meta_payload = _build_meta(
            args=args,
            outp=outp,
            requested_universe=len(uni),
            rows_written=len(out_df),
            success_tickers=success_count,
            failed_rows=failed_rows,
            status="in_progress",
            endpoint_access_ok=endpoint_access_ok,
            first_network_error=first_network_error,
            provisional=provisional,
        )
        if (idx % max(1, args.partial_save_every) == 0) or network_error_seen or (idx == total):
            _persist_state(outp=outp, out_df=out_df, failed_rows=failed_rows, meta_payload=meta_payload, final_write=False)
            print(f"[INFO] partial_saved rows={len(out_df)} path={_partial_path(outp)}")

        if network_error_seen:
            status = "failed_network_blocked"
            print(f"[ERROR] network failure detected at ticker={ticker}; fast-fail engaged")
            break

        time.sleep(args.sleep)

    out = _materialize_output(rows, asof, args.with_industry, args.industry_ref_csv)
    success_count = int(out["shares"].notna().sum()) if "shares" in out.columns else 0
    provisional = bool(status != "completed" or out["shares"].isna().any())
    meta_payload = _build_meta(
        args=args,
        outp=outp,
        requested_universe=len(uni),
        rows_written=len(out),
        success_tickers=success_count,
        failed_rows=failed_rows,
        status=status,
        endpoint_access_ok=endpoint_access_ok,
        first_network_error=first_network_error,
        provisional=provisional,
    )
    _persist_state(outp=outp, out_df=out, failed_rows=failed_rows, meta_payload=meta_payload, final_write=True)

    industry_null_ratio = float(out["industry_code"].isna().mean()) if "industry_code" in out.columns else 1.0
    print(f"[OK] saved: {outp}")
    print(f"[OK] failed_tickers: {_failed_path(outp)}")
    print(f"[OK] partial_output: {_partial_path(outp)}")
    print(
        f"[INFO] rows={len(out)} "
        f"tickers={out['ticker'].nunique() if 'ticker' in out.columns else 0} "
        f"shares_null_ratio={out['shares'].isna().mean() if 'shares' in out.columns else 1.0:.3f} "
        f"dart_induty_null_ratio={out['dart_induty_code'].isna().mean() if 'dart_induty_code' in out.columns else 1.0:.3f} "
        f"industry_code_null_ratio={industry_null_ratio:.3f}"
    )
    print(
        f"[INFO] success_tickers={success_count} "
        f"failed_ticker_records={len(failed_rows)} "
        f"endpoint_access_ok={endpoint_access_ok} "
        f"collection_status={status}"
    )

    if status != "completed":
        raise RuntimeError(
            f"DART endpoint access failed; fast-failed with provisional output. first_network_error={first_network_error}"
        )


if __name__ == "__main__":
    main()
