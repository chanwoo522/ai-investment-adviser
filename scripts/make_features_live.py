#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd

# ---- path bootstrap (schema import 안정화) ----
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# ---------------------------------------------

from schema import TICKER_COL, NAME_COL, FEATURES_LIVE_REQUIRED_COLS

ROOT = Path(__file__).resolve().parents[1]
PRO = ROOT / "data" / "processed"


def _pick_factors_path(asof: str, metric: str, v: int) -> Path:
    """Prefer exact match; otherwise pick latest factors_ttm_acc2 with asof <= requested (avoid lookahead)."""
    want = PRO / f"factors_ttm_acc2__asof={asof}__metric={metric}__v={v}.parquet"
    if want.exists():
        return want

    candidates = sorted(PRO.glob(f"factors_ttm_acc2__asof=????-??-??__metric={metric}__v={v}.parquet"))
    le: list[tuple[str, Path]] = []
    for p in candidates:
        a = p.name.split("__asof=")[1].split("__metric=")[0]
        if a <= asof:
            le.append((a, p))
    if not le:
        raise FileNotFoundError(
            f"No factors_ttm_acc2 found with asof <= {asof} (metric={metric}, v={v}). "
            f"Run build_factors_ttm_acc2.py first."
        )
    le.sort(key=lambda x: x[0])
    picked = le[-1][1]
    print(f"[WARN] No exact factors for asof={asof}. Using latest <= asof: {picked.name}")
    return picked


def _require_cols(df: pd.DataFrame, cols: list[str]) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"Required columns missing: {missing}\nHave: {list(df.columns)}")


