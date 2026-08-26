from __future__ import annotations

"""Pure contracts for the ADVISOR_FULL_RESET_V2 correction run.

The functions in this module deliberately do not accept current-account
membership when constructing model scores or ranks.  Account holdings enter
only the account-as-of valuation and current-versus-target comparison layers.
"""

from dataclasses import dataclass
from math import floor
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from scripts.common.security_id import normalize_security_id


SCORE_TOLERANCE = 1e-12
CASH_RETURN_CONTRACT = "ZERO_NOMINAL_RETURN_CONSERVATIVE"


def _ticker(value: object) -> str:
    return str(normalize_security_id(value))


def _number(value: object, *, default: float | None = None) -> float | None:
    result = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(result):
        return default
    return float(result)


def build_account_valuation_snapshot(
    holdings: pd.DataFrame,
    account_snapshot: Mapping[str, Any],
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Value the current account only from the broker account-as-of snapshot."""

    required = {"ticker", "name", "asset_class", "broker_market_value"}
    missing = required.difference(holdings.columns)
    if missing:
        raise ValueError(f"broker holdings are missing columns: {sorted(missing)}")
    account_asof = str(account_snapshot.get("account_asof") or "").strip()
    gross_nav = _number(account_snapshot.get("gross_account_nav"))
    signed_cash = _number(account_snapshot.get("cash_balance_signed"))
    if not account_asof or gross_nav is None or gross_nav <= 0 or signed_cash is None:
        raise ValueError("verified account_asof, gross_account_nav and signed cash are required")

    rows = holdings.copy()
    rows["ticker"] = rows["ticker"].map(_ticker)
    rows["account_valuation_asof"] = account_asof
    rows["current_qty"] = pd.to_numeric(
        rows.get("current_qty", rows.get("shares")), errors="coerce"
    )
    rows["account_asof_price"] = pd.to_numeric(
        rows.get("broker_market_price", rows.get("broker_price")), errors="coerce"
    )
    rows["account_asof_value"] = pd.to_numeric(
        rows["broker_market_value"], errors="coerce"
    )
    if rows["account_asof_value"].isna().any():
        raise ValueError("every broker security row requires broker_market_value")
    rows["valuation_source"] = "BROKER_EXACT_SNAPSHOT"
    rows["broker_market_price"] = rows["account_asof_price"]
    rows["broker_market_value"] = rows["account_asof_value"]
    rows["strategy_bucket"] = rows["asset_class"].map(
        lambda value: "CASH_EQUIVALENT_BUCKET"
        if str(value) == "CASH_EQUIVALENT"
        else ("EQUITY_BUCKET" if str(value) == "EQUITY" else str(value))
    )
    rows["implementation_vehicle"] = rows["ticker"].where(
        rows["asset_class"].astype(str).eq("CASH_EQUIVALENT"), ""
    )

    cash_payload = {
        "ticker": "ACCOUNT_CASH",
        "name": "현금",
        "asset_class": "CASH",
        "account_valuation_asof": account_asof,
        "current_qty": pd.NA,
        "account_asof_price": pd.NA,
        "account_asof_value": signed_cash,
        "valuation_source": "BROKER_EXACT_SNAPSHOT",
        "broker_market_price": pd.NA,
        "broker_market_value": signed_cash,
        "strategy_bucket": "CASH_EQUIVALENT_BUCKET",
        "implementation_vehicle": "",
    }
    for column in cash_payload:
        if column not in rows.columns:
            rows[column] = pd.NA
    rows.loc[len(rows)] = pd.Series(cash_payload)
    rows["current_weight"] = rows["account_asof_value"] / gross_nav
    component_total = float(rows["account_asof_value"].sum())
    identity_error = component_total - gross_nav
    identity_pass = abs(identity_error) <= 0.5
    if not identity_pass:
        raise ValueError(
            f"broker NAV identity failed: components={component_total}, gross_nav={gross_nav}"
        )
    summary = {
        "account_valuation_contract": "BROKER_ACCOUNT_ASOF_EXACT_SNAPSHOT_V2",
        "account_valuation_asof": account_asof,
        "gross_account_nav": gross_nav,
        "component_value_total": component_total,
        "nav_identity_error": identity_error,
        "nav_identity_status": "PASS",
        "current_value_source": "broker_market_value",
        "current_weight_denominator": "broker_gross_account_nav",
        "model_information_price_used_for_current_account": False,
    }
    columns = [
        "ticker",
        "name",
        "asset_class",
        "market",
        "strategy_bucket",
        "implementation_vehicle",
        "account_valuation_asof",
        "current_qty",
        "account_asof_price",
        "account_asof_value",
        "broker_market_price",
        "broker_market_value",
        "current_weight",
        "valuation_source",
    ]
    return rows.loc[:, columns], summary


def calculate_account_asof_liquidation(
    account_valuation: pd.DataFrame,
    *,
    commission_rate: float,
    sell_tax_rate_by_market: Mapping[str, float],
    default_sell_tax_rate: float,
    liability_value: float = 0.0,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Apply hypothetical sale costs to every broker-valued EQUITY position."""

    if commission_rate < 0 or default_sell_tax_rate < 0:
        raise ValueError("cost rates must be non-negative")
    frame = account_valuation.copy()
    required = {"ticker", "asset_class", "account_asof_value", "account_valuation_asof"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"account valuation is missing columns: {sorted(missing)}")
    frame["hypothetical_gross_sell_value"] = 0.0
    frame["hypothetical_sell_commission"] = 0.0
    frame["hypothetical_sell_tax"] = 0.0
    frame["hypothetical_net_liquidation_proceeds"] = frame["account_asof_value"]
    equity = frame["asset_class"].astype(str).eq("EQUITY")
    frame.loc[equity, "hypothetical_gross_sell_value"] = frame.loc[
        equity, "account_asof_value"
    ]
    markets = frame.get("market", pd.Series(index=frame.index, dtype="object"))
    rates = markets.map(
        lambda market: float(sell_tax_rate_by_market.get(str(market), default_sell_tax_rate))
    )
    frame["market_specific_sell_tax_rate"] = rates.where(equity, 0.0)
    frame.loc[equity, "hypothetical_sell_commission"] = (
        frame.loc[equity, "account_asof_value"] * commission_rate
    )
    frame.loc[equity, "hypothetical_sell_tax"] = (
        frame.loc[equity, "account_asof_value"] * frame.loc[equity, "market_specific_sell_tax_rate"]
    )
    frame.loc[equity, "hypothetical_net_liquidation_proceeds"] = (
        frame.loc[equity, "account_asof_value"]
        - frame.loc[equity, "hypothetical_sell_commission"]
        - frame.loc[equity, "hypothetical_sell_tax"]
    )
    frame["full_reset_liquidation_applied"] = equity
    frame["hypothetical_net_proceeds"] = frame[
        "hypothetical_net_liquidation_proceeds"
    ]

    equity_value = float(frame.loc[equity, "account_asof_value"].sum())
    cash_equivalent_value = float(
        frame.loc[frame["asset_class"].astype(str).eq("CASH_EQUIVALENT"), "account_asof_value"].sum()
    )
    cash_value = float(
        frame.loc[frame["asset_class"].astype(str).eq("CASH"), "account_asof_value"].sum()
    )
    commission = float(frame["hypothetical_sell_commission"].sum())
    tax = float(frame["hypothetical_sell_tax"].sum())
    net_equity = equity_value - commission - tax
    capital = net_equity + cash_equivalent_value + cash_value - float(liability_value)
    if capital <= 0:
        raise ValueError("advisory_rebalance_capital must be positive")
    summary = {
        "contract": "ACCOUNT_ASOF_HYPOTHETICAL_FULL_LIQUIDATION_V2",
        "account_valuation_asof": str(frame["account_valuation_asof"].iloc[0]),
        "equity_market_value": equity_value,
        "cash_equivalent_value": cash_equivalent_value,
        "cash_value": cash_value,
        "liability_value": float(liability_value),
        "hypothetical_sell_commission": commission,
        "hypothetical_sell_tax": tax,
        "hypothetical_equity_net_proceeds": net_equity,
        "advisory_rebalance_capital": capital,
        "equity_positions_liquidated": int(equity.sum()),
        "all_equities_liquidated": bool(
            frame.loc[equity, "full_reset_liquidation_applied"].all()
        ),
        "broker_market_value_used": True,
        "cash_equivalent_equity_tax_applied": False,
        "synthetic_cash_used": False,
    }
    return frame, summary


def build_common_reference_price_snapshot(
    prices_path: Path,
    tickers: Sequence[str],
    *,
    requested_asof: str,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Choose the latest official date shared by every selected equity."""

    selected = sorted({_ticker(item) for item in tickers})
    prices = pd.read_parquet(prices_path, columns=["date", "Close", "ticker"])
    prices["ticker"] = prices["ticker"].map(_ticker)
    prices["date"] = pd.to_datetime(prices["date"], errors="coerce")
    prices["reference_price"] = pd.to_numeric(prices["Close"], errors="coerce")
    cutoff = pd.Timestamp(requested_asof)
    eligible = prices.loc[
        prices["ticker"].isin(selected)
        & prices["date"].le(cutoff)
        & prices["reference_price"].gt(0)
    ].copy()
    per_date_count = eligible.groupby("date")["ticker"].nunique()
    common_dates = per_date_count.index[per_date_count.eq(len(selected))]
    common_asof = max(common_dates) if len(common_dates) else pd.NaT
    if pd.isna(common_asof):
        result = pd.DataFrame(
            {
                "ticker": selected,
                "reference_price_asof": pd.NA,
                "reference_price": pd.NA,
                "reference_price_source": "OFFICIAL_DAILY_CLOSE",
                "reference_price_status": "NA_NO_COMMON_OFFICIAL_DATE",
            }
        )
    else:
        day = eligible.loc[eligible["date"].eq(common_asof)].sort_values(
            ["ticker", "date"]
        ).drop_duplicates("ticker", keep="last")
        result = pd.DataFrame({"ticker": selected}).merge(
            day[["ticker", "reference_price"]], on="ticker", how="left", validate="one_to_one"
        )
        result["reference_price_asof"] = common_asof.date().isoformat()
        result["reference_price_source"] = "OFFICIAL_DAILY_CLOSE"
        result["reference_price_status"] = result["reference_price"].notna().map(
            {True: "AVAILABLE_COMMON_CUTOFF", False: "NA_AT_COMMON_CUTOFF"}
        )
        result = result[
            [
                "ticker",
                "reference_price_asof",
                "reference_price",
                "reference_price_source",
                "reference_price_status",
            ]
        ]
    summary = {
        "requested_reference_price_asof": requested_asof,
        "reference_price_asof": (
            common_asof.date().isoformat() if not pd.isna(common_asof) else None
        ),
        "reference_price_contract": "LATEST_COMMON_OFFICIAL_CLOSE_ON_OR_BEFORE_REQUESTED_DATE",
        "selected_ticker_count": len(selected),
        "available_at_common_cutoff_count": int(result["reference_price"].notna().sum()),
        "all_selected_prices_same_cutoff": bool(
            len(selected) > 0 and result["reference_price"].notna().all()
        ),
    }
    return result, summary


def build_v2_target_portfolio(
    top_k: pd.DataFrame,
    reference_prices: pd.DataFrame,
    *,
    advisory_rebalance_capital: float,
    target_cash_equivalent_weight: float,
    buy_commission_rate: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if advisory_rebalance_capital <= 0:
        raise ValueError("advisory_rebalance_capital must be positive")
    count = len(top_k)
    if count <= 0:
        raise ValueError("Top-K must not be empty")
    if not 0 < target_cash_equivalent_weight < 1:
        raise ValueError("target cash-equivalent weight must be between zero and one")
    equity_weight_each = (1.0 - target_cash_equivalent_weight) / count
    target = top_k.copy()
    target["ticker"] = target["ticker"].map(_ticker)
    prices = reference_prices.copy()
    prices["ticker"] = prices["ticker"].map(_ticker)
    target = target.merge(prices, on="ticker", how="left", validate="one_to_one")
    target["asset_class"] = "EQUITY"
    target["target_weight"] = equity_weight_each
    target["target_value"] = advisory_rebalance_capital * equity_weight_each
    per_name = "model_score" if "model_score" in target else "score"
    target["model_score"] = pd.to_numeric(target[per_name], errors="coerce")
    if "model_rank" not in target:
        target["model_rank"] = range(1, count + 1)
    target["reference_target_qty"] = target.apply(
        lambda row: (
            floor(
                float(row["target_value"])
                / ((1.0 + buy_commission_rate) * float(row["reference_price"]))
            )
            if pd.notna(row.get("reference_price")) and float(row["reference_price"]) > 0
            else pd.NA
        ),
        axis=1,
    )
    target["illustrative_target_value"] = target.apply(
        lambda row: (
            float(row["reference_target_qty"]) * float(row["reference_price"])
            if pd.notna(row["reference_target_qty"]) and pd.notna(row["reference_price"])
            else pd.NA
        ),
        axis=1,
    )
    target["estimated_buy_commission"] = target["illustrative_target_value"].map(
        lambda value: float(value) * buy_commission_rate if pd.notna(value) else pd.NA
    )
    target["equity_weighting"] = "EQUAL_WEIGHT"

    known_equity_spend = float(
        (
            pd.to_numeric(target["illustrative_target_value"], errors="coerce").fillna(0)
            + pd.to_numeric(target["estimated_buy_commission"], errors="coerce").fillna(0)
        ).sum()
    )
    resulting_cash = advisory_rebalance_capital - known_equity_spend
    cash_row = {
        "ticker": "CASH_EQUIVALENT_BUCKET",
        "name": "현금성 자산",
        "asset_class": "CASH_EQUIVALENT_BUCKET",
        "model_selected": False,
        "model_rank": pd.NA,
        "model_score": pd.NA,
        "target_weight": target_cash_equivalent_weight,
        "target_value": advisory_rebalance_capital * target_cash_equivalent_weight,
        "reference_price_asof": target["reference_price_asof"].dropna().iloc[0]
        if target["reference_price_asof"].notna().any()
        else pd.NA,
        "reference_price": pd.NA,
        "reference_price_source": "STRATEGIC_BUCKET_NOT_A_SECURITY",
        "reference_price_status": "NOT_APPLICABLE",
        "reference_target_qty": pd.NA,
        "illustrative_target_value": resulting_cash,
        "estimated_buy_commission": 0.0,
        "equity_weighting": "EQUAL_WEIGHT",
        "cash_return_contract": CASH_RETURN_CONTRACT,
        "implementation_vehicle": "437350",
    }
    target["cash_return_contract"] = pd.NA
    target["implementation_vehicle"] = pd.NA
    for column in cash_row:
        if column not in target.columns:
            target[column] = pd.NA
    # A mixed security/bucket artifact intentionally carries NA in columns
    # that do not apply to one side.  Object dtype keeps that schema stable
    # across pandas versions without its all-NA concat inference.
    target_object = target.astype(object)
    cash_object = pd.DataFrame([cash_row], columns=target.columns).astype(object)
    portfolio = pd.concat(
        [target_object, cash_object], ignore_index=True, sort=False
    )
    weight_total = float(portfolio["target_weight"].sum())
    cash_floor = resulting_cash / advisory_rebalance_capital
    summary = {
        "top_k": count,
        "target_equity_weight_each": equity_weight_each,
        "target_equity_weight_total": 1.0 - target_cash_equivalent_weight,
        "target_cash_equivalent_weight": target_cash_equivalent_weight,
        "target_weight_total": weight_total,
        "buy_commission_rate": buy_commission_rate,
        "known_reference_price_count": int(target["reference_price"].notna().sum()),
        "missing_reference_price_count": int(target["reference_price"].isna().sum()),
        "illustrative_known_equity_spend_including_commission": known_equity_spend,
        "resulting_cash_equivalent_value": resulting_cash,
        "resulting_cash_equivalent_weight": cash_floor,
        "cash_floor_satisfied": bool(cash_floor + 1e-12 >= target_cash_equivalent_weight),
        "cash_return_contract": CASH_RETURN_CONTRACT,
        "implementation_vehicle": "437350",
    }
    if abs(weight_total - 1.0) > 1e-12 or not summary["cash_floor_satisfied"]:
        raise ValueError("target portfolio weight or cash floor contract failed")
    return portfolio, summary


def build_top_k_boundary_watchlist(
    scores: pd.DataFrame,
    *,
    top_k: int,
    fragile_threshold: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    required = {"ticker", "name", "model_rank", "model_score", "quality_penalty_total"}
    missing = required.difference(scores.columns)
    if missing:
        raise ValueError(f"scores are missing boundary fields: {sorted(missing)}")
    ranked = scores.loc[pd.to_numeric(scores["model_rank"], errors="coerce").notna()].copy()
    ranked["rank"] = pd.to_numeric(ranked["model_rank"], errors="raise").astype(int)
    ranked = ranked.sort_values(["rank", "ticker"])
    window = ranked.loc[ranked["rank"].between(max(1, top_k - 2), top_k + 5)].copy()
    kth = ranked.loc[ranked["rank"].eq(top_k)]
    next_row = ranked.loc[ranked["rank"].eq(top_k + 1)]
    if len(kth) != 1 or len(next_row) != 1:
        raise ValueError("rank K and K+1 are required for boundary QA")
    kth_score = float(kth["model_score"].iloc[0])
    next_score = float(next_row["model_score"].iloc[0])
    window["score_gap_vs_k"] = window["model_score"] - kth_score
    window["score_gap_vs_previous"] = window["model_score"].shift(1) - window["model_score"]
    window["quality_penalty"] = window["quality_penalty_total"]
    window["selection_status"] = window["rank"].le(top_k).map(
        {True: "SELECTED", False: "WATCHLIST_NOT_SELECTED"}
    )
    gap = kth_score - next_score
    status = "FRAGILE" if gap < fragile_threshold else "STABLE"
    result = window[
        [
            "rank",
            "ticker",
            "name",
            "model_score",
            "score_gap_vs_k",
            "score_gap_vs_previous",
            "quality_penalty",
            "selection_status",
        ]
    ].reset_index(drop=True)
    summary = {
        "top_k": top_k,
        "fragile_threshold": fragile_threshold,
        "rank_k_score": kth_score,
        "rank_k_plus_1_score": next_score,
        "rank_k_minus_rank_k_plus_1_gap": gap,
        "selection_boundary_status": status,
        "automatic_replacement_applied": False,
        "top_k_expansion_applied": False,
        "watchlist_rank_start": max(1, top_k - 2),
        "watchlist_rank_end": top_k + 5,
    }
    return result, summary


def build_current_vs_target_v2(
    account_valuation: pd.DataFrame,
    target_portfolio: pd.DataFrame,
) -> pd.DataFrame:
    """Build a comparison-only view without feeding account state to selection."""

    current = account_valuation.copy()
    target = target_portfolio.copy()
    current["ticker"] = current["ticker"].astype(str).map(
        lambda value: _ticker(value) if value not in {"ACCOUNT_CASH"} else value
    )
    target["ticker"] = target["ticker"].astype(str).map(
        lambda value: _ticker(value)
        if value != "CASH_EQUIVALENT_BUCKET"
        else value
    )
    current_equity = current.loc[current["asset_class"].astype(str).eq("EQUITY")].copy()
    current_equity = current_equity.rename(
        columns={
            "account_asof_value": "current_value",
            "account_asof_price": "current_price",
        }
    )
    current_equity = current_equity[
        [
            "ticker",
            "name",
            "current_qty",
            "current_price",
            "current_value",
            "current_weight",
        ]
    ]
    target_equity = target.loc[target["asset_class"].astype(str).eq("EQUITY")].copy()
    target_equity = target_equity[
        [
            "ticker",
            "name",
            "model_rank",
            "model_score",
            "target_weight",
            "target_value",
            "illustrative_target_value",
            "reference_target_qty",
        ]
    ]
    comparison = current_equity.merge(
        target_equity,
        on="ticker",
        how="outer",
        suffixes=("_current", "_target"),
        validate="one_to_one",
    )
    comparison["name"] = comparison.get("name_target").combine_first(
        comparison.get("name_current")
    )
    comparison["asset_class"] = "EQUITY"
    comparison["previously_held"] = comparison["current_value"].fillna(0).gt(0)
    comparison["model_selected"] = comparison["target_weight"].fillna(0).gt(0)
    comparison["current_qty"] = pd.to_numeric(
        comparison["current_qty"], errors="coerce"
    ).fillna(0)
    comparison["current_value"] = pd.to_numeric(
        comparison["current_value"], errors="coerce"
    ).fillna(0.0)
    comparison["current_weight"] = pd.to_numeric(
        comparison["current_weight"], errors="coerce"
    ).fillna(0.0)
    for column in ("target_weight", "target_value", "illustrative_target_value"):
        comparison[column] = pd.to_numeric(
            comparison[column], errors="coerce"
        ).fillna(0.0)

    comparison["transition_status"] = "NOT_HELD_NOT_SELECTED"
    comparison.loc[
        comparison["previously_held"] & comparison["model_selected"],
        "transition_status",
    ] = "RESELECTED"
    comparison.loc[
        ~comparison["previously_held"] & comparison["model_selected"],
        "transition_status",
    ] = "NEW_SELECTION"
    comparison.loc[
        comparison["previously_held"] & ~comparison["model_selected"],
        "transition_status",
    ] = "DROPPED"

    cash_like = current["asset_class"].astype(str).isin(
        ["CASH_EQUIVALENT", "CASH"]
    )
    current_cash_value = float(current.loc[cash_like, "account_asof_value"].sum())
    current_cash_weight = float(current.loc[cash_like, "current_weight"].sum())
    vehicle = current.loc[
        current["asset_class"].astype(str).eq("CASH_EQUIVALENT"), "ticker"
    ].astype(str)
    cash_target = target.loc[target["ticker"].eq("CASH_EQUIVALENT_BUCKET")]
    if len(cash_target) != 1:
        raise ValueError("target portfolio requires one CASH_EQUIVALENT_BUCKET row")
    cash_payload = cash_target.iloc[0]
    cash_row = {
        "ticker": "CASH_EQUIVALENT_BUCKET",
        "name": "현금성 자산",
        "asset_class": "CASH_EQUIVALENT_BUCKET",
        "model_rank": pd.NA,
        "model_score": pd.NA,
        "target_weight": cash_payload["target_weight"],
        "target_value": cash_payload["target_value"],
        "illustrative_target_value": cash_payload["illustrative_target_value"],
        "reference_target_qty": pd.NA,
        "previously_held": bool(current_cash_value != 0),
        "model_selected": False,
        "current_qty": pd.NA,
        "current_value": current_cash_value,
        "current_weight": current_cash_weight,
        "transition_status": "CASH_EQUIVALENT",
        "implementation_vehicle": "|".join(vehicle.tolist()),
    }
    for column in cash_row:
        if column not in comparison.columns:
            comparison[column] = pd.NA
    comparison = pd.concat(
        [
            comparison.astype(object),
            pd.DataFrame([cash_row], columns=comparison.columns).astype(object),
        ],
        ignore_index=True,
        sort=False,
    )
    priority = {
        "RESELECTED": 0,
        "NEW_SELECTION": 1,
        "DROPPED": 2,
        "CASH_EQUIVALENT": 3,
        "NOT_HELD_NOT_SELECTED": 4,
    }
    comparison["_priority"] = comparison["transition_status"].map(priority)
    comparison = comparison.sort_values(
        ["_priority", "model_rank", "ticker"], na_position="last", kind="stable"
    ).drop(columns=["_priority", "name_current", "name_target"], errors="ignore")
    output_columns = [
        "ticker",
        "name",
        "asset_class",
        "model_selected",
        "model_rank",
        "model_score",
        "target_weight",
        "target_value",
        "illustrative_target_value",
        "reference_target_qty",
        "previously_held",
        "current_qty",
        "current_value",
        "current_weight",
        "transition_status",
        "implementation_vehicle",
    ]
    return comparison.loc[:, output_columns].reset_index(drop=True)


def build_sector_exposure(
    positions: pd.DataFrame,
    industry_mapping: pd.DataFrame,
    *,
    scope: str,
    value_column: str,
    weight_column: str,
    include_cash_bucket: bool = False,
) -> pd.DataFrame:
    """Aggregate exact-ticker advisor risk sectors for account or target."""

    frame = positions.copy()
    frame["ticker"] = frame["ticker"].astype(str)
    mapping = industry_mapping.copy()
    mapping["ticker"] = mapping["ticker"].astype(str)
    if mapping["ticker"].duplicated().any():
        raise ValueError("industry mapping must be one-to-one by ticker")
    security = frame.loc[
        frame["ticker"].ne("ACCOUNT_CASH")
        & (include_cash_bucket | frame["ticker"].ne("CASH_EQUIVALENT_BUCKET"))
    ].copy()
    if "asset_class" in security.columns and not include_cash_bucket:
        security = security.loc[security["asset_class"].astype(str).eq("EQUITY")]
    security = security.merge(
        mapping[["ticker", "advisor_sector", "advisor_sector_source"]],
        on="ticker",
        how="left",
        validate="many_to_one",
    )
    if security["advisor_sector"].isna().any():
        missing = security.loc[security["advisor_sector"].isna(), "ticker"].tolist()
        raise ValueError(f"advisor sector missing for positions: {missing}")
    security[value_column] = pd.to_numeric(security[value_column], errors="coerce").fillna(0.0)
    security[weight_column] = pd.to_numeric(security[weight_column], errors="coerce").fillna(0.0)
    result = (
        security.groupby(
            ["advisor_sector", "advisor_sector_source"], dropna=False, as_index=False
        )
        .agg(
            position_count=("ticker", "nunique"),
            sector_value=(value_column, "sum"),
            sector_weight=(weight_column, "sum"),
        )
        .sort_values(["sector_weight", "advisor_sector"], ascending=[False, True])
        .reset_index(drop=True)
    )
    result.insert(0, "portfolio_scope", scope)
    return result


def build_blocker_taxonomy(
    *,
    score_parity_pass: bool,
    advisor_contract_checks: Mapping[str, bool],
    activity_ledger_available: bool,
) -> dict[str, Any]:
    model = []
    if not score_parity_pass:
        model.append("SCORE_PARITY_FAILURE")
    model.extend(
        [
            "HISTORICAL_PIT_UNIVERSE_UNAVAILABLE",
            "HISTORICAL_MCAP_TRADED_VALUE_UNAVAILABLE",
            "FULL_CALENDAR_RETURN_COVERAGE_INCOMPLETE",
            "KRX300_ALIGNED_HISTORY_INCOMPLETE",
            "HISTORICAL_SELL_TAX_SCHEDULE_UNAVAILABLE",
            "FRESH_START_BACKTEST_NOT_VALIDATED",
        ]
    )
    advisor = [
        name for name, passed in advisor_contract_checks.items() if not bool(passed)
    ]
    warnings = [
        "PRIOR_MODEL_ARTIFACT_PROVISIONAL",
        "PRIOR_MODEL_BENCHMARK_PERIOD_MISMATCH",
        "ACTUAL_ACCOUNT_RETURN_NOT_CASHFLOW_ADJUSTED",
    ]
    if not activity_ledger_available:
        warnings.insert(0, "ACTIVITY_LEDGER_UNAVAILABLE")
    return {
        "MODEL_PROMOTION_BLOCKERS": model,
        "ADVISOR_REPORT_BLOCKERS": advisor,
        "PERFORMANCE_CERTIFICATION_WARNINGS": warnings,
        "ACCOUNT_VALUATION_WARNINGS": [
            "437350_CUTOFF_PRICE_NON_KRX_PRIMARY_SOURCE"
        ],
        "advisor_report_generation_allowed": not advisor,
        "performance_warnings_block_advisor_report": False,
        "production_promotion_allowed": not model and not advisor,
    }
