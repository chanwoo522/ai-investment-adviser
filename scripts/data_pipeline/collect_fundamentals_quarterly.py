from __future__ import annotations

import os
import time
import argparse
import re
from pathlib import Path
from datetime import datetime
import pandas as pd
import OpenDartReader as ODR

ROOT = Path(".")
DATA = ROOT / "data"
RAW = DATA / "raw" / "dart"
PRO = DATA / "processed"
LOGS = ROOT / "logs"

REPRT_Q1 = "11013"
REPRT_H1 = "11012"
REPRT_Q3 = "11014"
REPRT_Y = "11011"

FLOW_METRICS = {
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
}

STOCK_METRICS = {
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

ALL_METRICS = {**FLOW_METRICS, **STOCK_METRICS}
FLOW_KEYS = ["Revenue", "OpIncome", "NetIncome", "CFO", "CAPEX"]
STOCK_KEYS = ["Assets", "Equity", "Liabilities"]


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
        return float(s.replace(",", ""))
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


def _ensure_cols(df: pd.DataFrame, cols: list[str]):
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return df


def _normalize_ticker_series(s: pd.Series) -> pd.Series:
    return s.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)


def _extract_asof_from_name(name: str) -> str | None:
    m = re.search(r"__asof=(\d{4}-\d{2}-\d{2})__", name)
    return m.group(1) if m else None


def _read_any_table(p: Path) -> pd.DataFrame:
    if p.suffix.lower() == ".csv":
        return pd.read_csv(p)
    if p.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(p)
    return pd.read_parquet(p)


def _pick_amount(
    df: pd.DataFrame,
    sj_div: str,
    prefer_ids: list[str],
    prefer_names: list[str],
    amount_cols: list[str] | None = None,
) -> float | None:
    if df is None or len(df) == 0:
        return None

    sj_divs = [sj_div]
    if sj_div == "CIS":
        sj_divs = ["CIS", "IS"]

    sub = df[df["sj_div"].isin(sj_divs)].copy()
    if len(sub) == 0:
        return None

    amount_cols = amount_cols or ["thstrm_amount"]
    amount_cols = [c for c in amount_cols if c in sub.columns] or ["thstrm_amount"]

    def _first_amount(row) -> float | None:
        for c in amount_cols:
            v = _safe_num(row.get(c))
            if v is not None:
                return v
        return None

    if "account_id" in sub.columns:
        for aid in prefer_ids:
            hit = sub[sub["account_id"] == aid]
            if len(hit) > 0:
                v = _first_amount(hit.iloc[0])
                if v is not None:
                    return v

    if "account_nm" in sub.columns:
        names = sub["account_nm"].astype(str)
        for kw in prefer_names:
            hit = sub[names.str.contains(kw, na=False, regex=False)]
            if len(hit) > 0:
                for _, r in hit.iterrows():
                    v = _first_amount(r)
                    if v is not None:
                        return v
    return None


def _flow_amount_cols_for_reprt(reprt: str) -> list[str]:
    if reprt == REPRT_Q1:
        return ["thstrm_amount", "thstrm_add_amount"]
    if reprt in {REPRT_H1, REPRT_Q3}:
        return ["thstrm_add_amount", "thstrm_amount"]
    if reprt == REPRT_Y:
        return ["thstrm_amount", "thstrm_add_amount"]
    return ["thstrm_amount", "thstrm_add_amount"]


def _extract_metrics(fin_df: pd.DataFrame, reprt: str) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    flow_amount_cols = _flow_amount_cols_for_reprt(reprt)
    stock_amount_cols = ["thstrm_amount", "thstrm_add_amount"]
    for k, spec in ALL_METRICS.items():
        amount_cols = flow_amount_cols if (k in FLOW_KEYS or k.startswith("CAPEX_")) else stock_amount_cols
        out[k] = _pick_amount(fin_df, spec["sj_div"], spec["ids"], spec["names"], amount_cols=amount_cols)

    if out.get("CAPEX_PPE") is not None or out.get("CAPEX_INT") is not None:
        out["CAPEX"] = (out.get("CAPEX_PPE") or 0.0) + (out.get("CAPEX_INT") or 0.0)
    else:
        out["CAPEX"] = None

    out.pop("CAPEX_PPE", None)
    out.pop("CAPEX_INT", None)
    return out


