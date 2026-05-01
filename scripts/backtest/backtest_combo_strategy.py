from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

_THIS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _THIS_DIR.parent
_REPO_ROOT = _SCRIPTS_DIR.parent
for _p in (_THIS_DIR, _SCRIPTS_DIR, _REPO_ROOT):
    _ps = str(_p)
    if _ps not in sys.path:
        sys.path.insert(0, _ps)

from common.combo_strategy import (
    ComboBacktestSummary,
    build_combo_tickers,
    calc_cagr_from_nav,
    calc_mdd_from_nav,
    calc_sharpe,
    calc_turnover,
    normalize_ticker_series,
    portfolio_month_return,
)


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _load_factor_picks(path: Path, rebalance_col: str) -> pd.DataFrame:
    df = pd.read_parquet(path).copy()
    if "ticker" not in df.columns:
        raise ValueError("factor picks parquet must contain ticker")
    if rebalance_col not in df.columns:
        if rebalance_col == "rebalance_month_end" and "rebalance_month" in df.columns:
            rebalance_col = "rebalance_month"
        else:
            raise ValueError(f"factor picks parquet must contain {rebalance_col}")
    df["ticker"] = normalize_ticker_series(df["ticker"])
    df[rebalance_col] = pd.to_datetime(df[rebalance_col], errors="coerce")
    order_cols = [c for c in ["score_adj", "score", "score_rank", "ticker"] if c in df.columns]
    ascending = [False if c in {"score_adj", "score"} else True for c in order_cols]
    if order_cols:
        df = df.sort_values(order_cols, ascending=ascending, na_position="last").reset_index(drop=True)
    out = df.dropna(subset=["ticker", rebalance_col]).copy()
    if rebalance_col != "rebalance_month_end":
        out["rebalance_month_end"] = out[rebalance_col]
    return out


def _load_ai_holdings(path: Path, portfolio_name: str) -> pd.DataFrame:
    df = pd.read_csv(path).copy()
    if "ticker" not in df.columns or "rebalance_month" not in df.columns:
        raise ValueError("ai holdings csv must contain ticker and rebalance_month")
    if "portfolio" in df.columns:
        df = df.loc[df["portfolio"].astype(str) == str(portfolio_name)].copy()
    df["ticker"] = normalize_ticker_series(df["ticker"])
    df["rebalance_month"] = pd.to_datetime(df["rebalance_month"], errors="coerce")
    order_cols = [c for c in ["rank", "score_ai", "score_baseline", "ticker"] if c in df.columns]
    ascending = [True if c == "rank" or c == "ticker" else False for c in order_cols]
    if order_cols:
        df = df.sort_values(order_cols, ascending=ascending, na_position="last").reset_index(drop=True)
    return df.dropna(subset=["ticker", "rebalance_month"]).copy()


def _load_returns(path: Path) -> pd.DataFrame:
    ret = pd.read_parquet(path).copy()
    if "month_end" not in ret.columns and "month" in ret.columns:
        ret["month_end"] = pd.to_datetime(ret["month"], errors="coerce").dt.to_period("M").dt.to_timestamp("M")
    else:
        ret["month_end"] = pd.to_datetime(ret["month_end"], errors="coerce")
    ret["ticker"] = normalize_ticker_series(ret["ticker"])
    ret["ret_1m"] = pd.to_numeric(ret["ret_1m"], errors="coerce").fillna(0.0)
    return ret.dropna(subset=["ticker", "month_end"]).copy()


