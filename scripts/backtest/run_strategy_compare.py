from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import pandas as pd


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


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the 4-way strategy comparison pipeline.")
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

    quant_summary = _load_single_row_csv(Path(f"data/processed/report__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={quant_out_v}.csv"))
    factor_summary = _load_single_row_csv(Path(f"data/processed/report__asof={args.asof}__metric={args.metric}__k={args.k}__strat={args.strategy}__v={factor_out_v}.csv"))
    wf_summary = json.loads((wf_dir / "summary.json").read_text(encoding="utf-8"))
    combo_summary = json.loads((combo_dir / "summary.json").read_text(encoding="utf-8"))

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

    comparison = pd.DataFrame(rows)
    comparison["rank_score"] = comparison.apply(_score_for_ranking, axis=1)
    comparison = comparison.sort_values(["rank_score", "sharpe_net", "cagr_net"], ascending=[False, False, False]).reset_index(drop=True)
    best = comparison.iloc[0].to_dict()
    best["krx_master_meta"] = master_meta
    best["rerun_required_when_primary_collection_recovers"] = provisional

    comparison_path = out_dir / "strategy_comparison.csv"
    best_path = out_dir / "best_strategy.json"
    comparison.to_csv(comparison_path, index=False, encoding="utf-8-sig")
    best_path.write_text(json.dumps(best, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] comparison : {comparison_path}")
    print(f"[OK] best       : {best_path}")
    print(f"[INFO] selected strategy_variant={best['strategy_variant']}")


if __name__ == "__main__":
    main()
