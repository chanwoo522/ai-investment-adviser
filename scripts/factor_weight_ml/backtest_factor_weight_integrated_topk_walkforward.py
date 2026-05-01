#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

import joblib
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import Ridge, ElasticNet
from sklearn.ensemble import RandomForestRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

# ---------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------
_THIS_DIR = os.path.abspath(os.path.dirname(__file__))
_SCRIPTS_DIR = os.path.abspath(os.path.join(_THIS_DIR, ".."))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
_BACKTEST_DIR = os.path.join(_SCRIPTS_DIR, "backtest")
for _p in (_THIS_DIR, _SCRIPTS_DIR, _BACKTEST_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from backtest_quarterly_rebalance_v2 import (  # type: ignore
    load_strategy,
    load_strategy_runtime,
    apply_filters,
    standardize_factor,
    normalize_ticker_series,
    compute_rebalance_month_from_yq,
    select_top_k_with_group_cap,
    load_group_map,
    detect_group_col,
)

# ---------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------
DEFAULT_BUCKET_SPECS: dict[str, list[str]] = {
    "profit_accel": ["OpIncome_acc2_log1p", "op_growth_streak2"],
    "revenue_support": ["Revenue_acc2", "rev_growth_streak2"],
    "quality": ["Quality_CFO_to_Assets"],
    "balance_sheet": ["Debt_to_Equity_log"],
}
RAW_FACTOR_DEFAULTS = {"op_growth_streak2", "rev_growth_streak2"}


# ---------------------------------------------------------------------
# Basic helpers
# ---------------------------------------------------------------------
def _to_num(s: pd.Series | Any) -> pd.Series:
    if isinstance(s, pd.Series):
        return pd.to_numeric(s, errors="coerce")
    return pd.to_numeric(pd.Series([s]), errors="coerce")


def _ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


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
    raise FileNotFoundError("features_live not found: " + " / ".join(str(p) for p in candidates))


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


def ensure_filter_alias_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    alias_pairs = [
        ("op_cur_q", ["op_cur_q", "OpIncome_cur_q", "op_income_cur_q", "operating_income_cur_q"]),
        ("OpIncome_ttm", ["OpIncome_ttm", "op_income", "op_income_ttm", "operating_income_ttm"]),
        ("traded_value", ["traded_value", "거래대금", "trading_value", "value"]),
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


def _strict_filter_op_qoq(df: pd.DataFrame, rebalance_month: pd.Timestamp) -> pd.DataFrame:
    if "op_qoq" not in df.columns:
        return df
    op_qoq_num = _to_num(df["op_qoq"])
    strict_qoq = df.loc[op_qoq_num.notna() & (op_qoq_num > 0)].copy()
    if len(strict_qoq) == 0:
        print(f"[WARN] skip strict filter op_qoq > 0 for rebalance_month={rebalance_month.date()}: would empty the rebalance universe")
        return df
    return strict_qoq


def _apply_strategy_filters(cohort: pd.DataFrame, filters: dict, rebalance_month: pd.Timestamp, filter_fallback: str) -> pd.DataFrame:
    x = apply_filters(cohort.copy(), filters, verbose=True)
    x = _strict_filter_op_qoq(x, rebalance_month)
    if len(x) == 0:
        msg = f"All rows filtered out for rebalance_month={rebalance_month.date()} with filters={filters}"
        if filter_fallback == "error":
            raise ValueError(msg)
        print(f"[WARN] {msg}; falling back to unfiltered universe")
        return cohort.copy()
    return x.copy()


def _mask_mult_for(df: pd.DataFrame, col: str) -> pd.Series:
    if ("CFO" in col) or ("Quality_CFO" in col) or ("CFO_to_Assets" in col):
        isnull = _to_num(df.get("CFO_isnull", 0.0)).fillna(0.0)
        warn = _to_num(df.get("CFO_warn", 0.0)).fillna(0.0)
        return (1.0 - 0.7 * isnull - 0.4 * warn).clip(0.0, 1.0).astype("float64")
    return pd.Series(1.0, index=df.index, dtype="float64")


def _build_active_bucket_specs(weights: dict[str, float], df_cols: list[str]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for bucket, cols in DEFAULT_BUCKET_SPECS.items():
        active: dict[str, float] = {}
        for c in cols:
            if c in weights and float(weights[c]) != 0.0 and c in df_cols:
                active[c] = float(weights[c])
        if active:
            out[bucket] = active
    return out


def _bucket_base_abs_weight_map(active_buckets: dict[str, dict[str, float]]) -> dict[str, float]:
    return {b: float(sum(abs(float(w)) for w in fmap.values())) for b, fmap in active_buckets.items()}


def _build_factor_to_bucket(active_buckets: dict[str, dict[str, float]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for bucket, fmap in active_buckets.items():
        for c in fmap.keys():
            out[c] = bucket
    return out


def _compute_signal_columns(cohort: pd.DataFrame, weights: dict[str, float], runtime_cfg: dict[str, Any]) -> tuple[pd.DataFrame, list[str]]:
    gg = cohort.copy()
    clip_z = runtime_cfg.get("clip_z", 5.0)
    clip_tiers = runtime_cfg.get("clip_tiers", [])
    use_robust_z = bool(runtime_cfg.get("use_robust_z", False))
    raw_factors = set(runtime_cfg.get("raw_factors", []))
    if not raw_factors:
        raw_factors = set(RAW_FACTOR_DEFAULTS)

    gg["score_base"] = 0.0
    used_cols: list[str] = []

    for c, ww in weights.items():
        ww = float(ww)
        if ww == 0.0 or c not in gg.columns:
            continue
        used_cols.append(c)

        raw = _to_num(gg[c])
        mult = _mask_mult_for(gg, c)

        if c in raw_factors:
            signal = raw.fillna(0.0).astype("float64")
            contrib = ww * signal
        else:
            fill = raw.median(skipna=True) if raw.notna().any() else 0.0
            signal = standardize_factor(
                raw.fillna(fill),
                use_robust_z=use_robust_z,
                clip_z=None if clip_z is None or float(clip_z) < 0 else float(clip_z),
                clip_tiers=clip_tiers if isinstance(clip_tiers, list) else [],
            ).astype("float64")
            contrib = ww * signal * mult

        gg[f"{c}__signal"] = signal
        gg[f"{c}__mult"] = mult
        gg[f"{c}__contrib_base"] = contrib
        gg["score_base"] += contrib

    gg["score_baseline"] = gg["score_base"]
    return gg, used_cols


def _compute_bucket_scores(scored: pd.DataFrame, active_buckets: dict[str, dict[str, float]]) -> pd.DataFrame:
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
            desirability = np.sign(float(ww)) * sig * mult
            num += base_abs * desirability
            den += base_abs
        out[f"{bucket}__score"] = (num / den).astype("float64") if den > 0 else np.nan
    return out


def _pred_to_bucket_shares(pred_alpha_row: dict[str, float], base_abs_map: dict[str, float], temperature: float = 1.0) -> dict[str, float]:
    buckets = list(base_abs_map.keys())
    if not buckets:
        return {}
    base = np.array([max(float(base_abs_map.get(b, 0.0)), 1e-8) for b in buckets], dtype=float)
    pred = np.array([float(pred_alpha_row.get(b, np.nan)) for b in buckets], dtype=float)

    if np.isnan(pred).all():
        shares = base / base.sum()
        return {b: float(w) for b, w in zip(buckets, shares)}

    mu = np.nanmean(pred)
    sd = np.nanstd(pred)
    if not np.isfinite(sd) or sd == 0:
        z = np.zeros(len(pred), dtype=float)
    else:
        z = (pred - mu) / sd
        z = np.where(np.isfinite(z), z, 0.0)

    z = np.clip(z, -2.0, 2.0)
    tilt = np.exp(z / max(float(temperature), 1e-8))
    raw = base * tilt
    shares = raw / raw.sum()
    return {b: float(w) for b, w in zip(buckets, shares)}


def _apply_profit_accel_cap(dyn_share: dict[str, float], base_share: dict[str, float], cap_delta: float | None) -> dict[str, float]:
    target_bucket = "profit_accel"
    out = dict(dyn_share)
    if cap_delta is None or target_bucket not in out or target_bucket not in base_share:
        return out
    cap_delta = float(cap_delta)
    if cap_delta < 0:
        return out
    wb = float(base_share[target_bucket])
    wd = float(out[target_bucket])
    wd_new = min(max(wd, max(0.0, wb - cap_delta)), min(1.0, wb + cap_delta))
    if abs(wd_new - wd) < 1e-12:
        return out
    others = [k for k in out.keys() if k != target_bucket]
    old_sum = sum(float(out[k]) for k in others)
    out[target_bucket] = wd_new
    remain = max(0.0, 1.0 - wd_new)
    if old_sum <= 0:
        base_other_sum = sum(float(base_share.get(k, 0.0)) for k in others)
        for k in others:
            out[k] = remain * float(base_share.get(k, 0.0)) / base_other_sum if base_other_sum > 0 else remain / max(len(others), 1)
    else:
        for k in others:
            out[k] = remain * float(out[k]) / old_sum
    s = sum(out.values())
    if s > 0:
        out = {k: float(v / s) for k, v in out.items()}
    return out


def _build_model(model_name: str, random_state: int) -> Pipeline:
    if model_name == "ridge":
        return Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("reg", Ridge(alpha=1.0, random_state=random_state)),
        ])
    if model_name == "elasticnet":
        return Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("scaler", StandardScaler()),
            ("reg", ElasticNet(alpha=0.05, l1_ratio=0.2, random_state=random_state, max_iter=10000)),
        ])
    if model_name == "random_forest":
        return Pipeline([
            ("imputer", SimpleImputer(strategy="median")),
            ("reg", RandomForestRegressor(
                n_estimators=300, max_depth=3, min_samples_leaf=4,
                random_state=random_state, n_jobs=-1,
            )),
        ])
    raise ValueError(f"unsupported model_name: {model_name}")


def _select_feature_cols_compact_dailyagg(df: pd.DataFrame) -> list[str]:
    candidates = [
        "context__profit_accel__mean",
        "context__profit_accel__spread_p90_p10",
        "context__revenue_support__mean",
        "context__revenue_support__spread_p90_p10",
        "context__balance_sheet__mean",
        "context__balance_sheet__spread_p90_p10",
        "lag1__profit_accel__alpha",
        "lag1__revenue_support__alpha",
        "lag1__balance_sheet__alpha",
        "lag1__baseline_ret",
        "dailyagg__universe__ret_20d_mean",
        "dailyagg__universe__vol_20d_mean",
        "dailyagg__universe__tv_mean_20d_mean",
        "dailyagg__profit_accel__ret_20d_mean",
        "dailyagg__revenue_support__ret_20d_mean",
        "dailyagg__balance_sheet__ret_20d_mean",
    ]
    return [c for c in candidates if c in df.columns and pd.api.types.is_numeric_dtype(df[c])]


def _detect_price_columns(df: pd.DataFrame) -> tuple[str, str, str]:
    tcol = next((c for c in ["ticker", "code", "종목코드", "symbol"] if c in df.columns), None)
    dcol = next((c for c in ["date", "Date", "dt", "ymd", "trd_date"] if c in df.columns), None)
    pcol = next((c for c in ["close", "Close", "adj_close", "price", "종가"] if c in df.columns), None)
    if not tcol or not dcol or not pcol:
        raise ValueError(f"Could not detect raw price columns from: {list(df.columns)}")
    return tcol, dcol, pcol


def _detect_volume_columns(df: pd.DataFrame) -> Optional[str]:
    return next((c for c in ["volume", "Volume", "거래량"] if c in df.columns), None)


def _detect_traded_value_columns(df: pd.DataFrame) -> Optional[str]:
    return next((c for c in ["traded_value", "거래대금", "trading_value", "value"] if c in df.columns), None)


def _load_raw_prices(path: Path, derive_traded_value: bool) -> pd.DataFrame:
    px0 = pd.read_parquet(path).copy()
    tcol, dcol, pcol = _detect_price_columns(px0)
    tvcol = _detect_traded_value_columns(px0)
    vcol = _detect_volume_columns(px0)

    keep = [tcol, dcol, pcol]
    if tvcol:
        keep.append(tvcol)
    if vcol and vcol not in keep:
        keep.append(vcol)

    px = px0[keep].copy()
    rename = {tcol: "ticker", dcol: "date", pcol: "price"}
    if tvcol:
        rename[tvcol] = "traded_value"
    if vcol:
        rename[vcol] = "volume"
    px = px.rename(columns=rename)

    px["ticker"] = normalize_ticker_series(px["ticker"])
    px["date"] = pd.to_datetime(px["date"], errors="coerce")
    px["price"] = _to_num(px["price"])
    if "traded_value" in px.columns:
        px["traded_value"] = _to_num(px["traded_value"])
    elif derive_traded_value and "volume" in px.columns:
        px["volume"] = _to_num(px["volume"])
        px["traded_value"] = px["price"] * px["volume"]
        print("[WARN] traded_value column not found; derived traded_value = price * volume")
    else:
        px["traded_value"] = np.nan

    px = px.dropna(subset=["ticker", "date", "price"]).sort_values(["ticker", "date"]).reset_index(drop=True)
    px["ret_1d"] = px.groupby("ticker")["price"].pct_change()

    for win in [20, 60]:
        px[f"ret_{win}d"] = px.groupby("ticker")["price"].transform(lambda s: s / s.shift(win) - 1.0)
        px[f"vol_{win}d"] = px.groupby("ticker")["ret_1d"].transform(lambda s: s.rolling(win, min_periods=max(10, win // 2)).std())
        px[f"tv_mean_{win}d"] = px.groupby("ticker")["traded_value"].transform(lambda s: s.rolling(win, min_periods=max(10, win // 2)).mean())

    return px


def _daily_snapshot_asof(px: pd.DataFrame, tickers: list[str], asof_date: pd.Timestamp) -> pd.DataFrame:
    if len(tickers) == 0:
        return pd.DataFrame(columns=["ticker"])
    right = px.loc[px["ticker"].isin(tickers)].copy().sort_values(["ticker", "date"]).reset_index(drop=True)
    chunks: list[pd.DataFrame] = []
    for tk in sorted(set(tickers)):
        p = right.loc[right["ticker"] == tk].copy()
        if len(p) == 0:
            chunks.append(pd.DataFrame({"ticker": [tk]}))
            continue
        left = pd.DataFrame({"asof_date": [pd.Timestamp(asof_date)]})
        merged = pd.merge_asof(
            left.sort_values("asof_date"),
            p.sort_values("date"),
            left_on="asof_date",
            right_on="date",
            direction="backward",
            allow_exact_matches=True,
        )
        merged["ticker"] = tk
        chunks.append(merged)
    return pd.concat(chunks, ignore_index=True)


def _safe_mean(series: pd.Series) -> float:
    x = _to_num(series)
    return float(x.mean()) if x.notna().sum() > 0 else np.nan


def _safe_std(series: pd.Series) -> float:
    x = _to_num(series)
    return float(x.std()) if x.notna().sum() > 1 else np.nan


def _build_forward_stock_returns(ret: pd.DataFrame, rb_months: list[pd.Timestamp]) -> dict[tuple[pd.Timestamp, str], float]:
    ret2 = ret[["ticker", "month_end", "ret_1m"]].copy()
    ret2["ticker"] = normalize_ticker_series(ret2["ticker"])
    ret2["month_end"] = pd.to_datetime(ret2["month_end"])
    ret2["ret_1m"] = _to_num(ret2["ret_1m"]).fillna(0.0)
    out: dict[tuple[pd.Timestamp, str], float] = {}
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


def _add_dailyagg_row_features(row: dict[str, Any], scored: pd.DataFrame, px: pd.DataFrame, rm: pd.Timestamp, active_buckets: dict[str, dict[str, float]], bucket_topn: int) -> dict[str, Any]:
    tickers = scored["ticker"].astype(str).tolist()
    snap = _daily_snapshot_asof(px, tickers=tickers, asof_date=rm)
    if len(snap) == 0:
        row["dailyagg__universe__ret_20d_mean"] = np.nan
        row["dailyagg__universe__vol_20d_mean"] = np.nan
        row["dailyagg__universe__tv_mean_20d_mean"] = np.nan
        for bucket in active_buckets.keys():
            row[f"dailyagg__{bucket}__ret_20d_mean"] = np.nan
        return row

    snap = snap.merge(
        scored[["ticker"] + [f"{b}__score" for b in active_buckets.keys() if f"{b}__score" in scored.columns]],
        on="ticker",
        how="left",
    )
    row["dailyagg__universe__ret_20d_mean"] = _safe_mean(snap["ret_20d"])
    row["dailyagg__universe__vol_20d_mean"] = _safe_mean(snap["vol_20d"])
    row["dailyagg__universe__tv_mean_20d_mean"] = _safe_mean(snap["tv_mean_20d"])

    for bucket in active_buckets.keys():
        score_col = f"{bucket}__score"
        part = snap.dropna(subset=[score_col]).copy() if score_col in snap.columns else pd.DataFrame()
        if len(part) == 0:
            row[f"dailyagg__{bucket}__ret_20d_mean"] = np.nan
            continue
        part = part.sort_values([score_col, "ticker"], ascending=[False, True]).head(int(bucket_topn))
        row[f"dailyagg__{bucket}__ret_20d_mean"] = _safe_mean(part["ret_20d"])

    return row


def _pick_topk_return(df: pd.DataFrame, score_col: str, top_k: int) -> float:
    x = df[["ticker", score_col, "fwd_return"]].copy()
    x[score_col] = _to_num(x[score_col])
    x["fwd_return"] = _to_num(x["fwd_return"])
    x = x.dropna(subset=[score_col, "fwd_return"]).sort_values([score_col, "ticker"], ascending=[False, True]).head(int(top_k))
    return float(x["fwd_return"].mean()) if len(x) else np.nan


def _make_regime_row(scored: pd.DataFrame, rm: pd.Timestamp, next_rm: pd.Timestamp, active_buckets: dict[str, dict[str, float]], raw_px: pd.DataFrame, top_k: int, bucket_topn: int, strategy: str, metric: str) -> dict[str, Any]:
    baseline_ret_next = _pick_topk_return(scored, "score_baseline", top_k)
    row: dict[str, Any] = {
        "rebalance_month": rm,
        "next_rebalance_month": next_rm,
        "strategy": strategy,
        "metric": metric,
        "top_k": int(top_k),
        "bucket_topn": int(bucket_topn),
        "n_filtered": int(len(scored)),
        "baseline_ret_next": float(baseline_ret_next) if pd.notna(baseline_ret_next) else np.nan,
    }

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

        bucket_ret_next = _pick_topk_return(scored, bucket_col, top_k)
        row[f"target__{bucket}__ret_next"] = float(bucket_ret_next) if pd.notna(bucket_ret_next) else np.nan
        row[f"target__{bucket}__alpha_next"] = (
            float(bucket_ret_next - baseline_ret_next)
            if pd.notna(bucket_ret_next) and pd.notna(baseline_ret_next)
            else np.nan
        )

    row = _add_dailyagg_row_features(row, scored, raw_px, rm, active_buckets, bucket_topn)
    return row


def _calc_turnover(prev: set[str], curr: set[str], k: int) -> float:
    if len(curr) == 0:
        return 0.0
    if len(prev) == 0:
        return 1.0
    w = 1.0 / max(k, 1)
    union = prev | curr
    return float(sum(abs((w if t in curr else 0.0) - (w if t in prev else 0.0)) for t in union))


def _calc_cagr(nav: float, months: int) -> float:
    if months <= 0 or nav <= 0:
        return np.nan
    return float(nav ** (12.0 / months) - 1.0)


def _calc_sharpe_monthly(ret: pd.Series) -> float:
    x = _to_num(ret).dropna()
    if len(x) < 2:
        return np.nan
    sd = x.std(ddof=1)
    if sd == 0 or not np.isfinite(sd):
        return np.nan
    return float(x.mean() / sd * np.sqrt(12.0))


def _calc_mdd_from_nav(nav: pd.Series) -> float:
    x = _to_num(nav).dropna()
    if len(x) == 0:
        return np.nan
    dd = x / x.cummax() - 1.0
    return float(dd.min())


def main() -> None:
    ap = argparse.ArgumentParser(description="Full walk-forward integrated Top-K AI Overlay backtest.")
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--feat_v", type=int, required=True)
    ap.add_argument("--ret_v", type=int, required=True)
    ap.add_argument("--ret_src", default="pykrx")
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--bucket_topn", type=int, default=20)
    ap.add_argument("--max_per_group", type=int, default=2)
    ap.add_argument("--raw_px_v", type=int, default=1)
    ap.add_argument("--derive_traded_value", action="store_true", help="If traded_value is missing but Volume exists, use Close*Volume.")
    ap.add_argument("--model", default="ridge", choices=["ridge", "elasticnet", "random_forest"])
    ap.add_argument("--feature_profile", default="compact_dailyagg", choices=["compact_dailyagg"])
    ap.add_argument("--min_train_rows", type=int, default=8)
    ap.add_argument("--random_state", type=int, default=42)
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--cap_profit_accel_delta", type=float, default=-1.0)
    ap.add_argument("--ai_overlay_strength", type=float, default=1.0)
    ap.add_argument("--fallback_before_min_train", choices=["baseline", "base_share"], default="baseline")
    ap.add_argument("--filter_fallback", choices=["full", "error"], default="full")
    ap.add_argument("--tcost_bps", type=float, default=30.0)
    ap.add_argument("--out_dir", required=True)
    args = ap.parse_args()

    p_feat = _pick_features_path(args.asof, args.metric, args.feat_v)
    p_ret = _pick_returns_path(args.asof, args.metric, args.ret_src, args.ret_v)
    p_raw = _pick_raw_prices_path(args.asof, args.raw_px_v)
    if p_raw is None:
        raise FileNotFoundError("raw daily price file not found")

    strat_path = Path("configs/strategies.yaml")
    if not strat_path.exists():
        strat_path = Path("strategies.yaml")
    weights, filters, desc = load_strategy(strat_path, args.strategy)
    runtime_cfg = load_strategy_runtime(strat_path, args.strategy)

    feat = pd.read_parquet(p_feat).copy()
    ret = pd.read_parquet(p_ret).copy()
    raw_px = _load_raw_prices(p_raw, derive_traded_value=bool(args.derive_traded_value))

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

    # attach industry/group map if available
    group_map, group_src, external_group_col = load_group_map(args.asof)
    if len(group_map) > 0 and external_group_col:
        existing_group_col = detect_group_col(feat)
        if existing_group_col and existing_group_col in feat.columns:
            feat = feat.merge(
                group_map.rename(columns={external_group_col: f"{external_group_col}__ext"}),
                on="ticker",
                how="left",
            )
            ext_col = f"{external_group_col}__ext"
            if ext_col in feat.columns:
                feat[existing_group_col] = feat[existing_group_col].where(feat[existing_group_col].notna(), feat[ext_col])
                feat = feat.drop(columns=[ext_col])
            group_col = existing_group_col
        else:
            feat = feat.merge(group_map, on="ticker", how="left")
            group_col = external_group_col
        print(f"[OK] group map source selected: {group_src} group_col={group_col}")
    else:
        group_col = detect_group_col(feat)

    if "month_end" not in ret.columns and "month" in ret.columns:
        ret["month_end"] = pd.to_datetime(ret["month"]).dt.to_period("M").dt.to_timestamp("M")
    else:
        ret["month_end"] = pd.to_datetime(ret["month_end"], errors="coerce")
    ret["ticker"] = normalize_ticker_series(ret["ticker"])
    ret["ret_1m"] = _to_num(ret["ret_1m"]).fillna(0.0)

    rb_months = sorted(pd.to_datetime(feat["rebalance_month"]).dropna().unique())
    rb_months = [pd.Timestamp(x) for x in rb_months]
    if len(rb_months) < 2:
        raise RuntimeError("Need at least 2 rebalance months")

    fwd_map = _build_forward_stock_returns(ret, rb_months)

    active_buckets = _build_active_bucket_specs(weights, list(feat.columns))
    if not active_buckets:
        raise RuntimeError("No active buckets found")
    base_abs_map = _bucket_base_abs_weight_map(active_buckets)
    base_share = _pred_to_bucket_shares({b: np.nan for b in active_buckets.keys()}, base_abs_map, temperature=1.0)
    factor_to_bucket = _build_factor_to_bucket(active_buckets)

    regime_rows: list[dict[str, Any]] = []
    pred_rows: list[dict[str, Any]] = []
    rebalance_rows: list[dict[str, Any]] = []
    holdings_rows: list[dict[str, Any]] = []
    scored_rows: list[pd.DataFrame] = []
    monthly_rows: list[dict[str, Any]] = []

    prev_base_set: set[str] = set()
    prev_ai_set: set[str] = set()
    nav_base_gross = 1.0
    nav_base_net = 1.0
    nav_ai_gross = 1.0
    nav_ai_net = 1.0
    tcost = float(args.tcost_bps) / 10000.0

    for i in range(len(rb_months) - 1):
        rm = rb_months[i]
        next_rm = rb_months[i + 1]
        cohort_all = feat.loc[feat["rebalance_month"] == rm].copy()
        if len(cohort_all) == 0:
            continue

        cohort = _apply_strategy_filters(cohort_all, filters, rm, args.filter_fallback)
        cohort["fwd_return"] = [fwd_map.get((rm, str(tk)), np.nan) for tk in cohort["ticker"].astype(str)]
        cohort = cohort.dropna(subset=["fwd_return"]).copy()
        if len(cohort) == 0:
            continue

        scored, used_cols = _compute_signal_columns(cohort, weights, runtime_cfg)
        scored = _compute_bucket_scores(scored, active_buckets)

        # Build current regime row. Its target is used only after this period for future training.
        row = _make_regime_row(
            scored=scored, rm=rm, next_rm=next_rm, active_buckets=active_buckets,
            raw_px=raw_px, top_k=int(args.k), bucket_topn=int(args.bucket_topn),
            strategy=args.strategy, metric=args.metric,
        )

        # Train on strictly previous rows only.
        train_df = pd.DataFrame(regime_rows).copy()
        feature_cols = _select_feature_cols_compact_dailyagg(train_df) if len(train_df) else []
        pred_alpha: dict[str, float] = {b: np.nan for b in active_buckets.keys()}
        model_status = "fallback"

        if len(train_df) >= int(args.min_train_rows) and feature_cols:
            model_status = "trained"
            for b in active_buckets.keys():
                tgt = f"target__{b}__alpha_next"
                if tgt not in train_df.columns:
                    pred_alpha[b] = 0.0
                    continue
                y = _to_num(train_df[tgt])
                X = train_df[feature_cols].copy()
                mask = y.notna()
                X = X.loc[mask].copy()
                y = y.loc[mask].copy()
                if len(X) < max(4, int(args.min_train_rows) // 2):
                    pred_alpha[b] = float(y.mean()) if len(y) else 0.0
                    continue
                mdl = _build_model(args.model, int(args.random_state))
                mdl.fit(X, y)
                X_test = pd.DataFrame([{c: row.get(c, np.nan) for c in feature_cols}], columns=feature_cols)
                pred_alpha[b] = float(mdl.predict(X_test)[0])

        if model_status == "trained" or args.fallback_before_min_train == "base_share":
            dyn_share = _pred_to_bucket_shares(pred_alpha, base_abs_map, temperature=float(args.temperature))
            cap_val = None if float(args.cap_profit_accel_delta) < 0 else float(args.cap_profit_accel_delta)
            dyn_share = _apply_profit_accel_cap(dyn_share, base_share, cap_delta=cap_val)
            bucket_mult = {}
            for b in base_share.keys():
                wb = float(base_share.get(b, 0.0))
                ratio = 1.0 if wb <= 0 else float(dyn_share.get(b, wb)) / wb
                bucket_mult[b] = 1.0 + float(args.ai_overlay_strength) * (ratio - 1.0)
        else:
            dyn_share = dict(base_share)
            bucket_mult = {b: 1.0 for b in active_buckets.keys()}

        scored["score_ai"] = 0.0
        for c in used_cols:
            bucket = factor_to_bucket.get(c)
            mult = float(bucket_mult.get(bucket, 1.0)) if bucket else 1.0
            scored[f"{c}__ai_mult"] = mult
            scored[f"{c}__contrib_ai"] = scored[f"{c}__contrib_base"] * mult
            scored["score_ai"] += scored[f"{c}__contrib_ai"]

        scored_dump = scored.copy()
        scored_dump["rebalance_month"] = rm
        scored_dump["model_status"] = model_status
        scored_dump["score_ai_rank"] = scored_dump["score_ai"].rank(ascending=False, method="min")
        scored_dump["score_ai_rank_pct"] = (
            scored_dump["score_ai"].rank(ascending=True, pct=True, method="average").astype("float64")
            if len(scored_dump)
            else np.nan
        )
        keep_scored_cols = [
            c
            for c in [
                "rebalance_month",
                "model_status",
                "ticker",
                "name",
                group_col,
                "score_base",
                "score_baseline",
                "score_ai",
                "score_ai_rank",
                "score_ai_rank_pct",
                "fwd_return",
            ]
            if c and c in scored_dump.columns
        ]
        scored_rows.append(scored_dump[keep_scored_cols].copy())

        effective_group_col = group_col if group_col and group_col in scored.columns else None
        base_pick = select_top_k_with_group_cap(scored.copy(), int(args.k), effective_group_col, int(args.max_per_group)).copy()
        ai_pick = select_top_k_with_group_cap(
            scored.sort_values(["score_ai", "ticker"], ascending=[False, True]).copy(),
            int(args.k), effective_group_col, int(args.max_per_group)
        ).copy()

        base_set = set(base_pick["ticker"].astype(str).tolist())
        ai_set = set(ai_pick["ticker"].astype(str).tolist())
        base_turnover = _calc_turnover(prev_base_set, base_set, int(args.k))
        ai_turnover = _calc_turnover(prev_ai_set, ai_set, int(args.k))
        overlap_count = len(base_set & ai_set)

        seg_months = sorted(pd.to_datetime(ret.loc[(ret["month_end"] >= rm) & (ret["month_end"] < next_rm), "month_end"].dropna().unique()))
        period_base_gross = 1.0
        period_base_net = 1.0
        period_ai_gross = 1.0
        period_ai_net = 1.0

        for j, me in enumerate(seg_months):
            me = pd.Timestamp(me)
            seg = ret.loc[ret["month_end"] == me, ["ticker", "ret_1m"]].copy()
            base_ret = _to_num(seg.loc[seg["ticker"].astype(str).isin(base_set), "ret_1m"]).mean()
            ai_ret = _to_num(seg.loc[seg["ticker"].astype(str).isin(ai_set), "ret_1m"]).mean()
            base_ret = float(base_ret) if pd.notna(base_ret) else 0.0
            ai_ret = float(ai_ret) if pd.notna(ai_ret) else 0.0
            base_net_ret = base_ret - (base_turnover * tcost if j == 0 else 0.0)
            ai_net_ret = ai_ret - (ai_turnover * tcost if j == 0 else 0.0)

            nav_base_gross *= (1.0 + base_ret)
            nav_base_net *= (1.0 + base_net_ret)
            nav_ai_gross *= (1.0 + ai_ret)
            nav_ai_net *= (1.0 + ai_net_ret)

            period_base_gross *= (1.0 + base_ret)
            period_base_net *= (1.0 + base_net_ret)
            period_ai_gross *= (1.0 + ai_ret)
            period_ai_net *= (1.0 + ai_net_ret)

            monthly_rows.append({
                "month_end": me,
                "rebalance_month": rm,
                "baseline_month_ret_gross": base_ret,
                "baseline_month_ret_net": base_net_ret,
                "ai_month_ret_gross": ai_ret,
                "ai_month_ret_net": ai_net_ret,
                "baseline_nav_gross": nav_base_gross,
                "baseline_nav_net": nav_base_net,
                "ai_nav_gross": nav_ai_gross,
                "ai_nav_net": nav_ai_net,
                "model_status": model_status,
            })

        base_period_ret_gross = period_base_gross - 1.0
        base_period_ret_net = period_base_net - 1.0
        ai_period_ret_gross = period_ai_gross - 1.0
        ai_period_ret_net = period_ai_net - 1.0

        rec: dict[str, Any] = {
            "rebalance_month": rm,
            "next_rebalance_month": next_rm,
            "model_status": model_status,
            "train_rows": int(len(train_df)),
            "feature_count": int(len(feature_cols)),
            "baseline_period_ret_gross": base_period_ret_gross,
            "baseline_period_ret_net": base_period_ret_net,
            "ai_period_ret_gross": ai_period_ret_gross,
            "ai_period_ret_net": ai_period_ret_net,
            "period_diff_net": ai_period_ret_net - base_period_ret_net,
            "baseline_turnover": base_turnover,
            "ai_turnover": ai_turnover,
            "overlap_count": overlap_count,
        }
        for b in active_buckets.keys():
            rec[f"pred_alpha__{b}"] = pred_alpha.get(b, np.nan)
            rec[f"base_share__{b}"] = base_share.get(b, np.nan)
            rec[f"dyn_share__{b}"] = dyn_share.get(b, np.nan)
            rec[f"bucket_mult__{b}"] = bucket_mult.get(b, np.nan)
        rebalance_rows.append(rec)

        pred_row = {"rebalance_month": rm, "model_status": model_status, "train_rows": int(len(train_df))}
        for b in active_buckets.keys():
            pred_row[f"pred_alpha__{b}"] = pred_alpha.get(b, np.nan)
            pred_row[f"base_share__{b}"] = base_share.get(b, np.nan)
            pred_row[f"dyn_share__{b}"] = dyn_share.get(b, np.nan)
            pred_row[f"bucket_mult__{b}"] = bucket_mult.get(b, np.nan)
        pred_rows.append(pred_row)

        for label, pick in [("baseline", base_pick), ("ai", ai_pick)]:
            cols = [c for c in ["ticker", "name", "score_baseline", "score_ai", "fwd_return", group_col] if c and c in pick.columns]
            for rank, (_, prow) in enumerate(pick.sort_values(["score_ai" if label == "ai" else "score_baseline", "ticker"], ascending=[False, True]).iterrows(), start=1):
                h = {"rebalance_month": rm, "portfolio": label, "rank": rank}
                for c in cols:
                    h[c] = prow.get(c)
                holdings_rows.append(h)

        # Append current row only after prediction/trading for this period.
        regime_rows.append(row)
        prev_base_set = base_set
        prev_ai_set = ai_set

    monthly = pd.DataFrame(monthly_rows)
    rebalance = pd.DataFrame(rebalance_rows)
    holdings = pd.DataFrame(holdings_rows)
    scored_universe = pd.concat(scored_rows, ignore_index=True) if scored_rows else pd.DataFrame()
    regime_ds = pd.DataFrame(regime_rows)
    preds = pd.DataFrame(pred_rows)

    out_dir = Path(args.out_dir)
    _ensure_dir(out_dir)

    monthly_path = out_dir / "walkforward_integrated_monthly_nav.csv"
    rebalance_path = out_dir / "walkforward_integrated_rebalance_detail.csv"
    holdings_path = out_dir / "walkforward_integrated_holdings.csv"
    scored_universe_path = out_dir / "walkforward_integrated_scored_universe.parquet"
    regime_path = out_dir / "walkforward_regime_dataset_used.parquet"
    preds_path = out_dir / "walkforward_oos_predictions.csv"
    summary_path = out_dir / "summary.json"

    monthly.to_csv(monthly_path, index=False, encoding="utf-8-sig")
    rebalance.to_csv(rebalance_path, index=False, encoding="utf-8-sig")
    holdings.to_csv(holdings_path, index=False, encoding="utf-8-sig")
    scored_universe.to_parquet(scored_universe_path, index=False)
    regime_ds.to_parquet(regime_path, index=False)
    preds.to_csv(preds_path, index=False, encoding="utf-8-sig")

    months = int(len(monthly))
    trained_reb = rebalance.loc[rebalance["model_status"] == "trained"].copy() if len(rebalance) else pd.DataFrame()
    trained_months = int(monthly.loc[monthly["model_status"] == "trained"].shape[0]) if len(monthly) else 0

    summary = {
        "asof": args.asof,
        "metric": args.metric,
        "strategy": args.strategy,
        "feat_v": int(args.feat_v),
        "ret_v": int(args.ret_v),
        "k": int(args.k),
        "max_per_group": int(args.max_per_group),
        "tcost_bps": float(args.tcost_bps),
        "model": args.model,
        "feature_profile": args.feature_profile,
        "min_train_rows": int(args.min_train_rows),
        "temperature": float(args.temperature),
        "cap_profit_accel_delta": None if float(args.cap_profit_accel_delta) < 0 else float(args.cap_profit_accel_delta),
        "ai_overlay_strength": float(args.ai_overlay_strength),
        "fallback_before_min_train": args.fallback_before_min_train,
        "derive_traded_value": bool(args.derive_traded_value),
        "periods": int(len(rebalance)),
        "trained_periods": int((rebalance["model_status"] == "trained").sum()) if len(rebalance) else 0,
        "months": months,
        "trained_months": trained_months,
        "first_rebalance_month": str(pd.Timestamp(rebalance["rebalance_month"].iloc[0]).date()) if len(rebalance) else None,
        "last_rebalance_month": str(pd.Timestamp(rebalance["rebalance_month"].iloc[-1]).date()) if len(rebalance) else None,
        "baseline_nav_gross": float(monthly["baseline_nav_gross"].iloc[-1]) if len(monthly) else np.nan,
        "ai_nav_gross": float(monthly["ai_nav_gross"].iloc[-1]) if len(monthly) else np.nan,
        "baseline_nav_net": float(monthly["baseline_nav_net"].iloc[-1]) if len(monthly) else np.nan,
        "ai_nav_net": float(monthly["ai_nav_net"].iloc[-1]) if len(monthly) else np.nan,
        "baseline_cagr_net": _calc_cagr(float(monthly["baseline_nav_net"].iloc[-1]), months) if len(monthly) else np.nan,
        "ai_cagr_net": _calc_cagr(float(monthly["ai_nav_net"].iloc[-1]), months) if len(monthly) else np.nan,
        "baseline_sharpe_net": _calc_sharpe_monthly(monthly["baseline_month_ret_net"]) if len(monthly) else np.nan,
        "ai_sharpe_net": _calc_sharpe_monthly(monthly["ai_month_ret_net"]) if len(monthly) else np.nan,
        "baseline_mdd_net": _calc_mdd_from_nav(monthly["baseline_nav_net"]) if len(monthly) else np.nan,
        "ai_mdd_net": _calc_mdd_from_nav(monthly["ai_nav_net"]) if len(monthly) else np.nan,
        "avg_baseline_turnover": float(rebalance["baseline_turnover"].mean()) if len(rebalance) else np.nan,
        "avg_ai_turnover": float(rebalance["ai_turnover"].mean()) if len(rebalance) else np.nan,
        "avg_overlap_count": float(rebalance["overlap_count"].mean()) if len(rebalance) else np.nan,
        "ai_win_rate_period_net": float((rebalance["period_diff_net"] > 0).mean()) if len(rebalance) else np.nan,
    }

    if len(trained_reb) > 0:
        trained_monthly = monthly.loc[monthly["model_status"] == "trained"].copy()
        # Trained-only NAV starts from 1 at first trained month using monthly returns only.
        b_nav = (1.0 + _to_num(trained_monthly["baseline_month_ret_net"]).fillna(0.0)).cumprod()
        a_nav = (1.0 + _to_num(trained_monthly["ai_month_ret_net"]).fillna(0.0)).cumprod()
        summary.update({
            "trained_only_periods": int(len(trained_reb)),
            "trained_only_baseline_nav_net": float(b_nav.iloc[-1]) if len(b_nav) else np.nan,
            "trained_only_ai_nav_net": float(a_nav.iloc[-1]) if len(a_nav) else np.nan,
            "trained_only_baseline_cagr_net": _calc_cagr(float(b_nav.iloc[-1]), len(trained_monthly)) if len(b_nav) else np.nan,
            "trained_only_ai_cagr_net": _calc_cagr(float(a_nav.iloc[-1]), len(trained_monthly)) if len(a_nav) else np.nan,
            "trained_only_baseline_sharpe_net": _calc_sharpe_monthly(trained_monthly["baseline_month_ret_net"]),
            "trained_only_ai_sharpe_net": _calc_sharpe_monthly(trained_monthly["ai_month_ret_net"]),
            "trained_only_baseline_mdd_net": _calc_mdd_from_nav(b_nav),
            "trained_only_ai_mdd_net": _calc_mdd_from_nav(a_nav),
            "trained_only_ai_win_rate_period_net": float((trained_reb["period_diff_net"] > 0).mean()),
        })

    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("========== FULL WALK-FORWARD INTEGRATED TOP-K AI OVERLAY BACKTEST ==========")
    print(f"periods={summary['periods']} trained_periods={summary['trained_periods']} months={summary['months']}")
    print(f"rebalance_range={summary['first_rebalance_month']} -> {summary['last_rebalance_month']}")
    print("")
    print("[ALL PERIODS NET]")
    print(f"baseline_nav_net={summary['baseline_nav_net']:.6f}")
    print(f"ai_nav_net={summary['ai_nav_net']:.6f}")
    print(f"baseline_cagr_net={summary['baseline_cagr_net']:.6f}")
    print(f"ai_cagr_net={summary['ai_cagr_net']:.6f}")
    print(f"baseline_sharpe_net={summary['baseline_sharpe_net']:.6f}")
    print(f"ai_sharpe_net={summary['ai_sharpe_net']:.6f}")
    print(f"baseline_mdd_net={summary['baseline_mdd_net']:.6f}")
    print(f"ai_mdd_net={summary['ai_mdd_net']:.6f}")
    print(f"ai_win_rate_period_net={summary['ai_win_rate_period_net']:.6f}")
    if "trained_only_periods" in summary:
        print("")
        print("[TRAINED-ONLY PERIODS NET]")
        print(f"trained_only_periods={summary['trained_only_periods']}")
        print(f"baseline_nav_net={summary['trained_only_baseline_nav_net']:.6f}")
        print(f"ai_nav_net={summary['trained_only_ai_nav_net']:.6f}")
        print(f"baseline_cagr_net={summary['trained_only_baseline_cagr_net']:.6f}")
        print(f"ai_cagr_net={summary['trained_only_ai_cagr_net']:.6f}")
        print(f"baseline_sharpe_net={summary['trained_only_baseline_sharpe_net']:.6f}")
        print(f"ai_sharpe_net={summary['trained_only_ai_sharpe_net']:.6f}")
        print(f"baseline_mdd_net={summary['trained_only_baseline_mdd_net']:.6f}")
        print(f"ai_mdd_net={summary['trained_only_ai_mdd_net']:.6f}")
        print(f"ai_win_rate_period_net={summary['trained_only_ai_win_rate_period_net']:.6f}")
    print("")
    print(f"[OK] monthly  : {monthly_path}")
    print(f"[OK] rebalance: {rebalance_path}")
    print(f"[OK] holdings : {holdings_path}")
    print(f"[OK] scored   : {scored_universe_path}")
    print(f"[OK] regime   : {regime_path}")
    print(f"[OK] preds    : {preds_path}")
    print(f"[OK] summary  : {summary_path}")


if __name__ == "__main__":
    main()
