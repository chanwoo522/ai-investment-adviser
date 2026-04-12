# >>> ACTIVE_FALLBACK (USED BY prepare_asof - DO NOT REMOVE)
#!/usr/bin/env python
from __future__ import annotations

import argparse
import re
from pathlib import Path
import pandas as pd

# ---- path bootstrap (schema import ?덉젙?? ----
import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# ---------------------------------------------

from schema import RETURNS_MONTHLY_COLS, TICKER_COL, MONTH_COL


def _extract_asof_from_name(name: str) -> str | None:
    m = re.search(r"__asof=(\d{4}-\d{2}-\d{2})__", name)
    return m.group(1) if m else None


def _find_prices_daily_path(asof: str, metric: str, px_v: int) -> Path:
    root = Path("data/processed")
    exact = root / f"prices_daily__src=pykrx__start=20160101__asof={asof}__metric={metric}__v={px_v}.parquet"
    if exact.exists():
        return exact

    cands = sorted(root.glob(f"prices_daily__src=pykrx__start=20160101__asof=*__metric={metric}__v={px_v}.parquet"))
    valid = []
    for p in cands:
        a = _extract_asof_from_name(p.name)
        if a and a <= asof:
            valid.append((a, p))

    if not valid:
        raise FileNotFoundError(exact)

    valid.sort(key=lambda x: x[0], reverse=True)
    print(f"[WARN] exact prices_daily not found. Using latest <= asof: {valid[0][1]}")
    return valid[0][1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--px_v", type=int, default=1)
    ap.add_argument("--out_v", type=int, default=1)
    ap.add_argument("--start", default=None)
    args = ap.parse_args()

    asof = args.asof
    metric = args.metric
    px_v = args.px_v
    out_v = args.out_v

    p_px = _find_prices_daily_path(asof, metric, px_v)

    px = pd.read_parquet(p_px)

    # normalize common variants -> ticker/date/close
    col_map = {}
    if "ticker" not in px.columns:
        for c in ["code", "醫낅ぉ肄붾뱶"]:
            if c in px.columns:
                col_map[c] = "ticker"
                break
    if "date" not in px.columns:
        for c in ["Date", "trade_date", "TRD_DD", "?좎쭨"]:
            if c in px.columns:
                col_map[c] = "date"
                break
    if "close" not in px.columns:
        for c in ["Close", "adj_close", "Adj Close", "醫낃?", "醫낃?(??"]:
            if c in px.columns:
                col_map[c] = "close"
                break
    if col_map:
        px = px.rename(columns=col_map)

    missing = [c for c in ["ticker", "date", "close"] if c not in px.columns]
    if missing:
        raise ValueError(f"prices_daily must include {missing}. cols={list(px.columns)[:30]}")

    px["ticker"] = px["ticker"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
    px["date"] = pd.to_datetime(px["date"])
    px = px.sort_values(["ticker", "date"])
    px["month"] = px["date"].dt.to_period("M").dt.to_timestamp("M")

    # month-end close per ticker
    last = px.groupby(["ticker", "month"], as_index=False).tail(1)[["ticker", "month", "close"]].copy()
    last = last.sort_values(["ticker", "month"])
    last["ret"] = last.groupby("ticker")["close"].pct_change()
    out = last.dropna(subset=["ret"]).reset_index(drop=True)

    # ---- schema normalize: month_end + ret_1m ----
    out = out.rename(columns={"month": MONTH_COL, "ret": "ret_1m"})
    out[MONTH_COL] = pd.to_datetime(out[MONTH_COL]).dt.normalize()
    out["ret_1m"] = pd.to_numeric(out["ret_1m"], errors="coerce")

    out = out[RETURNS_MONTHLY_COLS].dropna(subset=["ret_1m"]).reset_index(drop=True)

    if list(out.columns) != RETURNS_MONTHLY_COLS:
        raise ValueError(f"returns_monthly schema drifted. got={list(out.columns)} expected={RETURNS_MONTHLY_COLS}")

    p_out = Path("data/processed") / f"returns_monthly__src=pykrx__asof={asof}__metric={metric}__v={out_v}.parquet"
    out.to_parquet(p_out, index=False)
    print("[OK] saved:", p_out)
    print("rows:", len(out), "tickers:", out[TICKER_COL].nunique())
    print("months:", out[MONTH_COL].min(), "to", out[MONTH_COL].max())


if __name__ == "__main__":
    main()
