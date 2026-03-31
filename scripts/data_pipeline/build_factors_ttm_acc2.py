from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PRO = ROOT / "data" / "processed"

FLOW_COLS = ["Revenue", "OpIncome", "NetIncome", "CFO", "CAPEX"]
STOCK_COLS = ["Assets", "Equity", "Liabilities"]


def _extract_asof_from_name(name: str) -> str | None:
    m = re.search(r"__asof=(\d{4}-\d{2}-\d{2})__", name)
    return m.group(1) if m else None


def _extract_v_from_name(name: str) -> int | None:
    m = re.search(r"__v=(\d+)\.parquet$", name)
    return int(m.group(1)) if m else None


def _source_priority(name: str) -> int:
    if "__src=dart_merged__fs=CFS__" in name:
        return 0
    if "__src=dart__fs=CFS__" in name:
        return 1
    if "__src=dart__fs=OFS__" in name:
        return 2
    return 9


def _collect_candidate_paths(asof: str) -> list[Path]:
    pats = [
        "fundamentals_quarterly__asof=*__src=dart_merged__fs=CFS__y=*__v=*.parquet",
        "fundamentals_quarterly__asof=*__src=dart__fs=CFS__y=*__v=*.parquet",
        "fundamentals_quarterly__asof=*__src=dart__fs=OFS__y=*__v=*.parquet",
    ]
    out: list[Path] = []
    for pat in pats:
        for p in PRO.glob(pat):
            a = _extract_asof_from_name(p.name)
            if a and a <= asof:
                out.append(p)
    return sorted(set(out))


def _find_best_fundamentals(asof: str, in_v: int) -> Path:
    candidates = _collect_candidate_paths(asof)
    if not candidates:
        raise FileNotFoundError(f"No fundamentals_quarterly parquet found for asof<={asof}")

    scored: list[tuple[str, int, int, str, Path]] = []
    for p in candidates:
        a = _extract_asof_from_name(p.name)
        v = _extract_v_from_name(p.name)
        pri = _source_priority(p.name)
        exact_v = 1 if (v is not None and v == in_v) else 0
        scored.append((a or "", exact_v, -pri, p.name, p))

    scored = sorted(scored, key=lambda x: (x[0], x[1], x[2], x[3]))
    return scored[-1][4]


def _quarter_key(df: pd.DataFrame) -> pd.Series:
    return df["year"].astype(int) * 10 + df["quarter"].astype(int)


