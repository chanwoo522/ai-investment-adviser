from __future__ import annotations

import argparse
import glob
import os
import re
from pathlib import Path
from typing import List, Tuple, Optional

import pandas as pd


# ----------------------------
# Utilities: parquet selection
# ----------------------------

_Y_RE = re.compile(r"__y=(\d{4})-(\d{4})__")
_V_RE = re.compile(r"__v=(\d+)\.parquet$", re.IGNORECASE)


def _score_parquet(path: str) -> Tuple[int, int, int]:
    """
    Higher is better.
    1) prefer longer year span (e.g., 2016-2025 > 2016-2024)
    2) prefer higher version suffix __v=N
    3) prefer lexicographically later as final tiebreak
    """
    y_span = -1
    m = _Y_RE.search(path)
    if m:
        y_span = int(m.group(2)) - int(m.group(1))

    v = 0
    mv = _V_RE.search(path)
    if mv:
        v = int(mv.group(1))

    # third tiebreaker: stable ordering
    return (y_span, v, 0)


def pick_fundamentals_parquet(pattern: str) -> Path:
    """Pick the best *valid* parquet among candidates.

    We have seen cases where a parquet path exists but is a tiny/corrupted stub
    (e.g., a few hundred bytes) which pyarrow reads as an empty (0,0) dataframe.
    This picker filters out such invalid candidates before scoring.
    """

    cands = sorted(glob.glob(pattern))
    if not cands:
        raise FileNotFoundError(
            "fundamentals_quarterly parquet not found.\n"
            f"- cwd={os.getcwd()}\n"
            f"- pattern={pattern}"
        )

    # Lazy import: pyarrow is the recommended engine; if unavailable, we fall back
    # to a size-only heuristic.
    pq = None
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception:
        pq = None

    MIN_BYTES = 4096

    valid: List[str] = []
    invalid_reasons: List[Tuple[str, str]] = []
    for p in cands:
        try:
            sz = os.path.getsize(p)
        except OSError as e:
            invalid_reasons.append((p, f"stat_error:{repr(e)}"))
            continue

        if sz < MIN_BYTES:
            invalid_reasons.append((p, f"too_small:{sz}B"))
            continue

        if pq is not None:
            try:
                pf = pq.ParquetFile(p)
                if pf.schema_arrow is None or len(pf.schema_arrow) == 0:
                    invalid_reasons.append((p, "empty_schema"))
                    continue
            except Exception as e:
                invalid_reasons.append((p, f"parquet_open_error:{repr(e)}"))
                continue

        valid.append(p)

    if not valid:
        msg = (
            "No valid fundamentals parquet found after validation.\n"
            f"- cwd={os.getcwd()}\n"
            f"- pattern={pattern}\n"
            f"- candidates={cands}\n"
        )
        if invalid_reasons:
            msg += "- invalid_reasons=\n" + "\n".join([f"  - {pp}: {rr}" for pp, rr in invalid_reasons])
        raise FileNotFoundError(msg)

    # Choose best by (year-span, version). For ties, keep the last one.
    best = None
    best_key = None
    for p in valid:
        key = _score_parquet(p)
        if best is None or key > best_key:
            best, best_key = p, key
        elif key == best_key and p > best:
            best = p

    return Path(best)


# ----------------------------
# Schema normalization
# ----------------------------

def _coerce_quarter(x) -> Optional[int]:
    if pd.isna(x):
        return None
    s = str(x).strip().upper()
    # Common forms: 1, 2, 3, 4 / Q1..Q4 / 1Q, 2Q, etc.
    s = s.replace("분기", "").replace("QUARTER", "").strip()
    m = re.search(r"([1-4])", s)
    if m:
        return int(m.group(1))
    try:
        i = int(float(s))
        if 1 <= i <= 4:
            return i
    except Exception:
        pass
    return None


