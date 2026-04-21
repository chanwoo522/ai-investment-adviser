#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd


def _to_num(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")


def _calc_sharpe(x: pd.Series) -> float:
    x = _to_num(x).dropna()
    if len(x) < 2:
        return np.nan
    sd = x.std(ddof=1)
    if sd == 0 or not np.isfinite(sd):
        return np.nan
    return float(x.mean() / sd)


def _calc_mdd(ret_series: pd.Series) -> float:
    x = _to_num(ret_series).fillna(0.0)
    nav = (1.0 + x).cumprod()
    peak = nav.cummax()
    dd = nav / peak - 1.0
    return float(dd.min()) if len(dd) else np.nan


def _pred_to_bucket_shares(
    pred_alpha_row: dict[str, float],
    base_abs_map: dict[str, float],
    temperature: float = 1.0,
) -> dict[str, float]:
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


def _apply_profit_accel_cap(
    dyn_share: dict[str, float],
    base_share: dict[str, float],
    cap_delta: float | None,
    target_bucket: str = "profit_accel",
) -> dict[str, float]:
    """
    Cap target_bucket share movement around baseline share by ±cap_delta.
    After capping, redistribute remaining mass proportionally to other dynamic shares.
    """
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
        # fallback to baseline proportions for others
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

    # final normalize
    s = sum(out.values())
    if s > 0:
        for k in list(out.keys()):
            out[k] = float(out[k] / s)

    return out


def _load_predictions(preds_path: Path) -> pd.DataFrame:
    if preds_path.suffix.lower() == ".csv":
        preds = pd.read_csv(preds_path)
    else:
        preds = pd.read_parquet(preds_path)

    if "rebalance_month" not in preds.columns:
        raise ValueError("predictions file must contain rebalance_month")

    preds["rebalance_month"] = pd.to_datetime(preds["rebalance_month"], errors="coerce")
    preds = preds.dropna(subset=["rebalance_month"]).copy()
    preds = preds.sort_values("rebalance_month").reset_index(drop=True)
    return preds


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset_path", required=True)
    ap.add_argument("--model_path", required=True)
    ap.add_argument("--preds_path", required=True, help="OOS predictions csv/parquet from train_factor_weight_model.py")
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--cap_profit_accel_delta", type=float, default=-1.0, help="If >=0, clamp profit_accel share to baseline±delta")
    ap.add_argument("--out_dir", default="")
    args = ap.parse_args()

    ds_path = Path(args.dataset_path)
    model_path = Path(args.model_path)
    preds_path = Path(args.preds_path)

    if not ds_path.exists():
        raise FileNotFoundError(ds_path)
    if not model_path.exists():
        raise FileNotFoundError(model_path)
    if not preds_path.exists():
        raise FileNotFoundError(preds_path)

    df = pd.read_parquet(ds_path).copy()
    df["rebalance_month"] = pd.to_datetime(df["rebalance_month"], errors="coerce")
    df = df.dropna(subset=["rebalance_month"]).sort_values("rebalance_month").reset_index(drop=True)

    model_pack = joblib.load(model_path)
    preds = _load_predictions(preds_path)

    target_cols = [c for c in df.columns if c.startswith("target__") and c.endswith("__alpha_next")]
    if not target_cols:
        raise RuntimeError("No target__*__alpha_next columns in dataset")

    buckets = [c.replace("target__", "").replace("__alpha_next", "") for c in target_cols]
    base_abs_map: dict[str, float] = model_pack.get("base_abs_bucket_weight_map", {})

    use_cols = ["rebalance_month"] + [f"pred_alpha__{b}" for b in buckets if f"pred_alpha__{b}" in preds.columns]
    preds2 = preds[use_cols].copy()

    bt = df.merge(preds2, on="rebalance_month", how="inner")
    bt = bt.sort_values("rebalance_month").reset_index(drop=True)

    if len(bt) == 0:
        raise RuntimeError("No overlapping rebalance_month between dataset and predictions")

    rows: list[dict[str, Any]] = []

    for _, row in bt.iterrows():
        baseline_ret = float(row["baseline_ret_next"]) if pd.notna(row["baseline_ret_next"]) else np.nan

        pred_alpha = {}
        true_alpha = {}
        true_bucket_ret = {}

        for b in buckets:
            pred_alpha[b] = float(row.get(f"pred_alpha__{b}", np.nan))
            true_alpha[b] = float(row.get(f"target__{b}__alpha_next", np.nan))
            true_bucket_ret[b] = float(row.get(f"target__{b}__ret_next", np.nan))

        dyn_share_uncapped = _pred_to_bucket_shares(
            pred_alpha_row=pred_alpha,
            base_abs_map=base_abs_map,
            temperature=float(args.temperature),
        )

        base_share = _pred_to_bucket_shares(
            pred_alpha_row={b: np.nan for b in buckets},
            base_abs_map=base_abs_map,
            temperature=1.0,
        )

        cap_val = None if float(args.cap_profit_accel_delta) < 0 else float(args.cap_profit_accel_delta)
        dyn_share = _apply_profit_accel_cap(
            dyn_share=dyn_share_uncapped,
            base_share=base_share,
            cap_delta=cap_val,
            target_bucket="profit_accel",
        )

        adjusted_ret = 0.0
        base_bucket_mix_ret = 0.0
        contrib_diff_sum = 0.0

        out_row: dict[str, Any] = {
            "rebalance_month": row["rebalance_month"],
            "baseline_ret_next": baseline_ret,
        }

        for b in buckets:
            realized_bucket_ret = true_bucket_ret[b]
            wb = float(base_share.get(b, np.nan))
            wu = float(dyn_share_uncapped.get(b, np.nan))
            wd = float(dyn_share.get(b, np.nan))

            out_row[f"base_share__{b}"] = wb
            out_row[f"dyn_share_uncapped__{b}"] = wu
            out_row[f"dyn_share__{b}"] = wd
            out_row[f"pred_alpha__{b}"] = pred_alpha[b]
            out_row[f"true_alpha__{b}"] = true_alpha[b]
            out_row[f"bucket_ret__{b}"] = realized_bucket_ret

            if np.isfinite(realized_bucket_ret):
                if np.isfinite(wd):
                    adjusted_ret += wd * realized_bucket_ret
                if np.isfinite(wb):
                    base_bucket_mix_ret += wb * realized_bucket_ret

            if np.isfinite(realized_bucket_ret) and np.isfinite(wd) and np.isfinite(wb):
                contrib = (wd - wb) * realized_bucket_ret
                out_row[f"contrib_diff__{b}"] = contrib
                contrib_diff_sum += contrib
            else:
                out_row[f"contrib_diff__{b}"] = np.nan

        out_row["adjusted_ret_next"] = adjusted_ret if np.isfinite(adjusted_ret) else np.nan
        out_row["base_bucket_mix_ret"] = base_bucket_mix_ret if np.isfinite(base_bucket_mix_ret) else np.nan
        out_row["ret_diff_vs_baseline"] = (
            out_row["adjusted_ret_next"] - baseline_ret
            if pd.notna(out_row["adjusted_ret_next"]) and pd.notna(baseline_ret)
            else np.nan
        )
        out_row["ret_diff_vs_base_bucket_mix"] = (
            out_row["adjusted_ret_next"] - out_row["base_bucket_mix_ret"]
            if pd.notna(out_row["adjusted_ret_next"]) and pd.notna(out_row["base_bucket_mix_ret"])
            else np.nan
        )
        out_row["sum_contrib_diff"] = contrib_diff_sum

        rows.append(out_row)

    detail = pd.DataFrame(rows).sort_values("rebalance_month").reset_index(drop=True)

    baseline = _to_num(detail["baseline_ret_next"])
    adjusted = _to_num(detail["adjusted_ret_next"])
    diff = adjusted - baseline

    summary = {
        "dataset_path": str(ds_path),
        "model_path": str(model_path),
        "preds_path": str(preds_path),
        "temperature": float(args.temperature),
        "cap_profit_accel_delta": None if float(args.cap_profit_accel_delta) < 0 else float(args.cap_profit_accel_delta),
        "periods": int(len(detail)),
        "baseline_avg_q_ret": float(baseline.mean()),
        "adjusted_avg_q_ret": float(adjusted.mean()),
        "return_diff": float(diff.mean()),
        "baseline_sharpe": _calc_sharpe(baseline),
        "adjusted_sharpe": _calc_sharpe(adjusted),
        "baseline_mdd": _calc_mdd(baseline),
        "adjusted_mdd": _calc_mdd(adjusted),
        "win_rate_adjusted_gt_baseline": float((diff > 0).mean()),
        "positive_diff_periods": int((diff > 0).sum()),
        "negative_diff_periods": int((diff < 0).sum()),
    }

    bucket_contrib = {}
    for c in [x for x in detail.columns if x.startswith("contrib_diff__")]:
        bucket = c.replace("contrib_diff__", "")
        bucket_contrib[bucket] = float(_to_num(detail[c]).mean())
    summary["bucket_mean_contrib_diff"] = bucket_contrib

    rank_df = detail[["rebalance_month", "baseline_ret_next", "adjusted_ret_next", "ret_diff_vs_baseline"]].copy()
    rank_df = rank_df.sort_values("ret_diff_vs_baseline", ascending=False).reset_index(drop=True)
    summary["best_5_periods"] = [
        {
            "rebalance_month": str(pd.Timestamp(r["rebalance_month"]).date()),
            "ret_diff_vs_baseline": float(r["ret_diff_vs_baseline"]),
            "baseline_ret_next": float(r["baseline_ret_next"]),
            "adjusted_ret_next": float(r["adjusted_ret_next"]),
        }
        for _, r in rank_df.head(5).iterrows()
    ]
    summary["worst_5_periods"] = [
        {
            "rebalance_month": str(pd.Timestamp(r["rebalance_month"]).date()),
            "ret_diff_vs_baseline": float(r["ret_diff_vs_baseline"]),
            "baseline_ret_next": float(r["baseline_ret_next"]),
            "adjusted_ret_next": float(r["adjusted_ret_next"]),
        }
        for _, r in rank_df.tail(5).sort_values("ret_diff_vs_baseline").iterrows()
    ]

    out_dir = Path(args.out_dir) if args.out_dir else Path(
        f"data/factor_weight_ml/reports/{ds_path.stem}__temp={args.temperature:.2f}__capPA={args.cap_profit_accel_delta:.2f}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    detail_path = out_dir / "backtest_detail.csv"
    summary_path = out_dir / "summary.json"

    detail.to_csv(detail_path, index=False, encoding="utf-8-sig")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print("========== RESULT ==========")
    print(f"periods={summary['periods']}")
    print("")
    print("[RETURN]")
    print(f"baseline_avg_q_ret={summary['baseline_avg_q_ret']:.6f}")
    print(f"adjusted_avg_q_ret={summary['adjusted_avg_q_ret']:.6f}")
    print("")
    print("[SHARPE]")
    print(f"baseline_sharpe={summary['baseline_sharpe']:.4f}")
    print(f"adjusted_sharpe={summary['adjusted_sharpe']:.4f}")
    print("")
    print("[MDD]")
    print(f"baseline_mdd={summary['baseline_mdd']:.6f}")
    print(f"adjusted_mdd={summary['adjusted_mdd']:.6f}")
    print("")
    print("[DIFF]")
    print(f"return_diff={summary['return_diff']:.6f}")
    print(f"win_rate_adjusted_gt_baseline={summary['win_rate_adjusted_gt_baseline']:.4f}")
    print("")
    print("[BUCKET CONTRIB]")
    for k, v in summary["bucket_mean_contrib_diff"].items():
        print(f"{k}={v:.6f}")
    print("")
    print(f"[OK] detail   : {detail_path}")
    print(f"[OK] summary  : {summary_path}")


if __name__ == "__main__":
    main()