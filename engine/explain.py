# engine/explain.py
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .io import save_df


@dataclass(frozen=True)
class ExplainConfig:
    out_dir: Path
    asof: str
    metric: str
    strategy: str
    out_v: int


def _tag(cfg: ExplainConfig) -> str:
    return f"asof={cfg.asof}__metric={cfg.metric}__strat={cfg.strategy}__v={cfg.out_v}"


def save_holdings(holdings: pd.DataFrame, cfg: ExplainConfig) -> Path:
    p = cfg.out_dir / f"portfolio_holdings__{_tag(cfg)}.csv"
    save_df(holdings, p)
    return p


def save_inputs_long(inputs_long: pd.DataFrame, cfg: ExplainConfig) -> Path:
    p = cfg.out_dir / f"portfolio_inputs_long__{_tag(cfg)}.parquet"
    save_df(inputs_long, p)
    return p


def save_peers(peers_long: pd.DataFrame, cfg: ExplainConfig) -> Path:
    p = cfg.out_dir / f"portfolio_peers__{_tag(cfg)}.parquet"
    save_df(peers_long, p)
    return p