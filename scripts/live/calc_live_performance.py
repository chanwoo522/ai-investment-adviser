#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


def normalize_ticker(s: pd.Series) -> pd.Series:
    return s.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)


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
) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    pos = positions[["ticker", "post_trade_shares", "base_price", "base_value"]].copy()
    pos = pos[pos["post_trade_shares"] > 0].copy()

    if pos.empty:
        raise ValueError("No positive post-trade holdings found")

    invested_base = float(pos["base_value"].sum())
    cash = float(total_capital - invested_base)

    px = prices.merge(pos[["ticker", "post_trade_shares", "base_price", "base_value"]], on="ticker", how="inner")
    px = px[px["date"] >= pd.to_datetime(start_date)].copy()
    if end_date:
        px = px[px["date"] <= pd.to_datetime(end_date)].copy()

    if px.empty:
        min_dt = prices['date'].min() if len(prices) else None
        max_dt = prices['date'].max() if len(prices) else None
        raise ValueError(f"No daily prices available in requested date range: start={start_date} end={end_date}; available={min_dt}..{max_dt}")

    px["position_value"] = px["post_trade_shares"] * px["price"]

    daily = (
        px.groupby("date", as_index=False)["position_value"]
        .sum()
        .rename(columns={"position_value": "portfolio_value_ex_cash"})
    )
    daily["cash"] = cash
    daily["nav"] = daily["portfolio_value_ex_cash"] + daily["cash"]

    initial_nav = float(total_capital)
    daily["cum_return"] = daily["nav"] / initial_nav - 1.0
    daily["daily_return"] = daily["nav"].pct_change().fillna(daily["cum_return"])

    last_date = daily["date"].max()
    last_px = px[px["date"] == last_date].copy()
    contrib = last_px.copy()
    contrib["pnl"] = contrib["position_value"] - contrib["base_value"]
    contrib["contribution_to_total_return"] = contrib["pnl"] / initial_nav
    last_nav = float(daily.loc[daily["date"] == last_date, "nav"].iloc[0])
    contrib["weight_at_last_nav"] = contrib["position_value"] / last_nav
    contrib = contrib.sort_values("contribution_to_total_return", ascending=False)

    summary = {
        "start_date": str(pd.to_datetime(start_date).date()),
        "end_date": str(pd.to_datetime(last_date).date()),
        "initial_nav": initial_nav,
        "invested_base": invested_base,
        "cash_after_rebalance": cash,
        "last_nav": last_nav,
        "cum_return": float(daily["cum_return"].iloc[-1]),
        "num_positions": int(len(pos)),
    }
    return daily, contrib, summary


def maybe_load_benchmark(path: Optional[str], start_date: str, end_date: str) -> Optional[pd.DataFrame]:
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"benchmark file not found: {p}")

    if p.suffix.lower() == ".parquet":
        df = pd.read_parquet(p)
    else:
        df = pd.read_csv(p)

    cols = df.columns.tolist()
    date_col = pick_first_existing(cols, ["date", "Date", "dt", "ymd", "trd_date"])
    price_col = pick_first_existing(cols, ["close", "Close", "adj_close", "price", "index_level"])
    if date_col is None or price_col is None:
        raise ValueError(f"Could not detect benchmark date/price columns: {cols}")

    out = df[[date_col, price_col]].copy()
    out = out.rename(columns={date_col: "date", price_col: "benchmark_price"})
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["benchmark_price"] = pd.to_numeric(out["benchmark_price"], errors="coerce")
    out = out.dropna().sort_values("date")
    out = out[(out["date"] >= pd.to_datetime(start_date)) & (out["date"] <= pd.to_datetime(end_date))].copy()
    if out.empty:
        return None
    base = float(out["benchmark_price"].iloc[0])
    out["benchmark_cum_return"] = out["benchmark_price"] / base - 1.0
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execution_plan", required=True)
    ap.add_argument("--prices_daily", required=True)
    ap.add_argument("--total_capital", required=True, type=float)
    ap.add_argument("--target_date", required=True, help="rebalance target date, e.g. 2026-03-31")
    ap.add_argument("--end_date", default=None, help="performance end date; default = latest available")
    ap.add_argument("--benchmark", default=None, help="optional benchmark csv/parquet with date+price")
    ap.add_argument("--output_dir", required=True)
    ap.add_argument("--tag", default=None, help="optional tag for output filenames")
    args = ap.parse_args()

    if args.end_date is not None and pd.to_datetime(args.end_date) < pd.to_datetime(args.target_date):
        raise ValueError(f"end_date must be >= target_date: target_date={args.target_date} end_date={args.end_date}")

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    tag = args.tag or f"target={args.target_date}"

    exec_df = load_execution_plan(args.execution_plan)
    prices = load_prices_daily(args.prices_daily)

    base_px = latest_price_on_or_before(prices, args.target_date)
    positions = build_post_rebalance_positions(exec_df, base_px)

    daily, contrib, summary = calc_portfolio_timeseries(
        positions=positions,
        prices=prices,
        total_capital=args.total_capital,
        start_date=args.target_date,
        end_date=args.end_date,
    )

    bm = maybe_load_benchmark(args.benchmark, summary["start_date"], summary["end_date"])
    if bm is not None:
        daily = daily.merge(bm[["date", "benchmark_cum_return"]], on="date", how="left")
        if pd.notna(daily["benchmark_cum_return"]).any():
            last_bm = daily["benchmark_cum_return"].dropna().iloc[-1]
            summary["benchmark_cum_return"] = float(last_bm)
            summary["active_return"] = float(summary["cum_return"] - last_bm)

    positions_path = out_dir / f"live_positions__{tag}.csv"
    daily_path = out_dir / f"live_performance_daily__{tag}.csv"
    contrib_path = out_dir / f"live_contribution__{tag}.csv"
    summary_path = out_dir / f"live_performance_summary__{tag}.json"

    positions.to_csv(positions_path, index=False, encoding="utf-8-sig")
    daily.to_csv(daily_path, index=False, encoding="utf-8-sig")
    contrib.to_csv(contrib_path, index=False, encoding="utf-8-sig")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] positions : {positions_path}")
    print(f"[OK] daily     : {daily_path}")
    print(f"[OK] contrib   : {contrib_path}")
    print(f"[OK] summary   : {summary_path}")

    print("\n[SUMMARY]")
    for k, v in summary.items():
        print(f"{k}: {v}")


if __name__ == "__main__":
    main()
