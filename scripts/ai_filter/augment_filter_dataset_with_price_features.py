#!/usr/bin/env python
from __future__ import annotations

import argparse
import re
from pathlib import Path

import numpy as np
import pandas as pd


def normalize_ticker(s: pd.Series) -> pd.Series:
    return s.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)


def _extract_asof_from_name(name: str) -> str | None:
    m = re.search(r"__asof=(\d{4}-\d{2}-\d{2})__", name)
    return m.group(1) if m else None


def pick_price_path(asof: str, metric: str, px_v: int) -> Path:
    root = Path("data/processed")

    exact = root / f"prices_daily__src=pykrx__start=20160101__asof={asof}__metric={metric}__v={px_v}.parquet"
    if exact.exists():
        return exact

    cands = sorted(root.glob(f"prices_daily__src=pykrx__start=20160101__asof=*__metric={metric}__v={px_v}.parquet"))
    eligible: list[tuple[str, Path]] = []
    for p in cands:
        a = _extract_asof_from_name(p.name)
        if a and a <= asof:
            eligible.append((a, p))

    if not eligible:
        raise FileNotFoundError(
            f"prices_daily parquet not found for asof<={asof}, metric={metric}, v={px_v}"
        )

    eligible.sort(key=lambda x: x[0])
    picked = eligible[-1][1]
    print(f"[WARN] no exact prices_daily for asof={asof}; using latest <= asof: {picked.name}")
    return picked


def load_prices(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path).copy()

    cols = list(df.columns)
    ticker_col = next((c for c in ["ticker", "code", "종목코드", "symbol"] if c in cols), None)
    date_col = next((c for c in ["date", "Date", "dt", "ymd", "trd_date"] if c in cols), None)
    price_col = next((c for c in ["close", "Close", "adj_close", "price"] if c in cols), None)

    if ticker_col is None or date_col is None or price_col is None:
        raise ValueError(f"Could not detect ticker/date/price columns from: {cols}")

    out = df[[ticker_col, date_col, price_col]].copy()
    out.columns = ["ticker", "date", "price"]
    out["ticker"] = normalize_ticker(out["ticker"])
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    out = out.dropna(subset=["ticker", "date", "price"]).copy()
    out = out.sort_values(["ticker", "date"]).reset_index(drop=True)
    return out


