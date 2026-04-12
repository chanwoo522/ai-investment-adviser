#!/usr/bin/env python
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--ticker", default="229200", help="KODEX KOSDAQ150 ETF ticker by default")
    ap.add_argument("--benchmark_name", default="KOSDAQ150")
    ap.add_argument("--output_csv", required=True)
    args = ap.parse_args()

    start_dt = pd.to_datetime(args.start)
    end_dt = pd.to_datetime(args.end)
    if end_dt < start_dt:
        raise ValueError(f"end date must be >= start date: start={args.start} end={args.end}")

    try:
        from pykrx import stock
    except Exception as e:
        raise RuntimeError("pykrx is required for benchmark generation") from e

    ticker = str(args.ticker).zfill(6)
    start = start_dt.strftime("%Y%m%d")
    end = end_dt.strftime("%Y%m%d")

    df = None
    last_err = None
    for fn_name in ["get_etf_ohlcv_by_date", "get_market_ohlcv_by_date"]:
        fn = getattr(stock, fn_name, None)
        if fn is None:
            continue
        try:
            temp = fn(start, end, ticker)
            if temp is not None and len(temp) > 0:
                df = temp.copy()
                break
        except Exception as e:
            last_err = e
            continue

    if df is None or len(df) == 0:
        raise RuntimeError(f"failed to fetch benchmark data for ticker={ticker}; last_err={last_err}")

    df = df.reset_index()
    date_col = next((c for c in ["날짜", "date", "Date"] if c in df.columns), df.columns[0])
    price_col = next((c for c in ["종가", "close", "Close"] if c in df.columns), None)
    if price_col is None:
        raise ValueError(f"could not find close column in benchmark data columns={list(df.columns)}")

    out = df[[date_col, price_col]].copy()
    out.columns = ["date", "price"]
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    out = out.dropna(subset=["date", "price"]).sort_values("date").copy()
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    out["benchmark_name"] = args.benchmark_name
    out["ticker"] = ticker

    out_path = Path(args.output_csv)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False, encoding="utf-8-sig")

    print(f"[OK] benchmark: {out_path}")
    print(f"[INFO] rows: {len(out)}")
    print(f"[INFO] start: {out['date'].iloc[0]}")
    print(f"[INFO] end: {out['date'].iloc[-1]}")
    print(f"[INFO] ticker: {ticker}")
    print(f"[INFO] benchmark_name: {args.benchmark_name}")


if __name__ == "__main__":
    main()