def _has_any(d: dict[str, float | None] | None, keys: list[str] | None = None) -> bool:
    if d is None:
        return False
    ks = keys if keys is not None else list(d.keys())
    return any(d.get(k) is not None for k in ks)


def _diff(a: dict[str, float | None], b: dict[str, float | None]) -> dict[str, float | None]:
    out = {}
    keys = set(a.keys()) | set(b.keys())
    for k in keys:
        va, vb = a.get(k), b.get(k)
        out[k] = None if va is None or vb is None else va - vb
    return out


def _make_quarter_row(
    flow_vals: dict[str, float | None] | None,
    stock_vals: dict[str, float | None] | None,
) -> dict[str, float | None] | None:
    out = {}
    if flow_vals is not None:
        out.update({k: flow_vals.get(k) for k in FLOW_KEYS})
    if stock_vals is not None:
        out.update({k: stock_vals.get(k) for k in STOCK_KEYS})
    return out if _has_any(out) else None


def _quarterize(cum_q1, cum_h1, cum_q3, cum_y):
    q1_flow = {k: cum_q1.get(k) for k in FLOW_KEYS} if _has_any(cum_q1, FLOW_KEYS) else None
    q2_flow = _diff(
        {k: cum_h1.get(k) for k in FLOW_KEYS},
        {k: cum_q1.get(k) for k in FLOW_KEYS},
    ) if _has_any(cum_h1, FLOW_KEYS) and _has_any(cum_q1, FLOW_KEYS) else None
    q3_flow = _diff(
        {k: cum_q3.get(k) for k in FLOW_KEYS},
        {k: cum_h1.get(k) for k in FLOW_KEYS},
    ) if _has_any(cum_q3, FLOW_KEYS) and _has_any(cum_h1, FLOW_KEYS) else None
    q4_flow = _diff(
        {k: cum_y.get(k) for k in FLOW_KEYS},
        {k: cum_q3.get(k) for k in FLOW_KEYS},
    ) if _has_any(cum_y, FLOW_KEYS) and _has_any(cum_q3, FLOW_KEYS) else (
        {k: cum_y.get(k) for k in FLOW_KEYS} if _has_any(cum_y, FLOW_KEYS) else None
    )

    q1_stock = {k: cum_q1.get(k) for k in STOCK_KEYS} if _has_any(cum_q1, STOCK_KEYS) else None
    q2_stock = {k: cum_h1.get(k) for k in STOCK_KEYS} if _has_any(cum_h1, STOCK_KEYS) else None
    q3_stock = {k: cum_q3.get(k) for k in STOCK_KEYS} if _has_any(cum_q3, STOCK_KEYS) else None
    q4_stock = {k: cum_y.get(k) for k in STOCK_KEYS} if _has_any(cum_y, STOCK_KEYS) else None

    return (
        _make_quarter_row(q1_flow, q1_stock),
        _make_quarter_row(q2_flow, q2_stock),
        _make_quarter_row(q3_flow, q3_stock),
        _make_quarter_row(q4_flow, q4_stock),
    )


def _discover_universe(asof: str) -> Path | None:
    candidates = [
        PRO / f"universe__asof={asof}__metric=revenue_op__v=1.parquet",
        PRO / f"universe__asof={asof}__metric=revenue_op__v=1.csv",
    ]
    candidates.extend(
        sorted(PRO.glob(f"universe__asof={asof}__src=phase1__mcap_top=*__trd_bot=*__v=*.parquet"), reverse=True)
    )
    for p in candidates:
        if p.exists():
            return p
    return None


