#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from sklearn.impute import SimpleImputer
from sklearn.linear_model import ElasticNet, Ridge
from sklearn.ensemble import RandomForestRegressor
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler


# ---------------------------------------------------------
# Helpers
# ---------------------------------------------------------

def _ensure_dir(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


def _to_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _safe_corr(a: pd.Series, b: pd.Series) -> float:
    x = _to_num(a)
    y = _to_num(b)
    mask = x.notna() & y.notna()
    if mask.sum() < 3:
        return np.nan
    xv = x.loc[mask].astype(float)
    yv = y.loc[mask].astype(float)
    if xv.std(ddof=1) == 0 or yv.std(ddof=1) == 0:
        return np.nan
    return float(xv.corr(yv))


def _safe_mae(y_true: pd.Series, y_pred: pd.Series) -> float:
    x = _to_num(y_true)
    y = _to_num(y_pred)
    mask = x.notna() & y.notna()
    if mask.sum() == 0:
        return np.nan
    return float((x.loc[mask] - y.loc[mask]).abs().mean())


def _safe_rmse(y_true: pd.Series, y_pred: pd.Series) -> float:
    x = _to_num(y_true)
    y = _to_num(y_pred)
    mask = x.notna() & y.notna()
    if mask.sum() == 0:
        return np.nan
    return float(np.sqrt(((x.loc[mask] - y.loc[mask]) ** 2).mean()))


def _sign_hit_rate(y_true: pd.Series, y_pred: pd.Series) -> float:
    x = _to_num(y_true)
    y = _to_num(y_pred)
    mask = x.notna() & y.notna()
    if mask.sum() == 0:
        return np.nan
    sx = np.sign(x.loc[mask].astype(float))
    sy = np.sign(y.loc[mask].astype(float))
    return float((sx == sy).mean())


def _extract_target_cols(df: pd.DataFrame) -> list[str]:
    cols = [c for c in df.columns if c.startswith("target__") and c.endswith("__alpha_next")]
    return sorted(cols)


def _extract_bucket_name(target_col: str) -> str:
    x = target_col.replace("target__", "")
    x = x.replace("__alpha_next", "")
    return x


def _build_model(model_name: str, random_state: int) -> Pipeline:
    if model_name == "ridge":
        return Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("reg", Ridge(alpha=1.0, random_state=random_state)),
            ]
        )

    if model_name == "elasticnet":
        return Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("scaler", StandardScaler()),
                ("reg", ElasticNet(alpha=0.05, l1_ratio=0.2, random_state=random_state, max_iter=10000)),
            ]
        )

    if model_name == "random_forest":
        return Pipeline(
            steps=[
                ("imputer", SimpleImputer(strategy="median")),
                ("reg", RandomForestRegressor(
                    n_estimators=300,
                    max_depth=3,
                    min_samples_leaf=4,
                    random_state=random_state,
                    n_jobs=-1,
                )),
            ]
        )

    raise ValueError(f"unsupported model_name: {model_name}")


def _bucket_base_abs_weight_map(meta: dict[str, Any]) -> dict[str, float]:
    out: dict[str, float] = {}
    active = meta.get("active_buckets", {}) if isinstance(meta, dict) else {}
    if not isinstance(active, dict):
        return out

    for bucket, fmap in active.items():
        if not isinstance(fmap, dict):
            continue
        s = 0.0
        for _, w in fmap.items():
            try:
                s += abs(float(w))
            except Exception:
                pass
        out[str(bucket)] = float(s)
    return out


def _pred_to_bucket_shares(
    pred_alpha_row: dict[str, float],
    base_abs_map: dict[str, float],
    temperature: float = 1.0,
) -> dict[str, float]:
    buckets = list(base_abs_map.keys())
    if not buckets:
        return {}

    base = np.array([max(base_abs_map.get(b, 0.0), 1e-8) for b in buckets], dtype=float)
    pred = np.array([float(pred_alpha_row.get(b, np.nan)) for b in buckets], dtype=float)

    if np.isnan(pred).all():
        shares = base / base.sum()
        return {b: float(w) for b, w in zip(buckets, shares)}

    pred_ser = pd.Series(pred)
    mu = pred_ser.mean(skipna=True)
    sd = pred_ser.std(skipna=True)
    if not np.isfinite(sd) or sd == 0:
        z = np.zeros(len(pred), dtype=float)
    else:
        z = ((pred - mu) / sd)
        z = np.where(np.isfinite(z), z, 0.0)

    z = np.clip(z, -2.0, 2.0)
    tilt = np.exp(z / max(float(temperature), 1e-8))
    raw = base * tilt
    shares = raw / raw.sum()
    return {b: float(w) for b, w in zip(buckets, shares)}


