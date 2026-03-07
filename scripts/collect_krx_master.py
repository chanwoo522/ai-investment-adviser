#!/usr/bin/env python
from __future__ import annotations

# ---- path bootstrap (schema import 안정화) ----
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# ---------------------------------------------

import argparse
from datetime import datetime

import pandas as pd

from schema import TICKER_COL, NAME_COL
from krx_safe import to_ymd, backoff_date_call


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True, help="YYYY-MM-DD")
    ap.add_argument("--out_v", type=int, default=1)
    ap.add_argument("--src", default="pykrx", help="label in filename (default: pykrx)")
    ap.add_argument("--max_back_days", type=int, default=14)
    ap.add_argument("--soft_fail", action="store_true", help="If KRX call fails, save minimal fallback and exit 0.")
    ap.add_argument("--fallback_from", choices=["universe", "none"], default="universe",
                    help="When soft_fail: build minimal ticker list from universe csv if exists.")
    args = ap.parse_args()

    out = Path("data/processed") / f"krx_master__asof={args.asof}__src={args.src}__v={args.out_v}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)

    # If already exists, do nothing (repro safety)
    if out.exists():
        print("[OK] exists:", out)
        return

    asof_ymd = to_ymd(args.asof)

    try:
        # Delay import to avoid hard dependency if env differs
        from pykrx import stock  # type: ignore

        def _tickers(d: str) -> pd.DataFrame:
            t = stock.get_market_ticker_list(d, market="ALL")
            return pd.DataFrame({TICKER_COL: [str(x) for x in t]})

        used_ymd, df_t = backoff_date_call(
            _tickers,
            asof_ymd,
            max_back_days=int(args.max_back_days),
            require_cols={TICKER_COL},
        )

        tickers = df_t[TICKER_COL].astype(str).tolist()

        names = []
        for t in tickers:
            try:
                names.append(stock.get_market_ticker_name(t))
            except Exception:
                names.append(None)

        master = pd.DataFrame(
            {
                TICKER_COL: tickers,
                NAME_COL: names,
                "asof_ymd": used_ymd,
                "created_at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        master[TICKER_COL] = master[TICKER_COL].astype(str)
        master.to_parquet(out, index=False)
        print("[OK] master:", out)
        print(f"[INFO] used_ymd={used_ymd} rows={len(master)} tickers={master[TICKER_COL].nunique()}")
        return

    except Exception as e:
        # Hard fail by default (원래 엄격 모드)
        if not args.soft_fail:
            raise

        print(f"[WARN] KRX master collection failed (soft_fail enabled): {e}")

        # Fallback: build minimal ticker list
        tickers = None
        if args.fallback_from == "universe":
            p_uni = Path("data/processed") / f"universe__asof={args.asof}__metric=revenue_op__v=1.csv"
            if p_uni.exists():
                try:
                    uni = pd.read_csv(p_uni, dtype={TICKER_COL: str})
                    if TICKER_COL in uni.columns:
                        tickers = sorted(uni[TICKER_COL].dropna().astype(str).unique().tolist())
                        print(f"[WARN] fallback tickers from universe: {p_uni} (n={len(tickers)})")
                except Exception as e2:
                    print(f"[WARN] universe fallback failed: {e2}")

        if tickers is None:
            tickers = []

        master = pd.DataFrame(
            {
                TICKER_COL: tickers,
                NAME_COL: [None] * len(tickers),
                "asof_ymd": None,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "notes": "fallback(master): KRX call empty/failed",
            }
        )
        master.to_parquet(out, index=False)
        print("[OK] master fallback saved:", out)
        print(f"[INFO] rows={len(master)} tickers={len(tickers)}")
        return


if __name__ == "__main__":
    main()