def _load_universe(asof: str, lp: Path, universe_path: str | None = None) -> pd.DataFrame:
    uni = Path(universe_path) if universe_path else _discover_universe(asof)
    if uni is None or not uni.exists():
        raise FileNotFoundError(
            "Universe not found. Provide --universe or run build_universe.py / export_universe_csv.py first."
        )
    df = pd.read_csv(uni) if uni.suffix.lower() == ".csv" else pd.read_parquet(uni)
    df = df.copy()
    df["ticker"] = _normalize_ticker_series(df["ticker"])
    _write_log(f"[INFO] universe={uni} rows={len(df)}", lp)
    return df


def _collect_union_tickers(asof: str, metric: str, lp: Path) -> pd.DataFrame:
    frames = []
    patterns = [
        "krx_master__asof=*__src=pykrx__v=*.parquet",
        "krx_marketdata__asof=*__src=pykrx__lookback=365d__v=*.parquet",
        "fundamentals_quarterly__asof=*__src=dart__fs=CFS__y=*__v=*.parquet",
        "fundamentals_quarterly__asof=*__src=dart__fs=OFS__y=*__v=*.parquet",
        f"universe__asof=*__metric={metric}__v=*.csv",
        f"universe__asof=*__metric={metric}__v=*.parquet",
        f"features_live__asof=*__metric={metric}__v=*.parquet",
        "shares_industry__asof=*__src=dart__*.parquet",
    ]

    for pat in patterns:
        for p in PRO.glob(pat):
            a = _extract_asof_from_name(p.name)
            if a and a <= asof:
                try:
                    df = _read_any_table(p)
                    if "ticker" in df.columns:
                        x = df[["ticker"]].copy()
                        x["ticker"] = _normalize_ticker_series(x["ticker"])
                        frames.append(x)
                except Exception as e:
                    _write_log(f"[WARN] failed reading union source {p}: {e}", lp)

    p_cur = PRO / "my_current_holdings.csv"
    if p_cur.exists():
        try:
            cur = pd.read_csv(p_cur)
            if "ticker" in cur.columns:
                x = cur[["ticker"]].copy()
                x["ticker"] = _normalize_ticker_series(x["ticker"])
                frames.append(x)
        except Exception as e:
            _write_log(f"[WARN] failed reading current holdings {p_cur}: {e}", lp)

    if not frames:
        raise RuntimeError("No union ticker source available.")

    out = pd.concat(frames, ignore_index=True)
    out = out.dropna(subset=["ticker"]).drop_duplicates("ticker").copy()
    _write_log(f"[INFO] union tickers rows={len(out)}", lp)
    return out


def _load_explicit_tickers(tickers_arg: str | None, lp: Path) -> pd.DataFrame:
    if not tickers_arg:
        return pd.DataFrame(columns=["ticker"])
    toks = [x.strip() for x in str(tickers_arg).split(",") if str(x).strip()]
    if not toks:
        return pd.DataFrame(columns=["ticker"])
    df = pd.DataFrame({"ticker": toks})
    df["ticker"] = _normalize_ticker_series(df["ticker"])
    df = df.dropna(subset=["ticker"]).drop_duplicates("ticker")
    _write_log(f"[INFO] explicit tickers rows={len(df)}", lp)
    return df


def _build_input_tickers(
    asof: str,
    metric: str,
    lp: Path,
    mode: str,
    universe_path: str | None,
    tickers_arg: str | None,
) -> pd.DataFrame:
    explicit = _load_explicit_tickers(tickers_arg, lp)

    if len(explicit) > 0:
        _write_log("[INFO] explicit tickers provided -> restrict mode enabled", lp)
        return explicit.copy()

    parts = []

    if mode in {"universe", "union", "missing_only"}:
        try:
            uni = _load_universe(asof, lp, universe_path)
            parts.append(uni[["ticker"]].copy())
        except Exception as e:
            if mode == "universe":
                raise
            _write_log(f"[WARN] universe load skipped in mode={mode}: {e}", lp)

    if mode in {"union", "missing_only"}:
        parts.append(_collect_union_tickers(asof, metric, lp))

    if not parts:
        raise RuntimeError(f"No ticker inputs built for mode={mode}")

    out = pd.concat(parts, ignore_index=True)
    out["ticker"] = _normalize_ticker_series(out["ticker"])
    out = out.dropna(subset=["ticker"]).drop_duplicates("ticker").copy()
    _write_log(f"[INFO] input tickers rows={len(out)} mode={mode}", lp)
    return out