def normalize_schema(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.shape[1] == 0:
        raise ValueError("Loaded fundamentals dataframe has no columns (empty schema).")

    cols = df.columns.tolist()
    rename = {}

    # ticker
    if "ticker" not in cols:
        for cand in ["Ticker", "TICKER", "stock_code", "stockCode", "종목코드"]:
            if cand in cols:
                rename[cand] = "ticker"
                break

    # year
    if "year" not in cols:
        for cand in ["Year", "bsns_year", "bsnsYear", "연도"]:
            if cand in cols:
                rename[cand] = "year"
                break

    # quarter
    if "quarter" not in cols:
        for cand in ["Quarter", "q", "qtr", "분기", "reprt_code", "reprtCode"]:
            if cand in cols:
                rename[cand] = "quarter"
                break

    # revenue / op (operating profit)
    # Your parquet (shown in terminal) uses 'Revenue' and 'OpIncome'
    if "revenue" not in cols:
        for cand in ["Revenue", "revenue_amt", "sales", "매출액"]:
            if cand in cols:
                rename[cand] = "revenue"
                break

    if "op" not in cols:
        for cand in ["OpIncome", "operating_income", "영업이익"]:
            if cand in cols:
                rename[cand] = "op"
                break

    df = df.rename(columns=rename)

    required = ["ticker", "year", "quarter", "revenue", "op"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            "Required columns not found after normalize.\n"
            f"- missing={missing}\n"
            f"- available={df.columns.tolist()}"
        )

    # ticker normalize: 6-digit string
    df["ticker"] = (
        df["ticker"]
        .astype(str)
        .str.replace(r"\.0$", "", regex=True)
        .str.replace(r"\s+", "", regex=True)
        .str.zfill(6)
    )

    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")

    df["quarter"] = df["quarter"].map(_coerce_quarter).astype("Int64")

    # Drop rows with invalid year/quarter
    df = df.dropna(subset=["year", "quarter"]).copy()
    df["year"] = df["year"].astype(int)
    df["quarter"] = df["quarter"].astype(int)

    # quarter_key: sortable integer, e.g., 20161 for 2016Q1
    df["quarter_key"] = df["year"] * 10 + df["quarter"]

    # Ensure numeric for revenue/op
    df["revenue"] = pd.to_numeric(df["revenue"], errors="coerce")
    df["op"] = pd.to_numeric(df["op"], errors="coerce")

    return df


# ----------------------------
# Main pipeline
# ----------------------------

def build_exclude_lists(
    df: pd.DataFrame,
    lookback_q: int,
    min_valid_q: int,
    bad_ratio_threshold: float,
    metric: str = "revenue_op",
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Returns (agg, exclude, keep)
    """
    df = df.sort_values(["ticker", "quarter_key"])

    # last lookback_q quarters per ticker
    recent = df.groupby("ticker", group_keys=False).tail(lookback_q).copy()
    metric = (metric or "revenue_op").strip().lower()
    if metric == "revenue_op":
        # bad: revenue/op missing or <= 0
        bad_a = recent["revenue"].isna() | (recent["revenue"] <= 0)
        bad_b = recent["op"].isna() | (recent["op"] <= 0)
        bad = bad_a | bad_b
    elif metric == "cfo_capex":
        # bad: CFO missing or <= 0; CAPEX missing (CAPEX can be 0 for some firms)
        if "CFO" not in recent.columns or "CAPEX" not in recent.columns:
            raise ValueError(
                "metric=cfo_capex requires CFO and CAPEX columns in fundamentals. "
                f"available={recent.columns.tolist()}"
            )
        cfo = pd.to_numeric(recent["CFO"], errors="coerce")
        capex = pd.to_numeric(recent["CAPEX"], errors="coerce")
        bad = cfo.isna() | (cfo <= 0) | capex.isna()
    elif metric == "revenue_op_cfo":
        # bad: revenue/op bad OR CFO bad (missing or <= 0)
        if "CFO" not in recent.columns:
            raise ValueError(
                "metric=revenue_op_cfo requires CFO column in fundamentals. "
                f"available={recent.columns.tolist()}"
            )
        bad_a = recent["revenue"].isna() | (recent["revenue"] <= 0)
        bad_b = recent["op"].isna() | (recent["op"] <= 0)
        cfo = pd.to_numeric(recent["CFO"], errors="coerce")
        bad_c = cfo.isna() | (cfo <= 0)
        bad = bad_a | bad_b | bad_c
    else:
        raise ValueError("Unknown metric: " + metric)

    recent["bad"] = bad.astype(int)
    recent["valid"] = (~bad).astype(int)
    agg = (
        recent.groupby("ticker")
        .agg(
            n_q=("quarter_key", "count"),
            n_valid=("valid", "sum"),
            bad_ratio=("bad", "mean"),
            last_q=("quarter_key", "max"),
        )
        .reset_index()
    )

    exclude = agg[(agg["n_valid"] < min_valid_q) | (agg["bad_ratio"] > bad_ratio_threshold)].copy()
    keep = agg.drop(exclude.index).copy()

    return agg, exclude, keep


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True, help="as-of date, e.g. 2026-02-18")
    ap.add_argument("--lookback_q", type=int, default=8)
    ap.add_argument("--min_valid_q", type=int, default=4)
    ap.add_argument("--bad_ratio", type=float, default=0.5, help="threshold: exclude if bad_ratio > this")
    ap.add_argument(
        "--src_pattern",
        default=None,
        help="override parquet glob pattern (optional). If not set, uses ./data/processed/fundamentals_quarterly__asof={asof}*.parquet",
    )
    ap.add_argument("--debug", action="store_true")
    ap.add_argument(
        "--metric",
        default="revenue_op",
        choices=["revenue_op", "cfo_capex", "revenue_op_cfo"],
        help="Which fields define a 'valid' quarter. Default: revenue_op.",
    )
    return ap.parse_args()


def main() -> None:
    args = parse_args()

    patt = args.src_pattern or rf".\data\processed\fundamentals_quarterly__asof={args.asof}*.parquet"
    parquet_path = pick_fundamentals_parquet(patt)

    if args.debug:
        print("[DEBUG] cwd:", os.getcwd())
        print("[DEBUG] pattern:", patt)
        print("[DEBUG] chosen parquet:", parquet_path)
        print("[DEBUG] exists:", os.path.exists(parquet_path))
        try:
            print("[DEBUG] file_size:", os.path.getsize(parquet_path))
        except OSError as e:
            print("[DEBUG] file_size: <error>", repr(e))

    # Be explicit about parquet engine to avoid environment-dependent defaults.
    # (Some setups default to fastparquet; others to pyarrow.)
    read_errs = []
    df = None
    for engine in ["pyarrow", "fastparquet", None]:
        try:
            if engine is None:
                tmp = pd.read_parquet(parquet_path)
            else:
                tmp = pd.read_parquet(parquet_path, engine=engine)
            df = tmp
            if args.debug:
                print(f"[DEBUG] read_parquet engine={engine} -> shape={df.shape}, cols={list(df.columns)[:20]}")
            # If schema is empty, try the next engine.
            if df.shape[1] == 0:
                continue
            break
        except Exception as e:
            read_errs.append((engine, repr(e)))

    if df is None or df.shape[1] == 0:
        raise ValueError(
            "Loaded fundamentals dataframe has no columns (empty schema) after read_parquet.\n"
            f"- parquet_path={parquet_path}\n"
            f"- exists={os.path.exists(parquet_path)}\n"
            + (f"- read_errors={read_errs}\n" if read_errs else "")
            + "This usually indicates a parquet engine mismatch or a corrupted parquet file."
        )

    df = normalize_schema(df)

    # Attach stable per-ticker metadata for nicer outputs (and easier debugging).
    meta = None
    if {"ticker", "name", "corp_code"}.issubset(df.columns):
        meta = (
            df.sort_values(["ticker", "quarter_key"])
            .groupby("ticker", as_index=False)
            .agg(name=("name", "last"), corp_code=("corp_code", "last"))
        )


    if args.debug:
        print("[DEBUG] normalized columns:", df.columns.tolist())
        print("[DEBUG] shape:", df.shape)
        print("[DEBUG] head(2):\n", df.head(2).to_string(index=False))

    agg, exclude, keep = build_exclude_lists(
        df=df,
        lookback_q=args.lookback_q,
        min_valid_q=args.min_valid_q,
        bad_ratio_threshold=args.bad_ratio,
        metric=args.metric,
    )

    out_ex = Path(
        rf".\data\processed\exclude_missing_fin__asof={args.asof}__lbq={args.lookback_q}__minv={args.min_valid_q}__bad={args.bad_ratio}__metric={args.metric}__v=1.csv"
    )
    out_ke = Path(
        rf".\data\processed\keep_missing_fin__asof={args.asof}__lbq={args.lookback_q}__minv={args.min_valid_q}__bad={args.bad_ratio}__metric={args.metric}__v=1.csv"
    )
    out_ex.parent.mkdir(parents=True, exist_ok=True)

    exclude.to_csv(out_ex, index=False, encoding="utf-8-sig")
    keep.to_csv(out_ke, index=False, encoding="utf-8-sig")

    print("fundamentals:", str(parquet_path))
    print("tickers_total:", int(agg.shape[0]))
    print("excluded:", int(exclude.shape[0]))
    print("kept:", int(keep.shape[0]))
    print("saved:", str(out_ex))
    print("saved:", str(out_ke))


if __name__ == "__main__":
    main()
