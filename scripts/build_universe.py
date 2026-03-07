#!/usr/bin/env python
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def _run(script_rel: str, args: list[str]) -> None:
    """Run another repo script in a platform-safe way."""
    script = Path(__file__).resolve().parent / script_rel
    cmd = [sys.executable, str(script)] + args
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"Command failed ({r.returncode}): {' '.join(cmd)}")


def _pick_master_path(asof: str) -> Path:
    root = Path("data/processed")
    cands = sorted(root.glob(f"krx_master__asof={asof}__*.parquet"))
    if cands:
        return cands[-1]

    # Try: auto-generate master if missing
    print("[WARN] krx_master parquet not found. Auto-running collect_krx_master.py ...")
    _run("collect_krx_master.py", ["--asof", asof])

    cands = sorted(root.glob(f"krx_master__asof={asof}__*.parquet"))
    if not cands:
        raise FileNotFoundError(
            "krx_master parquet not found even after collect_krx_master.py.\n"
            "Check internet access / pykrx availability."
        )
    return cands[-1]


def _pick_marketdata_path(asof: str) -> Path:
    root = Path("data/processed")
    cands = sorted(root.glob(f"krx_marketdata__asof={asof}__*.parquet"))
    if cands:
        return cands[-1]

    # Auto-generate if missing
    print("[WARN] krx_marketdata parquet not found. Auto-running collect_krx_marketdata.py ...")
    _run("collect_krx_marketdata.py", ["--asof", asof])

    cands = sorted(root.glob(f"krx_marketdata__asof={asof}__*.parquet"))
    if not cands:
        raise FileNotFoundError(
            "krx_marketdata parquet not found even after collect_krx_marketdata.py.\n"
            "Check internet access / pykrx availability."
        )
    return cands[-1]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--mcap_top", type=int, default=800)
    ap.add_argument("--trd_bot", type=float, default=0.1)
    ap.add_argument("--cap_buckets", type=str, default="large,mid,small")
    args = ap.parse_args()

    asof = args.asof
    master_p = _pick_master_path(asof)
    market_p = _pick_marketdata_path(asof)

    import pandas as pd

    master = pd.read_parquet(master_p)
    mkt = pd.read_parquet(market_p)

    # Merge to get mcap + traded value
    df = master.merge(mkt, on="ticker", how="inner")

    # Basic filters
    if "mcap" not in df.columns:
        raise ValueError("marketdata must include 'mcap' column.")

    # traded value column name can vary by collector
    if "traded_value" not in df.columns:
        for alt in ("trd_value", "trading_value", "traded_val", "value"):
            if alt in df.columns:
                df["traded_value"] = df[alt]
                break

    # last-resort: derive traded_value from close*vol if available
    if "traded_value" not in df.columns and ("close" in df.columns) and ("vol" in df.columns):
        df["traded_value"] = df["close"] * df["vol"]

    if "traded_value" not in df.columns:
        raise ValueError("marketdata must include a traded value column (e.g., traded_value or trd_value).")

    df = df.sort_values("mcap", ascending=False)
    df = df.head(int(args.mcap_top)).copy()

    # Exclude bottom by traded value ratio (relative to median)
    med_tv = df["traded_value"].median()
    thr = float(args.trd_bot) * med_tv
    df = df[df["traded_value"] >= thr].copy()

    # Assign cap buckets by quantiles on mcap among remaining tickers
    buckets = [b.strip() for b in args.cap_buckets.split(",") if b.strip()]
    if len(buckets) != 3:
        raise ValueError("--cap_buckets must contain exactly 3 comma-separated names (e.g., large,mid,small).")

    q1 = df["mcap"].quantile(2/3)
    q2 = df["mcap"].quantile(1/3)

    def _bucket(x: float) -> str:
        if x >= q1:
            return buckets[0]
        if x >= q2:
            return buckets[1]
        return buckets[2]

    df["cap_bucket"] = df["mcap"].map(_bucket)

    out = Path("data/processed") / f"universe__asof={asof}__src=phase1__mcap_top={args.mcap_top}__trd_bot={args.trd_bot}__v=1.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)

    print("[OK] master:", master_p)
    print("[OK] market:", market_p)
    print("[OK] saved:", out)
    print("rows:", len(df), "tickers:", df["ticker"].nunique(), "buckets:", df["cap_bucket"].value_counts().to_dict())


if __name__ == "__main__":
    main()
