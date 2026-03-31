#!/usr/bin/env python
from __future__ import annotations

import argparse
import subprocess
import sys
import re
from pathlib import Path


def _run(script_rel: str, args: list[str]) -> None:
    script = Path(__file__).resolve().parent / script_rel
    cmd = [sys.executable, str(script)] + args
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"Command failed ({r.returncode}): {' '.join(cmd)}")


def _extract_asof_from_name(name: str) -> str | None:
    m = re.search(r"__asof=(\d{4}-\d{2}-\d{2})__", name)
    return m.group(1) if m else None


def _pick_best_path(root: Path, pattern: str, asof: str) -> Path | None:
    cands = sorted(root.glob(pattern))
    if not cands:
        return None

    scored: list[tuple[int, str, str, Path]] = []
    for p in cands:
        a = _extract_asof_from_name(p.name)
        if a is None:
            continue
        if a == asof:
            bucket = 0
        elif a < asof:
            bucket = 1
        else:
            bucket = 2
        scored.append((bucket, a, p.name, p))

    exact = sorted([x for x in scored if x[0] == 0], key=lambda x: (x[1], x[2]), reverse=True)
    older = sorted([x for x in scored if x[0] == 1], key=lambda x: (x[1], x[2]), reverse=True)
    newer = sorted([x for x in scored if x[0] == 2], key=lambda x: (x[1], x[2]), reverse=False)

    ordered = exact + older + newer
    return ordered[0][3] if ordered else None


def _pick_master_path(asof: str) -> Path:
    root = Path("data/processed")
    p = _pick_best_path(root, "krx_master__asof=*__*.parquet", asof)
    if p is not None:
        if _extract_asof_from_name(p.name) != asof:
            print(f"[WARN] using nearest krx_master fallback: requested={asof} picked={p.name}")
        return p

    print("[WARN] krx_master parquet not found. Auto-running collect_krx_master.py ...")
    _run("collect_krx_master.py", ["--asof", asof])

    p = _pick_best_path(root, "krx_master__asof=*__*.parquet", asof)
    if p is None:
        raise FileNotFoundError("krx_master parquet not found even after collect_krx_master.py.")
    if _extract_asof_from_name(p.name) != asof:
        print(f"[WARN] using nearest krx_master fallback after auto-run: requested={asof} picked={p.name}")
    return p


def _pick_marketdata_path(asof: str) -> Path:
    root = Path("data/processed")
    p = _pick_best_path(root, "krx_marketdata__asof=*__*.parquet", asof)
    if p is not None:
        if _extract_asof_from_name(p.name) != asof:
            print(f"[WARN] using nearest krx_marketdata fallback: requested={asof} picked={p.name}")
        return p

    print("[WARN] krx_marketdata parquet not found. Auto-running collect_krx_marketdata.py ...")
    _run("collect_krx_marketdata.py", ["--asof", asof])

    p = _pick_best_path(root, "krx_marketdata__asof=*__*.parquet", asof)
    if p is None:
        raise FileNotFoundError("krx_marketdata parquet not found even after collect_krx_marketdata.py.")
    if _extract_asof_from_name(p.name) != asof:
        print(f"[WARN] using nearest krx_marketdata fallback after auto-run: requested={asof} picked={p.name}")
    return p


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

    if "ticker" not in master.columns or "ticker" not in mkt.columns:
        raise ValueError("Both master and marketdata must contain 'ticker'.")

    master["ticker"] = master["ticker"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
    mkt["ticker"] = mkt["ticker"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)

    # 핵심 수정:
    # 기존 inner join(master x marketdata) 때문에 master에 없는 티커가 전부 탈락했음.
    # marketdata는 058470을 가지고 있었는데 master에 없어서 리노공업이 universe 단계에서 사라졌음.
    # 따라서 marketdata를 기준(left)으로 잡고 master의 name/asof/notes는 보조 정보로 붙인다.
    master_keep = [c for c in ["ticker", "name", "asof_ymd", "created_at", "notes"] if c in master.columns]
    master_small = master[master_keep].drop_duplicates("ticker").copy()

    df = mkt.merge(master_small, on="ticker", how="left", suffixes=("", "_master"))

    if "name" not in df.columns:
        df["name"] = pd.NA

    if "mcap" not in df.columns and "market_cap" in df.columns:
        df["mcap"] = df["market_cap"]

    if "mcap" not in df.columns:
        raise ValueError("marketdata must include 'mcap' or 'market_cap' column.")

    if "traded_value" not in df.columns:
        for alt in ("trd_value", "trading_value", "traded_val", "value"):
            if alt in df.columns:
                df["traded_value"] = df[alt]
                break

    if "traded_value" not in df.columns and ("close" in df.columns) and ("volume" in df.columns):
        df["traded_value"] = pd.to_numeric(df["close"], errors="coerce") * pd.to_numeric(df["volume"], errors="coerce")

    if "traded_value" not in df.columns and ("close" in df.columns) and ("vol" in df.columns):
        df["traded_value"] = pd.to_numeric(df["close"], errors="coerce") * pd.to_numeric(df["vol"], errors="coerce")

    df["mcap"] = pd.to_numeric(df["mcap"], errors="coerce")
    if "traded_value" in df.columns:
        df["traded_value"] = pd.to_numeric(df["traded_value"], errors="coerce")
    else:
        df["traded_value"] = pd.NA

    df = df.dropna(subset=["ticker", "mcap"]).copy()
    df = df.sort_values("mcap", ascending=False).head(int(args.mcap_top)).copy()
    before_liq = df.copy()

    use_liq_filter = True
    med_tv = pd.to_numeric(df["traded_value"], errors="coerce").median()
    if pd.isna(med_tv) or med_tv <= 0:
        use_liq_filter = False
        print("[WARN] traded_value median unavailable/non-positive -> skip liquidity filter and use mcap_top universe only")

    if use_liq_filter:
        thr = float(args.trd_bot) * med_tv
        liq = df[df["traded_value"] >= thr].copy()
        if len(liq) == 0:
            print("[WARN] liquidity filter resulted in 0 rows -> fallback to pre-liquidity mcap_top universe")
            df = before_liq
        else:
            df = liq

    buckets = [b.strip() for b in args.cap_buckets.split(",") if b.strip()]
    if len(buckets) != 3:
        raise ValueError("--cap_buckets must contain exactly 3 comma-separated names (e.g., large,mid,small).")

    q1 = df["mcap"].quantile(2 / 3)
    q2 = df["mcap"].quantile(1 / 3)

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

    missing_name = int(df["name"].isna().sum()) if "name" in df.columns else len(df)

    print("[OK] master:", master_p)
    print("[OK] market:", market_p)
    print("[OK] saved:", out)
    print("rows:", len(df), "tickers:", df["ticker"].nunique(), "buckets:", df["cap_bucket"].value_counts().to_dict())
    print("[INFO] name_missing_rows:", missing_name)


if __name__ == "__main__":
    main()
