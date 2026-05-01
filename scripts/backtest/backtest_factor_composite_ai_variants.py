from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _THIS_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent
for _p in (_THIS_DIR, _SCRIPTS_DIR, _REPO_ROOT):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

from common.combo_strategy import calc_cagr_from_nav, calc_mdd_from_nav, calc_sharpe
from common.portfolio_modules import (
    apply_expectation_overlay,
    apply_quality_soft_penalty,
    load_strategy_modules_config,
    select_target_portfolio_with_mcap_groups,
)
from scripts.backtest.backtest_quarterly_rebalance_v2 import (
    _load_name_map,
    apply_filters,
    attach_names,
    detect_group_col,
    ensure_dir,
    load_group_map,
    load_price_history,
    load_strategy,
    load_strategy_runtime,
    lookup_prices_on_or_before,
    normalize_ticker_series,
    safe_fill_for_z,
    select_target_portfolio,
    standardize_factor,
)


def _to_num(s: Any) -> pd.Series:
    if isinstance(s, pd.Series):
        return pd.to_numeric(s, errors="coerce")
    return pd.to_numeric(pd.Series([s]), errors="coerce")


def _calc_cagr_from_months(nav: pd.Series) -> float:
    x = pd.to_numeric(nav, errors="coerce").dropna()
    if len(x) == 0:
        return np.nan
    nav_end = float(x.iloc[-1])
    months = int(len(x))
    if months <= 0:
        return np.nan
    years = months / 12.0
    if years <= 0 or nav_end <= 0:
        return np.nan
    return float(nav_end ** (1.0 / years) - 1.0)


def _strict_filter_op_qoq(df: pd.DataFrame, rebalance_month: pd.Timestamp) -> pd.DataFrame:
    if "op_qoq" not in df.columns:
        return df
    op_qoq_num = _to_num(df["op_qoq"])
    strict_qoq = df.loc[op_qoq_num.notna() & (op_qoq_num > 0)].copy()
    if len(strict_qoq) == 0:
        print(f"[WARN] skip strict filter op_qoq > 0 for rebalance_month={rebalance_month.date()}: would empty the rebalance universe")
        return df
    return strict_qoq


def _mask_mult_for(df: pd.DataFrame, col: str) -> pd.Series:
    if ("CFO" in col) or ("Quality_CFO" in col) or ("CFO_to_Assets" in col):
        isnull = _to_num(df.get("CFO_isnull", 0.0)).fillna(0.0)
        warn = _to_num(df.get("CFO_warn", 0.0)).fillna(0.0)
        return (1.0 - 0.7 * isnull - 0.4 * warn).clip(0.0, 1.0).astype("float64")
    return pd.Series(1.0, index=df.index, dtype="float64")


