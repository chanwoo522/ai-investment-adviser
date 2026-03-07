from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd

from scripts.krx_safe import backoff_date_call


def _yyyymmdd(asof: str) -> str:
    return asof.replace("-", "")


def _norm_ticker_col(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["ticker"])
    if "ticker" in df.columns:
        out = df.copy()
        out["ticker"] = out["ticker"].astype(str).str.zfill(6)
        return out
    for c in ["code", "종목코드", "티커", "stock_code"]:
        if c in df.columns:
            out = df.rename(columns={c: "ticker"}).copy()
            out["ticker"] = out["ticker"].astype(str).str.zfill(6)
            return out
    out = df.reset_index().copy()
    if "ticker" not in out.columns:
        out = out.rename(columns={out.columns[0]: "ticker"})
    out["ticker"] = out["ticker"].astype(str).str.zfill(6)
    return out


def _fetch_mcap_pykrx(ymd: str) -> pd.DataFrame:
    from pykrx import stock

    df = stock.get_market_cap_by_ticker(ymd, market="ALL")
    df = _norm_ticker_col(df)

    mcol = next((c for c in ["시가총액", "MarketCap", "market_cap", "mcap", "MKT_CAP"] if c in df.columns), None)
    if mcol is None:
        raise RuntimeError(f"pykrx returned without market cap column. cols={list(df.columns)}")

    out = df[["ticker", mcol]].rename(columns={mcol: "mcap"}).copy()
    out["mcap"] = pd.to_numeric(out["mcap"], errors="coerce")
    return out


def _find_latest_marketdata(processed_dir: Path) -> Path | None:
    cands = list(processed_dir.glob("krx_marketdata__asof=*_*.parquet"))
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime)
    return cands[-1]


def _load_universe_tickers(processed_dir: Path, asof: str) -> pd.DataFrame:
    cands = sorted(processed_dir.glob(f"universe__asof={asof}__*.csv")) + sorted(
        processed_dir.glob(f"universe__asof={asof}__*.parquet")
    )
    if not cands:
        cands = sorted(processed_dir.glob("universe__asof=*_*.csv")) + sorted(
            processed_dir.glob("universe__asof=*_*.parquet")
        )
    if not cands:
        return pd.DataFrame(columns=["ticker"])

    p = cands[-1]
    df = pd.read_csv(p) if p.suffix.lower() == ".csv" else pd.read_parquet(p)
    df = _norm_ticker_col(df)
    return df[["ticker"]].drop_duplicates()


def _find_latest_shares_industry(processed_dir: Path, asof: str) -> Path | None:
    cands = list(processed_dir.glob(f"shares_industry__asof={asof}__*.parquet"))
    if not cands:
        cands = list(processed_dir.glob("shares_industry__asof=*_*.parquet"))
    if not cands:
        return None
    cands.sort(key=lambda p: p.stat().st_mtime)
    return cands[-1]


def _merge_shares_industry(out: pd.DataFrame, processed_dir: Path, asof: str) -> pd.DataFrame:
    p = _find_latest_shares_industry(processed_dir, asof)
    out2 = _norm_ticker_col(out.copy())

    if p is None:
        if "shares" not in out2.columns:
            out2["shares"] = pd.NA
        if "industry4" not in out2.columns:
            out2["industry4"] = pd.NA
        return out2

    s = pd.read_parquet(p)
    s = _norm_ticker_col(s)

    shares_col = next((c for c in ["shares", "listed_shares", "lstg_stkco", "share_cnt"] if c in s.columns), None)
    ind_col = next((c for c in ["industry4", "induty_code", "industry", "krx_industry", "sector"] if c in s.columns), None)

    keep = ["ticker"]
    if shares_col:
        keep.append(shares_col)
    if ind_col and ind_col not in keep:
        keep.append(ind_col)

    sm = s[keep].drop_duplicates("ticker").copy()

    ren = {}
    if shares_col and shares_col != "shares":
        ren[shares_col] = "shares"
    if ind_col and ind_col != "industry4":
        ren[ind_col] = "industry4"
    if ren:
        sm = sm.rename(columns=ren)

    out2 = out2.merge(sm, on="ticker", how="left", suffixes=("", "_si"))

    if "shares_si" in out2.columns:
        if "shares" not in out2.columns:
            out2["shares"] = pd.NA
        out2["shares"] = out2["shares_si"].combine_first(out2["shares"])
        out2 = out2.drop(columns=["shares_si"])

    if "industry4_si" in out2.columns:
        if "industry4" not in out2.columns:
            out2["industry4"] = pd.NA
        out2["industry4"] = out2["industry4_si"].combine_first(out2["industry4"])
        out2 = out2.drop(columns=["industry4_si"])

    if "shares" not in out2.columns:
        out2["shares"] = pd.NA
    if "industry4" not in out2.columns:
        out2["industry4"] = pd.NA

    return out2


def _find_prices_daily(processed_dir: Path, asof: str, metric: str, prices_v: int) -> Path:
    p = processed_dir / f"prices_daily__src=pykrx__start=20160101__asof={asof}__metric={metric}__v={prices_v}.parquet"
    if p.exists():
        return p
    cands = list(processed_dir.glob(f"prices_daily__src=pykrx__start=20160101__asof={asof}__metric={metric}__v=*.parquet"))
    if not cands:
        cands = list(processed_dir.glob("prices_daily__src=pykrx__start=20160101__asof=*_metric=*_v=*.parquet"))
    if not cands:
        raise FileNotFoundError("prices_daily parquet not found under data/processed")
    cands.sort(key=lambda p: p.stat().st_mtime)
    return cands[-1]


