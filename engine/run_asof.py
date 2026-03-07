# engine/run_asof.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from .io import (
    Paths, ensure_dir,
    load_features_live, load_returns_monthly, load_universe, load_master,
    load_marketdata, merge_market_cap_into_master,
)
from .peer import PeerConfig, add_industry_capbucket
from .scoring_core import compute_core_score
from .scoring_pattern import compute_pattern_scores, PatternConfig
from .explain import ExplainConfig, save_holdings, save_inputs_long, save_peers
from .orders import OrdersConfig, build_orders, save_orders, save_summary_md


@dataclass(frozen=True)
class RunAsOfConfig:
    asof: str
    metric: str
    strategy: str
    k: int

    features_v: int
    returns_v: int
    universe_v: int
    master_v: int

    marketdata_v: int = 2
    marketdata_src: str = "synthetic_dart"
    marketdata_lookback: str = "365d"

    master_src: str = "pykrx"
    returns_src: str = "pykrx"
    out_v: int = 1
    root: Path = Path(".")


def _norm_ticker(df: pd.DataFrame) -> pd.DataFrame:
    for c in ["ticker", "code", "종목코드", "티커", "stock_code"]:
        if c in df.columns:
            if c != "ticker":
                df = df.rename(columns={c: "ticker"})
            df["ticker"] = df["ticker"].astype(str).str.zfill(6)
            return df
    raise ValueError("ticker column not found.")


def _reason_top_features(inputs_long: pd.DataFrame, holdings_tickers: list[str], topn: int = 3) -> pd.DataFrame:
    if inputs_long is None or len(inputs_long) == 0:
        return pd.DataFrame({"ticker": holdings_tickers, "reason_top_features": [""] * len(holdings_tickers)})

    x = inputs_long.copy()
    if "ticker" not in x.columns:
        return pd.DataFrame({"ticker": holdings_tickers, "reason_top_features": [""] * len(holdings_tickers)})

    x["ticker"] = x["ticker"].astype(str).str.zfill(6)
    x = x[x["ticker"].isin([str(t).zfill(6) for t in holdings_tickers])].copy()
    if len(x) == 0:
        return pd.DataFrame({"ticker": holdings_tickers, "reason_top_features": [""] * len(holdings_tickers)})

    z = pd.to_numeric(x.get("z_robust", np.nan), errors="coerce").fillna(0.0).abs()
    w = pd.to_numeric(x.get("weight", np.nan), errors="coerce").fillna(0.0).abs()
    x["impact"] = z * w
    x = x.sort_values(["ticker", "impact"], ascending=[True, False])

    rows = []
    for t, g in x.groupby("ticker"):
        feats = g["feature_name"].astype(str).head(topn).tolist() if "feature_name" in g.columns else []
        rows.append({"ticker": t, "reason_top_features": ", ".join(feats)})
    return pd.DataFrame(rows)


def _build_peers_long(df_scored: pd.DataFrame, top: pd.DataFrame) -> pd.DataFrame:
    """
    Robust peers materialization:
    For each top ticker, attach all tickers in same (industry, cap_bucket).
    Always returns fixed columns, even if empty.
    """
    out_cols = ["ticker", "peer_ticker", "industry", "cap_bucket"]

    if df_scored is None or len(df_scored) == 0 or top is None or len(top) == 0:
        return pd.DataFrame(columns=out_cols)

    need = {"ticker", "industry", "cap_bucket"}
    if not need.issubset(set(df_scored.columns)) or "ticker" not in top.columns:
        return pd.DataFrame(columns=out_cols)

    base = df_scored[["ticker", "industry", "cap_bucket"]].copy().drop_duplicates()
    base["ticker"] = base["ticker"].astype(str).str.zfill(6)

    top_keys = top[["ticker", "industry", "cap_bucket"]].copy().drop_duplicates()
    top_keys["ticker"] = top_keys["ticker"].astype(str).str.zfill(6)

    peers_long = top_keys.merge(
        base.rename(columns={"ticker": "peer_ticker"}),
        on=["industry", "cap_bucket"],
        how="left",
    )

    peers_long = peers_long[["ticker", "peer_ticker", "industry", "cap_bucket"]].copy()
    peers_long["peer_ticker"] = peers_long["peer_ticker"].astype(str).str.zfill(6)
    return peers_long


