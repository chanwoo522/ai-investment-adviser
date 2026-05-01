from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _THIS_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent
for _p in (_THIS_DIR, _SCRIPTS_DIR, _REPO_ROOT):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

from common.combo_strategy import calc_mdd_from_nav, calc_sharpe


def _python_exe() -> str:
    for cand in [Path(".venv/Scripts/python.exe"), Path(".venv/bin/python")]:
        if cand.exists():
            return str(cand)
    return sys.executable


def _run(label: str, script: str, args: list[str]) -> None:
    cmd = [_python_exe(), script] + args
    print(f"[RUN][{label}] {' '.join(cmd)}")
    subprocess.run(cmd, check=True)


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _load_single_row_csv(path: Path) -> pd.Series:
    df = pd.read_csv(path)
    if len(df) == 0:
        raise RuntimeError(f"empty summary csv: {path}")
    return df.iloc[0]


def _to_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _normalize_ticker_value(value: object) -> str:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits.zfill(6) if digits else ""


def _group_sets(df: pd.DataFrame, ticker_col: str = "ticker") -> dict[pd.Timestamp, set[str]]:
    if len(df) == 0 or "rebalance_month" not in df.columns or ticker_col not in df.columns:
        return {}
    out: dict[pd.Timestamp, set[str]] = {}
    for rm, grp in df.groupby("rebalance_month"):
        vals = [_normalize_ticker_value(v) for v in grp[ticker_col].tolist()]
        out[pd.Timestamp(rm)] = {v for v in vals if v}
    return out


def _build_period_return_map(monthly_df: pd.DataFrame) -> dict[pd.Timestamp, float]:
    if len(monthly_df) == 0:
        return {}
    out: dict[pd.Timestamp, float] = {}
    for rm, grp in monthly_df.groupby("rebalance_month"):
        ret = float((1.0 + _to_num(grp["ret_net"]).fillna(0.0)).prod() - 1.0)
        out[pd.Timestamp(rm)] = ret
    return out


def _calc_cagr_from_nav(nav: pd.Series) -> float:
    x = _to_num(nav).dropna()
    if len(x) == 0:
        return np.nan
    months = int(len(x))
    years = months / 12.0
    if years <= 0:
        return np.nan
    end_nav = float(x.iloc[-1])
    if end_nav <= 0:
        return np.nan
    return float(end_nav ** (1.0 / years) - 1.0)


def _load_monthly_frame(strategy_variant: str, *, asof: str, metric: str, k: int, strategy: str, quant_out_v: int, factor_out_v: int, wf_dir: Path, combo_dir: Path, variants_dir: Path) -> pd.DataFrame:
    if strategy_variant == "quant_only":
        p = Path(f"data/processed/bt__asof={asof}__metric={metric}__k={k}__strat={strategy}__v={quant_out_v}.csv")
        df = pd.read_csv(p).copy()
        return df.rename(columns={"ret": "ret_net", "nav_net": "nav_net"})[["month_end", "rebalance_month", "ret_net", "nav_net"]]
    if strategy_variant == "factor_composite":
        p = Path(f"data/processed/bt__asof={asof}__metric={metric}__k={k}__strat={strategy}__v={factor_out_v}.csv")
        df = pd.read_csv(p).copy()
        return df.rename(columns={"ret": "ret_net", "nav_net": "nav_net"})[["month_end", "rebalance_month", "ret_net", "nav_net"]]
    if strategy_variant == "ai_wf_topk":
        df = pd.read_csv(wf_dir / "walkforward_integrated_monthly_nav.csv").copy()
        return df.rename(columns={"ai_month_ret_net": "ret_net", "ai_nav_net": "nav_net"})[["month_end", "rebalance_month", "ret_net", "nav_net"]]
    if strategy_variant == "combo_overlay_replace":
        df = pd.read_csv(combo_dir / "combo_monthly_nav.csv").copy()
        return df.rename(columns={"combo_month_ret_net": "ret_net", "combo_nav_net": "nav_net"})[["month_end", "rebalance_month", "ret_net", "nav_net"]]
    df = pd.read_csv(variants_dir / strategy_variant / "variant_monthly_nav.csv").copy()
    return df[["month_end", "rebalance_month", "month_ret_net", "nav_net"]].rename(columns={"month_ret_net": "ret_net"})


