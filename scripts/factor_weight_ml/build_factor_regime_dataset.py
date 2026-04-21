#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

# ---- path bootstrap ----
_THIS_DIR = os.path.abspath(os.path.dirname(__file__))
_SCRIPTS_DIR = os.path.abspath(os.path.join(_THIS_DIR, ".."))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
_BACKTEST_DIR = os.path.join(_SCRIPTS_DIR, "backtest")
_LIVE_DIR = os.path.join(_SCRIPTS_DIR, "live")

for _p in (_THIS_DIR, _SCRIPTS_DIR, _BACKTEST_DIR, _LIVE_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest_quarterly_rebalance_v2 import (  # type: ignore
    load_strategy,
    load_strategy_runtime,
    apply_filters,
    standardize_factor,
    normalize_ticker_series,
    compute_rebalance_month_from_yq,
)

# ---------------------------------------------------------
# Constants
# ---------------------------------------------------------

DEFAULT_BUCKET_SPECS: dict[str, list[str]] = {
    "profit_accel": ["OpIncome_acc2_log1p", "op_growth_streak2"],
    "revenue_support": ["Revenue_acc2", "rev_growth_streak2"],
    "quality": ["Quality_CFO_to_Assets"],
    "balance_sheet": ["Debt_to_Equity_log"],
}

RAW_FACTOR_DEFAULTS = {"op_growth_streak2", "rev_growth_streak2"}

# ---------------------------------------------------------
# File/path helpers
# ---------------------------------------------------------

def _extract_asof_from_name(name: str) -> str | None:
    m = re.search(r"__asof=(\d{4}-\d{2}-\d{2})__", name)
    return m.group(1) if m else None


def _pick_features_path(asof: str, metric: str, feat_v: int) -> Path:
    candidates = [
        Path(rf"data/features/features_live/features_live__asof={asof}__metric={metric}__v={feat_v}.parquet"),
        Path(rf"data/processed/features_live__asof={asof}__metric={metric}__v={feat_v}.parquet"),
    ]
    for p in candidates:
        if p.exists():
            return p
    raise FileNotFoundError(
        "features_live not found. tried: " + " / ".join(str(p) for p in candidates)
    )


def _pick_returns_path(asof: str, metric: str, ret_src: str, ret_v: int) -> Path:
    p = Path(rf"data/processed/returns_monthly__src={ret_src}__asof={asof}__metric={metric}__v={ret_v}.parquet")
    if not p.exists():
        raise FileNotFoundError(p)
    return p


def _pick_raw_prices_path(asof: str, raw_px_v: int) -> Optional[Path]:
    exact = Path(rf"data/processed/prices_raw__src=pykrx__start=20110101__asof={asof}__freq=d__v={raw_px_v}.parquet")
    if exact.exists():
        return exact

    cands = sorted(Path("data/processed").glob(f"prices_raw__src=pykrx__start=*__asof=*__freq=d__v={raw_px_v}.parquet"))
    eligible: list[tuple[str, Path]] = []
    for p in cands:
        a = _extract_asof_from_name(p.name)
        if a and a <= asof:
            eligible.append((a, p))
    if not eligible:
        return None
    eligible.sort(key=lambda x: x[0])
    picked = eligible[-1][1]
    print(f"[WARN] no exact raw prices for asof={asof}; using latest <= asof: {picked.name}")
    return picked


def _ensure_dir(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------
# Local helpers aligned with baseline logic
# ---------------------------------------------------------

def ensure_filter_alias_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    alias_pairs = [
        ("op_cur_q", ["op_cur_q", "OpIncome_cur_q", "op_income_cur_q", "operating_income_cur_q"]),
        ("OpIncome_ttm", ["OpIncome_ttm", "op_income", "op_income_ttm", "operating_income_ttm"]),
        ("traded_value", ["traded_value", "거래대금", "trading_value"]),
        ("mcap", ["mcap", "market_cap", "시가총액"]),
        ("op_qoq", ["op_qoq", "OpIncome_qoq", "op_income_qoq", "operating_income_qoq"]),
    ]
    for dst, cands in alias_pairs:
        if dst in out.columns:
            continue
        for src in cands:
            if src in out.columns:
                out[dst] = out[src]
                break
    return out


def _to_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _mask_mult_for(df: pd.DataFrame, col: str) -> pd.Series:
    if ("CFO" in col) or ("Quality_CFO" in col) or ("CFO_to_Assets" in col):
        isnull = _to_num(df.get("CFO_isnull", 0.0)).fillna(0.0)
        warn = _to_num(df.get("CFO_warn", 0.0)).fillna(0.0)
        return (1.0 - 0.7 * isnull - 0.4 * warn).clip(0.0, 1.0).astype("float64")
    return pd.Series(1.0, index=df.index, dtype="float64")


def _strict_filter_op_qoq(df: pd.DataFrame, rebalance_month: pd.Timestamp) -> pd.DataFrame:
    if "op_qoq" not in df.columns:
        return df
    op_qoq_num = _to_num(df["op_qoq"])
    strict_qoq = df.loc[op_qoq_num.notna() & (op_qoq_num > 0)].copy()
    if len(strict_qoq) == 0:
        print(f"[WARN] skip strict filter op_qoq > 0 for rebalance_month={rebalance_month.date()}: would empty the rebalance universe")
        return df
    return strict_qoq


def _apply_strategy_filters(
    cohort: pd.DataFrame,
    filters: dict,
    rebalance_month: pd.Timestamp,
    filter_fallback: str,
) -> pd.DataFrame:
    x = apply_filters(cohort.copy(), filters, verbose=True)
    x = _strict_filter_op_qoq(x, rebalance_month=rebalance_month)

    if len(x) == 0:
        msg = f"All rows filtered out for rebalance_month={rebalance_month.date()} with filters={filters}"
        if filter_fallback == "error":
            raise ValueError(msg)
        print(f"[WARN] {msg}; falling back to unfiltered universe (legacy behavior)")
        return cohort.copy()
    return x.copy()


def _build_active_bucket_specs(
    weights: dict[str, float],
    df_cols: list[str],
    bucket_specs: dict[str, list[str]],
) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for bucket, cols in bucket_specs.items():
        active: dict[str, float] = {}
        for c in cols:
            if c in weights and float(weights[c]) != 0.0 and c in df_cols:
                active[c] = float(weights[c])
        if active:
            out[bucket] = active
    return out


def _compute_signal_columns(
    cohort: pd.DataFrame,
    weights: dict[str, float],
    runtime_cfg: dict[str, Any],
) -> pd.DataFrame:
    """
    Compute baseline-consistent per-factor transformed signals and contributions.
    This mirrors score_latest_rebalance / backtest semantics.
    """
    gg = cohort.copy()

    clip_z = runtime_cfg.get("clip_z", 5.0)
    clip_tiers = runtime_cfg.get("clip_tiers", [])
    use_robust_z = bool(runtime_cfg.get("use_robust_z", False))
    raw_factors = set(runtime_cfg.get("raw_factors", []))
    if not raw_factors:
        raw_factors = set(RAW_FACTOR_DEFAULTS)

    gg["baseline_score"] = 0.0

    for c, ww in weights.items():
        ww = float(ww)
        if ww == 0.0 or c not in gg.columns:
            continue

        raw = _to_num(gg[c])
        mult = _mask_mult_for(gg, c)

        if c in raw_factors:
            signal = raw.fillna(0.0).astype("float64")
            contrib = ww * signal
        else:
            signal = standardize_factor(
                raw.fillna(raw.median(skipna=True) if raw.notna().any() else 0.0),
                use_robust_z=use_robust_z,
                clip_z=None if clip_z is None or float(clip_z) < 0 else float(clip_z),
                clip_tiers=clip_tiers if isinstance(clip_tiers, list) else [],
            ).astype("float64")
            contrib = ww * signal * mult

        gg[f"{c}__signal"] = signal
        gg[f"{c}__mult"] = mult
        gg[f"{c}__contrib"] = contrib
        gg["baseline_score"] += contrib

    return gg


def _compute_bucket_scores(
    scored: pd.DataFrame,
    active_buckets: dict[str, dict[str, float]],
) -> pd.DataFrame:
    """
    Bucket score is a positive-direction desirability score.
    It uses sign(weight) * signal * mult for non-raw factors, or sign(weight) * raw signal for raw factors,
    then weighted-average by abs(base_weight) inside each bucket.
    """
    out = scored.copy()

    for bucket, factor_map in active_buckets.items():
        num = pd.Series(0.0, index=out.index, dtype="float64")
        den = 0.0

        for c, ww in factor_map.items():
            base_abs = abs(float(ww))
            if base_abs == 0.0:
                continue

            sig = _to_num(out.get(f"{c}__signal", pd.Series(index=out.index, dtype="float64"))).fillna(0.0)
            mult = _to_num(out.get(f"{c}__mult", pd.Series(1.0, index=out.index, dtype="float64"))).fillna(1.0)

            # positive desirability direction
            desirability = np.sign(float(ww)) * sig * mult

            num += base_abs * desirability
            den += base_abs

        if den > 0:
            out[f"{bucket}__score"] = (num / den).astype("float64")
        else:
            out[f"{bucket}__score"] = np.nan

    return out


def _pick_topk_equal_weight_forward_return(
    df: pd.DataFrame,
    score_col: str,
    fwd_ret_col: str,
    top_k: int,
) -> float:
    x = df.copy()
    x[score_col] = _to_num(x[score_col])
    x[fwd_ret_col] = _to_num(x[fwd_ret_col])

    x = x.dropna(subset=[score_col, fwd_ret_col]).copy()
    if len(x) == 0:
        return np.nan

    x = x.sort_values([score_col, "ticker"], ascending=[False, True]).head(int(top_k))
    if len(x) == 0:
        return np.nan
    return float(x[fwd_ret_col].mean())


def _build_forward_stock_returns(
    ret: pd.DataFrame,
    rb_months: list[pd.Timestamp],
) -> dict[tuple[pd.Timestamp, str], float]:
    ret2 = ret[["ticker", "month_end", "ret_1m"]].copy()
    ret2["ticker"] = normalize_ticker_series(ret2["ticker"])
    ret2["month_end"] = pd.to_datetime(ret2["month_end"])
    ret2["ret_1m"] = _to_num(ret2["ret_1m"]).fillna(0.0)

    out: dict[tuple[pd.Timestamp, str], float] = {}
    if len(rb_months) < 2:
        return out

    for i in range(len(rb_months) - 1):
        rb = pd.Timestamp(rb_months[i])
        next_rb = pd.Timestamp(rb_months[i + 1])

        seg = ret2.loc[(ret2["month_end"] >= rb) & (ret2["month_end"] < next_rb)].copy()
        if len(seg) == 0:
            continue

        stock_cum = (
            seg.groupby("ticker", as_index=False)["ret_1m"]
            .apply(lambda x: float(np.prod(1.0 + x.to_numpy()) - 1.0))
            .rename(columns={"ret_1m": "fwd_return"})
        )

        for _, row in stock_cum.iterrows():
            out[(rb, str(row["ticker"]))] = float(row["fwd_return"])

    return out


# ---------------------------------------------------------
# Benchmark regime features
# ---------------------------------------------------------

def _detect_price_columns(df: pd.DataFrame) -> tuple[str, str, str]:
    tcol = next((c for c in ["ticker", "code", "종목코드", "symbol"] if c in df.columns), None)
    dcol = next((c for c in ["date", "Date", "dt", "ymd", "trd_date"] if c in df.columns), None)
    pcol = next((c for c in ["close", "Close", "adj_close", "price", "종가"] if c in df.columns), None)

    if not tcol or not dcol or not pcol:
        raise ValueError(f"Could not detect raw price columns from: {list(df.columns)}")
    return tcol, dcol, pcol


def _build_benchmark_context(
    raw_px_path: Optional[Path],
    benchmark_ticker: str,
    rebalance_months: list[pd.Timestamp],
) -> pd.DataFrame:
    cols = [
        "rebalance_month",
        "benchmark_ret_21d",
        "benchmark_ret_63d",
        "benchmark_ret_126d",
        "benchmark_vol_21d",
        "benchmark_vol_63d",
        "benchmark_mdd_63d",
        "benchmark_mdd_126d",
    ]
    if raw_px_path is None or not raw_px_path.exists():
        return pd.DataFrame(columns=cols)

    px = pd.read_parquet(raw_px_path).copy()
    tcol, dcol, pcol = _detect_price_columns(px)

    px = px[[tcol, dcol, pcol]].copy()
    px.columns = ["ticker", "date", "price"]
    px["ticker"] = normalize_ticker_series(px["ticker"])
    px["date"] = pd.to_datetime(px["date"], errors="coerce")
    px["price"] = _to_num(px["price"])
    px = px.dropna(subset=["ticker", "date", "price"]).copy()

    px = px.loc[px["ticker"] == str(benchmark_ticker).zfill(6)].copy()
    if len(px) == 0:
        return pd.DataFrame(columns=cols)

    px = px.sort_values("date").reset_index(drop=True)
    px["ret_1d"] = px["price"].pct_change()

    for win in [21, 63, 126]:
        px[f"ret_{win}d"] = px["price"] / px["price"].shift(win) - 1.0

    for win in [21, 63]:
        px[f"vol_{win}d"] = px["ret_1d"].rolling(win, min_periods=max(10, win // 2)).std()

    def trailing_mdd(price: pd.Series, win: int) -> pd.Series:
        roll_max = price.rolling(win, min_periods=max(10, win // 2)).max()
        dd = price / roll_max - 1.0
        return dd.rolling(win, min_periods=max(10, win // 2)).min()

    for win in [63, 126]:
        px[f"mdd_{win}d"] = trailing_mdd(px["price"], win)

    left = pd.DataFrame({"rebalance_month": pd.to_datetime(rebalance_months)})
    right = px[[
        "date",
        "ret_21d", "ret_63d", "ret_126d",
        "vol_21d", "vol_63d",
        "mdd_63d", "mdd_126d",
    ]].copy().sort_values("date")

    left = left.sort_values("rebalance_month")
    merged = pd.merge_asof(
        left,
        right,
        left_on="rebalance_month",
        right_on="date",
        direction="backward",
        allow_exact_matches=True,
    ).drop(columns=["date"], errors="ignore")

    merged = merged.rename(columns={
        "ret_21d": "benchmark_ret_21d",
        "ret_63d": "benchmark_ret_63d",
        "ret_126d": "benchmark_ret_126d",
        "vol_21d": "benchmark_vol_21d",
        "vol_63d": "benchmark_vol_63d",
        "mdd_63d": "benchmark_mdd_63d",
        "mdd_126d": "benchmark_mdd_126d",
    })
    return merged


# ---------------------------------------------------------
# Main dataset builder
# ---------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--feat_v", type=int, required=True)
    ap.add_argument("--ret_v", type=int, required=True)
    ap.add_argument("--ret_src", default="pykrx")
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--top_k", type=int, default=10, help="top-k used for baseline and bucket portfolio return calculation")
    ap.add_argument("--benchmark_ticker", default="069500")
    ap.add_argument("--raw_px_v", type=int, default=1)
    ap.add_argument("--filter_fallback", choices=["full", "error"], default="full")
    ap.add_argument("--out_v", type=int, default=1)
    ap.add_argument("--out_path", default="")
    args = ap.parse_args()

    p_feat = _pick_features_path(args.asof, args.metric, args.feat_v)
    p_ret = _pick_returns_path(args.asof, args.metric, args.ret_src, args.ret_v)
    p_raw = _pick_raw_prices_path(args.asof, args.raw_px_v)

    strat_path = Path("configs/strategies.yaml")
    if not strat_path.exists():
        strat_path = Path("strategies.yaml")
    if not strat_path.exists():
        raise FileNotFoundError("strategies.yaml not found in configs/ or project root.")

    weights, filters, desc = load_strategy(strat_path, args.strategy)
    runtime_cfg = load_strategy_runtime(strat_path, args.strategy)

    feat = pd.read_parquet(p_feat).copy()
    ret = pd.read_parquet(p_ret).copy()

    feat["ticker"] = normalize_ticker_series(feat["ticker"])
    feat["year"] = pd.to_numeric(feat["year"], errors="coerce").astype("Int64")
    feat["quarter"] = pd.to_numeric(feat["quarter"], errors="coerce").astype("Int64")
    feat = feat.dropna(subset=["ticker", "year", "quarter"]).copy()
    feat["year"] = feat["year"].astype(int)
    feat["quarter"] = feat["quarter"].astype(int)

    if "rebalance_month" not in feat.columns:
        feat["rebalance_month"] = compute_rebalance_month_from_yq(feat["year"], feat["quarter"])
    feat["rebalance_month"] = pd.to_datetime(feat["rebalance_month"], errors="coerce")

    feat = ensure_filter_alias_columns(feat)

    if "month_end" not in ret.columns and "month" in ret.columns:
        ret["month_end"] = pd.to_datetime(ret["month"]).dt.to_period("M").dt.to_timestamp("M")
    else:
        ret["month_end"] = pd.to_datetime(ret["month_end"])
    ret["ticker"] = normalize_ticker_series(ret["ticker"])
    ret["ret_1m"] = _to_num(ret["ret_1m"]).fillna(0.0)

    rb_months = sorted(pd.to_datetime(feat["rebalance_month"]).dropna().unique())
    if len(rb_months) < 2:
        raise RuntimeError("Need at least 2 rebalance months to compute forward factor outcomes.")

    fwd_map = _build_forward_stock_returns(ret, rb_months)
    bm_ctx = _build_benchmark_context(
        raw_px_path=p_raw,
        benchmark_ticker=str(args.benchmark_ticker).zfill(6),
        rebalance_months=rb_months,
    )

    active_buckets = _build_active_bucket_specs(
        weights=weights,
        df_cols=list(feat.columns),
        bucket_specs=DEFAULT_BUCKET_SPECS,
    )
    if not active_buckets:
        raise RuntimeError(
            "No active factor buckets found. "
            "Check strategy weights vs features_live columns."
        )

    rows: list[dict[str, Any]] = []

    for i in range(len(rb_months) - 1):
        rm = pd.Timestamp(rb_months[i])
        next_rm = pd.Timestamp(rb_months[i + 1])

        cohort_all = feat.loc[feat["rebalance_month"] == rm].copy()
        if len(cohort_all) == 0:
            continue

        cohort_filtered = _apply_strategy_filters(
            cohort=cohort_all,
            filters=filters,
            rebalance_month=rm,
            filter_fallback=args.filter_fallback,
        )

        # attach next-period forward return
        cohort_filtered["fwd_return"] = [
            fwd_map.get((rm, str(tk)), np.nan)
            for tk in cohort_filtered["ticker"].astype(str)
        ]
        cohort_filtered = cohort_filtered.dropna(subset=["fwd_return"]).copy()
        if len(cohort_filtered) == 0:
            continue

        scored = _compute_signal_columns(
            cohort=cohort_filtered,
            weights=weights,
            runtime_cfg=runtime_cfg,
        )
        scored = _compute_bucket_scores(scored, active_buckets=active_buckets)

        baseline_ret_next = _pick_topk_equal_weight_forward_return(
            scored, score_col="baseline_score", fwd_ret_col="fwd_return", top_k=args.top_k
        )

        row: dict[str, Any] = {
            "rebalance_month": rm,
            "next_rebalance_month": next_rm,
            "strategy": args.strategy,
            "metric": args.metric,
            "top_k": int(args.top_k),
            "n_total": int(len(cohort_all)),
            "n_filtered": int(len(cohort_filtered)),
            "filter_pass_rate": float(len(cohort_filtered) / max(len(cohort_all), 1)),
            "baseline_ret_next": float(baseline_ret_next) if pd.notna(baseline_ret_next) else np.nan,
            "benchmark_ticker": str(args.benchmark_ticker).zfill(6),
        }

        # cohort context features
        for c in [
            "OpIncome_acc2_log1p", "Revenue_acc2", "Quality_CFO_to_Assets", "Debt_to_Equity_log",
            "op_growth_streak2", "rev_growth_streak2", "CFO_isnull", "CFO_warn",
        ]:
            if c in cohort_filtered.columns:
                x = _to_num(cohort_filtered[c])
                row[f"context__{c}__mean"] = float(x.mean()) if len(x) else np.nan
                row[f"context__{c}__std"] = float(x.std()) if len(x) > 1 else np.nan

        # bucket context + targets
        for bucket in active_buckets.keys():
            bucket_col = f"{bucket}__score"
            x = _to_num(scored[bucket_col])

            row[f"context__{bucket}__mean"] = float(x.mean()) if len(x) else np.nan
            row[f"context__{bucket}__std"] = float(x.std()) if len(x) > 1 else np.nan
            if len(x) > 0:
                q_hi = x.quantile(0.9)
                q_lo = x.quantile(0.1)
                row[f"context__{bucket}__spread_p90_p10"] = float(q_hi - q_lo) if pd.notna(q_hi) and pd.notna(q_lo) else np.nan
            else:
                row[f"context__{bucket}__spread_p90_p10"] = np.nan

            bucket_ret_next = _pick_topk_equal_weight_forward_return(
                scored, score_col=bucket_col, fwd_ret_col="fwd_return", top_k=args.top_k
            )
            row[f"target__{bucket}__ret_next"] = float(bucket_ret_next) if pd.notna(bucket_ret_next) else np.nan
            row[f"target__{bucket}__alpha_next"] = (
                float(bucket_ret_next - baseline_ret_next)
                if pd.notna(bucket_ret_next) and pd.notna(baseline_ret_next)
                else np.nan
            )

        rows.append(row)

    if not rows:
        raise RuntimeError("No factor regime rows built. Check overlap between features and returns.")

    ds = pd.DataFrame(rows).sort_values("rebalance_month").reset_index(drop=True)

    # attach benchmark context
    if len(bm_ctx) > 0:
        ds = ds.merge(bm_ctx, on="rebalance_month", how="left")

    # add lag features of previous realized factor efficacy (available at time t)
    active_bucket_names = list(active_buckets.keys())
    for bucket in active_bucket_names:
        tgt = f"target__{bucket}__alpha_next"
        if tgt in ds.columns:
            ds[f"lag1__{bucket}__alpha"] = _to_num(ds[tgt]).shift(1)
            ds[f"lag2__{bucket}__alpha"] = _to_num(ds[tgt]).shift(2)

    ds["lag1__baseline_ret"] = _to_num(ds["baseline_ret_next"]).shift(1)
    ds["lag2__baseline_ret"] = _to_num(ds["baseline_ret_next"]).shift(2)

    if args.out_path:
        out_path = Path(args.out_path)
    else:
        out_path = Path(
            rf"data/factor_weight_ml/datasets/factor_regime_dataset__asof={args.asof}"
            rf"__metric={args.metric}__strat={args.strategy}__featv={args.feat_v}"
            rf"__retv={args.ret_v}__topk={args.top_k}__v={args.out_v}.parquet"
        )

    _ensure_dir(out_path)
    ds.to_parquet(out_path, index=False)

    meta = {
        "asof": args.asof,
        "metric": args.metric,
        "strategy": args.strategy,
        "feat_v": int(args.feat_v),
        "ret_v": int(args.ret_v),
        "ret_src": args.ret_src,
        "top_k": int(args.top_k),
        "benchmark_ticker": str(args.benchmark_ticker).zfill(6),
        "features_path": str(p_feat),
        "returns_path": str(p_ret),
        "raw_prices_path": str(p_raw) if p_raw else None,
        "strategies_yaml": str(strat_path),
        "strategy_desc": desc,
        "active_buckets": active_buckets,
        "runtime_cfg": runtime_cfg,
        "rows": int(len(ds)),
        "rebalance_months": int(ds["rebalance_month"].nunique()),
        "columns": list(ds.columns),
    }

    meta_path = out_path.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] features_path : {p_feat}")
    print(f"[OK] returns_path  : {p_ret}")
    print(f"[OK] raw_prices    : {p_raw}")
    print(f"[OK] saved dataset : {out_path}")
    print(f"[OK] saved meta    : {meta_path}")
    print(f"[INFO] rows={len(ds)} rebalance_months={ds['rebalance_month'].nunique()}")
    print(f"[INFO] active_buckets={list(active_buckets.keys())}")


if __name__ == "__main__":
    main()