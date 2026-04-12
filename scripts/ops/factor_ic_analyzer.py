from __future__ import annotations

import argparse
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
PRO = ROOT / "data" / "processed"

DEFAULT_FACTORS = [
    "Revenue_ttm_yoy",
    "OpIncome_ttm_yoy",
    "Revenue_acc2",
    "OpIncome_acc2",
    "Debt_to_Equity_log",
    "Quality_CFO_to_Assets",
]


def _find_features(asof: str, metric: str, feat_v: int) -> Path:
    p = PRO / f"features_live__asof={asof}__metric={metric}__v={feat_v}.parquet"
    if not p.exists():
        raise FileNotFoundError(p)
    return p


def _find_returns(asof: str, metric: str, ret_v: int, ret_src: str) -> Path:
    p = PRO / f"returns_monthly__src={ret_src}__asof={asof}__metric={metric}__v={ret_v}.parquet"
    if not p.exists():
        raise FileNotFoundError(p)
    return p


def _normalize_ticker(s: pd.Series) -> pd.Series:
    return s.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)


def _find_month_col(df: pd.DataFrame) -> str:
    candidates = ["rebalance_month", "month_end", "month", "date", "ym"]
    for c in candidates:
        if c in df.columns:
            return c
    raise ValueError(f"Could not find month column in returns file. columns={list(df.columns)}")


def _find_forward_ret_col(df: pd.DataFrame) -> str | None:
    candidates = [
        "stock_forward_ret_to_next_rebalance",
        "forward_ret",
        "fwd_ret",
        "ret_fwd",
        "next_ret",
    ]
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _find_monthly_ret_col(df: pd.DataFrame) -> str | None:
    candidates = [
        "ret_1m",
        "ret",
        "return",
        "monthly_ret",
        "month_ret",
        "stock_ret",
    ]
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _build_rebalance_month_from_yq(year: pd.Series, quarter: pd.Series) -> pd.Series:
    """
    Rebalance rule:
      Q1 end +45d -> month end
      Q2 end +45d -> month end
      Q3 end +45d -> month end
      Q4 end +90d -> month end
    """
    year = pd.to_numeric(year, errors="coerce")
    quarter = pd.to_numeric(quarter, errors="coerce")

    q_end_month = quarter.map({1: 3, 2: 6, 3: 9, 4: 12})
    q_end = pd.to_datetime(
        year.astype("Int64").astype(str)
        + "-"
        + q_end_month.astype("Int64").astype(str).str.zfill(2)
        + "-01",
        errors="coerce",
    ).dt.to_period("M").dt.to_timestamp("M")

    offset_days = np.select(
        [
            quarter.eq(1),
            quarter.eq(2),
            quarter.eq(3),
            quarter.eq(4),
        ],
        [45, 45, 45, 90],
        default=45,
    )

    reb = (q_end + pd.to_timedelta(offset_days, unit="D")).dt.to_period("M").dt.to_timestamp("M")
    return reb