def _corp_map(dart, lp: Path) -> pd.DataFrame:
    corp = _retry(lambda: dart.corp_codes, lp=lp, desc="dart.corp_codes").copy()
    corp["stock_code"] = corp["stock_code"].astype(str).str.zfill(6)
    corp = corp[corp["stock_code"].str.match(r"^\d{6}$", na=False)].drop_duplicates("stock_code")
    _write_log(f"[INFO] corp_codes rows={len(corp)}", lp)
    return corp[["corp_code", "stock_code", "corp_name"]]


def _apply_coverage_filter(df: pd.DataFrame, coverage_csv: str | None, lp: Path) -> pd.DataFrame:
    if not coverage_csv:
        return df
    p = Path(coverage_csv)
    if not p.exists():
        raise FileNotFoundError(f"coverage_csv not found: {p}")
    cov = pd.read_csv(p)
    cov["ticker"] = _normalize_ticker_series(cov["ticker"])
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


def _fetch_finstate_all(
    dart,
    corp_code: str,
    year: int,
    reprt: str,
    fs_div: str,
    asof: str,
    force: bool,
    lp: Path,
):
    p = _raw_cache_path(corp_code, year, reprt, fs_div, asof)
    if (not force) and p.exists():
        return pd.read_parquet(p)

    df = _retry(
        lambda: dart.finstate_all(corp_code, year, reprt_code=reprt, fs_div=fs_div),
        lp=lp,
        desc=f"finstate_all corp={corp_code} year={year} reprt={reprt} fs={fs_div}",
    )
    if df is None or len(df) == 0:
        RAW.mkdir(parents=True, exist_ok=True)
        _parquet_atomic(pd.DataFrame(), p)
        return pd.DataFrame()

    _ensure_cols(df, ["sj_div", "account_id", "account_nm", "thstrm_amount", "thstrm_add_amount"])
    _parquet_atomic(df, p)
    return df


def _fetch_finstate_with_fallback(
    dart,
    corp_code: str,
    year: int,
    reprt: str,
    primary_fs: str,
    asof: str,
    force: bool,
    lp: Path,
):
    """
    개선된 fallback 순서:
    1. finstate_all(CFS)
    2. finstate(CFS)
    3. finstate_all(OFS)
    4. finstate(OFS)
    """

    def _try_finstate_all(fs):
        try:
            return _fetch_finstate_all(dart, corp_code, year, reprt, fs, asof, force, lp)
        except Exception:
            return pd.DataFrame()

    def _try_finstate(fs):
        try:
            df = dart.finstate(corp_code, year, reprt_code=reprt, fs_div=fs)
            if df is None or len(df) == 0:
                return pd.DataFrame()
            _ensure_cols(df, ["sj_div", "account_id", "account_nm", "thstrm_amount", "thstrm_add_amount"])
            return df
        except Exception:
            return pd.DataFrame()

    order = []
    if primary_fs == "CFS":
        order = ["CFS", "OFS"]
    else:
        order = ["OFS", "CFS"]

    for fs in order:
        # 1️⃣ finstate_all
        df = _try_finstate_all(fs)
        if df is not None and len(df) > 0:
            return df, fs

        # 2️⃣ finstate fallback
        df = _try_finstate(fs)
        if df is not None and len(df) > 0:
            _write_log(f"[INFO] finstate fallback used: corp={corp_code} year={year} reprt={reprt} fs={fs}", lp)
            return df, fs

    return pd.DataFrame(), None


