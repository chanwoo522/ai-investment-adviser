#!/usr/bin/env python
"""Compare the validated legacy strategy with an advisor fresh-start variant.

This module is intentionally isolated from the production scoring/latest paths.  It
only reads caller supplied artifacts and writes comparison artifacts to a new output
directory.  It never promotes a model and it never mutates any input artifact.

The important timing convention is that a portfolio selected at a month-end starts
earning returns in the *following* month.  The return labelled with the rebalance
month therefore belongs to the portfolio selected at the preceding rebalance.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from common.portfolio_modules import apply_expectation_overlay, apply_quality_soft_penalty
from scripts.backtest.backtest_quarterly_rebalance_v2 import (
    apply_filters,
    compute_rebalance_month_from_yq,
    safe_fill_for_z,
    standardize_factor,
)


LEGACY_VARIANT = "D_quality_filter_debt_profitaccel_liq"
FRESH_START_VARIANT = "D_quality_filter_debt_profitaccel_liq_fresh_start"
DEFAULT_COMMISSION_RATE = 0.00015
DEFAULT_SELL_TAX_RATE = 0.002
DEFAULT_CASH_EQUIVALENT_WEIGHT = 0.10
DEFAULT_PIT_MASTER_UNIVERSE_STATUS = "CURRENT_SNAPSHOT_NOT_POINT_IN_TIME"
PIT_VERIFIED_STATUSES = {
    "VERIFIED_POINT_IN_TIME",
    "POINT_IN_TIME_VERIFIED",
    "HISTORICAL_POINT_IN_TIME_VERIFIED",
}

OUTPUT_FILES = {
    "comparison_summary": "fresh_start_comparison_summary.csv",
    "periods": "fresh_start_comparison_periods.csv",
    "monthly": "fresh_start_comparison_monthly.csv",
    "selection": "fresh_start_comparison_selection.csv",
    "sector_exposure": "fresh_start_sector_exposure.csv",
}


def _read_table(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    suffix = p.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(p)
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(p)
    if suffix in {".json", ".jsonl"}:
        return pd.read_json(p, lines=suffix == ".jsonl")
    return pd.read_parquet(p)


def _ticker_series(values: pd.Series) -> pd.Series:
    """Normalize Korean security identifiers without turning missing values into 000nan."""

    def one(value: Any) -> str | pd.NA:
        if pd.isna(value):
            return pd.NA
        text = str(value).strip()
        if text.endswith(".0") and text[:-2].isdigit():
            text = text[:-2]
        if text.isdigit():
            return text.zfill(6)
        return text.upper()

    return values.map(one).astype("string")


def _first_column(columns: Iterable[str], candidates: Iterable[str]) -> str | None:
    have = set(columns)
    return next((name for name in candidates if name in have), None)


def _as_bool(values: pd.Series, default: bool = True) -> pd.Series:
    if pd.api.types.is_bool_dtype(values):
        return values.fillna(default).astype(bool)
    numeric = pd.to_numeric(values, errors="coerce")
    text = values.astype("string").str.strip().str.lower()
    mapped = text.map(
        {
            "true": True,
            "t": True,
            "yes": True,
            "y": True,
            "1": True,
            "false": False,
            "f": False,
            "no": False,
            "n": False,
            "0": False,
        }
    )
    out = mapped.where(mapped.notna(), numeric.map({1.0: True, 0.0: False}))
    return out.astype("boolean").fillna(default).astype(bool)


def _month_end(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, errors="coerce").dt.to_period("M").dt.to_timestamp("M")


def _prepare_features(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    ticker_col = _first_column(out.columns, ["ticker", "code", "symbol", "종목코드"])
    if ticker_col is None:
        raise ValueError("features artifact is missing a ticker column")
    if ticker_col != "ticker":
        out = out.rename(columns={ticker_col: "ticker"})
    out["ticker"] = _ticker_series(out["ticker"])
    out = out.dropna(subset=["ticker"])

    rebalance_col = _first_column(
        out.columns,
        ["rebalance_month", "rebalance_month_end", "rebalance_date"],
    )
    if rebalance_col is not None:
        out["rebalance_month"] = _month_end(out[rebalance_col])
    elif {"year", "quarter"}.issubset(out.columns):
        out["rebalance_month"] = compute_rebalance_month_from_yq(out["year"], out["quarter"])
    else:
        raise ValueError(
            "features artifact needs rebalance_month (or rebalance_month_end) "
            "or the year/quarter pair"
        )
    out = out.dropna(subset=["rebalance_month"])
    if "sector" not in out.columns:
        sector_col = _first_column(
            out.columns,
            ["sector_name", "industry_name", "industry", "industry_l4", "industry4", "업종명"],
        )
        if sector_col is not None:
            out["sector"] = out[sector_col]
    return out.drop_duplicates(["rebalance_month", "ticker"], keep="last").reset_index(drop=True)


def _return_column(columns: Iterable[str]) -> str | None:
    return _first_column(
        columns,
        ["ret_1m", "monthly_return", "price_return", "return", "ret", "수익률"],
    )


def _prepare_returns(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    ticker_col = _first_column(out.columns, ["ticker", "code", "symbol", "종목코드"])
    date_col = _first_column(out.columns, ["month_end", "month", "date", "Date", "asof"])
    if ticker_col is None or date_col is None:
        raise ValueError("returns artifact needs ticker and month/date columns")
    if ticker_col != "ticker":
        out = out.rename(columns={ticker_col: "ticker"})
    out["ticker"] = _ticker_series(out["ticker"])
    out["month_end"] = _month_end(out[date_col])

    return_col = _return_column(out.columns)
    if return_col is not None:
        out["ret_1m"] = pd.to_numeric(out[return_col], errors="coerce")
        out = out.dropna(subset=["ticker", "month_end", "ret_1m"])
        # This also correctly reduces daily-return inputs to a monthly price return.
        out = (
            out.groupby(["ticker", "month_end"], as_index=False)["ret_1m"]
            .agg(lambda x: float((1.0 + x).prod() - 1.0))
            .sort_values(["ticker", "month_end"])
        )
        return out.reset_index(drop=True)

    price_col = _first_column(out.columns, ["close", "Close", "price", "index_level"])
    if price_col is None:
        raise ValueError("returns artifact needs a return or close/price column")
    out["close"] = pd.to_numeric(out[price_col], errors="coerce")
    out = out.dropna(subset=["ticker", "month_end", "close"])
    out = out.sort_values(["ticker", "month_end"]).groupby(
        ["ticker", "month_end"], as_index=False
    )["close"].last()
    out["ret_1m"] = out.groupby("ticker")["close"].pct_change()
    return out.dropna(subset=["ret_1m"])[["ticker", "month_end", "ret_1m"]].reset_index(drop=True)


def _prepare_master(raw: pd.DataFrame) -> pd.DataFrame:
    out = raw.copy()
    ticker_col = _first_column(out.columns, ["ticker", "code", "symbol", "종목코드"])
    if ticker_col is None:
        raise ValueError("certified security master is missing a ticker column")
    if ticker_col != "ticker":
        out = out.rename(columns={ticker_col: "ticker"})
    out["ticker"] = _ticker_series(out["ticker"])
    out = out.dropna(subset=["ticker"]).drop_duplicates("ticker", keep="last")
    sector_col = _first_column(
        out.columns,
        [
            "sector",
            "sector_name",
            "industry",
            "industry_name",
            "industry_l4",
            "업종명",
        ],
    )
    if sector_col is not None and sector_col != "sector":
        out = out.rename(columns={sector_col: "sector"})
    if "sector" not in out.columns:
        out["sector"] = "UNKNOWN"
    out["sector"] = out["sector"].astype("string").fillna("UNKNOWN")
    return out.reset_index(drop=True)


def _attach_master_eligibility(features: pd.DataFrame, master: pd.DataFrame) -> pd.DataFrame:
    metadata_cols = [
        c
        for c in [
            "ticker",
            "name",
            "sector",
            "asset_class",
            "security_type",
            "equity_universe_eligible",
            "factor_eligible",
            "top_k_eligible",
        ]
        if c in master.columns
    ]
    meta = master[metadata_cols].copy()
    meta["certified_master_matched"] = True
    out = features.merge(meta, on="ticker", how="left", suffixes=("", "__master"))
    for col in ["name", "sector"]:
        master_col = f"{col}__master"
        if master_col in out.columns:
            if col in out.columns:
                out[col] = out[col].where(out[col].notna(), out[master_col])
            else:
                out[col] = out[master_col]
            out = out.drop(columns=[master_col])
    if "sector" not in out.columns:
        out["sector"] = "UNKNOWN"
    out["sector"] = out["sector"].astype("string").fillna("UNKNOWN")

    out["certified_master_matched"] = (
        out["certified_master_matched"].astype("boolean").fillna(False).astype(bool)
    )
    # A current snapshot is already an acknowledged PIT limitation, but silently
    # treating rows absent from that supplied master as eligible would be worse.
    eligible = out["certified_master_matched"].copy()
    for flag in ["equity_universe_eligible", "factor_eligible", "top_k_eligible"]:
        if flag in out.columns:
            eligible &= _as_bool(out[flag], default=True)
    if "asset_class" in out.columns:
        asset = out["asset_class"].astype("string").str.strip().str.upper()
        eligible &= asset.isna() | asset.eq("") | asset.eq("EQUITY")
    if "security_type" in out.columns:
        security_type = out["security_type"].astype("string").str.upper()
        eligible &= ~security_type.str.contains("ETF|ETN|FUND|REIT|SPAC", na=False)

    # The cash-equivalent contract is fail-safe even when a historical master lacks
    # the newer classification flags.
    eligible &= out["ticker"].ne("437350")
    out["backtest_equity_eligible"] = eligible
    return out


def _load_strategy_contract(path: str | Path, strategy_name: str) -> dict[str, Any]:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    payload = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    strategies = payload.get("strategies", {}) if isinstance(payload, dict) else {}
    strategy = strategies.get(strategy_name) if isinstance(strategies, dict) else None
    if not isinstance(strategy, dict):
        raise KeyError(f"strategy {strategy_name!r} not found in {p}")
    weights = strategy.get("weights")
    if not isinstance(weights, dict) or not weights:
        raise ValueError(f"strategy {strategy_name!r} has no weights")
    weights = {str(k): float(v) for k, v in weights.items() if float(v) != 0.0}
    scoring = strategy.get("scoring", {}) if isinstance(strategy.get("scoring"), dict) else {}
    selection = strategy.get("selection", {}) if isinstance(strategy.get("selection"), dict) else {}
    return {
        "weights": weights,
        "filters": strategy.get("filters", {}) if isinstance(strategy.get("filters"), dict) else {},
        "scoring": scoring,
        "selection": selection,
        "quality_soft_penalty": scoring.get("quality_soft_penalty", {})
        if isinstance(scoring.get("quality_soft_penalty"), dict)
        else {},
        "expectation_overlay": scoring.get("expectation_overlay", {})
        if isinstance(scoring.get("expectation_overlay"), dict)
        else {},
    }


def _ensure_filter_aliases(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    aliases = {
        "op_cur_q": ["OpIncome_cur_q", "op_income_cur_q", "operating_income_cur_q"],
        "OpIncome_ttm": ["op_income", "op_income_ttm", "operating_income_ttm"],
        "traded_value": ["거래대금", "trading_value"],
        "mcap": ["market_cap", "시가총액"],
        "op_qoq": ["OpIncome_qoq", "op_income_qoq", "operating_income_qoq"],
    }
    for target, candidates in aliases.items():
        if target not in out.columns:
            source = _first_column(out.columns, candidates)
            if source is not None:
                out[target] = out[source]
    return out


def _required_filter_columns(filters: dict[str, Any]) -> list[str]:
    """Return the feature columns whose values are required by configured filters."""

    prefixes = ("require_notnull_", "maxq_", "minq_", "max_", "min_")
    columns: set[str] = set()
    for key in filters:
        text = str(key)
        for prefix in prefixes:
            if text.startswith(prefix) and len(text) > len(prefix):
                columns.add(text[len(prefix) :])
                break
    return sorted(columns)


def _historical_filter_input_audit(
    features: pd.DataFrame,
    contract: dict[str, Any],
    cutoff: pd.Timestamp,
) -> dict[str, Any]:
    """Audit PIT filter inputs without borrowing values from a current master.

    ``apply_filters`` intentionally skips an absent column. That behaviour is
    convenient in exploratory backtests but cannot silently validate a production
    candidate. The audit records both column presence and row-level value coverage
    for historical, master-eligible cross sections.
    """

    required = _required_filter_columns(contract.get("filters", {}))
    historical = features.loc[
        features["rebalance_month"].le(cutoff)
        & features["backtest_equity_eligible"].fillna(False)
    ].copy()
    eligible_rows = len(historical)
    coverage: dict[str, float] = {}
    missing: list[str] = []
    incomplete: list[str] = []
    for column in required:
        if column not in historical.columns:
            coverage[column] = 0.0
            missing.append(column)
            continue
        ratio = float(historical[column].notna().mean()) if eligible_rows else 0.0
        coverage[column] = ratio
        if not math.isclose(ratio, 1.0, abs_tol=1e-12):
            incomplete.append(column)

    complete = not missing and not incomplete
    if not required:
        status = "NOT_CONFIGURED"
        complete = True
    elif missing:
        status = "UNAVAILABLE_MISSING_COLUMNS"
    elif incomplete:
        status = "PARTIAL_VALUE_COVERAGE"
    else:
        status = "FULL"
    return {
        "required_columns": required,
        "missing_columns": missing,
        "incomplete_columns": incomplete,
        "eligible_feature_rows": eligible_rows,
        "column_coverage": coverage,
        "minimum_row_coverage": min(coverage.values()) if coverage else 1.0,
        "complete": complete,
        "status": status,
    }


def _score_cross_section(frame: pd.DataFrame, contract: dict[str, Any], rebalance_month: pd.Timestamp) -> pd.DataFrame:
    """Compute the shared, holding-independent validated score once per quarter."""

    eligible = frame.loc[frame["backtest_equity_eligible"]].copy()
    eligible = _ensure_filter_aliases(eligible)
    filtered = apply_filters(eligible, contract["filters"], verbose=False)
    if "op_qoq" in filtered.columns:
        op_qoq = pd.to_numeric(filtered["op_qoq"], errors="coerce")
        strict = filtered.loc[op_qoq.notna() & op_qoq.gt(0)].copy()
        if not strict.empty:
            filtered = strict
    if filtered.empty:
        raise ValueError(f"no eligible securities after filters at {rebalance_month.date()}")

    scoring = contract["scoring"]
    use_robust_z = bool(scoring.get("use_robust_z", False))
    clip_z_raw = scoring.get("clip_z", 5.0)
    clip_z = None if clip_z_raw is None or float(clip_z_raw) < 0 else float(clip_z_raw)
    clip_tiers = scoring.get("clip_tiers", [])
    if not isinstance(clip_tiers, list):
        clip_tiers = []
    raw_factors = set(scoring.get("raw_factors", []) or [])

    out = filtered.copy()
    out["score_base"] = 0.0
    for factor, weight in contract["weights"].items():
        if factor not in out.columns:
            continue
        raw = pd.to_numeric(out[factor], errors="coerce")
        if factor in raw_factors:
            signal = raw.fillna(0.0).astype(float)
        else:
            signal = standardize_factor(
                safe_fill_for_z(raw),
                use_robust_z=use_robust_z,
                clip_z=clip_z,
                clip_tiers=clip_tiers,
            )
        multiplier = pd.Series(1.0, index=out.index, dtype=float)
        if "CFO" in factor or "CFO_to_Assets" in factor:
            cfo_missing_source = (
                out["CFO_isnull"]
                if "CFO_isnull" in out.columns
                else pd.Series(0.0, index=out.index)
            )
            cfo_warn_source = (
                out["CFO_warn"] if "CFO_warn" in out.columns else pd.Series(0.0, index=out.index)
            )
            cfo_missing = pd.to_numeric(cfo_missing_source, errors="coerce").fillna(0.0)
            cfo_warn = pd.to_numeric(cfo_warn_source, errors="coerce").fillna(0.0)
            multiplier = (1.0 - 0.7 * cfo_missing - 0.4 * cfo_warn).clip(0.0, 1.0)
        contribution = float(weight) * signal * multiplier
        out[f"{factor}__signal"] = signal
        out[f"{factor}__contribution"] = contribution
        out["score_base"] += contribution

    if not any(factor in out.columns for factor in contract["weights"]):
        raise ValueError("none of the configured strategy factors exist in the features artifact")
    out["score_total"] = out["score_base"]
    out = apply_expectation_overlay(
        out,
        contract["expectation_overlay"],
        asof_date=rebalance_month,
        price_history=None,
    )
    out = apply_quality_soft_penalty(out, contract["quality_soft_penalty"])
    out["score_final"] = pd.to_numeric(out["score_total"], errors="coerce").fillna(-np.inf)
    out = out.sort_values(["score_final", "ticker"], ascending=[False, True]).reset_index(drop=True)
    out["model_rank"] = np.arange(1, len(out) + 1, dtype=int)
    return out


def _legacy_selection(
    scored: pd.DataFrame,
    previous: set[str],
    top_k: int,
    holding_bonus: float,
    keep_current_top_n: int,
) -> pd.DataFrame:
    work = scored.copy()
    is_previous = work["ticker"].isin(previous)
    work["holding_bonus_applied"] = np.where(is_previous, float(holding_bonus), 0.0)
    work["selection_score"] = work["score_final"] + work["holding_bonus_applied"]
    work = work.sort_values(
        ["selection_score", "score_final", "ticker"], ascending=[False, False, True]
    ).reset_index(drop=True)

    keep_count = min(max(int(keep_current_top_n), 0), max(int(top_k), 0))
    reserved = work.loc[work["ticker"].isin(previous)].head(keep_count).copy()
    reserved["selection_bucket"] = "keep_current_top_n"
    reserved["kept_from_previous"] = 1
    fill = work.loc[~work["ticker"].isin(set(reserved["ticker"]))].head(
        max(int(top_k) - len(reserved), 0)
    ).copy()
    fill["selection_bucket"] = "rank_fill"
    fill["kept_from_previous"] = 0
    pieces = [piece for piece in [reserved, fill] if not piece.empty]
    selected = pd.concat(pieces, ignore_index=True) if pieces else work.head(0).copy()
    return selected.sort_values(
        ["selection_score", "score_final", "ticker"], ascending=[False, False, True]
    ).reset_index(drop=True)


def _fresh_selection(scored: pd.DataFrame, top_k: int) -> pd.DataFrame:
    """Select without accepting (or consulting) any current/previous ticker set."""

    selected = scored.head(max(int(top_k), 0)).copy()
    selected["holding_bonus_applied"] = 0.0
    selected["selection_score"] = selected["score_final"]
    selected["selection_bucket"] = "fresh_start_rank"
    selected["kept_from_previous"] = 0
    return selected.reset_index(drop=True)


def _build_selections(
    features: pd.DataFrame,
    contract: dict[str, Any],
    cutoff: pd.Timestamp,
    top_k: int,
    holding_bonus: float,
    keep_current_top_n: int,
    cash_weight: float,
) -> pd.DataFrame:
    rows: list[pd.DataFrame] = []
    previous_by_variant: dict[str, set[str]] = {LEGACY_VARIANT: set(), FRESH_START_VARIANT: set()}
    rebalance_months = sorted(
        pd.Timestamp(value)
        for value in features.loc[features["rebalance_month"].le(cutoff), "rebalance_month"].unique()
    )
    if not rebalance_months:
        raise ValueError("no rebalance observations exist on or before cutoff_end")

    for rebalance_month in rebalance_months:
        cross_section = features.loc[features["rebalance_month"].eq(rebalance_month)].copy()
        scored = _score_cross_section(cross_section, contract, rebalance_month)

        variant_picks = {
            LEGACY_VARIANT: _legacy_selection(
                scored,
                previous_by_variant[LEGACY_VARIANT],
                top_k,
                holding_bonus,
                keep_current_top_n,
            ),
            # Deliberately no previous/current ticker argument in this call.
            FRESH_START_VARIANT: _fresh_selection(scored, top_k),
        }
        for variant, picked in variant_picks.items():
            previous = previous_by_variant[variant]
            picked = picked.copy()
            picked["variant"] = variant
            picked["rebalance_month"] = rebalance_month
            picked["selection_rank"] = np.arange(1, len(picked) + 1, dtype=int)
            picked["model_score"] = picked["score_final"]
            picked["previously_selected"] = picked["ticker"].isin(previous)
            picked["overlap_with_previous"] = picked["previously_selected"]
            picked["keep_current_top_n_contract"] = (
                min(int(keep_current_top_n), int(top_k)) if variant == LEGACY_VARIANT else 0
            )
            equity_weight = 1.0 if variant == LEGACY_VARIANT else 1.0 - cash_weight
            picked["target_weight"] = equity_weight / len(picked) if len(picked) else 0.0
            rows.append(picked)
            previous_by_variant[variant] = set(picked["ticker"].astype(str))
    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()


def _prepare_benchmark(path: str | Path | None) -> tuple[pd.DataFrame, str]:
    if path is None or not str(path).strip():
        return pd.DataFrame(columns=["month_end", "benchmark_return"]), "MISSING"
    p = Path(path)
    if not p.is_file():
        return pd.DataFrame(columns=["month_end", "benchmark_return"]), "MISSING"
    raw = _read_table(p)
    if raw.empty:
        return pd.DataFrame(columns=["month_end", "benchmark_return"]), "EMPTY"

    data = raw.copy()
    # A combined history may contain the KRX300 and 437350.  Prefer explicit KRX300
    # labels, then the official KRX index code 1028.
    label_col = _first_column(data.columns, ["ticker", "index_code", "symbol", "name", "index_name"])
    if label_col is not None and data[label_col].nunique(dropna=True) > 1:
        label = data[label_col].astype("string").str.upper().str.replace(" ", "", regex=False)
        mask = label.str.contains("KRX300|코스피300", na=False) | label.isin(["1028", "001028"])
        if mask.any():
            data = data.loc[mask].copy()

    date_col = _first_column(data.columns, ["month_end", "month", "date", "Date", "asof"])
    if date_col is None:
        return pd.DataFrame(columns=["month_end", "benchmark_return"]), "INVALID_SCHEMA"
    data["month_end"] = _month_end(data[date_col])
    ret_col = _return_column(data.columns)
    if ret_col is not None:
        data["benchmark_return"] = pd.to_numeric(data[ret_col], errors="coerce")
        data = data.dropna(subset=["month_end", "benchmark_return"])
        monthly = data.groupby("month_end", as_index=False)["benchmark_return"].agg(
            lambda x: float((1.0 + x).prod() - 1.0)
        )
    else:
        price_col = _first_column(data.columns, ["close", "Close", "price", "index_level"])
        if price_col is None:
            return pd.DataFrame(columns=["month_end", "benchmark_return"]), "INVALID_SCHEMA"
        data["close"] = pd.to_numeric(data[price_col], errors="coerce")
        monthly = data.dropna(subset=["month_end", "close"]).groupby("month_end", as_index=False)[
            "close"
        ].last()
        monthly["benchmark_return"] = monthly["close"].pct_change()
        monthly = monthly.dropna(subset=["benchmark_return"])[["month_end", "benchmark_return"]]
    return monthly.sort_values("month_end").reset_index(drop=True), "LOADED"


def _weights(selected: pd.DataFrame) -> dict[str, float]:
    return {
        str(row.ticker): float(row.target_weight)
        for row in selected[["ticker", "target_weight"]].itertuples(index=False)
    }


def _fresh_reset_allocation(
    previous_weights: dict[str, float],
    nominal_target_weights: dict[str, float],
    commission_rate: float,
    sell_tax_rate: float,
) -> tuple[dict[str, float], float, dict[str, Any]]:
    """Fund a 90/10 reset without borrowing the purchase commission from nowhere."""

    pre_reset_nav = 1.0
    sell_principal = float(sum(previous_weights.values())) if previous_weights else 0.0
    sell_commission = sell_principal * commission_rate
    sell_tax = sell_principal * sell_tax_rate
    after_sale_capital = pre_reset_nav - sell_commission - sell_tax
    if after_sale_capital <= 0.0:
        raise ValueError("fresh reset has non-positive capital after sale costs")

    equity_budget_including_buy_commission = (
        (1.0 - DEFAULT_CASH_EQUIVALENT_WEIGHT) * after_sale_capital
    )
    buy_principal = equity_budget_including_buy_commission / (1.0 + commission_rate)
    buy_commission = buy_principal * commission_rate
    cash_principal = DEFAULT_CASH_EQUIVALENT_WEIGHT * after_sale_capital
    post_cost_component_capital = buy_principal + cash_principal
    total_cost = sell_commission + sell_tax + buy_commission
    if post_cost_component_capital <= 0.0:
        raise ValueError("fresh reset has non-positive post-cost component capital")

    nominal_equity_total = float(sum(nominal_target_weights.values()))
    if nominal_equity_total <= 0.0:
        raise ValueError("fresh reset has no nominal equity target")
    post_cost_equity_weight = buy_principal / post_cost_component_capital
    post_cost_cash_weight = cash_principal / post_cost_component_capital
    actual_weights = {
        ticker: post_cost_equity_weight * nominal_weight / nominal_equity_total
        for ticker, nominal_weight in nominal_target_weights.items()
    }
    gross_traded = sell_principal + buy_principal
    turnover = buy_principal if not previous_weights else gross_traded / 2.0
    accounting_error = abs(post_cost_component_capital + total_cost - pre_reset_nav)
    ledger = {
        # Backward-compatible aliases, now explicitly principal ratios on pre-reset NAV.
        "buy_ratio": buy_principal,
        "sell_ratio": sell_principal,
        "buy_principal_ratio_on_pre_reset_nav": buy_principal,
        "sell_principal_ratio_on_pre_reset_nav": sell_principal,
        "gross_traded_ratio": gross_traded,
        "turnover": turnover,
        "pre_reset_nav_ratio_base": pre_reset_nav,
        "sell_commission_ratio_on_pre_reset_nav": sell_commission,
        "sell_tax_ratio_on_pre_reset_nav": sell_tax,
        "after_sale_capital_ratio_on_pre_reset_nav": after_sale_capital,
        "equity_budget_including_buy_commission_ratio": equity_budget_including_buy_commission,
        "buy_commission_ratio_on_pre_reset_nav": buy_commission,
        "cash_bucket_principal_ratio_on_pre_reset_nav": cash_principal,
        "post_cost_component_capital_ratio": post_cost_component_capital,
        "post_cost_equity_weight": post_cost_equity_weight,
        "post_cost_cash_equivalent_weight": post_cost_cash_weight,
        "cash_equivalent_trade_cost_ratio": 0.0,
        "commission_cost_ratio": sell_commission + buy_commission,
        "sell_tax_cost_ratio": sell_tax,
        "total_cost_ratio": total_cost,
        "reset_nav_factor": post_cost_component_capital,
        "reset_accounting_identity_error": accounting_error,
        "post_cost_cash_floor_satisfied": post_cost_cash_weight
        >= DEFAULT_CASH_EQUIVALENT_WEIGHT - 1e-12,
    }
    return actual_weights, post_cost_cash_weight, ledger


def _trade_ledger(
    variant: str,
    previous_weights: dict[str, float],
    current_weights: dict[str, float],
    commission_rate: float,
    sell_tax_rate: float,
) -> dict[str, Any]:
    if variant == FRESH_START_VARIANT:
        raise ValueError("fresh-start trades must use _fresh_reset_allocation")
    tickers = set(previous_weights) | set(current_weights)
    buy_ratio = float(
        sum(max(current_weights.get(t, 0.0) - previous_weights.get(t, 0.0), 0.0) for t in tickers)
    )
    sell_ratio = float(
        sum(max(previous_weights.get(t, 0.0) - current_weights.get(t, 0.0), 0.0) for t in tickers)
    )
    buy_commission = buy_ratio * commission_rate
    sell_commission = sell_ratio * commission_rate
    commission = buy_commission + sell_commission
    sell_tax = sell_ratio * sell_tax_rate
    gross = buy_ratio + sell_ratio
    turnover = buy_ratio if not previous_weights else gross / 2.0
    return {
        "buy_ratio": buy_ratio,
        "sell_ratio": sell_ratio,
        "buy_principal_ratio_on_pre_reset_nav": buy_ratio,
        "sell_principal_ratio_on_pre_reset_nav": sell_ratio,
        "gross_traded_ratio": gross,
        "turnover": turnover,
        "pre_reset_nav_ratio_base": 1.0,
        "sell_commission_ratio_on_pre_reset_nav": sell_commission,
        "sell_tax_ratio_on_pre_reset_nav": sell_tax,
        "after_sale_capital_ratio_on_pre_reset_nav": 1.0 - sell_commission - sell_tax,
        "equity_budget_including_buy_commission_ratio": buy_ratio + buy_commission,
        "buy_commission_ratio_on_pre_reset_nav": buy_commission,
        "cash_bucket_principal_ratio_on_pre_reset_nav": 0.0,
        "post_cost_component_capital_ratio": 1.0 - commission - sell_tax,
        "post_cost_equity_weight": 1.0,
        "post_cost_cash_equivalent_weight": 0.0,
        "cash_equivalent_trade_cost_ratio": 0.0,
        "commission_cost_ratio": commission,
        "sell_tax_cost_ratio": sell_tax,
        "total_cost_ratio": commission + sell_tax,
        "reset_nav_factor": 1.0 - commission - sell_tax,
        "reset_accounting_identity_error": 0.0,
        "post_cost_cash_floor_satisfied": True,
    }


def _compound(values: pd.Series | Iterable[float]) -> float:
    series = pd.Series(values, dtype=float).dropna()
    if series.empty:
        return float("nan")
    return float((1.0 + series).prod() - 1.0)


def _build_performance(
    selections: pd.DataFrame,
    returns: pd.DataFrame,
    benchmark: pd.DataFrame,
    cutoff: pd.Timestamp,
    commission_rate: float,
    sell_tax_rate: float,
    cash_return: float,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    returns_lookup = returns.set_index(["month_end", "ticker"])["ret_1m"]
    all_return_months = sorted(
        pd.Timestamp(m) for m in returns.loc[returns["month_end"].le(cutoff), "month_end"].unique()
    )
    benchmark_lookup = benchmark.set_index("month_end")["benchmark_return"].to_dict()
    monthly_rows: list[dict[str, Any]] = []
    period_rows: list[dict[str, Any]] = []

    for variant in [LEGACY_VARIANT, FRESH_START_VARIANT]:
        variant_selection = selections.loc[selections["variant"].eq(variant)].copy()
        rebalance_months = sorted(pd.Timestamp(m) for m in variant_selection["rebalance_month"].unique())
        previous_weights: dict[str, float] = {}
        previous_cash_weight = 1.0
        nav = 1.0
        benchmark_nav = 1.0
        for index, rebalance_month in enumerate(rebalance_months):
            selected = variant_selection.loc[
                variant_selection["rebalance_month"].eq(rebalance_month)
            ].copy()
            nominal_current_weights = _weights(selected)
            if variant == FRESH_START_VARIANT:
                current_weights, reset_cash_weight, trade = _fresh_reset_allocation(
                    previous_weights,
                    nominal_current_weights,
                    commission_rate,
                    sell_tax_rate,
                )
            else:
                current_weights = nominal_current_weights
                reset_cash_weight = 0.0
                trade = _trade_ledger(
                    variant,
                    previous_weights,
                    current_weights,
                    commission_rate,
                    sell_tax_rate,
                )
            next_rebalance = rebalance_months[index + 1] if index + 1 < len(rebalance_months) else None
            cutoff_month_end = cutoff.to_period("M").to_timestamp("M")
            last_complete_month = (
                cutoff_month_end
                if cutoff >= cutoff_month_end
                else cutoff_month_end - pd.offsets.MonthEnd(1)
            )
            expected_period_end = next_rebalance if next_rebalance is not None else last_complete_month
            expected_period_months = list(
                pd.date_range(
                    rebalance_month + pd.offsets.MonthEnd(1),
                    expected_period_end,
                    freq="ME",
                )
            )
            # Strictly greater than rebalance_month is the anti-look-ahead contract.
            months = [
                month
                for month in all_return_months
                if month > rebalance_month
                and month <= cutoff
                and (next_rebalance is None or month <= next_rebalance)
            ]
            previous_set = set(previous_weights)
            current_set = set(current_weights)
            overlap_count = len(previous_set & current_set)
            overlap_ratio = (
                overlap_count / max(len(previous_set), len(current_set), 1)
                if previous_set
                else float("nan")
            )
            nav_before_reset = nav
            nav *= float(trade["reset_nav_factor"])
            nav_after_reset = nav

            period_monthly: list[float] = []
            period_gross_monthly: list[float] = []
            period_benchmark: list[float] = []
            coverage_values: list[float] = []
            # Reset only at the quarterly rebalance.  Between rebalances the
            # constituent and cash weights below drift with their own returns.
            drifted_weights = current_weights.copy()
            drifted_cash_weight = float(reset_cash_weight)
            starting_equity_weight = float(sum(drifted_weights.values()))
            starting_cash_weight = float(drifted_cash_weight)
            for month_number, month in enumerate(months):
                constituent_returns: list[float] = []
                found_weight = 0.0
                equity_return = 0.0
                weight_start = drifted_weights.copy()
                cash_weight_start = float(drifted_cash_weight)
                realized_returns: dict[str, float] = {}
                for ticker, weight in weight_start.items():
                    try:
                        security_return = float(returns_lookup.loc[(month, ticker)])
                    except KeyError:
                        security_return = float("nan")
                    realized_returns[ticker] = security_return
                    constituent_returns.append(security_return)
                    if np.isfinite(security_return):
                        equity_return += weight * security_return
                        found_weight += weight
                constituent_coverage = (
                    float(np.isfinite(constituent_returns).sum()) / len(constituent_returns)
                    if constituent_returns
                    else 0.0
                )
                coverage_values.append(constituent_coverage)
                gross_return = (
                    equity_return
                    + cash_weight_start * cash_return
                    if constituent_coverage == 1.0
                    else float("nan")
                )

                # Missing returns make the performance observation provisional.
                # For weight carry only, a zero return prevents an unavailable
                # observation from silently deleting the constituent.
                ending_values = {
                    ticker: weight
                    * (1.0 + (realized_returns[ticker] if np.isfinite(realized_returns[ticker]) else 0.0))
                    for ticker, weight in weight_start.items()
                }
                ending_cash_value = cash_weight_start * (1.0 + cash_return)
                ending_total_value = float(sum(ending_values.values()) + ending_cash_value)
                if ending_total_value > 0.0 and np.isfinite(ending_total_value):
                    drifted_weights = {
                        ticker: value / ending_total_value for ticker, value in ending_values.items()
                    }
                    drifted_cash_weight = ending_cash_value / ending_total_value
                else:
                    drifted_weights = {ticker: 0.0 for ticker in ending_values}
                    drifted_cash_weight = 0.0

                cost = trade["total_cost_ratio"] if month_number == 0 else 0.0
                net_return = (
                    (
                        trade["reset_nav_factor"] * (1.0 + gross_return) - 1.0
                        if month_number == 0
                        else gross_return
                    )
                    if np.isfinite(gross_return)
                    else float("nan")
                )
                benchmark_return = float(benchmark_lookup.get(month, np.nan))
                if np.isfinite(net_return):
                    # Reset cost has already reduced NAV exactly once above; only
                    # the asset return is applied here.
                    nav *= 1.0 + gross_return
                    period_monthly.append(net_return)
                    period_gross_monthly.append(gross_return)
                if np.isfinite(benchmark_return):
                    benchmark_nav *= 1.0 + benchmark_return
                    period_benchmark.append(benchmark_return)
                monthly_rows.append(
                    {
                        "variant": variant,
                        "month_end": month,
                        "active_rebalance_month": rebalance_month,
                        "constituent_count": len(weight_start),
                        "constituent_coverage": constituent_coverage,
                        "equity_weight_with_return": found_weight,
                        "equity_weight_start": float(sum(weight_start.values())),
                        "equity_weight_end": float(sum(drifted_weights.values())),
                        "cash_equivalent_weight": cash_weight_start,
                        "cash_equivalent_weight_end": float(drifted_cash_weight),
                        "target_cash_equivalent_weight": DEFAULT_CASH_EQUIVALENT_WEIGHT
                        if variant == FRESH_START_VARIANT
                        else 0.0,
                        "cash_equivalent_return": cash_return
                        if variant == FRESH_START_VARIANT
                        else 0.0,
                        "gross_return": gross_return,
                        "cost_ratio": cost,
                        "reset_nav_factor": trade["reset_nav_factor"]
                        if month_number == 0
                        else 1.0,
                        "net_return": net_return,
                        "nav": nav,
                        "benchmark_return": benchmark_return,
                        "benchmark_nav": benchmark_nav,
                    }
                )

            gross_period_return = _compound(period_gross_monthly)
            net_period_return = _compound(period_monthly)
            benchmark_period_return = (
                _compound(period_benchmark) if len(period_benchmark) == len(months) and months else float("nan")
            )
            complete_rebalance_period = bool(
                next_rebalance is not None
                and expected_period_months
                and months == expected_period_months
                and coverage_values
                and min(coverage_values) == 1.0
            )
            period_rows.append(
                {
                    "variant": variant,
                    "rebalance_month": rebalance_month,
                    "next_rebalance_month": next_rebalance,
                    "return_start_month": months[0] if months else pd.NaT,
                    "return_end_month": months[-1] if months else pd.NaT,
                    "month_count": len(months),
                    "expected_calendar_month_count": len(expected_period_months),
                    "calendar_month_gap_count": max(len(expected_period_months) - len(months), 0),
                    "calendar_month_source_coverage": (
                        len(months) / len(expected_period_months) if expected_period_months else float("nan")
                    ),
                    "return_coverage": float(np.mean(coverage_values)) if coverage_values else float("nan"),
                    "nav_before_reset": nav_before_reset,
                    "nav_after_reset_before_returns": nav_after_reset,
                    "reset_cost_nav_amount": nav_before_reset - nav_after_reset,
                    "pre_rebalance_equity_weight": float(sum(previous_weights.values())),
                    "pre_rebalance_cash_equivalent_weight": float(previous_cash_weight),
                    "starting_equity_weight": starting_equity_weight,
                    "starting_cash_equivalent_weight": starting_cash_weight,
                    "ending_equity_weight": float(sum(drifted_weights.values())),
                    "ending_cash_equivalent_weight": float(drifted_cash_weight),
                    "previous_position_count": len(previous_set),
                    "position_count": len(current_set),
                    "overlap_count": overlap_count,
                    "overlap_ratio": overlap_ratio,
                    **trade,
                    "gross_period_return": gross_period_return,
                    "net_period_return": net_period_return,
                    "benchmark_period_return": benchmark_period_return,
                    "excess_period_return": net_period_return - benchmark_period_return
                    if np.isfinite(net_period_return) and np.isfinite(benchmark_period_return)
                    else float("nan"),
                    "complete_rebalance_period": complete_rebalance_period,
                    "period_status": (
                        "COMPLETE"
                        if complete_rebalance_period
                        else ("PARTIAL" if months else "NO_COMPLETE_RETURN_MONTH")
                    ),
                }
            )
            # The next rebalance trades against actual drifted ending weights, not
            # against the stale target weights from this rebalance.
            previous_weights = drifted_weights
            previous_cash_weight = float(drifted_cash_weight)

    monthly = pd.DataFrame(monthly_rows)
    if monthly.empty:
        monthly = pd.DataFrame(
            columns=[
                "variant",
                "month_end",
                "active_rebalance_month",
                "constituent_count",
                "constituent_coverage",
                "equity_weight_with_return",
                "equity_weight_start",
                "equity_weight_end",
                "cash_equivalent_weight",
                "cash_equivalent_weight_end",
                "target_cash_equivalent_weight",
                "cash_equivalent_return",
                "gross_return",
                "cost_ratio",
                "reset_nav_factor",
                "net_return",
                "nav",
                "benchmark_return",
                "benchmark_nav",
                "running_peak",
                "drawdown",
            ]
        )
    else:
        # Include the pre-backtest NAV=1 baseline so a loss (or entry cost) in the
        # first observed month is represented in MDD.
        monthly["running_peak"] = monthly.groupby("variant")["nav"].cummax().clip(lower=1.0)
        monthly["drawdown"] = monthly["nav"] / monthly["running_peak"] - 1.0
    periods = pd.DataFrame(period_rows)
    return periods, monthly


def _build_sector_exposure(selections: pd.DataFrame) -> pd.DataFrame:
    equity = (
        selections.groupby(["variant", "rebalance_month", "sector"], dropna=False, as_index=False)
        .agg(target_weight=("target_weight", "sum"), position_count=("ticker", "nunique"))
    )
    equity["asset_bucket"] = "EQUITY"
    cash_rebalances = selections.loc[
        selections["variant"].eq(FRESH_START_VARIANT), ["variant", "rebalance_month"]
    ].drop_duplicates()
    if not cash_rebalances.empty:
        cash_rebalances["sector"] = "CASH_EQUIVALENT"
        cash_rebalances["target_weight"] = DEFAULT_CASH_EQUIVALENT_WEIGHT
        cash_rebalances["position_count"] = 1
        cash_rebalances["asset_bucket"] = "CASH_EQUIVALENT"
        equity = pd.concat([equity, cash_rebalances], ignore_index=True)
    return equity.sort_values(["variant", "rebalance_month", "asset_bucket", "sector"]).reset_index(drop=True)


def _drawdown_recovery(monthly: pd.DataFrame) -> tuple[float, bool]:
    valid = monthly.dropna(subset=["nav", "drawdown"]).sort_values("month_end")
    if valid.empty:
        return float("nan"), False
    trough_index = valid["drawdown"].idxmin()
    trough = valid.loc[trough_index]
    before = valid.loc[valid["month_end"].le(trough["month_end"])]
    peak_nav = max(1.0, float(before["nav"].cummax().iloc[-1]))
    after = valid.loc[valid["month_end"].gt(trough["month_end"])]
    recovered = after.loc[after["nav"].ge(peak_nav - 1e-12)]
    if recovered.empty:
        return float("nan"), False
    recovery_month = pd.Timestamp(recovered.iloc[0]["month_end"])
    months = (recovery_month.year - trough["month_end"].year) * 12 + (
        recovery_month.month - trough["month_end"].month
    )
    return float(months), True


def _summary_metrics(
    variant: str,
    selections: pd.DataFrame,
    periods: pd.DataFrame,
    monthly: pd.DataFrame,
    sector_exposure: pd.DataFrame,
    benchmark: pd.DataFrame,
    benchmark_load_status: str,
    cash_history_coverage: float,
    legacy_holding_bonus: float,
    legacy_keep_current_top_n: int,
    cutoff: pd.Timestamp,
    pit_master_universe_status: str,
    master_match_stats: dict[str, Any],
    filter_input_stats: dict[str, Any],
) -> dict[str, Any]:
    sel = selections.loc[selections["variant"].eq(variant)]
    per = periods.loc[(periods["variant"].eq(variant)) & periods["month_count"].gt(0)].copy()
    mon = monthly.loc[monthly["variant"].eq(variant)].copy()
    valid_returns = pd.to_numeric(mon.get("net_return", pd.Series(dtype=float)), errors="coerce").dropna()
    n_months = len(valid_returns)
    observed_total_return = _compound(valid_returns)
    observed_cagr = (
        (1.0 + observed_total_return) ** (12.0 / n_months) - 1.0
        if n_months and observed_total_return > -1
        else np.nan
    )
    std = valid_returns.std(ddof=1)
    observed_sharpe = (
        math.sqrt(12.0) * valid_returns.mean() / std
        if len(valid_returns) > 1 and std > 0
        else np.nan
    )
    observed_mdd = pd.to_numeric(
        mon.get("drawdown", pd.Series(dtype=float)), errors="coerce"
    ).min()
    observed_recovery_months, observed_recovered = _drawdown_recovery(mon)

    completed_quarters = per.loc[per["complete_rebalance_period"].fillna(False)]
    observed_positive_hit = (
        completed_quarters["net_period_return"].gt(0).mean()
        if not completed_quarters.empty
        else np.nan
    )

    first_rebalance = sel["rebalance_month"].min() if not sel.empty else pd.NaT
    cutoff_month_end = cutoff.to_period("M").to_timestamp("M")
    last_complete_month = (
        cutoff_month_end if cutoff >= cutoff_month_end else cutoff_month_end - pd.offsets.MonthEnd(1)
    )
    first_expected_month = (
        pd.Timestamp(first_rebalance) + pd.offsets.MonthEnd(1)
        if pd.notna(first_rebalance)
        else pd.NaT
    )
    expected_months = (
        list(pd.date_range(first_expected_month, last_complete_month, freq="ME"))
        if pd.notna(first_expected_month) and first_expected_month <= last_complete_month
        else []
    )
    expected_set = set(expected_months)
    expected_count = len(expected_months)
    source_month_set = set(pd.to_datetime(mon.get("month_end", pd.Series(dtype="datetime64[ns]")).dropna()))
    source_present_count = len(expected_set & source_month_set)
    valid_month_set = set(
        pd.to_datetime(mon.loc[pd.to_numeric(mon["net_return"], errors="coerce").notna(), "month_end"])
    )
    complete_return_count = len(expected_set & valid_month_set)
    source_month_coverage = source_present_count / expected_count if expected_count else 0.0
    full_calendar_return_coverage = complete_return_count / expected_count if expected_count else 0.0
    calendar_gap_count = max(expected_count - complete_return_count, 0)
    source_gap_count = max(expected_count - source_present_count, 0)
    selected_security_coverage = (
        float(pd.to_numeric(mon["constituent_coverage"], errors="coerce").mean())
        if not mon.empty
        else 0.0
    )
    return_data_complete = bool(
        expected_count > 0
        and math.isclose(full_calendar_return_coverage, 1.0, abs_tol=1e-12)
        and math.isclose(selected_security_coverage, 1.0, abs_tol=1e-12)
    )
    filter_inputs_complete = bool(filter_input_stats.get("complete", False))
    formal_metrics_available = return_data_complete and filter_inputs_complete

    benchmark_expected = benchmark.loc[
        benchmark["month_end"].isin(expected_set)
        & pd.to_numeric(benchmark["benchmark_return"], errors="coerce").notna()
    ].drop_duplicates("month_end")
    benchmark_covered_count = len(benchmark_expected)
    benchmark_coverage = benchmark_covered_count / expected_count if expected_count else 0.0
    if benchmark_load_status != "LOADED":
        benchmark_status = benchmark_load_status
    elif expected_count == 0:
        benchmark_status = "NO_EVALUATION_WINDOW"
    elif benchmark_covered_count == 0:
        benchmark_status = "NO_OVERLAP"
    elif benchmark_covered_count < expected_count:
        benchmark_status = "PARTIAL"
    else:
        benchmark_status = "FULL"

    if benchmark_status == "FULL":
        benchmark_total = _compound(benchmark_expected.sort_values("month_end")["benchmark_return"])
        benchmark_cagr = (
            (1.0 + benchmark_total) ** (12.0 / expected_count) - 1.0
            if expected_count and benchmark_total > -1
            else np.nan
        )
    else:
        benchmark_total = benchmark_cagr = np.nan

    if formal_metrics_available and benchmark_status == "FULL":
        portfolio_expected = mon.loc[
            mon["month_end"].isin(expected_set), ["month_end", "net_return"]
        ].drop_duplicates("month_end")
        aligned = portfolio_expected.merge(
            benchmark_expected[["month_end", "benchmark_return"]], on="month_end", how="inner"
        ).sort_values("month_end")
        portfolio_aligned = _compound(aligned["net_return"])
        benchmark_aligned = _compound(aligned["benchmark_return"])
        excess_total = (1.0 + portfolio_aligned) / (1.0 + benchmark_aligned) - 1.0
        portfolio_aligned_cagr = (
            (1.0 + portfolio_aligned) ** (12.0 / expected_count) - 1.0
            if portfolio_aligned > -1
            else np.nan
        )
        excess_cagr = portfolio_aligned_cagr - benchmark_cagr
        benchmark_periods = completed_quarters.dropna(
            subset=["net_period_return", "benchmark_period_return"]
        )
        excess_hit = (
            benchmark_periods["net_period_return"]
            .gt(benchmark_periods["benchmark_period_return"])
            .mean()
            if not benchmark_periods.empty
            else np.nan
        )
    else:
        excess_total = excess_cagr = excess_hit = np.nan

    total_return = observed_total_return if formal_metrics_available else np.nan
    cagr = observed_cagr if formal_metrics_available else np.nan
    mdd = observed_mdd if formal_metrics_available else np.nan
    sharpe = observed_sharpe if formal_metrics_available else np.nan
    positive_hit = observed_positive_hit if formal_metrics_available else np.nan
    recovery_months = observed_recovery_months if formal_metrics_available else np.nan
    recovered = observed_recovered if formal_metrics_available else pd.NA

    period_overlap = per.loc[per["previous_position_count"].gt(0), "overlap_ratio"]
    sector = sector_exposure.loc[
        (sector_exposure["variant"].eq(variant)) & sector_exposure["asset_bucket"].eq("EQUITY")
    ]
    max_sector = sector.groupby("rebalance_month")["target_weight"].max().mean() if not sector.empty else np.nan
    hhi_by_rebalance = sel.groupby("rebalance_month")["target_weight"].apply(lambda x: float((x**2).sum()))
    equity_hhi = hhi_by_rebalance.mean() if not hhi_by_rebalance.empty else np.nan
    total_hhi = (
        equity_hhi + DEFAULT_CASH_EQUIVALENT_WEIGHT**2
        if variant == FRESH_START_VARIANT and np.isfinite(equity_hhi)
        else equity_hhi
    )
    cash_history_status = (
        "FULL"
        if cash_history_coverage >= 1.0
        else ("PARTIAL" if cash_history_coverage > 0.0 else "MISSING")
    )

    blockers: list[str] = []
    if not return_data_complete:
        if full_calendar_return_coverage < 1.0:
            blockers.append("FULL_CALENDAR_RETURN_COVERAGE_INCOMPLETE")
        if selected_security_coverage < 1.0:
            blockers.append("SELECTED_SECURITY_RETURN_COVERAGE_INCOMPLETE")
    missing_filter_columns = set(filter_input_stats.get("missing_columns", []))
    incomplete_filter_columns = set(filter_input_stats.get("incomplete_columns", []))
    unavailable_filter_columns = missing_filter_columns | incomplete_filter_columns
    if missing_filter_columns:
        blockers.append("HISTORICAL_REQUIRED_FILTER_INPUTS_UNAVAILABLE")
    elif incomplete_filter_columns:
        blockers.append("HISTORICAL_REQUIRED_FILTER_INPUTS_INCOMPLETE")
    if unavailable_filter_columns & {"mcap", "traded_value"}:
        blockers.append("HISTORICAL_MCAP_TRADED_VALUE_FILTER_INPUTS_UNAVAILABLE")
    if benchmark_status != "FULL":
        blockers.append(f"KRX300_HISTORY_{benchmark_status}")
    normalized_pit_status = str(pit_master_universe_status).strip().upper()
    if normalized_pit_status not in PIT_VERIFIED_STATUSES:
        blockers.append("HISTORICAL_POINT_IN_TIME_SECURITY_MASTER_UNIVERSE_UNAVAILABLE")
    if int(master_match_stats.get("unmatched_feature_rows", 0)) > 0:
        blockers.append("HISTORICAL_SECURITY_MASTER_UNMATCHED_FEATURE_ROWS")
    if variant == FRESH_START_VARIANT:
        blockers.append("CASH_EQUIVALENT_RETURN_PLACEHOLDER_437350")
        if cash_history_coverage < 1.0:
            blockers.append("437350_HISTORY_INCOMPLETE")
        blockers.append("MANUAL_VALIDATION_AND_PROMOTION_REQUIRED")
        promotion_status = "BLOCKED"
    else:
        blockers.append("REFERENCE_VARIANT_NOT_FOR_PROMOTION")
        promotion_status = "BLOCKED" if not formal_metrics_available else "REFERENCE_ONLY"

    return {
        "variant": variant,
        "holding_bonus_contract": legacy_holding_bonus if variant == LEGACY_VARIANT else 0.0,
        "keep_current_top_n_contract": legacy_keep_current_top_n
        if variant == LEGACY_VARIANT
        else 0,
        "holding_information_used_for_selection": variant == LEGACY_VARIANT,
        "pit_master_universe_status": normalized_pit_status,
        "security_master_feature_rows": int(master_match_stats.get("feature_rows", 0)),
        "security_master_matched_feature_rows": int(
            master_match_stats.get("matched_feature_rows", 0)
        ),
        "security_master_unmatched_feature_rows": int(
            master_match_stats.get("unmatched_feature_rows", 0)
        ),
        "security_master_match_coverage": float(master_match_stats.get("row_coverage", 0.0)),
        "security_master_unique_feature_tickers": int(
            master_match_stats.get("unique_feature_tickers", 0)
        ),
        "security_master_matched_unique_tickers": int(
            master_match_stats.get("matched_unique_tickers", 0)
        ),
        "security_master_unmatched_unique_tickers": int(
            master_match_stats.get("unmatched_unique_tickers", 0)
        ),
        "historical_filter_input_status": str(filter_input_stats.get("status", "UNKNOWN")),
        "historical_filter_inputs_complete": filter_inputs_complete,
        "historical_filter_eligible_feature_rows": int(
            filter_input_stats.get("eligible_feature_rows", 0)
        ),
        "historical_required_filter_columns": "|".join(
            filter_input_stats.get("required_columns", [])
        ),
        "historical_missing_filter_columns": "|".join(
            filter_input_stats.get("missing_columns", [])
        ),
        "historical_incomplete_filter_columns": "|".join(
            filter_input_stats.get("incomplete_columns", [])
        ),
        "historical_filter_input_minimum_row_coverage": float(
            filter_input_stats.get("minimum_row_coverage", 0.0)
        ),
        "historical_filter_input_column_coverage_json": json.dumps(
            filter_input_stats.get("column_coverage", {}),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        "evaluation_status": "OK" if formal_metrics_available else "PROVISIONAL_INCOMPLETE_DATA",
        "production_comparison_metrics_status": "AVAILABLE"
        if formal_metrics_available
        else (
            "UNAVAILABLE_HISTORICAL_FILTER_INPUTS"
            if not filter_inputs_complete
            else "UNAVAILABLE_INCOMPLETE_RETURN_DATA"
        ),
        "evaluation_start": first_expected_month,
        "evaluation_end": last_complete_month,
        "actual_observation_start": mon["month_end"].min() if not mon.empty else pd.NaT,
        "actual_observation_end": mon["month_end"].max() if not mon.empty else pd.NaT,
        "expected_calendar_months": expected_count,
        "calendar_months_with_source_rows": source_present_count,
        "calendar_month_source_coverage": source_month_coverage,
        "calendar_month_source_gap_count": source_gap_count,
        "calendar_months_with_complete_portfolio_return": complete_return_count,
        "full_calendar_return_coverage": full_calendar_return_coverage,
        "calendar_month_gap_count": calendar_gap_count,
        "observed_months": n_months,
        "rebalance_count": sel["rebalance_month"].nunique(),
        "evaluated_rebalance_count": len(per),
        "equity_weight": 1.0 if variant == LEGACY_VARIANT else 1.0 - DEFAULT_CASH_EQUIVALENT_WEIGHT,
        "cash_equivalent_weight": 0.0 if variant == LEGACY_VARIANT else DEFAULT_CASH_EQUIVALENT_WEIGHT,
        "cash_equivalent_return_assumption": 0.0,
        "cash_equivalent_history_status": "ZERO_RETURN_PLACEHOLDER"
        if variant == FRESH_START_VARIANT
        else "NOT_APPLICABLE",
        "cash_equivalent_437350_coverage": cash_history_coverage,
        "cash_equivalent_437350_history_status": cash_history_status,
        "observed_sample_metrics_status": "DIAGNOSTIC_ONLY_NOT_PRODUCTION_COMPARABLE",
        "observed_sample_total_return": observed_total_return,
        "observed_sample_CAGR": observed_cagr,
        "observed_sample_MDD": observed_mdd,
        "observed_sample_Sharpe": observed_sharpe,
        "observed_sample_positive_quarter_hit_rate": observed_positive_hit,
        "observed_sample_quarter_count": len(completed_quarters),
        "observed_sample_drawdown_recovery_months": observed_recovery_months,
        "observed_sample_drawdown_recovered": observed_recovered,
        "total_return": total_return,
        "CAGR": cagr,
        "MDD": mdd,
        "Sharpe": sharpe,
        "positive_quarter_hit_rate": positive_hit,
        "average_turnover": per["turnover"].mean() if not per.empty else np.nan,
        "total_turnover": per["turnover"].sum() if not per.empty else np.nan,
        "average_gross_traded_ratio": per["gross_traded_ratio"].mean() if not per.empty else np.nan,
        "total_gross_traded_ratio": per["gross_traded_ratio"].sum() if not per.empty else np.nan,
        "total_trading_cost_ratio": per["total_cost_ratio"].sum() if not per.empty else np.nan,
        "compounded_trading_cost_drag": 1.0 - float((1.0 - per["total_cost_ratio"]).prod())
        if not per.empty
        else np.nan,
        "minimum_post_cost_cash_equivalent_weight": per[
            "post_cost_cash_equivalent_weight"
        ].min()
        if not per.empty
        else np.nan,
        "post_cost_cash_floor_satisfied_all_rebalances": bool(
            per["post_cost_cash_floor_satisfied"].fillna(False).all()
        )
        if not per.empty
        else False,
        "maximum_reset_accounting_identity_error": per[
            "reset_accounting_identity_error"
        ].max()
        if not per.empty
        else np.nan,
        "average_overlap_ratio": period_overlap.mean() if not period_overlap.empty else np.nan,
        "average_equity_concentration_hhi": equity_hhi,
        "average_total_concentration_hhi": total_hhi,
        "average_max_sector_weight": max_sector,
        "drawdown_recovery_months": recovery_months,
        "drawdown_recovered": recovered,
        "selected_security_return_coverage": selected_security_coverage,
        "krx300_status": benchmark_status,
        "krx300_metrics_status": "AVAILABLE"
        if benchmark_status == "FULL"
        else "UNAVAILABLE_INCOMPLETE_KRX300_DATA",
        "krx300_excess_metrics_status": "AVAILABLE"
        if formal_metrics_available and benchmark_status == "FULL"
        else "UNAVAILABLE_INCOMPLETE_PORTFOLIO_OR_KRX300_DATA",
        "krx300_expected_months": expected_count,
        "krx300_covered_months": benchmark_covered_count,
        "krx300_gap_months": max(expected_count - benchmark_covered_count, 0),
        "krx300_monthly_coverage": benchmark_coverage,
        "krx300_total_return": benchmark_total,
        "krx300_CAGR": benchmark_cagr,
        "krx300_excess_total_return": excess_total,
        "krx300_excess_CAGR": excess_cagr,
        "krx300_excess_quarter_hit_rate": excess_hit,
        "promotion_status": promotion_status,
        "promotion_blockers": "|".join(dict.fromkeys(blockers)) if blockers else "NONE",
    }


def _cash_history_coverage(returns: pd.DataFrame, evaluation_months: list[pd.Timestamp]) -> float:
    if not evaluation_months:
        return 0.0
    available = set(
        returns.loc[returns["ticker"].eq("437350"), "month_end"].dropna().map(pd.Timestamp)
    )
    return len(available & set(evaluation_months)) / len(set(evaluation_months))


def run_fresh_start_comparison(
    features_path: str | Path,
    returns_path: str | Path,
    strategy_config_path: str | Path,
    master_path: str | Path,
    benchmark_path: str | Path | None,
    output_dir: str | Path,
    cutoff_end: str | pd.Timestamp,
    *,
    strategy_name: str = LEGACY_VARIANT,
    top_k: int | None = None,
    legacy_holding_bonus: float = 0.5,
    legacy_keep_current_top_n: int = 3,
    target_cash_equivalent_weight: float = DEFAULT_CASH_EQUIVALENT_WEIGHT,
    commission_rate: float = DEFAULT_COMMISSION_RATE,
    sell_tax_rate: float = DEFAULT_SELL_TAX_RATE,
    cash_equivalent_return: float = 0.0,
    pit_master_universe_status: str = DEFAULT_PIT_MASTER_UNIVERSE_STATUS,
) -> dict[str, Any]:
    """Run an immutable legacy-vs-fresh-start comparison.

    Fresh-start selection is a pure function of the quarter's model inputs and the
    strategy contract.  Previous holdings are retained only after selection to
    report overlap and to calculate the mandated full-reset trading costs.
    """

    cutoff = pd.Timestamp(cutoff_end)
    if pd.isna(cutoff):
        raise ValueError("cutoff_end is invalid")
    if not 0.0 <= float(target_cash_equivalent_weight) < 1.0:
        raise ValueError("target_cash_equivalent_weight must be in [0, 1)")
    if not math.isclose(
        float(target_cash_equivalent_weight), DEFAULT_CASH_EQUIVALENT_WEIGHT, abs_tol=1e-12
    ):
        raise ValueError("ADVISOR_FULL_RESET backtest requires a 10% cash-equivalent bucket")
    if float(cash_equivalent_return) != 0.0:
        raise ValueError("this validation contract requires the documented zero-return cash placeholder")
    if min(float(commission_rate), float(sell_tax_rate)) < 0:
        raise ValueError("transaction cost rates must be non-negative")

    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    paths = {key: output / filename for key, filename in OUTPUT_FILES.items()}
    existing = [path for path in paths.values() if path.exists()]
    if existing:
        raise FileExistsError(
            "immutable backtest outputs already exist; choose a new output_dir: "
            + ", ".join(str(path) for path in existing)
        )

    contract = _load_strategy_contract(strategy_config_path, strategy_name)
    resolved_top_k = int(
        top_k
        if top_k is not None
        else contract["selection"].get("portfolio_size", 10)
    )
    if resolved_top_k <= 0:
        raise ValueError("top_k must be positive")

    # Aliases are derived only from the historical feature artifact. In
    # particular, current-master market cap/trading values are never backfilled
    # into historical periods.
    features = _ensure_filter_aliases(_prepare_features(_read_table(features_path)))
    master = _prepare_master(_read_table(master_path))
    features = _attach_master_eligibility(features, master)
    master_audit = features.loc[features["rebalance_month"].le(cutoff)].copy()
    master_matched = master_audit["certified_master_matched"].fillna(False).astype(bool)
    feature_row_count = len(master_audit)
    matched_row_count = int(master_matched.sum())
    all_feature_tickers = set(master_audit["ticker"].astype(str))
    matched_feature_tickers = set(master_audit.loc[master_matched, "ticker"].astype(str))
    master_match_stats = {
        "feature_rows": feature_row_count,
        "matched_feature_rows": matched_row_count,
        "unmatched_feature_rows": feature_row_count - matched_row_count,
        "row_coverage": matched_row_count / feature_row_count if feature_row_count else 0.0,
        "unique_feature_tickers": len(all_feature_tickers),
        "matched_unique_tickers": len(matched_feature_tickers),
        "unmatched_unique_tickers": len(all_feature_tickers - matched_feature_tickers),
    }
    filter_input_stats = _historical_filter_input_audit(features, contract, cutoff)
    returns = _prepare_returns(_read_table(returns_path))
    returns = returns.loc[returns["month_end"].le(cutoff)].copy()
    benchmark, benchmark_load_status = _prepare_benchmark(benchmark_path)
    benchmark = benchmark.loc[benchmark["month_end"].le(cutoff)].copy()

    selections = _build_selections(
        features,
        contract,
        cutoff,
        resolved_top_k,
        float(legacy_holding_bonus),
        int(legacy_keep_current_top_n),
        float(target_cash_equivalent_weight),
    )
    periods, monthly = _build_performance(
        selections,
        returns,
        benchmark,
        cutoff,
        float(commission_rate),
        float(sell_tax_rate),
        float(cash_equivalent_return),
    )
    sector_exposure = _build_sector_exposure(selections)
    first_rebalance = selections["rebalance_month"].min()
    cutoff_month_end = cutoff.to_period("M").to_timestamp("M")
    last_complete_month = (
        cutoff_month_end if cutoff >= cutoff_month_end else cutoff_month_end - pd.offsets.MonthEnd(1)
    )
    evaluation_months = list(
        pd.date_range(
            pd.Timestamp(first_rebalance) + pd.offsets.MonthEnd(1),
            last_complete_month,
            freq="ME",
        )
    )
    cash_history_coverage = _cash_history_coverage(returns, evaluation_months)
    summary = pd.DataFrame(
        [
            _summary_metrics(
                variant,
                selections,
                periods,
                monthly,
                sector_exposure,
                benchmark,
                benchmark_load_status,
                cash_history_coverage,
                float(legacy_holding_bonus),
                int(legacy_keep_current_top_n),
                cutoff,
                str(pit_master_universe_status),
                master_match_stats,
                filter_input_stats,
            )
            for variant in [LEGACY_VARIANT, FRESH_START_VARIANT]
        ]
    )

    # Stable plain-language aliases keep downstream QA independent of capitalization
    # while the explicit fields above document the precise calculation.
    summary["quarterly_hit_rate"] = summary["positive_quarter_hit_rate"]
    summary["turnover"] = summary["average_turnover"]
    summary["gross_traded_ratio"] = summary["average_gross_traded_ratio"]
    summary["total_trading_costs"] = summary["total_trading_cost_ratio"]
    summary["overlap"] = summary["average_overlap_ratio"]
    summary["concentration"] = summary["average_total_concentration_hhi"]
    summary["drawdown_recovery"] = summary["drawdown_recovery_months"]
    summary["krx300_excess_return"] = summary["krx300_excess_total_return"]

    periods["total_trading_cost_ratio"] = periods["total_cost_ratio"]
    periods["full_reset_applied"] = periods["variant"].eq(FRESH_START_VARIANT)
    monthly["portfolio_return"] = monthly["net_return"]
    monthly["transaction_cost_ratio"] = monthly["cost_ratio"]

    selection_columns = [
        "variant",
        "rebalance_month",
        "ticker",
        "name",
        "sector",
        "model_rank",
        "selection_rank",
        "score_base",
        "model_score",
        "selection_score",
        "holding_bonus_applied",
        "keep_current_top_n_contract",
        "selection_bucket",
        "kept_from_previous",
        "previously_selected",
        "overlap_with_previous",
        "target_weight",
        "holding_bonus",
        "keep_current_top_n",
    ]
    selections["holding_bonus"] = selections["holding_bonus_applied"]
    selections["keep_current_top_n"] = selections["keep_current_top_n_contract"]
    for column in selection_columns:
        if column not in selections.columns:
            selections[column] = pd.NA
    selection_output = selections[selection_columns].sort_values(
        ["variant", "rebalance_month", "selection_rank"]
    )

    artifacts = {
        "comparison_summary": summary,
        "periods": periods,
        "monthly": monthly,
        "selection": selection_output,
        "sector_exposure": sector_exposure,
    }
    for key, frame in artifacts.items():
        frame.to_csv(paths[key], index=False, encoding="utf-8-sig")

    return {**artifacts, "paths": paths}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features-path", required=True)
    parser.add_argument("--returns-path", required=True)
    parser.add_argument("--strategy-config-path", required=True)
    parser.add_argument("--master-path", required=True)
    parser.add_argument("--benchmark-path", default="")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--cutoff-end", required=True)
    parser.add_argument("--strategy-name", default=LEGACY_VARIANT)
    parser.add_argument("--top-k", type=int, default=None)
    parser.add_argument("--legacy-holding-bonus", type=float, default=0.5)
    parser.add_argument("--legacy-keep-current-top-n", type=int, default=3)
    parser.add_argument("--commission-rate", type=float, default=DEFAULT_COMMISSION_RATE)
    parser.add_argument("--sell-tax-rate", type=float, default=DEFAULT_SELL_TAX_RATE)
    parser.add_argument(
        "--pit-master-universe-status",
        default=DEFAULT_PIT_MASTER_UNIVERSE_STATUS,
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    result = run_fresh_start_comparison(
        args.features_path,
        args.returns_path,
        args.strategy_config_path,
        args.master_path,
        args.benchmark_path or None,
        args.output_dir,
        args.cutoff_end,
        strategy_name=args.strategy_name,
        top_k=args.top_k,
        legacy_holding_bonus=args.legacy_holding_bonus,
        legacy_keep_current_top_n=args.legacy_keep_current_top_n,
        commission_rate=args.commission_rate,
        sell_tax_rate=args.sell_tax_rate,
        pit_master_universe_status=args.pit_master_universe_status,
    )
    print(json.dumps({key: str(value) for key, value in result["paths"].items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
