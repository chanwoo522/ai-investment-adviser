#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.ensemble import RandomForestRegressor

# Optional boosters
try:
    from xgboost import XGBRegressor
except Exception:
    XGBRegressor = None

try:
    from lightgbm import LGBMRegressor
except Exception:
    LGBMRegressor = None


LABEL_COLS = {"label_l1", "label_l2", "label_l3"}
FUTURE_COLS = {
    "entry_price", "entry_price_date", "exit_price", "exit_price_date",
    "fwd_return", "benchmark_return", "benchmark_entry_date", "benchmark_exit_date",
    "excess_return", "next_rebalance_month",
}
ID_TIME_COLS = {
    "ticker", "corp_code", "name", "name_final",
    "year", "quarter", "quarter_key", "rebalance_month",
    "benchmark_name", "benchmark_ticker",
}
CONTROL_COLS = {
    "passed_filters",
    # score_raw는 이번 실험에서는 명시적으로 사용 가능
}


FEATURE_PRESETS: dict[str, list[str]] = {
    "core_plus_score": [
        "score_raw",
        "OpIncome_acc2_log1p",
        "Revenue_acc2",
        "Debt_to_Equity_log",
        "Quality_CFO_to_Assets",
        "op_growth_streak2",
        "rev_growth_streak2",
        "op_qoq",
        "revenue_qoq",
        "OpIncome_ttm_yoy",
        "Revenue_ttm_yoy",
        "NetIncome_ttm_yoy",
        "CFO_ttm_yoy",
        "OpMargin_ttm",
        "OpIncome_to_Assets_ttm",
        "CFO_to_Assets_ttm",
        "history_quarters",
        "CFO_isnull",
        "CFO_warn",
        "CFO_safe",
    ],
    "core_plus_score_price": [
        "score_raw",
        "OpIncome_acc2_log1p",
        "Revenue_acc2",
        "Debt_to_Equity_log",
        "Quality_CFO_to_Assets",
        "op_growth_streak2",
        "rev_growth_streak2",
        "op_qoq",
        "revenue_qoq",
        "OpIncome_ttm_yoy",
        "Revenue_ttm_yoy",
        "NetIncome_ttm_yoy",
        "CFO_ttm_yoy",
        "OpMargin_ttm",
        "OpIncome_to_Assets_ttm",
        "CFO_to_Assets_ttm",
        "history_quarters",
        "CFO_isnull",
        "CFO_warn",
        "CFO_safe",
        "ret_21d",
        "ret_63d",
        "ret_126d",
        "ret_252d",
        "vol_21d",
        "vol_63d",
        "mdd_63d",
        "mdd_126d",
    ],
    "all_numeric_plus_score": [],  # special handling
}


def zscore_series(s: pd.Series) -> pd.Series:
    x = pd.to_numeric(s, errors="coerce")
    mu = x.mean()
    sd = x.std()
    if pd.isna(sd) or sd == 0:
        return pd.Series(np.zeros(len(x)), index=x.index, dtype="float64")
    return ((x - mu) / sd).astype("float64")


def pick_feature_cols(df: pd.DataFrame, feature_set: str) -> list[str]:
    if feature_set == "all_numeric_plus_score":
        exclude = LABEL_COLS | FUTURE_COLS | ID_TIME_COLS | CONTROL_COLS
        cols = []
        for c in df.columns:
            if c in exclude:
                continue
            if pd.api.types.is_numeric_dtype(df[c]):
                cols.append(c)
        return cols

    preset = FEATURE_PRESETS.get(feature_set)
    if preset is None:
        raise ValueError(f"unsupported feature_set: {feature_set}")

    cols = [c for c in preset if c in df.columns]
    if not cols:
        raise RuntimeError(f"No usable feature columns found for feature_set={feature_set}")
    return cols


