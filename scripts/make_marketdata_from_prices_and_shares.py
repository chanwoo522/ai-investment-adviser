from __future__ import annotations

import argparse
from pathlib import Path
import pandas as pd


def _tag(asof: str) -> str:
    return asof.replace("-", "")


def _pick_col(df: pd.DataFrame, cands: list[str]) -> str:
    for c in cands:
        if c in df.columns:
            return c
    raise ValueError(f"none of columns exist: {cands}. have={list(df.columns)[:50]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)                  # YYYY-MM-DD
    ap.add_argument("--prices_v", type=int, required=True)
    ap.add_argument("--shares_path", required=True)           # parquet/csv containing ticker + shares
    ap.add_argument("--shares_col", default="shares")         # column name for shares in shares file
    ap.add_argument("--out_v", type=int, required=True)
    ap.add_argument("--src", default="synthetic")             # marketdata src tag
    ap.add_argument("--lookback_days", type=int, default=365) # naming only
    args = ap.parse_args()

    root = Path(".")
    p_prices = root / "data/processed" / f"prices_daily__src=pykrx__start=20160101__asof={args.asof}__metric=revenue_op__v={args.prices_v}.parquet"
    if not p_prices.exists():
        raise FileNotFoundError(p_prices)

    prices = pd.read_parquet(p_prices)
    prices["ticker"] = prices["ticker"].astype(str).str.zfill(6)

    # pick last close <= asof (prices file includes dates up to asof)
    c_date = _pick_col(prices, ["date"])
    c_close = _pick_col(prices, ["Close", "close", "종가"])
    prices[c_date] = pd.to_datetime(prices[c_date])
    # latest date per ticker
    last_px = (prices.sort_values([c_date])
                    .groupby("ticker", as_index=False)
                    .tail(1)[["ticker", c_date, c_close]]
                    .rename(columns={c_close: "close"}))

    # load shares
    sp = Path(args.shares_path)
    if not sp.exists():
        raise FileNotFoundError(sp)
    if sp.suffix.lower() == ".csv":
        sh = pd.read_csv(sp)
    else:
        sh = pd.read_parquet(sp)
    sh["ticker"] = sh["ticker"].astype(str).str.zfill(6)
    if args.shares_col not in sh.columns:
        raise ValueError(f"shares_col={args.shares_col} not in shares file. cols={list(sh.columns)}")
    sh = sh[["ticker", args.shares_col]].rename(columns={args.shares_col: "shares"}).drop_duplicates("ticker")
    sh["shares"] = pd.to_numeric(sh["shares"], errors="coerce")

    out = last_px.merge(sh, on="ticker", how="left")
    out["mcap"] = out["close"] * out["shares"]
    out["asof_ymd"] = _tag(args.asof)
    out["src"] = args.src

    outp = root / "data/processed" / f"krx_marketdata__asof={args.asof}__src={args.src}__lookback={args.lookback_days}d__v={args.out_v}.parquet"
    outp.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(outp, index=False)

    print(f"[OK] saved: {outp}")
    print(f"[INFO] rows={len(out)} tickers={out['ticker'].nunique()} mcap_null_ratio={out['mcap'].isna().mean():.3f}")


if __name__ == "__main__":
    main()