def _synth_mcap_from_prices_and_shares(
    out: pd.DataFrame,
    processed_dir: Path,
    asof: str,
    metric: str,
    prices_v: int,
    max_back_days: int,
) -> pd.DataFrame:
    out2 = _norm_ticker_col(out.copy())

    if "shares" not in out2.columns:
        out2["used_px_date"] = None
        return out2

    ppx = _find_prices_daily(processed_dir, asof, metric, prices_v)
    px = pd.read_parquet(ppx)
    px["ticker"] = px["ticker"].astype(str).str.zfill(6)
    px["date"] = pd.to_datetime(px["date"])

    target = pd.Timestamp(asof)
    lo = target - pd.Timedelta(days=int(max_back_days))
    cand = px.loc[(px["date"] <= target) & (px["date"] >= lo), "date"].max()

    if pd.isna(cand):
        out2["used_px_date"] = None
        return out2

    sub = px.loc[px["date"] == cand, ["ticker", "Close"]].copy()

    # avoid Close_x / Close_y confusion
    if "Close" in out2.columns:
        out2 = out2.drop(columns=["Close"])

    out2 = out2.merge(sub, on="ticker", how="left")

    if "mcap" not in out2.columns:
        out2["mcap"] = pd.NA

    mask = out2["mcap"].isna() & out2["Close"].notna() & out2["shares"].notna()
    out2.loc[mask, "mcap"] = out2.loc[mask, "Close"].astype(float) * out2.loc[mask, "shares"].astype(float)

    out2["market_cap"] = out2["mcap"]
    out2["used_px_date"] = cand.strftime("%Y-%m-%d")
    return out2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--lookback", type=int, default=365)
    ap.add_argument("--out_v", type=int, required=True)
    ap.add_argument("--src", default="pykrx")
    ap.add_argument("--soft_fail", action="store_true")
    ap.add_argument("--max_back_days", type=int, default=14)
    ap.add_argument("--prices_v", type=int, default=1)
    ap.add_argument("--synth_mcap", action="store_true")
    args = ap.parse_args()

    processed_dir = Path("data/processed")
    processed_dir.mkdir(parents=True, exist_ok=True)

    asof_ymd = _yyyymmdd(args.asof)
    mcap = None
    used_ymd = asof_ymd

    try:
        used_ymd, mcap = backoff_date_call(_fetch_mcap_pykrx, asof_ymd, max_back_days=args.max_back_days)
        print(f"[OK] pykrx mcap fetched: used_ymd={used_ymd} rows={len(mcap)}")
    except Exception as e:
        print(f"[WARN] pykrx mcap failed: {e}")

        latest = _find_latest_marketdata(processed_dir)
        if latest is not None:
            try:
                fb = pd.read_parquet(latest)
                fb = _norm_ticker_col(fb)
                if "mcap" not in fb.columns and "market_cap" in fb.columns:
                    fb = fb.rename(columns={"market_cap": "mcap"})
                if "mcap" in fb.columns:
                    mcap = fb.copy()
                    if "asof_ymd" in fb.columns and len(fb):
                        used_ymd = str(fb["asof_ymd"].iloc[0])
                    print(f"[OK] fallback marketdata used: {latest}")
            except Exception as e2:
                print(f"[WARN] fallback marketdata load failed: {e2}")

        if mcap is None:
            uni = _load_universe_tickers(processed_dir, args.asof)
            mcap = uni.copy()
            mcap["mcap"] = pd.NA
            print(f"[WARN] mcap unavailable; created NaN mcap from universe tickers: n={len(mcap)}")

        if not args.soft_fail:
            raise

    out = _norm_ticker_col(mcap.copy())
    out["asof_ymd"] = used_ymd
    out["created_at"] = datetime.now().isoformat(timespec="seconds")

    out = _merge_shares_industry(out, processed_dir, args.asof)

    if args.synth_mcap:
        out = _synth_mcap_from_prices_and_shares(
            out=out,
            processed_dir=processed_dir,
            asof=args.asof,
            metric=args.metric,
            prices_v=args.prices_v,
            max_back_days=args.max_back_days,
        )

    if "market_cap" not in out.columns and "mcap" in out.columns:
        out["market_cap"] = out["mcap"]

    outp = processed_dir / f"krx_marketdata__asof={args.asof}__src={args.src}__lookback={args.lookback}d__v={args.out_v}.parquet"
    out.to_parquet(outp, index=False)

    used_px_date = out["used_px_date"].iloc[0] if "used_px_date" in out.columns and len(out) else None
    mcap_null = float(out["mcap"].isna().mean()) if "mcap" in out.columns else 1.0
    shares_null = float(out["shares"].isna().mean()) if "shares" in out.columns else 1.0
    ind_null = float(out["industry4"].isna().mean()) if "industry4" in out.columns else 1.0

    print(f"[OK] saved: {outp}")
    print(
        f"[INFO] rows={len(out)} tickers={out['ticker'].nunique(dropna=True)} "
        f"mcap_null_ratio={mcap_null:.3f} shares_null_ratio={shares_null:.3f} "
        f"industry_null_ratio={ind_null:.3f} used_px_date={used_px_date}"
    )


if __name__ == "__main__":
    main()