#!/usr/bin/env python
from __future__ import annotations

import argparse
import calendar
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import numpy as np
import pandas as pd
from scripts.common.security_id import normalize_security_id_series
from scripts.live.performance_cost_basis import (
    CANONICAL_CONTRIBUTION_COLUMNS,
    EXECUTION_WINDOW_ATTRIBUTION_COLUMNS,
    STATIC_METHOD,
    TRANSACTION_METHOD,
    calculate_execution_window_attribution,
    calculate_transaction_aware_performance,
    load_certified_execution_fill_ledger,
    load_user_confirmed_broker_statement_execution_evidence,
    materialize_static_positions,
    resolve_performance_cost_basis,
    sha256_file,
    validate_contribution_reconciliation,
    validate_certified_interval_contract,
    validate_static_capital_contract,
)


NAV_DIVIDEND_TREATMENT = "EXCLUDED"
BENCHMARK_ALIGNMENT_POLICY = "EXACT_PORTFOLIO_TRADING_DATES_NO_FILL"
OFFICIAL_INDEX_REQUIRED_METADATA = {
    "benchmark_name": "KRX 300",
    "asset_type": "INDEX",
    "return_type": "PRICE",
    "source": "KRX_OFFICIAL_INDEX",
}
BENCHMARK_METADATA_COLUMNS = [
    "benchmark_name",
    "benchmark_identifier",
    "asset_type",
    "return_type",
    "source",
    "index_master_market",
]


@dataclass(frozen=True)
class MonthlyPerformanceResult:
    """Monthly static-portfolio performance on exact common official dates."""

    monthly_performance: pd.DataFrame
    security_returns: pd.DataFrame
    qa: dict[str, Any]


