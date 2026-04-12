#!/usr/bin/env python
from __future__ import annotations

# ---- path bootstrap ----
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, SCRIPTS_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
# ------------------------

import argparse
import re
from datetime import datetime

import pandas as pd

from krx_safe import backoff_date_call
from common.industry_map import attach_industry, standardize_industry_columns


def _yyyymmdd(asof: str) -> str:
    return asof.replace("-", "")


def _norm_ticker_col(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=["ticker"])

    if "ticker" in df.columns:
        out = df.copy()
        out["ticker"] = out["ticker"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
        return out

    for c in ["code", "종목코드", "티커", "stock_code", "symbol"]:
        if c in df.columns:
            out = df.rename(columns={c: "ticker"}).copy()
            out["ticker"] = out["ticker"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
            return out

    out = df.reset_index().copy()
    if "ticker" not in out.columns:
        out = out.rename(columns={out.columns[0]: "ticker"})
    out["ticker"] = out["ticker"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
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


def _fetch_fundamental_pykrx(ymd: str) -> pd.DataFrame:
    from pykrx import stock

    df = stock.get_market_fundamental_by_ticker(ymd, market="ALL")
    df = _norm_ticker_col(df)

    rename_map: dict[str, str] = {}
    for src, dst in [("PER", "per"), ("PBR", "pbr"), ("EPS", "eps"), ("BPS", "bps"), ("DIV", "div_yield"), ("DPS", "dps")]:
        if src in df.columns:
            rename_map[src] = dst
        elif src.lower() in df.columns:
            rename_map[src.lower()] = dst

    keep = ["ticker"] + list(rename_map.keys())
    out = df[keep].rename(columns=rename_map).copy()
    for c in ["per", "pbr", "eps", "bps", "div_yield", "dps"]:
        if c in out.columns:
            out[c] = pd.to_numeric(out[c], errors="coerce")
    return out


def _find_latest_marketdata(processed_dir: Path) -> Path | None:
    cands = list(processed_dir.glob("krx_marketdata__asof=*.parquet"))
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


def _merge_shares_only(out: pd.DataFrame, processed_dir: Path, asof: str) -> pd.DataFrame:
    p = _find_latest_shares_industry(processed_dir, asof)
    out2 = _norm_ticker_col(out.copy())

    if p is None:
        if "shares" not in out2.columns:
            out2["shares"] = pd.NA
        return standardize_industry_columns(out2)

    s = pd.read_parquet(p)
    s = _norm_ticker_col(s)

    shares_col = next((c for c in ["shares", "listed_shares", "lstg_stkco", "share_cnt"] if c in s.columns), None)
    if shares_col is None:
        if "shares" not in out2.columns:
            out2["shares"] = pd.NA
        return standardize_industry_columns(out2)

    sm = s[["ticker", shares_col]].drop_duplicates("ticker").rename(columns={shares_col: "shares"}).copy()
    out2 = out2.merge(sm, on="ticker", how="left", suffixes=("", "_si"))

    if "shares_si" in out2.columns:
        if "shares" not in out2.columns:
            out2["shares"] = pd.NA
        out2["shares"] = pd.to_numeric(out2["shares_si"], errors="coerce").combine_first(
            pd.to_numeric(out2["shares"], errors="coerce")
        )
        out2 = out2.drop(columns=["shares_si"], errors="ignore")

    if "shares" not in out2.columns:
        out2["shares"] = pd.NA

    return standardize_industry_columns(out2)


def _find_prices_daily(processed_dir: Path, asof: str, metric: str, prices_v: int) -> Path:
    p = processed_dir / f"prices_daily__src=pykrx__start=20160101__asof={asof}__metric={metric}__v={prices_v}.parquet"
    if p.exists():
        return p
    cands = list(processed_dir.glob(f"prices_daily__src=pykrx__start=20160101__asof={asof}__metric={metric}__v=*.parquet"))
    if not cands:
        cands = list(processed_dir.glob(f"prices_daily__src=pykrx__start=20160101__asof=*__metric={metric}__v={prices_v}.parquet"))
    if not cands:
        raise FileNotFoundError("prices_daily parquet not found under data/processed")
    cands.sort(key=lambda p: p.stat().st_mtime)
    return cands[-1]


def _attach_latest_price_stats(
    out: pd.DataFrame,
    processed_dir: Path,
    asof: str,
    metric: str,
    prices_v: int,
    max_back_days: int,
) -> pd.DataFrame:
    out2 = _norm_ticker_col(out.copy())

    dup_cols = [c for c in ["close", "volume", "traded_value", "used_px_date"] if c in out2.columns]
    if dup_cols:
        out2 = out2.drop(columns=dup_cols)

    ppx = _find_prices_daily(processed_dir, asof, metric, prices_v)
    px = pd.read_parquet(ppx)
    px["ticker"] = px["ticker"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
    px["date"] = pd.to_datetime(px["date"])

    close_col = next((c for c in ["Close", "close", "종가"] if c in px.columns), None)
    vol_col = next((c for c in ["Volume", "volume", "거래량"] if c in px.columns), None)
    if close_col is None:
        raise ValueError(f"prices_daily missing close column. cols={list(px.columns)}")

    target = pd.Timestamp(asof)
    lo = target - pd.Timedelta(days=int(max_back_days))

    sub = px.loc[(px["date"] <= target) & (px["date"] >= lo)].copy()
    if len(sub) == 0:
        out2["used_px_date"] = None
        out2["close"] = pd.NA
        out2["volume"] = pd.NA
        out2["traded_value"] = pd.NA
        return out2

    sub = sub.sort_values(["ticker", "date"])
    last = sub.groupby("ticker", as_index=False).tail(1).copy()

    last = last.rename(columns={close_col: "close"})
    if vol_col is not None:
        last = last.rename(columns={vol_col: "volume"})
    else:
        last["volume"] = pd.NA

    last["close"] = pd.to_numeric(last["close"], errors="coerce")
    last["volume"] = pd.to_numeric(last["volume"], errors="coerce")
    last["traded_value"] = last["close"] * last["volume"]
    last["used_px_date"] = last["date"].dt.strftime("%Y-%m-%d")

    last = last[["ticker", "close", "volume", "traded_value", "used_px_date"]].copy()
    out2 = out2.merge(last, on="ticker", how="left")
    return out2


def _synth_mcap_from_prices_and_shares(out: pd.DataFrame) -> pd.DataFrame:
    out2 = _norm_ticker_col(out.copy())

    if "mcap" not in out2.columns:
        out2["mcap"] = pd.NA

    if "shares" in out2.columns and "close" in out2.columns:
        mask = out2["mcap"].isna() & out2["close"].notna() & out2["shares"].notna()
        out2.loc[mask, "mcap"] = pd.to_numeric(out2.loc[mask, "close"], errors="coerce") * pd.to_numeric(
            out2.loc[mask, "shares"], errors="coerce"
        )

    out2["market_cap"] = out2["mcap"]
    return out2


def _coalesce_duplicate_named_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Exact duplicate column name cleanup.
    If the same column name appears multiple times, keep first appearance order
    and combine later duplicates with combine_first().
    """
    if df is None or df.empty:
        return df

    buckets: dict[str, pd.Series] = {}
    order: list[str] = []

    for i, col in enumerate(df.columns):
        s = df.iloc[:, i]
        if col not in buckets:
            buckets[col] = s.copy()
            order.append(col)
        else:
            buckets[col] = buckets[col].combine_first(s)

    out = pd.DataFrame({c: buckets[c] for c in order})
    return out


def _coalesce_suffix_family(df: pd.DataFrame, base: str, suffixes: tuple[str, ...] = ("", "_x", "_y", "_ref", "_si")) -> pd.DataFrame:
    """
    For columns like close / close_x / close_y, create a single base column
    using priority order and drop the suffix variants.
    """
    present = [f"{base}{s}" if s else base for s in suffixes if (f"{base}{s}" if s else base) in df.columns]
    if not present:
        return df

    merged = None
    for col in present:
        s = df[col]
        merged = s.copy() if merged is None else merged.combine_first(s)

    df2 = df.drop(columns=present, errors="ignore").copy()
    df2[base] = merged
    return df2


def _cleanup_marketdata_columns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Final hygiene before parquet write:
    1) remove exact duplicate column names
    2) coalesce known suffix families
    3) one more exact duplicate cleanup
    """
    out = df.copy()

    # Step 1: exact duplicate names
    out = _coalesce_duplicate_named_columns(out)

    # Step 2: suffix families that often appear after fallback / merge chains
    family_bases = [
        "close",
        "volume",
        "traded_value",
        "used_px_date",
        "industry4",
        "industry_code",
        "industry_name",
        "shares",
        "mcap",
        "market_cap",
        "per",
        "pbr",
        "eps",
        "bps",
        "div_yield",
        "dps",
    ]
    for base in family_bases:
        out = _coalesce_suffix_family(out, base)

    # Step 3: clean up any remaining exact duplicates one more time
    out = _coalesce_duplicate_named_columns(out)

    # Make sure ticker stays normalized
    out = _norm_ticker_col(out)

    return out


def main() -> None:
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
    fundamentals = None

    try:
        used_ymd, mcap = backoff_date_call(_fetch_mcap_pykrx, asof_ymd, max_back_days=args.max_back_days)
        print(f"[OK] pykrx mcap fetched: used_ymd={used_ymd} rows={len(mcap)}")
        try:
            used_fund_ymd, fundamentals = backoff_date_call(
                _fetch_fundamental_pykrx, asof_ymd, max_back_days=args.max_back_days
            )
            if used_fund_ymd != used_ymd:
                print(f"[WARN] market_fundamental used different date: {used_fund_ymd} (mcap used {used_ymd})")
            print(f"[OK] pykrx market fundamentals fetched: used_ymd={used_fund_ymd} rows={len(fundamentals)}")
        except Exception as fe:
            print(f"[WARN] pykrx market fundamentals failed: {fe}")
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

        if mcap is None and not args.soft_fail:
            raise

    out = _norm_ticker_col(mcap.copy())
    if fundamentals is not None and len(fundamentals):
        out = out.merge(fundamentals, on="ticker", how="left")
    out["asof_ymd"] = used_ymd
    out["created_at"] = datetime.now().isoformat(timespec="seconds")

    out = _merge_shares_only(out, processed_dir, args.asof)
    out = _attach_latest_price_stats(
        out=out,
        processed_dir=processed_dir,
        asof=args.asof,
        metric=args.metric,
        prices_v=args.prices_v,
        max_back_days=args.max_back_days,
    )

    try:
        out = attach_industry(out, prefer_reference=True)
    except TypeError:
        # backwards compatibility if helper signature differs
        out = attach_industry(out)
    except Exception as e:
        print(f"[WARN] attach_industry failed: {e}")

    out = standardize_industry_columns(out)

    if args.synth_mcap:
        out = _synth_mcap_from_prices_and_shares(out)

    if "market_cap" not in out.columns and "mcap" in out.columns:
        out["market_cap"] = out["mcap"]

    # --- critical fix: merge/fallback duplicate-column hygiene before parquet ---
    out = _cleanup_marketdata_columns(out)

    outp = processed_dir / (
        f"krx_marketdata__asof={args.asof}__src={args.src}__lookback={args.lookback}d__v={args.out_v}.parquet"
    )
    out.to_parquet(outp, index=False)

    used_px_date = (
        out["used_px_date"].dropna().iloc[0]
        if "used_px_date" in out.columns and out["used_px_date"].notna().any()
        else None
    )
    mcap_null = float(out["mcap"].isna().mean()) if "mcap" in out.columns else 1.0
    shares_null = float(out["shares"].isna().mean()) if "shares" in out.columns else 1.0
    ind_null = float(out["industry_code"].isna().mean()) if "industry_code" in out.columns else 1.0
    trd_null = float(out["traded_value"].isna().mean()) if "traded_value" in out.columns else 1.0

    print(f"[OK] saved: {outp}")
    print(
        f"[INFO] rows={len(out)} tickers={out['ticker'].nunique(dropna=True)} "
        f"mcap_null_ratio={mcap_null:.3f} shares_null_ratio={shares_null:.3f} "
        f"industry_null_ratio={ind_null:.3f} traded_value_null_ratio={trd_null:.3f} "
        f"used_px_date={used_px_date}"
    )


if __name__ == "__main__":
    main()