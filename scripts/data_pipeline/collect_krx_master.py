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
import re

import pandas as pd

from schema import TICKER_COL, NAME_COL
from krx_safe import to_ymd, backoff_date_call


def normalize_ticker_series(s: pd.Series) -> pd.Series:
    return s.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)


def _pick_first_existing(cols, candidates):
    cols = set(cols)
    for c in candidates:
        if c in cols:
            return c
    return None


def _read_any_table(p: Path) -> pd.DataFrame:
    if p.suffix.lower() == ".csv":
        return pd.read_csv(p)
    if p.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(p)
    return pd.read_parquet(p)


def _name_map_from_df(df: pd.DataFrame) -> pd.DataFrame:
    tcol = _pick_first_existing(df.columns, ["ticker", "code", "종목코드", "symbol", TICKER_COL])
    ncol = _pick_first_existing(df.columns, ["name", "종목명", "company_name", "corp_name", "short_name", "한글종목명", NAME_COL])
    if tcol is None:
        return pd.DataFrame(columns=[TICKER_COL, NAME_COL])

    out = df.copy()
    out[TICKER_COL] = normalize_ticker_series(out[tcol])

    if ncol is not None:
        out[NAME_COL] = out[ncol].astype("string")
    else:
        out[NAME_COL] = pd.NA

    out = out[[TICKER_COL, NAME_COL]].dropna(subset=[TICKER_COL]).drop_duplicates(TICKER_COL)
    return out


def _extract_asof_from_name(name: str) -> str | None:
    m = re.search(r"__asof=(\d{4}-\d{2}-\d{2})__", name)
    return m.group(1) if m else None


def _source_priority(name: str) -> int:
    # 낮을수록 우선
    if "krx_master__" in name:
        return 0
    if "universe__" in name:
        return 1
    if "features_live__" in name:
        return 2
    if "features_phase1__" in name:
        return 3
    if "krx_marketdata__" in name:
        return 4
    return 9


def _fallback_master_from_local(asof: str) -> tuple[pd.DataFrame, Path | None]:
    """
    Build a minimal-but-usable master from local artifacts.
    Priority:
      1) exact asof
      2) latest <= asof
      3) nearest > asof
    Within same asof bucket, prefer:
      krx_master > universe > features_live > features_phase1 > krx_marketdata
    """
    root = Path("data/processed")
    candidates: list[Path] = []

    patterns = [
        "krx_master__asof=*__src=pykrx__v=*.parquet",
        "krx_master__asof=*__src=pykrx__v=*.csv",
        "universe__asof=*__*.parquet",
        "universe__asof=*__*.csv",
        "features_live__asof=*__*.parquet",
        "features_phase1__asof=*__*.parquet",
        "features_phase1__asof=*__*.csv",
        "krx_marketdata__asof=*__*.parquet",
    ]

    for pat in patterns:
        candidates.extend(sorted(root.glob(pat)))

    scored: list[tuple[int, str, int, str, Path]] = []
    for p in candidates:
        a = _extract_asof_from_name(p.name)
        if a is None:
            continue

        if a == asof:
            bucket = 0
        elif a < asof:
            bucket = 1
        else:
            bucket = 2

        src_pri = _source_priority(p.name)
        scored.append((bucket, a, -src_pri, p.name, p))

    # exact asof 우선, 그 다음 latest <= asof, 마지막으로 nearest future
    left = sorted([x for x in scored if x[0] == 0], key=lambda x: (x[1], x[2], x[3]), reverse=True)
    mid = sorted([x for x in scored if x[0] == 1], key=lambda x: (x[1], x[2], x[3]), reverse=True)
    right = sorted([x for x in scored if x[0] == 2], key=lambda x: (x[1], x[2], x[3]), reverse=False)

    ordered = left + mid + right

    seen = set()
    for _, _, _, _, p in ordered:
        if p in seen:
            continue
        seen.add(p)
        try:
            df = _read_any_table(p)
            out = _name_map_from_df(df)
            if len(out) > 0:
                return out, p
        except Exception as e:
            print(f"[WARN] failed reading fallback source {p}: {e}")
            continue

    return pd.DataFrame(columns=[TICKER_COL, NAME_COL]), None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True, help="YYYY-MM-DD")
    ap.add_argument("--out_v", type=int, default=1)
    ap.add_argument("--src", default="pykrx", help="label in filename (default: pykrx)")
    ap.add_argument("--max_back_days", type=int, default=14)
    args = ap.parse_args()

    out = Path("data/processed") / f"krx_master__asof={args.asof}__src={args.src}__v={args.out_v}.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)

    if out.exists():
        print("[OK] exists:", out)
        return

    asof_ymd = to_ymd(args.asof)

    try:
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

        tickers = normalize_ticker_series(df_t[TICKER_COL]).dropna().tolist()

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
                "notes": "primary(pykrx)",
            }
        )
        master[TICKER_COL] = master[TICKER_COL].astype(str)
        master = master.drop_duplicates(TICKER_COL)
        master.to_parquet(out, index=False)
        print("[OK] master:", out)
        print(f"[INFO] used_ymd={used_ymd} rows={len(master)} tickers={master[TICKER_COL].nunique()}")
        return

    except Exception as e:
        print(f"[WARN] KRX master collection failed, switching to local fallback: {e}")

        master, src = _fallback_master_from_local(args.asof)
        if len(master) == 0:
            raise RuntimeError(
                f"KRX master collection failed and no usable local fallback source was found for asof={args.asof}"
            ) from e

        print(f"[WARN] local fallback master source used: {src} rows={len(master)}")

        master["asof_ymd"] = pd.NA
        master["created_at"] = datetime.now().isoformat(timespec="seconds")
        master["notes"] = f"fallback(local nearest): {src.name if src else 'unknown'}"
        master = master[[TICKER_COL, NAME_COL, "asof_ymd", "created_at", "notes"]].drop_duplicates(TICKER_COL)

        out.parent.mkdir(parents=True, exist_ok=True)
        master.to_parquet(out, index=False)
        print("[OK] master fallback saved:", out)
        print(f"[INFO] rows={len(master)} tickers={master[TICKER_COL].nunique()}")
        return


if __name__ == "__main__":
    main()