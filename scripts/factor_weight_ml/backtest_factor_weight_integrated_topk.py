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
    detect_group_col,
    select_top_k_with_group_cap,
    load_group_map,
)

DEFAULT_BUCKET_SPECS: dict[str, list[str]] = {
    "profit_accel": ["OpIncome_acc2_log1p", "op_growth_streak2"],
    "revenue_support": ["Revenue_acc2", "rev_growth_streak2"],
    "quality": ["Quality_CFO_to_Assets"],
    "balance_sheet": ["Debt_to_Equity_log"],
}
RAW_FACTOR_DEFAULTS = {"op_growth_streak2", "rev_growth_streak2"}


def _to_num(s: Any) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


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
    raise FileNotFoundError("features_live not found. tried: " + " / ".join(str(p) for p in candidates))


def _pick_returns_path(asof: str, metric: str, ret_src: str, ret_v: int) -> Path:
    p = Path(rf"data/processed/returns_monthly__src={ret_src}__asof={asof}__metric={metric}__v={ret_v}.parquet")
    if not p.exists():
        raise FileNotFoundError(p)
    return p


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


def _fill_group_map(feat: pd.DataFrame, asof: str) -> pd.DataFrame:
    out = feat.copy()
    try:
        group_map, group_src, external_group_col = load_group_map(asof)
    except Exception:
        return out
    if len(group_map) == 0 or not external_group_col:
        return out
    existing_group_col = detect_group_col(out)
    if existing_group_col and existing_group_col in out.columns:
        gm = group_map.rename(columns={external_group_col: f"{external_group_col}__ext"})
        out = out.merge(gm, on="ticker", how="left")
        ext_col = f"{external_group_col}__ext"
        if ext_col in out.columns:
            out[existing_group_col] = out[existing_group_col].where(out[existing_group_col].notna(), out[ext_col])
            out = out.drop(columns=[ext_col])
    else:
        out = out.merge(group_map, on="ticker", how="left")
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
    x = _strict_filter_op_qoq(x, rebalance_month=rebalance_month)
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


