# engine/io.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

import pandas as pd


@dataclass(frozen=True)
class Paths:
    root: Path = Path(".")
    processed_dir: Path = Path("data/processed")
    explain_dir: Path = Path("data/processed/explainability")

    def processed(self) -> Path:
        return (self.root / self.processed_dir).resolve()

    def explain(self) -> Path:
        return (self.root / self.explain_dir).resolve()


def _tag(asof: str) -> str:
    # expects YYYY-MM-DD
    return asof


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def save_df(df: pd.DataFrame, path: Path) -> None:
    ensure_dir(path.parent)
    suf = path.suffix.lower()
    if suf == ".csv":
        df.to_csv(path, index=False, encoding="utf-8-sig")
    elif suf == ".parquet":
        df.to_parquet(path, index=False)
    else:
        raise ValueError(f"Unsupported format: {path}")


# ---- core processed paths ----
def features_live_path(asof: str, metric: str, v: int, root: Path = Path(".")) -> Path:
    fn = f"features_live__asof={_tag(asof)}__metric={metric}__v={v}.parquet"
    return root / "data/processed" / fn


def returns_monthly_path(asof: str, metric: str, v: int, src: str = "pykrx", root: Path = Path(".")) -> Path:
    fn = f"returns_monthly__src={src}__asof={_tag(asof)}__metric={metric}__v={v}.parquet"
    return root / "data/processed" / fn


def universe_path(asof: str, metric: str, v: int, root: Path = Path(".")) -> Path:
    fn_csv = f"universe__asof={_tag(asof)}__metric={metric}__v={v}.csv"
    fn_pq = f"universe__asof={_tag(asof)}__metric={metric}__v={v}.parquet"
    p_csv = root / "data/processed" / fn_csv
    p_pq = root / "data/processed" / fn_pq
    return p_pq if p_pq.exists() else p_csv


def master_path(asof: str, v: int, src: str = "pykrx", root: Path = Path(".")) -> Path:
    fn = f"krx_master__asof={_tag(asof)}__src={src}__v={v}.parquet"
    return root / "data/processed" / fn


# ---- marketdata (mcap) ----
def _parse_lookback_days(lookback: Union[str, int]) -> int:
    if isinstance(lookback, int):
        return int(lookback)
    s = str(lookback).strip().lower()
    if s.endswith("d"):
        return int(s[:-1])
    return int(s)


def marketdata_path(asof: str, v: int, src: str = "pykrx", lookback: Union[str, int] = "365d", root: Path = Path(".")) -> Path:
    lb = _parse_lookback_days(lookback)
    fn = f"krx_marketdata__asof={_tag(asof)}__src={src}__lookback={lb}d__v={v}.parquet"
    return root / "data/processed" / fn


def load_marketdata(asof: str, v: int, src: str = "pykrx", lookback: Union[str, int] = "365d", root: Path = Path(".")) -> pd.DataFrame:
    p = marketdata_path(asof, v, src, lookback, root)
    if not p.exists():
        raise FileNotFoundError(f"krx_marketdata not found: {p}")
    return pd.read_parquet(p)


def _pick_col(df: pd.DataFrame, candidates: list[str]) -> Optional[str]:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def _norm_ticker(df: pd.DataFrame) -> pd.DataFrame:
    tcol = _pick_col(df, ["ticker", "code", "종목코드", "티커", "stock_code"])
    if tcol is None:
        raise ValueError("ticker column not found (ticker/code/종목코드/티커/stock_code).")
    if tcol != "ticker":
        df = df.rename(columns={tcol: "ticker"})
    df["ticker"] = df["ticker"].astype(str).str.zfill(6)
    return df


def merge_market_cap_into_master(master: pd.DataFrame, marketdata: pd.DataFrame, out_mcap_col: str = "market_cap") -> pd.DataFrame:
    """
    Merge market cap into master as `out_mcap_col`.
    marketdata may have: mcap / market_cap / 시가총액
    """
    m = _norm_ticker(master.copy())
    md = _norm_ticker(marketdata.copy())

    mcap_col = _pick_col(md, ["market_cap", "mcap", "시가총액", "MKT_CAP", "mkcap", "mkt_cap"])
    if mcap_col is None:
        raise ValueError(f"marketdata missing market cap col. cols={list(md.columns)}")

    md_small = md[["ticker", mcap_col]].rename(columns={mcap_col: out_mcap_col}).drop_duplicates("ticker")
    out = m.merge(md_small, on="ticker", how="left")
    return out


# ---- loaders ----
def load_features_live(asof: str, metric: str, v: int, root: Path = Path(".")) -> pd.DataFrame:
    p = features_live_path(asof, metric, v, root)
    if not p.exists():
        raise FileNotFoundError(f"features_live not found: {p}")
    return pd.read_parquet(p)


def load_returns_monthly(asof: str, metric: str, v: int, src: str = "pykrx", root: Path = Path(".")) -> pd.DataFrame:
    p = returns_monthly_path(asof, metric, v, src, root)
    if not p.exists():
        raise FileNotFoundError(f"returns_monthly not found: {p}")
    return pd.read_parquet(p)


def load_universe(asof: str, metric: str, v: int, root: Path = Path(".")) -> pd.DataFrame:
    p = universe_path(asof, metric, v, root)
    if not p.exists():
        raise FileNotFoundError(f"universe not found: {p}")
    if p.suffix.lower() == ".csv":
        return pd.read_csv(p)
    return pd.read_parquet(p)


def load_master(asof: str, v: int, src: str = "pykrx", root: Path = Path(".")) -> pd.DataFrame:
    p = master_path(asof, v, src, root)
    if not p.exists():
        raise FileNotFoundError(f"krx_master not found: {p}")
    return pd.read_parquet(p)