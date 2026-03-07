# scripts/make_prices_daily.py
import argparse
from pathlib import Path
import pandas as pd
from pykrx import stock
import datetime as dt

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--start", default="20160101")
    ap.add_argument("--end", default=None)
    ap.add_argument("--universe_v", default="1")
    ap.add_argument("--out_v", default="1")
    args = ap.parse_args()

    if args.end is None:
        args.end = dt.datetime.today().strftime("%Y%m%d")

    p_uni = Path(rf"data/processed/universe__asof={args.asof}__metric={args.metric}__v={args.universe_v}.csv")
    out   = Path(rf"data/processed/prices_daily__src=pykrx__start={args.start}__asof={args.asof}__metric={args.metric}__v={args.out_v}.parquet")

    # --- fallback: if expected universe file missing, pick latest universe*asof*.csv ---
    if not p_uni.exists():
        cand = sorted(
            Path("data/processed").glob(f"universe*{args.asof}*.csv"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if not cand:
            raise FileNotFoundError(p_uni)
        print(f"[WARN] universe not found at expected path. Using fallback: {cand[0]}")
        p_uni = cand[0]

    univ = pd.read_csv(p_uni, dtype={"ticker": str})
    tickers = sorted(univ["ticker"].astype(str).str.zfill(6).unique())

    rows = []
    failed = []

    for i, t in enumerate(tickers, 1):
        try:
            df = stock.get_market_ohlcv_by_date(args.start, args.end, t)
            if df.empty:
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

            keep = ["date", "Open", "High", "Low", "Close", "Volume", "ticker"]
            df = df[keep]
            rows.append(df)

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
    print("rows:", len(px), "tickers:", px["ticker"].nunique())

    if failed:
        fail_path = out.with_suffix(".failed.csv")
        pd.DataFrame(failed, columns=["ticker","error"]).to_csv(fail_path, index=False)
        print("[WARN] failed tickers:", len(failed))

if __name__ == "__main__":
    main()