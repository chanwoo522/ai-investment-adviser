# engine/robustz.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class RobustZConfig:
    """
    Robust z-score config.
    z = (x - median) / (IQR / 1.349)
    """
    eps: float = 1e-12
    clip_z: Optional[float] = None  # e.g. 6.0
    fillna: Optional[float] = None  # e.g. 0.0


def robustz(x: pd.Series | np.ndarray, *, eps: float = 1e-12) -> pd.Series:
    """
    Robust z-score using median and IQR scaled to match std under normality:
      z = (x - median) / (IQR / 1.349)
    Safe for NaNs and constant series.
    """
    if isinstance(x, np.ndarray):
        s = pd.Series(x)
    else:
        s = pd.Series(x).copy()

    s = pd.to_numeric(s, errors="coerce")

    med = s.median(skipna=True)
    q1 = s.quantile(0.25, interpolation="linear")
    q3 = s.quantile(0.75, interpolation="linear")
    iqr = (q3 - q1)

    denom = (iqr / 1.349)
    if pd.isna(denom) or abs(float(denom)) < eps:
        out = s.copy()
        out[:] = np.where(s.isna(), np.nan, 0.0)
        return out

    return (s - med) / denom


# canonical alias used across engine
def robust_z(x: pd.Series | np.ndarray, *, eps: float = 1e-12) -> pd.Series:
    return robustz(x, eps=eps)


def _apply_cfg(z: pd.Series, cfg: RobustZConfig) -> pd.Series:
    out = z
    if cfg.clip_z is not None:
        out = out.clip(lower=-float(cfg.clip_z), upper=float(cfg.clip_z))
    if cfg.fillna is not None:
        out = out.fillna(float(cfg.fillna))
    return out


def robust_z_by_group(
    df: pd.DataFrame,
    value_col: str,
    group_col: str,
    *,
    cfg: RobustZConfig = RobustZConfig(),
) -> pd.Series:
    """
    Returns a Series aligned to df.index: robust z-score of df[value_col] within each group.
    """
    if value_col not in df.columns:
        raise ValueError(f"value_col not in df: {value_col}")
    if group_col not in df.columns:
        # fallback: one global group
        s = robust_z(pd.to_numeric(df[value_col], errors="coerce"), eps=cfg.eps)
        return _apply_cfg(s, cfg)

    def _calc(g: pd.DataFrame) -> pd.Series:
        s = robust_z(pd.to_numeric(g[value_col], errors="coerce"), eps=cfg.eps)
        return _apply_cfg(s, cfg)

    z = df.groupby(group_col, group_keys=False).apply(_calc)
    # groupby/apply returns Series aligned; just ensure index match
    z = z.reindex(df.index)
    return z


def robust_z_by_groups(
    df: pd.DataFrame,
    value_col: str,
    group_cols: Sequence[str],
    *,
    cfg: RobustZConfig = RobustZConfig(),
) -> pd.Series:
    """
    Robust z within combined group keys (e.g. industry x cap_bucket).
    Creates a temporary key and calls robust_z_by_group.
    """
    missing = [c for c in group_cols if c not in df.columns]
    if missing:
        # fallback: global
        s = robust_z(pd.to_numeric(df[value_col], errors="coerce"), eps=cfg.eps)
        return _apply_cfg(s, cfg)

    key = df[group_cols].astype(str).agg("|".join, axis=1)
    tmp = df.copy()
    tmp["__rz_key__"] = key
    return robust_z_by_group(tmp, value_col=value_col, group_col="__rz_key__", cfg=cfg)