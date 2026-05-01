#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def _write_meta(output_csv: Path, meta: dict) -> Path:
    meta_path = output_csv.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return meta_path


def _normalize_benchmark_frame(df: pd.DataFrame, ticker: str, benchmark_name: str) -> pd.DataFrame:
    date_col = next((c for c in ["date", "Date"] if c in df.columns), None)
    price_col = next((c for c in ["price", "close", "Close", "benchmark_price"] if c in df.columns), None)
    if date_col is None or price_col is None:
        raise ValueError(f"could not detect date/price columns in benchmark frame: {list(df.columns)}")

    out = df[[date_col, price_col]].copy()
    out.columns = ["date", "price"]
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    out = out.dropna(subset=["date", "price"]).sort_values("date").copy()
    out["date"] = out["date"].dt.strftime("%Y-%m-%d")
    out["benchmark_name"] = benchmark_name
    out["ticker"] = ticker
    return out


def _load_existing_benchmark_fallback(
    output_csv: Path,
    ticker: str,
    benchmark_name: str,
    start_dt: pd.Timestamp,
    end_dt: pd.Timestamp,
) -> tuple[pd.DataFrame | None, dict]:
    candidates: list[Path] = []
    if output_csv.exists():
        candidates.append(output_csv)
    candidates.extend(sorted(output_csv.parent.glob(f"benchmark__ticker={ticker}__target=*.csv")))

    for p in candidates:
        try:
            temp = pd.read_csv(p)
            out = _normalize_benchmark_frame(temp, ticker=ticker, benchmark_name=benchmark_name)
            out["date"] = pd.to_datetime(out["date"], errors="coerce")
            out = out[(out["date"] >= start_dt) & (out["date"] <= end_dt)].copy()
            if len(out) == 0:
                continue
            out["date"] = out["date"].dt.strftime("%Y-%m-%d")
            return out, {
                "fallback_source_detail": "existing_benchmark_csv",
                "fallback_source_path": str(p),
            }
        except Exception:
            continue
    return None, {}


def _load_prices_raw_fallback(
    ticker: str,
    benchmark_name: str,
    start_dt: pd.Timestamp,
    end_dt: pd.Timestamp,
) -> tuple[pd.DataFrame | None, dict]:
    processed = Path("data/processed")
    cands = sorted(processed.glob("prices_raw__src=pykrx__start=*__asof=*__freq=d__v=*.parquet"))
    for p in reversed(cands):
        try:
            px = pd.read_parquet(p)
        except Exception:
            continue

        tcol = next((c for c in ["ticker", "code", "symbol"] if c in px.columns), None)
        dcol = next((c for c in ["date", "Date", "dt", "ymd", "trd_date"] if c in px.columns), None)
        pcol = next((c for c in ["close", "Close", "adj_close", "price"] if c in px.columns), None)
        if not tcol or not dcol or not pcol:
            continue

        temp = px[[tcol, dcol, pcol]].copy()
        temp.columns = ["ticker", "date", "price"]
        temp["ticker"] = temp["ticker"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
        temp["date"] = pd.to_datetime(temp["date"], errors="coerce")
        temp["price"] = pd.to_numeric(temp["price"], errors="coerce")
        temp = temp.dropna(subset=["ticker", "date", "price"]).copy()
        temp = temp[temp["ticker"] == ticker].copy()
        temp = temp[(temp["date"] >= start_dt) & (temp["date"] <= end_dt)].copy()
        if len(temp) == 0:
            continue

        out = temp[["date", "price"]].sort_values("date").copy()
        out["date"] = out["date"].dt.strftime("%Y-%m-%d")
        out["benchmark_name"] = benchmark_name
        out["ticker"] = ticker
        return out, {
            "fallback_source_detail": "prices_raw_parquet",
            "fallback_source_path": str(p),
        }
    return None, {}


def _fetch_live_benchmark(
    ticker: str,
    benchmark_name: str,
    start_dt: pd.Timestamp,
    end_dt: pd.Timestamp,
) -> tuple[pd.DataFrame | None, Exception | None]:
    try:
        from pykrx import stock
    except Exception as e:
        raise RuntimeError("pykrx is required for benchmark generation") from e

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
        return None, last_err

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
    out["benchmark_name"] = benchmark_name
    out["ticker"] = ticker
    return out, None


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

    ticker = str(args.ticker).zfill(6)
    out_path = Path(args.output_csv)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    meta = {
        "ticker": ticker,
        "benchmark_name": args.benchmark_name,
        "start": start_dt.strftime("%Y-%m-%d"),
        "end": end_dt.strftime("%Y-%m-%d"),
        "output_csv": str(out_path),
        "source": "live",
        "provisional": False,
        "collection_status": "live_success",
        "live_error": None,
        "fallback_source_detail": None,
        "fallback_source_path": None,
    }

    out, live_err = _fetch_live_benchmark(ticker, args.benchmark_name, start_dt, end_dt)
    if out is None or len(out) == 0:
        meta["source"] = "fallback"
        meta["provisional"] = True
        meta["collection_status"] = "fallback_success"
        meta["live_error"] = repr(live_err) if live_err else "live fetch returned empty"
        print(f"[WARN] live benchmark fetch failed for ticker={ticker}; trying local fallback. live_error={meta['live_error']}")

        out, fb_meta = _load_existing_benchmark_fallback(out_path, ticker, args.benchmark_name, start_dt, end_dt)
        if out is None or len(out) == 0:
            out, fb_meta = _load_prices_raw_fallback(ticker, args.benchmark_name, start_dt, end_dt)
        if out is None or len(out) == 0:
            meta["collection_status"] = "failed_no_fallback"
            meta_path = _write_meta(out_path, meta)
            print(f"[OK] benchmark meta: {meta_path}")
            raise RuntimeError(
                f"failed to fetch benchmark data for ticker={ticker}; "
                f"live_error={live_err}; no local fallback available"
            )
        meta.update(fb_meta)

    out.to_csv(out_path, index=False, encoding="utf-8-sig")
    meta["rows"] = int(len(out))
    meta["first_date"] = str(out["date"].iloc[0])
    meta["last_date"] = str(out["date"].iloc[-1])
    meta_path = _write_meta(out_path, meta)

    print(f"[OK] benchmark: {out_path}")
    print(f"[OK] benchmark meta: {meta_path}")
    print(f"[INFO] rows: {len(out)}")
    print(f"[INFO] start: {out['date'].iloc[0]}")
    print(f"[INFO] end: {out['date'].iloc[-1]}")
    print(f"[INFO] ticker: {ticker}")
    print(f"[INFO] benchmark_name: {args.benchmark_name}")
    print(f"[INFO] source: {meta['source']}")
    print(f"[INFO] provisional: {meta['provisional']}")


if __name__ == "__main__":
    main()