def _load_eval_meta(strategy_variant: str, *, asof: str, metric: str, k: int, strategy: str, quant_out_v: int, factor_out_v: int, wf_dir: Path, combo_dir: Path, variants_dir: Path) -> dict[str, object]:
    monthly_df = _load_monthly_frame(
        strategy_variant,
        asof=asof,
        metric=metric,
        k=k,
        strategy=strategy,
        quant_out_v=quant_out_v,
        factor_out_v=factor_out_v,
        wf_dir=wf_dir,
        combo_dir=combo_dir,
        variants_dir=variants_dir,
    ).copy()
    monthly_df["month_end"] = pd.to_datetime(monthly_df["month_end"], errors="coerce")
    monthly_df["rebalance_month"] = pd.to_datetime(monthly_df["rebalance_month"], errors="coerce")

    if strategy_variant == "ai_wf_topk":
        summary = json.loads((wf_dir / "summary.json").read_text(encoding="utf-8"))
        return {
            "eval_months": int(summary.get("months", len(monthly_df))),
            "eval_periods": int(summary.get("periods", monthly_df["rebalance_month"].nunique())),
            "trained_only_eval": False,
            "trained_months": int(summary.get("trained_months", 0)),
            "trained_periods": int(summary.get("trained_periods", 0)),
            "fallback_baseline_included": bool(int(summary.get("trained_periods", 0)) < int(summary.get("periods", 0))),
            "eval_start_month": str(pd.Timestamp(monthly_df["month_end"].min()).date()) if len(monthly_df) else "",
            "eval_end_month": str(pd.Timestamp(monthly_df["month_end"].max()).date()) if len(monthly_df) else "",
        }

    if strategy_variant == "combo_overlay_replace":
        summary = json.loads((combo_dir / "summary.json").read_text(encoding="utf-8"))
        return {
            "eval_months": int(len(monthly_df)),
            "eval_periods": int(summary.get("periods", monthly_df["rebalance_month"].nunique())),
            "trained_only_eval": False,
            "trained_months": 0,
            "trained_periods": 0,
            "fallback_baseline_included": False,
            "eval_start_month": str(pd.Timestamp(monthly_df["month_end"].min()).date()) if len(monthly_df) else "",
            "eval_end_month": str(pd.Timestamp(monthly_df["month_end"].max()).date()) if len(monthly_df) else "",
        }

    if strategy_variant in {"factor_composite_ai_filter", "factor_composite_ai_overlay", "factor_composite_ai_limited_replace"}:
        summary = json.loads((variants_dir / strategy_variant / "summary.json").read_text(encoding="utf-8"))
        periods = int(summary.get("periods", monthly_df["rebalance_month"].nunique()))
        trained_periods = int(summary.get("trained_periods", 0))
        return {
            "eval_months": int(summary.get("months", len(monthly_df))),
            "eval_periods": periods,
            "trained_only_eval": False,
            "trained_months": int(monthly_df.loc[pd.to_datetime(monthly_df["rebalance_month"]).isin(set(pd.read_csv(wf_dir / "walkforward_oos_predictions.csv").assign(rebalance_month=lambda x: pd.to_datetime(x["rebalance_month"], errors="coerce")).loc[lambda x: x["model_status"].astype(str) == "trained", "rebalance_month"].dropna().tolist()))].shape[0]),
            "trained_periods": trained_periods,
            "fallback_baseline_included": bool(trained_periods < periods),
            "eval_start_month": str(pd.Timestamp(monthly_df["month_end"].min()).date()) if len(monthly_df) else "",
            "eval_end_month": str(pd.Timestamp(monthly_df["month_end"].max()).date()) if len(monthly_df) else "",
        }

    return {
        "eval_months": int(len(monthly_df)),
        "eval_periods": int(monthly_df["rebalance_month"].nunique()) if len(monthly_df) else 0,
        "trained_only_eval": False,
        "trained_months": 0,
        "trained_periods": 0,
        "fallback_baseline_included": False,
        "eval_start_month": str(pd.Timestamp(monthly_df["month_end"].min()).date()) if len(monthly_df) else "",
        "eval_end_month": str(pd.Timestamp(monthly_df["month_end"].max()).date()) if len(monthly_df) else "",
    }