def _required_reprts_for_missing_quarters(missing_quarters: set[int]) -> list[str]:
    req = set()
    for q in missing_quarters:
        if q == 1:
            req.add(REPRT_Q1)
        elif q == 2:
            req.update([REPRT_Q1, REPRT_H1])
        elif q == 3:
            req.update([REPRT_H1, REPRT_Q3])
        elif q == 4:
            req.update([REPRT_Q3, REPRT_Y])
    return [r for r in [REPRT_Q1, REPRT_H1, REPRT_Q3, REPRT_Y] if r in req]


def _existing_output_path(asof: str, fs_div: str, start_year: int, end_year: int) -> Path:
    return PRO / f"fundamentals_quarterly__asof={asof}__src=dart__fs={fs_div}__y={start_year}-{end_year}__v=1.parquet"


def _load_existing_rows(path: Path, lp: Path) -> pd.DataFrame:
    if not path.exists():
        _write_log(f"[INFO] no existing fundamentals cache at {path}", lp)
        return pd.DataFrame()
    df = pd.read_parquet(path)
    if "ticker" in df.columns:
        df["ticker"] = _normalize_ticker_series(df["ticker"])
    _write_log(f"[INFO] existing fundamentals rows={len(df)} from {path}", lp)
    return df


def _existing_quarter_map(existing: pd.DataFrame) -> dict[tuple[str, int], set[int]]:
    out: dict[tuple[str, int], set[int]] = {}
    if len(existing) == 0:
        return out

    tmp = existing.copy()
    tmp["ticker"] = _normalize_ticker_series(tmp["ticker"])
    tmp["year"] = pd.to_numeric(tmp["year"], errors="coerce").astype("Int64")
    tmp["quarter"] = pd.to_numeric(tmp["quarter"], errors="coerce").astype("Int64")
    tmp = tmp.dropna(subset=["ticker", "year", "quarter"]).copy()

    for (ticker, year), g in tmp.groupby(["ticker", "year"]):
        out[(str(ticker).zfill(6), int(year))] = set(g["quarter"].astype(int).tolist())
    return out