def _load_ai_scored(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".parquet":
        df = pd.read_parquet(path).copy()
    else:
        df = pd.read_csv(path).copy()
    df["ticker"] = normalize_ticker_series(df["ticker"])
    df["rebalance_month"] = pd.to_datetime(df["rebalance_month"], errors="coerce")
    df["score_ai"] = _to_num(df.get("score_ai", np.nan)).astype("float64")
    if "score_ai_rank_pct" not in df.columns:
        df["score_ai_rank_pct"] = (
            df.groupby("rebalance_month")["score_ai"].rank(ascending=True, pct=True, method="average").astype("float64")
        )
    else:
        df["score_ai_rank_pct"] = _to_num(df["score_ai_rank_pct"]).astype("float64")
    return df


def _load_factor_baseline_picks(asof: str, metric: str, k: int, strategy: str, out_v: int) -> pd.DataFrame:
    p = Path(
        rf"data/processed/picks__asof={asof}__metric={metric}__k={k}__strat={strategy}__v={out_v}.parquet"
    )
    if not p.exists():
        raise FileNotFoundError(p)
    df = pd.read_parquet(p).copy()
    df["ticker"] = normalize_ticker_series(df["ticker"])
    df["rebalance_month"] = pd.to_datetime(df["rebalance_month"], errors="coerce")
    return df


def _group_ticker_sets(df: pd.DataFrame, ticker_col: str = "ticker") -> dict[pd.Timestamp, list[str]]:
    if len(df) == 0:
        return {}
    out: dict[pd.Timestamp, list[str]] = {}
    for rm, grp in df.groupby("rebalance_month"):
        tickers = normalize_ticker_series(grp[ticker_col]).tolist()
        tickers = [tk for tk in tickers if tk]
        out[pd.Timestamp(rm)] = list(dict.fromkeys(tickers))
    return out


def _apply_hysteresis(scored: pd.DataFrame, prev_hold: set[str] | None, hold_bonus: float) -> pd.DataFrame:
    out = scored.copy()
    out["score_adj"] = out["score"]
    out["hold_bonus_applied"] = 0.0
    if prev_hold and float(hold_bonus) != 0.0:
        mask = out["ticker"].isin(prev_hold)
        out.loc[mask, "score_adj"] += float(hold_bonus)
        out.loc[mask, "hold_bonus_applied"] = float(hold_bonus)
    return out


def _select_base_portfolio(
    scored: pd.DataFrame,
    portfolio_size: int,
    keep_current_top_n: int,
    max_per_group: int,
    current_tickers: set[str],
    mcap_grouping_cfg: dict[str, Any],
) -> pd.DataFrame:
    effective_group_col = detect_group_col(scored)
    picked = select_target_portfolio_with_mcap_groups(
        scored,
        portfolio_size=portfolio_size,
        keep_current_top_n=keep_current_top_n,
        effective_group_col=effective_group_col,
        max_per_group=max_per_group,
        current_tickers=current_tickers,
        base_selector=select_target_portfolio,
        grouping_cfg=mcap_grouping_cfg,
    ).copy()
    return picked


def _build_base_scores(
    cohort: pd.DataFrame,
    rm: pd.Timestamp,
    *,
    weights: dict[str, float],
    filters: dict[str, Any],
    resolved_clip_z: float | None,
    resolved_use_robust_z: bool,
    resolved_clip_tiers: list[dict],
    resolved_raw_factors: list[str],
    expectation_cfg: dict[str, Any],
    quality_penalty_cfg: dict[str, Any],
    price_history: pd.DataFrame,
) -> pd.DataFrame:
    gg = cohort.copy()
    gg_filtered = apply_filters(gg, filters, verbose=True)
    gg_filtered = _strict_filter_op_qoq(gg_filtered, rm)
    gg = gg_filtered.copy() if len(gg_filtered) else cohort.copy()

    raw_factor_set = set(resolved_raw_factors or [])
    gg["score_total_base"] = 0.0

    for c, ww in weights.items():
        ww = float(ww)
        if ww == 0.0 or c not in gg.columns:
            continue
        raw = pd.to_numeric(gg[c], errors="coerce")
        if c in raw_factor_set:
            z = raw.fillna(0.0).astype("float64")
            mult = pd.Series(1.0, index=gg.index, dtype="float64")
            contrib_base = ww * z
        else:
            z = standardize_factor(
                safe_fill_for_z(raw),
                use_robust_z=resolved_use_robust_z,
                clip_z=None if resolved_clip_z is None or resolved_clip_z < 0 else resolved_clip_z,
                clip_tiers=resolved_clip_tiers,
            ).astype("float64")
            mult = _mask_mult_for(gg, c)
            contrib_base = ww * z * mult

        gg[f"{c}__raw"] = raw
        gg[f"{c}__signal"] = z
        gg[f"{c}__mult"] = mult
        gg[f"{c}__contrib_base"] = contrib_base
        gg["score_total_base"] += contrib_base

    gg["score_total"] = gg["score_total_base"]
    gg = apply_expectation_overlay(gg, expectation_cfg, asof_date=rm, price_history=price_history)
    gg = apply_quality_soft_penalty(gg, quality_penalty_cfg)
    gg["score"] = gg["score_total"]
    gg["rebalance_month"] = rm
    return gg


def _rank_pct_desc(series: pd.Series) -> pd.Series:
    return series.rank(ascending=True, pct=True, method="average").astype("float64")


def _prepare_variant_frame(
    base_scored: pd.DataFrame,
    ai_period: pd.DataFrame,
    variant: str,
    *,
    prev_hold: set[str] | None,
    hold_bonus: float,
    ai_threshold: float,
    ai_overlay_alpha: float,
    ai_overlay_cap: float,
) -> pd.DataFrame:
    scored = base_scored.copy()
    scored = scored.merge(
        ai_period[["ticker", "score_ai", "score_ai_rank_pct", "model_status"]],
        on="ticker",
        how="left",
    )
    scored["score_ai"] = _to_num(scored.get("score_ai", np.nan)).fillna(scored["score_total_base"]).astype("float64")
    scored["score_ai_rank_pct"] = _to_num(scored.get("score_ai_rank_pct", np.nan)).fillna(0.5).astype("float64")
    scored["model_status"] = scored.get("model_status", "fallback").fillna("fallback")
    scored = _apply_hysteresis(scored, prev_hold, hold_bonus=hold_bonus)
    scored["score_factor"] = scored["score_adj"].astype("float64")
    scored["ai_filter_pass"] = (scored["score_ai_rank_pct"] >= float(ai_threshold)).astype(int)

    if variant == "factor_composite_ai_overlay":
        ai_z = standardize_factor(safe_fill_for_z(scored["score_ai"]))
        ai_adj = (float(ai_overlay_alpha) * ai_z).clip(-float(ai_overlay_cap), float(ai_overlay_cap)).astype("float64")
        scored["clipped_ai_adjustment"] = ai_adj
        scored["score_final"] = scored["score_factor"] + scored["clipped_ai_adjustment"]
    else:
        scored["clipped_ai_adjustment"] = 0.0
        scored["score_final"] = scored["score_factor"]

    return scored.sort_values(["score_factor", "ticker"], ascending=[False, True]).reset_index(drop=True)


def _fill_to_k(
    selected: pd.DataFrame,
    fallback_pool: pd.DataFrame,
    *,
    portfolio_size: int,
    keep_current_top_n: int,
    max_per_group: int,
    current_tickers: set[str],
    mcap_grouping_cfg: dict[str, Any],
) -> pd.DataFrame:
    if len(selected) >= portfolio_size:
        return selected.copy()
    remain = fallback_pool.loc[~fallback_pool["ticker"].isin(selected["ticker"])].copy()
    combo = pd.concat([selected, remain], ignore_index=True)
    combo = combo.sort_values(["score_final", "score_factor", "ticker"], ascending=[False, False, True]).reset_index(drop=True)
    return _select_base_portfolio(
        combo,
        portfolio_size=portfolio_size,
        keep_current_top_n=keep_current_top_n,
        max_per_group=max_per_group,
        current_tickers=current_tickers,
        mcap_grouping_cfg=mcap_grouping_cfg,
    )


def _select_filter_variant(
    scored: pd.DataFrame,
    *,
    portfolio_size: int,
    keep_current_top_n: int,
    max_per_group: int,
    current_tickers: set[str],
    mcap_grouping_cfg: dict[str, Any],
    preselect_mult: float,
) -> pd.DataFrame:
    preselect_n = max(portfolio_size, int(math.ceil(float(preselect_mult) * portfolio_size)))
    preselect = scored.sort_values(["score_factor", "ticker"], ascending=[False, True]).head(preselect_n).copy()
    passed = preselect.loc[preselect["ai_filter_pass"] == 1].copy()
    passed["score_final"] = passed["score_factor"]
    picked = _select_base_portfolio(
        passed.sort_values(["score_final", "score_factor", "ticker"], ascending=[False, False, True]),
        portfolio_size=portfolio_size,
        keep_current_top_n=keep_current_top_n,
        max_per_group=max_per_group,
        current_tickers=current_tickers,
        mcap_grouping_cfg=mcap_grouping_cfg,
    )
    return _fill_to_k(
        picked,
        preselect,
        portfolio_size=portfolio_size,
        keep_current_top_n=keep_current_top_n,
        max_per_group=max_per_group,
        current_tickers=current_tickers,
        mcap_grouping_cfg=mcap_grouping_cfg,
    )


def _select_overlay_variant(
    scored: pd.DataFrame,
    *,
    portfolio_size: int,
    keep_current_top_n: int,
    max_per_group: int,
    current_tickers: set[str],
    mcap_grouping_cfg: dict[str, Any],
) -> pd.DataFrame:
    ranked = scored.sort_values(["score_final", "score_factor", "ticker"], ascending=[False, False, True]).copy()
    return _select_base_portfolio(
        ranked,
        portfolio_size=portfolio_size,
        keep_current_top_n=keep_current_top_n,
        max_per_group=max_per_group,
        current_tickers=current_tickers,
        mcap_grouping_cfg=mcap_grouping_cfg,
    )


def _select_limited_replace_variant(
    scored: pd.DataFrame,
    *,
    portfolio_size: int,
    keep_current_top_n: int,
    max_per_group: int,
    current_tickers: set[str],
    mcap_grouping_cfg: dict[str, Any],
    max_replace: int,
) -> tuple[pd.DataFrame, int]:
    base_pick = _select_base_portfolio(
        scored.sort_values(["score_factor", "ticker"], ascending=[False, True]).copy(),
        portfolio_size=portfolio_size,
        keep_current_top_n=keep_current_top_n,
        max_per_group=max_per_group,
        current_tickers=current_tickers,
        mcap_grouping_cfg=mcap_grouping_cfg,
    ).copy()
    base_pick["score_final"] = base_pick["score_factor"]

    current = base_pick.copy()
    changed = 0
    candidates = scored.sort_values(["score_ai_rank_pct", "score_factor", "ticker"], ascending=[False, False, True]).copy()
    removable = current.sort_values(["score_ai_rank_pct", "score_factor", "ticker"], ascending=[True, True, True]).copy()

    for _, cand in candidates.iterrows():
        if changed >= int(max_replace):
            break
        cand_tk = str(cand["ticker"])
        if cand_tk in set(current["ticker"].astype(str)):
            continue
        if float(cand.get("score_ai_rank_pct", 0.0)) < 0.55:
            continue
        updated = False
        for _, rem in removable.iterrows():
            rem_tk = str(rem["ticker"])
            if rem_tk not in set(current["ticker"].astype(str)):
                continue
            if float(cand.get("score_ai_rank_pct", 0.0)) <= float(rem.get("score_ai_rank_pct", 0.0)):
                continue
            trial = current.loc[current["ticker"].astype(str) != rem_tk].copy()
            trial = pd.concat([trial, pd.DataFrame([cand])], ignore_index=True)
            checked = _select_base_portfolio(
                trial.sort_values(["score_factor", "ticker"], ascending=[False, True]).copy(),
                portfolio_size=portfolio_size,
                keep_current_top_n=0,
                max_per_group=max_per_group,
                current_tickers=set(trial["ticker"].astype(str).tolist()),
                mcap_grouping_cfg=mcap_grouping_cfg,
            )
            checked_set = set(checked["ticker"].astype(str).tolist())
            if cand_tk in checked_set and len(checked_set) == portfolio_size:
                current = checked.copy()
                current["score_final"] = current["score_factor"]
                changed += 1
                removable = current.sort_values(["score_ai_rank_pct", "score_factor", "ticker"], ascending=[True, True, True]).copy()
                updated = True
                break
        if updated:
            continue

    return current.copy(), changed


def _build_period_return_map(monthly_df: pd.DataFrame, ret_col: str) -> dict[pd.Timestamp, float]:
    if len(monthly_df) == 0:
        return {}
    out: dict[pd.Timestamp, float] = {}
    for rm, grp in monthly_df.groupby("rebalance_month"):
        vals = (1.0 + _to_num(grp[ret_col]).fillna(0.0)).prod() - 1.0
        out[pd.Timestamp(rm)] = float(vals)
    return out


def _backtest_variant(
    picks_df: pd.DataFrame,
    ret: pd.DataFrame,
    *,
    variant: str,
    tcost_bps: float,
) -> pd.DataFrame:
    if len(picks_df) == 0:
        return pd.DataFrame(columns=["month_end", "rebalance_month", "variant", "month_ret_gross", "month_ret_net", "nav_net"])
    ret2 = ret[["ticker", "month_end", "ret_1m"]].copy()
    ret2["ret_1m"] = _to_num(ret2["ret_1m"]).fillna(0.0)
    months = [pd.Timestamp(m) for m in sorted(ret2["month_end"].dropna().unique())]
    rb_series = pd.Series(sorted(pd.to_datetime(picks_df["rebalance_month"]).dropna().unique())).sort_values()
    turnover_map = {
        pd.Timestamp(rm): float(turn)
        for rm, turn in picks_df[["rebalance_month", "turnover"]].drop_duplicates().itertuples(index=False, name=None)
    }

    def last_rb(m: pd.Timestamp) -> pd.Timestamp:
        idx = rb_series.searchsorted(m, side="right") - 1
        return rb_series.iloc[0] if idx < 0 else rb_series.iloc[int(idx)]

    nav_net = 1.0
    rows: list[dict[str, Any]] = []
    for m in months:
        rb = last_rb(m)
        w = picks_df.loc[picks_df["rebalance_month"] == rb, ["ticker", "w"]].copy()
        seg = ret2.loc[ret2["month_end"] == m].merge(w, on="ticker", how="inner")
        port_ret = 0.0 if len(seg) == 0 else float((seg["w"] * seg["ret_1m"]).sum())
        turnover = turnover_map.get(pd.Timestamp(rb), 0.0) if m == rb else 0.0
        tcost = turnover * float(tcost_bps) / 10000.0 if m == rb else 0.0
        net_ret = port_ret - tcost
        nav_net *= (1.0 + net_ret)
        rows.append(
            {
                "month_end": m,
                "rebalance_month": rb,
                "variant": variant,
                "month_ret_gross": port_ret,
                "month_ret_net": net_ret,
                "nav_net": nav_net,
            }
        )
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest controlled AI auxiliary variants on top of factor_composite baseline mechanics.")
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--strategy", default="D_quality_filter_debt_profitaccel_liq")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--feat_v", type=int, default=3291)
    ap.add_argument("--ret_v", type=int, default=1)
    ap.add_argument("--tcost_bps", type=float, default=30.0)
    ap.add_argument("--max_per_group", type=int, default=0)
    ap.add_argument("--factor_baseline_v", type=int, default=102)
    ap.add_argument("--ai_scored_path", required=True)
    ap.add_argument("--out_dir", required=True)
    ap.add_argument("--ai_threshold", type=float, default=0.55)
    ap.add_argument("--preselect_mult", type=float, default=2.0)
    ap.add_argument("--ai_overlay_alpha", type=float, default=0.15)
    ap.add_argument("--ai_overlay_cap", type=float, default=0.20)
    ap.add_argument("--max_replace", type=int, default=2)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    ensure_dir(out_dir / "dummy.txt")

    feat_candidates = [
        Path(rf"data/features/features_live/features_live__asof={args.asof}__metric={args.metric}__v={args.feat_v}.parquet"),
        Path(rf"data/processed/features_live__asof={args.asof}__metric={args.metric}__v={args.feat_v}.parquet"),
    ]
    p_feat = next((p for p in feat_candidates if p.exists()), None)
    if p_feat is None:
        raise FileNotFoundError("features_live not found")
    p_ret = Path(rf"data/processed/returns_monthly__src=pykrx__asof={args.asof}__metric={args.metric}__v={args.ret_v}.parquet")
    if not p_ret.exists():
        raise FileNotFoundError(p_ret)

    feat = pd.read_parquet(p_feat).copy()
    ret = pd.read_parquet(p_ret).copy()
    feat["ticker"] = normalize_ticker_series(feat["ticker"])
    ret["ticker"] = normalize_ticker_series(ret["ticker"])
    if "month_end" not in ret.columns and "month" in ret.columns:
        ret["month_end"] = pd.to_datetime(ret["month"]).dt.to_period("M").dt.to_timestamp("M")
    else:
        ret["month_end"] = pd.to_datetime(ret["month_end"], errors="coerce")
    ret["ret_1m"] = _to_num(ret["ret_1m"]).fillna(0.0)

    name_map = _load_name_map(args.asof, feat=feat)
    feat = attach_names(feat, name_map)
    group_map, group_src, external_group_col = load_group_map(args.asof)
    existing_group_col = detect_group_col(feat)
    if len(group_map) > 0 and external_group_col:
        if existing_group_col and existing_group_col in feat.columns:
            feat = feat.merge(group_map.rename(columns={external_group_col: f"{external_group_col}__ext"}), on="ticker", how="left")
            ext_col = f"{external_group_col}__ext"
            if ext_col in feat.columns:
                feat[existing_group_col] = feat[existing_group_col].where(feat[existing_group_col].notna(), feat[ext_col])
                feat = feat.drop(columns=[ext_col])
        else:
            feat = feat.merge(group_map, on="ticker", how="left")
        print(f"[OK] group map source selected: {group_src}")

    weights, filters, _desc = load_strategy(Path("configs/strategies.yaml"), args.strategy)
    runtime_cfg = load_strategy_runtime(Path("configs/strategies.yaml"), args.strategy)
    modules_cfg = load_strategy_modules_config(Path("configs/strategies.yaml"), args.strategy)
    expectation_cfg = modules_cfg.get("expectation_overlay", {})
    quality_penalty_cfg = modules_cfg.get("quality_soft_penalty", {})
    mcap_grouping_cfg = modules_cfg.get("mcap_grouping", {})

    resolved_hold_bonus = float(runtime_cfg.get("hold_bonus", 0.0))
    resolved_clip_z = runtime_cfg.get("clip_z", 5.0)
    resolved_use_robust_z = bool(runtime_cfg.get("use_robust_z", False))
    resolved_clip_tiers = list(runtime_cfg.get("clip_tiers", [])) if isinstance(runtime_cfg.get("clip_tiers", []), list) else []
    resolved_raw_factors = list(runtime_cfg.get("raw_factors", [])) if isinstance(runtime_cfg.get("raw_factors", []), list) else []
    if not resolved_raw_factors:
        resolved_raw_factors = [c for c in weights.keys() if c in {"op_growth_streak2", "rev_growth_streak2"}]
    resolved_portfolio_size = int(runtime_cfg.get("portfolio_size", args.k))
    resolved_keep_current_top_n = int(runtime_cfg.get("keep_current_top_n", 0))

    price_hist = load_price_history(args.asof, args.metric)
    ai_scored = _load_ai_scored(Path(args.ai_scored_path))
    factor_baseline_picks = _load_factor_baseline_picks(args.asof, args.metric, args.k, args.strategy, args.factor_baseline_v)
    factor_sets = _group_ticker_sets(factor_baseline_picks)

    rb_months = [
        pd.Timestamp(m)
        for m in sorted(pd.to_datetime(feat["rebalance_month"]).dropna().unique())
        if m >= ret["month_end"].min() and m <= ret["month_end"].max()
    ]

    variant_prev_hold: dict[str, set[str]] = {
        "factor_composite_ai_filter": set(),
        "factor_composite_ai_overlay": set(),
        "factor_composite_ai_limited_replace": set(),
    }
    variant_prev_weights: dict[str, pd.DataFrame | None] = {k: None for k in variant_prev_hold}
    variant_pick_rows: dict[str, list[pd.DataFrame]] = {k: [] for k in variant_prev_hold}
    variant_reb_rows: dict[str, list[dict[str, Any]]] = {k: [] for k in variant_prev_hold}

    for rm in rb_months:
        cohort = feat.loc[pd.to_datetime(feat["rebalance_month"]) == rm].copy()
        if len(cohort) == 0:
            continue
        base_scored = _build_base_scores(
            cohort,
            rm,
            weights=weights,
            filters=filters,
            resolved_clip_z=resolved_clip_z,
            resolved_use_robust_z=resolved_use_robust_z,
            resolved_clip_tiers=resolved_clip_tiers,
            resolved_raw_factors=resolved_raw_factors,
            expectation_cfg=expectation_cfg,
            quality_penalty_cfg=quality_penalty_cfg,
            price_history=price_hist,
        )
        ai_period = ai_scored.loc[ai_scored["rebalance_month"] == rm].copy()
        if len(ai_period) == 0:
            ai_period = pd.DataFrame({"ticker": base_scored["ticker"], "score_ai": base_scored["score_total_base"], "score_ai_rank_pct": 0.5, "model_status": "fallback"})

        model_status = str(ai_period["model_status"].dropna().iloc[0]) if "model_status" in ai_period.columns and len(ai_period["model_status"].dropna()) else "fallback"
        factor_set = set(factor_sets.get(rm, []))

        for variant in variant_prev_hold.keys():
            scored_variant = _prepare_variant_frame(
                base_scored,
                ai_period,
                variant,
                prev_hold=variant_prev_hold[variant],
                hold_bonus=resolved_hold_bonus,
                ai_threshold=float(args.ai_threshold),
                ai_overlay_alpha=float(args.ai_overlay_alpha),
                ai_overlay_cap=float(args.ai_overlay_cap),
            )

            if variant == "factor_composite_ai_filter":
                picked = _select_filter_variant(
                    scored_variant,
                    portfolio_size=resolved_portfolio_size,
                    keep_current_top_n=resolved_keep_current_top_n,
                    max_per_group=int(args.max_per_group),
                    current_tickers=variant_prev_hold[variant],
                    mcap_grouping_cfg=mcap_grouping_cfg,
                    preselect_mult=float(args.preselect_mult),
                )
                ai_changed_count = int(resolved_portfolio_size - len(set(picked["ticker"].astype(str).tolist()) & factor_set))
            elif variant == "factor_composite_ai_overlay":
                picked = _select_overlay_variant(
                    scored_variant,
                    portfolio_size=resolved_portfolio_size,
                    keep_current_top_n=resolved_keep_current_top_n,
                    max_per_group=int(args.max_per_group),
                    current_tickers=variant_prev_hold[variant],
                    mcap_grouping_cfg=mcap_grouping_cfg,
                )
                ai_changed_count = int(resolved_portfolio_size - len(set(picked["ticker"].astype(str).tolist()) & factor_set))
            else:
                picked, ai_changed_count = _select_limited_replace_variant(
                    scored_variant,
                    portfolio_size=resolved_portfolio_size,
                    keep_current_top_n=resolved_keep_current_top_n,
                    max_per_group=int(args.max_per_group),
                    current_tickers=variant_prev_hold[variant],
                    mcap_grouping_cfg=mcap_grouping_cfg,
                    max_replace=int(args.max_replace),
                )

            picked = picked.copy()
            picked["rebalance_month"] = rm
            picked["rebalance_month_end"] = rm
            picked["w"] = 1.0 / max(len(picked), 1)
            px_now = lookup_prices_on_or_before(price_hist, picked["ticker"], rm)
            picked = picked.merge(px_now, on="ticker", how="left")

            cur_weights = picked[["ticker", "w"]].copy()
            prev_weights = variant_prev_weights[variant]
            if prev_weights is None:
                turnover = float(cur_weights["w"].abs().sum())
            else:
                tw = prev_weights.merge(cur_weights, on="ticker", how="outer", suffixes=("_prev", "_cur")).fillna(0.0)
                turnover = float((tw["w_cur"] - tw["w_prev"]).abs().sum() / 2.0)
            variant_prev_weights[variant] = cur_weights.copy()
            variant_prev_hold[variant] = set(picked["ticker"].astype(str).tolist())

            picked["variant"] = variant
            picked["turnover"] = turnover
            picked["model_status"] = model_status
            picked["score_rank"] = picked["score_factor"].rank(ascending=False, method="min")
            picked["selected_from_ai_filter"] = picked.get("ai_filter_pass", 0)
            variant_pick_rows[variant].append(picked.copy())

            picked_set = set(picked["ticker"].astype(str).tolist())
            overlap_count = len(picked_set & factor_set)
            overlap_ratio = float(overlap_count / max(len(picked_set), 1))
            variant_reb_rows[variant].append(
                {
                    "rebalance_month": rm,
                    "variant": variant,
                    "model_status": model_status,
                    "turnover": turnover,
                    "overlap_with_factor_count": overlap_count,
                    "overlap_with_factor_ratio": overlap_ratio,
                    "ai_changed_count": ai_changed_count,
                    "tickers": ",".join(picked["ticker"].astype(str).tolist()),
                }
            )

    factor_bt = pd.read_csv(
        rf"data/processed/bt__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={args.factor_baseline_v}.csv"
    )
    factor_bt["rebalance_month"] = pd.to_datetime(factor_bt["rebalance_month"], errors="coerce")
    factor_bt["month_end"] = pd.to_datetime(factor_bt["month_end"], errors="coerce")
    factor_period_map = _build_period_return_map(factor_bt, "ret")
    trained_rms = set(ai_scored.loc[ai_scored["model_status"] == "trained", "rebalance_month"].dropna().unique().tolist())

    summary_rows: list[dict[str, Any]] = []
    for variant, picks_list in variant_pick_rows.items():
        picks_df = pd.concat(picks_list, ignore_index=True) if picks_list else pd.DataFrame()
        reb_df = pd.DataFrame(variant_reb_rows[variant])
        monthly_df = _backtest_variant(picks_df, ret, variant=variant, tcost_bps=float(args.tcost_bps))
        out_variant_dir = out_dir / variant
        ensure_dir(out_variant_dir / "dummy.txt")
        picks_path = out_variant_dir / "variant_picks.parquet"
        reb_path = out_variant_dir / "variant_rebalance_detail.csv"
        monthly_path = out_variant_dir / "variant_monthly_nav.csv"
        picks_df.to_parquet(picks_path, index=False)
        reb_df.to_csv(reb_path, index=False, encoding="utf-8-sig")
        monthly_df.to_csv(monthly_path, index=False, encoding="utf-8-sig")

        period_map = _build_period_return_map(monthly_df, "month_ret_net")
        comp_keys = sorted(set(period_map.keys()) & set(factor_period_map.keys()))
        period_win_rate = float(np.mean([period_map[k] > factor_period_map[k] for k in comp_keys])) if comp_keys else np.nan

        trained_monthly = monthly_df.loc[monthly_df["rebalance_month"].isin(trained_rms)].copy()
        trained_nav_series = (1.0 + _to_num(trained_monthly["month_ret_net"]).fillna(0.0)).cumprod() if len(trained_monthly) else pd.Series(dtype="float64")
        trained_nav = float(trained_nav_series.iloc[-1]) if len(trained_nav_series) else np.nan
        trained_sharpe = calc_sharpe(trained_monthly["month_ret_net"]) if len(trained_monthly) else np.nan
        trained_cagr = _calc_cagr_from_months(trained_nav_series) if len(trained_nav_series) else np.nan

        summary = {
            "strategy_variant": variant,
            "net_nav": float(monthly_df["nav_net"].iloc[-1]) if len(monthly_df) else np.nan,
            "cagr_net": _calc_cagr_from_months(monthly_df["nav_net"]),
            "sharpe_net": calc_sharpe(monthly_df["month_ret_net"]),
            "maxdd_net": calc_mdd_from_nav(monthly_df["nav_net"]),
            "avg_turnover": float(reb_df["turnover"].mean()) if len(reb_df) else np.nan,
            "avg_overlap_ratio": float(reb_df["overlap_with_factor_ratio"].mean()) if len(reb_df) else np.nan,
            "avg_ai_changed_count": float(reb_df["ai_changed_count"].mean()) if len(reb_df) else np.nan,
            "period_win_rate_vs_factor_composite": period_win_rate,
            "trained_only_nav_net": trained_nav,
            "trained_only_cagr_net": trained_cagr,
            "trained_only_sharpe_net": trained_sharpe,
            "months": int(len(monthly_df)),
            "periods": int(len(reb_df)),
            "trained_periods": int(reb_df.loc[reb_df["model_status"] == "trained"].shape[0]) if len(reb_df) else 0,
        }
        (out_variant_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        summary_rows.append(summary)

    summary_df = pd.DataFrame(summary_rows)
    summary_df.to_csv(out_dir / "variant_summary.csv", index=False, encoding="utf-8-sig")
    (out_dir / "summary.json").write_text(
        json.dumps({"variants": summary_rows, "asof": args.asof, "metric": args.metric}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[OK] variant summary: {out_dir / 'variant_summary.csv'}")


if __name__ == "__main__":
    main()
