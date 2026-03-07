# engine/scoring_pattern.py
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
import pandas as pd

from .robustz import RobustZConfig, robust_z_by_groups


@dataclass(frozen=True)
class PatternConfig:
    # Pattern bonus is limited (guideline)
    bonus_weights: Optional[Dict[str, float]] = None
    cap: float = 0.7
    score_col: str = "score_pattern"
    probs_prefix: str = "p_pattern_"
    robustz: RobustZConfig = RobustZConfig()

    def __post_init__(self):
        if self.bonus_weights is None:
            object.__setattr__(
                self,
                "bonus_weights",
                {
                    "razor": 0.5,
                    "platform": 0.4,
                    "rnd": 0.3,
                    "commodity": 0.0,
                    "infra": 0.0,
                },
            )


def _ensure_z(work: pd.DataFrame, raw_col: str, z_col: str, cfg: PatternConfig) -> None:
    """
    Pattern is industry-agnostic here: robust z over whole universe.
    """
    if z_col in work.columns:
        return
    if raw_col not in work.columns:
        work[z_col] = np.nan
        return
    z = robust_z_by_groups(work, value_col=raw_col, group_cols=[], cfg=cfg.robustz)
    work[z_col] = z


def compute_pattern_scores(df: pd.DataFrame, cfg: PatternConfig = PatternConfig()) -> pd.DataFrame:
    work = df.copy()

    mapping = {
        "rev_cagr": "rev_cagr",
        "ebit_cagr": "ebit_cagr",
        "gross_margin": "gross_margin",
        "ebit_margin": "ebit_margin",
        "roic": "roic",
        "fcf_conv": "fcf_conv",
        "fcf_stability": "fcf_stability",
        "capex_ratio": "capex_ratio",
        "capex_growth": "capex_growth",
        "rd_ratio": "rd_ratio",
        "rev_vol": "rev_vol",
        "ebit_vol": "ebit_vol",
    }

    # create z_ columns safely
    for raw in mapping.values():
        _ensure_z(work, raw_col=raw, z_col=f"z_{raw}", cfg=cfg)

    z = {raw: work[f"z_{raw}"] for raw in mapping.values()}

    razor_raw = z["gross_margin"] + z["ebit_margin"] + z["fcf_conv"] + z["fcf_stability"] + 0.5 * z["rev_cagr"]
    commodity_raw = z["rev_vol"] + z["ebit_vol"] - z["fcf_stability"]
    platform_raw = z["gross_margin"] + z["rev_cagr"] - z["capex_ratio"] + 0.5 * z["roic"]
    rnd_raw = z["rd_ratio"] + z["rev_cagr"] + z["roic"]
    infra_raw = z["capex_ratio"] + z["capex_growth"] + 0.5 * z["rev_cagr"]

    raws = pd.DataFrame(
        {
            "razor": razor_raw,
            "commodity": commodity_raw,
            "platform": platform_raw,
            "rnd": rnd_raw,
            "infra": infra_raw,
        },
        index=work.index,
    )

    scores = raws.clip(lower=0.0)
    ssum = scores.sum(axis=1)
    probs = scores.div(ssum.replace(0.0, np.nan), axis=0).fillna(0.2)

    for k in ["razor", "commodity", "platform", "rnd", "infra"]:
        work[f"{cfg.probs_prefix}{k}"] = probs[k].astype(float)

    bw = cfg.bonus_weights or {}
    bonus = (
        probs["razor"] * bw.get("razor", 0.0)
        + probs["platform"] * bw.get("platform", 0.0)
        + probs["rnd"] * bw.get("rnd", 0.0)
        + probs["commodity"] * bw.get("commodity", 0.0)
        + probs["infra"] * bw.get("infra", 0.0)
    ).clip(upper=float(cfg.cap))

    # if none of raw pattern inputs exist -> force 0 (avoid accidental constant bonus)
    any_raw_exists = False
    for raw in mapping.values():
        if raw in work.columns and work[raw].notna().any():
            any_raw_exists = True
            break
    if not any_raw_exists:
        bonus[:] = 0.0

    work[cfg.score_col] = bonus.astype(float)
    return work