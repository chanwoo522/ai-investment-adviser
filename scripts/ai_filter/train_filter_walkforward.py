# STATUS: archived experiment
# NOTE: hard classification filter showed unstable / non-robust results vs baseline.

#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

import joblib
import numpy as np
import pandas as pd

from sklearn.impute import SimpleImputer
from sklearn.metrics import (
    average_precision_score,
    balanced_accuracy_score,
    brier_score_loss,
    matthews_corrcoef,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import RandomForestClassifier

# Optional boosters
try:
    from xgboost import XGBClassifier
except Exception:
    XGBClassifier = None

try:
    from lightgbm import LGBMClassifier
except Exception:
    LGBMClassifier = None


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
    "passed_filters", "score_raw",  # 전략 비교용 통제 컬럼, 모델 입력에서는 제외
}


def pick_feature_cols(df: pd.DataFrame) -> list[str]:
    exclude = LABEL_COLS | FUTURE_COLS | ID_TIME_COLS | CONTROL_COLS
    cols = []
    for c in df.columns:
        if c in exclude:
            continue
        if pd.api.types.is_numeric_dtype(df[c]):
            cols.append(c)
    return cols


def make_model(model_name: str, random_state: int):
    if model_name == "logistic":
        return Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("clf", LogisticRegression(
                    C=1.0,
                    class_weight="balanced",
                    max_iter=500,
                    random_state=random_state,
                )),
            ]
        )

    if model_name == "random_forest":
        return Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("clf", RandomForestClassifier(
                    n_estimators=400,
                    max_depth=6,
                    min_samples_leaf=10,
                    class_weight="balanced_subsample",
                    random_state=random_state,
                    n_jobs=-1,
                )),
            ]
        )

    if model_name == "xgboost":
        if XGBClassifier is None:
            raise ImportError(
                "xgboost is not installed. Run: pip install xgboost"
            )
        return Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("clf", XGBClassifier(
                    n_estimators=400,
                    max_depth=4,
                    learning_rate=0.05,
                    subsample=0.8,
                    colsample_bytree=0.8,
                    reg_lambda=1.0,
                    reg_alpha=0.0,
                    random_state=random_state,
                    n_jobs=-1,
                    eval_metric="logloss",
                )),
            ]
        )

    if model_name == "lightgbm":
        if LGBMClassifier is None:
            raise ImportError(
                "lightgbm is not installed. Run: pip install lightgbm"
            )
        return Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("clf", LGBMClassifier(
                    n_estimators=400,
                    max_depth=-1,
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


def prob_to_label(prob: np.ndarray, threshold: float) -> np.ndarray:
    return (prob >= float(threshold)).astype(int)


def select_threshold_by_valid(y_true: np.ndarray, prob: np.ndarray, grid: Iterable[float]) -> float:
    best_thr = 0.50
    best_score = -np.inf
    for thr in grid:
        pred = prob_to_label(prob, thr)
        try:
            score = balanced_accuracy_score(y_true, pred)
        except Exception:
            score = -np.inf
        if score > best_score:
            best_score = score
            best_thr = float(thr)
    return best_thr


def metric_row(y_true: np.ndarray, prob: np.ndarray, threshold: float) -> dict:
    pred = prob_to_label(prob, threshold)
    out = {
        "threshold": float(threshold),
        "pr_auc": np.nan,
        "roc_auc": np.nan,
        "balanced_acc": np.nan,
        "brier": np.nan,
        "mcc": np.nan,
    }

    y_true = np.asarray(y_true).astype(int)
    prob = np.asarray(prob).astype(float)
    pred = np.asarray(pred).astype(int)

    if len(np.unique(y_true)) >= 2:
        out["pr_auc"] = float(average_precision_score(y_true, prob))
        out["roc_auc"] = float(roc_auc_score(y_true, prob))
        out["brier"] = float(brier_score_loss(y_true, prob))
        out["balanced_acc"] = float(balanced_accuracy_score(y_true, pred))
        out["mcc"] = float(matthews_corrcoef(y_true, pred))
    return out


def apply_cohort(df: pd.DataFrame, cohort_mode: str, top_n: int) -> pd.DataFrame:
    out = df.copy()

    if cohort_mode == "all":
        return out

    if cohort_mode == "passed_filters":
        return out.loc[
            pd.to_numeric(out["passed_filters"], errors="coerce").fillna(False).astype(bool)
        ].copy()

    if cohort_mode == "topn_score":
        chunks = []
        for _, g in out.groupby("rebalance_month", sort=True):
            g2 = g.loc[
                pd.to_numeric(g["passed_filters"], errors="coerce").fillna(False).astype(bool)
            ].copy()
            g2["score_raw"] = pd.to_numeric(g2["score_raw"], errors="coerce")
            g2 = g2.sort_values(
                ["score_raw", "ticker"],
                ascending=[False, True],
                na_position="last",
            ).head(int(top_n))
            chunks.append(g2)
        if chunks:
            return pd.concat(chunks, ignore_index=True)
        return out.head(0).copy()

    raise ValueError(f"unsupported cohort_mode: {cohort_mode}")


def make_strategy_eval_subset(df: pd.DataFrame) -> pd.DataFrame:
    """
    전략 성과 비교용 모집단 정의를 한 곳에서만 관리한다.
    baseline / filtered / passed_prob 계산이 모두 이 subset을 공유해야
    길이 불일치가 발생하지 않는다.
    """
    g = df.copy()
    g = g.loc[
        pd.to_numeric(g["passed_filters"], errors="coerce").fillna(False).astype(bool)
    ].copy()
    g["score_raw"] = pd.to_numeric(g["score_raw"], errors="coerce")
    g["fwd_return"] = pd.to_numeric(g["fwd_return"], errors="coerce")
    g["excess_return"] = pd.to_numeric(g["excess_return"], errors="coerce")
    g = g.dropna(subset=["score_raw", "fwd_return"]).copy()
    g = g.sort_values(["score_raw", "ticker"], ascending=[False, True]).reset_index(drop=True)
    return g


def baseline_topk_return(test_df: pd.DataFrame, top_k: int) -> dict:
    g = make_strategy_eval_subset(test_df)

    picked = g.head(int(top_k))
    if len(picked) == 0:
        return {"baseline_n": 0, "baseline_mean_ret": np.nan, "baseline_mean_excess": np.nan}

    return {
        "baseline_n": int(len(picked)),
        "baseline_mean_ret": float(picked["fwd_return"].mean()),
        "baseline_mean_excess": float(picked["excess_return"].mean()),
    }


def filtered_topk_return(test_df: pd.DataFrame, passed_prob: np.ndarray, threshold: float, top_k: int) -> dict:
    g = make_strategy_eval_subset(test_df)

    if len(g) == 0:
        return {"filtered_n": 0, "filtered_mean_ret": np.nan, "filtered_mean_excess": np.nan}

    if len(g) != len(passed_prob):
        raise ValueError(
            f"Length mismatch in filtered_topk_return: len(g)={len(g)} vs len(passed_prob)={len(passed_prob)}"
        )

    g = g.copy()
    g["pred_prob"] = np.asarray(passed_prob, dtype=float)

    passed = g.loc[g["pred_prob"] >= float(threshold)].copy()

    if len(passed) >= int(top_k):
        picked = passed.head(int(top_k)).copy()
    else:
        need = int(top_k) - len(passed)
        remain = g.loc[~g["ticker"].isin(passed["ticker"])].copy()
        backfill = remain.head(need)
        picked = pd.concat([passed, backfill], ignore_index=True)

    return {
        "filtered_n": int(len(picked)),
        "filtered_mean_ret": float(picked["fwd_return"].mean()) if len(picked) > 0 else np.nan,
        "filtered_mean_excess": float(picked["excess_return"].mean()) if len(picked) > 0 else np.nan,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset_path", required=True)
    ap.add_argument("--label", required=True, choices=["label_l1", "label_l2", "label_l3"])
    ap.add_argument(
        "--model",
        default="logistic",
        choices=["logistic", "random_forest", "xgboost", "lightgbm"],
    )
    ap.add_argument("--cohort_mode", default="passed_filters", choices=["all", "passed_filters", "topn_score"])
    ap.add_argument("--top_n_score", type=int, default=100)
    ap.add_argument("--top_k_strategy", type=int, default=10)
    ap.add_argument("--train_min_quarters", type=int, default=24)
    ap.add_argument("--valid_quarters", type=int, default=4)
    ap.add_argument("--require_total_quarters", type=int, default=40)
    ap.add_argument("--random_state", type=int, default=42)
    ap.add_argument("--out_dir", default="")
    args = ap.parse_args()

    ds = pd.read_parquet(args.dataset_path).copy()
    ds["rebalance_month"] = pd.to_datetime(ds["rebalance_month"], errors="coerce")
    ds = ds.dropna(subset=["rebalance_month", args.label]).copy()

    ds = apply_cohort(ds, cohort_mode=args.cohort_mode, top_n=args.top_n_score)

    rb_list = sorted(pd.to_datetime(ds["rebalance_month"]).dropna().unique())
    if len(rb_list) < int(args.require_total_quarters):
        raise RuntimeError(
            f"Need at least {args.require_total_quarters} rebalance quarters for this experiment. "
            f"Current unique rebalance_month count={len(rb_list)}"
        )

    feature_cols = pick_feature_cols(ds)
    if not feature_cols:
        raise RuntimeError("No feature columns selected.")

    out_dir = Path(args.out_dir) if args.out_dir else Path(
        f"data/ai_filter/reports/{Path(args.dataset_path).stem}__{args.label}__{args.model}__{args.cohort_mode}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    fold_rows = []
    pred_rows = []

    threshold_grid = [0.40, 0.45, 0.50, 0.55, 0.60]

    start_idx = int(args.train_min_quarters) + int(args.valid_quarters)
    if start_idx >= len(rb_list):
        raise RuntimeError("Not enough quarters for train/valid/test split.")

    for test_idx in range(start_idx, len(rb_list)):
        train_months = rb_list[: test_idx - int(args.valid_quarters)]
        valid_months = rb_list[test_idx - int(args.valid_quarters): test_idx]
        test_month = rb_list[test_idx]

        train_df = ds.loc[ds["rebalance_month"].isin(train_months)].copy()
        valid_df = ds.loc[ds["rebalance_month"].isin(valid_months)].copy()
        test_df = ds.loc[ds["rebalance_month"] == test_month].copy()

        if len(train_df) == 0 or len(valid_df) == 0 or len(test_df) == 0:
            continue

        X_train = train_df[feature_cols].copy()
        y_train = pd.to_numeric(train_df[args.label], errors="coerce").astype(int).values

        X_valid = valid_df[feature_cols].copy()
        y_valid = pd.to_numeric(valid_df[args.label], errors="coerce").astype(int).values

        X_test = test_df[feature_cols].copy()
        y_test = pd.to_numeric(test_df[args.label], errors="coerce").astype(int).values

        model = make_model(args.model, random_state=int(args.random_state))
        model.fit(X_train, y_train)

        valid_prob = model.predict_proba(X_valid)[:, 1]
        best_thr = select_threshold_by_valid(y_valid, valid_prob, threshold_grid)

        test_prob = model.predict_proba(X_test)[:, 1]
        cls_metrics = metric_row(y_test, test_prob, threshold=best_thr)

        # 전략 성과 비교
        strat_base = baseline_topk_return(test_df, top_k=int(args.top_k_strategy))

        test_df_strat = make_strategy_eval_subset(test_df)
        X_test_strat = test_df_strat[feature_cols].copy()
        if len(test_df_strat) > 0:
            test_prob_strat = model.predict_proba(X_test_strat)[:, 1]
        else:
            test_prob_strat = np.array([], dtype=float)

        strat_filt = filtered_topk_return(
            test_df,
            passed_prob=test_prob_strat,
            threshold=best_thr,
            top_k=int(args.top_k_strategy),
        )

        row = {
            "test_rebalance_month": str(pd.Timestamp(test_month).date()),
            "train_start": str(pd.Timestamp(train_months[0]).date()),
            "train_end": str(pd.Timestamp(train_months[-1]).date()),
            "valid_start": str(pd.Timestamp(valid_months[0]).date()),
            "valid_end": str(pd.Timestamp(valid_months[-1]).date()),
            "test_n": int(len(test_df)),
            "test_pos_rate": float(np.mean(y_test)),
            **cls_metrics,
            **strat_base,
            **strat_filt,
        }
        fold_rows.append(row)

        tmp = test_df[["ticker", "rebalance_month", "fwd_return", "excess_return", "score_raw", args.label]].copy()
        tmp["pred_prob"] = test_prob
        tmp["pred_label"] = (tmp["pred_prob"] >= best_thr).astype(int)
        tmp["threshold"] = best_thr
        pred_rows.append(tmp)

    if not fold_rows:
        raise RuntimeError("No walk-forward folds were evaluated.")

    folds = pd.DataFrame(fold_rows)
    preds = pd.concat(pred_rows, ignore_index=True)

    summary = {
        "dataset_path": str(args.dataset_path),
        "label": args.label,
        "model": args.model,
        "cohort_mode": args.cohort_mode,
        "top_n_score": int(args.top_n_score),
        "top_k_strategy": int(args.top_k_strategy),
        "feature_count": int(len(feature_cols)),
        "feature_cols": feature_cols,
        "folds": int(len(folds)),
        "mean_pr_auc": float(pd.to_numeric(folds["pr_auc"], errors="coerce").mean()),
        "mean_roc_auc": float(pd.to_numeric(folds["roc_auc"], errors="coerce").mean()),
        "mean_balanced_acc": float(pd.to_numeric(folds["balanced_acc"], errors="coerce").mean()),
        "mean_brier": float(pd.to_numeric(folds["brier"], errors="coerce").mean()),
        "mean_mcc": float(pd.to_numeric(folds["mcc"], errors="coerce").mean()),
        "baseline_avg_quarterly_ret": float(pd.to_numeric(folds["baseline_mean_ret"], errors="coerce").mean()),
        "filtered_avg_quarterly_ret": float(pd.to_numeric(folds["filtered_mean_ret"], errors="coerce").mean()),
        "baseline_avg_quarterly_excess": float(pd.to_numeric(folds["baseline_mean_excess"], errors="coerce").mean()),
        "filtered_avg_quarterly_excess": float(pd.to_numeric(folds["filtered_mean_excess"], errors="coerce").mean()),
    }

    final_model = make_model(args.model, random_state=int(args.random_state))
    X_all = ds[feature_cols].copy()
    y_all = pd.to_numeric(ds[args.label], errors="coerce").astype(int).values
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
            "label": args.label,
            "model_name": args.model,
            "cohort_mode": args.cohort_mode,
        },
        model_path,
    )
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] folds   : {folds_path}")
    print(f"[OK] preds   : {preds_path}")
    print(f"[OK] model   : {model_path}")
    print(f"[OK] summary : {summary_path}")
    print(f"[INFO] mean_pr_auc={summary['mean_pr_auc']:.6f}")
    print(f"[INFO] mean_balanced_acc={summary['mean_balanced_acc']:.6f}")
    print(f"[INFO] baseline_avg_q_ret={summary['baseline_avg_quarterly_ret']:.6f}")
    print(f"[INFO] filtered_avg_q_ret={summary['filtered_avg_quarterly_ret']:.6f}")


if __name__ == "__main__":
    main()