def _to_float(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").astype("float64")


def _add_derived_columns(df: pd.DataFrame, cfo_col: str) -> dict:
    """Add columns needed by strategies/backtest without breaking older snapshots."""
    notes = {}

    # (1) CFO missing mask + warn + safe (policy)
    if cfo_col in df.columns:
        cfo_raw = pd.to_numeric(df[cfo_col], errors="coerce").astype("float64")

        df["CFO_isnull"] = cfo_raw.isna().astype("int8")

        # 경고: 전기 대비 급변 (ticker 기준)
        prev = df.groupby(TICKER_COL)[cfo_col].shift(1)
        prev = pd.to_numeric(prev, errors="coerce").astype("float64")

        jump = (cfo_raw - prev).abs()
        df["CFO_warn"] = (
            (cfo_raw.notna()) & (prev.notna()) & (jump > 5.0 * (prev.abs() + 1e-9))
        ).astype("int8")

        df["CFO_safe"] = cfo_raw.fillna(0.0)

    else:
        df["CFO_isnull"] = 1
        df["CFO_warn"] = 0
        df["CFO_safe"] = 0.0
        notes["CFO_masking"] = f"cfo_col '{cfo_col}' not found -> CFO_isnull=1, CFO_warn=0, CFO_safe=0"

    # (2) Debt_to_Equity_log expected by strategies.yaml
    if "Debt_to_Equity_log" not in df.columns:
        if "Debt_to_Equity" in df.columns:
            x = pd.to_numeric(df["Debt_to_Equity"], errors="coerce")
            x = x.where(x > 0)
            df["Debt_to_Equity_log"] = np.log1p(x)
        else:
            notes["Debt_to_Equity_log"] = "Neither Debt_to_Equity_log nor Debt_to_Equity exists"

    # (3) Quality_CFO_to_Assets expected by strategies.yaml
    if "Quality_CFO_to_Assets" not in df.columns:
        if "CFO_to_Assets_ttm" in df.columns:
            df["Quality_CFO_to_Assets"] = _to_float(df["CFO_to_Assets_ttm"])
        elif cfo_col in df.columns and "Assets_ttm" in df.columns:
            cfo = _to_float(df[cfo_col])
            assets = _to_float(df["Assets_ttm"])
            df["Quality_CFO_to_Assets"] = cfo / assets.replace(0.0, np.nan)
            notes["Quality_CFO_to_Assets"] = "derived from CFO_ttm / Assets_ttm"
        else:
            notes["Quality_CFO_to_Assets"] = "Cannot derive (need CFO_to_Assets_ttm or CFO_ttm+Assets_ttm)"

    return notes


def _print_null_ratios(df: pd.DataFrame, cols: list[str]) -> dict:
    present = [c for c in cols if c in df.columns]
    ratios = {c: float(df[c].isna().mean()) for c in present}
    print("[CHECK] null ratios (present cols):")
    for c in present:
        print(f"  - {c}: {ratios[c]:.3f}")
    return ratios


def _save_meta(meta_path: Path, meta: dict) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _try_attach_name(df: pd.DataFrame, asof: str, master_src: str, master_v: int) -> tuple[pd.DataFrame, dict]:
    """
    name 컬럼이 없을 때만 krx_master를 붙인다.
    - 파일이 없으면 조용히 넘어가고 notes에만 남긴다. (기능 축소/실패 유발 X)
    """
    notes = {}
    if NAME_COL in df.columns:
        return df, notes

    p_master = PRO / f"krx_master__asof={asof}__src={master_src}__v={master_v}.parquet"
    if not p_master.exists():
        notes["name_attach"] = f"krx_master not found -> skip attach ({p_master.name})"
        return df, notes

    m = pd.read_parquet(p_master)
    if TICKER_COL not in m.columns or NAME_COL not in m.columns:
        notes["name_attach"] = f"krx_master missing cols -> skip attach (have={list(m.columns)})"
        return df, notes

    m = m[[TICKER_COL, NAME_COL]].drop_duplicates(TICKER_COL)
    m[TICKER_COL] = m[TICKER_COL].astype(str)

    out = df.merge(m, on=TICKER_COL, how="left")
    notes["name_attach"] = f"attached from {p_master.name}"
    return out, notes


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--in_v", type=int, default=2)
    ap.add_argument("--out_v", type=int, default=2)
    ap.add_argument("--cfo_col", default="CFO_ttm", help="CFO column name (default: CFO_ttm).")
    ap.add_argument("--save_meta", action="store_true")

    # name 보강 옵션(기본은 "있으면 붙이고, 없으면 스킵")
    ap.add_argument("--master_src", default="pykrx")
    ap.add_argument("--master_v", type=int, default=1)

    args = ap.parse_args()

    p_in = _pick_factors_path(args.asof, args.metric, args.in_v)
    df = pd.read_parquet(p_in)

    # Backtest compatibility
    _require_cols(df, FEATURES_LIVE_REQUIRED_COLS)
    df = df.copy()
    df[TICKER_COL] = df[TICKER_COL].astype(str)
    df["year"] = df["year"].astype(int)
    df["quarter"] = df["quarter"].astype(int)

    notes = _add_derived_columns(df, args.cfo_col)

    # name 컬럼 보강(있을 때만)
    df, notes2 = _try_attach_name(df, args.asof, args.master_src, args.master_v)
    notes.update(notes2)

    print(f"[INFO] rows: {len(df)}")
    print(f"[INFO] tickers: {df[TICKER_COL].nunique()}")

    nulls = _print_null_ratios(
        df,
        cols=[args.cfo_col, "CFO_isnull", "Quality_CFO_to_Assets", "Debt_to_Equity_log",
              "Revenue_ttm_yoy", "OpIncome_ttm_yoy", "Revenue_acc2", "OpIncome_acc2", "CFO_warn", "CFO_safe"],
    )

    p_out = PRO / f"features_live__asof={args.asof}__metric={args.metric}__v={args.out_v}.parquet"
    p_out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p_out, index=False)

    print(f"[OK] src:   {p_in.as_posix()}")
    print(f"[OK] saved: {p_out.as_posix()}")

    if args.save_meta:
        meta = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "asof": args.asof,
            "metric": args.metric,
            "in_v": args.in_v,
            "out_v": args.out_v,
            "input_path": p_in.as_posix(),
            "output_path": p_out.as_posix(),
            "rows": int(len(df)),
            "tickers": int(df[TICKER_COL].nunique()),
            "columns": list(map(str, df.columns)),
            "null_ratios": nulls,
            "notes": notes,
        }
        meta_path = PRO / f"features_live__asof={args.asof}__metric={args.metric}__v={args.out_v}.meta.json"
        _save_meta(meta_path, meta)
        print(f"[OK] meta saved: {meta_path.as_posix()}")


if __name__ == "__main__":
    main()