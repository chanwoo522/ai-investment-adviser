#!/usr/bin/env python
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd
from pykrx import stock

from krx_safe import to_ymd, backoff_date_call


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True, help="YYYY-MM-DD")
    ap.add_argument("--freq", default="d", choices=["d"], help="daily only for now")
    ap.add_argument("--lookback_years", type=int, default=15)
    ap.add_argument("--out_v", type=int, default=1)
    args = ap.parse_args()

    asof_ymd = to_ymd(args.asof)

    # Determine date range
    end = datetime.strptime(asof_ymd, "%Y%m%d")
    start = end.replace(year=end.year - int(args.lookback_years))
    start_ymd = start.strftime("%Y%m%d")

    # Always pass explicit date to avoid internal nearest-business-day.
    def _tickers(d: str) -> pd.DataFrame:
        t = stock.get_market_ticker_list(d, market="ALL")
        return pd.DataFrame({"ticker": [str(x) for x in t]})

    used_ymd, df_t = backoff_date_call(_tickers, asof_ymd, max_back_days=14, require_cols={"ticker"})
    tickers = df_t["ticker"].astype(str).tolist()

    rows = []
    for i, t in enumerate(tickers, 1):
        try:
            ohlcv = stock.get_market_ohlcv_by_date(start_ymd, used_ymd, t)
            if ohlcv is None or len(ohlcv) == 0:
                continue
            ohlcv = ohlcv.reset_index()

            # Normalize columns (Korean)
            ren = {}
            if "날짜" in ohlcv.columns:
                ren["날짜"] = "date"
            elif "index" in ohlcv.columns:
                ren["index"] = "date"

            if "시가" in ohlcv.columns:
                ren["시가"] = "open"
            if "고가" in ohlcv.columns:
                ren["고가"] = "high"
            if "저가" in ohlcv.columns:
                ren["저가"] = "low"
            if "종가" in ohlcv.columns:
                ren["종가"] = "close"
            if "거래량" in ohlcv.columns:
                ren["거래량"] = "vol"
            if "거래대금" in ohlcv.columns:
                ren["거래대금"] = "value"

            df = ohlcv.rename(columns=ren)
            if "date" not in df.columns:
                df = df.rename(columns={df.columns[0]: "date"})

            df["ticker"] = str(t)
            # make date yyyy-mm-dd for easier joins
            df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")

            keep = ["date", "ticker"]
            for c in ("open", "high", "low", "close", "vol", "value"):
                if c in df.columns:
                    keep.append(c)
            df = df[keep].copy()

            # numeric conversion
            for c in ("open", "high", "low", "close", "vol", "value"):
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")

            rows.append(df)
        except Exception:
            continue

        if i % 100 == 0:
            print(f"[INFO] {i}/{len(tickers)} tickers processed...")

    out = Path("data/processed") / f"prices__asof={args.asof}__src=pykrx__d__start={start_ymd}__end={used_ymd}__v={args.out_v}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)

    if not rows:
        raise RuntimeError("No price data collected. Check pykrx connectivity / KRX response.")

    px = pd.concat(rows, ignore_index=True)
    px.to_parquet(out, index=False)

    print("[OK] saved:", out)
    print(f"[INFO] used_ymd={used_ymd} rows={len(px)} tickers={px['ticker'].nunique()} date_range={px['date'].min()}..{px['date'].max()}")


if __name__ == "__main__":
    main()