def _build_forward_return_from_monthly(ret: pd.DataFrame, month_col: str, monthly_ret_col: str) -> pd.DataFrame:
    """
    Build next-month forward return from monthly simple returns.
    Align current month_end with next month's realized return.
    """
    x = ret[["ticker", month_col, monthly_ret_col]].copy()
    x["ticker"] = _normalize_ticker(x["ticker"])
    x[month_col] = pd.to_datetime(x[month_col]).dt.to_period("M").dt.to_timestamp("M")
    x[monthly_ret_col] = pd.to_numeric(x[monthly_ret_col], errors="coerce")

    x = x.sort_values(["ticker", month_col]).copy()
    x["fwd_ret"] = x.groupby("ticker")[monthly_ret_col].shift(-1)

    return x[["ticker", month_col, "fwd_ret"]].rename(columns={month_col: "rebalance_month"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--feat_v", type=int, required=True)
    ap.add_argument("--ret_v", type=int, default=1)
    ap.add_argument("--ret_src", default="pykrx")
    ap.add_argument("--factors", default=",".join(DEFAULT_FACTORS))
    ap.add_argument("--min_n", type=int, default=10)
    ap.add_argument("--out_v", type=int, default=1)
    args = ap.parse_args()

    p_feat = _find_features(args.asof, args.metric, args.feat_v)
    p_ret = _find_returns(args.asof, args.metric, args.ret_v, args.ret_src)

    feat = pd.read_parquet(p_feat).copy()
    ret = pd.read_parquet(p_ret).copy()

    print("[OK] features loaded :", p_feat)
    print("[OK] returns loaded  :", p_ret)
    print("[INFO] features columns:", list(feat.columns))
    print("[INFO] returns columns :", list(ret.columns))

    if "ticker" not in feat.columns:
        raise ValueError("features file missing column: ticker")
    if "ticker" not in ret.columns:
        raise ValueError("returns file missing column: ticker")

    feat["ticker"] = _normalize_ticker(feat["ticker"])
    ret["ticker"] = _normalize_ticker(ret["ticker"])

    # features rebalance_month
    if "rebalance_month" in feat.columns:
        feat["rebalance_month"] = pd.to_datetime(feat["rebalance_month"]).dt.to_period("M").dt.to_timestamp("M")
    else:
        if not {"year", "quarter"}.issubset(feat.columns):
            raise ValueError("features file must contain either rebalance_month or year/quarter")
        feat["rebalance_month"] = _build_rebalance_month_from_yq(feat["year"], feat["quarter"])

    # returns forward return
    month_col = _find_month_col(ret)
    fwd_col = _find_forward_ret_col(ret)

    if fwd_col is not None:
        ret2 = ret[["ticker", month_col, fwd_col]].copy()
        ret2[month_col] = pd.to_datetime(ret2[month_col]).dt.to_period("M").dt.to_timestamp("M")
        ret2["fwd_ret"] = pd.to_numeric(ret2[fwd_col], errors="coerce")
        ret2 = ret2.rename(columns={month_col: "rebalance_month"})
        print(f"[OK] using forward return column: {fwd_col}")
    else:
        monthly_ret_col = _find_monthly_ret_col(ret)
        if monthly_ret_col is None:
            raise ValueError(
                f"Could not find usable return column in returns file. columns={list(ret.columns)}"
            )
        ret2 = _build_forward_return_from_monthly(ret, month_col, monthly_ret_col)
        print(f"[OK] built forward returns from monthly column: {monthly_ret_col}")

    df = feat.merge(
        ret2[["ticker", "rebalance_month", "fwd_ret"]],
        on=["ticker", "rebalance_month"],
        how="left",
    )

    factors = [x.strip() for x in args.factors.split(",") if x.strip()]
    available = [f for f in factors if f in df.columns]
    missing = [f for f in factors if f not in df.columns]

    if not available:
        raise ValueError("None of the requested factors exist in features file.")

    print("[INFO] factors used:", available)
    if missing:
        print("[WARN] missing factors:", missing)

    rows = []

    for m, g in df.groupby("rebalance_month"):
        for f in available:
            x = pd.to_numeric(g[f], errors="coerce")
            y = pd.to_numeric(g["fwd_ret"], errors="coerce")
            z = pd.DataFrame({"x": x, "y": y}).dropna()

            if len(z) < args.min_n:
                rows.append(
                    {
                        "rebalance_month": m,
                        "factor": f,
                        "n": len(z),
                        "ic_pearson": np.nan,
                        "ic_spearman": np.nan,
                    }
                )
                continue

            rows.append(
                {
                    "rebalance_month": m,
                    "factor": f,
                    "n": len(z),
                    "ic_pearson": z["x"].corr(z["y"], method="pearson"),
                    "ic_spearman": z["x"].corr(z["y"], method="spearman"),
                }
            )

    out = pd.DataFrame(rows).sort_values(["factor", "rebalance_month"]).reset_index(drop=True)

    summary = (
        out.groupby("factor")
        .agg(
            months=("rebalance_month", "count"),
            valid_months=("ic_spearman", lambda s: s.notna().sum()),
            mean_ic_pearson=("ic_pearson", "mean"),
            mean_ic_spearman=("ic_spearman", "mean"),
            median_ic_spearman=("ic_spearman", "median"),
            std_ic_spearman=("ic_spearman", "std"),
        )
        .reset_index()
        .sort_values("mean_ic_spearman", ascending=False)
    )

    p_out = PRO / f"factor_ic__asof={args.asof}__metric={args.metric}__v={args.out_v}.csv"
    p_sum = PRO / f"factor_ic_summary__asof={args.asof}__metric={args.metric}__v={args.out_v}.csv"

    out.to_csv(p_out, index=False, encoding="utf-8-sig")
    summary.to_csv(p_sum, index=False, encoding="utf-8-sig")

    print()
    print("========== FACTOR IC SUMMARY ==========")
    print(summary.to_string(index=False))
    print()
    print("[OK] saved detail :", p_out)
    print("[OK] saved summary:", p_sum)


if __name__ == "__main__":
    main()