#!/usr/bin/env python
# scripts/make_returns_monthly_fdr.py
from __future__ import annotations

import argparse
from pathlib import Path
import pandas as pd

def _yyyymmdd(s: str) -> str:
    s = str(s).strip()
    if "-" in s:
        return s.replace("-", "")
    if len(s) == 8 and s.isdigit():
        return s
    raise ValueError(f"bad date: {s}")

def _to_month_end(dt: pd.Timestamp) -> pd.Timestamp:
    # month end normalize
    return (dt + pd.offsets.MonthEnd(0)).normalize()

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True, help="YYYY-MM-DD")
    ap.add_argument("--metric", required=True)
    ap.add_argument("--feat_v", type=int, required=True, help="features_live v")
    ap.add_argument("--out_v", type=int, default=1, help="returns_monthly v")
    ap.add_argument("--start", default="20160101", help="YYYYMMDD")
    ap.add_argument("--end", default=None, help="YYYYMMDD (default=asof)")
    ap.add_argument("--min_days", type=int, default=200, help="min daily rows per ticker to keep")
    args = ap.parse_args()

    try:
        import FinanceDataReader as fdr
    except Exception as e:
        raise RuntimeError("FinanceDataReader is required. Run: pip install FinanceDataReader") from e

    asof_ymd = _yyyymmdd(args.asof)
    start_ymd = _yyyymmdd(args.start)
    end_ymd = _yyyymmdd(args.end) if args.end else asof_ymd

    # 1) tickers from features_live (우리가 지금 가진 '사실상 유니버스')
    p_feat = Path(f"data/processed/features_live__asof={args.asof}__metric={args.metric}__v={args.feat_v}.parquet")
    if not p_feat.exists():
        raise FileNotFoundError(p_feat)

    feat = pd.read_parquet(p_feat, columns=["ticker"])
    tickers = sorted({str(x) for x in feat["ticker"].dropna().astype(str).tolist()})
    if not tickers:
        raise RuntimeError("No tickers found in features_live.")

    print(f"[INFO] tickers from features_live: {len(tickers)}")

    # 2) download daily close via FDR and compute month-end returns
    out_rows = []
    bad = 0

    for i, t in enumerate(tickers, 1):
        # FDR: KRX 종목코드는 보통 '005930' 형태로 동작
        try:
            df = fdr.DataReader(t, start_ymd, end_ymd)
        except Exception:
            bad += 1
            continue

        if df is None or len(df) < args.min_days:
            bad += 1
            continue

        # normalize
        df = df.reset_index()
        # column name tolerance
        if "Date" in df.columns:
            df = df.rename(columns={"Date": "date"})
        elif "날짜" in df.columns:
            df = df.rename(columns={"날짜": "date"})
        else:
            df = df.rename(columns={df.columns[0]: "date"})

        # close column
        if "Close" in df.columns:
            close_col = "Close"
        elif "종가" in df.columns:
            close_col = "종가"
        elif "close" in df.columns:
            close_col = "close"
        else:
            # cannot find close
            bad += 1
            continue

        df["date"] = pd.to_datetime(df["date"])
        df["close"] = pd.to_numeric(df[close_col], errors="coerce")
        df = df.dropna(subset=["close"]).sort_values("date")

        if len(df) < args.min_days:
            bad += 1
            continue

        df["month"] = df["date"].apply(_to_month_end)
        m = df.groupby("month", as_index=False)["close"].last()
        m["ticker"] = t
        m.sort_values(by=["ticker", "month"], inplace=True)
        m["ret"] = m.groupby("ticker")["close"].pct_change()

        out_rows.append(m[["ticker", "month", "ret"]])

        if i % 50 == 0:
            print(f"[INFO] {i}/{len(tickers)} done (bad={bad})")

    if not out_rows:
        raise RuntimeError("No returns generated. (FDR may be blocked or all tickers failed)")

    ret = pd.concat(out_rows, ignore_index=True)

    # ---- schema normalize: month_end + ret_1m ----
    ret = ret.rename(columns={"month": "month_end", "ret": "ret_1m"})
    ret["month_end"] = pd.to_datetime(ret["month_end"]).dt.normalize()
    ret["ret_1m"] = pd.to_numeric(ret["ret_1m"], errors="coerce")

    # keep canonical columns only (불필요한 흔들림 방지)
    ret = ret[["ticker", "month_end", "ret_1m"]].dropna(subset=["ret_1m"]).reset_index(drop=True)

    out = Path(f"data/processed/returns_monthly__src=fdr__asof={args.asof}__metric={args.metric}__v={args.out_v}.parquet")
    out.parent.mkdir(parents=True, exist_ok=True)
    ret.to_parquet(out, index=False)

    # ALSO save a copy with the filename that backtest expects (src=pykrx)
    out_expected = Path(f"data/processed/returns_monthly__src=pykrx__asof={args.asof}__metric={args.metric}__v={args.out_v}.parquet")
    ret.to_parquet(out_expected, index=False)

    print(f"[OK] saved: {out}")
    print(f"[OK] saved (expected path): {out_expected}")
    print(f"[INFO] rows={len(ret)} tickers={ret['ticker'].nunique()} bad={bad} months={ret['month_end'].min()}..{ret['month_end'].max()}")

if __name__ == "__main__":
    main()