# ---------------------------------------------------------
# Feature selection profiles
# ---------------------------------------------------------

def _select_feature_cols_auto(df: pd.DataFrame) -> list[str]:
    """
    Conservative auto policy:
      - context__*
      - benchmark_*
      - lag1__*
      - lag2__*
    """
    feat_cols: list[str] = []
    for c in df.columns:
        if c.startswith("context__"):
            feat_cols.append(c)
        elif c.startswith("benchmark_"):
            feat_cols.append(c)
        elif c.startswith("lag1__"):
            feat_cols.append(c)
        elif c.startswith("lag2__"):
            feat_cols.append(c)

    feat_cols = [c for c in feat_cols if c in df.columns]
    feat_cols = [c for c in feat_cols if pd.api.types.is_numeric_dtype(df[c])]
    return sorted(set(feat_cols))


def _select_feature_cols_compact(df: pd.DataFrame) -> list[str]:
    candidates = [
        "context__profit_accel__mean",
        "context__profit_accel__spread_p90_p10",
        "context__revenue_support__mean",
        "context__revenue_support__spread_p90_p10",
        "context__balance_sheet__mean",
        "context__balance_sheet__spread_p90_p10",
        "benchmark_ret_20d",
        "benchmark_ret_60d",
        "benchmark_vol_20d",
        "benchmark_mdd_60d",
        "lag1__profit_accel__alpha",
        "lag1__revenue_support__alpha",
        "lag1__balance_sheet__alpha",
        "lag1__baseline_ret",
    ]
    return [c for c in candidates if c in df.columns and pd.api.types.is_numeric_dtype(df[c])]


def _select_feature_cols_compact_dailyagg(df: pd.DataFrame) -> list[str]:
    """
    Small-sample friendly profile.
    Keep:
      - bucket context means/spreads
      - lag1 only
      - daily aggregated universe/bucket response
    Exclude:
      - lag2
      - duplicated benchmark dailyagg vs benchmark_* pairs
      - overly wide raw context set
    """
    candidates = [
        # bucket context
        "context__profit_accel__mean",
        "context__profit_accel__spread_p90_p10",
        "context__revenue_support__mean",
        "context__revenue_support__spread_p90_p10",
        "context__balance_sheet__mean",
        "context__balance_sheet__spread_p90_p10",

        # lag1 only
        "lag1__profit_accel__alpha",
        "lag1__revenue_support__alpha",
        "lag1__balance_sheet__alpha",
        "lag1__baseline_ret",

        # daily aggregated universe
        "dailyagg__universe__ret_20d_mean",
        "dailyagg__universe__vol_20d_mean",
        "dailyagg__universe__tv_mean_20d_mean",

        # daily aggregated bucket response
        "dailyagg__profit_accel__ret_20d_mean",
        "dailyagg__revenue_support__ret_20d_mean",
        "dailyagg__balance_sheet__ret_20d_mean",
    ]
    return [c for c in candidates if c in df.columns and pd.api.types.is_numeric_dtype(df[c])]


