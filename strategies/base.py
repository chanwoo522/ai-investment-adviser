# strategies/base.py
from __future__ import annotations

from typing import List, Protocol
import pandas as pd

from engine.scoring_core import FeatureSpec


class Strategy(Protocol):
    name: str

    def prefilter(self, df: pd.DataFrame) -> pd.DataFrame:
        ...

    def core_specs(self, df: pd.DataFrame) -> List[FeatureSpec]:
        ...

    def build_portfolio(self, df_scored: pd.DataFrame, k: int) -> pd.DataFrame:
        ...