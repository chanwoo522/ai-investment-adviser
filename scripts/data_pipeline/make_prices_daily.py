#!/usr/bin/env python
from __future__ import annotations

import argparse
import re
from pathlib import Path
import pandas as pd
from pykrx import stock

# ---- path bootstrap ----
import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# ------------------------

from krx_safe import to_ymd


def normalize_ticker_series(s: pd.Series) -> pd.Series:
    return s.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)


def _extract_asof_from_name(name: str) -> str | None:
    m = re.search(r"__asof=(\d{4}-\d{2}-\d{2})__", name)
    return m.group(1) if m else None


def _read_any_table(p: Path) -> pd.DataFrame:
    if p.suffix.lower() == ".csv":
        return pd.read_csv(p)
    if p.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(p)
    return pd.read_parquet(p)


def _collect_union_tickers(asof: str, metric: str) -> list[str]:
    root = Path("data/processed")
    frames = []

    patterns = [
        f"krx_master__asof=*__src=pykrx__v=*.parquet",
        f"krx_marketdata__asof=*__src=pykrx__lookback=365d__v=*.parquet",
        f"fundamentals_quarterly__asof=*__src=dart__fs=CFS__y=2016-2025__v=*.parquet",
        f"universe__asof=*__metric={metric}__v=*.csv",
        f"universe__asof=*__metric={metric}__v=*.parquet",
        f"features_live__asof=*__metric={metric}__v=*.parquet",
        f"shares_industry__asof=*__src=dart__*.parquet",
    ]

    for pat in patterns:
        for p in root.glob(pat):
            a = _extract_asof_from_name(p.name)
            if a and a <= asof:
                try:
                    df = _read_any_table(p)
                    if "ticker" in df.columns:
                        x = df[["ticker"]].copy()
                        x["ticker"] = normalize_ticker_series(x["ticker"])
                        frames.append(x)
                except Exception as e:
                    print(f"[WARN] failed reading ticker source {p}: {e}")

    # current holdings optional
    p_cur = root / "my_current_holdings.csv"
    if p_cur.exists():
        try:
            cur = pd.read_csv(p_cur)
            if "ticker" in cur.columns:
                x = cur[["ticker"]].copy()
                x["ticker"] = normalize_ticker_series(x["ticker"])
                frames.append(x)
        except Exception as e:
            print(f"[WARN] failed reading current holdings {p_cur}: {e}")

    if not frames:
        raise FileNotFoundError("No usable union ticker source found.")

    out = pd.concat(frames, ignore_index=True)
    out = out.dropna(subset=["ticker"]).drop_duplicates("ticker")
    return sorted(out["ticker"].tolist())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--start", default="20160101")
    ap.add_argument("--end", default=None)
    ap.add_argument("--universe_v", default="1")  # kept for compatibility
    ap.add_argument("--out_v", default="1")
    args = ap.parse_args()

    asof = args.asof
    metric = args.metric
    end_ymd = to_ymd(args.end if args.end is not None else args.asof)

    out = Path(rf"data/processed/prices_daily__src=pykrx__start={args.start}__asof={asof}__metric={metric}__v={args.out_v}.parquet")
    if out.exists():
        print("[OK] exists:", out)
        return

    tickers = _collect_union_tickers(asof, metric)
    print(f"[INFO] union ticker source n={len(tickers)} end={end_ymd}")

    rows = []
    failed = []

    for i, t in enumerate(tickers, 1):
        try:
            df = stock.get_market_ohlcv_by_date(args.start, end_ymd, t)
            if df is None or len(df) == 0:
                failed.append((t, "empty"))
                continue

            df = df.reset_index()
            df["ticker"] = t
            df = df.rename(columns={
                "날짜": "date",
                "종가": "Close",
                "시가": "Open",
                "고가": "High",
                "저가": "Low",
                "거래량": "Volume"
            })

            if "date" not in df.columns:
                df = df.rename(columns={df.columns[0]: "date"})

            keep = [c for c in ["date", "Open", "High", "Low", "Close", "Volume", "ticker"] if c in df.columns]
            rows.append(df[keep].copy())

        except Exception as e:
            failed.append((t, repr(e)))

        if i % 50 == 0:
            print(f"[INFO] {i}/{len(tickers)} done")

    if not rows:
        raise SystemExit("No price data downloaded.")

    px = pd.concat(rows, ignore_index=True)
    px["date"] = pd.to_datetime(px["date"])
    px = px.sort_values(["ticker", "date"])

    out.parent.mkdir(parents=True, exist_ok=True)
    px.to_parquet(out, index=False)

    print("[OK] saved:", out)
    print("rows:", len(px), "tickers:", px["ticker"].nunique(), "date_max:", px["date"].max())

    if failed:
        fail_path = out.with_suffix(".failed.csv")
        pd.DataFrame(failed, columns=["ticker", "error"]).to_csv(fail_path, index=False, encoding="utf-8-sig")
        print("[WARN] failed tickers:", len(failed), "->", fail_path)


if __name__ == "__main__":
    main()