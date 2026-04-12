#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import os
import sys
from pathlib import Path

_THIS_DIR = os.path.abspath(os.path.dirname(__file__))
_SCRIPTS_DIR = os.path.abspath(os.path.join(_THIS_DIR, ".."))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
_BACKTEST_DIR = os.path.abspath(os.path.join(_SCRIPTS_DIR, "backtest"))
_LIVE_DIR = os.path.abspath(os.path.join(_SCRIPTS_DIR, "live"))
_LEGACY_DIR = os.path.abspath(os.path.join(_REPO_ROOT, "legacy"))

for _p in (_LEGACY_DIR, _BACKTEST_DIR, _LIVE_DIR, _THIS_DIR, _SCRIPTS_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from schema import TICKER_COL, NAME_COL, FEATURES_LIVE_REQUIRED_COLS
from backtest_quarterly_rebalance_v2 import (
    compute_rebalance_month_from_yq,
    detect_group_col,
    load_group_map,
)

from common.industry_map import attach_industry, standardize_industry_columns

# New structured locations (preferred)
DATA = ROOT / "data"
PROCESSED = DATA / "processed"
FEATURES_DIR = DATA / "features"
INTERIM_DIR = DATA / "interim"

FACTORS_DIR = FEATURES_DIR / "factors_ttm_acc2"
FEATURES_LIVE_DIR = FEATURES_DIR / "features_live"
FUNDAMENTALS_Q_DIR = INTERIM_DIR / "fundamentals_quarterly"


def normalize_ticker_series(s: pd.Series) -> pd.Series:
    x = s.astype(str).str.extract(r"(\d+)")[0]
    return x.str.zfill(6)


def normalize_industry_code_series(s: pd.Series) -> pd.Series:
    x = s.astype(str).str.extract(r"(\d+)")[0]
    return x.str.zfill(6)


def ensure_industry_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    code_col = None
    for c in ["industry_code", "industry6", "industry6_code", "industry_code6", "industry4", "induty_code"]:
        if c in out.columns:
            code_col = c
            break
    if code_col is not None:
        out["industry_code"] = normalize_industry_code_series(out[code_col])
    elif "industry_code" not in out.columns:
        out["industry_code"] = pd.NA

    name_col = None
    for c in ["industry_name", "industry_nm", "krx_industry_name", "업종명", "sector_name"]:
        if c in out.columns:
            name_col = c
            break
    if name_col is not None and "industry_name" not in out.columns:
        out["industry_name"] = out[name_col].astype("string")
    elif "industry_name" not in out.columns:
        out["industry_name"] = pd.NA

    # legacy alias 유지 (downstream compatibility)
    if "industry4" not in out.columns:
        out["industry4"] = out["industry_code"]
    return out




def _coalesce_duplicate_columns(df: pd.DataFrame) -> pd.DataFrame:
    cols = list(df.columns)
    seen = set()
    out_data: dict[str, pd.Series] = {}
    ordered: list[str] = []
    for i, name in enumerate(cols):
        if name in seen:
            continue
        idxs = [j for j, c in enumerate(cols) if c == name]
        s = df.iloc[:, idxs[0]].copy()
        for j in idxs[1:]:
            other = df.iloc[:, j]
            try:
                s = s.combine_first(other)
            except Exception:
                s = s.where(~s.isna(), other)
        out_data[name] = s
        ordered.append(name)
        seen.add(name)
    return pd.DataFrame(out_data, columns=ordered)


def _refresh_industry_from_reference(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    try:
        try:
            out = attach_industry(out, prefer_reference=True)
        except TypeError:
            out = attach_industry(out)
    except Exception:
        pass
    try:
        out = standardize_industry_columns(out)
    except Exception:
        out = ensure_industry_columns(out)
    out = _coalesce_duplicate_columns(out)
    return out

def _extract_asof_from_name(name: str) -> str | None:
    m = re.search(r"__asof=(\d{4}-\d{2}-\d{2})__", name)
    return m.group(1) if m else None


def _extract_v_from_name(name: str) -> int | None:
    m = re.search(r"__v=(\d+)\.parquet$", name)
    return int(m.group(1)) if m else None


def _pick_factors_path(asof: str, metric: str, v: int) -> Path:
    preferred = FACTORS_DIR / f"factors_ttm_acc2__asof={asof}__metric={metric}__v={v}.parquet"
    legacy = PROCESSED / f"factors_ttm_acc2__asof={asof}__metric={metric}__v={v}.parquet"

    for p in [preferred, legacy]:
        if p.exists():
            return p

    candidates: list[Path] = []
    for base in [FACTORS_DIR, PROCESSED]:
        candidates.extend(base.glob(f"factors_ttm_acc2__asof=????-??-??__metric={metric}__v={v}.parquet"))

    le: list[tuple[str, Path]] = []
    for p in sorted(set(candidates)):
        a = _extract_asof_from_name(p.name)
        if a and a <= asof:
            le.append((a, p))

    if not le:
        raise FileNotFoundError(
            f"No factors_ttm_acc2 found with asof <= {asof} (metric={metric}, v={v}). "
            f"Looked in: {FACTORS_DIR} and {PROCESSED}"
        )

    le.sort(key=lambda x: x[0])
    picked = le[-1][1]
    print(f"[WARN] No exact factors for asof={asof}. Using latest <= asof: {picked.name}")
    return picked


def _pick_fundamentals_q_path(asof: str) -> Path:
    """
    Prefer merged CFS from structured interim dir, then legacy processed dir.
    Fallback to the latest <= asof using source priority:
      dart_merged CFS > dart CFS > dart OFS
    """
    preferred_patterns = [
        FUNDAMENTALS_Q_DIR / f"fundamentals_quarterly__asof={asof}__src=dart_merged__fs=CFS__y=*__v=*.parquet",
        FUNDAMENTALS_Q_DIR / f"fundamentals_quarterly__asof={asof}__src=dart__fs=CFS__y=*__v=*.parquet",
        FUNDAMENTALS_Q_DIR / f"fundamentals_quarterly__asof={asof}__src=dart__fs=OFS__y=*__v=*.parquet",
        PROCESSED / f"fundamentals_quarterly__asof={asof}__src=dart_merged__fs=CFS__y=*__v=*.parquet",
        PROCESSED / f"fundamentals_quarterly__asof={asof}__src=dart__fs=CFS__y=*__v=*.parquet",
        PROCESSED / f"fundamentals_quarterly__asof={asof}__src=dart__fs=OFS__y=*__v=*.parquet",
    ]
    for pat in preferred_patterns:
        hits = sorted(pat.parent.glob(pat.name))
        if hits:
            return hits[-1]

    def _priority(name: str) -> int:
        if "__src=dart_merged__fs=CFS__" in name:
            return 0
        if "__src=dart__fs=CFS__" in name:
            return 1
        if "__src=dart__fs=OFS__" in name:
            return 2
        return 9

    candidates: list[Path] = []
    for base in [FUNDAMENTALS_Q_DIR, PROCESSED]:
        candidates.extend(base.glob("fundamentals_quarterly__asof=????-??-??__src=*.parquet"))

    scored: list[tuple[str, int, str, Path]] = []
    for p in sorted(set(candidates)):
        a = _extract_asof_from_name(p.name)
        if a and a <= asof:
            scored.append((a, -_priority(p.name), p.name, p))

    if not scored:
        raise FileNotFoundError(
            f"No fundamentals_quarterly found with asof <= {asof}. "
            f"Looked in: {FUNDAMENTALS_Q_DIR} and {PROCESSED}"
        )

    scored.sort(key=lambda x: (x[0], x[1], x[2]))
    picked = scored[-1][3]
    print(f"[WARN] No exact fundamentals for asof={asof}. Using latest <= asof: {picked.name}")
    return picked


def _require_cols(df: pd.DataFrame, cols: list[str]) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"Required columns missing: {missing}\nHave: {list(df.columns)}")


def _to_float(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce").astype("float64")



def _build_factor_rows_from_fundamentals(p_fq: Path, missing_tickers: list[str], template_cols: list[str]) -> pd.DataFrame:
    fq = pd.read_parquet(p_fq).copy()
    if TICKER_COL not in fq.columns or "year" not in fq.columns or "quarter" not in fq.columns:
        return pd.DataFrame(columns=template_cols)

    fq[TICKER_COL] = normalize_ticker_series(fq[TICKER_COL])
    fq = fq[fq[TICKER_COL].isin(missing_tickers)].copy()
    if len(fq) == 0:
        return pd.DataFrame(columns=template_cols)

    fq["year"] = pd.to_numeric(fq["year"], errors="coerce").astype("Int64")
    fq["quarter"] = pd.to_numeric(fq["quarter"], errors="coerce").astype("Int64")
    fq = fq.dropna(subset=[TICKER_COL, "year", "quarter"]).copy()
    fq["year"] = fq["year"].astype(int)
    fq["quarter"] = fq["quarter"].astype(int)
    fq = fq.sort_values([TICKER_COL, "year", "quarter"]).drop_duplicates([TICKER_COL, "year", "quarter"], keep="last").copy()

    out = pd.DataFrame(index=fq.index)
    out[TICKER_COL] = fq[TICKER_COL]
    out["year"] = fq["year"]
    out["quarter"] = fq["quarter"]
    for c in [NAME_COL, "corp_code", "fs_div", "fs_div_used", "Assets", "Equity", "Liabilities"]:
        if c in fq.columns:
            out[c] = fq[c]

    def _rolling4(series: pd.Series) -> pd.Series:
        return pd.to_numeric(series, errors="coerce").rolling(4, min_periods=4).sum()

    for src, prefix in [("Revenue", "Revenue"), ("OpIncome", "OpIncome"), ("NetIncome", "NetIncome"), ("CFO", "CFO"), ("CAPEX", "CAPEX")]:
        if src not in fq.columns:
            continue
        vals = pd.to_numeric(fq[src], errors="coerce")
        out[src] = vals
        ttm = vals.groupby(fq[TICKER_COL]).transform(_rolling4)
        prev_ttm = ttm.groupby(fq[TICKER_COL]).shift(4)
        yoy = np.where(prev_ttm.abs() > 1e-12, ttm / prev_ttm - 1.0, np.nan)
        yoy = pd.Series(yoy, index=fq.index, dtype="float64")
        yoy_prev = yoy.groupby(fq[TICKER_COL]).shift(4)
        acc2 = yoy - yoy_prev

        out[f"{prefix}_ttm"] = ttm.astype("float64")
        out[f"{prefix}_ttm_yoy"] = yoy.astype("float64")
        out[f"{prefix}_acc2"] = acc2.astype("float64")
        out[f"{prefix}_ttm_ready"] = ttm.notna().astype("int8")
        out[f"{prefix}_ttm_yoy_ready"] = yoy.notna().astype("int8")
        out[f"{prefix}_acc2_ready"] = acc2.notna().astype("int8")
        out[f"{prefix}_acc2_missing_reason"] = pd.NA

    if "Assets" in out.columns:
        out["Assets_ttm"] = pd.to_numeric(out["Assets"], errors="coerce")
    if {"Liabilities", "Equity"}.issubset(out.columns):
        eq = pd.to_numeric(out["Equity"], errors="coerce")
        liab = pd.to_numeric(out["Liabilities"], errors="coerce")
        dte = liab / eq.replace(0.0, np.nan)
        out["Debt_to_Equity"] = dte.astype("float64")
        out["Debt_to_Equity_log"] = np.sign(dte) * np.log1p(np.abs(dte))
    if {"CFO_ttm", "Assets_ttm"}.issubset(out.columns):
        out["CFO_to_Assets_ttm"] = pd.to_numeric(out["CFO_ttm"], errors="coerce") / pd.to_numeric(out["Assets_ttm"], errors="coerce").replace(0.0, np.nan)
        out["Quality_CFO_to_Assets"] = out["CFO_to_Assets_ttm"]

    for src_col in ["OpIncome_acc2", "Revenue_acc2"]:
        log_col = f"{src_col}_log1p"
        if src_col in out.columns:
            x = pd.to_numeric(out[src_col], errors="coerce")
            out[log_col] = np.sign(x) * np.log1p(np.abs(x))

    for c in template_cols:
        if c not in out.columns:
            out[c] = pd.NA
    return out[template_cols].copy()


def _append_missing_rows_from_fundamentals(df: pd.DataFrame, p_fq: Path) -> tuple[pd.DataFrame, dict]:
    notes: dict[str, str] = {}
    if not {"year", "quarter", TICKER_COL}.issubset(df.columns):
        notes["missing_fundamental_rows"] = "skipped: factors frame missing year/quarter/ticker"
        return df, notes

    fq = pd.read_parquet(p_fq).copy()
    if TICKER_COL not in fq.columns or "year" not in fq.columns or "quarter" not in fq.columns:
        notes["missing_fundamental_rows"] = "skipped: fundamentals missing ticker/year/quarter"
        return df, notes

    fq[TICKER_COL] = normalize_ticker_series(fq[TICKER_COL])
    missing_tickers = sorted(set(fq[TICKER_COL].dropna().astype(str)) - set(df[TICKER_COL].dropna().astype(str)))
    if not missing_tickers:
        notes["missing_fundamental_rows"] = "none"
        return df, notes

    extra = _build_factor_rows_from_fundamentals(p_fq, missing_tickers, template_cols=list(df.columns))
    if len(extra) == 0:
        notes["missing_fundamental_rows"] = "none_after_build"
        return df, notes

    out = pd.concat([df, extra], ignore_index=True)
    out = out.sort_values([TICKER_COL, "year", "quarter"]).drop_duplicates([TICKER_COL, "year", "quarter"], keep="first").copy()
    notes["missing_fundamental_rows"] = f"appended {len(extra)} rows across {len(missing_tickers)} tickers from fundamentals"
    return out, notes


def _add_quarterly_snapshot_from_fundamentals(df: pd.DataFrame, p_fq: Path) -> dict:
    """
    Build report-only single-quarter snapshot metrics from already-quarterized
    fundamentals_quarterly source, NOT from TTM back-solving.

    This preserves DART report semantics:
      Q1: 1Q report
      Q2: half-year cumulative - Q1 cumulative
      Q3: 3Q cumulative - half-year cumulative
      Q4: annual cumulative - Q3 cumulative
    """
    notes: dict[str, str] = {}

    if not {"year", "quarter", TICKER_COL}.issubset(df.columns):
        notes["quarterly_snapshot"] = "skipped: missing year/quarter/ticker in factors frame"
        return notes

    fq = pd.read_parquet(p_fq).copy()
    needed = {TICKER_COL, "year", "quarter", "Revenue", "OpIncome"}
    miss = [c for c in needed if c not in fq.columns]
    if miss:
        notes["quarterly_snapshot"] = f"skipped: fundamentals missing cols={miss}"
        return notes

    fq[TICKER_COL] = normalize_ticker_series(fq[TICKER_COL])
    fq["year"] = pd.to_numeric(fq["year"], errors="coerce").astype("Int64")
    fq["quarter"] = pd.to_numeric(fq["quarter"], errors="coerce").astype("Int64")
    fq = fq.dropna(subset=[TICKER_COL, "year", "quarter"]).copy()
    fq["year"] = fq["year"].astype(int)
    fq["quarter"] = fq["quarter"].astype(int)

    fq = fq.sort_values([TICKER_COL, "year", "quarter"]).drop_duplicates([TICKER_COL, "year", "quarter"], keep="last").copy()

    fq["Revenue"] = pd.to_numeric(fq["Revenue"], errors="coerce").astype("float64")
    fq["OpIncome"] = pd.to_numeric(fq["OpIncome"], errors="coerce").astype("float64")

    # Revenue single-quarter snapshot
    fq["revenue_prev_q"] = fq.groupby(TICKER_COL)["Revenue"].shift(1)
    fq["revenue_cur_q"] = fq["Revenue"]
    denom_rev = fq["revenue_prev_q"].abs().replace(0.0, np.nan)
    fq["revenue_qoq"] = (fq["revenue_cur_q"] - fq["revenue_prev_q"]) / denom_rev

    # Operating income single-quarter snapshot
    fq["op_prev_q"] = fq.groupby(TICKER_COL)["OpIncome"].shift(1)
    fq["op_cur_q"] = fq["OpIncome"]
    denom_op = fq["op_prev_q"].abs().replace(0.0, np.nan)
    fq["op_qoq"] = (fq["op_cur_q"] - fq["op_prev_q"]) / denom_op

    qcols = [
        TICKER_COL, "year", "quarter",
        "revenue_prev_q", "revenue_cur_q", "revenue_qoq",
        "op_prev_q", "op_cur_q", "op_qoq",
    ]
    qsnap = fq[qcols].copy()

    merged = df.merge(qsnap, on=[TICKER_COL, "year", "quarter"], how="left", suffixes=("", "__fq"))
    for c in ["revenue_prev_q", "revenue_cur_q", "revenue_qoq", "op_prev_q", "op_cur_q", "op_qoq"]:
        fq_col = f"{c}__fq"
        if fq_col in merged.columns:
            merged[c] = pd.to_numeric(merged[fq_col], errors="coerce").astype("float64")
            merged = merged.drop(columns=[fq_col])

    # mutate in place at caller expectation
    for c in ["revenue_prev_q", "revenue_cur_q", "revenue_qoq", "op_prev_q", "op_cur_q", "op_qoq"]:
        df[c] = merged[c]

    notes["quarterly_snapshot"] = f"attached from fundamentals source: {p_fq.name}"
    return notes


def _add_derived_columns(df: pd.DataFrame, cfo_col: str) -> dict:
    notes: dict[str, str] = {}

    # streak / shift 기반 파생변수는 시계열 정렬이 전제됨
    if {TICKER_COL, "year", "quarter"}.issubset(df.columns):
        df.sort_values([TICKER_COL, "year", "quarter"], inplace=True)
        df.reset_index(drop=True, inplace=True)

    if cfo_col in df.columns:
        cfo_raw = pd.to_numeric(df[cfo_col], errors="coerce").astype("float64")
        df["CFO_isnull"] = cfo_raw.isna().astype("int8")

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

    if "Debt_to_Equity_log" not in df.columns:
        if "Debt_to_Equity" in df.columns:
            x = pd.to_numeric(df["Debt_to_Equity"], errors="coerce")
            x = x.where(x > 0)
            df["Debt_to_Equity_log"] = np.log1p(x)
        else:
            notes["Debt_to_Equity_log"] = "Neither Debt_to_Equity_log nor Debt_to_Equity exists"

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

    for src_col in ["OpIncome_acc2", "Revenue_acc2"]:
        log_col = f"{src_col}_log1p"
        if log_col not in df.columns and src_col in df.columns:
            x = _to_float(df[src_col])
            df[log_col] = np.sign(x) * np.log1p(np.abs(x))
            notes[log_col] = f"derived from signed log1p({src_col})"

    if {"op_prev_q", "op_cur_q"}.issubset(df.columns):
        prev_prev_q = pd.to_numeric(df.groupby(TICKER_COL)["op_cur_q"].shift(2), errors="coerce")
        prev_q = _to_float(df["op_prev_q"])
        cur_q = _to_float(df["op_cur_q"])
        df["op_growth_streak2"] = (
            prev_prev_q.notna() &
            prev_q.notna() &
            cur_q.notna() &
            (prev_q > prev_prev_q) &
            (cur_q > prev_q) &
            (prev_q > 0) &
            (cur_q > 0)
        ).astype("int8")
    else:
        df["op_growth_streak2"] = 0

    if {"revenue_prev_q", "revenue_cur_q"}.issubset(df.columns):
        prev_prev_rev = pd.to_numeric(df.groupby(TICKER_COL)["revenue_cur_q"].shift(2), errors="coerce")
        prev_rev = _to_float(df["revenue_prev_q"])
        cur_rev = _to_float(df["revenue_cur_q"])
        df["rev_growth_streak2"] = (
            prev_prev_rev.notna() &
            prev_rev.notna() &
            cur_rev.notna() &
            (prev_rev > prev_prev_rev) &
            (cur_rev > prev_rev)
        ).astype("int8")
    else:
        df["rev_growth_streak2"] = 0

    if {"year", "quarter"}.issubset(df.columns):
        year_num = pd.to_numeric(df["year"], errors="coerce")
        q_num = pd.to_numeric(df["quarter"], errors="coerce")
        if year_num.notna().all() and q_num.notna().all():
            df["quarter_key"] = year_num.astype(int) * 100 + q_num.astype(int)
            df["rebalance_month"] = compute_rebalance_month_from_yq(df["year"], df["quarter"])
            notes["rebalance_month"] = "attached via compute_rebalance_month_from_yq"
        else:
            notes["rebalance_month"] = "skipped: invalid year/quarter values"

    return notes


def _attach_group_map(df: pd.DataFrame, asof: str) -> tuple[pd.DataFrame, dict]:
    out = df.copy()
    notes: dict[str, str] = {}

    group_map, group_src, external_group_col = load_group_map(asof)
    existing_group_col = detect_group_col(out)

    if len(group_map) == 0 or not external_group_col:
        notes["group_map"] = "load_group_map returned empty"
        return out, notes

    if existing_group_col and existing_group_col in out.columns:
        out = out.merge(
            group_map.rename(columns={external_group_col: f"{external_group_col}__ext"}),
            on=TICKER_COL,
            how="left",
        )
        ext_col = f"{external_group_col}__ext"
        if ext_col in out.columns:
            out[existing_group_col] = out[existing_group_col].where(out[existing_group_col].notna(), out[ext_col])
            out = out.drop(columns=[ext_col])
        notes["group_map"] = f"filled existing {existing_group_col} from {group_src}"
    else:
        out = out.merge(group_map, on=TICKER_COL, how="left")
        notes["group_map"] = f"merged {external_group_col} from {group_src}"

    return out, notes


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
    notes: dict[str, str] = {}
    if NAME_COL in df.columns:
        return df, notes

    candidate_paths = [
        INTERIM_DIR / "marketdata" / f"krx_master__asof={asof}__src={master_src}__v={master_v}.parquet",
        PROCESSED / f"krx_master__asof={asof}__src={master_src}__v={master_v}.parquet",
    ]

    p_master = next((p for p in candidate_paths if p.exists()), None)
    if p_master is None:
        notes["name_attach"] = "krx_master not found -> skip attach"
        return df, notes

    m = pd.read_parquet(p_master)
    if TICKER_COL not in m.columns or NAME_COL not in m.columns:
        notes["name_attach"] = f"krx_master missing cols -> skip attach (have={list(m.columns)})"
        return df, notes

    m = m[[TICKER_COL, NAME_COL]].copy()
    m[TICKER_COL] = normalize_ticker_series(m[TICKER_COL])
    m = m.dropna(subset=[TICKER_COL]).drop_duplicates(TICKER_COL)

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
    ap.add_argument("--master_src", default="pykrx")
    ap.add_argument("--master_v", type=int, default=1)
    args = ap.parse_args()

    p_in = _pick_factors_path(args.asof, args.metric, args.in_v)
    p_fq = _pick_fundamentals_q_path(args.asof)

    df = pd.read_parquet(p_in)
    _require_cols(df, FEATURES_LIVE_REQUIRED_COLS)

    df = df.copy()
    df = ensure_industry_columns(df)
    df = _refresh_industry_from_reference(df)
    df[TICKER_COL] = normalize_ticker_series(df[TICKER_COL])
    df["year"] = pd.to_numeric(df["year"], errors="raise").astype(int)
    df["quarter"] = pd.to_numeric(df["quarter"], errors="raise").astype(int)

    notes = {"input_factors_path": p_in.name, "input_fundamentals_path": p_fq.name}
    df, notes_missing = _append_missing_rows_from_fundamentals(df, p_fq)
    notes.update(notes_missing)
    notes.update(_add_quarterly_snapshot_from_fundamentals(df, p_fq))
    notes.update(_add_derived_columns(df, args.cfo_col))

    df, notes2 = _try_attach_name(df, args.asof, args.master_src, args.master_v)
    notes.update(notes2)

    df, notes3 = _attach_group_map(df, args.asof)
    notes.update(notes3)

    df = _refresh_industry_from_reference(df)

    print(f"[INFO] rows: {len(df)}")
    print(f"[INFO] tickers: {df[TICKER_COL].nunique()}")

    nulls = _print_null_ratios(
        df,
        cols=[
            args.cfo_col,
            "CFO_isnull",
            "Quality_CFO_to_Assets",
            "Debt_to_Equity_log",
            "Revenue_ttm_yoy",
            "OpIncome_ttm_yoy",
            "Revenue_acc2",
            "OpIncome_acc2",
            "Revenue_acc2_ready",
            "OpIncome_acc2_ready",
            "CFO_warn",
            "CFO_safe",
            "rebalance_month",
            "industry_code",
            "industry_name",
            "industry4",
            "history_quarters",
            "revenue_prev_q",
            "revenue_cur_q",
            "revenue_qoq",
            "op_prev_q",
            "op_cur_q",
            "op_qoq",
            "OpIncome_acc2_log1p", "Revenue_acc2_log1p", "op_growth_streak2", "rev_growth_streak2",
        ],
    )

    df = _coalesce_duplicate_columns(df)

    FEATURES_LIVE_DIR.mkdir(parents=True, exist_ok=True)
    p_out = FEATURES_LIVE_DIR / f"features_live__asof={args.asof}__metric={args.metric}__v={args.out_v}.parquet"
    df.to_parquet(p_out, index=False)

    print(f"[OK] src factors      : {p_in.as_posix()}")
    print(f"[OK] src fundamentals : {p_fq.as_posix()}")
    print(f"[OK] saved           : {p_out.as_posix()}")

    if args.save_meta:
        meta = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "asof": args.asof,
            "metric": args.metric,
            "in_v": args.in_v,
            "out_v": args.out_v,
            "input_factors_path": p_in.as_posix(),
            "input_fundamentals_path": p_fq.as_posix(),
            "output_path": p_out.as_posix(),
            "rows": int(len(df)),
            "tickers": int(df[TICKER_COL].nunique()),
            "columns": list(map(str, df.columns)),
            "null_ratios": nulls,
            "notes": notes,
        }
        meta_path = FEATURES_LIVE_DIR / f"features_live__asof={args.asof}__metric={args.metric}__v={args.out_v}.meta.json"
        _save_meta(meta_path, meta)
        print(f"[OK] meta saved      : {meta_path.as_posix()}")


if __name__ == "__main__":
    main()
