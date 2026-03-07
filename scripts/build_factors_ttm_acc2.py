#!/usr/bin/env python
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
PRO = ROOT / "data" / "processed"


def _find_fundamentals(asof: str, in_v: int) -> Path:
    # Prefer exact match with fs=OFS (policy default), otherwise any fs
    exact = list(PRO.glob(f"fundamentals_quarterly__asof={asof}__src=dart__fs=OFS__v={in_v}.parquet"))
    if exact:
        return exact[0]
    anyfs = list(PRO.glob(f"fundamentals_quarterly__asof={asof}__src=dart__fs=*__v={in_v}.parquet"))
    if anyfs:
        return anyfs[0]
    raise FileNotFoundError(
        f"fundamentals_quarterly parquet not found for asof={asof} (v={in_v}). "
        f"Run: python .\\scripts\\collect_fundamentals_quarterly.py --asof {asof}"
    )


def _ttm_yoy_acc2(df: pd.DataFrame, col: str) -> tuple[pd.Series, pd.Series, pd.Series]:
    g = df.groupby("ticker", sort=False)[col]
    ttm = g.rolling(4, min_periods=4).sum().reset_index(level=0, drop=True)
    ttm_prev = ttm.groupby(df["ticker"], sort=False).shift(4)
    yoy = (ttm / ttm_prev) - 1.0
    acc2 = yoy.groupby(df["ticker"], sort=False).shift(4)
    acc2 = yoy - acc2
    return ttm, yoy, acc2


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")  # used for filename only (matches pipeline)
    ap.add_argument("--in_v", type=int, default=1, help="fundamentals_quarterly version")
    ap.add_argument("--out_v", type=int, default=2)
    args = ap.parse_args()

    p_in = _find_fundamentals(args.asof, args.in_v)
    df = pd.read_parquet(p_in)

    import numpy as np

    # --- derive Debt_to_Equity if missing ---
    if "Debt_to_Equity" not in df.columns:
        if ("Liabilities" in df.columns) and ("Equity" in df.columns):
            eq = pd.to_numeric(df["Equity"], errors="coerce")
            liab = pd.to_numeric(df["Liabilities"], errors="coerce")

            # prevent division blow-ups
            eq_safe = eq.where(eq.abs() > 1e-9)
            df["Debt_to_Equity"] = liab / eq_safe
        else:
            # if we truly can't derive, keep the error behavior
            pass

    # (optional) clean infinities that break stats/logs
    if "Debt_to_Equity" in df.columns:
        df["Debt_to_Equity"] = pd.to_numeric(df["Debt_to_Equity"], errors="coerce")
        df.loc[~np.isfinite(df["Debt_to_Equity"]), "Debt_to_Equity"] = np.nan

    need = ["ticker", "year", "quarter", "Revenue", "OpIncome", "CAPEX", "Debt_to_Equity"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        raise ValueError(f"Required columns missing in fundamentals: {missing}\nAvailable: {list(df.columns)}")

    df = df.copy()
    df["ticker"] = df["ticker"].astype(str)
    df = df.sort_values(["ticker", "year", "quarter"]).reset_index(drop=True)

    # TTM/YoY/Acc2
    for base, out_prefix in [("Revenue", "Revenue"), ("OpIncome", "OpIncome"), ("CAPEX", "CAPEX")]:
        ttm, yoy, acc2 = _ttm_yoy_acc2(df, base)
        df[f"{out_prefix}_ttm"] = ttm
        df[f"{out_prefix}_ttm_yoy"] = yoy
        df[f"{out_prefix}_acc2"] = acc2

    # Debt_to_Equity_log
    dte = pd.to_numeric(df["Debt_to_Equity"], errors="coerce")
    dte = dte.clip(lower=0)
    df["Debt_to_Equity_log"] = np.log1p(dte)

    # Keep only factor columns + keys
    keep = [
            "ticker", "name", "corp_code", "year", "quarter", "quarter_key",
            "Revenue_ttm", "Revenue_ttm_yoy", "Revenue_acc2",
            "OpIncome_ttm", "OpIncome_ttm_yoy", "OpIncome_acc2",
            "NetIncome_ttm", "NetIncome_ttm_yoy", "NetIncome_acc2",
            "CFO_ttm", "CFO_ttm_yoy", "CFO_acc2",
            "CAPEX_ttm", "CAPEX_ttm_yoy", "CAPEX_acc2",
            "CFO_to_Assets_ttm", "CAPEX_to_Assets_ttm",
            "Debt_to_Equity", "Debt_to_Equity_log",
        ]
    out = df[[c for c in keep if c in df.columns]].copy()

    p_out = PRO / f"factors_ttm_acc2__asof={args.asof}__metric={args.metric}__v={args.out_v}.parquet"
    out.to_parquet(p_out, index=False)

    print(f"[OK] src:  {p_in.as_posix()}")
    print(f"[OK] saved:{p_out.as_posix()}")
    print(f"rows: {len(out)} tickers: {out['ticker'].nunique()}")


if __name__ == "__main__":
    main()