def add_price_features(px: pd.DataFrame) -> pd.DataFrame:
    g = px.copy()
    g = g.sort_values(["ticker", "date"]).reset_index(drop=True)

    for win in [21, 63, 126, 252]:
        g[f"ret_{win}d"] = g.groupby("ticker")["price"].transform(
            lambda s: s / s.shift(win) - 1.0
        )

    g["ret_1d"] = g.groupby("ticker")["price"].pct_change()

    for win in [21, 63]:
        g[f"vol_{win}d"] = g.groupby("ticker")["ret_1d"].transform(
            lambda s: s.rolling(win, min_periods=max(10, win // 2)).std()
        )

    def trailing_mdd(price: pd.Series, win: int) -> pd.Series:
        roll_max = price.rolling(win, min_periods=max(10, win // 2)).max()
        dd = price / roll_max - 1.0
        return dd.rolling(win, min_periods=max(10, win // 2)).min()

    for win in [63, 126]:
        g[f"mdd_{win}d"] = g.groupby("ticker")["price"].transform(
            lambda s: trailing_mdd(s, win)
        )

    keep = [
        "ticker", "date",
        "ret_21d", "ret_63d", "ret_126d", "ret_252d",
        "vol_21d", "vol_63d",
        "mdd_63d", "mdd_126d",
    ]
    return g[keep].copy()


def _clean_merged_ticker_columns(df: pd.DataFrame, fallback_ticker: str) -> pd.DataFrame:
    out = df.copy()

    if "ticker" not in out.columns:
        if "ticker_x" in out.columns:
            out = out.rename(columns={"ticker_x": "ticker"})
        elif "ticker_y" in out.columns:
            out = out.rename(columns={"ticker_y": "ticker"})
        else:
            out["ticker"] = fallback_ticker

    if "ticker_y" in out.columns:
        out = out.drop(columns=["ticker_y"])

    if "ticker_x" in out.columns and "ticker" in out.columns and "ticker_x" != "ticker":
        out = out.drop(columns=["ticker_x"])

    out["ticker"] = normalize_ticker(out["ticker"])
    return out


def asof_merge_features(ds: pd.DataFrame, feat_px: pd.DataFrame) -> pd.DataFrame:
    """
    pandas.merge_asof는 정렬 조건에 민감해서 by+ticker를 한 번에 처리할 때
    오류가 날 수 있다. 가장 안전하게 ticker별로 merge_asof 후 concat한다.
    """
    left = ds.copy()
    left["rebalance_month"] = pd.to_datetime(left["rebalance_month"], errors="coerce")
    left["ticker"] = normalize_ticker(left["ticker"])
    left = left.dropna(subset=["ticker", "rebalance_month"]).copy()

    right = feat_px.copy()
    right["date"] = pd.to_datetime(right["date"], errors="coerce")
    right["ticker"] = normalize_ticker(right["ticker"])
    right = right.dropna(subset=["ticker", "date"]).copy()

    out_chunks: list[pd.DataFrame] = []

    left_tickers = set(left["ticker"].unique())
    right_tickers = set(right["ticker"].unique())

    common_tickers = sorted(left_tickers & right_tickers)
    only_left = sorted(left_tickers - right_tickers)

    for tk in common_tickers:
        ltk = left.loc[left["ticker"] == tk].copy()
        rtk = right.loc[right["ticker"] == tk].copy()

        ltk = ltk.sort_values("rebalance_month").reset_index(drop=True)
        rtk = rtk.sort_values("date").reset_index(drop=True)

        merged = pd.merge_asof(
            ltk,
            rtk,
            left_on="rebalance_month",
            right_on="date",
            direction="backward",
            allow_exact_matches=True,
        )
        merged = _clean_merged_ticker_columns(merged, fallback_ticker=tk)
        out_chunks.append(merged)

    for tk in only_left:
        ltk = left.loc[left["ticker"] == tk].copy()
        ltk["date"] = pd.NaT
        for c in ["ret_21d", "ret_63d", "ret_126d", "ret_252d", "vol_21d", "vol_63d", "mdd_63d", "mdd_126d"]:
            ltk[c] = np.nan
        ltk["ticker"] = normalize_ticker(ltk["ticker"])
        out_chunks.append(ltk)

    if not out_chunks:
        raise RuntimeError("No rows to merge in asof_merge_features().")

    out = pd.concat(out_chunks, ignore_index=True)

    if "ticker" not in out.columns:
        raise RuntimeError("ticker column missing after asof merge.")

    if "rebalance_month" not in out.columns:
        raise RuntimeError("rebalance_month column missing after asof merge.")

    out["ticker"] = normalize_ticker(out["ticker"])
    out["rebalance_month"] = pd.to_datetime(out["rebalance_month"], errors="coerce")
    out = out.sort_values(["rebalance_month", "ticker"]).reset_index(drop=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset_path", required=True)
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--px_v", type=int, default=1)
    ap.add_argument("--out_path", default="")
    args = ap.parse_args()

    ds_path = Path(args.dataset_path)
    if not ds_path.exists():
        raise FileNotFoundError(f"dataset not found: {ds_path}")

    ds = pd.read_parquet(ds_path).copy()
    if "ticker" not in ds.columns or "rebalance_month" not in ds.columns:
        raise ValueError("dataset must contain ticker and rebalance_month")

    px_path = pick_price_path(args.asof, args.metric, args.px_v)
    px = load_prices(px_path)
    feat_px = add_price_features(px)

    out = asof_merge_features(ds, feat_px)

    feature_cols = [
        "ret_21d", "ret_63d", "ret_126d", "ret_252d",
        "vol_21d", "vol_63d", "mdd_63d", "mdd_126d",
    ]
    miss_stats = {
        c: float(out[c].isna().mean())
        for c in feature_cols
        if c in out.columns
    }

    if args.out_path:
        out_path = Path(args.out_path)
    else:
        stem = ds_path.stem + "__pricefeat"
        out_path = ds_path.with_name(stem + ds_path.suffix)

    out.to_parquet(out_path, index=False)

    print(f"[OK] dataset_in : {ds_path}")
    print(f"[OK] prices     : {px_path}")
    print(f"[OK] dataset_out: {out_path}")
    print(f"[INFO] rows={len(out)}")
    print("[INFO] missing_rate")
    for k, v in miss_stats.items():
        print(f"  - {k}: {v:.4f}")


if __name__ == "__main__":
    main()