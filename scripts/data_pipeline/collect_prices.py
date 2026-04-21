# >>> ACTIVE_FALLBACK (PROMOTED TO data_pipeline; KEEP)
#!/usr/bin/env python
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pandas as pd
from pykrx import stock

# ---- path bootstrap ----
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# ------------------------


def normalize_ticker_series(s: pd.Series) -> pd.Series:
    return s.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)


def _extract_asof_from_name(name: str) -> str | None:
    m = re.search(r"__asof=(\d{4}-\d{2}-\d{2})__", name)
    return m.group(1) if m else None


def _read_any_table(p: Path) -> pd.DataFrame:
    suf = p.suffix.lower()
    if suf == ".csv":
        return pd.read_csv(p)
    if suf in {".xlsx", ".xls"}:
        return pd.read_excel(p)
    return pd.read_parquet(p)


def _collect_union_tickers(asof: str, metric: str) -> list[str]:
    root = Path("data/processed")
    frames: list[pd.DataFrame] = []

    patterns = [
        "krx_master__asof=*__src=pykrx__v=*.parquet",
        "krx_marketdata__asof=*__src=pykrx__lookback=365d__v=*.parquet",
        "fundamentals_quarterly__asof=*__src=dart__fs=CFS__y=2016-2025__v=*.parquet",
        f"universe__asof=*__metric={metric}__v=*.csv",
        f"universe__asof=*__metric={metric}__v=*.parquet",
        f"features_live__asof=*__metric={metric}__v=*.parquet",
        "shares_industry__asof=*__src=dart__*.parquet",
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

    # optional old path
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
        return []

    out = pd.concat(frames, ignore_index=True)
    out = out.dropna(subset=["ticker"]).drop_duplicates("ticker")
    return sorted(out["ticker"].tolist())


def _normalize_ohlcv_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    pykrx get_market_ohlcv_by_date() result normalization.
    Handles both normal Korean column names and mojibake/broken names by:
    1) renaming known variants
    2) falling back to positional assignment
    """
    df = df.copy()

    # first column is always date-like after reset_index()
    if len(df.columns) > 0 and "date" not in df.columns:
        df = df.rename(columns={df.columns[0]: "date"})

    rename_map = {
        "날짜": "date",
        "Date": "date",
        "시가": "Open",
        "고가": "High",
        "저가": "Low",
        "종가": "Close",
        "거래량": "Volume",
        "Open": "Open",
        "High": "High",
        "Low": "Low",
        "Close": "Close",
        "Volume": "Volume",
    }
    df = df.rename(columns={c: rename_map[c] for c in df.columns if c in rename_map})

    required = ["date", "Open", "High", "Low", "Close", "Volume"]
    if all(c in df.columns for c in required):
        return df

    # positional fallback:
    # after reset_index(), typical order is:
    # [date, open, high, low, close, volume, value, change]
    if len(df.columns) >= 6:
        col0 = df.columns[0]
        numeric_candidates = [c for c in df.columns[1:] if c != "ticker"]

        if len(numeric_candidates) >= 5:
            pos_map = {
                col0: "date",
                numeric_candidates[0]: "Open",
                numeric_candidates[1]: "High",
                numeric_candidates[2]: "Low",
                numeric_candidates[3]: "Close",
                numeric_candidates[4]: "Volume",
            }
            df = df.rename(columns=pos_map)

    return df


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--freq", default="d", choices=["d"])
    ap.add_argument("--lookback_years", type=int, default=15)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--out_v", type=int, default=1)
    args = ap.parse_args()

    if args.freq != "d":
        raise ValueError("Only daily freq supported.")

    asof = args.asof
    asof_ymd = asof.replace("-", "")
    start_ymd = f"{int(asof[:4]) - args.lookback_years}0101"

    out = Path(
        f"data/processed/prices_raw__src=pykrx__start={start_ymd}__asof={asof}__freq=d__v={args.out_v}.parquet"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    tickers = _collect_union_tickers(asof, args.metric)
    # ---- benchmark ETF 강제 포함 ----
    BENCHMARK_TICKERS = ["069500"]  # KODEX 200
    tickers = sorted(set(tickers) | set(BENCHMARK_TICKERS))
    # --------------------------------
    if not tickers:
        raise RuntimeError("No union tickers available for collect_prices.")

    print(f"[INFO] union tickers: {len(tickers)}")

    rows: list[pd.DataFrame] = []
    failed: list[tuple[str, str]] = []

    for i, t in enumerate(tickers, 1):
        try:
            df = stock.get_market_ohlcv_by_date(start_ymd, asof_ymd, t)
            if df is None or len(df) == 0:
                failed.append((t, "empty"))
                continue

            df = df.reset_index()
            df["ticker"] = t
            df = _normalize_ohlcv_columns(df)

            if "date" not in df.columns:
                df = df.rename(columns={df.columns[0]: "date"})

            keep = [c for c in ["date", "Open", "High", "Low", "Close", "Volume", "ticker"] if c in df.columns]
            if not {"date", "Close", "ticker"}.issubset(set(keep)):
                failed.append((t, f"normalized columns missing: {list(df.columns)}"))
                continue

            rows.append(df[keep].copy())

        except Exception as e:
            failed.append((t, repr(e)))

        if i % 50 == 0:
            print(f"[INFO] {i}/{len(tickers)} done")

    if not rows:
        raise RuntimeError("No price rows collected.")

    px = pd.concat(rows, ignore_index=True)
    px["date"] = pd.to_datetime(px["date"])
    px = px.sort_values(["ticker", "date"])

    px.to_parquet(out, index=False)
    print(f"[OK] saved: {out}")
    print(f"[INFO] rows={len(px)} tickers={px['ticker'].nunique()} date_max={px['date'].max()}")

    if failed:
        fail_path = out.with_suffix(".failed.csv")
        pd.DataFrame(failed, columns=["ticker", "error"]).to_csv(
            fail_path, index=False, encoding="utf-8-sig"
        )
        print(f"[WARN] failed tickers={len(failed)} -> {fail_path}")


if __name__ == "__main__":
    main()