def make_model(model_name: str, random_state: int):
    if model_name == "random_forest":
        return Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("reg", RandomForestRegressor(
                    n_estimators=400,
                    max_depth=6,
                    min_samples_leaf=10,
                    random_state=random_state,
                    n_jobs=-1,
                )),
            ]
        )

    if model_name == "xgboost":
        if XGBRegressor is None:
            raise ImportError("xgboost is not installed. Run: pip install xgboost")
        return Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("reg", XGBRegressor(
                    n_estimators=400,
                    max_depth=4,
                    learning_rate=0.05,
                    subsample=0.8,
                    colsample_bytree=0.8,
                    reg_lambda=1.0,
                    reg_alpha=0.0,
                    random_state=random_state,
                    n_jobs=-1,
                )),
            ]
        )

    if model_name == "lightgbm":
        if LGBMRegressor is None:
            raise ImportError("lightgbm is not installed. Run: pip install lightgbm")
        return Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("reg", LGBMRegressor(
                    n_estimators=400,
                    learning_rate=0.05,
                    num_leaves=31,
                    subsample=0.8,
                    colsample_bytree=0.8,
                    random_state=random_state,
                    n_jobs=-1,
                    verbose=-1,
                )),
            ]
        )

    raise ValueError(f"unsupported model_name: {model_name}")


def make_candidate_subset(
    df: pd.DataFrame,
    *,
    top_n_score: int,
    require_nonnull_return: bool,
) -> pd.DataFrame:
    g = df.copy()
    g = g.loc[pd.to_numeric(g["passed_filters"], errors="coerce").fillna(False).astype(bool)].copy()
    g["score_raw"] = pd.to_numeric(g["score_raw"], errors="coerce")
    g["fwd_return"] = pd.to_numeric(g["fwd_return"], errors="coerce")
    g["excess_return"] = pd.to_numeric(g["excess_return"], errors="coerce")
    g = g.dropna(subset=["score_raw"]).copy()
    if require_nonnull_return:
        g = g.dropna(subset=["fwd_return"]).copy()
    g = g.sort_values(["score_raw", "ticker"], ascending=[False, True]).head(int(top_n_score)).copy()
    return g.reset_index(drop=True)


def add_relative_target(df: pd.DataFrame, target_mode: str) -> pd.DataFrame:
    g = df.copy()

    if target_mode == "cohort_rank_fwd":
        vals = pd.to_numeric(g["fwd_return"], errors="coerce")
    elif target_mode == "cohort_rank_excess":
        vals = pd.to_numeric(g["excess_return"], errors="coerce")
    else:
        raise ValueError(f"unsupported target_mode: {target_mode}")

    g["y_target"] = vals.rank(method="average", pct=True)
    return g


def equal_weight_topk_return(df: pd.DataFrame, top_k: int) -> dict:
    g = df.copy()
    g = g.sort_values(["score_raw", "ticker"], ascending=[False, True]).head(int(top_k)).copy()

    if len(g) == 0:
        return {"n": 0, "mean_ret": np.nan, "mean_excess": np.nan}

    return {
        "n": int(len(g)),
        "mean_ret": float(pd.to_numeric(g["fwd_return"], errors="coerce").mean()),
        "mean_excess": float(pd.to_numeric(g["excess_return"], errors="coerce").mean()),
    }


def adjusted_topk_return(df: pd.DataFrame, ai_pred: np.ndarray, lam: float, top_k: int) -> dict:
    g = df.copy().reset_index(drop=True)
    if len(g) != len(ai_pred):
        raise ValueError(f"Length mismatch: len(df)={len(g)} vs len(ai_pred)={len(ai_pred)}")

    g["score_z"] = zscore_series(g["score_raw"])
    g["ai_z"] = zscore_series(pd.Series(ai_pred, index=g.index))
    g["score_final"] = g["score_z"] + float(lam) * g["ai_z"]

    picked = g.sort_values(["score_final", "ticker"], ascending=[False, True]).head(int(top_k)).copy()

    if len(picked) == 0:
        return {"n": 0, "mean_ret": np.nan, "mean_excess": np.nan}

    return {
        "n": int(len(picked)),
        "mean_ret": float(pd.to_numeric(picked["fwd_return"], errors="coerce").mean()),
        "mean_excess": float(pd.to_numeric(picked["excess_return"], errors="coerce").mean()),
    }


