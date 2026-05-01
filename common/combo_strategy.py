from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import numpy as np
import pandas as pd


def normalize_ticker_value(value: object) -> str:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits.zfill(6) if digits else ""


def normalize_ticker_series(s: pd.Series) -> pd.Series:
    return s.map(normalize_ticker_value)


def build_combo_tickers(
    base_tickers: Iterable[str],
    ai_tickers: Iterable[str],
    k: int,
    variant: str = "overlay_replace",
) -> list[str]:
    base = [normalize_ticker_value(x) for x in base_tickers if normalize_ticker_value(x)]
    ai = [normalize_ticker_value(x) for x in ai_tickers if normalize_ticker_value(x)]
    base = list(dict.fromkeys(base))
    ai = list(dict.fromkeys(ai))
    k = max(0, int(k))
    if k == 0:
        return []

    base_set = set(base)
    ai_set = set(ai)

    if variant == "union_equal":
        ordered = ai + [tk for tk in base if tk not in ai_set]
        return ordered[:k]

    if variant == "intersection_priority":
        overlap = [tk for tk in ai if tk in base_set]
        ai_only = [tk for tk in ai if tk not in base_set]
        base_only = [tk for tk in base if tk not in ai_set]
        return (overlap + ai_only + base_only)[:k]

    if variant != "overlay_replace":
        raise ValueError(f"unsupported combo variant: {variant}")

    overlap = [tk for tk in ai if tk in base_set]
    ai_only = [tk for tk in ai if tk not in base_set]
    base_only = [tk for tk in base if tk not in ai_set]
    return (overlap + ai_only + base_only)[:k]


def calc_turnover(prev_tickers: Iterable[str], new_tickers: Iterable[str]) -> float:
    prev = set(normalize_ticker_value(x) for x in prev_tickers if normalize_ticker_value(x))
    new = set(normalize_ticker_value(x) for x in new_tickers if normalize_ticker_value(x))
    if not prev and not new:
        return 0.0
    if not prev or not new:
        return 1.0
    shared = len(prev & new)
    return float(1.0 - (shared / max(len(prev), len(new))))


def portfolio_month_return(ret: pd.DataFrame, tickers: list[str], month_end: pd.Timestamp) -> float:
    if not tickers:
        return np.nan
    seg = ret.loc[(ret["month_end"] == month_end) & (ret["ticker"].isin(tickers)), ["ticker", "ret_1m"]].copy()
    if len(seg) == 0:
        return 0.0
    ret_map = dict(zip(seg["ticker"].astype(str), pd.to_numeric(seg["ret_1m"], errors="coerce").fillna(0.0).astype(float)))
    vals = [float(ret_map.get(tk, 0.0)) for tk in tickers]
    return float(np.mean(vals))


def calc_cagr_from_nav(nav: pd.Series, periods_per_year: int = 12) -> float:
    x = pd.to_numeric(nav, errors="coerce").dropna()
    if len(x) < 2:
        return np.nan
    years = len(x) / float(periods_per_year)
    if years <= 0 or x.iloc[0] <= 0:
        return np.nan
    return float((x.iloc[-1] / x.iloc[0]) ** (1.0 / years) - 1.0)


def calc_sharpe(ret: pd.Series, periods_per_year: int = 12) -> float:
    x = pd.to_numeric(ret, errors="coerce").dropna()
    if len(x) < 2:
        return np.nan
    sd = x.std(ddof=1)
    if not np.isfinite(sd) or sd == 0.0:
        return np.nan
    return float(x.mean() / sd * np.sqrt(periods_per_year))


def calc_mdd_from_nav(nav: pd.Series) -> float:
    x = pd.to_numeric(nav, errors="coerce").dropna()
    if len(x) == 0:
        return np.nan
    dd = x / x.cummax() - 1.0
    return float(dd.min())


@dataclass(frozen=True)
class ComboBacktestSummary:
    strategy_variant: str
    periods: int
    months: int
    final_nav_net: float
    cagr_net: float
    sharpe_net: float
    mdd_net: float
    avg_turnover: float
    avg_overlap_ratio: float

    def to_dict(self) -> dict[str, float | int | str]:
        return {
            "strategy_variant": self.strategy_variant,
            "periods": self.periods,
            "months": self.months,
            "net_nav": self.final_nav_net,
            "cagr_net": self.cagr_net,
            "sharpe_net": self.sharpe_net,
            "maxdd_net": self.mdd_net,
            "avg_turnover": self.avg_turnover,
            "avg_overlap_ratio": self.avg_overlap_ratio,
        }