def _compute_ttm_from_quarterly_flow(df: pd.DataFrame, col: str, out_prefix: str) -> pd.DataFrame:
    work = df[["ticker", "year", "quarter", col]].copy()
    work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.sort_values(["ticker", "year", "quarter"]).copy()

    grp = work.groupby("ticker", group_keys=False)
    work[f"{out_prefix}_ttm"] = grp[col].transform(lambda s: s.rolling(4, min_periods=4).sum())

    prev_ttm = work[["ticker", "year", "quarter", f"{out_prefix}_ttm"]].copy()
    prev_ttm["year"] = prev_ttm["year"] + 1
    prev_ttm = prev_ttm.rename(columns={f"{out_prefix}_ttm": f"{out_prefix}_ttm_prev"})
    work = work.merge(prev_ttm, on=["ticker", "year", "quarter"], how="left")

    numer = pd.to_numeric(work[f"{out_prefix}_ttm"], errors="coerce")
    denom = pd.to_numeric(work[f"{out_prefix}_ttm_prev"], errors="coerce")
    work[f"{out_prefix}_ttm_yoy"] = np.where(denom.abs() > 1e-12, numer / denom - 1.0, np.nan)

    prev_yoy = work[["ticker", "year", "quarter", f"{out_prefix}_ttm_yoy"]].copy()
    prev_yoy["year"] = prev_yoy["year"] + 1
    prev_yoy = prev_yoy.rename(columns={f"{out_prefix}_ttm_yoy": f"{out_prefix}_ttm_yoy_prev"})
    work = work.merge(prev_yoy, on=["ticker", "year", "quarter"], how="left")

    work[f"{out_prefix}_acc2"] = (
        pd.to_numeric(work[f"{out_prefix}_ttm_yoy"], errors="coerce")
        - pd.to_numeric(work[f"{out_prefix}_ttm_yoy_prev"], errors="coerce")
    )

    work[f"{out_prefix}_ttm_ready"] = work[f"{out_prefix}_ttm"].notna().astype("int8")
    work[f"{out_prefix}_ttm_yoy_ready"] = work[f"{out_prefix}_ttm_yoy"].notna().astype("int8")
    work[f"{out_prefix}_acc2_ready"] = work[f"{out_prefix}_acc2"].notna().astype("int8")

    reason = np.where(
        work[f"{out_prefix}_acc2"].notna(),
        "",
        np.where(
            work[f"{out_prefix}_ttm_yoy"].isna(),
            "missing_ttm_yoy",
            np.where(work[f"{out_prefix}_ttm_yoy_prev"].isna(), "missing_prev_yoy", "other"),
        ),
    )
    work[f"{out_prefix}_acc2_missing_reason"] = pd.Series(reason, index=work.index).replace("", pd.NA)

    return work[
        [
            "ticker",
            "year",
            "quarter",
            f"{out_prefix}_ttm",
            f"{out_prefix}_ttm_yoy",
            f"{out_prefix}_acc2",
            f"{out_prefix}_ttm_ready",
            f"{out_prefix}_ttm_yoy_ready",
            f"{out_prefix}_acc2_ready",
            f"{out_prefix}_acc2_missing_reason",
        ]
    ]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--in_v", type=int, default=1)
    ap.add_argument("--out_v", type=int, default=2)
    ap.add_argument("--input_parquet", default=None)
    args = ap.parse_args()

    if args.input_parquet:
        p_in = Path(args.input_parquet)
        if not p_in.exists():
            raise FileNotFoundError(f"input parquet not found: {p_in}")
    else:
        p_in = _find_best_fundamentals(args.asof, args.in_v)

    df = pd.read_parquet(p_in).copy()
    required = ["ticker", "year", "quarter"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns in fundamentals input: {missing}")

    df["ticker"] = df["ticker"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
    df["quarter"] = pd.to_numeric(df["quarter"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["ticker", "year", "quarter"]).copy()
    df["year"] = df["year"].astype(int)
    df["quarter"] = df["quarter"].astype(int)

    dedup_cols = [c for c in ["ticker", "year", "quarter"] if c in df.columns]
    df = df.sort_values(dedup_cols).drop_duplicates(dedup_cols, keep="last").copy()
    df["quarter_key"] = _quarter_key(df)

    if "Debt_to_Equity" not in df.columns and {"Liabilities", "Equity"}.issubset(df.columns):
        eq = pd.to_numeric(df["Equity"], errors="coerce")
        li = pd.to_numeric(df["Liabilities"], errors="coerce")
        eq_safe = eq.where(eq.abs() > 1e-12)
        df["Debt_to_Equity"] = li / eq_safe

    if "Debt_to_Equity" in df.columns:
        dte = pd.to_numeric(df["Debt_to_Equity"], errors="coerce")
        dte = dte.where(dte >= 0)
        dte = dte.clip(upper=10)
        df["Debt_to_Equity_log"] = np.log1p(dte)

    base_cols = ["ticker", "year", "quarter", "quarter_key"]
    optional_cols = [c for c in ["name", "corp_code", "fs_div", "fs_div_used"] if c in df.columns]
    stock_keep = [c for c in STOCK_COLS if c in df.columns]
    out = df[base_cols + optional_cols + stock_keep].copy()

    if {"Liabilities", "Equity"}.issubset(df.columns):
        out["Debt_to_Equity"] = df["Debt_to_Equity"]
        out["Debt_to_Equity_log"] = df["Debt_to_Equity_log"]

    for base in FLOW_COLS:
        if base in df.columns:
            part = _compute_ttm_from_quarterly_flow(df, base, base)
            out = out.merge(part, on=["ticker", "year", "quarter"], how="left")


    for acc_col in ["Revenue_acc2", "OpIncome_acc2"]:
        if acc_col in out.columns:
            x = pd.to_numeric(out[acc_col], errors="coerce")
            out[f"{acc_col}_log1p"] = np.sign(x) * np.log1p(np.abs(x))

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

    hist = (
        out.groupby("ticker", as_index=False)
        .agg(history_quarters=("quarter_key", "count"), first_quarter_key=("quarter_key", "min"), latest_quarter_key=("quarter_key", "max"))
    )
    out = out.merge(hist, on="ticker", how="left")

    PRO.mkdir(parents=True, exist_ok=True)
    p_out = PRO / f"factors_ttm_acc2__asof={args.asof}__metric={args.metric}__v={args.out_v}.parquet"
    out.to_parquet(p_out, index=False)

    print(f"[OK] src:  {p_in}")
    print(f"[OK] saved:{p_out}")
    print(f"rows: {len(out)} tickers: {out['ticker'].nunique()}")


if __name__ == "__main__":
    main()