def _load_rebalance_sets(strategy_variant: str, *, asof: str, metric: str, k: int, strategy: str, quant_out_v: int, factor_out_v: int, wf_dir: Path, combo_dir: Path, variants_dir: Path) -> dict[pd.Timestamp, set[str]]:
    if strategy_variant == "quant_only":
        df = pd.read_parquet(f"data/processed/picks__asof={asof}__metric={metric}__k={k}__strat={strategy}__v={quant_out_v}.parquet")
        return _group_sets(df)
    if strategy_variant == "factor_composite":
        df = pd.read_parquet(f"data/processed/picks__asof={asof}__metric={metric}__k={k}__strat={strategy}__v={factor_out_v}.parquet")
        return _group_sets(df)
    if strategy_variant == "ai_wf_topk":
        df = pd.read_csv(wf_dir / "walkforward_integrated_holdings.csv").copy()
        df = df.loc[df["portfolio"].astype(str) == "ai"].copy()
        return _group_sets(df)
    if strategy_variant == "combo_overlay_replace":
        df = pd.read_csv(combo_dir / "combo_rebalance_detail.csv").copy()
        out: dict[pd.Timestamp, set[str]] = {}
        for _, row in df.iterrows():
            rm = pd.Timestamp(row["rebalance_month"])
            vals = [_normalize_ticker_value(v) for v in str(row.get("combo_tickers", "")).split(",")]
            out[rm] = {v for v in vals if v}
        return out
    df = pd.read_csv(variants_dir / strategy_variant / "variant_rebalance_detail.csv").copy()
    out = {}
    for _, row in df.iterrows():
        rm = pd.Timestamp(row["rebalance_month"])
        vals = [_normalize_ticker_value(v) for v in str(row.get("tickers", "")).split(",")]
        out[rm] = {v for v in vals if v}
    return out


def _calc_overlap_stats(target_sets: dict[pd.Timestamp, set[str]], factor_sets: dict[pd.Timestamp, set[str]]) -> tuple[float, float]:
    keys = sorted(set(target_sets.keys()) & set(factor_sets.keys()))
    if not keys:
        return np.nan, np.nan
    overlap_ratios = []
    changed_counts = []
    for k in keys:
        lhs = target_sets.get(k, set())
        rhs = factor_sets.get(k, set())
        denom = max(len(rhs), 1)
        overlap = len(lhs & rhs)
        overlap_ratios.append(float(overlap / denom))
        changed_counts.append(float(max(len(rhs) - overlap, 0)))
    return float(np.mean(overlap_ratios)), float(np.mean(changed_counts))


def _calc_period_win_rate(target_monthly: pd.DataFrame, factor_monthly: pd.DataFrame) -> float:
    lhs = _build_period_return_map(target_monthly)
    rhs = _build_period_return_map(factor_monthly)
    keys = sorted(set(lhs.keys()) & set(rhs.keys()))
    if not keys:
        return np.nan
    return float(np.mean([lhs[k] > rhs[k] for k in keys]))


def _calc_trained_only_stats(monthly_df: pd.DataFrame, trained_rms: set[pd.Timestamp]) -> tuple[float, float]:
    trained = monthly_df.loc[pd.to_datetime(monthly_df["rebalance_month"]).isin(trained_rms)].copy()
    if len(trained) == 0:
        return np.nan, np.nan
    nav = (1.0 + _to_num(trained["ret_net"]).fillna(0.0)).cumprod()
    return float(nav.iloc[-1]), _calc_cagr_from_nav(nav)