def _select_feature_cols(df: pd.DataFrame, feature_profile: str) -> list[str]:
    if feature_profile == "auto":
        cols = _select_feature_cols_auto(df)
    elif feature_profile == "compact":
        cols = _select_feature_cols_compact(df)
    elif feature_profile == "compact_dailyagg":
        cols = _select_feature_cols_compact_dailyagg(df)
    else:
        raise ValueError(f"unsupported feature_profile: {feature_profile}")

    if not cols:
        raise RuntimeError(f"No usable feature columns found for feature_profile={feature_profile}")
    return cols


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset_path", required=True)
    ap.add_argument("--meta_path", default="", help="Optional explicit meta json path. Default: dataset_path.with_suffix('.meta.json')")
    ap.add_argument("--model", default="ridge", choices=["ridge", "elasticnet", "random_forest"])
    ap.add_argument("--feature_profile", default="auto", choices=["auto", "compact", "compact_dailyagg"])
    ap.add_argument("--min_train_rows", type=int, default=20)
    ap.add_argument("--valid_rows", type=int, default=4)
    ap.add_argument("--random_state", type=int, default=42)
    ap.add_argument("--temperature", type=float, default=1.0, help="Used only for predicted bucket share transform")
    ap.add_argument("--out_dir", default="")
    args = ap.parse_args()

    ds_path = Path(args.dataset_path)
    if not ds_path.exists():
        raise FileNotFoundError(ds_path)

    if args.meta_path:
        meta_path = Path(args.meta_path)
    else:
        meta_path = ds_path.with_suffix(".meta.json")

    if not meta_path.exists():
        raise FileNotFoundError(f"meta json not found: {meta_path}")

    df = pd.read_parquet(ds_path).copy()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))

    if "rebalance_month" not in df.columns:
        raise ValueError("dataset must contain rebalance_month")

    df["rebalance_month"] = pd.to_datetime(df["rebalance_month"], errors="coerce")
    df = df.dropna(subset=["rebalance_month"]).sort_values("rebalance_month").reset_index(drop=True)

    target_cols = _extract_target_cols(df)
    if not target_cols:
        raise RuntimeError("No target__*__alpha_next columns found in dataset")

    feature_cols = _select_feature_cols(df, feature_profile=args.feature_profile)
    base_abs_map = _bucket_base_abs_weight_map(meta)
    bucket_names = [_extract_bucket_name(c) for c in target_cols]

    n_rows = len(df)
    start_idx = int(args.min_train_rows) + int(args.valid_rows)
    if n_rows <= start_idx:
        raise RuntimeError(
            f"Not enough rows for walk-forward. rows={n_rows}, "
            f"need > min_train_rows+valid_rows={start_idx}"
        )

    fold_rows: list[dict[str, Any]] = []
    pred_rows: list[dict[str, Any]] = []

    for test_idx in range(start_idx, n_rows):
        train_df = df.iloc[: test_idx - int(args.valid_rows)].copy()
        valid_df = df.iloc[test_idx - int(args.valid_rows): test_idx].copy()
        test_df = df.iloc[[test_idx]].copy()

        fold_rec: dict[str, Any] = {
            "test_rebalance_month": str(pd.Timestamp(test_df["rebalance_month"].iloc[0]).date()),
            "train_rows": int(len(train_df)),
            "valid_rows": int(len(valid_df)),
            "test_rows": int(len(test_df)),
        }

        test_pred_alpha: dict[str, float] = {}

        for tgt in target_cols:
            bucket = _extract_bucket_name(tgt)

            y_train = _to_num(train_df[tgt])
            X_train = train_df[feature_cols].copy()

            mask_train = y_train.notna()
            X_train = X_train.loc[mask_train].copy()
            y_train = y_train.loc[mask_train].copy()

            if len(X_train) < max(8, int(args.min_train_rows) // 2):
                pred_test = float(y_train.mean()) if len(y_train) > 0 else 0.0
                pred_valid = pd.Series(pred_test, index=valid_df.index, dtype="float64")
            else:
                model = _build_model(args.model, random_state=int(args.random_state))
                model.fit(X_train, y_train)

                pred_valid = pd.Series(
                    model.predict(valid_df[feature_cols].copy()),
                    index=valid_df.index,
                    dtype="float64",
                )
                pred_test = float(model.predict(test_df[feature_cols].copy())[0])

            fold_rec[f"{bucket}__valid_corr"] = _safe_corr(valid_df[tgt], pred_valid)
            fold_rec[f"{bucket}__valid_mae"] = _safe_mae(valid_df[tgt], pred_valid)
            fold_rec[f"{bucket}__valid_rmse"] = _safe_rmse(valid_df[tgt], pred_valid)
            fold_rec[f"{bucket}__valid_sign_hit"] = _sign_hit_rate(valid_df[tgt], pred_valid)

            true_test = _to_num(test_df[tgt]).iloc[0]
            fold_rec[f"{bucket}__test_true"] = float(true_test) if pd.notna(true_test) else np.nan
            fold_rec[f"{bucket}__test_pred"] = float(pred_test)

            test_pred_alpha[bucket] = float(pred_test)

        pred_shares = _pred_to_bucket_shares(
            pred_alpha_row=test_pred_alpha,
            base_abs_map=base_abs_map,
            temperature=float(args.temperature),
        )

        rec = {
            "rebalance_month": test_df["rebalance_month"].iloc[0],
        }
        for tgt in target_cols:
            bucket = _extract_bucket_name(tgt)
            true_val = _to_num(test_df[tgt]).iloc[0]
            rec[f"true_alpha__{bucket}"] = float(true_val) if pd.notna(true_val) else np.nan
            rec[f"pred_alpha__{bucket}"] = float(test_pred_alpha.get(bucket, np.nan))
            rec[f"pred_share__{bucket}"] = float(pred_shares.get(bucket, np.nan))
        pred_rows.append(rec)
        fold_rows.append(fold_rec)

    folds = pd.DataFrame(fold_rows)
    preds = pd.DataFrame(pred_rows).sort_values("rebalance_month").reset_index(drop=True)

    # final models on full sample
    final_models: dict[str, Any] = {}
    for tgt in target_cols:
        bucket = _extract_bucket_name(tgt)

        y = _to_num(df[tgt])
        X = df[feature_cols].copy()
        mask = y.notna()

        X = X.loc[mask].copy()
        y = y.loc[mask].copy()

        if len(X) == 0:
            final_models[bucket] = {
                "fallback_mean": 0.0,
                "model": None,
            }
            continue

        if len(X) < max(8, int(args.min_train_rows) // 2):
            final_models[bucket] = {
                "fallback_mean": float(y.mean()),
                "model": None,
            }
            continue

        mdl = _build_model(args.model, random_state=int(args.random_state))
        mdl.fit(X, y)
        final_models[bucket] = {
            "fallback_mean": float(y.mean()),
            "model": mdl,
        }

    summary: dict[str, Any] = {
        "dataset_path": str(ds_path),
        "meta_path": str(meta_path),
        "model_type": args.model,
        "feature_profile": args.feature_profile,
        "rows": int(len(df)),
        "feature_count": int(len(feature_cols)),
        "feature_cols": feature_cols,
        "target_cols": target_cols,
        "bucket_names": bucket_names,
        "base_abs_bucket_weight_map": base_abs_map,
        "walkforward_folds": int(len(folds)),
    }

    for tgt in target_cols:
        bucket = _extract_bucket_name(tgt)
        summary[f"{bucket}__mean_valid_corr"] = float(_to_num(folds[f"{bucket}__valid_corr"]).mean())
        summary[f"{bucket}__mean_valid_mae"] = float(_to_num(folds[f"{bucket}__valid_mae"]).mean())
        summary[f"{bucket}__mean_valid_rmse"] = float(_to_num(folds[f"{bucket}__valid_rmse"]).mean())
        summary[f"{bucket}__mean_valid_sign_hit"] = float(_to_num(folds[f"{bucket}__valid_sign_hit"]).mean())

        if f"true_alpha__{bucket}" in preds.columns and f"pred_alpha__{bucket}" in preds.columns:
            summary[f"{bucket}__oos_corr"] = _safe_corr(
                preds[f"true_alpha__{bucket}"], preds[f"pred_alpha__{bucket}"]
            )
            summary[f"{bucket}__oos_mae"] = _safe_mae(
                preds[f"true_alpha__{bucket}"], preds[f"pred_alpha__{bucket}"]
            )
            summary[f"{bucket}__oos_sign_hit"] = _sign_hit_rate(
                preds[f"true_alpha__{bucket}"], preds[f"pred_alpha__{bucket}"]
            )

    out_dir = Path(args.out_dir) if args.out_dir else Path(
        f"data/factor_weight_ml/models/{ds_path.stem}__{args.model}__{args.feature_profile}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    folds_path = out_dir / "fold_metrics.csv"
    preds_path = out_dir / "oos_predictions.csv"
    model_path = out_dir / "factor_weight_model.joblib"
    summary_path = out_dir / "summary.json"

    folds.to_csv(folds_path, index=False, encoding="utf-8-sig")
    preds.to_csv(preds_path, index=False, encoding="utf-8-sig")
    joblib.dump(
        {
            "model_type": args.model,
            "feature_profile": args.feature_profile,
            "feature_cols": feature_cols,
            "target_cols": target_cols,
            "bucket_names": bucket_names,
            "base_abs_bucket_weight_map": base_abs_map,
            "temperature": float(args.temperature),
            "models": final_models,
            "dataset_path": str(ds_path),
            "meta_path": str(meta_path),
        },
        model_path,
    )
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] folds   : {folds_path}")
    print(f"[OK] preds   : {preds_path}")
    print(f"[OK] model   : {model_path}")
    print(f"[OK] summary : {summary_path}")
    print(f"[INFO] rows={summary['rows']} features={summary['feature_count']} folds={summary['walkforward_folds']}")
    for bucket in bucket_names:
        oos_corr = summary.get(f"{bucket}__oos_corr", np.nan)
        oos_mae = summary.get(f"{bucket}__oos_mae", np.nan)
        print(f"[INFO] {bucket}: oos_corr={oos_corr:.6f} oos_mae={oos_mae:.6f}")


if __name__ == "__main__":
    main()