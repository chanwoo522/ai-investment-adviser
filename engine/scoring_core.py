# engine/scoring_core.py
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple, Optional

import numpy as np
import pandas as pd

from .robustz import robust_z
from .peer import PeerConfig


@dataclass(frozen=True)
class CoreSpec:
    feature: str
    weight: float = 1.0
    higher_is_better: bool = True
    clip_z: float = 6.0  # cap z-score magnitude for stability


# Backward compatibility alias (some strategies used FeatureSpec)
FeatureSpec = CoreSpec


def compute_core_score(
    df: pd.DataFrame,
    specs: List[CoreSpec],
    peer_cfg: PeerConfig,
    score_col: str = "score_core",
    z_col_prefix: str = "z_",
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Compute peer-aware robust z-scores per feature and weighted sum.
    Returns:
      - df_scored: original df + z columns + score_col
      - inputs_long: long-format explanation table
    """
    if df is None or len(df) == 0:
        out = df.copy() if df is not None else pd.DataFrame()
        out[score_col] = np.nan
        return out, pd.DataFrame(columns=["ticker", "feature_name", "raw_value", "z_robust", "weight", "peer_key"])

    work = df.copy()

    # Determine peer key column
    peer_key_col: Optional[str] = None
    for c in ["peer_key", "peer_group", "peer"]:
        if c in work.columns:
            peer_key_col = c
            break
    if peer_key_col is None:
        peer_key_col = "__peer_all__"
        work[peer_key_col] = "ALL"

    work[score_col] = 0.0
    rows = []

    for spec in specs:
        f = spec.feature
        if f not in work.columns:
            continue

        raw = pd.to_numeric(work[f], errors="coerce")

        # robust z per peer group (avoid pandas groupby.apply ambiguity)
        parts = []
        for _, idx in work.groupby(peer_key_col).groups.items():
            s = pd.to_numeric(work.loc[idx, f], errors="coerce")
            z_part = robust_z(s, eps=1e-12)
            z_part.index = idx
            parts.append(z_part)

        z = pd.concat(parts).reindex(work.index)
        z = pd.to_numeric(z, errors="coerce").clip(lower=-float(spec.clip_z), upper=float(spec.clip_z))

        if not spec.higher_is_better:
            z = -z

        zcol = f"{z_col_prefix}{f}"
        work[zcol] = z

        w = float(spec.weight)
        work[score_col] = work[score_col] + w * work[zcol].fillna(0.0)

        rows.append(
            pd.DataFrame(
                {
                    "ticker": work["ticker"].astype(str),
                    "feature_name": f,
                    "raw_value": raw,
                    "z_robust": work[zcol],
                    "weight": w,
                    "peer_key": work[peer_key_col].astype(str),
                }
            )
        )

    inputs_long = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(
        columns=["ticker", "feature_name", "raw_value", "z_robust", "weight", "peer_key"]
    )
    return work, inputs_long