def run_asof(strategy_impl, cfg: RunAsOfConfig, prev_holdings: Optional[pd.DataFrame] = None) -> dict:
    paths = Paths(root=cfg.root)
    explain_dir = ensure_dir(paths.explain())

    df_feat = _norm_ticker(load_features_live(cfg.asof, cfg.metric, cfg.features_v, cfg.root))
    _ = load_returns_monthly(cfg.asof, cfg.metric, cfg.returns_v, cfg.returns_src, cfg.root)
    df_uni = _norm_ticker(load_universe(cfg.asof, cfg.metric, cfg.universe_v, cfg.root))
    df_mst = _norm_ticker(load_master(cfg.asof, cfg.master_v, cfg.master_src, cfg.root))

    try:
        df_md = load_marketdata(
            cfg.asof,
            v=cfg.marketdata_v,
            src=cfg.marketdata_src,
            lookback=cfg.marketdata_lookback,
            root=cfg.root,
        )
        df_mst = merge_market_cap_into_master(df_mst, df_md, out_mcap_col="market_cap")

        # marketdata에 industry4가 있으면 master에도 같이 붙여서 peer에 활용
        if "industry4" in df_md.columns:
            md_small = df_md[["ticker", "industry4"]].drop_duplicates("ticker")
            df_mst = df_mst.merge(md_small, on="ticker", how="left")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[WARN] marketdata merge skipped: {e}")

    uni = set(df_uni["ticker"].tolist())
    df = df_feat[df_feat["ticker"].isin(uni)].copy()

    # features_live에 industry가 없으면 master에서 끌어와서 peer 의미 살리기
    if "industry4" not in df.columns and "industry4" in df_mst.columns:
        df = df.merge(df_mst[["ticker", "industry4"]].drop_duplicates("ticker"), on="ticker", how="left")

    df = add_industry_capbucket(df, df_mst, PeerConfig())

    df["peer_key"] = df["industry"].astype(str) + "|" + df["cap_bucket"].astype(str)

    if hasattr(strategy_impl, "prefilter") and callable(strategy_impl.prefilter):
        df = strategy_impl.prefilter(df)

    specs = strategy_impl.core_specs(df)
    df_scored, inputs_long = compute_core_score(df, specs, PeerConfig())

    try:
        pconf = strategy_impl.pattern_config()
        if not isinstance(pconf, PatternConfig):
            pconf = PatternConfig()
    except Exception:
        pconf = PatternConfig()
    df_scored = compute_pattern_scores(df_scored, pconf)

    df_scored["score_total"] = df_scored.get("score_core", 0.0)
    if "score_pattern" in df_scored.columns:
        df_scored["score_total"] = df_scored["score_total"].fillna(0.0) + df_scored["score_pattern"].fillna(0.0)

    df_scored = df_scored.sort_values("score_total", ascending=False)

    top = df_scored.head(int(cfg.k)).copy()
    top["weight"] = 1.0 / max(1, len(top))

    peers_long = _build_peers_long(df_scored, top)

    econf = ExplainConfig(
        out_dir=explain_dir,
        asof=cfg.asof,
        metric=cfg.metric,
        strategy=cfg.strategy,
        out_v=cfg.out_v,
    )
    p_holdings = save_holdings(top, econf)
    p_inputs = save_inputs_long(inputs_long, econf)
    p_peers = save_peers(peers_long, econf)

    reasons = _reason_top_features(inputs_long, top["ticker"].tolist(), topn=3)

    oconf = OrdersConfig(
        out_dir=explain_dir,
        asof=cfg.asof,
        metric=cfg.metric,
        strategy=cfg.strategy,
        k=cfg.k,
        out_v=cfg.out_v,
    )

    orders, _orders_summary = build_orders(holdings_now=top, prev_holdings=prev_holdings, reasons=reasons)
    p_orders = save_orders(orders, oconf)
    p_summary = save_summary_md(top, orders, oconf)

    return {
        "holdings": top,
        "inputs_long": inputs_long,
        "peers": peers_long,
        "orders": orders,
        "paths": {
            "holdings": str(p_holdings),
            "inputs_long": str(p_inputs),
            "peers": str(p_peers),
            "orders": str(p_orders),
            "summary": str(p_summary),
        },
    }