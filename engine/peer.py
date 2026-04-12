# engine/peer.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
import pandas as pd


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


@dataclass(frozen=True)
class PeerConfig:
    # marketdata / shares_industry 기준 industry4, induty_code 우선
    industry_candidates: Tuple[str, ...] = (
        "industry4",
        "induty_code",
        "industry",
        "sector",
        "krx_industry",
    )
    mcap_candidates: Tuple[str, ...] = (
        "market_cap",
        "mcap",
        "mkt_cap",
        "mkcap",
        "시가총액",
        "MKT_CAP",
    )
    n_cap_buckets: int = 5


def add_industry_capbucket(
    df_features: pd.DataFrame,
    df_master: pd.DataFrame,
    cfg: PeerConfig = PeerConfig(),
) -> pd.DataFrame:
    """
    Peer = Industry × CapBucket
    - industry: prefer features, fallback master
    - market_cap: prefer master
    """
    f = _norm_ticker(df_features.copy())
    m = _norm_ticker(df_master.copy())

    # industry from features, else master
    ind_col_f = _pick_col(f, list(cfg.industry_candidates))
    ind_col_m = _pick_col(m, list(cfg.industry_candidates))

    if ind_col_f is not None:
        f["industry"] = f[ind_col_f].astype(str)
    elif ind_col_m is not None:
        m_small_ind = m[["ticker", ind_col_m]].rename(columns={ind_col_m: "industry"}).drop_duplicates("ticker")
        f = f.merge(m_small_ind, on="ticker", how="left")
        f["industry"] = f["industry"].fillna("ALL").astype(str)
    else:
        f["industry"] = "ALL"

    # market cap from master
    mcap_col = _pick_col(m, list(cfg.mcap_candidates))
    if mcap_col is None:
        f["market_cap"] = np.nan
        f["cap_bucket"] = 0
        return f

    m_small = m[["ticker", mcap_col]].rename(columns={mcap_col: "market_cap"}).drop_duplicates("ticker")
    out = f.merge(m_small, on="ticker", how="left")

    if out["market_cap"].notna().sum() >= cfg.n_cap_buckets * 5:
        try:
            out["cap_bucket"] = pd.qcut(
                out["market_cap"],
                q=cfg.n_cap_buckets,
                labels=False,
                duplicates="drop",
            )
        except Exception:
            out["cap_bucket"] = 0
    else:
        out["cap_bucket"] = 0

    return out


def build_peer_groups(df: pd.DataFrame) -> pd.DataFrame:
    out_cols = ["industry", "cap_bucket", "peer_members"]

    need = {"ticker", "industry", "cap_bucket"}
    miss = need - set(df.columns)
    if miss:
        return pd.DataFrame(columns=out_cols)

    g = (
        df[["ticker", "industry", "cap_bucket"]]
        .drop_duplicates()
        .groupby(["industry", "cap_bucket"])["ticker"]
        .apply(list)
        .reset_index()
        .rename(columns={"ticker": "peer_members"})
    )
    return g[out_cols]


def materialize_peers(peer_groups: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    out_cols = ["ticker", "peer_ticker", "industry", "cap_bucket"]

    if peer_groups is None or len(peer_groups) == 0:
        return pd.DataFrame(columns=out_cols)
    if tickers is None or len(tickers) == 0:
        return pd.DataFrame(columns=out_cols)

    lookup = {}
    for _, r in peer_groups.iterrows():
        members = [str(x).zfill(6) for x in r["peer_members"]]
        for t in members:
            lookup[t] = (r["industry"], int(r["cap_bucket"]), members)

    rows = []
    for t in tickers:
        t = str(t).zfill(6)
        if t not in lookup:
            continue
        ind, cb, members = lookup[t]
        for p in members:
            rows.append(
                {
                    "ticker": t,
                    "peer_ticker": p,
                    "industry": ind,
                    "cap_bucket": cb,
                }
            )

    return pd.DataFrame(rows, columns=out_cols)