def main() -> None:
    ap = argparse.ArgumentParser(description="Backtest combo strategy from factor-composite picks and AI WF holdings.")
    ap.add_argument("--factor_picks_path", required=True)
    ap.add_argument("--ai_holdings_path", required=True)
    ap.add_argument("--returns_path", required=True)
    ap.add_argument("--out_dir", required=True)
    ap.add_argument("--variant", default="overlay_replace", choices=["overlay_replace", "intersection_priority", "union_equal"])
    ap.add_argument("--portfolio_name", default="ai")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--rebalance_col", default="rebalance_month_end")
    ap.add_argument("--tcost_bps", type=float, default=30.0)
    args = ap.parse_args()

    factor_picks = _load_factor_picks(Path(args.factor_picks_path), args.rebalance_col)
    ai_holdings = _load_ai_holdings(Path(args.ai_holdings_path), args.portfolio_name)
    ret = _load_returns(Path(args.returns_path))

    base_month_col = args.rebalance_col
    ai_month_col = "rebalance_month"
    rb_base = sorted(pd.to_datetime(factor_picks[base_month_col]).dropna().unique())
    rb_ai = set(pd.to_datetime(ai_holdings[ai_month_col]).dropna().unique())
    rb_months = [pd.Timestamp(x) for x in rb_base if pd.Timestamp(x) in rb_ai]
    rb_months = sorted(rb_months)
    if len(rb_months) < 2:
        raise RuntimeError("Need at least two overlapping rebalance months between factor picks and ai holdings.")

    out_dir = Path(args.out_dir)
    _ensure_dir(out_dir)

    monthly_rows: list[dict[str, object]] = []
    rebalance_rows: list[dict[str, object]] = []
    holdings_rows: list[dict[str, object]] = []

    prev_combo: list[str] = []
    nav_net = 1.0

    for idx, rb in enumerate(rb_months[:-1]):
        next_rb = pd.Timestamp(rb_months[idx + 1])
        base_seg = factor_picks.loc[factor_picks[base_month_col] == rb].copy()
        ai_seg = ai_holdings.loc[ai_holdings[ai_month_col] == rb].copy()
        if len(base_seg) == 0 or len(ai_seg) == 0:
            continue

        base_tickers = base_seg["ticker"].astype(str).tolist()
        ai_tickers = ai_seg["ticker"].astype(str).tolist()
        combo_tickers = build_combo_tickers(base_tickers, ai_tickers, k=int(args.k), variant=args.variant)
        if not combo_tickers:
            continue

        overlap = [tk for tk in combo_tickers if tk in set(base_tickers) and tk in set(ai_tickers)]
        turnover = calc_turnover(prev_combo, combo_tickers)

        rebalance_rows.append(
            {
                "rebalance_month": rb,
                "next_rebalance_month": next_rb,
                "variant": args.variant,
                "base_count": len(base_tickers),
                "ai_count": len(ai_tickers),
                "combo_count": len(combo_tickers),
                "overlap_count": len(overlap),
                "overlap_ratio": float(len(overlap) / max(len(combo_tickers), 1)),
                "turnover": turnover,
                "combo_tickers": ",".join(combo_tickers),
                "base_tickers": ",".join(base_tickers[: int(args.k)]),
                "ai_tickers": ",".join(ai_tickers[: int(args.k)]),
            }
        )

        for rank, tk in enumerate(combo_tickers, start=1):
            holdings_rows.append(
                {
                    "rebalance_month": rb,
                    "portfolio": args.variant,
                    "rank": rank,
                    "ticker": tk,
                    "in_base": int(tk in set(base_tickers)),
                    "in_ai": int(tk in set(ai_tickers)),
                }
            )

        monthly_slice = sorted(x for x in ret["month_end"].dropna().unique() if pd.Timestamp(rb) <= pd.Timestamp(x) < pd.Timestamp(next_rb))
        for month_idx, month_end in enumerate(monthly_slice):
            month_end = pd.Timestamp(month_end)
            gross_ret = portfolio_month_return(ret, combo_tickers, month_end)
            net_ret = float(gross_ret) - (float(args.tcost_bps) / 10000.0 if month_idx == 0 else 0.0)
            nav_net *= (1.0 + net_ret)
            monthly_rows.append(
                {
                    "month_end": month_end,
                    "rebalance_month": rb,
                    "variant": args.variant,
                    "combo_month_ret_gross": gross_ret,
                    "combo_month_ret_net": net_ret,
                    "combo_nav_net": nav_net,
                }
            )
        prev_combo = combo_tickers

    monthly = pd.DataFrame(monthly_rows)
    rebalance = pd.DataFrame(rebalance_rows)
    holdings = pd.DataFrame(holdings_rows)
    if monthly.empty:
        raise RuntimeError("Combo monthly result is empty.")

    summary = ComboBacktestSummary(
        strategy_variant=f"combo_{args.variant}",
        periods=int(len(rebalance)),
        months=int(len(monthly)),
        final_nav_net=float(monthly["combo_nav_net"].iloc[-1]),
        cagr_net=calc_cagr_from_nav(monthly["combo_nav_net"]),
        sharpe_net=calc_sharpe(monthly["combo_month_ret_net"]),
        mdd_net=calc_mdd_from_nav(monthly["combo_nav_net"]),
        avg_turnover=float(pd.to_numeric(rebalance["turnover"], errors="coerce").mean()) if not rebalance.empty else float("nan"),
        avg_overlap_ratio=float(pd.to_numeric(rebalance["overlap_ratio"], errors="coerce").mean()) if not rebalance.empty else float("nan"),
    )

    monthly_path = out_dir / "combo_monthly_nav.csv"
    rebalance_path = out_dir / "combo_rebalance_detail.csv"
    holdings_path = out_dir / "combo_holdings.csv"
    summary_path = out_dir / "summary.json"

    monthly.to_csv(monthly_path, index=False, encoding="utf-8-sig")
    rebalance.to_csv(rebalance_path, index=False, encoding="utf-8-sig")
    holdings.to_csv(holdings_path, index=False, encoding="utf-8-sig")
    summary_path.write_text(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] monthly  : {monthly_path}")
    print(f"[OK] rebalance: {rebalance_path}")
    print(f"[OK] holdings : {holdings_path}")
    print(f"[OK] summary  : {summary_path}")


if __name__ == "__main__":
    main()