def _calc_common_period_stats(monthly_df: pd.DataFrame, common_start: pd.Timestamp, common_end: pd.Timestamp) -> dict[str, float]:
    mm = monthly_df.copy()
    mm["month_end"] = pd.to_datetime(mm["month_end"], errors="coerce")
    mm = mm.loc[(mm["month_end"] >= common_start) & (mm["month_end"] <= common_end)].copy()
    if len(mm) == 0:
        return {
            "net_nav_common": np.nan,
            "cagr_common": np.nan,
            "sharpe_common": np.nan,
            "mdd_common": np.nan,
        }
    ret = _to_num(mm["ret_net"]).fillna(0.0)
    nav = (1.0 + ret).cumprod()
    return {
        "net_nav_common": float(nav.iloc[-1]),
        "cagr_common": _calc_cagr_from_nav(nav),
        "sharpe_common": float(calc_sharpe(ret)),
        "mdd_common": float(calc_mdd_from_nav(nav)),
    }


def _load_master_meta(asof: str) -> dict:
    meta_path = Path(f"data/processed/krx_master__asof={asof}__src=pykrx__v=1.meta.json")
    if not meta_path.exists():
        return {}
    return json.loads(meta_path.read_text(encoding="utf-8"))


def _score_for_ranking(row: pd.Series) -> float:
    cagr = float(pd.to_numeric(pd.Series([row.get("cagr_net")]), errors="coerce").iloc[0])
    sharpe = float(pd.to_numeric(pd.Series([row.get("sharpe_net")]), errors="coerce").iloc[0])
    mdd = float(pd.to_numeric(pd.Series([row.get("maxdd_net")]), errors="coerce").iloc[0])
    nav = float(pd.to_numeric(pd.Series([row.get("net_nav")]), errors="coerce").iloc[0])
    turnover = float(pd.to_numeric(pd.Series([row.get("avg_turnover")]), errors="coerce").iloc[0])
    cagr = 0.0 if pd.isna(cagr) else cagr
    sharpe = 0.0 if pd.isna(sharpe) else sharpe
    mdd = -1.0 if pd.isna(mdd) else abs(mdd)
    nav = 0.0 if pd.isna(nav) else nav
    turnover = 0.0 if pd.isna(turnover) else turnover
    return (2.0 * cagr) + (1.5 * sharpe) + (0.25 * nav) - (0.75 * mdd) - (0.15 * turnover)


