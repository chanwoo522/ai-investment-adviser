# scripts/krx_safe.py
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, Optional, Tuple, Any

import pandas as pd


def to_ymd(asof: str) -> str:
    """
    Convert 'YYYY-MM-DD' or 'YYYYMMDD' -> 'YYYYMMDD'
    """
    s = str(asof).strip()
    if "-" in s:
        return s.replace("-", "")
    if len(s) == 8 and s.isdigit():
        return s
    raise ValueError(f"Invalid date format: {asof} (expected YYYY-MM-DD or YYYYMMDD)")


def ensure_ticker_col(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalize ticker into a column named 'ticker'.
    Handles cases where ticker is index, or column name is '티커'.
    """
    if df is None:
        return df
    if not isinstance(df, pd.DataFrame):
        raise TypeError(f"ensure_ticker_col expects DataFrame, got {type(df)}")

    if "ticker" in df.columns:
        return df

    # Common: pykrx returns ticker as index
    out = df.copy()
    if out.index is not None:
        out = out.reset_index()
        # after reset_index the index column name could be '티커', 'ticker', or 'index'
        if "티커" in out.columns and "ticker" not in out.columns:
            out = out.rename(columns={"티커": "ticker"})
        if "index" in out.columns and "ticker" not in out.columns:
            out = out.rename(columns={"index": "ticker"})
    if "ticker" not in out.columns:
        raise KeyError("Could not normalize 'ticker' column from DataFrame (no ticker column / index).")
    out["ticker"] = out["ticker"].astype(str)
    return out


def _is_nonempty_df(x: Any) -> bool:
    return isinstance(x, pd.DataFrame) and len(x) > 0 and len(x.columns) > 0


def backoff_date_call(
    fn: Callable[[str], pd.DataFrame],
    asof_ymd: str,
    max_back_days: int = 14,
    require_cols: Optional[set[str]] = None,
) -> Tuple[str, pd.DataFrame]:
    """
    Call fn(date_ymd). If result is empty or missing required columns, go back day-by-day up to max_back_days.
    Returns (used_date_ymd, df).
    """
    d0 = datetime.strptime(asof_ymd, "%Y%m%d")

    last_err: Optional[Exception] = None
    for i in range(max_back_days + 1):
        q = (d0 - timedelta(days=i)).strftime("%Y%m%d")
        try:
            df = fn(q)
            if not _is_nonempty_df(df):
                continue
            if require_cols:
                # columns might be in Korean; caller can normalize before checking
                if not require_cols.issubset(set(df.columns)):
                    continue
            return q, df
        except Exception as e:
            last_err = e
            continue

    if last_err:
        raise RuntimeError(f"KRX call failed for {asof_ymd} (backed off {max_back_days}d). Last error: {last_err}") from last_err
    raise RuntimeError(f"KRX call returned empty for {asof_ymd} (backed off {max_back_days}d).")


def normalize_mcap_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    pykrx get_market_cap_by_ticker returns DF with Korean columns:
      종가, 시가총액, 거래량, 거래대금
    Normalize to:
      ticker, close, mcap, vol, traded_value
    """
    df = ensure_ticker_col(df)

    # Try Korean first
    ren = {}
    if "종가" in df.columns:
        ren["종가"] = "close"
    if "시가총액" in df.columns:
        ren["시가총액"] = "mcap"
    if "거래량" in df.columns:
        ren["거래량"] = "vol"
    if "거래대금" in df.columns:
        ren["거래대금"] = "traded_value"

    # Some pykrx versions may already be English-ish; support fallback
    if "close" not in df.columns and "종가" not in df.columns:
        for alt in ("종가", "Close", "종가(원)", "종가_원"):
            if alt in df.columns:
                ren[alt] = "close"
                break
    if "mcap" not in df.columns and "시가총액" not in df.columns:
        for alt in ("시가총액", "MarketCap", "시총"):
            if alt in df.columns:
                ren[alt] = "mcap"
                break
    if "vol" not in df.columns and "거래량" not in df.columns:
        for alt in ("거래량", "Volume", "거래수량"):
            if alt in df.columns:
                ren[alt] = "vol"
                break
    if "traded_value" not in df.columns and "거래대금" not in df.columns:
        for alt in ("거래대금", "Value", "거래금액"):
            if alt in df.columns:
                ren[alt] = "traded_value"
                break

    out = df.rename(columns=ren)

    need = {"ticker", "close", "mcap", "vol"}
    if not need.issubset(set(out.columns)):
        raise KeyError(f"normalize_mcap_df: required cols missing. need={need}, have={set(out.columns)}")

    if "traded_value" not in out.columns:
        # derive approximate traded_value if missing
        out["traded_value"] = out["close"] * out["vol"]

    # numeric safe
    for c in ("close", "mcap", "vol", "traded_value"):
        out[c] = pd.to_numeric(out[c], errors="coerce")

    return out[["ticker", "close", "mcap", "vol", "traded_value"]].copy()