def _build_active_bucket_specs(weights: dict[str, float], df_cols: list[str], bucket_specs: dict[str, list[str]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for bucket, cols in bucket_specs.items():
        active: dict[str, float] = {}
        for c in cols:
            if c in weights and float(weights[c]) != 0.0 and c in df_cols:
                active[c] = float(weights[c])
        if active:
            out[bucket] = active
    return out


def _bucket_base_abs_weight_map(active_buckets: dict[str, dict[str, float]]) -> dict[str, float]:
    return {b: float(sum(abs(float(w)) for w in fmap.values())) for b, fmap in active_buckets.items()}


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


def _apply_profit_accel_cap(dyn_share: dict[str, float], base_share: dict[str, float], cap_delta: float | None, target_bucket: str = "profit_accel") -> dict[str, float]:
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
    others = [k for k in out if k != target_bucket]
    other_old_sum = sum(float(out[k]) for k in others)
    out[target_bucket] = wd_new
    remain = max(0.0, 1.0 - wd_new)
    if other_old_sum <= 0:
        base_other_sum = sum(float(base_share.get(k, 0.0)) for k in others)
        for k in others:
            out[k] = remain * float(base_share.get(k, 0.0)) / base_other_sum if base_other_sum > 0 else remain / max(len(others), 1)
    else:
        for k in others:
            out[k] = remain * float(out[k]) / other_old_sum
    s = sum(out.values())
    if s > 0:
        for k in list(out.keys()):
            out[k] = float(out[k] / s)
    return out


def _factor_to_bucket(active_buckets: dict[str, dict[str, float]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for bucket, fmap in active_buckets.items():
        for c in fmap:
            out[c] = bucket
    return out


def _compute_scores(cohort: pd.DataFrame, weights: dict[str, float], runtime_cfg: dict[str, Any], bucket_multipliers: dict[str, float] | None) -> pd.DataFrame:
    gg = cohort.copy()
    clip_z = runtime_cfg.get("clip_z", 5.0)
    clip_tiers = runtime_cfg.get("clip_tiers", [])
    use_robust_z = bool(runtime_cfg.get("use_robust_z", False))
    raw_factors = set(runtime_cfg.get("raw_factors", [])) or set(RAW_FACTOR_DEFAULTS)
    active_buckets = _build_active_bucket_specs(weights, list(gg.columns), DEFAULT_BUCKET_SPECS)
    f2b = _factor_to_bucket(active_buckets)

    gg["score_base"] = 0.0
    gg["score_ai"] = 0.0
    used_cols: list[str] = []

    for c, ww0 in weights.items():
        ww = float(ww0)
        if ww == 0.0 or c not in gg.columns:
            continue
        used_cols.append(c)
        raw = _to_num(gg[c])
        mult = _mask_mult_for(gg, c)
        if c in raw_factors:
            signal = raw.fillna(0.0).astype("float64")
            contrib_base = ww * signal
        else:
            fill = raw.median(skipna=True) if raw.notna().any() else 0.0
            signal = standardize_factor(
                raw.fillna(fill),
                use_robust_z=use_robust_z,
                clip_z=None if clip_z is None or float(clip_z) < 0 else float(clip_z),
                clip_tiers=clip_tiers if isinstance(clip_tiers, list) else [],
            ).astype("float64")
            contrib_base = ww * signal * mult
        bucket = f2b.get(c)
        ai_mult = float((bucket_multipliers or {}).get(bucket, 1.0)) if bucket else 1.0
        gg[f"{c}__signal"] = signal
        gg[f"{c}__contrib_base"] = contrib_base
        gg[f"{c}__bucket"] = bucket if bucket else pd.NA
        gg[f"{c}__ai_mult"] = ai_mult
        gg[f"{c}__contrib_ai"] = contrib_base * ai_mult
        gg["score_base"] += contrib_base
        gg["score_ai"] += contrib_base * ai_mult

    gg["score_base_rank"] = gg["score_base"].rank(ascending=False, method="min")
    gg["score_ai_rank"] = gg["score_ai"].rank(ascending=False, method="min")
    return gg


def _load_predictions(preds_path: Path) -> pd.DataFrame:
    if preds_path.suffix.lower() == ".csv":
        preds = pd.read_csv(preds_path)
    else:
        preds = pd.read_parquet(preds_path)
    preds["rebalance_month"] = pd.to_datetime(preds["rebalance_month"], errors="coerce")
    return preds.dropna(subset=["rebalance_month"]).sort_values("rebalance_month").reset_index(drop=True)


def _weights_for_selected(selected: pd.DataFrame, k: int) -> dict[str, float]:
    tickers = selected["ticker"].astype(str).tolist()
    if not tickers:
        return {}
    w = 1.0 / min(len(tickers), int(k))
    return {tk: w for tk in tickers[: int(k)]}


def _turnover(prev: dict[str, float], new: dict[str, float]) -> float:
    keys = set(prev) | set(new)
    return float(sum(abs(float(new.get(k, 0.0)) - float(prev.get(k, 0.0))) for k in keys))


def _portfolio_month_ret(ret: pd.DataFrame, tickers: list[str], month_end: pd.Timestamp) -> float:
    if not tickers:
        return np.nan
    seg = ret.loc[(ret["month_end"] == month_end) & (ret["ticker"].isin(tickers)), ["ticker", "ret_1m"]].copy()
    if len(seg) == 0:
        return 0.0
    # missing selected tickers are treated as 0% monthly return to avoid survivorship-by-dropping.
    m = dict(zip(seg["ticker"].astype(str), _to_num(seg["ret_1m"]).fillna(0.0).astype(float)))
    vals = [float(m.get(tk, 0.0)) for tk in tickers]
    return float(np.mean(vals))


def _calc_mdd(nav: pd.Series) -> float:
    x = _to_num(nav).dropna()
    if len(x) == 0:
        return np.nan
    dd = x / x.cummax() - 1.0
    return float(dd.min())


def _calc_cagr(nav: pd.Series, periods_per_year: int = 12) -> float:
    x = _to_num(nav).dropna()
    if len(x) < 2:
        return np.nan
    years = len(x) / float(periods_per_year)
    if years <= 0 or x.iloc[0] <= 0:
        return np.nan
    return float((x.iloc[-1] / x.iloc[0]) ** (1.0 / years) - 1.0)


def _calc_sharpe(ret: pd.Series, periods_per_year: int = 12) -> float:
    x = _to_num(ret).dropna()
    if len(x) < 2:
        return np.nan
    sd = x.std(ddof=1)
    if sd == 0 or not np.isfinite(sd):
        return np.nan
    return float(x.mean() / sd * np.sqrt(periods_per_year))


def main() -> None:
    ap = argparse.ArgumentParser(description="Integrated Top-K portfolio backtest using OOS factor-weight predictions.")
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--feat_v", type=int, required=True)
    ap.add_argument("--ret_v", type=int, required=True)
    ap.add_argument("--ret_src", default="pykrx")
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--preds_path", required=True, help="OOS predictions from train_factor_weight_model.py")
    ap.add_argument("--model_path", required=True, help="factor_weight_model.joblib; used for base_abs_bucket_weight_map")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--max_per_group", type=int, default=0)
    ap.add_argument("--group_col", default="")
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--cap_profit_accel_delta", type=float, default=-1.0)
    ap.add_argument("--tcost_bps", type=float, default=30.0)
    ap.add_argument("--filter_fallback", choices=["full", "error"], default="full")
    ap.add_argument("--out_dir", default="")
    args = ap.parse_args()

    p_feat = _pick_features_path(args.asof, args.metric, args.feat_v)
    p_ret = _pick_returns_path(args.asof, args.metric, args.ret_src, args.ret_v)
    strat_path = Path("configs/strategies.yaml") if Path("configs/strategies.yaml").exists() else Path("strategies.yaml")
    if not strat_path.exists():
        raise FileNotFoundError("strategies.yaml not found in configs/ or project root.")

    weights, filters, desc = load_strategy(strat_path, args.strategy)
    runtime_cfg = load_strategy_runtime(strat_path, args.strategy)
    model_pack = joblib.load(args.model_path)
    preds = _load_predictions(Path(args.preds_path))

    feat = pd.read_parquet(p_feat).copy()
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
    feat = _fill_group_map(feat, args.asof)

    ret = pd.read_parquet(p_ret).copy()
    if "month_end" not in ret.columns and "month" in ret.columns:
        ret["month_end"] = pd.to_datetime(ret["month"]).dt.to_period("M").dt.to_timestamp("M")
    else:
        ret["month_end"] = pd.to_datetime(ret["month_end"], errors="coerce")
    ret["ticker"] = normalize_ticker_series(ret["ticker"])
    ret["ret_1m"] = _to_num(ret["ret_1m"]).fillna(0.0)

    rb_months_all = sorted(pd.to_datetime(feat["rebalance_month"]).dropna().unique())
    pred_months = set(pd.to_datetime(preds["rebalance_month"]).dt.normalize())
    rb_months = [pd.Timestamp(x) for x in rb_months_all if pd.Timestamp(x).normalize() in pred_months]
    rb_months = [x for x in rb_months if rb_months_all.index(np.datetime64(x)) < len(rb_months_all) - 1]
    if len(rb_months) == 0:
        raise RuntimeError("No overlapping rebalance months between features and OOS predictions.")

    group_col = args.group_col.strip() or detect_group_col(feat)
    if not group_col or group_col not in feat.columns:
        group_col = None

    active_buckets = _build_active_bucket_specs(weights, list(feat.columns), DEFAULT_BUCKET_SPECS)
    base_abs_map = model_pack.get("base_abs_bucket_weight_map") or _bucket_base_abs_weight_map(active_buckets)
    base_share = _pred_to_bucket_shares({b: np.nan for b in base_abs_map}, base_abs_map, temperature=1.0)

    pred_lookup = preds.set_index("rebalance_month")
    prev_base_w: dict[str, float] = {}
    prev_ai_w: dict[str, float] = {}
    nav_base_gross = 1.0
    nav_base_net = 1.0
    nav_ai_gross = 1.0
    nav_ai_net = 1.0
    monthly_rows: list[dict[str, Any]] = []
    rebalance_rows: list[dict[str, Any]] = []
    holdings_rows: list[dict[str, Any]] = []

    for rb in rb_months:
        rb = pd.Timestamp(rb)
        next_candidates = [pd.Timestamp(x) for x in rb_months_all if pd.Timestamp(x) > rb]
        if not next_candidates:
            continue
        next_rb = next_candidates[0]

        cohort_all = feat.loc[feat["rebalance_month"] == rb].copy()
        if len(cohort_all) == 0:
            continue
        cohort = _apply_strategy_filters(cohort_all, filters, rb, args.filter_fallback)

        pred_row = pred_lookup.loc[rb]
        if isinstance(pred_row, pd.DataFrame):
            pred_row = pred_row.iloc[0]
        pred_alpha = {b: float(pred_row.get(f"pred_alpha__{b}", np.nan)) for b in base_abs_map.keys()}
        dyn_share_uncapped = _pred_to_bucket_shares(pred_alpha, base_abs_map, temperature=float(args.temperature))
        cap_val = None if float(args.cap_profit_accel_delta) < 0 else float(args.cap_profit_accel_delta)
        dyn_share = _apply_profit_accel_cap(dyn_share_uncapped, base_share, cap_val, target_bucket="profit_accel")
        bucket_mult = {b: (float(dyn_share.get(b, 0.0)) / float(base_share.get(b, np.nan)) if float(base_share.get(b, np.nan)) > 0 else 1.0) for b in base_abs_map.keys()}

        scored = _compute_scores(cohort, weights, runtime_cfg, bucket_mult)
        base_sel = select_top_k_with_group_cap(scored.sort_values(["score_base", "ticker"], ascending=[False, True]), int(args.k), group_col, int(args.max_per_group)).copy()
        ai_sel = select_top_k_with_group_cap(scored.sort_values(["score_ai", "ticker"], ascending=[False, True]), int(args.k), group_col, int(args.max_per_group)).copy()
        base_tickers = base_sel["ticker"].astype(str).tolist()
        ai_tickers = ai_sel["ticker"].astype(str).tolist()
        base_w = _weights_for_selected(base_sel, int(args.k))
        ai_w = _weights_for_selected(ai_sel, int(args.k))
        base_turnover = _turnover(prev_base_w, base_w)
        ai_turnover = _turnover(prev_ai_w, ai_w)
        base_cost = base_turnover * float(args.tcost_bps) / 10000.0
        ai_cost = ai_turnover * float(args.tcost_bps) / 10000.0
        prev_base_w = base_w
        prev_ai_w = ai_w

        months = sorted(ret.loc[(ret["month_end"] >= rb) & (ret["month_end"] < next_rb), "month_end"].dropna().unique())
        period_base_gross = 1.0
        period_base_net = 1.0
        period_ai_gross = 1.0
        period_ai_net = 1.0
        first_month = True
        for m0 in months:
            m = pd.Timestamp(m0)
            r_base = _portfolio_month_ret(ret, base_tickers, m)
            r_ai = _portfolio_month_ret(ret, ai_tickers, m)
            r_base_net = r_base - (base_cost if first_month else 0.0)
            r_ai_net = r_ai - (ai_cost if first_month else 0.0)
            first_month = False
            nav_base_gross *= (1.0 + r_base)
            nav_ai_gross *= (1.0 + r_ai)
            nav_base_net *= (1.0 + r_base_net)
            nav_ai_net *= (1.0 + r_ai_net)
            period_base_gross *= (1.0 + r_base)
            period_ai_gross *= (1.0 + r_ai)
            period_base_net *= (1.0 + r_base_net)
            period_ai_net *= (1.0 + r_ai_net)
            monthly_rows.append({
                "rebalance_month": rb,
                "next_rebalance_month": next_rb,
                "month_end": m,
                "baseline_ret_gross": r_base,
                "ai_ret_gross": r_ai,
                "baseline_ret_net": r_base_net,
                "ai_ret_net": r_ai_net,
                "baseline_nav_gross": nav_base_gross,
                "ai_nav_gross": nav_ai_gross,
                "baseline_nav_net": nav_base_net,
                "ai_nav_net": nav_ai_net,
                "baseline_turnover_applied": base_turnover if m == months[0] else 0.0,
                "ai_turnover_applied": ai_turnover if m == months[0] else 0.0,
            })

        rebalance_rows.append({
            "rebalance_month": rb,
            "next_rebalance_month": next_rb,
            "n_filtered": int(len(cohort)),
            "baseline_topk": ",".join(base_tickers),
            "ai_topk": ",".join(ai_tickers),
            "overlap_count": int(len(set(base_tickers) & set(ai_tickers))),
            "baseline_turnover": base_turnover,
            "ai_turnover": ai_turnover,
            "baseline_period_ret_gross": period_base_gross - 1.0,
            "ai_period_ret_gross": period_ai_gross - 1.0,
            "baseline_period_ret_net": period_base_net - 1.0,
            "ai_period_ret_net": period_ai_net - 1.0,
            **{f"pred_alpha__{b}": pred_alpha.get(b, np.nan) for b in base_abs_map.keys()},
            **{f"base_share__{b}": base_share.get(b, np.nan) for b in base_abs_map.keys()},
            **{f"dyn_share__{b}": dyn_share.get(b, np.nan) for b in base_abs_map.keys()},
            **{f"bucket_mult__{b}": bucket_mult.get(b, np.nan) for b in base_abs_map.keys()},
        })
        for label, sel, score_col in [("baseline", base_sel, "score_base"), ("ai", ai_sel, "score_ai")]:
            tmp = sel.copy()
            tmp["portfolio"] = label
            tmp["rebalance_month"] = rb
            tmp["score_used"] = tmp[score_col]
            keep_cols = [c for c in ["rebalance_month", "portfolio", "ticker", "name", group_col, "score_base", "score_ai", "score_used"] if c and c in tmp.columns]
            holdings_rows.extend(tmp[keep_cols].to_dict("records"))

    monthly = pd.DataFrame(monthly_rows)
    rebalance = pd.DataFrame(rebalance_rows)
    holdings = pd.DataFrame(holdings_rows)
    if len(monthly) == 0:
        raise RuntimeError("No monthly rows produced. Check returns overlap.")

    summary = {
        "asof": args.asof,
        "metric": args.metric,
        "strategy": args.strategy,
        "feat_v": int(args.feat_v),
        "ret_v": int(args.ret_v),
        "k": int(args.k),
        "max_per_group": int(args.max_per_group),
        "tcost_bps": float(args.tcost_bps),
        "temperature": float(args.temperature),
        "cap_profit_accel_delta": None if float(args.cap_profit_accel_delta) < 0 else float(args.cap_profit_accel_delta),
        "periods": int(len(rebalance)),
        "months": int(len(monthly)),
        "first_rebalance_month": str(pd.Timestamp(rebalance["rebalance_month"].min()).date()) if len(rebalance) else None,
        "last_rebalance_month": str(pd.Timestamp(rebalance["rebalance_month"].max()).date()) if len(rebalance) else None,
        "baseline_nav_gross": float(monthly["baseline_nav_gross"].iloc[-1]),
        "ai_nav_gross": float(monthly["ai_nav_gross"].iloc[-1]),
        "baseline_nav_net": float(monthly["baseline_nav_net"].iloc[-1]),
        "ai_nav_net": float(monthly["ai_nav_net"].iloc[-1]),
        "baseline_cagr_net": _calc_cagr(monthly["baseline_nav_net"]),
        "ai_cagr_net": _calc_cagr(monthly["ai_nav_net"]),
        "baseline_sharpe_net": _calc_sharpe(monthly["baseline_ret_net"]),
        "ai_sharpe_net": _calc_sharpe(monthly["ai_ret_net"]),
        "baseline_mdd_net": _calc_mdd(monthly["baseline_nav_net"]),
        "ai_mdd_net": _calc_mdd(monthly["ai_nav_net"]),
        "avg_baseline_turnover": float(rebalance["baseline_turnover"].mean()) if len(rebalance) else np.nan,
        "avg_ai_turnover": float(rebalance["ai_turnover"].mean()) if len(rebalance) else np.nan,
        "avg_overlap_count": float(rebalance["overlap_count"].mean()) if len(rebalance) else np.nan,
        "ai_win_rate_period_net": float((rebalance["ai_period_ret_net"] > rebalance["baseline_period_ret_net"]).mean()) if len(rebalance) else np.nan,
    }

    out_dir = Path(args.out_dir) if args.out_dir else Path(
        f"data/factor_weight_ml/reports/integrated_topk__asof={args.asof}__strategy={args.strategy}__k={args.k}__temp={args.temperature:.2f}__capPA={args.cap_profit_accel_delta:.2f}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    monthly_path = out_dir / "integrated_monthly_nav.csv"
    rebalance_path = out_dir / "integrated_rebalance_detail.csv"
    holdings_path = out_dir / "integrated_holdings.csv"
    summary_path = out_dir / "summary.json"
    monthly.to_csv(monthly_path, index=False, encoding="utf-8-sig")
    rebalance.to_csv(rebalance_path, index=False, encoding="utf-8-sig")
    holdings.to_csv(holdings_path, index=False, encoding="utf-8-sig")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    print("========== INTEGRATED TOP-K AI OVERLAY BACKTEST ==========")
    print(f"periods={summary['periods']} months={summary['months']}")
    print(f"rebalance_range={summary['first_rebalance_month']} -> {summary['last_rebalance_month']}")
    print("")
    print("[NET NAV]")
    print(f"baseline_nav_net={summary['baseline_nav_net']:.6f}")
    print(f"ai_nav_net={summary['ai_nav_net']:.6f}")
    print("")
    print("[CAGR NET]")
    print(f"baseline_cagr_net={summary['baseline_cagr_net']:.6f}")
    print(f"ai_cagr_net={summary['ai_cagr_net']:.6f}")
    print("")
    print("[SHARPE NET]")
    print(f"baseline_sharpe_net={summary['baseline_sharpe_net']:.6f}")
    print(f"ai_sharpe_net={summary['ai_sharpe_net']:.6f}")
    print("")
    print("[MDD NET]")
    print(f"baseline_mdd_net={summary['baseline_mdd_net']:.6f}")
    print(f"ai_mdd_net={summary['ai_mdd_net']:.6f}")
    print("")
    print("[TURNOVER / OVERLAP]")
    print(f"avg_baseline_turnover={summary['avg_baseline_turnover']:.6f}")
    print(f"avg_ai_turnover={summary['avg_ai_turnover']:.6f}")
    print(f"avg_overlap_count={summary['avg_overlap_count']:.6f}")
    print(f"ai_win_rate_period_net={summary['ai_win_rate_period_net']:.6f}")
    print("")
    print(f"[OK] monthly  : {monthly_path}")
    print(f"[OK] rebalance: {rebalance_path}")
    print(f"[OK] holdings : {holdings_path}")
    print(f"[OK] summary  : {summary_path}")


if __name__ == "__main__":
    main()