def _score_for_ranking_common(row: pd.Series) -> float:
    cagr = float(pd.to_numeric(pd.Series([row.get("cagr_common")]), errors="coerce").iloc[0])
    sharpe = float(pd.to_numeric(pd.Series([row.get("sharpe_common")]), errors="coerce").iloc[0])
    mdd = float(pd.to_numeric(pd.Series([row.get("mdd_common")]), errors="coerce").iloc[0])
    nav = float(pd.to_numeric(pd.Series([row.get("net_nav_common")]), errors="coerce").iloc[0])
    turnover = float(pd.to_numeric(pd.Series([row.get("avg_turnover")]), errors="coerce").iloc[0])
    cagr = 0.0 if pd.isna(cagr) else cagr
    sharpe = 0.0 if pd.isna(sharpe) else sharpe
    mdd = -1.0 if pd.isna(mdd) else abs(mdd)
    nav = 0.0 if pd.isna(nav) else nav
    turnover = 0.0 if pd.isna(turnover) else turnover
    return (2.0 * cagr) + (1.5 * sharpe) + (0.25 * nav) - (0.75 * mdd) - (0.15 * turnover)


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the 7-way strategy comparison pipeline.")
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--strategy", default="D_quality_filter_debt_profitaccel_liq")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--feat_v", type=int, default=3291)
    ap.add_argument("--ret_v", type=int, default=1)
    ap.add_argument("--factor_v", type=int, default=3291)
    ap.add_argument("--raw_px_v", type=int, default=1)
    ap.add_argument("--tcost_bps", type=float, default=30.0)
    ap.add_argument("--model", default="ridge", choices=["ridge", "elasticnet", "random_forest"])
    ap.add_argument("--feature_profile", default="compact_dailyagg")
    ap.add_argument("--min_train_rows", type=int, default=20)
    ap.add_argument("--valid_rows", type=int, default=4)
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--cap_profit_accel_delta", type=float, default=-1.0)
    ap.add_argument("--ai_overlay_strength", type=float, default=1.0)
    ap.add_argument("--mcap_top", type=int, default=800)
    ap.add_argument("--trd_bot", type=float, default=0.1)
    ap.add_argument("--start", default="20110101")
    ap.add_argument("--lookback_years", type=int, default=15)
    ap.add_argument("--out_dir", default="")
    ap.add_argument("--skip_prepare", action="store_true")
    ap.add_argument("--ai_filter_threshold", type=float, default=0.55)
    ap.add_argument("--ai_filter_preselect_mult", type=float, default=2.0)
    ap.add_argument("--ai_overlay_alpha_limited", type=float, default=0.15)
    ap.add_argument("--ai_overlay_cap_limited", type=float, default=0.20)
    ap.add_argument("--ai_limited_replace_max", type=int, default=2)
    args = ap.parse_args()

    out_dir = Path(args.out_dir) if args.out_dir else Path(f"artifacts/strategy_compare/asof={args.asof}")
    _ensure_dir(out_dir)

    prepare_args = [
        "--asof", args.asof,
        "--metric", args.metric,
        "--universe_v", "1",
        "--mcap_top", str(args.mcap_top),
        "--trd_bot", str(args.trd_bot),
        "--px_v", str(args.raw_px_v),
        "--ret_v", str(args.ret_v),
        "--lookback_years", str(args.lookback_years),
        "--start", args.start,
        "--fund_v", "1",
        "--factor_in_v", "1",
        "--factor_out_v", str(args.factor_v),
        "--fs_div", "CFS",
        "--with_shares_industry",
        "--shares_out_v", "1",
        "--fund_mode", "union",
    ]
    if not args.skip_prepare:
        _run("prepare_asof", "scripts/data_pipeline/prepare_asof.py", prepare_args)

    _run(
        "make_features_live",
        "scripts/data_pipeline/make_features_live.py",
        ["--asof", args.asof, "--metric", args.metric, "--in_v", str(args.factor_v), "--out_v", str(args.feat_v), "--save_meta"],
    )
    master_meta = _load_master_meta(args.asof)
    provisional = bool(master_meta.get("provisional", False))
    provisional_source = str(master_meta.get("source", ""))

    dataset_path = out_dir / "factor_regime_dataset.parquet"
    _run(
        "build_factor_regime_dataset",
        "scripts/factor_weight_ml/build_factor_regime_dataset_dailyagg.py",
        [
            "--asof", args.asof,
            "--metric", args.metric,
            "--feat_v", str(args.feat_v),
            "--ret_v", str(args.ret_v),
            "--strategy", args.strategy,
            "--raw_px_v", str(args.raw_px_v),
            "--out_path", str(dataset_path),
        ],
    )

    model_dir = out_dir / "factor_weight_model"
    _run(
        "train_factor_weight_model",
        "scripts/factor_weight_ml/train_factor_weight_model.py",
        [
            "--dataset_path", str(dataset_path),
            "--model", args.model,
            "--feature_profile", args.feature_profile,
            "--min_train_rows", str(args.min_train_rows),
            "--valid_rows", str(args.valid_rows),
            "--temperature", str(args.temperature),
            "--out_dir", str(model_dir),
        ],
    )

    model_path = model_dir / "factor_weight_model.joblib"

    quant_out_v = 101
    factor_out_v = 102

    _run(
        "quant_only",
        "scripts/backtest/backtest_quarterly_rebalance_v2.py",
        [
            "--asof", args.asof,
            "--metric", args.metric,
            "--feat_v", str(args.feat_v),
            "--ret_v", str(args.ret_v),
            "--k", str(args.k),
            "--strategy", args.strategy,
            "--out_v", str(quant_out_v),
            "--tcost_bps", str(args.tcost_bps),
            "--hold_bonus", "0",
            "--disable_quality_penalty",
            "--disable_expectation_overlay",
        ],
    )

    _run(
        "factor_composite",
        "scripts/backtest/backtest_quarterly_rebalance_v2.py",
        [
            "--asof", args.asof,
            "--metric", args.metric,
            "--feat_v", str(args.feat_v),
            "--ret_v", str(args.ret_v),
            "--k", str(args.k),
            "--strategy", args.strategy,
            "--out_v", str(factor_out_v),
            "--tcost_bps", str(args.tcost_bps),
            "--ai_model_path", str(model_path),
            "--ai_raw_px_v", str(args.raw_px_v),
            "--ai_temperature", str(args.temperature),
            "--ai_cap_profit_accel_delta", str(args.cap_profit_accel_delta),
            "--ai_overlay_strength", str(args.ai_overlay_strength),
        ],
    )

    wf_dir = out_dir / "ai_wf_topk"
    _run(
        "ai_wf_topk",
        "scripts/factor_weight_ml/backtest_factor_weight_integrated_topk_walkforward.py",
        [
            "--asof", args.asof,
            "--metric", args.metric,
            "--feat_v", str(args.feat_v),
            "--ret_v", str(args.ret_v),
            "--strategy", args.strategy,
            "--k", str(args.k),
            "--raw_px_v", str(args.raw_px_v),
            "--derive_traded_value",
            "--model", args.model,
            "--feature_profile", args.feature_profile,
            "--min_train_rows", str(args.min_train_rows),
            "--temperature", str(args.temperature),
            "--cap_profit_accel_delta", str(args.cap_profit_accel_delta),
            "--ai_overlay_strength", str(args.ai_overlay_strength),
            "--tcost_bps", str(args.tcost_bps),
            "--out_dir", str(wf_dir),
        ],
    )
    ai_scored_path = wf_dir / "walkforward_integrated_scored_universe.parquet"

    factor_picks_path = Path(f"data/processed/picks__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={factor_out_v}.parquet")
    returns_path = Path(f"data/processed/returns_monthly__src=pykrx__asof={args.asof}__metric={args.metric}__v={args.ret_v}.parquet")

    combo_dir = out_dir / "combo"
    _run(
        "combo",
        "scripts/backtest/backtest_combo_strategy.py",
        [
            "--factor_picks_path", str(factor_picks_path),
            "--ai_holdings_path", str(wf_dir / "walkforward_integrated_holdings.csv"),
            "--returns_path", str(returns_path),
            "--out_dir", str(combo_dir),
            "--variant", "overlay_replace",
            "--k", str(args.k),
            "--tcost_bps", str(args.tcost_bps),
        ],
    )

    variants_dir = out_dir / "factor_composite_ai_variants"
    _run(
        "factor_composite_ai_variants",
        "scripts/backtest/backtest_factor_composite_ai_variants.py",
        [
            "--asof", args.asof,
            "--metric", args.metric,
            "--strategy", args.strategy,
            "--k", str(args.k),
            "--feat_v", str(args.feat_v),
            "--ret_v", str(args.ret_v),
            "--tcost_bps", str(args.tcost_bps),
            "--max_per_group", "0",
            "--factor_baseline_v", str(factor_out_v),
            "--ai_scored_path", str(ai_scored_path),
            "--out_dir", str(variants_dir),
            "--ai_threshold", str(args.ai_filter_threshold),
            "--preselect_mult", str(args.ai_filter_preselect_mult),
            "--ai_overlay_alpha", str(args.ai_overlay_alpha_limited),
            "--ai_overlay_cap", str(args.ai_overlay_cap_limited),
            "--max_replace", str(args.ai_limited_replace_max),
        ],
    )

    quant_summary = _load_single_row_csv(Path(f"data/processed/report__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={quant_out_v}.csv"))
    factor_summary = _load_single_row_csv(Path(f"data/processed/report__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={factor_out_v}.csv"))
    wf_summary = json.loads((wf_dir / "summary.json").read_text(encoding="utf-8"))
    combo_summary = json.loads((combo_dir / "summary.json").read_text(encoding="utf-8"))
    variant_summary = pd.read_csv(variants_dir / "variant_summary.csv")

    rows = [
        {
            "strategy_variant": "quant_only",
            "net_nav": float(quant_summary.get("net_nav")),
            "cagr_net": float(quant_summary.get("cagr_net")),
            "sharpe_net": float(quant_summary.get("sharpe_net")),
            "maxdd_net": float(quant_summary.get("maxdd_net")),
            "avg_turnover": float(quant_summary.get("avg_turnover")),
            "provisional": provisional,
            "provisional_source": provisional_source,
        },
        {
            "strategy_variant": "factor_composite",
            "net_nav": float(factor_summary.get("net_nav")),
            "cagr_net": float(factor_summary.get("cagr_net")),
            "sharpe_net": float(factor_summary.get("sharpe_net")),
            "maxdd_net": float(factor_summary.get("maxdd_net")),
            "avg_turnover": float(factor_summary.get("avg_turnover")),
            "provisional": provisional,
            "provisional_source": provisional_source,
        },
        {
            "strategy_variant": "ai_wf_topk",
            "net_nav": float(wf_summary.get("ai_nav_net")),
            "cagr_net": float(wf_summary.get("ai_cagr_net")),
            "sharpe_net": float(wf_summary.get("ai_sharpe_net")),
            "maxdd_net": float(wf_summary.get("ai_mdd_net")),
            "avg_turnover": float(wf_summary.get("avg_ai_turnover")),
            "provisional": provisional,
            "provisional_source": provisional_source,
        },
        {
            "strategy_variant": str(combo_summary.get("strategy_variant", "combo")),
            "net_nav": float(combo_summary.get("net_nav")),
            "cagr_net": float(combo_summary.get("cagr_net")),
            "sharpe_net": float(combo_summary.get("sharpe_net")),
            "maxdd_net": float(combo_summary.get("maxdd_net")),
            "avg_turnover": float(combo_summary.get("avg_turnover")),
            "avg_overlap_ratio": float(combo_summary.get("avg_overlap_ratio")),
            "provisional": provisional,
            "provisional_source": provisional_source,
        },
    ]
    for _, row in variant_summary.iterrows():
        rows.append(
            {
                "strategy_variant": str(row.get("strategy_variant")),
                "net_nav": float(row.get("net_nav")),
                "cagr_net": float(row.get("cagr_net")),
                "sharpe_net": float(row.get("sharpe_net")),
                "maxdd_net": float(row.get("maxdd_net")),
                "avg_turnover": float(row.get("avg_turnover")),
                "avg_overlap_ratio": float(row.get("avg_overlap_ratio")),
                "avg_ai_changed_count": float(row.get("avg_ai_changed_count")),
                "trained_only_nav_net": float(row.get("trained_only_nav_net")),
                "trained_only_cagr_net": float(row.get("trained_only_cagr_net")),
                "period_win_rate_vs_factor_composite": float(row.get("period_win_rate_vs_factor_composite")),
                "provisional": provisional,
                "provisional_source": provisional_source,
            }
        )

    comparison = pd.DataFrame(rows)
    factor_monthly = _load_monthly_frame(
        "factor_composite",
        asof=args.asof,
        metric=args.metric,
        k=args.k,
        strategy=args.strategy,
        quant_out_v=quant_out_v,
        factor_out_v=factor_out_v,
        wf_dir=wf_dir,
        combo_dir=combo_dir,
        variants_dir=variants_dir,
    )
    factor_sets = _load_rebalance_sets(
        "factor_composite",
        asof=args.asof,
        metric=args.metric,
        k=args.k,
        strategy=args.strategy,
        quant_out_v=quant_out_v,
        factor_out_v=factor_out_v,
        wf_dir=wf_dir,
        combo_dir=combo_dir,
        variants_dir=variants_dir,
    )
    preds_df = pd.read_csv(wf_dir / "walkforward_oos_predictions.csv")
    preds_df["rebalance_month"] = pd.to_datetime(preds_df["rebalance_month"], errors="coerce")
    trained_rms = set(preds_df.loc[preds_df["model_status"].astype(str) == "trained", "rebalance_month"].dropna().tolist())

    enrich_rows = []
    monthly_by_variant: dict[str, pd.DataFrame] = {}
    for _, row in comparison.iterrows():
        variant = str(row["strategy_variant"])
        monthly_df = _load_monthly_frame(
            variant,
            asof=args.asof,
            metric=args.metric,
            k=args.k,
            strategy=args.strategy,
            quant_out_v=quant_out_v,
            factor_out_v=factor_out_v,
            wf_dir=wf_dir,
            combo_dir=combo_dir,
            variants_dir=variants_dir,
        )
        monthly_by_variant[variant] = monthly_df.copy()
        sets_df = _load_rebalance_sets(
            variant,
            asof=args.asof,
            metric=args.metric,
            k=args.k,
            strategy=args.strategy,
            quant_out_v=quant_out_v,
            factor_out_v=factor_out_v,
            wf_dir=wf_dir,
            combo_dir=combo_dir,
            variants_dir=variants_dir,
        )
        overlap_ratio, changed_count = _calc_overlap_stats(sets_df, factor_sets)
        trained_nav, trained_cagr = _calc_trained_only_stats(monthly_df, trained_rms)
        win_rate = 1.0 if variant == "factor_composite" else _calc_period_win_rate(monthly_df, factor_monthly)
        eval_meta = _load_eval_meta(
            variant,
            asof=args.asof,
            metric=args.metric,
            k=args.k,
            strategy=args.strategy,
            quant_out_v=quant_out_v,
            factor_out_v=factor_out_v,
            wf_dir=wf_dir,
            combo_dir=combo_dir,
            variants_dir=variants_dir,
        )
        row2 = row.to_dict()
        row2["avg_overlap_ratio"] = overlap_ratio if pd.isna(row.get("avg_overlap_ratio", np.nan)) else row.get("avg_overlap_ratio")
        row2["avg_ai_changed_count"] = 0.0 if variant == "factor_composite" else (changed_count if pd.isna(row.get("avg_ai_changed_count", np.nan)) else row.get("avg_ai_changed_count"))
        row2["trained_only_nav_net"] = trained_nav if pd.isna(row.get("trained_only_nav_net", np.nan)) else row.get("trained_only_nav_net")
        row2["trained_only_cagr_net"] = trained_cagr if pd.isna(row.get("trained_only_cagr_net", np.nan)) else row.get("trained_only_cagr_net")
        row2["period_win_rate_vs_factor_composite"] = win_rate if pd.isna(row.get("period_win_rate_vs_factor_composite", np.nan)) else row.get("period_win_rate_vs_factor_composite")
        row2.update(eval_meta)
        enrich_rows.append(row2)
    comparison = pd.DataFrame(enrich_rows)
    common_start = pd.to_datetime(comparison["eval_start_month"], errors="coerce").max()
    common_end = pd.to_datetime(comparison["eval_end_month"], errors="coerce").min()
    common_rows = []
    for _, row in comparison.iterrows():
        variant = str(row["strategy_variant"])
        row2 = row.to_dict()
        row2["common_eval_start_month"] = str(pd.Timestamp(common_start).date()) if pd.notna(common_start) else ""
        row2["common_eval_end_month"] = str(pd.Timestamp(common_end).date()) if pd.notna(common_end) else ""
        row2.update(_calc_common_period_stats(monthly_by_variant[variant], common_start, common_end))
        common_rows.append(row2)
    comparison = pd.DataFrame(common_rows)
    comparison["rank_score"] = comparison.apply(_score_for_ranking, axis=1)
    comparison["common_rank_score"] = comparison.apply(_score_for_ranking_common, axis=1)
    comparison = comparison.sort_values(["common_rank_score", "cagr_common", "sharpe_common"], ascending=[False, False, False]).reset_index(drop=True)
    full_winner = comparison.sort_values(["rank_score", "sharpe_net", "cagr_net"], ascending=[False, False, False]).iloc[0].to_dict()
    common_winner = comparison.iloc[0].to_dict()
    best = {
        "full_period_winner": full_winner,
        "common_period_winner": common_winner,
        "common_eval_start_month": str(pd.Timestamp(common_start).date()) if pd.notna(common_start) else "",
        "common_eval_end_month": str(pd.Timestamp(common_end).date()) if pd.notna(common_end) else "",
        "krx_master_meta": master_meta,
        "rerun_required_when_primary_collection_recovers": provisional,
    }

    comparison_path = out_dir / "strategy_comparison.csv"
    best_path = out_dir / "best_strategy.json"
    comparison.to_csv(comparison_path, index=False, encoding="utf-8-sig")
    best_path.write_text(json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] comparison : {comparison_path}")
    print(f"[OK] best       : {best_path}")
    print(f"[INFO] selected full_period_winner={full_winner['strategy_variant']}")
    print(f"[INFO] selected common_period_winner={common_winner['strategy_variant']}")


if __name__ == "__main__":
    main()