def _available_quarters_for_year(year: int, asof_ts: pd.Timestamp) -> set[int]:
    if year < asof_ts.year:
        return {1, 2, 3, 4}
    if year > asof_ts.year:
        return set()
    cutoff_quarter = int((asof_ts.month - 1) // 3 + 1)
    return set(range(1, cutoff_quarter + 1))


def _filter_available_quarters(df: pd.DataFrame, asof: str) -> pd.DataFrame:
    """
    Point-in-time safety:
    keep only quarters that should be observable by the given asof date.
    Example: asof=2025-11-16 -> allow 2025 Q1,Q2,Q3 and drop 2025 Q4.
    """
    if df is None or len(df) == 0:
        return df

    out = df.copy()
    asof_ts = pd.to_datetime(asof)
    cutoff_year = int(asof_ts.year)
    cutoff_quarter = int((asof_ts.month - 1) // 3 + 1)

    out["year"] = pd.to_numeric(out["year"], errors="coerce")
    out["quarter"] = pd.to_numeric(out["quarter"], errors="coerce")

    mask = (
        (out["year"] < cutoff_year) |
        ((out["year"] == cutoff_year) & (out["quarter"] <= cutoff_quarter))
    )
    return out.loc[mask].copy()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--universe", default=None)
    ap.add_argument("--coverage_csv", default=None)
    ap.add_argument("--start_year", type=int, default=None)
    ap.add_argument("--end_year", type=int, default=None)
    ap.add_argument("--fs_div", default="CFS", choices=["CFS", "OFS"])
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.25)
    ap.add_argument("--limit", type=int, default=None)

    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--mode", default="universe", choices=["universe", "union", "missing_only"])
    ap.add_argument("--tickers", default=None, help="comma-separated explicit tickers to fetch ONLY")
    ap.add_argument("--merge_existing", action="store_true", help="merge new rows into existing output parquet")
    ap.add_argument("--recent_n_years", type=int, default=None, help="override year range to recent N years ending at end_year")
    args = ap.parse_args()

    asof = args.asof
    asof_ymd = _norm_date(asof)
    lp = _log_path(asof)

    key = os.getenv("DART_API_KEY")
    if not key:
        raise RuntimeError("DART_API_KEY env var not set.")
    dart = ODR(key)

    asof_year = int(asof_ymd[:4])
    start_year = args.start_year if args.start_year is not None else asof_year - 10
    end_year = args.end_year if args.end_year is not None else asof_year - 1

    if args.recent_n_years is not None and int(args.recent_n_years) > 0:
        start_year = end_year - int(args.recent_n_years) + 1

    _write_log(f"[INFO] year range {start_year}..{end_year}", lp)
    _write_log(f"[INFO] mode={args.mode} metric={args.metric} merge_existing={args.merge_existing}", lp)
    _write_log(f"[INFO] primary fs_div={args.fs_div} (fallback enabled)", lp)

    inp = _build_input_tickers(
        asof=asof,
        metric=args.metric,
        lp=lp,
        mode=args.mode,
        universe_path=args.universe,
        tickers_arg=args.tickers,
    )

    corp = _corp_map(dart, lp)

    df = inp.merge(corp, left_on="ticker", right_on="stock_code", how="left")
    _write_log(f"[CHECK] corp_code missing ratio={df['corp_code'].isna().mean():.3f}", lp)

    if "name" not in df.columns:
        df["name"] = None
    if "corp_name" in df.columns:
        df["name"] = df["name"].where(df["name"].notna(), df["corp_name"])

    df = _apply_coverage_filter(df, args.coverage_csv, lp)

    before_cc = len(df)
    df = df[df["corp_code"].notna()].copy()
    _write_log(f"[INFO] corp_code usable rows: {before_cc} -> {len(df)}", lp)

    if args.limit:
        df = df.head(args.limit)
        _write_log(f"[INFO] limit applied rows={len(df)}", lp)

    out_path = _existing_output_path(asof, args.fs_div, start_year, end_year)
    existing = _load_existing_rows(out_path, lp)
    existing_qmap = _existing_quarter_map(existing)

    df_work = df[["ticker", "name", "corp_code"]].drop_duplicates().copy()

    if len(df_work) == 0:
        _write_log("[OK] nothing to fetch; no fundamentals work items", lp)
        if args.merge_existing and len(existing):
            _parquet_atomic(existing, out_path)
            _write_log(f"[OK] preserved existing: {out_path} rows={len(existing)}", lp)
        return

    rows = []
    ok_cnt = 0
    skip_cnt = 0
    err_cnt = 0
    asof_ts = pd.to_datetime(asof)

    for i, r in df_work.iterrows():
        ticker = str(r["ticker"]).zfill(6)
        name = r.get("name")
        corp_code = r.get("corp_code")

        if pd.isna(corp_code) or corp_code is None or str(corp_code).strip() == "":
            skip_cnt += 1
            continue

        corp_code = str(corp_code).zfill(8) if str(corp_code).isdigit() else str(corp_code)
        _write_log(f"[{i+1}/{len(df_work)}] {ticker} {name} corp={corp_code}", lp)

        try:
            years = list(range(start_year, end_year + 1))

            for y in years:
                allowed_quarters = _available_quarters_for_year(y, asof_ts)
                if len(allowed_quarters) == 0:
                    continue

                missing_quarters = set(allowed_quarters)
                if (not args.force) and args.mode == "missing_only":
                    existing_q = existing_qmap.get((ticker, y), set())
                    missing_quarters = set(allowed_quarters) - existing_q

                if len(missing_quarters) == 0:
                    continue

                needed_reprts = [REPRT_Q1, REPRT_H1, REPRT_Q3, REPRT_Y] if args.force else _required_reprts_for_missing_quarters(missing_quarters)

                fin_map: dict[str, pd.DataFrame] = {}
                fs_used_map: dict[str, str | None] = {}

                for reprt in needed_reprts:
                    fin_df, fs_used = _fetch_finstate_with_fallback(
                        dart, corp_code, y, reprt, args.fs_div, asof, args.force, lp
                    )
                    fin_map[reprt] = fin_df
                    fs_used_map[reprt] = fs_used
                    time.sleep(args.sleep)

                fin_q1 = fin_map.get(REPRT_Q1, pd.DataFrame())
                fin_h1 = fin_map.get(REPRT_H1, pd.DataFrame())
                fin_q3 = fin_map.get(REPRT_Q3, pd.DataFrame())
                fin_y = fin_map.get(REPRT_Y, pd.DataFrame())

                if all(fin is None or len(fin) == 0 for fin in [fin_q1, fin_h1, fin_q3, fin_y]):
                    continue

                empty = {k: None for k in list(FLOW_KEYS) + list(STOCK_KEYS)}
                cum_q1 = _extract_metrics(fin_q1, REPRT_Q1) if fin_q1 is not None and len(fin_q1) else empty
                cum_h1 = _extract_metrics(fin_h1, REPRT_H1) if fin_h1 is not None and len(fin_h1) else empty
                cum_q3 = _extract_metrics(fin_q3, REPRT_Q3) if fin_q3 is not None and len(fin_q3) else empty
                cum_y = _extract_metrics(fin_y, REPRT_Y) if fin_y is not None and len(fin_y) else empty
                
                all_cum = [cum_q1, cum_h1, cum_q3, cum_y]
                valid_metrics = [
                    v
                    for dct in all_cum
                    for v in [dct.get(k) for k in (FLOW_KEYS + STOCK_KEYS)]
                    if (v is not None) and (not pd.isna(v)) and (float(v) != 0.0)
                ]
                if len(valid_metrics) == 0:
                    _write_log(
                        f"[WARN] no usable metrics → skip: {ticker} {name} corp={corp_code} year={y}",
                        lp
                    )
                    continue
                
                qrows = _quarterize(cum_q1, cum_h1, cum_q3, cum_y)
                fs_map = {
                    1: fs_used_map.get(REPRT_Q1),
                    2: fs_used_map.get(REPRT_H1),
                    3: fs_used_map.get(REPRT_Q3),
                    4: fs_used_map.get(REPRT_Y),
                }

                for q, dct in zip([1, 2, 3, 4], qrows):
                    if q not in allowed_quarters:
                        continue
                    if q not in missing_quarters and not args.force:
                        continue
                    if dct is None:
                        continue
                    rows.append({
                        "ticker": ticker,
                        "name": name,
                        "corp_code": corp_code,
                        "year": y,
                        "quarter": q,
                        "asof": asof,
                        "fs_div": args.fs_div,
                        "fs_div_used": fs_map.get(q),
                        **{k: dct.get(k) for k in FLOW_KEYS + STOCK_KEYS},
                    })

            ok_cnt += 1

        except Exception as e:
            err_cnt += 1
            _write_log(f"[ERR] {ticker} {name} corp={corp_code} err={type(e).__name__}: {e}", lp)
            continue

    new_df = pd.DataFrame(rows)

    if args.merge_existing and len(existing) > 0:
        out_df = pd.concat([existing, new_df], ignore_index=True)
        if len(out_df) > 0:
            out_df["ticker"] = _normalize_ticker_series(out_df["ticker"])
            out_df = out_df.drop_duplicates(["ticker", "year", "quarter"], keep="last").copy()
    else:
        out_df = new_df.copy()

    out_df = _filter_available_quarters(out_df, asof)
    _parquet_atomic(out_df, out_path)
    _write_log(
        f"[OK] saved: {out_path} rows={len(out_df)} new_rows={len(new_df)} "
        f"tickers_ok={ok_cnt} skipped={skip_cnt} err={err_cnt}",
        lp,
    )

    if len(out_df) > 0:
        out_df = out_df.sort_values(["ticker", "year", "quarter"]).reset_index(drop=True)
        _write_log("[CHECK] quarter coverage:", lp)
        cov = out_df.groupby(["year", "quarter"])["ticker"].nunique()
        for idx, val in cov.items():
            _write_log(f"  - {idx}: {int(val)}", lp)


if __name__ == "__main__":
    main()
