from __future__ import annotations

import os
import time
import argparse
from pathlib import Path
from datetime import datetime
import pandas as pd

import OpenDartReader as ODR


# -----------------------------
# Paths
# -----------------------------
ROOT = Path(".")
DATA = ROOT / "data"
RAW  = DATA / "raw" / "dart"
PRO  = DATA / "processed"
LOGS = ROOT / "logs"


# -----------------------------
# Helpers
# -----------------------------
def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _log_path(asof: str) -> Path:
    LOGS.mkdir(parents=True, exist_ok=True)
    return LOGS / f"collect_fundamentals_quarterly__asof={asof}__ts={_ts()}.log"


def _write_log(msg: str, lp: Path):
    lp.parent.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    with open(lp, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line)


def _norm_date(asof: str) -> str:
    s = asof.strip().replace("-", "")
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"--asof must be YYYY-MM-DD or YYYYMMDD. got={asof}")
    return s


def _parquet_atomic(df: pd.DataFrame, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


def _safe_num(x):
    try:
        if x is None:
            return None
        s = str(x).strip()
        if s == "" or s.lower() == "nan":
            return None
        s = s.replace(",", "")
        return float(s)
    except Exception:
        return None


def _retry(fn, tries=4, base_sleep=0.8, lp: Path | None = None, desc: str = ""):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if lp:
                _write_log(f"[WARN] {desc} try={i+1}/{tries} err={type(e).__name__}: {e}", lp)
            if i == tries - 1:
                raise
            time.sleep(base_sleep * (2 ** i))


def _pick_amount(df: pd.DataFrame, sj_div: str, prefer_ids: list[str], prefer_names: list[str]) -> float | None:
    """
    df: OpenDartReader finstate_all output
    sj_div: 'BS' 'CIS' 'CF'
    prefer_ids: prioritized account_id list
    prefer_names: fallback keywords in account_nm
    """
    if df is None or len(df) == 0:
        return None

    # DART sj_div는 기업/보고서에 따라 'CIS'(포괄손익계산서) 대신 'IS'(손익계산서)로 내려오는 경우가 많음.
    # 특히 매출/영업이익/순이익은 IS에도 동일하게 존재하므로 CIS 우선, 없으면 IS로 자동 fallback.
    sj_divs = [sj_div]
    if sj_div == "CIS":
        sj_divs = ["CIS", "IS"]

    sub = df[df["sj_div"].isin(sj_divs)].copy()
    if len(sub) == 0:
        return None

    # 1) prefer account_id exact
    if "account_id" in sub.columns:
        for aid in prefer_ids:
            hit = sub[sub["account_id"] == aid]
            if len(hit) > 0:
                v = _safe_num(hit.iloc[0].get("thstrm_amount"))
                if v is not None:
                    return v

    # 2) fallback by account_nm contains
    if "account_nm" in sub.columns:
        names = sub["account_nm"].astype(str)
        for kw in prefer_names:
            hit = sub[names.str.contains(kw, na=False, regex=False)]
            if len(hit) > 0:
                # 첫 번째를 쓰되, 숫자 변환 가능한 것 우선
                for _, r in hit.iterrows():
                    v = _safe_num(r.get("thstrm_amount"))
                    if v is not None:
                        return v

    return None


def _ensure_cols(df: pd.DataFrame, cols: list[str]):
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return df


# -----------------------------
# DART Report Codes (누적)
# -----------------------------
REPRT_Q1 = "11013"  # 1분기보고서
REPRT_H1 = "11012"  # 반기보고서(상반기 누적)
REPRT_Q3 = "11014"  # 3분기보고서(1~3Q 누적)
REPRT_Y  = "11011"  # 사업보고서(연간 누적)


# -----------------------------
# Metric definitions (TTM용 원천 분기값)
# -----------------------------
METRICS = {
    "Revenue": {
        "sj_div": "CIS",
        "ids": ["ifrs-full_Revenue"],
        "names": ["매출", "수익"],
    },
    "OpIncome": {
        "sj_div": "CIS",
        "ids": ["dart_OperatingIncomeLoss"],
        "names": ["영업이익", "영업이익(손실)"],
    },
    "NetIncome": {
        "sj_div": "CIS",
        "ids": ["ifrs-full_ProfitLoss"],
        "names": ["당기순이익", "당기순이익(손실)"],
    },
    "CFO": {
        "sj_div": "CF",
        "ids": ["ifrs-full_CashFlowsFromUsedInOperatingActivities"],
        "names": ["영업활동순현금흐름"],
    },
    # CAPEX 근사: PPE + Intangible purchase (현금유출, 보통 +값으로 주는 경우 많음)
    "CAPEX_PPE": {
        "sj_div": "CF",
        "ids": ["ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"],
        "names": ["유형자산의 취득"],
    },
    "CAPEX_INT": {
        "sj_div": "CF",
        "ids": ["ifrs-full_PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities"],
        "names": ["무형자산의 취득"],
    },
    "Assets": {
        "sj_div": "BS",
        "ids": ["ifrs-full_Assets"],
        "names": ["자산총계"],
    },
    "Equity": {
        "sj_div": "BS",
        "ids": ["ifrs-full_Equity"],
        "names": ["자본총계"],
    },
    "Liabilities": {
        "sj_div": "BS",
        "ids": ["ifrs-full_Liabilities"],
        "names": ["부채총계"],
    },
}


def _load_universe(asof: str, lp: Path, universe_path: str | None = None) -> pd.DataFrame:
    """Load universe parquet.

    If universe_path is None, uses the legacy phase1 naming convention.
    """
    if universe_path is None:
        uni = PRO / f"universe__asof={asof}__src=phase1__mcapTop=0.7__trdBot=0.3__capQ=4__v=1.parquet"
    else:
        uni = Path(universe_path)

    if not uni.exists():
        raise FileNotFoundError(f"Universe not found: {uni}. Provide --universe or run build_universe.py first.")

    df = pd.read_parquet(uni)
    df["ticker"] = df["ticker"].astype(str).str.zfill(6)
    _write_log(f"[INFO] universe={uni} rows={len(df)}", lp)
    return df


def _corp_map(dart, lp: Path) -> pd.DataFrame:
    # corp_codes(): 상장사 포함 전체 법인코드 테이블
    def _fetch():
        return dart.corp_codes

    corp = _retry(_fetch, lp=lp, desc="dart.corp_codes")
    # 표준 컬럼: corp_code, stock_code, corp_name
    corp = corp.copy()
    corp["stock_code"] = corp["stock_code"].astype(str).str.zfill(6)
    corp = corp[corp["stock_code"].str.match(r"^\d{6}$", na=False)]
    corp = corp.drop_duplicates("stock_code")
    _write_log(f"[INFO] corp_codes rows={len(corp)}", lp)
    return corp[["corp_code", "stock_code", "corp_name"]]


def _apply_coverage_filter(df: pd.DataFrame, coverage_csv: str | None, lp: Path) -> pd.DataFrame:
    """Optionally filter tickers by a precheck coverage CSV.

    Expected columns: ticker, has_cfs (1/0). If has_cfs column is missing, no filtering.
    """
    if not coverage_csv:
        return df
    p = Path(coverage_csv)
    if not p.exists():
        raise FileNotFoundError(f"coverage_csv not found: {p}")
    cov = pd.read_csv(p)
    if "ticker" not in cov.columns:
        raise ValueError(f"coverage_csv missing column 'ticker': {p}")
    cov["ticker"] = cov["ticker"].astype(str).str.zfill(6)
    if "has_cfs" in cov.columns:
        keep = set(cov.loc[cov["has_cfs"].astype(int) == 1, "ticker"].tolist())
        before = len(df)
        df2 = df[df["ticker"].isin(keep)].copy()
        _write_log(f"[INFO] coverage filter applied: {before} -> {len(df2)} (has_cfs=1)", lp)
        return df2
    _write_log(f"[WARN] coverage_csv has no 'has_cfs' column; skipping filter: {p}", lp)
    return df


def _raw_cache_path(corp_code: str, year: int, reprt: str, fs_div: str, asof: str) -> Path:
    return RAW / f"finstate_all__corp={corp_code}__year={year}__reprt={reprt}__fs={fs_div}__asof={asof}.parquet"


def _fetch_finstate_all(dart, corp_code: str, year: int, reprt: str, fs_div: str, asof: str, force: bool, lp: Path):
    p = _raw_cache_path(corp_code, year, reprt, fs_div, asof)
    if (not force) and p.exists():
        return pd.read_parquet(p)

    def _call():
        return dart.finstate_all(corp_code, year, reprt_code=reprt, fs_div=fs_div)

    df = _retry(_call, lp=lp, desc=f"finstate_all corp={corp_code} year={year} reprt={reprt} fs={fs_div}")

    if df is None or len(df) == 0:
        # 캐시를 남겨서 재시도 시에도 빠르게 처리
        RAW.mkdir(parents=True, exist_ok=True)
        _parquet_atomic(pd.DataFrame(), p)
        return pd.DataFrame()

    _ensure_cols(df, ["sj_div", "account_id", "account_nm", "thstrm_amount"])
    _parquet_atomic(df, p)
    return df


def _extract_metrics(fin_df: pd.DataFrame) -> dict[str, float | None]:
    out = {}
    for k, spec in METRICS.items():
        v = _pick_amount(
            fin_df,
            sj_div=spec["sj_div"],
            prefer_ids=spec["ids"],
            prefer_names=spec["names"],
        )
        out[k] = v
    # CAPEX 합치기
    capex = None
    if out.get("CAPEX_PPE") is not None or out.get("CAPEX_INT") is not None:
        capex = (out.get("CAPEX_PPE") or 0.0) + (out.get("CAPEX_INT") or 0.0)
    out["CAPEX"] = capex
    return out


def _all_none(d: dict[str, float | None]) -> bool:
    return all(v is None for v in d.values())


def _diff(a: dict[str, float | None], b: dict[str, float | None]) -> dict[str, float | None]:
    # a - b (None 처리)
    out = {}
    keys = set(a.keys()) | set(b.keys())
    for k in keys:
        va, vb = a.get(k), b.get(k)
        if va is None and vb is None:
            out[k] = None
        elif va is None:
            out[k] = None
        elif vb is None:
            out[k] = None
        else:
            out[k] = va - vb
    return out


def _quarterize(cum_q1, cum_h1, cum_q3, cum_y):
    """
    입력: 누적 값 dict
    출력: 분기 값 dict Q1,Q2,Q3,Q4
    """
    q1 = cum_q1
    q2 = _diff(cum_h1, cum_q1)
    q3 = _diff(cum_q3, cum_h1)
    q4 = _diff(cum_y,  cum_q3)
    return q1, q2, q3, q4


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--universe", default=None, help="override universe parquet path")
    ap.add_argument("--coverage_csv", default=None, help="optional precheck CSV to filter tickers (expects columns ticker,has_cfs)")
    ap.add_argument("--no_fast_skip_empty_q1", dest="fast_skip_empty_q1", action="store_false", help="do not skip remaining reprt codes when Q1 is empty")
    ap.set_defaults(fast_skip_empty_q1=True)
    ap.add_argument("--start_year", type=int, default=None)
    ap.add_argument("--end_year", type=int, default=None)
    ap.add_argument("--fs_div", default="CFS", choices=["CFS", "OFS"], help="CFS=연결, OFS=별도")
    ap.add_argument("--force", action="store_true", help="re-download & rebuild raw caches")
    ap.add_argument("--sleep", type=float, default=0.25, help="api pacing seconds per call")
    ap.add_argument("--limit", type=int, default=None, help="debug: limit tickers")
    args = ap.parse_args()

    asof = args.asof
    asof_ymd = _norm_date(asof)
    lp = _log_path(asof)

    key = os.getenv("DART_API_KEY")
    if not key:
        raise RuntimeError("DART_API_KEY env var not set.")
    dart = ODR(key)

    uni = _load_universe(asof, lp, args.universe)
    corp = _corp_map(dart, lp)

    df = uni.merge(corp, left_on="ticker", right_on="stock_code", how="left")
    miss = df["corp_code"].isna().mean()
    _write_log(f"[CHECK] corp_code missing ratio={miss:.3f}", lp)

    # optional: filter by precheck coverage
    df = _apply_coverage_filter(df, args.coverage_csv, lp)

    if args.limit:
        df = df.head(args.limit)

    # year range: last 10y default
    asof_year = int(asof_ymd[:4])
    start_year = args.start_year if args.start_year is not None else asof_year - 10
    end_year   = args.end_year   if args.end_year   is not None else asof_year - 1
    _write_log(f"[INFO] year range {start_year}..{end_year}", lp)

    rows = []
    n = len(df)
    ok_cnt, skip_cnt, err_cnt = 0, 0, 0

    for i, r in df.iterrows():
        ticker = str(r["ticker"]).zfill(6)
        name   = r.get("name")
        corp_code = r.get("corp_code")

        if pd.isna(corp_code) or corp_code is None or str(corp_code).strip() == "":
            skip_cnt += 1
            continue

        corp_code = str(corp_code).zfill(8) if str(corp_code).isdigit() else str(corp_code)

        _write_log(f"[{i+1}/{n}] {ticker} {name} corp={corp_code}", lp)

        try:
            for y in range(start_year, end_year + 1):
                # 누적 4종을 받아서 분기화
                fin_q1 = _fetch_finstate_all(dart, corp_code, y, REPRT_Q1, args.fs_div, asof, args.force, lp)
                time.sleep(args.sleep)

                # ✅ 개선: Q1이 비어도 연간(Y)은 한번 확인
                if fin_q1 is None or len(fin_q1) == 0:
                    fin_y = _fetch_finstate_all(dart, corp_code, y, REPRT_Y, args.fs_div, asof, args.force, lp)
                    time.sleep(args.sleep)

                    # 연간도 비면 이 해는 스킵
                    if fin_y is None or len(fin_y) == 0:
                        continue

                    # 연간은 있으면: 최소한 Q4(=연간 누적)라도 기록(나머지 분기는 None)
                    cum_y = _extract_metrics(fin_y)
                    if _all_none(cum_y):
                        continue

                    rows.append({
                        "ticker": ticker,
                        "name": name,
                        "corp_code": corp_code,
                        "year": y,
                        "quarter": 4,
                        "asof": asof,
                        "fs_div": args.fs_div,
                        "Revenue": cum_y.get("Revenue"),
                        "OpIncome": cum_y.get("OpIncome"),
                        "NetIncome": cum_y.get("NetIncome"),
                        "CFO": cum_y.get("CFO"),
                        "CAPEX": cum_y.get("CAPEX"),
                        "Assets": cum_y.get("Assets"),
                        "Equity": cum_y.get("Equity"),
                        "Liabilities": cum_y.get("Liabilities"),
                    })
                    continue

                fin_h1 = _fetch_finstate_all(dart, corp_code, y, REPRT_H1, args.fs_div, asof, args.force, lp)
                time.sleep(args.sleep)
                fin_q3 = _fetch_finstate_all(dart, corp_code, y, REPRT_Q3, args.fs_div, asof, args.force, lp)
                time.sleep(args.sleep)
                fin_y  = _fetch_finstate_all(dart, corp_code, y, REPRT_Y,  args.fs_div, asof, args.force, lp)
                time.sleep(args.sleep)

                # If all reports are empty, skip this year entirely
                if (fin_q1 is None or len(fin_q1) == 0) and (fin_h1 is None or len(fin_h1) == 0) and (fin_q3 is None or len(fin_q3) == 0) and (fin_y is None or len(fin_y) == 0):
                    continue

                cum_q1 = _extract_metrics(fin_q1)

                cum_h1 = _extract_metrics(fin_h1)
                cum_q3 = _extract_metrics(fin_q3)
                cum_y  = _extract_metrics(fin_y)

                # If the annual cumulative is all None, skip (no meaningful financials)
                if _all_none(cum_y):
                    continue

                q1, q2, q3, q4 = _quarterize(cum_q1, cum_h1, cum_q3, cum_y)

                for q, dct in zip([1, 2, 3, 4], [q1, q2, q3, q4]):
                    rows.append({
                        "ticker": ticker,
                        "name": name,
                        "corp_code": corp_code,
                        "year": y,
                        "quarter": q,
                        "asof": asof,
                        "fs_div": args.fs_div,
                        "Revenue": dct.get("Revenue"),
                        "OpIncome": dct.get("OpIncome"),
                        "NetIncome": dct.get("NetIncome"),
                        "CFO": dct.get("CFO"),
                        "CAPEX": dct.get("CAPEX"),
                        "Assets": dct.get("Assets"),
                        "Equity": dct.get("Equity"),
                        "Liabilities": dct.get("Liabilities"),
                    })

            ok_cnt += 1

        except Exception as e:
            err_cnt += 1
            _write_log(f"[ERR] {ticker} {name} corp={corp_code} err={type(e).__name__}: {e}", lp)
            continue

    out_df = pd.DataFrame(rows)

    # 저장 (long format)
    out_path = PRO / f"fundamentals_quarterly__asof={asof}__src=dart__fs={args.fs_div}__y={start_year}-{end_year}__v=1.parquet"
    _parquet_atomic(out_df, out_path)
    _write_log(f"[OK] saved: {out_path} rows={len(out_df)} tickers_ok={ok_cnt} skipped={skip_cnt} err={err_cnt}", lp)

    # 간단 검증
    if len(out_df) > 0:
        _write_log(f"[CHECK] null ratios:", lp)
        for c in ["Revenue","OpIncome","NetIncome","CFO","CAPEX","Assets","Equity"]:
            _write_log(f"  - {c}: {out_df[c].isna().mean():.3f}", lp)


if __name__ == "__main__":
    main()
