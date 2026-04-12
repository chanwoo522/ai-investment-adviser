from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


FLOW_COLS = ["Revenue", "OpIncome", "NetIncome", "CFO", "CAPEX"]

PANEL_TO_ENGINE_COLS = {
    "assets": "Assets",
    "equity": "Equity",
    "liabilities": "Liabilities",
    "revenue": "Revenue",
    "op_income": "OpIncome",
    "net_income": "NetIncome",
    "cfo": "CFO",
    "capex": "CAPEX",
}


def compute_ttm_from_cumulative(df: pd.DataFrame, col: str, out_prefix: str) -> pd.DataFrame:
    """
    기존 build_factors_ttm_acc2.py와 동일한 방식.

    누적 flow 기준:
      Q4 TTM = 당해 Q4 cumulative
      Q1/Q2/Q3 TTM = 당해 cumulative + 전년도 Q4 cumulative - 전년도 같은 분기 cumulative
    """
    work = df[["ticker", "year", "quarter", col]].copy()

    prev_same = work.copy()
    prev_same["year"] = prev_same["year"] + 1
    prev_same = prev_same.rename(columns={col: f"{col}_prev_same_q"})

    prev_fy = work[work["quarter"] == 4].copy()
    prev_fy["year"] = prev_fy["year"] + 1
    prev_fy = prev_fy.rename(columns={col: f"{col}_prev_fy"}).drop(columns=["quarter"])

    work = work.merge(prev_same, on=["ticker", "year", "quarter"], how="left")
    work = work.merge(prev_fy, on=["ticker", "year"], how="left")

    cur = pd.to_numeric(work[col], errors="coerce")
    prev_same_q = pd.to_numeric(work[f"{col}_prev_same_q"], errors="coerce")
    prev_fy_q4 = pd.to_numeric(work[f"{col}_prev_fy"], errors="coerce")

    work[f"{out_prefix}_ttm"] = np.where(
        work["quarter"].eq(4),
        cur,
        cur + prev_fy_q4 - prev_same_q,
    )

    prev_ttm = work[["ticker", "year", "quarter", f"{out_prefix}_ttm"]].copy()
    prev_ttm["year"] = prev_ttm["year"] + 1
    prev_ttm = prev_ttm.rename(columns={f"{out_prefix}_ttm": f"{out_prefix}_ttm_prev"})
    work = work.merge(prev_ttm, on=["ticker", "year", "quarter"], how="left")

    numer = pd.to_numeric(work[f"{out_prefix}_ttm"], errors="coerce")
    denom = pd.to_numeric(work[f"{out_prefix}_ttm_prev"], errors="coerce")
    work[f"{out_prefix}_ttm_yoy"] = np.where(
        denom.abs() > 1e-12,
        numer / denom - 1.0,
        np.nan,
    )

    prev_yoy = work[["ticker", "year", "quarter", f"{out_prefix}_ttm_yoy"]].copy()
    prev_yoy["year"] = prev_yoy["year"] + 1
    prev_yoy = prev_yoy.rename(columns={f"{out_prefix}_ttm_yoy": f"{out_prefix}_ttm_yoy_prev"})
    work = work.merge(prev_yoy, on=["ticker", "year", "quarter"], how="left")

    work[f"{out_prefix}_acc2"] = (
        pd.to_numeric(work[f"{out_prefix}_ttm_yoy"], errors="coerce")
        - pd.to_numeric(work[f"{out_prefix}_ttm_yoy_prev"], errors="coerce")
    )

    return work[
        ["ticker", "year", "quarter", f"{out_prefix}_ttm", f"{out_prefix}_ttm_yoy", f"{out_prefix}_acc2"]
    ]


def winsorize_by_group(
    df: pd.DataFrame,
    cols: list[str],
    group_cols: list[str],
    lower_q: float = 0.01,
    upper_q: float = 0.99,
    min_obs: int = 20,
) -> pd.DataFrame:
    """
    분기 단면 기준 winsorize.
    표본 수가 너무 적은 그룹은 그대로 둔다.
    """
    out = df.copy()

    for col in cols:
        if col not in out.columns:
            continue

        def _clip(x: pd.Series) -> pd.Series:
            s = pd.to_numeric(x, errors="coerce")
            if s.notna().sum() < min_obs:
                return s
            lo = s.quantile(lower_q)
            hi = s.quantile(upper_q)
            return s.clip(lower=lo, upper=hi)

        out[col] = out.groupby(group_cols)[col].transform(_clip)

    return out


