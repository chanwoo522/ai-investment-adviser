#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# ---- path bootstrap ----
_THIS_DIR = os.path.abspath(os.path.dirname(__file__))
_SCRIPTS_DIR = os.path.abspath(os.path.join(_THIS_DIR, ".."))
_REPO_ROOT = os.path.abspath(os.path.join(_THIS_DIR, "..", ".."))
_BACKTEST_DIR = os.path.join(_SCRIPTS_DIR, "backtest")
for _p in (_THIS_DIR, _SCRIPTS_DIR, _BACKTEST_DIR, _REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

try:
    from backtest_quarterly_rebalance_v2 import (
        normalize_ticker_series,
        load_strategy,
        load_strategy_runtime,
        apply_filters,
        standardize_factor,
        safe_fill_for_z,
    )
except ModuleNotFoundError:
    from backtest.backtest_quarterly_rebalance_v2 import (
        normalize_ticker_series,
        load_strategy,
        load_strategy_runtime,
        apply_filters,
        standardize_factor,
        safe_fill_for_z,
    )

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
PROCESSED = DATA / "processed"
FEATURES_DIR = DATA / "features" / "features_live"
AI_FILTER_DIR = DATA / "ai_filter" / "datasets"

DEFAULT_BENCHMARK_TICKER = "069500"   # KODEX 200
DEFAULT_BENCHMARK_NAME = "KODEX200_PROXY"


def _extract_asof_from_name(name: str) -> str | None:
    m = re.search(r"__asof=(\d{4}-\d{2}-\d{2})__", name)
    return m.group(1) if m else None


def _extract_v_from_name(name: str) -> int | None:
    m = re.search(r"__v=(\d+)\.(parquet|csv)$", name)
    return int(m.group(1)) if m else None


def pick_features_path(asof: str, metric: str, feat_v: int) -> Path:
    candidates = [
        FEATURES_DIR / f"features_live__asof={asof}__metric={metric}__v={feat_v}.parquet",
        PROCESSED / f"features_live__asof={asof}__metric={metric}__v={feat_v}.parquet",
    ]
    for p in candidates:
        if p.exists():
            return p

    # fallback: latest <= asof with same v
    fallback_candidates: list[tuple[str, Path]] = []
    for base in [FEATURES_DIR, PROCESSED]:
        for p in base.glob(f"features_live__asof=*__metric={metric}__v={feat_v}.parquet"):
            a = _extract_asof_from_name(p.name)
            if a and a <= asof:
                fallback_candidates.append((a, p))

    if not fallback_candidates:
        raise FileNotFoundError(
            "features_live not found.\n"
            f"tried exact: {[str(p) for p in candidates]}\n"
            f"and no latest <= {asof} for metric={metric}, v={feat_v}"
        )

    fallback_candidates.sort(key=lambda x: x[0])
    picked = fallback_candidates[-1][1]
    print(f"[WARN] no exact features_live for asof={asof}; using latest <= asof: {picked.name}")
    return picked


def pick_processed_prices_path(asof: str, metric: str, px_v: int) -> Path:
    exact = PROCESSED / f"prices_daily__src=pykrx__start=20160101__asof={asof}__metric={metric}__v={px_v}.parquet"
    if exact.exists():
        return exact

    cands = sorted(PROCESSED.glob(f"prices_daily__src=pykrx__start=20160101__asof=*__metric={metric}__v={px_v}.parquet"))
    eligible: list[tuple[str, Path]] = []
    for p in cands:
        a = _extract_asof_from_name(p.name)
        if a and a <= asof:
            eligible.append((a, p))

    if not eligible:
        raise FileNotFoundError(
            f"processed prices_daily parquet not found for asof<={asof}, metric={metric}, v={px_v}"
        )

    eligible.sort(key=lambda x: x[0])
    picked = eligible[-1][1]
    print(f"[WARN] no exact processed prices for asof={asof}; using latest <= asof: {picked.name}")
    return picked


def pick_raw_prices_path(asof: str, raw_v: int) -> Path:
    search_bases = [
        DATA / "raw",
        DATA / "interim",
        PROCESSED,
        DATA,
    ]

    patterns = [
        f"prices_raw__src=pykrx__start=*__asof={asof}__freq=d__v={raw_v}.parquet",
        f"prices_raw__src=pykrx__start=*__asof=*__freq=d__v={raw_v}.parquet",
    ]

    # exact first
    for base in search_bases:
        if not base.exists():
            continue
        for pat in [patterns[0]]:
            hits = sorted(base.rglob(pat))
            if hits:
                return hits[-1]

    # fallback latest <= asof
    eligible: list[tuple[str, Path]] = []
    for base in search_bases:
        if not base.exists():
            continue
        for p in base.rglob(patterns[1]):
            a = _extract_asof_from_name(p.name)
            if a and a <= asof:
                eligible.append((a, p))

    if not eligible:
        raise FileNotFoundError(
            f"raw prices parquet not found for asof<={asof}, raw_v={raw_v}. "
            f"Expected something like prices_raw__src=pykrx__start=*__asof=*__freq=d__v={raw_v}.parquet"
        )

    eligible.sort(key=lambda x: x[0])
    picked = eligible[-1][1]
    print(f"[WARN] no exact raw prices for asof={asof}; using latest <= asof: {picked.name}")
    return picked


def load_prices_any(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"price file not found: {path}")

    if path.suffix.lower() == ".parquet":
        df = pd.read_parquet(path)
    elif path.suffix.lower() in {".csv", ".txt"}:
        df = pd.read_csv(path)
    else:
        raise ValueError(f"unsupported price file type: {path}")

    cols = list(df.columns)
    ticker_col = next((c for c in ["ticker", "code", "종목코드", "symbol"] if c in cols), None)
    date_col = next((c for c in ["date", "Date", "dt", "ymd", "trd_date"] if c in cols), None)
    price_col = next((c for c in ["close", "Close", "adj_close", "price"] if c in cols), None)

    if ticker_col is None or date_col is None or price_col is None:
        raise ValueError(f"Could not detect ticker/date/price columns from: {cols}")

    out = df[[ticker_col, date_col, price_col]].copy()
    out = out.rename(columns={ticker_col: "ticker", date_col: "date", price_col: "price"})
    out["ticker"] = normalize_ticker_series(out["ticker"])
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    out["price"] = pd.to_numeric(out["price"], errors="coerce")
    out = out.dropna(subset=["ticker", "date", "price"]).copy()
    out = out.sort_values(["ticker", "date"]).reset_index(drop=True)
    return out


def get_last_price_on_or_before(px: pd.DataFrame, dt: pd.Timestamp) -> pd.DataFrame:
    sub = px.loc[px["date"] <= dt, ["ticker", "date", "price"]].copy()
    if len(sub) == 0:
        return pd.DataFrame(columns=["ticker", "price", "price_date"])
    sub = sub.sort_values(["ticker", "date"]).groupby("ticker", as_index=False).tail(1)
    sub = sub.rename(columns={"date": "price_date"})
    return sub[["ticker", "price", "price_date"]].copy()


def get_last_benchmark_on_or_before(px: pd.DataFrame, dt: pd.Timestamp, benchmark_ticker: str) -> tuple[float | None, pd.Timestamp | None]:
    sub = px.loc[(px["ticker"] == benchmark_ticker) & (px["date"] <= dt), ["date", "price"]].copy()
    if len(sub) == 0:
        return None, None
    row = sub.sort_values("date").tail(1).iloc[0]
    return float(row["price"]), pd.Timestamp(row["date"])


def ensure_alias_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    alias_pairs = [
        ("op_cur_q", ["op_cur_q", "OpIncome_cur_q", "op_income_cur_q", "operating_income_cur_q"]),
        ("OpIncome_ttm", ["OpIncome_ttm", "op_income", "op_income_ttm", "operating_income_ttm"]),
        ("traded_value", ["traded_value", "거래대금", "trading_value"]),
        ("mcap", ["mcap", "market_cap", "시가총액"]),
        ("op_qoq", ["op_qoq", "OpIncome_qoq", "op_income_qoq", "operating_income_qoq"]),
    ]
    for dst, cands in alias_pairs:
        if dst in out.columns:
            continue
        for src in cands:
            if src in out.columns:
                out[dst] = out[src]
                break
    return out


def apply_simple_clip(z: pd.Series, clip_z: float | None = None) -> pd.Series:
    z = pd.to_numeric(z, errors="coerce").replace([pd.NA, np.inf, -np.inf], pd.NA).fillna(0.0).astype("float64")
    if clip_z is None or clip_z < 0:
        return z
    return z.clip(lower=-float(clip_z), upper=float(clip_z)).astype("float64")


def make_historical_score(
    g: pd.DataFrame,
    weights: dict[str, float],
    filters: dict,
    clip_z: float | None,
    raw_factors: list[str],
) -> pd.DataFrame:
    full = g.copy()
    full["passed_filters"] = False
    full["score_raw"] = np.nan

    filtered = apply_filters(g.copy(), filters, verbose=False)

    if "op_qoq" in filtered.columns:
        op_qoq_num = pd.to_numeric(filtered["op_qoq"], errors="coerce")
        strict_qoq = filtered.loc[op_qoq_num.notna() & (op_qoq_num > 0)].copy()
        if len(strict_qoq) > 0:
            filtered = strict_qoq

    if len(filtered) == 0:
        return full

    gg = filtered.copy()
    gg["score_raw"] = 0.0
    raw_factors = set(raw_factors or [])

    for c, ww in weights.items():
        ww = float(ww)
        if ww == 0.0 or c not in gg.columns:
            continue

        s = pd.to_numeric(gg[c], errors="coerce")
        if c in raw_factors:
            z = s.fillna(0.0).astype("float64")
        else:
            z = standardize_factor(safe_fill_for_z(s), use_robust_z=False, clip_z=None)
            z = apply_simple_clip(z, clip_z=clip_z)

        gg["score_raw"] += ww * z

    passed_set = set(gg["ticker"].astype(str).tolist())
    full.loc[full["ticker"].astype(str).isin(passed_set), "passed_filters"] = True
    full = full.merge(
        gg[["ticker", "score_raw"]].drop_duplicates("ticker", keep="last"),
        on="ticker",
        how="left",
        suffixes=("", "_new"),
    )
    if "score_raw_new" in full.columns:
        full["score_raw"] = full["score_raw_new"].combine_first(full["score_raw"])
        full = full.drop(columns=["score_raw_new"])

    return full


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--feat_v", type=int, required=True)
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--px_v", type=int, default=1)
    ap.add_argument("--raw_px_v", type=int, default=1)
    ap.add_argument("--benchmark_ticker", default=DEFAULT_BENCHMARK_TICKER)
    ap.add_argument("--benchmark_name", default=DEFAULT_BENCHMARK_NAME)
    ap.add_argument("--label3_q", type=float, default=0.70)
    ap.add_argument("--min_history_quarters", type=int, default=4)
    ap.add_argument("--out_path", default="")
    args = ap.parse_args()

    benchmark_ticker = str(args.benchmark_ticker).zfill(6)

    feat_path = pick_features_path(args.asof, args.metric, args.feat_v)
    stock_price_path = pick_processed_prices_path(args.asof, args.metric, args.px_v)
    raw_price_path = pick_raw_prices_path(args.asof, args.raw_px_v)

    feat = pd.read_parquet(feat_path).copy()
    feat["ticker"] = normalize_ticker_series(feat["ticker"])
    feat["rebalance_month"] = pd.to_datetime(feat["rebalance_month"], errors="coerce")
    feat = ensure_alias_columns(feat)
    feat = feat.dropna(subset=["ticker", "rebalance_month"]).copy()

    if "history_quarters" in feat.columns:
        feat["history_quarters"] = pd.to_numeric(feat["history_quarters"], errors="coerce")
        feat = feat.loc[feat["history_quarters"].fillna(0) >= int(args.min_history_quarters)].copy()

    strat_path = ROOT / "configs" / "strategies.yaml"
    if not strat_path.exists():
        strat_path = ROOT / "strategies.yaml"
    if not strat_path.exists():
        raise FileNotFoundError("strategies.yaml not found in configs/ or project root.")

    weights, filters, _desc = load_strategy(strat_path, args.strategy)
    runtime_cfg = load_strategy_runtime(strat_path, args.strategy)
    clip_z = runtime_cfg.get("clip_z", 5.0)
    raw_factors = list(runtime_cfg.get("raw_factors", [])) if isinstance(runtime_cfg, dict) else []
    if not raw_factors:
        raw_factors = [c for c in weights.keys() if c in {"op_growth_streak2", "rev_growth_streak2"}]

    stock_px = load_prices_any(stock_price_path)
    raw_px = load_prices_any(raw_price_path)

    if not (raw_px["ticker"] == benchmark_ticker).any():
        raise RuntimeError(
            f"benchmark_ticker={benchmark_ticker} not found in raw prices file: {raw_price_path}\n"
            f"Need benchmark ETF price in full raw market prices."
        )

    rb_list = sorted(pd.to_datetime(feat["rebalance_month"]).dropna().unique())
    if len(rb_list) < 2:
        raise RuntimeError("Need at least 2 rebalance dates to build forward labels.")

    rows = []
    for i in range(len(rb_list) - 1):
        rb_t = pd.Timestamp(rb_list[i])
        rb_t1 = pd.Timestamp(rb_list[i + 1])

        g = feat.loc[feat["rebalance_month"] == rb_t].copy()
        if len(g) == 0:
            continue

        g = g.loc[g["ticker"] != benchmark_ticker].copy()
        if len(g) == 0:
            continue

        g = make_historical_score(
            g=g,
            weights=weights,
            filters=filters,
            clip_z=clip_z,
            raw_factors=raw_factors,
        )

        px_t = get_last_price_on_or_before(stock_px, rb_t)
        px_t1 = get_last_price_on_or_before(stock_px, rb_t1)

        g = g.merge(
            px_t.rename(columns={"price": "entry_price", "price_date": "entry_price_date"}),
            on="ticker",
            how="left",
        )
        g = g.merge(
            px_t1.rename(columns={"price": "exit_price", "price_date": "exit_price_date"}),
            on="ticker",
            how="left",
        )

        g["entry_price"] = pd.to_numeric(g["entry_price"], errors="coerce")
        g["exit_price"] = pd.to_numeric(g["exit_price"], errors="coerce")
        g["fwd_return"] = (g["exit_price"] / g["entry_price"]) - 1.0

        bench_t, bench_t_date = get_last_benchmark_on_or_before(raw_px, rb_t, benchmark_ticker)
        bench_t1, bench_t1_date = get_last_benchmark_on_or_before(raw_px, rb_t1, benchmark_ticker)

        if bench_t is None or bench_t1 is None or bench_t <= 0:
            bench_ret = np.nan
        else:
            bench_ret = (bench_t1 / bench_t) - 1.0

        g["benchmark_name"] = args.benchmark_name
        g["benchmark_ticker"] = benchmark_ticker
        g["benchmark_return"] = bench_ret
        g["benchmark_entry_date"] = bench_t_date
        g["benchmark_exit_date"] = bench_t1_date
        g["excess_return"] = pd.to_numeric(g["fwd_return"], errors="coerce") - pd.to_numeric(g["benchmark_return"], errors="coerce")

        g["label_l1"] = (pd.to_numeric(g["fwd_return"], errors="coerce") > 0).astype("Int64")
        g["label_l2"] = (pd.to_numeric(g["excess_return"], errors="coerce") > 0).astype("Int64")

        q = pd.to_numeric(g["fwd_return"], errors="coerce").quantile(float(args.label3_q))
        g["label_l3"] = (pd.to_numeric(g["fwd_return"], errors="coerce") >= q).astype("Int64")

        g["next_rebalance_month"] = rb_t1
        rows.append(g)

    if not rows:
        raise RuntimeError("No labeled rows were built.")

    ds = pd.concat(rows, ignore_index=True)
    ds = ds.sort_values(["rebalance_month", "ticker"]).reset_index(drop=True)

    AI_FILTER_DIR.mkdir(parents=True, exist_ok=True)
    out_path = Path(args.out_path) if args.out_path else AI_FILTER_DIR / (
        f"filter_dataset__asof={args.asof}__metric={args.metric}"
        f"__strat={args.strategy}__featv={args.feat_v}__pxv={args.px_v}"
        f"__rawpxv={args.raw_px_v}__bench={benchmark_ticker}.parquet"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ds.to_parquet(out_path, index=False)

    meta = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "asof": args.asof,
        "metric": args.metric,
        "strategy": args.strategy,
        "feat_v": int(args.feat_v),
        "px_v": int(args.px_v),
        "raw_px_v": int(args.raw_px_v),
        "features_path": str(feat_path),
        "stock_prices_path": str(stock_price_path),
        "raw_prices_path": str(raw_price_path),
        "benchmark_ticker": benchmark_ticker,
        "benchmark_name": args.benchmark_name,
        "rows": int(len(ds)),
        "tickers": int(ds["ticker"].nunique()),
        "rebalance_months": int(ds["rebalance_month"].nunique()),
        "label1_pos_rate": float(pd.to_numeric(ds["label_l1"], errors="coerce").mean()),
        "label2_pos_rate": float(pd.to_numeric(ds["label_l2"], errors="coerce").mean()),
        "label3_pos_rate": float(pd.to_numeric(ds["label_l3"], errors="coerce").mean()),
        "columns": list(map(str, ds.columns)),
    }
    meta_path = out_path.with_suffix(".meta.json")
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[OK] features        : {feat_path}")
    print(f"[OK] stock prices    : {stock_price_path}")
    print(f"[OK] raw prices      : {raw_price_path}")
    print(f"[OK] benchmark       : ticker={benchmark_ticker} name={args.benchmark_name}")
    print(f"[OK] dataset         : {out_path}")
    print(f"[OK] meta            : {meta_path}")
    print(f"[INFO] rows={len(ds)} tickers={ds['ticker'].nunique()} rebalance_months={ds['rebalance_month'].nunique()}")
    print(f"[INFO] label_l1 pos rate={meta['label1_pos_rate']:.4f}")
    print(f"[INFO] label_l2 pos rate={meta['label2_pos_rate']:.4f}")
    print(f"[INFO] label_l3 pos rate={meta['label3_pos_rate']:.4f}")


if __name__ == "__main__":
    main()