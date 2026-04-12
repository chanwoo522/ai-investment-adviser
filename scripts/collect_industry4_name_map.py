#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Iterable

import pandas as pd


# -------------------------------------------------
# Utilities
# -------------------------------------------------

def normalize_ticker_series(s: pd.Series) -> pd.Series:
    x = s.astype(str).str.extract(r"(\d+)")[0]
    return x.str.zfill(6)


def pick_first(cols: Iterable[str], candidates: list[str]) -> str | None:
    cols = list(cols)
    lower_map = {str(c).lower(): c for c in cols}
    for cand in candidates:
        if cand in cols:
            return cand
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None


def load_any_table(path: Path) -> pd.DataFrame:
    suf = path.suffix.lower()
    if suf == ".parquet":
        return pd.read_parquet(path)
    if suf in {".csv", ".txt"}:
        return pd.read_csv(path)
    if suf in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    raise ValueError(f"Unsupported file type: {path}")


def ensure_dir(p: Path) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)


# -------------------------------------------------
# KIND file normalization
# -------------------------------------------------

def normalize_kind_listing(df: pd.DataFrame) -> pd.DataFrame:
    cols = list(df.columns)

    ticker_col = pick_first(cols, [
        "ticker", "종목코드", "회사코드", "code", "symbol"
    ])
    name_col = pick_first(cols, [
        "name", "회사명", "종목명", "법인명", "corp_name"
    ])
    industry_name_col = pick_first(cols, [
        "industry_name", "업종명", "업종", "industry", "sector_name"
    ])
    market_col = pick_first(cols, [
        "market", "시장구분", "시장", "market_type"
    ])

    out = df.copy()

    if ticker_col is not None:
        out["ticker"] = normalize_ticker_series(out[ticker_col])
    else:
        out["ticker"] = pd.NA

    out["name"] = out[name_col].astype(str).str.strip() if name_col is not None else pd.NA
    out["industry_name"] = out[industry_name_col].astype(str).str.strip() if industry_name_col is not None else pd.NA
    out["market"] = out[market_col].astype(str).str.strip() if market_col is not None else pd.NA

    if out["industry_name"].notna().any():
        out["industry_name"] = (
            out["industry_name"]
            .replace({"nan": pd.NA, "None": pd.NA, "<NA>": pd.NA})
            .astype("string")
            .str.replace(r"\s+", " ", regex=True)
            .str.strip()
        )

    keep = ["ticker", "name", "market", "industry_name"]
    return out[keep].copy()


# -------------------------------------------------
# Local code source normalization
# -------------------------------------------------

def normalize_code_source(df: pd.DataFrame) -> pd.DataFrame:
    cols = list(df.columns)
    ticker_col = pick_first(cols, ["ticker", "종목코드", "code", "symbol"])
    industry4_col = pick_first(cols, ["industry4", "induty_code", "industry", "sector"])
    name_col = pick_first(cols, ["name", "종목명", "회사명", "corp_name"])
    market_col = pick_first(cols, ["market", "시장", "시장구분", "market_type"])

    if ticker_col is None or industry4_col is None:
        raise ValueError(f"Need ticker + industry4-like cols. Have: {cols}")

    out = df.copy()
    out["ticker"] = normalize_ticker_series(out[ticker_col])
    out["industry4"] = pd.to_numeric(out[industry4_col], errors="coerce").astype("Int64")
    out["name"] = out[name_col].astype(str).str.strip() if name_col is not None else pd.NA
    out["market"] = out[market_col].astype(str).str.strip() if market_col is not None else pd.NA

    out = out[["ticker", "industry4", "name", "market"]].copy()
    out = out.dropna(subset=["ticker", "industry4"]).drop_duplicates(["ticker"], keep="last")
    return out


# -------------------------------------------------
# Matching logic
# -------------------------------------------------

