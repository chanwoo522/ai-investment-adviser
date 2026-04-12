# strategies/B_growth_plus_quality.py
from __future__ import annotations

from engine.scoring_core import CoreSpec
from engine.scoring_pattern import PatternConfig


class B_growth_plus_quality:
    name = "B_growth_plus_quality"

    def prefilter(self, df):
        return df

    def core_specs(self, df):
        return [
            CoreSpec("Revenue_ttm_yoy", weight=1.0, higher_is_better=True),
            CoreSpec("OpIncome_ttm_yoy", weight=1.0, higher_is_better=True),
            CoreSpec("Debt_to_Equity_log", weight=0.5, higher_is_better=False),
            CoreSpec("CFO_safe", weight=0.5, higher_is_better=True),
        ]

    def pattern_config(self):
        return PatternConfig()