def _normalise_monthly_performance_inputs(
    starting_positions: pd.DataFrame,
    official_equity_prices: pd.DataFrame,
    official_krx300: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    positions = starting_positions.copy()
    prices = official_equity_prices.copy()
    benchmark = official_krx300.copy()
    required_positions = {"ticker", "shares", "position_value"}
    required_prices = {"date", "ticker", "close"}
    required_benchmark = {"date", "krx300_price_index"}
    if missing := required_positions.difference(positions.columns):
        raise ValueError(f"starting_positions missing columns: {sorted(missing)}")
    if missing := required_prices.difference(prices.columns):
        raise ValueError(f"official_equity_prices missing columns: {sorted(missing)}")
    if missing := required_benchmark.difference(benchmark.columns):
        raise ValueError(f"official_krx300 missing columns: {sorted(missing)}")

    positions["ticker"] = normalize_ticker(positions["ticker"])
    positions["shares"] = pd.to_numeric(positions["shares"], errors="raise")
    positions["position_value"] = pd.to_numeric(positions["position_value"], errors="raise")
    prices["ticker"] = normalize_ticker(prices["ticker"])
    prices["date"] = pd.to_datetime(prices["date"], errors="raise").dt.normalize()
    prices["close"] = pd.to_numeric(prices["close"], errors="raise")
    benchmark["date"] = pd.to_datetime(benchmark["date"], errors="raise").dt.normalize()
    benchmark["krx300_price_index"] = pd.to_numeric(
        benchmark["krx300_price_index"], errors="raise"
    )
    if positions.empty or positions["ticker"].duplicated().any():
        raise ValueError("starting_positions must contain one row per ticker")
    if prices.duplicated(["date", "ticker"]).any():
        raise ValueError("official_equity_prices contains duplicate ticker/date rows")
    if benchmark["date"].duplicated().any():
        raise ValueError("official_krx300 contains duplicate dates")
    if positions["shares"].le(0).any() or positions["position_value"].le(0).any():
        raise ValueError("starting positions must have positive shares and position_value")
    if prices["close"].le(0).any() or benchmark["krx300_price_index"].le(0).any():
        raise ValueError("official prices and index levels must be positive")
    return positions, prices, benchmark


def _assert_static_quantity_contract(
    positions: pd.DataFrame,
    activity_ledger: pd.DataFrame | None,
    *,
    require_quantity_parity: bool,
) -> None:
    if not require_quantity_parity:
        return
    end_quantity_col = next(
        (column for column in ("end_quantity", "current_qty", "ending_shares") if column in positions.columns),
        None,
    )
    if end_quantity_col is not None:
        ending = pd.to_numeric(positions[end_quantity_col], errors="raise")
        if not ending.eq(positions["shares"]).all() and activity_ledger is None:
            raise RuntimeError("MONTHLY_PERFORMANCE_BLOCKED_ACTIVITY_LEDGER_REQUIRED")
    if activity_ledger is None or activity_ledger.empty:
        return
    ledger = activity_ledger.copy()
    quantity_column = next(
        (
            column
            for column in ("signed_quantity", "quantity_delta", "trade_quantity", "shares_delta")
            if column in ledger.columns
        ),
        None,
    )
    if quantity_column is None:
        raise ValueError("activity_ledger lacks a signed quantity column")
    ledger[quantity_column] = pd.to_numeric(ledger[quantity_column], errors="raise")
    if "ticker" not in ledger.columns:
        raise ValueError("activity_ledger lacks ticker")
    ledger["ticker"] = normalize_ticker(ledger["ticker"])
    deltas = ledger.groupby("ticker")[quantity_column].sum()
    if deltas.abs().gt(0).any():
        raise RuntimeError("MONTHLY_PERFORMANCE_REQUIRES_TRANSACTION_AWARE_LEDGER")


def build_monthly_portfolio_performance(
    *,
    starting_positions: pd.DataFrame,
    official_equity_prices: pd.DataFrame,
    official_krx300: pd.DataFrame,
    performance_start: str | pd.Timestamp,
    performance_end: str | pd.Timestamp,
    activity_ledger: pd.DataFrame | None = None,
    require_quantity_parity: bool = True,
) -> MonthlyPerformanceResult:
    """Build exact-date monthly NAV without filling prices or using future values.

    This is the parameterised form of the historical 26Q3 subscriber-report
    calculation.  It intentionally preserves the original column names,
    rounding, KRX300 equivalent-NAV method, and security-return calculation.
    """

    positions, prices, benchmark = _normalise_monthly_performance_inputs(
        starting_positions, official_equity_prices, official_krx300
    )
    start = pd.Timestamp(performance_start).normalize()
    end = pd.Timestamp(performance_end).normalize()
    if start > end:
        raise ValueError("performance_start must be on or before performance_end")
    _assert_static_quantity_contract(
        positions, activity_ledger, require_quantity_parity=require_quantity_parity
    )

    tickers = tuple(positions["ticker"].astype(str))
    prices = prices.loc[
        prices["ticker"].isin(tickers) & prices["date"].between(start, end)
    ].copy()
    benchmark = benchmark.loc[benchmark["date"].between(start, end)].copy()
    complete_equity_dates = (
        prices.groupby("date")["ticker"].nunique().loc[lambda values: values.eq(len(tickers))].index
    )
    common = sorted(set(complete_equity_dates).intersection(set(benchmark["date"])))
    if start not in common or end not in common:
        raise RuntimeError("common-date contract lacks exact performance endpoints")

    common_dates: list[pd.Timestamp] = [start]
    month_cursor = start.to_period("M")
    end_month = end.to_period("M")
    while month_cursor <= end_month:
        candidates = [
            date
            for date in common
            if date.to_period("M") == month_cursor and start <= date <= end
        ]
        if not candidates:
            raise RuntimeError(f"no complete common trading date for {month_cursor}")
        month_end = max(candidates)
        if month_end not in common_dates:
            common_dates.append(month_end)
        month_cursor += 1
    if end not in common_dates:
        common_dates.append(end)
    common_dates = sorted(common_dates)

    start_nav = float(positions["position_value"].sum())
    if start_nav <= 0:
        raise ValueError("starting NAV must be positive")
    quantity = positions.set_index("ticker")["shares"]
    price_wide = prices.pivot(index="date", columns="ticker", values="close")
    index_map = benchmark.set_index("date")["krx300_price_index"]
    index_start = float(index_map.loc[start])
    is_partial_end = end.day < calendar.monthrange(end.year, end.month)[1]
    rows: list[dict[str, Any]] = []
    previous_nav = start_nav
    previous_equivalent = start_nav
    previous_index = index_start
    for position, date in enumerate(common_dates):
        nav = start_nav if position == 0 else float((price_wide.loc[date, list(tickers)] * quantity).sum())
        index_level = float(index_map.loc[date])
        equivalent_nav = start_nav * index_level / index_start
        rows.append(
            {
                "date": date.strftime("%Y-%m-%d") + ("(부분월)" if date == end and is_partial_end else ""),
                "date_iso": date.strftime("%Y-%m-%d"),
                "portfolio_nav": round(nav),
                "portfolio_change_amount": round(nav - previous_nav) if position else 0,
                "portfolio_monthly_change_rate": nav / previous_nav - 1 if position else 0.0,
                "portfolio_cumulative_change_rate": nav / start_nav - 1 if position else 0.0,
                "krx300_price_index": index_level,
                "krx300_equivalent_nav": round(equivalent_nav),
                "krx300_change_amount": round(equivalent_nav - previous_equivalent) if position else 0,
                "krx300_monthly_change_rate": index_level / previous_index - 1 if position else 0.0,
                "krx300_cumulative_change_rate": index_level / index_start - 1 if position else 0.0,
                "cumulative_excess_return": (nav / start_nav - 1) - (index_level / index_start - 1),
            }
        )
        previous_nav = nav
        previous_equivalent = equivalent_nav
        previous_index = index_level
    monthly = pd.DataFrame(rows)

    end_prices = price_wide.loc[end]
    security_rows: list[dict[str, Any]] = []
    for item in positions.itertuples(index=False):
        ticker = str(item.ticker)
        start_value = float(item.position_value)
        end_value = float(item.shares) * float(end_prices.loc[ticker])
        security_rows.append(
            {
                "ticker": ticker,
                "name": getattr(item, "name", ticker),
                "quantity": int(item.shares),
                "start_value": round(start_value),
                "end_value": round(end_value),
                "change_amount": round(end_value - start_value),
                "change_rate": end_value / start_value - 1,
                "start_date": start.strftime("%Y-%m-%d"),
                "end_date": end.strftime("%Y-%m-%d"),
                "end_official_close": float(end_prices.loc[ticker]),
            }
        )
    security = pd.DataFrame(security_rows).sort_values("change_rate", ascending=False).reset_index(drop=True)
    start_error = abs(float(security["start_value"].sum()) - float(monthly.iloc[0]["portfolio_nav"]))
    end_error = abs(float(security["end_value"].sum()) - float(monthly.iloc[-1]["portfolio_nav"]))
    change_error = abs(
        float(security["change_amount"].sum())
        - (float(monthly.iloc[-1]["portfolio_nav"]) - float(monthly.iloc[0]["portfolio_nav"]))
    )
    qa = {
        "start_nav_basis": "ACTUAL_POST_TRADE_POSITION_VALUE",
        "common_dates": [date.strftime("%Y-%m-%d") for date in common_dates],
        "equity_count": len(tickers),
        "start_nav_reconciliation_error_krw": start_error,
        "end_nav_reconciliation_error_krw": end_error,
        "change_reconciliation_error_krw": change_error,
        "reconciliation_status": "PASS" if max(start_error, end_error, change_error) <= 1 else "FAIL",
        "fill_used": False,
        "etf_proxy_used": False,
    }
    if qa["reconciliation_status"] != "PASS":
        raise RuntimeError(f"performance reconciliation failed: {qa}")
    return MonthlyPerformanceResult(monthly, security, qa)


def normalize_ticker(s: pd.Series) -> pd.Series:
    return normalize_security_id_series(s)


def pick_first_existing(cols: list[str], candidates: list[str]) -> str | None:
    for c in candidates:
        if c in cols:
            return c
    return None


def parse_money(x) -> float:
    if pd.isna(x):
        return np.nan
    if isinstance(x, (int, float, np.integer, np.floating)):
        return float(x)
    s = str(x).strip().replace(",", "")
    unit_map = {
        "조원": 1_0000_0000_0000,
        "억원": 100_000_000,
        "백만원": 1_000_000,
        "만원": 10_000,
        "원": 1,
    }
    for unit, mult in unit_map.items():
        if s.endswith(unit):
            num = s[:-len(unit)].strip()
            return float(num) * mult
    return float(s)


def load_execution_plan(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"execution_plan not found: {p}")

    df = pd.read_csv(p)
    if "ticker" not in df.columns:
        raise ValueError("execution_plan must contain 'ticker'")

    df["ticker"] = normalize_ticker(df["ticker"])

    for col in ["shares", "planned_total_qty", "day1_qty", "day2_qty", "day3_qty"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)

    if "price" in df.columns:
        df["price"] = pd.to_numeric(df["price"], errors="coerce")

    for money_col in [
        "current_value", "target_value", "trade_value", "planned_total_value",
        "day1_order_value", "day2_order_value", "day3_order_value",
    ]:
        if money_col in df.columns:
            df[money_col] = df[money_col].map(parse_money)

    if "order_side" not in df.columns:
        trade = pd.to_numeric(df.get("trade_value"), errors="coerce").fillna(0)
        df["order_side"] = np.where(trade > 0, "BUY", np.where(trade < 0, "SELL", "HOLD"))

    return df


def load_holdings_csv(path: str | Path) -> pd.DataFrame:
    """Load an actual holdings snapshot with ticker/name/shares columns.

    This is intentionally separate from execution_plan mode.  A holdings snapshot
    represents what was actually held at the start of the performance window,
    so it avoids look-ahead bias during sandbox/smoke-test report generation.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"holdings_csv not found: {p}")

    df = pd.read_csv(p, dtype={"ticker": str}, encoding="utf-8-sig")
    if "ticker" not in df.columns:
        raise ValueError("holdings_csv must contain 'ticker'")
    if "shares" not in df.columns:
        raise ValueError("holdings_csv must contain 'shares'")

    out = df.copy()
    out["ticker"] = normalize_ticker(out["ticker"])
    out["shares"] = pd.to_numeric(out["shares"], errors="coerce").fillna(0)
    out = out[out["shares"] > 0].copy()

    if "name" not in out.columns:
        out["name"] = pd.NA

    out = (
        out.groupby(["ticker", "name"], dropna=False, as_index=False)["shares"]
        .sum()
        .sort_values("ticker")
        .reset_index(drop=True)
    )
    return out


def build_positions_from_holdings(holdings_df: pd.DataFrame, base_px: pd.DataFrame) -> pd.DataFrame:
    df = holdings_df.merge(base_px, on="ticker", how="left")
    if df["base_price"].isna().any():
        missing = df.loc[df["base_price"].isna(), ["ticker", "name", "shares"]]
        raise ValueError("base_price missing for holdings rows:\n" + missing.to_string(index=False))

    df["post_trade_shares"] = pd.to_numeric(df["shares"], errors="coerce").fillna(0)
    df["base_value"] = df["post_trade_shares"] * df["base_price"]
    df["name_final"] = df.get("name", pd.NA)
    df["action"] = "ACTUAL_HOLDING"
    df["reason"] = "actual holdings snapshot"
    df["order_side"] = "HOLDING"

    keep_cols = [
        "ticker", "name_final", "action", "reason", "order_side",
        "shares", "post_trade_shares", "base_date", "base_price", "base_value",
    ]
    keep_cols = [c for c in keep_cols if c in df.columns]
    return df[keep_cols].copy()


def load_prices_daily(path: str | Path, price_date: Optional[str] = None) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"prices_daily not found: {p}")

    if p.suffix.lower() == ".parquet":
        df = pd.read_parquet(p)
    else:
        df = pd.read_csv(p)

    cols = df.columns.tolist()
    ticker_col = pick_first_existing(cols, ["ticker"])
    date_col = pick_first_existing(cols, ["date", "Date", "dt", "ymd", "trd_date"])
    price_col = pick_first_existing(cols, ["close", "Close", "adj_close", "price"])

    if ticker_col is None or date_col is None or price_col is None:
        raise ValueError(f"Could not detect ticker/date/price columns in prices file: {cols}")

    out = df[[ticker_col, date_col, price_col]].copy()
    out = out.rename(columns={ticker_col: "ticker", date_col: "date", price_col: "price"})
    out["ticker"] = normalize_ticker(out["ticker"])
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    out = out.dropna(subset=["ticker", "date", "price"]).copy()

    if price_date is not None:
        cutoff = pd.to_datetime(price_date)
        out = out[out["date"] <= cutoff].copy()

    return out.sort_values(["ticker", "date"]).reset_index(drop=True)


def latest_price_on_or_before(prices: pd.DataFrame, target_date: str) -> pd.DataFrame:
    cutoff = pd.to_datetime(target_date)
    work = prices[prices["date"] <= cutoff].copy()
    if work.empty:
        raise ValueError(f"No price rows on or before {target_date}")
    work = work.sort_values(["ticker", "date"]).drop_duplicates("ticker", keep="last")
    return work[["ticker", "date", "price"]].rename(columns={"date": "base_date", "price": "base_price"})


def build_post_rebalance_positions(exec_df: pd.DataFrame, base_px: pd.DataFrame) -> pd.DataFrame:
    df = exec_df.merge(base_px, on="ticker", how="left")

    if "base_price" not in df.columns:
        raise ValueError("base_price missing after merge")

    if "shares" not in df.columns:
        df["shares"] = 0

    if "planned_total_qty" not in df.columns:
        qty_cols = [c for c in ["day1_qty", "day2_qty", "day3_qty"] if c in df.columns]
        if qty_cols:
            df["planned_total_qty"] = df[qty_cols].sum(axis=1)
        else:
            df["planned_total_qty"] = 0

    side = df["order_side"].astype(str).str.upper()
    signed_qty = np.where(side.eq("BUY"), df["planned_total_qty"],
                  np.where(side.eq("SELL"), -df["planned_total_qty"], 0))
    df["post_trade_shares"] = pd.to_numeric(df["shares"], errors="coerce").fillna(0) + signed_qty
    df["post_trade_shares"] = df["post_trade_shares"].clip(lower=0)
    df["base_value"] = df["post_trade_shares"] * df["base_price"]

    keep_cols = [
        "ticker", "name_final", "action", "reason", "order_side",
        "shares", "planned_total_qty", "post_trade_shares",
        "base_date", "base_price", "base_value",
        "score", "score_rank", "industry_code", "industry_name", "industry4",
    ]
    keep_cols = [c for c in keep_cols if c in df.columns]
    return df[keep_cols].copy()


def calc_portfolio_timeseries(
    positions: pd.DataFrame,
    prices: pd.DataFrame,
    total_capital: float,
    start_date: str,
    end_date: Optional[str] = None,
    capital_mode: str = "total",
    allow_signed_settlement_cash: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    pos = positions[["ticker", "post_trade_shares", "base_price", "base_value"]].copy()
    if "name_final" in positions.columns:
        pos["name"] = positions["name_final"].to_numpy()
    elif "name" in positions.columns:
        pos["name"] = positions["name"].to_numpy()
    else:
        pos["name"] = pd.NA
    if "base_date" in positions.columns:
        pos["base_date"] = pd.to_datetime(positions["base_date"], errors="coerce").dt.normalize()
    else:
        pos["base_date"] = pd.to_datetime(start_date).normalize()
    pos = pos[pos["post_trade_shares"] > 0].copy()

    if pos.empty:
        raise ValueError("No positive post-trade holdings found")
    if pos["ticker"].duplicated().any():
        duplicates = pos.loc[pos["ticker"].duplicated(False), "ticker"].astype(str).tolist()
        raise ValueError(f"positions contain duplicate tickers: {duplicates}")
    numeric = pos[["post_trade_shares", "base_price", "base_value"]].apply(
        pd.to_numeric, errors="coerce"
    )
    if numeric.isna().any(axis=None) or (~np.isfinite(numeric.to_numpy(dtype=float))).any():
        raise ValueError("positions contain invalid shares/base_price/base_value")
    if numeric["post_trade_shares"].le(0).any() or numeric["base_price"].le(0).any():
        raise ValueError("positions shares and base_price must be positive")

    invested_base = float(pos["base_value"].sum())
    if capital_mode == "invested":
        initial_nav = invested_base
        cash = 0.0
    elif capital_mode == "total":
        initial_nav = float(total_capital)
        cash = float(total_capital - invested_base)
    else:
        raise ValueError(f"unsupported capital_mode: {capital_mode}")
    if cash < -1e-8 and not allow_signed_settlement_cash:
        raise ValueError(f"static portfolio creates negative cash: {cash}")

    requested_start = pd.to_datetime(start_date).normalize()
    requested_end = pd.to_datetime(end_date).normalize() if end_date else None
    calendar_prices = prices.loc[prices["date"].ge(requested_start)].copy()
    if requested_end is not None:
        calendar_prices = calendar_prices.loc[calendar_prices["date"].le(requested_end)].copy()
    expected_dates = pd.Index(calendar_prices["date"].dropna().drop_duplicates().sort_values())
    px = calendar_prices.merge(
        pos[["ticker", "name", "post_trade_shares", "base_date", "base_price", "base_value"]],
        on="ticker",
        how="inner",
    )

    if px.empty:
        min_dt = prices['date'].min() if len(prices) else None
        max_dt = prices['date'].max() if len(prices) else None
        raise ValueError(f"No daily prices available in requested date range: start={start_date} end={end_date}; available={min_dt}..{max_dt}")
    if px.duplicated(["ticker", "date"]).any():
        raise ValueError("daily prices contain duplicate ticker/date rows for held positions")
    coverage = px.groupby("date")["ticker"].nunique().reindex(expected_dates, fill_value=0)
    incomplete = coverage.loc[coverage.ne(len(pos))]
    if not incomplete.empty:
        detail = {
            str(pd.Timestamp(date).date()): int(count)
            for date, count in incomplete.head(10).items()
        }
        raise ValueError(
            "static NAV requires one exact close per held ticker on every portfolio date; "
            f"expected={len(pos)} incomplete={detail}"
        )

    px["position_value"] = px["post_trade_shares"] * px["price"]

    daily = (
        px.groupby("date", as_index=False)["position_value"]
        .sum()
        .rename(columns={"position_value": "portfolio_value_ex_cash"})
    )
    daily["cash"] = cash
    daily["nav"] = daily["portfolio_value_ex_cash"] + daily["cash"]

    if initial_nav <= 0:
        raise ValueError(f"initial_nav must be positive: {initial_nav}")
    daily["cum_return"] = daily["nav"] / initial_nav - 1.0
    daily["daily_return"] = daily["nav"].pct_change().fillna(daily["cum_return"])
    daily["prior_peak_nav"] = pd.to_numeric(daily["nav"], errors="coerce").cummax()
    daily["drawdown"] = daily["nav"] / daily["prior_peak_nav"] - 1.0
    peak_dates = daily["date"].where(pd.to_numeric(daily["nav"], errors="coerce").eq(daily["prior_peak_nav"]))
    daily["drawdown_peak_date"] = peak_dates.ffill()

    last_date = daily["date"].max()
    last_px = px[px["date"] == last_date].copy()
    contrib = last_px.copy()
    contrib["pnl"] = contrib["position_value"] - contrib["base_value"]
    denom = contrib["base_value"].replace(0.0, np.nan)
    contrib["position_period_return"] = contrib["pnl"] / denom
    contrib["contribution_to_total_return"] = contrib["pnl"] / initial_nav
    last_nav = float(daily.loc[daily["date"] == last_date, "nav"].iloc[0])
    contrib["weight_at_last_nav"] = contrib["position_value"] / last_nav
    contrib["shares_at_start"] = contrib["post_trade_shares"]
    contrib["start_date"] = contrib["base_date"]
    contrib["start_price"] = contrib["base_price"]
    contrib["start_position_value"] = contrib["base_value"]
    contrib["end_date"] = contrib["date"]
    contrib["end_price"] = contrib["price"]
    contrib["end_position_value"] = contrib["position_value"]
    contrib["dividends"] = 0.0
    contrib["net_intermediate_trade_cashflow"] = 0.0
    contrib["trading_cost_allocated"] = 0.0
    contrib["period_pnl"] = contrib["pnl"]
    contrib["weight_at_start_nav"] = contrib["base_value"] / initial_nav
    contrib["weight_at_end_nav"] = contrib["weight_at_last_nav"]
    contrib["performance_basis"] = STATIC_METHOD
    contrib = contrib.sort_values("contribution_to_total_return", ascending=False)
    contrib = contrib[
        CANONICAL_CONTRIBUTION_COLUMNS
        + [column for column in contrib.columns if column not in CANONICAL_CONTRIBUTION_COLUMNS]
    ]
    dd_idx = daily["drawdown"].astype(float).idxmin()
    dd_peak_date = daily.loc[dd_idx, "drawdown_peak_date"] if dd_idx in daily.index else pd.NaT
    max_drawdown_date = daily.loc[dd_idx, "date"] if dd_idx in daily.index else pd.NaT
    peak_nav = float(pd.to_numeric(daily["prior_peak_nav"], errors="coerce").max())

    summary = {
        "start_date": str(pd.to_datetime(daily["date"].min()).date()),
        "end_date": str(pd.to_datetime(last_date).date()),
        "initial_nav": initial_nav,
        "invested_base": invested_base,
        "opening_cash": cash,
        "start_cash": cash,
        "cash_after_rebalance": cash,
        "last_cash": cash,
        "end_cash": cash,
        "cash_pnl": 0.0,
        "cash_contribution": 0.0,
        "total_transaction_cost": 0.0,
        "total_trading_costs": 0.0,
        "trading_cost_contribution": 0.0,
        "last_nav": last_nav,
        "cum_return": float(daily["cum_return"].iloc[-1]),
        "peak_nav": peak_nav,
        "max_drawdown": float(pd.to_numeric(daily["drawdown"], errors="coerce").min()),
        "drawdown_peak_date": str(pd.to_datetime(dd_peak_date).date()) if pd.notna(dd_peak_date) else None,
        "max_drawdown_date": (
            str(pd.to_datetime(max_drawdown_date).date()) if pd.notna(max_drawdown_date) else None
        ),
        "num_positions": int(len(pos)),
        "nav_dividend_treatment": NAV_DIVIDEND_TREATMENT,
        "cash_dividends_included": False,
        "dividends": 0.0,
        "external_cash_flows": 0.0,
        "external_cash_flow_treatment": "NONE_RECORDED; NONZERO_EXTERNAL_FLOWS_REQUIRE_CASH_FLOW_AWARE_TWR",
        "contribution_method": STATIC_METHOD,
    }
    validate_contribution_reconciliation(daily, contrib, summary)
    return daily, contrib, summary


def _constant_benchmark_metadata(df: pd.DataFrame) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for column in BENCHMARK_METADATA_COLUMNS:
        if column not in df.columns:
            continue
        values = df[column].dropna().astype(str).str.strip()
        values = values.loc[values.ne("")].drop_duplicates()
        if len(values) > 1:
            raise ValueError(
                f"benchmark metadata must be constant within one series: "
                f"column={column} values={values.tolist()}"
            )
        if len(values) == 1:
            metadata[column] = str(values.iloc[0])
    return metadata


def _validate_benchmark_metadata(
    metadata: dict[str, str],
    *,
    require_official_index: bool,
) -> None:
    return_type = metadata.get("return_type")
    if NAV_DIVIDEND_TREATMENT == "EXCLUDED" and return_type not in (None, "PRICE"):
        raise ValueError(
            "portfolio NAV excludes cash dividends, so benchmark return_type must be PRICE"
        )
    if not require_official_index:
        return
    missing = sorted(set(OFFICIAL_INDEX_REQUIRED_METADATA) - set(metadata))
    if missing:
        raise ValueError(f"official index benchmark metadata missing: {missing}")
    mismatches = {
        key: {"expected": expected, "actual": metadata.get(key)}
        for key, expected in OFFICIAL_INDEX_REQUIRED_METADATA.items()
        if metadata.get(key) != expected
    }
    if mismatches:
        raise ValueError(f"official index benchmark metadata mismatch: {mismatches}")
    identifier = str(metadata.get("benchmark_identifier", "")).strip()
    if not identifier:
        raise ValueError("official index benchmark_identifier is required")
    if identifier in {"229200", "292190", "304760"}:
        raise ValueError("ETF identifier is forbidden for the official KRX 300 index benchmark")


def maybe_load_benchmark(
    path: Optional[str],
    start_date: str,
    end_date: str,
    *,
    require_official_index: bool = False,
) -> Optional[pd.DataFrame]:
    if not path:
        if require_official_index:
            raise ValueError("official index benchmark file is required")
        return None
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"benchmark file not found: {p}")

    if p.suffix.lower() == ".parquet":
        df = pd.read_parquet(p)
    else:
        df = pd.read_csv(p)

    metadata = _constant_benchmark_metadata(df)
    _validate_benchmark_metadata(metadata, require_official_index=require_official_index)

    cols = df.columns.tolist()
    date_col = pick_first_existing(cols, ["date", "Date", "dt", "ymd", "trd_date"])
    price_col = pick_first_existing(cols, ["close", "Close", "adj_close", "price", "index_level"])
    if date_col is None or price_col is None:
        raise ValueError(f"Could not detect benchmark date/price columns: {cols}")

    out = df[[date_col, price_col]].copy()
    out = out.rename(columns={date_col: "date", price_col: "benchmark_price"})
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["benchmark_price"] = pd.to_numeric(out["benchmark_price"], errors="coerce")
    if out[["date", "benchmark_price"]].isna().any(axis=None):
        raise ValueError("benchmark contains invalid date or price values")
    out["date"] = out["date"].dt.normalize()
    if out["date"].duplicated().any():
        duplicate_dates = out.loc[out["date"].duplicated(False), "date"].dt.strftime("%Y-%m-%d")
        raise ValueError(f"benchmark contains duplicate dates: {duplicate_dates.tolist()}")
    if out["benchmark_price"].le(0).any():
        raise ValueError("benchmark contains a non-positive index level")
    out = out.sort_values("date")
    out = out[(out["date"] >= pd.to_datetime(start_date)) & (out["date"] <= pd.to_datetime(end_date))].copy()
    if out.empty:
        raise ValueError(
            f"benchmark has no rows in the portfolio window: start={start_date} end={end_date}"
        )
    base = float(out["benchmark_price"].iloc[0])
    out["benchmark_daily_return"] = out["benchmark_price"].pct_change(fill_method=None).fillna(0.0)
    out["benchmark_cum_return"] = out["benchmark_price"] / base - 1.0
    compounded = (1.0 + out["benchmark_daily_return"]).cumprod() - 1.0
    if not np.allclose(
        compounded.to_numpy(dtype=float),
        out["benchmark_cum_return"].to_numpy(dtype=float),
        atol=1e-12,
        rtol=1e-12,
    ):
        raise ValueError(
            "benchmark endpoint-level return and independently compounded daily returns disagree"
        )
    out["benchmark_cum_return_compounded"] = compounded
    out.attrs["benchmark_metadata"] = metadata
    return out


def align_benchmark_to_portfolio_dates(
    daily: pd.DataFrame,
    benchmark: pd.DataFrame,
) -> pd.DataFrame:
    """Join one official index observation to every portfolio trading date.

    Exchange holidays are absent from both calendars.  A date present on only
    one side is treated as incomplete upstream data.  No forward fill and,
    critically, no future-value backfill is permitted.
    """

    if "date" not in daily.columns or "date" not in benchmark.columns:
        raise ValueError("portfolio and benchmark must both contain date")
    portfolio = daily.copy()
    index = benchmark.copy()
    portfolio["date"] = pd.to_datetime(portfolio["date"], errors="coerce").dt.normalize()
    index["date"] = pd.to_datetime(index["date"], errors="coerce").dt.normalize()
    if portfolio["date"].isna().any() or index["date"].isna().any():
        raise ValueError("portfolio or benchmark contains an invalid date")
    if portfolio["date"].duplicated().any():
        raise ValueError("portfolio daily NAV contains duplicate trading dates")
    if index["date"].duplicated().any():
        raise ValueError("benchmark contains duplicate trading dates")
    if "benchmark_cum_return_compounded" not in index.columns:
        if "benchmark_daily_return" not in index.columns:
            raise ValueError("benchmark daily returns are required for independent compounding QA")
        index["benchmark_cum_return_compounded"] = (
            1.0 + pd.to_numeric(index["benchmark_daily_return"], errors="raise")
        ).cumprod() - 1.0

    portfolio_dates = set(portfolio["date"])
    benchmark_dates = set(index["date"])
    missing = sorted(portfolio_dates - benchmark_dates)
    extra = sorted(benchmark_dates - portfolio_dates)
    if missing or extra:
        def _iso(values: list[pd.Timestamp]) -> list[str]:
            return [pd.Timestamp(value).strftime("%Y-%m-%d") for value in values]

        raise ValueError(
            "benchmark/portfolio trading calendars are not identical under the no-fill policy: "
            f"missing_benchmark_dates={_iso(missing)} extra_benchmark_dates={_iso(extra)}"
        )

    keep = [
        column
        for column in (
            "date", "benchmark_price", "benchmark_daily_return", "benchmark_cum_return",
            "benchmark_cum_return_compounded",
        )
        if column in index.columns
    ]
    merged = portfolio.merge(index[keep], on="date", how="left", validate="one_to_one")
    merged = merged.sort_values("date").reset_index(drop=True)
    required = [
        "benchmark_price", "benchmark_daily_return", "benchmark_cum_return",
        "benchmark_cum_return_compounded",
    ]
    if merged[required].isna().any(axis=None):
        raise ValueError("benchmark alignment produced missing values; filling is forbidden")
    if abs(float(merged.iloc[0]["benchmark_cum_return"])) > 1e-12:
        raise ValueError("benchmark cumulative return must equal zero on the first portfolio date")
    if "cum_return" in merged and abs(float(merged.iloc[0]["cum_return"])) > 1e-12:
        raise ValueError("portfolio cumulative return must equal zero on the first common date")
    expected = (
        pd.to_numeric(merged["benchmark_price"], errors="raise")
        / float(merged.iloc[0]["benchmark_price"])
        - 1.0
    )
    if not np.allclose(
        expected.to_numpy(dtype=float),
        pd.to_numeric(merged["benchmark_cum_return"], errors="raise").to_numpy(dtype=float),
        atol=1e-12,
        rtol=1e-12,
    ):
        raise ValueError("benchmark cumulative return does not reproduce from official index levels")
    if not np.allclose(
        pd.to_numeric(merged["benchmark_cum_return_compounded"], errors="raise").to_numpy(dtype=float),
        pd.to_numeric(merged["benchmark_cum_return"], errors="raise").to_numpy(dtype=float),
        atol=1e-12,
        rtol=1e-12,
    ):
        raise ValueError("benchmark daily compounding does not reproduce endpoint-level return")
    return merged


def summarize_user_confirmed_execution_statement(
    statement_path: str | Path,
    manifest_path: str | Path,
    *,
    source_image_paths: list[str | Path] | tuple[str | Path, ...],
    derived_snapshot_path: str | Path,
    performance_start_date: pd.Timestamp,
) -> tuple[pd.DataFrame, dict]:
    """Reconcile same-day broker-statement cash/costs without pricing CASH_EQ.

    The statement contains one sold cash-equivalent funding instrument that is
    intentionally absent from the equity price panel.  Therefore mark-to-close
    attribution is explicitly not applicable, while every visible settlement
    amount, fee/tax, running deposit balance, and final signed balance must
    still reconcile exactly.
    """

    evidence = load_user_confirmed_broker_statement_execution_evidence(
        ledger_path=statement_path,
        manifest_path=manifest_path,
        source_image_paths=source_image_paths,
        derived_snapshot_path=derived_snapshot_path,
    )
    if evidence.execution_date.normalize() != performance_start_date.normalize():
        raise ValueError("user-confirmed execution date differs from performance start")
    statement = evidence.frame
    sides = statement["trade_side"]
    funding = statement["instrument_role"].eq("CASH_EQUIVALENT_FUNDING")
    gross_buy = float(statement.loc[sides.eq("BUY"), "trade_amount"].sum())
    gross_sell = float(statement.loc[sides.eq("SELL"), "trade_amount"].sum())
    trading_cost = float(statement["transaction_cost"].sum())
    final_cash = float(statement["deposit_balance"].dropna().iloc[-1])
    cost_residual = final_cash - (gross_sell - gross_buy - trading_cost)
    if abs(cost_residual) > 1e-8:
        raise ValueError("user-confirmed execution cash/cost reconciliation failed")
    identifiers = statement["ticker"].astype("string").fillna("").str.strip()
    identifiers = identifiers.where(~funding, statement["instrument_id"].astype(str))
    summary = {
        "schema_version": 1,
        "status": "NOT_APPLICABLE_MARK_TO_CLOSE_CASH_EQ_UNPRICED",
        "execution_window_type": "SAME_DAY",
        "first_fill_date": str(evidence.first_fill_date.date()),
        "last_fill_date": str(evidence.last_fill_date.date()),
        "execution_trading_day_count": 1,
        "fill_count": int(len(statement)),
        "traded_security_count": int(identifiers.nunique()),
        "gross_buy_value": gross_buy,
        "gross_sell_value": gross_sell,
        "net_execution_cashflow": final_cash,
        "settlement_cash_effect": final_cash,
        "final_deposit_balance": final_cash,
        "final_signed_settlement_balance": final_cash,
        "signed_settlement_liability": max(-final_cash, 0.0),
        "trading_cost": trading_cost,
        "gross_mark_to_last_fill_close_pnl": None,
        "net_mark_to_last_fill_close_pnl": None,
        "cost_reconciliation_residual": cost_residual,
        "cash_cost_reconciliation_status": "PASS",
        "attribution_basis": (
            "SAME_DAY_USER_CONFIRMED_BROKER_STATEMENT; MARK_TO_CLOSE_NOT_APPLICABLE_"
            "CASH_EQ_FUNDING_UNPRICED"
        ),
        "included_in_post_rebalance_performance": False,
        "source_type": "USER_CONFIRMED_BROKER_STATEMENT_IMAGES",
        "source_path": str(evidence.ledger_path),
        "source_sha256": evidence.ledger_sha256,
        "source_certification_path": str(evidence.manifest_path),
        "source_certification_sha256": evidence.manifest_sha256,
    }
    return pd.DataFrame(columns=EXECUTION_WINDOW_ATTRIBUTION_COLUMNS), summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--fill-ledger", "--fill_ledger", dest="fill_ledger", default=None,
        help="highest-priority certified executed-fill ledger",
    )
    ap.add_argument(
        "--fill-ledger-meta", "--fill_ledger_meta", dest="fill_ledger_meta", default=None,
        help="fill-ledger performance-cost-basis/v1 certification sidecar",
    )
    ap.add_argument(
        "--post-trade-snapshot", "--post_trade_snapshot", dest="post_trade_snapshot", default=None,
        help="certified post-trade holdings snapshot",
    )
    ap.add_argument(
        "--post-trade-snapshot-meta", "--post_trade_snapshot_meta",
        dest="post_trade_snapshot_meta", default=None,
        help="post-trade snapshot performance-cost-basis/v1 certification sidecar",
    )
    ap.add_argument(
        "--certified-manifest", "--certified_manifest", dest="certified_manifest", default=None,
        help="lowest-priority authoritative manifest with an explicit dated and hashed performance source",
    )
    ap.add_argument(
        "--execution-fill-ledger", dest="execution_fill_ledger", default=None,
        help="optional certified execution ledger used only for separate execution-window attribution",
    )
    ap.add_argument(
        "--execution-fill-ledger-meta", dest="execution_fill_ledger_meta", default=None,
        help="certification sidecar for --execution-fill-ledger",
    )
    ap.add_argument(
        "--user-statement-ledger", default=None,
        help="reviewed full broker-statement transcription used with its image-bound bundle",
    )
    ap.add_argument(
        "--user-statement-manifest", default=None,
        help="certification manifest for the complete user-confirmed statement bundle",
    )
    ap.add_argument(
        "--user-statement-image", action="append", default=[],
        help="one original statement page; repeat for every page in manifest order",
    )
    ap.add_argument(
        "--user-statement-snapshot", default=None,
        help="derived exact-close post-trade equity snapshot bound by the statement manifest",
    )
    ap.add_argument(
        "--execution_plan", default=None,
        help="UNCERTIFIED legacy fixture only; never evidence of executed trades",
    )
    ap.add_argument(
        "--holdings_csv", default=None,
        help="UNCERTIFIED legacy fixture only; filename dates are never parsed",
    )
    ap.add_argument(
        "--non-production-fixture", action="store_true",
        help="explicitly allow legacy holdings/execution-plan inputs and mark performance_production_ready=false",
    )
    ap.add_argument("--prices_daily", required=True)
    ap.add_argument("--total_capital", required=True, type=float)
    ap.add_argument("--capital_mode", choices=["total", "invested"], default="total", help="total: include cash from total_capital; invested: evaluate invested holdings only")
    ap.add_argument("--target_date", required=True, help="performance start / rebalance date, e.g. 2026-03-31")
    ap.add_argument("--end_date", default=None, help="performance end date; default = latest available")
    ap.add_argument("--benchmark", default=None, help="optional benchmark csv/parquet with date+price")
    ap.add_argument("--benchmark_name", default=None, help="optional benchmark display name")
    ap.add_argument(
        "--require-official-index",
        action="store_true",
        help="require the official KRX 300 INDEX/PRICE metadata contract",
    )
    ap.add_argument("--output_dir", required=True)
    ap.add_argument("--tag", default=None, help="optional tag for output filenames")
    args = ap.parse_args()

    if args.end_date is not None and pd.to_datetime(args.end_date) < pd.to_datetime(args.target_date):
        raise ValueError(f"end_date must be >= target_date: target_date={args.target_date} end_date={args.end_date}")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tag = args.tag or f"target={args.target_date}"

    prices = load_prices_daily(args.prices_daily)
    statement_requested = any(
        (
            args.user_statement_ledger,
            args.user_statement_manifest,
            args.user_statement_snapshot,
            bool(args.user_statement_image),
        )
    )
    certified_requested = any(
        value is not None
        for value in (args.fill_ledger, args.post_trade_snapshot, args.certified_manifest)
    ) or statement_requested
    legacy_requested = bool(args.execution_plan) or bool(args.holdings_csv)
    if bool(args.execution_fill_ledger) != bool(args.execution_fill_ledger_meta):
        raise ValueError("execution-window attribution requires both ledger and certification sidecar")
    statement_complete = all(
        (
            args.user_statement_ledger,
            args.user_statement_manifest,
            args.user_statement_snapshot,
            bool(args.user_statement_image),
        )
    )
    if statement_requested and not statement_complete:
        raise ValueError(
            "user-confirmed statement requires ledger, manifest, every image, and snapshot"
        )
    if args.execution_fill_ledger and statement_requested:
        raise ValueError("execution fill and user-confirmed statement modes are mutually exclusive")
    if legacy_requested and args.execution_fill_ledger:
        raise ValueError("execution-window attribution cannot be combined with legacy fixture performance")
    if certified_requested and legacy_requested:
        raise ValueError(
            "certified performance sources cannot be combined with legacy holdings/execution_plan"
        )

    if certified_requested:
        if args.capital_mode != "total":
            raise ValueError("certified performance requires capital_mode=total")
        basis = resolve_performance_cost_basis(
            fill_ledger=args.fill_ledger,
            fill_ledger_meta=args.fill_ledger_meta,
            user_statement_ledger=args.user_statement_ledger,
            user_statement_manifest=args.user_statement_manifest,
            user_statement_images=args.user_statement_image,
            user_statement_snapshot=args.user_statement_snapshot,
            post_trade_snapshot=args.post_trade_snapshot,
            post_trade_snapshot_meta=args.post_trade_snapshot_meta,
            certified_manifest=args.certified_manifest,
        )
        target_date = pd.Timestamp(args.target_date).normalize()
        if target_date != basis.basis_date:
            raise ValueError(
                "--target_date is only a requested evaluation boundary and must match certified provenance; "
                f"target_date={target_date.date()} certified_basis_date={basis.basis_date.date()}"
            )
        evaluation_end = (
            pd.Timestamp(args.end_date).normalize()
            if args.end_date
            else pd.to_datetime(prices["date"], errors="raise").max().normalize()
        )
        conditional_statement_performance = (
            basis.source_type == "USER_CONFIRMED_BROKER_STATEMENT_IMAGES"
            and basis.production_ready is False
            and str((basis.metadata or {}).get("interval_activity_status", "")).upper()
            == "UNKNOWN"
            and str(
                (basis.metadata or {}).get("performance_calculation_status", "")
            ).upper()
            == "CONDITIONAL_STATIC_SHADOW_LIABILITY"
        )
        if conditional_statement_performance:
            interval_contract = {
                "external_cash_flows": None,
                "external_cash_flow_treatment": "UNKNOWN_NOT_ASSUMED_ZERO",
                "external_cash_flows_verified": False,
                "transaction_history_complete": False,
                "transaction_history_through_date": None,
                "transaction_history_policy": "UNVERIFIED_INTERVAL_ACTIVITY",
                "intermediate_trades_verified_none": False,
                "interval_activity_status": "UNKNOWN",
                "performance_interval_activity_proven": False,
                "performance_blocking_failure": "PERFORMANCE_INTERVAL_ACTIVITY_NOT_PROVEN",
            }
        else:
            interval_contract = validate_certified_interval_contract(basis, evaluation_end)
            interval_contract.update(
                {
                    "interval_activity_status": "VERIFIED",
                    "performance_interval_activity_proven": True,
                    "performance_blocking_failure": None,
                }
            )
        if basis.calculation_method == TRANSACTION_METHOD:
            positions, daily, contrib, summary = calculate_transaction_aware_performance(
                basis,
                prices,
                total_capital=args.total_capital,
                end_date=args.end_date,
            )
        else:
            positions = materialize_static_positions(basis, prices)
            validate_static_capital_contract(
                positions,
                basis,
                args.total_capital,
                end_date=evaluation_end,
                allow_conditional_interval=conditional_statement_performance,
            )
            daily, contrib, summary = calc_portfolio_timeseries(
                positions=positions,
                prices=prices,
                total_capital=args.total_capital,
                start_date=str(basis.basis_date.date()),
                end_date=args.end_date,
                capital_mode="total",
                allow_signed_settlement_cash=(
                    basis.metadata is not None
                    and str(basis.metadata.get("opening_cash_basis", "")).strip().upper()
                    == "SIGNED_BROKER_SETTLEMENT_BALANCE"
                    and basis.metadata.get("signed_settlement_balance_verified") is True
                ),
            )
        source_type = basis.source_type
        source_path = basis.source_path
        summary.update(
            {
                **interval_contract,
                "performance_cost_basis_contract": "performance-cost-basis/v1",
                "performance_calculation_method": (
                    "CONDITIONAL_STATIC_SHADOW_LIABILITY"
                    if conditional_statement_performance
                    else basis.calculation_method
                ),
                "performance_production_ready": bool(basis.production_ready),
                "performance_cost_basis_status": (
                    "OPENING_BASIS_CERTIFIED_INTERVAL_ACTIVITY_UNKNOWN"
                    if conditional_statement_performance
                    else "CERTIFIED"
                ),
                "opening_basis_proven": (basis.metadata or {}).get(
                    "opening_basis_proven", True
                ),
                "source_sha256": basis.source_sha256,
                "source_basis_date": str(basis.basis_date.date()),
                "certified_opening_nav": basis.opening_nav,
                "certified_opening_cash": basis.opening_cash,
                "certified_post_fill_cash": basis.post_fill_cash,
                "source_certification_path": (
                    str(basis.certification_path) if basis.certification_path else None
                ),
                "source_certification_sha256": basis.certification_sha256,
                "filename_date_inference_used": False,
                "source_precedence": [
                    "FILL_LEDGER", "USER_CONFIRMED_BROKER_STATEMENT_IMAGES",
                    "POST_TRADE_SNAPSHOT", "CERTIFIED_MANIFEST", "FAIL"
                ],
            }
        )
    else:
        if not args.non_production_fixture:
            raise ValueError(
                "no certified performance source; legacy --holdings_csv/--execution_plan is allowed "
                "only with --non-production-fixture and is never production-ready"
            )
        if bool(args.execution_plan) == bool(args.holdings_csv):
            raise ValueError(
                "non-production fixture mode requires exactly one of --execution_plan or --holdings_csv"
            )
        base_px = latest_price_on_or_before(prices, args.target_date)
        if args.holdings_csv:
            holdings_df = load_holdings_csv(args.holdings_csv)
            positions = build_positions_from_holdings(holdings_df, base_px)
            source_type = "UNCERTIFIED_HOLDINGS_FIXTURE"
            source_path = args.holdings_csv
        else:
            exec_df = load_execution_plan(args.execution_plan)
            positions = build_post_rebalance_positions(exec_df, base_px)
            source_type = "UNCERTIFIED_EXECUTION_PLAN_FIXTURE"
            source_path = args.execution_plan
        daily, contrib, summary = calc_portfolio_timeseries(
            positions=positions,
            prices=prices,
            total_capital=args.total_capital,
            start_date=args.target_date,
            end_date=args.end_date,
            capital_mode=args.capital_mode,
        )
        summary.update(
            {
                "performance_cost_basis_contract": None,
                "performance_calculation_method": STATIC_METHOD,
                "performance_production_ready": False,
                "performance_cost_basis_status": "UNCERTIFIED_FIXTURE",
                "source_sha256": sha256_file(source_path),
                "source_basis_date": None,
                "source_certification_path": None,
                "source_certification_sha256": None,
                "filename_date_inference_used": False,
                "source_precedence": [
                    "FILL_LEDGER", "USER_CONFIRMED_BROKER_STATEMENT_IMAGES",
                    "POST_TRADE_SNAPSHOT", "CERTIFIED_MANIFEST", "FAIL"
                ],
            }
        )
    summary["source_type"] = source_type
    summary["source_path"] = str(source_path)
    summary["capital_mode"] = args.capital_mode

    execution_attribution_basis = None
    if args.execution_fill_ledger:
        if not certified_requested:
            raise ValueError(
                "execution-window attribution requires a certified primary performance source"
            )
        if basis.calculation_method != STATIC_METHOD:
            raise ValueError(
                "--execution-fill-ledger is attribution-only and requires a static certified "
                "post-trade snapshot as the primary NAV basis"
            )
        execution_attribution_basis = load_certified_execution_fill_ledger(
            args.execution_fill_ledger,
            args.execution_fill_ledger_meta,
        )
        if execution_attribution_basis.basis_date != basis.basis_date:
            raise ValueError(
                "execution ledger last_fill_date must equal the primary post-trade "
                "performance_start_date: "
                f"last_fill={execution_attribution_basis.basis_date.date()} "
                f"performance_start={basis.basis_date.date()}"
            )
    if statement_requested:
        if not certified_requested or basis.calculation_method != STATIC_METHOD:
            raise ValueError(
                "user-confirmed execution reconciliation requires a certified static snapshot"
            )
        execution_attribution, execution_attribution_summary = (
            summarize_user_confirmed_execution_statement(
                args.user_statement_ledger,
                args.user_statement_manifest,
                source_image_paths=args.user_statement_image,
                derived_snapshot_path=args.user_statement_snapshot,
                performance_start_date=basis.basis_date,
            )
        )
    elif execution_attribution_basis is not None:
        execution_attribution, execution_attribution_summary = (
            calculate_execution_window_attribution(execution_attribution_basis, prices)
        )
        execution_attribution_summary.update({
            "schema_version": 1,
            "source_type": "FILL_LEDGER",
            "source_path": str(execution_attribution_basis.source_path),
            "source_sha256": execution_attribution_basis.source_sha256,
            "source_certification_path": str(execution_attribution_basis.certification_path),
            "source_certification_sha256": execution_attribution_basis.certification_sha256,
        })
    else:
        execution_attribution = pd.DataFrame(columns=EXECUTION_WINDOW_ATTRIBUTION_COLUMNS)
        execution_attribution_summary = {
            "schema_version": 1,
            "status": "NOT_AVAILABLE_NO_CERTIFIED_FILL_LEDGER",
            "execution_window_type": "NOT_APPLICABLE",
            "first_fill_date": None,
            "last_fill_date": None,
            "execution_trading_day_count": None,
            "fill_count": None,
            "traded_security_count": None,
            "gross_buy_value": None,
            "gross_sell_value": None,
            "net_execution_cashflow": None,
            "settlement_cash_effect": None,
            "final_signed_settlement_balance": None,
            "signed_settlement_liability": None,
            "trading_cost": None,
            "gross_mark_to_last_fill_close_pnl": None,
            "net_mark_to_last_fill_close_pnl": None,
            "cost_reconciliation_residual": None,
            "attribution_basis": None,
            "included_in_post_rebalance_performance": False,
            "source_type": None,
            "source_path": None,
            "source_sha256": None,
            "source_certification_path": None,
            "source_certification_sha256": None,
        }
    summary["execution_window_attribution_status"] = execution_attribution_summary["status"]
    summary["execution_window_type"] = execution_attribution_summary[
        "execution_window_type"
    ]
    summary["execution_window_attribution_included_in_post_rebalance_performance"] = False

    bm = maybe_load_benchmark(
        args.benchmark,
        summary["start_date"],
        summary["end_date"],
        require_official_index=args.require_official_index,
    )
    if bm is not None:
        benchmark_metadata = dict(bm.attrs.get("benchmark_metadata") or {})
        daily = align_benchmark_to_portfolio_dates(daily, bm)
        daily["active_return"] = daily["cum_return"] - daily["benchmark_cum_return"]
        last_bm = float(daily["benchmark_cum_return"].iloc[-1])
        summary["benchmark_cum_return"] = last_bm
        summary["active_return"] = float(summary["cum_return"] - last_bm)
        summary["benchmark_last_available_date"] = str(pd.to_datetime(daily["date"].iloc[-1]).date())
        summary["benchmark_missing_after_last_available"] = False
        summary["benchmark_cum_return_filled_forward"] = False
        summary["benchmark_cum_return_backfilled"] = False
        summary["benchmark_alignment_policy"] = BENCHMARK_ALIGNMENT_POLICY
        summary["benchmark_missing_dates"] = 0
        summary["benchmark_extra_dates"] = 0
        summary["benchmark_start_date"] = str(pd.to_datetime(daily["date"].iloc[0]).date())
        summary["benchmark_start_level"] = float(daily["benchmark_price"].iloc[0])
        summary["benchmark_end_date"] = str(pd.to_datetime(daily["date"].iloc[-1]).date())
        summary["benchmark_end_level"] = float(daily["benchmark_price"].iloc[-1])
        summary["benchmark_aligned_row_count"] = int(len(daily))
        summary["benchmark_daily_compounding_reconciliation_status"] = "PASS"
        for key, value in benchmark_metadata.items():
            summary_key = "benchmark_asset_type" if key == "asset_type" else (
                "benchmark_return_type" if key == "return_type" else (
                    "benchmark_source" if key == "source" else key
                )
            )
            summary[summary_key] = value
        metadata_name = benchmark_metadata.get("benchmark_name")
        if args.benchmark_name and metadata_name and str(args.benchmark_name) != metadata_name:
            raise ValueError(
                f"benchmark_name argument disagrees with benchmark metadata: "
                f"argument={args.benchmark_name!r} metadata={metadata_name!r}"
            )
        if args.benchmark_name:
            summary["benchmark_name"] = str(args.benchmark_name)
        elif metadata_name:
            summary["benchmark_name"] = metadata_name

    positions_path = out_dir / f"live_positions__{tag}.csv"
    daily_path = out_dir / f"live_performance_daily__{tag}.csv"
    contrib_path = out_dir / f"live_contribution__{tag}.csv"
    summary_path = out_dir / f"live_performance_summary__{tag}.json"
    execution_attribution_path = out_dir / f"execution_window_attribution__{tag}.csv"
    execution_attribution_summary_path = (
        out_dir / f"execution_window_attribution_summary__{tag}.json"
    )

    execution_attribution.to_csv(execution_attribution_path, index=False, encoding="utf-8-sig")
    execution_attribution_summary.update(
        {
            "attribution_csv_path": str(execution_attribution_path.resolve()),
            "attribution_csv_sha256": sha256_file(execution_attribution_path),
        }
    )
    execution_attribution_summary_path.write_text(
        json.dumps(execution_attribution_summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    summary.update(
        {
            "execution_window_attribution_csv_path": str(
                execution_attribution_path.resolve()
            ),
            "execution_window_attribution_csv_sha256": sha256_file(
                execution_attribution_path
            ),
            "execution_window_attribution_json_path": str(
                execution_attribution_summary_path.resolve()
            ),
            "execution_window_attribution_json_sha256": sha256_file(
                execution_attribution_summary_path
            ),
            "execution_window_first_fill_date": execution_attribution_summary.get(
                "first_fill_date"
            ),
            "execution_window_last_fill_date": execution_attribution_summary.get(
                "last_fill_date"
            ),
            "execution_window_trading_day_count": execution_attribution_summary.get(
                "execution_trading_day_count"
            ),
            "execution_window_source_path": execution_attribution_summary.get("source_path"),
            "execution_window_source_sha256": execution_attribution_summary.get(
                "source_sha256"
            ),
            "execution_window_source_certification_path": (
                execution_attribution_summary.get("source_certification_path")
            ),
            "execution_window_source_certification_sha256": (
                execution_attribution_summary.get("source_certification_sha256")
            ),
        }
    )
    positions.to_csv(positions_path, index=False, encoding="utf-8-sig")
    daily.to_csv(daily_path, index=False, encoding="utf-8-sig")
    contrib.to_csv(contrib_path, index=False, encoding="utf-8-sig")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] positions : {positions_path}")
    print(f"[OK] daily     : {daily_path}")
    print(f"[OK] contrib   : {contrib_path}")
    print(f"[OK] summary   : {summary_path}")
    print(f"[OK] execution attribution: {execution_attribution_path}")
    print(f"[OK] execution attribution summary: {execution_attribution_summary_path}")

    print("\n[SUMMARY]")
    for k, v in summary.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