def build_industry_map(kind_df: pd.DataFrame, code_df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    # Prefer ticker match. If ticker missing in KIND file for some reason, fallback by name later.
    merged = code_df.merge(
        kind_df[["ticker", "name", "market", "industry_name"]].rename(
            columns={"name": "kind_name", "market": "kind_market"}
        ),
        on="ticker",
        how="left",
    )

    # name fallback for rows still missing industry_name
    need_name_fallback = merged["industry_name"].isna()
    if need_name_fallback.any() and kind_df["name"].notna().any():
        kind_name_map = kind_df[["name", "industry_name"]].copy()
        kind_name_map["name_key"] = kind_name_map["name"].astype(str).str.replace(r"\s+", "", regex=True)
        kind_name_map = kind_name_map.dropna(subset=["name_key", "industry_name"]).drop_duplicates(["name_key"], keep="last")

        merged["name_key"] = merged["name"].astype(str).str.replace(r"\s+", "", regex=True)
        merged = merged.merge(
            kind_name_map[["name_key", "industry_name"]].rename(columns={"industry_name": "industry_name_by_name"}),
            on="name_key",
            how="left",
        )
        merged["industry_name"] = merged["industry_name"].combine_first(merged["industry_name_by_name"])
        merged = merged.drop(columns=["name_key", "industry_name_by_name"], errors="ignore")

    matched_rows = merged[merged["industry_name"].notna()].copy()

    # Build industry4 -> name candidates and resolve conflicts by frequency.
    cand = (
        matched_rows.groupby(["industry4", "industry_name"], dropna=True)
        .size()
        .reset_index(name="n")
        .sort_values(["industry4", "n", "industry_name"], ascending=[True, False, True])
    )

    resolved = cand.groupby("industry4", as_index=False).head(1).copy()
    resolved["source"] = "KIND+local_code_join"

    conflict = cand.groupby("industry4", as_index=False).size().rename(columns={"size": "name_candidates"})
    resolved = resolved.merge(conflict, on="industry4", how="left")
    resolved["has_conflict"] = resolved["name_candidates"].fillna(0).astype(int) > 1

    diagnostics = {
        "code_rows": int(len(code_df)),
        "kind_rows": int(len(kind_df)),
        "matched_rows": int(len(matched_rows)),
        "matched_tickers": int(matched_rows["ticker"].nunique()) if len(matched_rows) else 0,
        "mapped_industry4_count": int(resolved["industry4"].nunique()) if len(resolved) else 0,
        "conflict_industry4_count": int(resolved["has_conflict"].sum()) if len(resolved) else 0,
    }

    ticker_match = matched_rows[["ticker", "industry4", "industry_name", "name", "market"]].drop_duplicates("ticker", keep="last")
    industry_map = resolved[["industry4", "industry_name", "source", "n", "name_candidates", "has_conflict"]].sort_values("industry4").reset_index(drop=True)
    return industry_map, ticker_match, diagnostics


# -------------------------------------------------
# Main
# -------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Build industry4 -> Korean industry name map from KIND listing + local code source.")
    ap.add_argument("--kind_file", required=True, help="KIND 상장법인목록 EXCEL/CSV/PARQUET 파일 경로")
    ap.add_argument("--code_file", required=True, help="Local file with ticker + industry4, e.g. krx_marketdata parquet")
    ap.add_argument("--asof", required=True)
    ap.add_argument("--output_csv", default="data/reference/industry4_name_map.csv")
    ap.add_argument("--output_ticker_csv", default="data/reference/ticker_industry_name_map.csv")
    ap.add_argument("--output_json", default="data/reference/industry4_name_map.diagnostics.json")
    args = ap.parse_args()

    kind_path = Path(args.kind_file)
    code_path = Path(args.code_file)

    kind_raw = load_any_table(kind_path)
    code_raw = load_any_table(code_path)

    kind_df = normalize_kind_listing(kind_raw)
    code_df = normalize_code_source(code_raw)

    industry_map, ticker_match, diagnostics = build_industry_map(kind_df, code_df)
    industry_map["asof"] = args.asof
    ticker_match["asof"] = args.asof

    out_csv = Path(args.output_csv)
    out_ticker_csv = Path(args.output_ticker_csv)
    out_json = Path(args.output_json)
    ensure_dir(out_csv)
    ensure_dir(out_ticker_csv)
    ensure_dir(out_json)

    industry_map.to_csv(out_csv, index=False, encoding="utf-8-sig")
    ticker_match.to_csv(out_ticker_csv, index=False, encoding="utf-8-sig")
    out_json.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] kind rows              : {len(kind_df)}")
    print(f"[OK] code rows              : {len(code_df)}")
    print(f"[OK] matched ticker rows    : {diagnostics['matched_rows']}")
    print(f"[OK] mapped industry4 count : {diagnostics['mapped_industry4_count']}")
    print(f"[OK] conflict industry4 cnt : {diagnostics['conflict_industry4_count']}")
    print(f"[OK] saved industry map     : {out_csv}")
    print(f"[OK] saved ticker map       : {out_ticker_csv}")
    print(f"[OK] saved diagnostics      : {out_json}")


if __name__ == "__main__":
    main()
