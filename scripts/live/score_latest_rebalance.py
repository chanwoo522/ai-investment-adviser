#!/usr/bin/env python
from __future__ import annotations

import os
import sys

# Allow this script to run from scripts/live as well as scripts/
_THIS_DIR = os.path.abspath(os.path.dirname(__file__))
_SCRIPTS_DIR = os.path.abspath(os.path.join(_THIS_DIR, ".."))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
_BACKTEST_DIR = os.path.join(_SCRIPTS_DIR, "backtest")
for _p in (_THIS_DIR, _SCRIPTS_DIR, _BACKTEST_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

import argparse
import json
import re
from pathlib import Path
from typing import Optional, Any

import joblib
import numpy as np
import pandas as pd

from common.portfolio_modules import (
    load_strategy_modules_config,
    apply_expectation_overlay,
    apply_quality_soft_penalty,
    select_target_portfolio_with_mcap_groups,
)

# backtest와 동일 로직 재사용
try:
    from backtest_quarterly_rebalance_v2 import (
        normalize_ticker_series,
        _load_name_map,
        attach_names,
        load_strategy,
        load_strategy_runtime,
        apply_filters,
        detect_group_col,
        load_group_map,
        zscore_safe,
        safe_fill_for_z,
        standardize_factor,
        compute_rebalance_month_from_yq,
        select_top_k_with_group_cap,
        load_price_history,
        lookup_prices_on_or_before,
    )
except ModuleNotFoundError:
    from backtest.backtest_quarterly_rebalance_v2 import (
        normalize_ticker_series,
        _load_name_map,
        attach_names,
        load_strategy,
        load_strategy_runtime,
        apply_filters,
        detect_group_col,
        load_group_map,
        zscore_safe,
        safe_fill_for_z,
        standardize_factor,
        compute_rebalance_month_from_yq,
        select_top_k_with_group_cap,
        load_price_history,
        lookup_prices_on_or_before,
    )


# --------------------------------------------------------------------------------------
# AI overlay constants
# --------------------------------------------------------------------------------------

DEFAULT_BUCKET_SPECS: dict[str, list[str]] = {
    "profit_accel": ["OpIncome_acc2_log1p", "op_growth_streak2"],
    "revenue_support": ["Revenue_acc2", "rev_growth_streak2"],
    "quality": ["Quality_CFO_to_Assets"],
    "balance_sheet": ["Debt_to_Equity_log"],
}

RAW_FACTOR_DEFAULTS = {"op_growth_streak2", "rev_growth_streak2"}


# --------------------------------------------------------------------------------------
# Existing helpers
# --------------------------------------------------------------------------------------

def resolve_holdings_csv_arg(holdings_csv: str, current_csv: str) -> Path:
    """
    실전 안정성을 위해 holdings_csv는 명시 입력을 원칙으로 한다.
    current_csv는 과거 호출 호환을 위한 deprecated alias로만 지원한다.
    """
    raw = holdings_csv.strip() if holdings_csv else ""
    legacy = current_csv.strip() if current_csv else ""

    if raw and legacy:
        raise ValueError("Use only one of --holdings_csv or --current_csv (deprecated alias), not both.")

    chosen = raw or legacy
    if not chosen:
        raise ValueError(
            "holdings csv must be explicitly provided. "
            "Use --holdings_csv .\\data\\portfolio\\current\\YYYYMMDD_holdings_clean.csv"
        )

    p = Path(chosen)
    if not p.exists():
        raise FileNotFoundError(f"holdings csv not found: {p}")

    return p


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


def attach_current_weights_from_prices(
    current: pd.DataFrame,
    asof: str,
    metric: str,
    target_dt: pd.Timestamp,
) -> pd.DataFrame:
    out = current.copy()

    px = load_price_history(asof, metric)
    if len(px) == 0:
        if "current_price" not in out.columns:
            out["current_price"] = pd.NA
        if "price_date" not in out.columns:
            out["price_date"] = pd.NaT
        if "current_value" not in out.columns:
            out["current_value"] = pd.NA
        if "current_weight" not in out.columns:
            out["current_weight"] = pd.NA
        return out

    px_now = lookup_prices_on_or_before(px, out["ticker"], target_dt)
    out = out.merge(px_now, on="ticker", how="left")

    if "shares" in out.columns:
        out["current_value"] = pd.to_numeric(out["shares"], errors="coerce") * pd.to_numeric(out["current_price"], errors="coerce")
        total = pd.to_numeric(out["current_value"], errors="coerce").sum(skipna=True)
        if pd.notna(total) and total > 0:
            out["current_weight"] = pd.to_numeric(out["current_value"], errors="coerce") / total
        else:
            out["current_weight"] = pd.NA
    else:
        out["current_value"] = pd.NA
        out["current_weight"] = pd.NA

    return out


def apply_piecewise_clip(z: pd.Series, clip_z: float | None = None, clip_tiers: list[dict] | None = None) -> pd.Series:
    z = pd.to_numeric(z, errors="coerce").replace([pd.NA, float("inf"), float("-inf")], pd.NA).fillna(0.0).astype("float64")
    if not clip_tiers:
        if clip_z is not None and pd.notna(clip_z) and float(clip_z) >= 0.0:
            return z.clip(lower=-float(clip_z), upper=float(clip_z)).astype("float64")
        return z.astype("float64")

    tiers: list[tuple[float, float]] = []
    for item in clip_tiers:
        if not isinstance(item, dict):
            continue
        try:
            upto = float(item.get("upto"))
            slope = float(item.get("slope", 1.0))
        except Exception:
            continue
        if not pd.notna(upto) or upto <= 0:
            continue
        slope = max(0.0, min(1.0, slope))
        tiers.append((upto, slope))
    tiers = sorted(tiers, key=lambda x: x[0])
    if not tiers:
        if clip_z is not None and pd.notna(clip_z) and float(clip_z) >= 0.0:
            return z.clip(lower=-float(clip_z), upper=float(clip_z)).astype("float64")
        return z.astype("float64")

    cap = float(clip_z) if clip_z is not None and pd.notna(clip_z) and float(clip_z) > 0 else tiers[-1][0]

    def _compress_one(v: float) -> float:
        sign = -1.0 if v < 0 else 1.0
        a = abs(v)
        out = 0.0
        prev = 0.0
        for upto, slope in tiers:
            hi = min(a, upto)
            if hi > prev:
                out += (hi - prev) * slope
                prev = hi
            if a <= upto:
                break
        if a > prev:
            tail_slope = tiers[-1][1]
            hi = min(a, cap)
            if hi > prev:
                out += (hi - prev) * tail_slope
        return sign * out

    return z.map(_compress_one).astype("float64")


def select_target_portfolio(
    scored_sorted: pd.DataFrame,
    portfolio_size: int,
    keep_current_top_n: int,
    effective_group_col: Optional[str],
    max_per_group: int,
    current_tickers: set[str] | None = None,
) -> pd.DataFrame:
    current_tickers = set(current_tickers or set())
    portfolio_size = max(0, int(portfolio_size))
    keep_current_top_n = max(0, min(int(keep_current_top_n), portfolio_size))

    if portfolio_size == 0 or len(scored_sorted) == 0:
        return scored_sorted.head(0).copy()

    current_scored = scored_sorted[scored_sorted["ticker"].astype(str).isin(current_tickers)].copy()
    reserved = select_top_k_with_group_cap(current_scored, keep_current_top_n, effective_group_col, int(max_per_group)).copy()
    reserved_set = set(reserved["ticker"].astype(str).tolist())

    remaining_slots = max(0, portfolio_size - len(reserved))
    rest_pool = scored_sorted[~scored_sorted["ticker"].astype(str).isin(reserved_set)].copy()
    fill = select_top_k_with_group_cap(rest_pool, remaining_slots, effective_group_col, int(max_per_group)).copy()

    if len(reserved) > 0:
        reserved["selection_bucket"] = "keep_current_top_n"
        reserved["kept_from_previous"] = 1
    if len(fill) > 0:
        fill["selection_bucket"] = "new_or_rank_fill"
        fill["kept_from_previous"] = fill["ticker"].astype(str).isin(current_tickers).astype(int)

    out = pd.concat([reserved, fill], ignore_index=True)
    out = out.drop_duplicates(subset=["ticker"], keep="first")
    out = out.sort_values(["score_adj", "score", "ticker"], ascending=[False, False, True], na_position="last").reset_index(drop=True)
    return out


def load_current_holdings_csv(p: Path) -> pd.DataFrame:
    df = pd.read_csv(p).copy()

    ticker_candidates = ["ticker", "code", "종목코드", "symbol"]
    tcol = None
    for c in ticker_candidates:
        if c in df.columns:
            tcol = c
            break
    if tcol is None:
        raise ValueError(
            f"Could not find ticker column in current holdings file. "
            f"Tried: {ticker_candidates}. Have: {list(df.columns)}"
        )

    rename_map = {tcol: "ticker"}

    if "name" not in df.columns:
        for c in ["종목명", "company_name", "corp_name", "short_name"]:
            if c in df.columns:
                rename_map[c] = "name"
                break

    if "shares" not in df.columns:
        for c in ["qty", "quantity", "보유수량", "수량"]:
            if c in df.columns:
                rename_map[c] = "shares"
                break

    if "weight" not in df.columns:
        for c in ["w", "target_weight", "비중"]:
            if c in df.columns:
                rename_map[c] = "weight"
                break

    df = df.rename(columns=rename_map)
    df["ticker"] = normalize_ticker_series(df["ticker"])
    df = df.dropna(subset=["ticker"]).drop_duplicates(subset=["ticker"]).copy()

    if "shares" in df.columns:
        df["shares"] = pd.to_numeric(df["shares"], errors="coerce")
    if "weight" in df.columns:
        df["weight"] = pd.to_numeric(df["weight"], errors="coerce")

    keep_cols = [c for c in ["ticker", "name", "shares", "weight"] if c in df.columns]
    return df[keep_cols].copy()


def validate_holdings_csv(df: pd.DataFrame, path: Path) -> pd.DataFrame:
    required = {"ticker", "name", "shares"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"holdings csv missing required columns {sorted(missing)}: {path}")

    out = df.copy()
    out["ticker"] = normalize_ticker_series(out["ticker"])
    if out["ticker"].isna().any():
        bad = out.loc[out["ticker"].isna(), ["ticker", "name"]].copy()
        raise ValueError(f"holdings csv contains invalid ticker values: {path}\n{bad.to_string(index=False)}")

    out["shares"] = pd.to_numeric(out["shares"], errors="coerce")
    if out["shares"].isna().any():
        bad = out.loc[out["shares"].isna(), ["ticker", "name", "shares"]].copy()
        raise ValueError(f"holdings csv contains invalid shares values: {path}\n{bad.to_string(index=False)}")

    out = out[out["shares"] > 0].copy()
    out = out.drop_duplicates(subset=["ticker"], keep="first").reset_index(drop=True)
    return out


def build_latest_available_map(feat: pd.DataFrame) -> pd.DataFrame:
    cols = [c for c in ["ticker", "rebalance_month", "year", "quarter"] if c in feat.columns]
    if not cols:
        return pd.DataFrame(columns=["ticker", "latest_available_rebalance_month", "latest_available_year", "latest_available_quarter"])

    x = feat[cols].copy()
    x = x.sort_values(["ticker", "rebalance_month"])
    x = x.groupby("ticker", as_index=False).tail(1).copy()

    ren = {}
    if "rebalance_month" in x.columns:
        ren["rebalance_month"] = "latest_available_rebalance_month"
    if "year" in x.columns:
        ren["year"] = "latest_available_year"
    if "quarter" in x.columns:
        ren["quarter"] = "latest_available_quarter"

    x = x.rename(columns=ren)
    return x


def build_latest_snapshot_map(
    feat: pd.DataFrame,
    group_col: Optional[str],
    weighted_cols: list[str],
) -> pd.DataFrame:
    keep_cols = ["ticker", "rebalance_month", "year", "quarter", "name"]
    if group_col and group_col in feat.columns:
        keep_cols.append(group_col)

    for c in weighted_cols:
        if c in feat.columns and c not in keep_cols:
            keep_cols.append(c)

    x = feat[keep_cols].copy()
    x = x.sort_values(["ticker", "rebalance_month"])
    x = x.groupby("ticker", as_index=False).tail(1).copy()

    ren = {
        "rebalance_month": "latest_available_rebalance_month",
        "year": "latest_available_year",
        "quarter": "latest_available_quarter",
        "name": "latest_available_name",
    }
    if group_col and group_col in x.columns:
        ren[group_col] = "latest_available_group"

    for c in weighted_cols:
        if c in x.columns:
            ren[c] = f"latest_{c}"

    return x.rename(columns=ren)


def classify_missing_reason(row: pd.Series, target_dt: pd.Timestamp, weighted_cols: list[str]) -> str:
    if pd.notna(row.get("score")):
        return "scored_in_target_cohort"

    latest_rb = row.get("latest_available_rebalance_month")
    if pd.isna(latest_rb):
        return "no_feature_history"

    latest_rb = pd.Timestamp(latest_rb)
    if latest_rb < pd.Timestamp(target_dt):
        latest_vals = [row.get(f"latest_{c}") for c in weighted_cols]
        if len(latest_vals) > 0 and all(pd.isna(v) for v in latest_vals):
            return f"not_in_target_cohort_and_latest_weighted_factors_missing(latest={latest_rb.date()})"
        return f"not_in_target_cohort(latest={latest_rb.date()})"

    if latest_rb > pd.Timestamp(target_dt):
        return f"latest_available_after_target(latest={latest_rb.date()})"

    return "in_target_cohort_but_unscored"


# --------------------------------------------------------------------------------------
# AI overlay helpers
# --------------------------------------------------------------------------------------

def _to_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _safe_mean(series: pd.Series) -> float:
    x = _to_num(series)
    return float(x.mean()) if x.notna().sum() > 0 else np.nan


def _safe_std(series: pd.Series) -> float:
    x = _to_num(series)
    return float(x.std()) if x.notna().sum() > 1 else np.nan


def _extract_asof_from_name(name: str) -> str | None:
    m = re.search(r"__asof=(\d{4}-\d{2}-\d{2})__", name)
    return m.group(1) if m else None


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
    return eligible[-1][1]


def _detect_price_columns(df: pd.DataFrame) -> tuple[str, str, str]:
    tcol = next((c for c in ["ticker", "code", "종목코드", "symbol"] if c in df.columns), None)
    dcol = next((c for c in ["date", "Date", "dt", "ymd", "trd_date"] if c in df.columns), None)
    pcol = next((c for c in ["close", "Close", "adj_close", "price", "종가"] if c in df.columns), None)
    if not tcol or not dcol or not pcol:
        raise ValueError(f"Could not detect raw price columns from: {list(df.columns)}")
    return tcol, dcol, pcol


def _detect_traded_value_columns(df: pd.DataFrame) -> Optional[str]:
    return next((c for c in ["traded_value", "거래대금", "trading_value", "value"] if c in df.columns), None)


def _load_raw_prices(path: Path) -> pd.DataFrame:
    px = pd.read_parquet(path).copy()
    tcol, dcol, pcol = _detect_price_columns(px)
    tvcol = _detect_traded_value_columns(px)

    keep = [tcol, dcol, pcol]
    if tvcol:
        keep.append(tvcol)

    px = px[keep].copy()
    rename_map = {tcol: "ticker", dcol: "date", pcol: "price"}
    if tvcol:
        rename_map[tvcol] = "traded_value"
    px = px.rename(columns=rename_map)

    px["ticker"] = normalize_ticker_series(px["ticker"])
    px["date"] = pd.to_datetime(px["date"], errors="coerce")
    px["price"] = _to_num(px["price"])
    if "traded_value" in px.columns:
        px["traded_value"] = _to_num(px["traded_value"])
    else:
        px["traded_value"] = np.nan

    px = px.dropna(subset=["ticker", "date", "price"]).copy()
    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)

    px["ret_1d"] = px.groupby("ticker")["price"].pct_change()

    for win in [20, 60]:
        px[f"ret_{win}d"] = px.groupby("ticker")["price"].transform(lambda s: s / s.shift(win) - 1.0)
        px[f"vol_{win}d"] = px.groupby("ticker")["ret_1d"].transform(
            lambda s: s.rolling(win, min_periods=max(10, win // 2)).std()
        )
        px[f"tv_mean_{win}d"] = px.groupby("ticker")["traded_value"].transform(
            lambda s: s.rolling(win, min_periods=max(10, win // 2)).mean()
        )

    def trailing_mdd(price: pd.Series, win: int) -> pd.Series:
        roll_max = price.rolling(win, min_periods=max(10, win // 2)).max()
        dd = price / roll_max - 1.0
        return dd.rolling(win, min_periods=max(10, win // 2)).min()

    for win in [60, 120]:
        px[f"mdd_{win}d"] = px.groupby("ticker")["price"].transform(lambda s: trailing_mdd(s, win))

    return px


def _daily_snapshot_asof(px: pd.DataFrame, tickers: list[str], asof_date: pd.Timestamp) -> pd.DataFrame:
    if len(tickers) == 0:
        return pd.DataFrame(columns=["ticker"])

    right = px.loc[px["ticker"].isin(tickers)].copy()
    right = right.sort_values(["ticker", "date"]).reset_index(drop=True)

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

        if den > 0:
            out[f"{bucket}__score"] = (num / den).astype("float64")
        else:
            out[f"{bucket}__score"] = np.nan

    return out


def _bucket_base_abs_weight_map(active_buckets: dict[str, dict[str, float]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for bucket, fmap in active_buckets.items():
        s = 0.0
        for _, w in fmap.items():
            try:
                s += abs(float(w))
            except Exception:
                pass
        out[bucket] = float(s)
    return out


def _pred_to_bucket_shares(pred_alpha_row: dict[str, float], base_abs_map: dict[str, float], temperature: float = 1.0) -> dict[str, float]:
    buckets = list(base_abs_map.keys())
    if not buckets:
        return {}

    base = np.array([max(base_abs_map.get(b, 0.0), 1e-8) for b in buckets], dtype=float)
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


def _apply_profit_accel_cap(
    dyn_share: dict[str, float],
    base_share: dict[str, float],
    cap_delta: float | None,
    target_bucket: str = "profit_accel",
) -> dict[str, float]:
    out = dict(dyn_share)

    if cap_delta is None:
        return out
    if target_bucket not in out or target_bucket not in base_share:
        return out

    cap_delta = float(cap_delta)
    if cap_delta < 0:
        return out

    wb = float(base_share[target_bucket])
    wd = float(out[target_bucket])

    lower = max(0.0, wb - cap_delta)
    upper = min(1.0, wb + cap_delta)
    wd_new = min(max(wd, lower), upper)

    if abs(wd_new - wd) < 1e-12:
        return out

    others = [k for k in out.keys() if k != target_bucket]
    other_old_sum = sum(float(out[k]) for k in others)

    out[target_bucket] = wd_new
    remain = max(0.0, 1.0 - wd_new)

    if other_old_sum <= 0:
        base_other_sum = sum(float(base_share.get(k, 0.0)) for k in others)
        if base_other_sum <= 0:
            for k in others:
                out[k] = remain / max(len(others), 1)
        else:
            for k in others:
                out[k] = remain * float(base_share.get(k, 0.0)) / base_other_sum
    else:
        for k in others:
            out[k] = remain * float(out[k]) / other_old_sum

    s = sum(out.values())
    if s > 0:
        for k in list(out.keys()):
            out[k] = float(out[k] / s)
    return out


def _build_factor_to_bucket(active_buckets: dict[str, dict[str, float]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for bucket, fmap in active_buckets.items():
        for c in fmap.keys():
            out[c] = bucket
    return out


def _latest_history_row_before_target(dataset_path: Path, target_dt: pd.Timestamp) -> pd.Series:
    if not dataset_path.exists():
        return pd.Series(dtype="object")

    hist = pd.read_parquet(dataset_path).copy()
    if "rebalance_month" not in hist.columns:
        return pd.Series(dtype="object")

    hist["rebalance_month"] = pd.to_datetime(hist["rebalance_month"], errors="coerce")
    hist = hist.dropna(subset=["rebalance_month"]).sort_values("rebalance_month")
    hist = hist.loc[hist["rebalance_month"] < pd.Timestamp(target_dt)].copy()
    if len(hist) == 0:
        return pd.Series(dtype="object")
    return hist.iloc[-1].copy()


def _build_live_ai_feature_row(
    scored_filtered: pd.DataFrame,
    raw_px: pd.DataFrame,
    target_dt: pd.Timestamp,
    history_row: pd.Series,
    active_buckets: dict[str, dict[str, float]],
) -> dict[str, Any]:
    row: dict[str, Any] = {}

    # bucket context
    for bucket in active_buckets.keys():
        bucket_col = f"{bucket}__score"
        x = _to_num(scored_filtered.get(bucket_col, pd.Series(dtype="float64")))
        row[f"context__{bucket}__mean"] = _safe_mean(x)
        if x.notna().sum() > 0:
            q_hi = x.quantile(0.9)
            q_lo = x.quantile(0.1)
            row[f"context__{bucket}__spread_p90_p10"] = float(q_hi - q_lo) if pd.notna(q_hi) and pd.notna(q_lo) else np.nan
        else:
            row[f"context__{bucket}__spread_p90_p10"] = np.nan

    # lag1 from historical regime dataset
    for bucket in active_buckets.keys():
        row[f"lag1__{bucket}__alpha"] = history_row.get(f"target__{bucket}__alpha_next", np.nan) if len(history_row) else np.nan
    row["lag1__baseline_ret"] = history_row.get("baseline_ret_next", np.nan) if len(history_row) else np.nan

    # dailyagg universe / bucket
    tickers = scored_filtered["ticker"].astype(str).tolist()
    snap = _daily_snapshot_asof(raw_px, tickers=tickers, asof_date=target_dt)
    if len(snap) > 0:
        snap = snap.merge(
            scored_filtered[["ticker"] + [f"{b}__score" for b in active_buckets.keys() if f"{b}__score" in scored_filtered.columns]],
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
            else:
                part = part.sort_values([score_col, "ticker"], ascending=[False, True]).head(20)
                row[f"dailyagg__{bucket}__ret_20d_mean"] = _safe_mean(part["ret_20d"])
    else:
        row["dailyagg__universe__ret_20d_mean"] = np.nan
        row["dailyagg__universe__vol_20d_mean"] = np.nan
        row["dailyagg__universe__tv_mean_20d_mean"] = np.nan
        for bucket in active_buckets.keys():
            row[f"dailyagg__{bucket}__ret_20d_mean"] = np.nan

    return row


def _load_ai_overlay_bundle(ai_model_path: str) -> dict[str, Any]:
    p = Path(ai_model_path)
    if not p.exists():
        raise FileNotFoundError(f"ai model path not found: {p}")

    obj = joblib.load(p)
    if not isinstance(obj, dict):
        raise ValueError("ai model file is not a dict-like joblib pack")

    required = ["feature_cols", "bucket_names", "models", "base_abs_bucket_weight_map"]
    missing = [k for k in required if k not in obj]
    if missing:
        raise ValueError(f"ai model pack missing required keys: {missing}")
    return obj


def _infer_live_ai_overlay(
    scored_filtered: pd.DataFrame,
    target_dt: pd.Timestamp,
    active_buckets: dict[str, dict[str, float]],
    ai_bundle: dict[str, Any],
    raw_px: pd.DataFrame,
    ai_temperature: float,
    ai_cap_profit_accel_delta: float | None,
    ai_overlay_strength: float,
) -> dict[str, Any]:
    dataset_path = Path(str(ai_bundle.get("dataset_path", ""))) if ai_bundle.get("dataset_path") else None
    history_row = _latest_history_row_before_target(dataset_path, target_dt) if dataset_path else pd.Series(dtype="object")

    live_feat = _build_live_ai_feature_row(
        scored_filtered=scored_filtered,
        raw_px=raw_px,
        target_dt=target_dt,
        history_row=history_row,
        active_buckets=active_buckets,
    )

    feature_cols: list[str] = list(ai_bundle.get("feature_cols", []))
    X = pd.DataFrame([{c: live_feat.get(c, np.nan) for c in feature_cols}], columns=feature_cols)

    pred_alpha: dict[str, float] = {}
    models = ai_bundle.get("models", {})
    for bucket in ai_bundle.get("bucket_names", []):
        info = models.get(bucket, {})
        mdl = info.get("model")
        fallback_mean = float(info.get("fallback_mean", 0.0))
        if mdl is None:
            pred = fallback_mean
        else:
            pred = float(mdl.predict(X)[0])
        pred_alpha[bucket] = pred

    base_abs_map: dict[str, float] = dict(ai_bundle.get("base_abs_bucket_weight_map", {}))
    base_share = _pred_to_bucket_shares(
        pred_alpha_row={b: np.nan for b in base_abs_map.keys()},
        base_abs_map=base_abs_map,
        temperature=1.0,
    )
    dyn_share = _pred_to_bucket_shares(
        pred_alpha_row=pred_alpha,
        base_abs_map=base_abs_map,
        temperature=float(ai_temperature),
    )
    dyn_share = _apply_profit_accel_cap(
        dyn_share=dyn_share,
        base_share=base_share,
        cap_delta=ai_cap_profit_accel_delta,
        target_bucket="profit_accel",
    )

    bucket_multipliers: dict[str, float] = {}
    for bucket in base_share.keys():
        wb = float(base_share.get(bucket, 0.0))
        wd = float(dyn_share.get(bucket, 0.0))
        ratio = 1.0 if wb <= 0 else (wd / wb)
        # blend toward 1.0 for safety
        blended = 1.0 + float(ai_overlay_strength) * (ratio - 1.0)
        bucket_multipliers[bucket] = float(blended)

    return {
        "feature_row": live_feat,
        "pred_alpha": pred_alpha,
        "base_share": base_share,
        "dyn_share": dyn_share,
        "bucket_multipliers": bucket_multipliers,
        "history_rebalance_month": history_row.get("rebalance_month", pd.NaT) if len(history_row) else pd.NaT,
    }


# --------------------------------------------------------------------------------------
# Existing prep
# --------------------------------------------------------------------------------------

def prepare_features(asof: str, metric: str, feat_v: int) -> tuple[pd.DataFrame, pd.DataFrame, Path]:
    feature_candidates = [
        Path(rf"data/features/features_live/features_live__asof={asof}__metric={metric}__v={feat_v}.parquet"),
        Path(rf"data/processed/features_live__asof={asof}__metric={metric}__v={feat_v}.parquet"),
    ]
    p_feat = next((p for p in feature_candidates if p.exists()), None)
    if p_feat is None:
        raise FileNotFoundError(
            "features_live not found. tried: "
            + " / ".join(str(p) for p in feature_candidates)
        )

    feat = pd.read_parquet(p_feat).copy()
    feat["ticker"] = normalize_ticker_series(feat["ticker"])

    name_map = _load_name_map(asof, feat=feat)
    feat = attach_names(feat, name_map)

    group_map, group_src, external_group_col = load_group_map(asof)
    existing_group_col = detect_group_col(feat)

    if len(group_map) > 0 and external_group_col:
        if existing_group_col and existing_group_col in feat.columns:
            feat = feat.merge(
                group_map.rename(columns={external_group_col: f"{external_group_col}__ext"}),
                on="ticker",
                how="left",
            )
            ext_col = f"{external_group_col}__ext"
            if ext_col in feat.columns:
                feat[existing_group_col] = feat[existing_group_col].where(
                    feat[existing_group_col].notna(), feat[ext_col]
                )
                feat = feat.drop(columns=[ext_col])
            nn = int(feat[existing_group_col].notna().sum())
            print(f"[OK] group map filled from {group_src}: group_col={existing_group_col} non_null={nn}/{len(feat)}")
        else:
            feat = feat.merge(group_map, on="ticker", how="left")
            nn = int(feat[external_group_col].notna().sum()) if external_group_col in feat.columns else 0
            print(f"[OK] group map merged from {group_src}: group_col={external_group_col} non_null={nn}/{len(feat)}")

    feat["quarter_key"] = feat["year"].astype(int) * 100 + feat["quarter"].astype(int)
    feat["rebalance_month"] = compute_rebalance_month_from_yq(feat["year"], feat["quarter"])

    return feat, name_map, p_feat


def choose_target_rebalance(feat: pd.DataFrame, target_date: str) -> pd.Timestamp:
    rb_months = sorted(pd.to_datetime(feat["rebalance_month"]).dropna().unique())
    if not rb_months:
        raise RuntimeError("No rebalance_month found in features_live.")

    if target_date:
        target = pd.Timestamp(target_date)
        if target not in rb_months:
            avail_tail = [str(pd.Timestamp(x).date()) for x in rb_months[-8:]]
            raise ValueError(f"target_date {target_date} not found. Available tail: {avail_tail}")
        return target

    return pd.Timestamp(rb_months[-1])


def make_scores_full_universe(
    g: pd.DataFrame,
    weights: dict[str, float],
    filters: dict,
    target_dt: pd.Timestamp,
    name_map: pd.DataFrame,
    group_col: Optional[str],
    filter_fallback: str = "full",
    clip_z: float | None = 5.0,
    hold_bonus: float = 0.0,
    current_tickers: set[str] | None = None,
    use_robust_z: bool = False,
    raw_factors: list[str] | None = None,
    clip_tiers: list[dict] | None = None,
    ai_bundle: dict[str, Any] | None = None,
    ai_raw_px: pd.DataFrame | None = None,
    ai_temperature: float = 1.0,
    ai_cap_profit_accel_delta: float | None = None,
    ai_overlay_strength: float = 1.0,
    price_history: pd.DataFrame | None = None,
    expectation_cfg: dict[str, Any] | None = None,
    quality_penalty_cfg: dict[str, Any] | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any] | None]:
    full = g.copy()
    full["passed_filters"] = False
    full["filter_status"] = "excluded_by_filter"

    filtered = apply_filters(g.copy(), filters, verbose=True)

    if "op_qoq" in filtered.columns:
        op_qoq_num = pd.to_numeric(filtered["op_qoq"], errors="coerce")
        strict_qoq = filtered.loc[op_qoq_num.notna() & (op_qoq_num > 0)].copy()
        if len(strict_qoq) == 0:
            print("[WARN] skip strict filter op_qoq > 0: would empty the rebalance universe")
        else:
            filtered = strict_qoq

    if len(filtered) == 0:
        msg = f"All rows filtered out for rebalance_month={pd.Timestamp(target_dt).date()} with filters={filters}"
        if filter_fallback == "error":
            raise ValueError(msg)
        print(f"[WARN] {msg}; falling back to unfiltered universe (legacy behavior)")
        scored_base = g.copy()
        full["passed_filters"] = True
        full["filter_status"] = "fallback_full_universe"
    else:
        scored_base = filtered.copy()
        passed_set = set(scored_base["ticker"].astype(str).tolist())
        full.loc[full["ticker"].isin(passed_set), "passed_filters"] = True
        full.loc[full["ticker"].isin(passed_set), "filter_status"] = "passed"

    gg = scored_base.copy()
    gg["score_total_base"] = 0.0
    gg["score_total"] = 0.0
    used_cols = []
    raw_factors = set(raw_factors or [])

    def _mask_mult_for(col: str) -> pd.Series:
        if ("CFO" in col) or ("Quality_CFO" in col) or ("CFO_to_Assets" in col):
            isnull = pd.to_numeric(gg.get("CFO_isnull", 0.0), errors="coerce").fillna(0.0)
            warn = pd.to_numeric(gg.get("CFO_warn", 0.0), errors="coerce").fillna(0.0)
            return (1.0 - 0.7 * isnull - 0.4 * warn).clip(0.0, 1.0).astype("float64")
        return pd.Series(1.0, index=gg.index, dtype="float64")

    # base contributions
    for c, ww in weights.items():
        ww = float(ww)
        if ww == 0.0 or c not in gg.columns:
            continue

        used_cols.append(c)
        raw = pd.to_numeric(gg[c], errors="coerce")
        if c in raw_factors:
            z = raw.fillna(0.0).astype("float64")
            rk = z.rank(ascending=False, method="min")
            mult = pd.Series(1.0, index=gg.index, dtype="float64")
            contrib_base = ww * z
        else:
            z_base = standardize_factor(safe_fill_for_z(raw), use_robust_z=use_robust_z, clip_z=None)
            z = apply_piecewise_clip(
                z_base,
                clip_z=None if clip_z is None or clip_z < 0 else clip_z,
                clip_tiers=clip_tiers,
            )
            rk = z.rank(ascending=False, method="min")
            mult = _mask_mult_for(c)
            contrib_base = ww * z * mult

        gg[f"{c}__raw"] = raw
        gg[f"{c}__signal"] = z
        gg[f"{c}__z"] = z
        gg[f"{c}__rank"] = rk
        gg[f"{c}__mult"] = mult
        gg[f"{c}__contrib_base"] = contrib_base
        gg["score_total_base"] += contrib_base

    ai_overlay_info: dict[str, Any] | None = None

    # optional AI overlay
    if ai_bundle is not None:
        active_buckets = _build_active_bucket_specs(weights=weights, df_cols=list(gg.columns), bucket_specs=DEFAULT_BUCKET_SPECS)
        gg = _compute_bucket_scores(gg, active_buckets=active_buckets)

        if ai_raw_px is None:
            raise ValueError("ai_bundle is provided but ai_raw_px is None")

        ai_overlay_info = _infer_live_ai_overlay(
            scored_filtered=gg,
            target_dt=target_dt,
            active_buckets=active_buckets,
            ai_bundle=ai_bundle,
            raw_px=ai_raw_px,
            ai_temperature=float(ai_temperature),
            ai_cap_profit_accel_delta=ai_cap_profit_accel_delta,
            ai_overlay_strength=float(ai_overlay_strength),
        )

        factor_to_bucket = _build_factor_to_bucket(active_buckets)
        gg["score_total"] = 0.0

        for c in used_cols:
            bucket = factor_to_bucket.get(c, None)
            ai_mult = float(ai_overlay_info["bucket_multipliers"].get(bucket, 1.0)) if bucket else 1.0
            gg[f"{c}__bucket"] = bucket if bucket is not None else pd.NA
            gg[f"{c}__ai_mult"] = ai_mult
            gg[f"{c}__contrib"] = gg[f"{c}__contrib_base"] * ai_mult
            gg["score_total"] += gg[f"{c}__contrib"]

        # stamp overlay summary columns so downstream csv keeps the info
        for bucket, val in ai_overlay_info["pred_alpha"].items():
            gg[f"ai_pred_alpha__{bucket}"] = float(val)
        for bucket, val in ai_overlay_info["base_share"].items():
            gg[f"ai_base_share__{bucket}"] = float(val)
        for bucket, val in ai_overlay_info["dyn_share"].items():
            gg[f"ai_dyn_share__{bucket}"] = float(val)
        for bucket, val in ai_overlay_info["bucket_multipliers"].items():
            gg[f"ai_bucket_mult__{bucket}"] = float(val)
        for k, v in ai_overlay_info["feature_row"].items():
            gg[f"ai_feat__{k}"] = v

        gg["ai_overlay_active"] = 1
    else:
        gg["score_total"] = gg["score_total_base"]
        for c in used_cols:
            gg[f"{c}__contrib"] = gg[f"{c}__contrib_base"]
            gg[f"{c}__ai_mult"] = 1.0
        gg["ai_overlay_active"] = 0

    gg = apply_expectation_overlay(
        gg,
        expectation_cfg,
        asof_date=target_dt,
        price_history=price_history,
    )
    gg = apply_quality_soft_penalty(
        gg,
        quality_penalty_cfg,
    )

    gg["score_base"] = gg["score_total_base"]
    gg["score"] = gg["score_total"]
    current_tickers = current_tickers or set()
    gg["hold_bonus_applied"] = gg["ticker"].astype(str).isin(current_tickers).astype(float) * float(hold_bonus)
    gg["score_adj"] = gg["score"] + gg["hold_bonus_applied"]
    gg["score_rank"] = gg["score"].rank(ascending=False, method="min")
    gg["score_adj_rank"] = gg["score_adj"].rank(ascending=False, method="min")
    gg["rebalance_month"] = target_dt

    keep = [
        "ticker",
        "rebalance_month",
        "score_total_base",
        "score_total",
        "score_base",
        "score",
        "hold_bonus_applied",
        "score_adj",
        "score_rank",
        "score_adj_rank",
        "ai_overlay_active",
    ]
    for c in used_cols:
        for suf in ("__raw", "__z", "__rank", "__mult", "__contrib_base", "__contrib", "__ai_mult", "__bucket"):
            col = f"{c}{suf}"
            if col in gg.columns:
                keep.append(col)

    for c in list(gg.columns):
        if c.startswith("ai_pred_alpha__") or c.startswith("ai_base_share__") or c.startswith("ai_dyn_share__") or c.startswith("ai_bucket_mult__") or c.startswith("ai_feat__"):
            keep.append(c)

    for c in [
        "expectation_overlay_active",
        "expectation_component_count",
        "expectation_valuation_z",
        "expectation_mom6_z",
        "expectation_mom12_z",
        "expectation_score",
        "expectation_penalty",
        "score_total_pre_expect",
        "score_total_post_expect",
        "quality_soft_penalty_active",
        "quality_penalty_netincome_ttm_nonpositive",
        "quality_penalty_netincome_acc2_negative",
        "quality_penalty_cfo_warn",
        "quality_penalty_cfo_isnull",
        "quality_penalty_total",
        "score_total_pre_quality",
        "score_total_post_quality",
        "mcap",
        "mcap_rank_pct",
        "mcap_group",
        "mcap_grouping_active",
        "mcap_group_selection_reason",
    ]:
        if c in gg.columns:
            keep.append(c)

    for c in ["name", "corp_code", "year", "quarter", "CFO_isnull", "CFO_warn", "NetIncome_ttm", "NetIncome_acc2", group_col]:
        if c and c in gg.columns and c not in keep:
            keep.insert(1, c)

    scored = gg[list(dict.fromkeys(keep))].copy()
    scored = attach_names(scored, name_map)

    full = attach_names(full, name_map)
    full["rebalance_month"] = target_dt
    full = full.merge(
        scored.drop(columns=["name"], errors="ignore"),
        on=["ticker", "rebalance_month"],
        how="left",
        suffixes=("", "_scored"),
    )

    return full, scored, ai_overlay_info


def main():
    ap = argparse.ArgumentParser(description="Score full universe for the latest rebalance and inspect current holdings.")
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--feat_v", type=int, required=True)
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--max_per_group", type=int, default=0)
    ap.add_argument("--group_col", default="")
    ap.add_argument("--target_date", default="", help="Optional rebalance date, e.g. 2026-02-28")
    ap.add_argument("--filter_fallback", choices=["full", "error"], default="full")
    ap.add_argument("--clip_z", type=float, default=None, help="Clip standardized factor z-scores to +/- this value. Set negative to disable clipping. If omitted, use strategy yaml default when available.")
    ap.add_argument("--use_robust_z", action="store_true", default=None, help="Use median/MAD-based robust z-score instead of mean/std z-score. If omitted, use strategy yaml default when available.")
    ap.add_argument("--holdings_csv", default="", help="Explicit current holdings CSV path. Required in production.")
    ap.add_argument("--current_csv", default="", help="DEPRECATED alias for --holdings_csv. Avoid using this.")
    ap.add_argument("--out_prefix", default="", help="Optional output prefix")

    # AI overlay args
    ap.add_argument("--ai_model_path", default="", help="Optional factor_weight_model.joblib path. If omitted, scoring stays baseline-only.")
    ap.add_argument("--ai_raw_px_v", type=int, default=1, help="raw daily prices version for live dailyagg feature inference")
    ap.add_argument("--ai_temperature", type=float, default=1.0)
    ap.add_argument("--ai_cap_profit_accel_delta", type=float, default=-1.0, help="If >=0, clamp profit_accel share to baseline±delta")
    ap.add_argument("--ai_overlay_strength", type=float, default=1.0, help="0=off, 1=full overlay")
    args = ap.parse_args()

    holdings_path = resolve_holdings_csv_arg(args.holdings_csv, args.current_csv)
    print(f"[INFO] explicit holdings csv: {holdings_path}")

    feat, name_map, feat_path = prepare_features(args.asof, args.metric, args.feat_v)
    print(f"[INFO] features_live used: {feat_path}")
    target_dt = choose_target_rebalance(feat, args.target_date)

    strat_path = Path("configs/strategies.yaml")
    if not strat_path.exists():
        strat_path = Path("strategies.yaml")
    if not strat_path.exists():
        raise FileNotFoundError("strategies.yaml not found in configs/ or project root.")

    weights, filters, desc = load_strategy(strat_path, args.strategy)
    runtime_cfg = load_strategy_runtime(strat_path, args.strategy)
    resolved_clip_z = args.clip_z if args.clip_z is not None else runtime_cfg.get("clip_z", 5.0)
    resolved_use_robust_z = bool(args.use_robust_z) if args.use_robust_z is not None else bool(runtime_cfg.get("use_robust_z", False))
    resolved_hold_bonus = float(runtime_cfg.get("hold_bonus", 0.0))
    resolved_clip_tiers = list(runtime_cfg.get("clip_tiers", [])) if isinstance(runtime_cfg.get("clip_tiers", []), list) else []
    selection_cfg = strategy_cfg = None
    try:
        import yaml as _yaml  # type: ignore
        with open(strat_path, "r", encoding="utf-8") as _f:
            _cfg_all = _yaml.safe_load(_f) or {}
        strategy_cfg = ((_cfg_all.get("strategies") or {}).get(args.strategy) or {}) if isinstance(_cfg_all, dict) else {}
    except Exception:
        strategy_cfg = {}
    selection_cfg = strategy_cfg.get("selection", {}) if isinstance(strategy_cfg, dict) else {}
    resolved_portfolio_size = int(selection_cfg.get("portfolio_size", args.k)) if isinstance(selection_cfg, dict) else int(args.k)
    resolved_keep_current_top_n = int(selection_cfg.get("keep_current_top_n", 0)) if isinstance(selection_cfg, dict) else 0
    resolved_raw_factors = list(runtime_cfg.get("raw_factors", [])) if isinstance(runtime_cfg, dict) else []
    if not resolved_raw_factors:
        resolved_raw_factors = [c for c in weights.keys() if c in {"op_growth_streak2", "rev_growth_streak2"}]
    weighted_cols = [c for c, ww in weights.items() if float(ww) != 0.0]
    print(f"[OK] strategies_yaml: {strat_path}")
    if desc:
        print(f"[INFO] strategy desc: {desc}")

    modules_cfg = load_strategy_modules_config(strat_path, args.strategy)
    expectation_cfg = modules_cfg.get("expectation_overlay", {})
    quality_penalty_cfg = modules_cfg.get("quality_soft_penalty", {})
    mcap_grouping_cfg = modules_cfg.get("mcap_grouping", {})

    group_col = args.group_col.strip() or detect_group_col(feat)
    if group_col and group_col in feat.columns:
        print(f"[OK] group column detected: {group_col}")
    else:
        group_col = None
        if args.max_per_group > 0:
            print("[WARN] max_per_group requested but no usable group column detected")

    price_hist = load_price_history(args.asof, args.metric)

    g = feat.loc[feat["rebalance_month"] == target_dt].copy()
    g = ensure_filter_alias_columns(g)
    if len(g) == 0:
        raise RuntimeError(f"No rows found for rebalance_month={target_dt.date()}")

    cohort_tickers = int(g["ticker"].nunique())
    all_tickers = int(feat["ticker"].nunique())
    print(f"[INFO] feature tickers total={all_tickers} target_cohort={cohort_tickers} coverage={cohort_tickers/max(all_tickers,1):.3f}")

    current_for_bonus = load_current_holdings_csv(holdings_path)
    current_for_bonus = validate_holdings_csv(current_for_bonus, holdings_path)
    current_tickers = set(current_for_bonus["ticker"].astype(str).tolist())

    # optional AI overlay bundle
    ai_bundle = None
    ai_raw_px = None
    if args.ai_model_path.strip():
        ai_bundle = _load_ai_overlay_bundle(args.ai_model_path.strip())
        raw_px_path = _pick_raw_prices_path(args.asof, int(args.ai_raw_px_v))
        if raw_px_path is None:
            raise FileNotFoundError(
                f"AI overlay requested but raw prices file not found for asof={args.asof}, raw_px_v={args.ai_raw_px_v}"
            )
        ai_raw_px = _load_raw_prices(raw_px_path)
        print(f"[OK] ai overlay model : {args.ai_model_path}")
        print(f"[OK] ai raw prices    : {raw_px_path}")

    full_scored, scored_only, ai_overlay_info = make_scores_full_universe(
        g=g,
        weights=weights,
        filters=filters,
        target_dt=target_dt,
        name_map=name_map,
        group_col=group_col,
        filter_fallback=args.filter_fallback,
        clip_z=None if resolved_clip_z is None or resolved_clip_z < 0 else resolved_clip_z,
        use_robust_z=resolved_use_robust_z,
        hold_bonus=resolved_hold_bonus,
        current_tickers=current_tickers,
        raw_factors=resolved_raw_factors,
        clip_tiers=resolved_clip_tiers,
        ai_bundle=ai_bundle,
        ai_raw_px=ai_raw_px,
        ai_temperature=float(args.ai_temperature),
        ai_cap_profit_accel_delta=None if float(args.ai_cap_profit_accel_delta) < 0 else float(args.ai_cap_profit_accel_delta),
        ai_overlay_strength=float(args.ai_overlay_strength),
        price_history=price_hist,
        expectation_cfg=expectation_cfg,
        quality_penalty_cfg=quality_penalty_cfg,
    )

    scored_sorted = scored_only.sort_values(["score_adj", "score", "ticker"], ascending=[False, False, True]).reset_index(drop=True)
    effective_group_col = group_col if (group_col and group_col in scored_sorted.columns) else detect_group_col(scored_sorted)
    target_topk = select_target_portfolio_with_mcap_groups(
        scored_sorted,
        portfolio_size=resolved_portfolio_size,
        keep_current_top_n=resolved_keep_current_top_n,
        effective_group_col=effective_group_col,
        max_per_group=int(args.max_per_group),
        current_tickers=current_tickers,
        base_selector=select_target_portfolio,
        grouping_cfg=mcap_grouping_cfg,
    ).copy()
    selected_set = set(target_topk["ticker"].astype(str).tolist())

    full_scored["selected_topk"] = full_scored["ticker"].astype(str).isin(selected_set)
    full_scored["selected_topk"] = full_scored["selected_topk"].astype(int)

    full_scored = full_scored.sort_values(
        ["passed_filters", "score", "ticker"],
        ascending=[False, False, True],
        na_position="last",
    ).reset_index(drop=True)

    default_prefix = (
        f"data/live/scores/latest_scores__asof={args.asof}__metric={args.metric}"
        f"__strat={args.strategy}__featv={args.feat_v}__target={target_dt.date()}"
    )
    out_prefix = args.out_prefix if args.out_prefix else default_prefix

    out_full = Path(out_prefix + "__full_universe.csv")
    out_topk = Path(out_prefix + "__topk.csv")
    out_full.parent.mkdir(parents=True, exist_ok=True)

    full_scored.to_csv(out_full, index=False, encoding="utf-8-sig")
    target_topk.to_csv(out_topk, index=False, encoding="utf-8-sig")

    coverage = {
        "asof": args.asof,
        "metric": args.metric,
        "feat_v": int(args.feat_v),
        "strategy": args.strategy,
        "target_date": str(pd.Timestamp(target_dt).date()),
        "holdings_csv": str(holdings_path),
        "features_live_path": str(feat_path),
        "all_feature_tickers": all_tickers,
        "target_cohort_tickers": cohort_tickers,
        "target_cohort_coverage": cohort_tickers / max(all_tickers, 1),
        "passed_filters_rows": int(full_scored["passed_filters"].sum()),
        "hold_bonus": resolved_hold_bonus,
        "clip_z": resolved_clip_z,
        "use_robust_z": resolved_use_robust_z,
        "selected_topk_rows": int(len(target_topk)),
        "raw_factors": resolved_raw_factors,
        "clip_tiers": resolved_clip_tiers,
        "portfolio_size": resolved_portfolio_size,
        "keep_current_top_n": resolved_keep_current_top_n,
        "ai_overlay_active": bool(ai_bundle is not None),
        "ai_model_path": args.ai_model_path.strip(),
        "ai_temperature": float(args.ai_temperature),
        "ai_cap_profit_accel_delta": None if float(args.ai_cap_profit_accel_delta) < 0 else float(args.ai_cap_profit_accel_delta),
        "ai_overlay_strength": float(args.ai_overlay_strength),
        "expectation_overlay": expectation_cfg,
        "quality_soft_penalty": quality_penalty_cfg,
        "mcap_grouping": mcap_grouping_cfg,
    }
    if ai_overlay_info is not None:
        coverage["ai_pred_alpha"] = ai_overlay_info["pred_alpha"]
        coverage["ai_base_share"] = ai_overlay_info["base_share"]
        coverage["ai_dyn_share"] = ai_overlay_info["dyn_share"]
        coverage["ai_bucket_multipliers"] = ai_overlay_info["bucket_multipliers"]
        coverage["ai_history_rebalance_month"] = str(pd.Timestamp(ai_overlay_info["history_rebalance_month"]).date()) if pd.notna(ai_overlay_info["history_rebalance_month"]) else None
    out_cov = Path(out_prefix + "__coverage.json")
    out_cov.write_text(json.dumps(coverage, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 110)
    print(f"[LATEST REBALANCE SCORE] target date : {target_dt.date()}")
    print(f"[LATEST REBALANCE SCORE] cohort size  : {len(g)}")
    print(f"[LATEST REBALANCE SCORE] passed      : {int(full_scored['passed_filters'].sum())}")
    print(f"[LATEST REBALANCE SCORE] selected    : {len(target_topk)}")
    if ai_overlay_info is not None:
        print(f"[AI OVERLAY] pred_alpha         : {ai_overlay_info['pred_alpha']}")
        print(f"[AI OVERLAY] dyn_share          : {ai_overlay_info['dyn_share']}")
        print(f"[AI OVERLAY] bucket_multipliers : {ai_overlay_info['bucket_multipliers']}")
    print("=" * 110)

    show_top = [c for c in [
        "ticker", "name", "score_base", "score", "score_adj", "hold_bonus_applied", "score_rank", "score_adj_rank",
        "selection_bucket", group_col, "mcap_group", "mcap_rank_pct", "mcap_group_selection_reason",
        "ai_overlay_active", "expectation_overlay_active", "expectation_score", "expectation_penalty",
        "quality_soft_penalty_active", "quality_penalty_total",
        "ai_pred_alpha__profit_accel", "ai_pred_alpha__revenue_support", "ai_pred_alpha__balance_sheet",
        "ai_bucket_mult__profit_accel", "ai_bucket_mult__revenue_support", "ai_bucket_mult__balance_sheet",
        "OpIncome_acc2__raw", "OpIncome_acc2__contrib",
        "OpIncome_acc2_log1p__raw", "OpIncome_acc2_log1p__contrib",
        "op_growth_streak2__raw", "op_growth_streak2__contrib",
        "Revenue_acc2__raw", "Revenue_acc2__contrib",
        "rev_growth_streak2__raw", "rev_growth_streak2__contrib",
        "Debt_to_Equity_log__raw", "Debt_to_Equity_log__contrib",
        "Quality_CFO_to_Assets__raw", "Quality_CFO_to_Assets__contrib",
        "CFO_isnull",
    ] if c and c in target_topk.columns]

    print("\n[TOP-K TARGET]")
    print(target_topk[show_top].to_string(index=False))

    current = load_current_holdings_csv(holdings_path)
    current = validate_holdings_csv(current, holdings_path)
    current = attach_current_weights_from_prices(
        current=current,
        asof=args.asof,
        metric=args.metric,
        target_dt=target_dt,
    )

    latest_map = build_latest_available_map(feat)
    latest_snapshot = build_latest_snapshot_map(feat, group_col=group_col, weighted_cols=weighted_cols)

    inspect = current.merge(
        full_scored,
        on="ticker",
        how="left",
        suffixes=("_current", ""),
    )

    inspect = inspect.merge(latest_map, on="ticker", how="left")
    inspect = inspect.merge(latest_snapshot, on="ticker", how="left")

    if "name_current" in inspect.columns:
        if "name" not in inspect.columns:
            inspect["name"] = inspect["name_current"]
        else:
            inspect["name"] = inspect["name"].where(inspect["name"].notna(), inspect["name_current"])

    if "name" in inspect.columns and "latest_available_name" in inspect.columns:
        inspect["name"] = inspect["name"].where(inspect["name"].notna(), inspect["latest_available_name"])

    inspect["in_target_topk"] = inspect["ticker"].astype(str).isin(selected_set).astype(int)

    if "filter_status" not in inspect.columns:
        inspect["filter_status"] = pd.NA
    inspect["filter_status"] = inspect["filter_status"].fillna("not_in_target_cohort")

    if "passed_filters" not in inspect.columns:
        inspect["passed_filters"] = False
    inspect["passed_filters"] = inspect["passed_filters"].fillna(False)

    if "selected_topk" not in inspect.columns:
        inspect["selected_topk"] = 0
    inspect["selected_topk"] = inspect["selected_topk"].fillna(0).astype(int)

    inspect["cohort_status"] = inspect["score"].apply(lambda x: "in_target_cohort" if pd.notna(x) else "not_in_target_cohort")
    inspect["score_availability_reason"] = inspect.apply(
        lambda r: classify_missing_reason(r, target_dt=target_dt, weighted_cols=weighted_cols),
        axis=1,
    )

    if group_col and group_col in inspect.columns and "latest_available_group" in inspect.columns:
        inspect[group_col] = inspect[group_col].where(inspect[group_col].notna(), inspect["latest_available_group"])

    for c in weighted_cols:
        latest_c = f"latest_{c}"
        raw_c = f"{c}__raw"
        if raw_c in inspect.columns and latest_c in inspect.columns:
            inspect[raw_c] = inspect[raw_c].where(inspect[raw_c].notna(), inspect[latest_c])

    out_cur = Path(out_prefix + "__current_holdings_scored.csv")
    inspect.to_csv(out_cur, index=False, encoding="utf-8-sig")

    show_cur = [c for c in [
        "ticker", "name", "shares",
        "current_price", "price_date", "current_value", "current_weight",
        "cohort_status", "score_availability_reason", "filter_status", "passed_filters", "selected_topk", "in_target_topk",
        "latest_available_rebalance_month", "latest_available_year", "latest_available_quarter",
        "score_base", "score", "score_rank", group_col, "mcap_group", "mcap_rank_pct",
        "expectation_overlay_active", "expectation_score", "expectation_penalty",
        "quality_soft_penalty_active", "quality_penalty_total",
        "ai_pred_alpha__profit_accel", "ai_pred_alpha__revenue_support", "ai_pred_alpha__balance_sheet",
        "ai_bucket_mult__profit_accel", "ai_bucket_mult__revenue_support", "ai_bucket_mult__balance_sheet",
        "OpIncome_acc2__raw", "OpIncome_acc2_log1p__raw", "op_growth_streak2__raw", "Revenue_acc2__raw", "rev_growth_streak2__raw",
        "Debt_to_Equity_log__raw", "Quality_CFO_to_Assets__raw",
        "CFO_isnull",
    ] if c and c in inspect.columns]

    print("\n[CURRENT HOLDINGS INSPECTION]")
    print(inspect[show_cur].to_string(index=False))
    print(f"\n[OK] saved current holdings scored: {out_cur}")

    print(f"\n[OK] saved full universe scored : {out_full}")
    print(f"[OK] saved top-k scored        : {out_topk}")
    print(f"[OK] saved coverage json       : {out_cov}")


if __name__ == "__main__":
    main()