#!/usr/bin/env python
"""
Export universe CSV for downstream scripts (e.g., make_prices_daily.py).

Why this exists:
- Some scripts expect: data/processed/universe__asof=...__metric=...__v=...csv
- Phase1 universe is stored as parquet: universe__asof=...__src=phase1__*.parquet

This tool finds the latest matching phase1 universe parquet and exports a
single-column CSV with 'ticker'.

Example (PowerShell):
  python .\scripts\ops\export_universe_csv.py --asof 2026-02-18 --metric revenue_op --out_v 1
"""
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def _pick_phase1_universe_parquet(root: Path, asof: str) -> Path:
    cands = sorted(root.glob(f"universe__asof={asof}__src=phase1__*.parquet"))
    if not cands:
        raise FileNotFoundError(
            f"No phase1 universe parquet for asof={asof} in {root}.\n"
            f"Hint: run scripts/build_universe.py first (after collecting krx_master/marketdata)."
        )
    return cands[-1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--out_v", type=int, default=1, help="output universe csv version")
    ap.add_argument("--root", default="data/processed", help="processed data root")
    args = ap.parse_args()

    root = Path(args.root)
    p = _pick_phase1_universe_parquet(root, args.asof)
    df = pd.read_parquet(p)

    if "ticker" not in df.columns:
        for alt in ["code", "종목코드"]:
            if alt in df.columns:
                df = df.rename(columns={alt: "ticker"})
                break

    if "ticker" not in df.columns:
        raise ValueError(f"ticker column not found. cols={list(df.columns)[:50]}")

    out = root / f"universe__asof={args.asof}__metric={args.metric}__v={args.out_v}.csv"
    df[["ticker"]].drop_duplicates().to_csv(out, index=False)

    print("[OK] src:", p)
    print("[OK] saved:", out)
    print("rows:", len(df), "tickers:", df["ticker"].nunique())


if __name__ == "__main__":
    main()