def mean_quarterly_return_for_lambda(valid_df: pd.DataFrame, pred: np.ndarray, lam: float, top_k: int) -> float:
    res = adjusted_topk_return(valid_df, ai_pred=pred, lam=lam, top_k=top_k)
    return float(res["mean_ret"]) if pd.notna(res["mean_ret"]) else -np.inf


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset_path", required=True)
    ap.add_argument("--model", default="random_forest", choices=["random_forest", "xgboost", "lightgbm"])
    ap.add_argument(
        "--feature_set",
        default="core_plus_score",
        choices=["core_plus_score", "core_plus_score_price", "all_numeric_plus_score"],
    )
    ap.add_argument("--target_mode", default="cohort_rank_fwd", choices=["cohort_rank_fwd", "cohort_rank_excess"])
    ap.add_argument("--top_n_score", type=int, default=20)
    ap.add_argument("--top_k", type=int, default=10)
    ap.add_argument("--train_min_quarters", type=int, default=24)
    ap.add_argument("--valid_quarters", type=int, default=4)
    ap.add_argument("--require_total_quarters", type=int, default=30)
    ap.add_argument("--lambda_grid", default="0.00,0.05,0.10,0.15,0.20,0.30")
    ap.add_argument("--random_state", type=int, default=42)
    ap.add_argument("--out_dir", default="")
    args = ap.parse_args()

    ds = pd.read_parquet(args.dataset_path).copy()
    ds["rebalance_month"] = pd.to_datetime(ds["rebalance_month"], errors="coerce")
    ds = ds.dropna(subset=["rebalance_month", "fwd_return"]).copy()

    rb_list = sorted(pd.to_datetime(ds["rebalance_month"]).dropna().unique())
    if len(rb_list) < int(args.require_total_quarters):
        raise RuntimeError(
            f"Need at least {args.require_total_quarters} rebalance quarters. "
            f"Current unique rebalance_month count={len(rb_list)}"
        )

    cohort_chunks = []
    for _, g in ds.groupby("rebalance_month", sort=True):
        gg = make_candidate_subset(
            g,
            top_n_score=int(args.top_n_score),
            require_nonnull_return=True,
        )
        if len(gg) > 0:
            gg = add_relative_target(gg, args.target_mode)
            cohort_chunks.append(gg)

    if not cohort_chunks:
        raise RuntimeError("No candidate cohorts were built.")

    cohort_df = pd.concat(cohort_chunks, ignore_index=True)
    feature_cols = pick_feature_cols(cohort_df, args.feature_set)

    out_dir = Path(args.out_dir) if args.out_dir else Path(
        f"data/ai_filter/reports/{Path(args.dataset_path).stem}"
        f"__score_adjust__{args.target_mode}__{args.model}__{args.feature_set}"
        f"__topn={args.top_n_score}__topk={args.top_k}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    lambda_grid = [float(x.strip()) for x in args.lambda_grid.split(",") if x.strip() != ""]
    if not lambda_grid:
        raise RuntimeError("lambda_grid is empty.")

    fold_rows = []
    pred_rows = []

    start_idx = int(args.train_min_quarters) + int(args.valid_quarters)
    if start_idx >= len(rb_list):
        raise RuntimeError("Not enough quarters for train/valid/test split.")

    for test_idx in range(start_idx, len(rb_list)):
        train_months = rb_list[: test_idx - int(args.valid_quarters)]
        valid_months = rb_list[test_idx - int(args.valid_quarters): test_idx]
        test_month = rb_list[test_idx]

        train_df = cohort_df.loc[cohort_df["rebalance_month"].isin(train_months)].copy()
        valid_df = cohort_df.loc[cohort_df["rebalance_month"].isin(valid_months)].copy()
        test_df = cohort_df.loc[cohort_df["rebalance_month"] == test_month].copy()

        if len(train_df) == 0 or len(valid_df) == 0 or len(test_df) == 0:
            continue

        X_train = train_df[feature_cols].copy()
        y_train = pd.to_numeric(train_df["y_target"], errors="coerce").astype(float).values

        X_valid = valid_df[feature_cols].copy()
        X_test = test_df[feature_cols].copy()

        model = make_model(args.model, random_state=int(args.random_state))
        model.fit(X_train, y_train)

        valid_pred = model.predict(X_valid)

        best_lam = 0.0
        best_valid_ret = -np.inf
        for lam in lambda_grid:
            cur = mean_quarterly_return_for_lambda(valid_df, pred=valid_pred, lam=lam, top_k=int(args.top_k))
            if cur > best_valid_ret:
                best_valid_ret = cur
                best_lam = float(lam)

        test_pred = model.predict(X_test)

        base = equal_weight_topk_return(test_df, top_k=int(args.top_k))
        adj = adjusted_topk_return(test_df, ai_pred=test_pred, lam=best_lam, top_k=int(args.top_k))

        row = {
            "test_rebalance_month": str(pd.Timestamp(test_month).date()),
            "train_start": str(pd.Timestamp(train_months[0]).date()),
            "train_end": str(pd.Timestamp(train_months[-1]).date()),
            "valid_start": str(pd.Timestamp(valid_months[0]).date()),
            "valid_end": str(pd.Timestamp(valid_months[-1]).date()),
            "test_n": int(len(test_df)),
            "best_lambda": float(best_lam),
            "valid_best_mean_ret": float(best_valid_ret),
            "baseline_n": int(base["n"]),
            "baseline_mean_ret": float(base["mean_ret"]),
            "baseline_mean_excess": float(base["mean_excess"]),
            "adjusted_n": int(adj["n"]),
            "adjusted_mean_ret": float(adj["mean_ret"]),
            "adjusted_mean_excess": float(adj["mean_excess"]),
        }
        fold_rows.append(row)

        tmp = test_df[[
            "ticker", "rebalance_month", "score_raw", "fwd_return", "excess_return", "y_target"
        ]].copy()
        tmp["ai_pred"] = test_pred
        tmp["best_lambda"] = best_lam
        pred_rows.append(tmp)

    if not fold_rows:
        raise RuntimeError("No walk-forward folds were evaluated.")

    folds = pd.DataFrame(fold_rows)
    preds = pd.concat(pred_rows, ignore_index=True)

    summary = {
        "dataset_path": str(args.dataset_path),
        "model": args.model,
        "feature_set": args.feature_set,
        "target_mode": args.target_mode,
        "top_n_score": int(args.top_n_score),
        "top_k": int(args.top_k),
        "feature_count": int(len(feature_cols)),
        "feature_cols": feature_cols,
        "folds": int(len(folds)),
        "baseline_avg_quarterly_ret": float(pd.to_numeric(folds["baseline_mean_ret"], errors="coerce").mean()),
        "adjusted_avg_quarterly_ret": float(pd.to_numeric(folds["adjusted_mean_ret"], errors="coerce").mean()),
        "baseline_avg_quarterly_excess": float(pd.to_numeric(folds["baseline_mean_excess"], errors="coerce").mean()),
        "adjusted_avg_quarterly_excess": float(pd.to_numeric(folds["adjusted_mean_excess"], errors="coerce").mean()),
        "avg_best_lambda": float(pd.to_numeric(folds["best_lambda"], errors="coerce").mean()),
    }

    final_model = make_model(args.model, random_state=int(args.random_state))
    X_all = cohort_df[feature_cols].copy()
    y_all = pd.to_numeric(cohort_df["y_target"], errors="coerce").astype(float).values
    final_model.fit(X_all, y_all)

    folds_path = out_dir / "fold_metrics.csv"
    preds_path = out_dir / "oos_predictions.csv"
    model_path = out_dir / "final_model.joblib"
    summary_path = out_dir / "summary.json"

    folds.to_csv(folds_path, index=False, encoding="utf-8-sig")
    preds.to_csv(preds_path, index=False, encoding="utf-8-sig")
    joblib.dump(
        {
            "model": final_model,
            "feature_cols": feature_cols,
            "model_name": args.model,
            "feature_set": args.feature_set,
            "target_mode": args.target_mode,
            "top_n_score": args.top_n_score,
            "top_k": args.top_k,
        },
        model_path,
    )
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] folds   : {folds_path}")
    print(f"[OK] preds   : {preds_path}")
    print(f"[OK] model   : {model_path}")
    print(f"[OK] summary : {summary_path}")
    print(f"[INFO] baseline_avg_q_ret={summary['baseline_avg_quarterly_ret']:.6f}")
    print(f"[INFO] adjusted_avg_q_ret={summary['adjusted_avg_quarterly_ret']:.6f}")
    print(f"[INFO] baseline_avg_q_excess={summary['baseline_avg_quarterly_excess']:.6f}")
    print(f"[INFO] adjusted_avg_q_excess={summary['adjusted_avg_quarterly_excess']:.6f}")
    print(f"[INFO] avg_best_lambda={summary['avg_best_lambda']:.6f}")


if __name__ == "__main__":
    main()