def add_cross_sectional_zscores(
    df: pd.DataFrame,
    cols: list[str],
    group_cols: list[str],
    suffix: str = "__z",
) -> pd.DataFrame:
    """
    있으면 나중에 분석/디버깅에 도움 되는 분기 단면 z-score.
    실제 운용에서 바로 쓰지 않아도 진단용으로 좋다.
    """
    out = df.copy()

    for col in cols:
        if col not in out.columns:
            continue

        def _z(x: pd.Series) -> pd.Series:
            s = pd.to_numeric(x, errors="coerce")
            mu = s.mean()
            sd = s.std(ddof=0)
            if pd.isna(sd) or sd <= 1e-12:
                return pd.Series(np.nan, index=s.index)
            return (s - mu) / sd

        out[f"{col}{suffix}"] = out.groupby(group_cols)[col].transform(_z)

    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--in_v", type=int, default=3)
    ap.add_argument("--out_v", type=int, default=5)
    ap.add_argument("--winsor_lower", type=float, default=0.01)
    ap.add_argument("--winsor_upper", type=float, default=0.99)
    ap.add_argument("--winsor_min_obs", type=int, default=20)
    ap.add_argument("--add_zscores", action="store_true")
    args = ap.parse_args()

    in_path = Path(
        f"data/intermediate/fundamentals_panel/"
        f"fundamentals_panel__asof={args.asof}__src=dart_raw_canonical_ticker__v={args.in_v}.parquet"
    )

    out_path = Path(
        f"data/features/factor_matrix/"
        f"features_from_panel__asof={args.asof}__v={args.out_v}.parquet"
    )

    df = pd.read_parquet(in_path)
    print(f"[INFO] loaded rows: {len(df)}")

    # ticker 없는 건 제외
    df = df[df["ticker"].notnull()].copy()
    print(f"[INFO] after ticker filter: {len(df)}")

    df["ticker"] = df["ticker"].astype(str).str.zfill(6)
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
    df["quarter"] = pd.to_numeric(df["quarter"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["ticker", "year", "quarter"]).copy()
    df["year"] = df["year"].astype(int)
    df["quarter"] = df["quarter"].astype(int)

    # CFS 우선, 없으면 OFS fallback
    if "fs" in df.columns:
        df["fs_rank"] = np.where(df["fs"].astype(str).str.upper() == "CFS", 0, 1)
        before = len(df)
        df = (
            df.sort_values(["ticker", "year", "quarter", "fs_rank"])
            .drop_duplicates(subset=["ticker", "year", "quarter"], keep="first")
            .copy()
        )
        print(f"[INFO] after fs dedupe (CFS preferred): {len(df)} (removed {before - len(df)})")
        df = df.drop(columns=["fs_rank"], errors="ignore")

    # 기존 엔진 호환 컬럼명 생성
    for src, dst in PANEL_TO_ENGINE_COLS.items():
        if src in df.columns and dst not in df.columns:
            df[dst] = pd.to_numeric(df[src], errors="coerce")

    # 정렬 및 키
    df = (
        df.sort_values(["ticker", "year", "quarter"])
        .drop_duplicates(["ticker", "year", "quarter"], keep="last")
        .copy()
    )
    df["quarter_key"] = df["year"] * 10 + df["quarter"]

    base_cols = ["ticker", "year", "quarter", "quarter_key"]
    optional_cols = [c for c in ["corp_code", "fs"] if c in df.columns]
    stock_keep = [c for c in ["Assets", "Equity", "Liabilities"] if c in df.columns]
    out = df[base_cols + optional_cols + stock_keep].copy()

    # Debt: 기존 로직과 동일
    if "Debt_to_Equity" not in df.columns and {"Liabilities", "Equity"}.issubset(df.columns):
        eq = pd.to_numeric(df["Equity"], errors="coerce")
        li = pd.to_numeric(df["Liabilities"], errors="coerce")
        eq_safe = eq.where(eq > 1e-12)
        df["Debt_to_Equity"] = li / eq_safe

    dte = pd.to_numeric(df["Debt_to_Equity"], errors="coerce")
    dte = dte.where(dte >= 0)   # 음수 leverage는 비정상 처리
    dte = dte.clip(upper=10)    # extreme 안정화
    df["Debt_to_Equity_log"] = np.log1p(dte)

    if {"Liabilities", "Equity"}.issubset(df.columns):
        out["Debt_to_Equity"] = df["Debt_to_Equity"]
        out["Debt_to_Equity_log"] = df["Debt_to_Equity_log"]

    # 핵심: 기존 build_factors_ttm_acc2.py와 동일한 TTM / YoY / acc2
    for base in FLOW_COLS:
        if base in df.columns:
            part = compute_ttm_from_cumulative(df, base, base)
            out = out.merge(part, on=["ticker", "year", "quarter"], how="left")

    # 비율들
    if {"CFO_ttm", "Assets"}.issubset(out.columns):
        assets = pd.to_numeric(out["Assets"], errors="coerce")
        out["Assets_ttm"] = assets
        out["CFO_to_Assets_ttm"] = pd.to_numeric(out["CFO_ttm"], errors="coerce") / assets.where(assets.abs() > 1e-12)

    if {"CAPEX_ttm", "Assets"}.issubset(out.columns):
        assets = pd.to_numeric(out["Assets"], errors="coerce")
        out["CAPEX_to_Assets_ttm"] = pd.to_numeric(out["CAPEX_ttm"], errors="coerce") / assets.where(assets.abs() > 1e-12)

    if {"OpIncome_ttm", "Revenue_ttm"}.issubset(out.columns):
        rev = pd.to_numeric(out["Revenue_ttm"], errors="coerce")
        op = pd.to_numeric(out["OpIncome_ttm"], errors="coerce")
        out["OpMargin_ttm"] = op / rev.where(rev.abs() > 1e-12)

    if {"OpIncome_ttm", "Assets"}.issubset(out.columns):
        assets = pd.to_numeric(out["Assets"], errors="coerce")
        op = pd.to_numeric(out["OpIncome_ttm"], errors="coerce")
        out["OpIncome_to_Assets_ttm"] = op / assets.where(assets.abs() > 1e-12)

    # 기존 전략 호환 컬럼
    out["Quality_CFO_to_Assets"] = out["CFO_to_Assets_ttm"] if "CFO_to_Assets_ttm" in out.columns else np.nan
    out["CFO_isnull"] = out["CFO_ttm"].isnull().astype(int) if "CFO_ttm" in out.columns else 1
    out["CFO_warn"] = (pd.to_numeric(out["CFO_ttm"], errors="coerce") < 0).astype(int) if "CFO_ttm" in out.columns else 0
    out["CFO_safe"] = (pd.to_numeric(out["CFO_ttm"], errors="coerce") > 0).astype(int) if "CFO_ttm" in out.columns else 0

    # -----------------------------
    # 추천 안정화: 분기 단면 winsorize
    # -----------------------------
    factor_cols = [
        "Revenue_ttm_yoy",
        "OpIncome_ttm_yoy",
        "NetIncome_ttm_yoy",
        "CFO_ttm_yoy",
        "CAPEX_ttm_yoy",
        "Revenue_acc2",
        "OpIncome_acc2",
        "NetIncome_acc2",
        "CFO_acc2",
        "CAPEX_acc2",
        "Debt_to_Equity_log",
        "Quality_CFO_to_Assets",
        "OpMargin_ttm",
        "OpIncome_to_Assets_ttm",
    ]
    factor_cols = [c for c in factor_cols if c in out.columns]

    out = winsorize_by_group(
        out,
        cols=factor_cols,
        group_cols=["year", "quarter"],
        lower_q=args.winsor_lower,
        upper_q=args.winsor_upper,
        min_obs=args.winsor_min_obs,
    )
    print("[INFO] winsorize applied")

    # 선택 옵션: 진단용 z-score
    if args.add_zscores:
        z_cols = [
            "Revenue_ttm_yoy",
            "OpIncome_ttm_yoy",
            "Revenue_acc2",
            "OpIncome_acc2",
            "Debt_to_Equity_log",
            "Quality_CFO_to_Assets",
        ]
        z_cols = [c for c in z_cols if c in out.columns]
        out = add_cross_sectional_zscores(out, z_cols, ["year", "quarter"])
        print("[INFO] cross-sectional z-scores added")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(out_path, index=False)

    print(f"[OK] saved: {out_path}")
    print(f"[INFO] rows: {len(out)}")
    print(f"[INFO] tickers: {out['ticker'].nunique(dropna=True)}")
    dup = out.duplicated(subset=["ticker", "year", "quarter"]).sum()
    print(f"[INFO] duplicate rows on ticker/year/quarter: {dup}")

    print("\n[CHECK]")
    show_cols = [c for c in [
        "ticker", "year", "quarter", "quarter_key", "corp_code", "fs",
        "Revenue_ttm_yoy", "OpIncome_ttm_yoy",
        "Revenue_acc2", "OpIncome_acc2",
        "Debt_to_Equity_log", "Quality_CFO_to_Assets", "CFO_isnull"
    ] if c in out.columns]
    print(out[show_cols].head().to_string())


if __name__ == "__main__":
    main()