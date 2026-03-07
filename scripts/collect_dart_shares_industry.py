# scripts/collect_dart_shares_industry.py
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd
import requests
import OpenDartReader as ODR


PRO = Path("data/processed")


def _tag(asof: str) -> str:
    return asof.replace("-", "")


def _load_universe(asof: str, metric: Optional[str] = None) -> pd.DataFrame:
    """
    Load universe tickers. Prefer metric-tagged universe, else any universe for that asof.
    """
    cands = []
    if metric:
        cands += sorted(PRO.glob(f"universe__asof={asof}__metric={metric}__v=*.csv"))
        cands += sorted(PRO.glob(f"universe__asof={asof}__metric={metric}__v=*.parquet"))
    cands += sorted(PRO.glob(f"universe__asof={asof}__*.csv"))
    cands += sorted(PRO.glob(f"universe__asof={asof}__*.parquet"))
    if not cands:
        raise FileNotFoundError(f"universe not found for asof={asof} in {PRO}")

    p = cands[-1]
    df = pd.read_csv(p) if p.suffix.lower() == ".csv" else pd.read_parquet(p)
    if "ticker" not in df.columns:
        raise ValueError(f"universe missing ticker col. file={p} cols={list(df.columns)}")
    df["ticker"] = df["ticker"].astype(str).str.zfill(6)
    return df[["ticker"]].drop_duplicates()


def _corp_map_from_odr(dart: ODR) -> pd.DataFrame:
    """
    OpenDartReader version compatibility:
    - some versions: dart.corp_codes is a DataFrame attribute
    - other versions: dart.corp_codes() is a function returning DataFrame
    """
    corp_obj = getattr(dart, "corp_codes", None)
    if corp_obj is None:
        raise RuntimeError("OpenDartReader has no corp_codes attribute/function.")

    corp_df = corp_obj() if callable(corp_obj) else corp_obj
    if not isinstance(corp_df, pd.DataFrame):
        raise RuntimeError(f"corp_codes returned non-DataFrame: {type(corp_df)}")

    need = {"corp_code", "stock_code"}
    if not need.issubset(set(corp_df.columns)):
        raise ValueError(f"Unexpected corp_codes schema: cols={list(corp_df.columns)}")

    out = corp_df[["corp_code", "stock_code"]].copy()
    if "corp_name" in corp_df.columns:
        out["name"] = corp_df["corp_name"]
    else:
        out["name"] = None

    out = out.rename(columns={"stock_code": "ticker"})
    out["ticker"] = out["ticker"].astype(str).str.zfill(6)
    out["corp_code"] = out["corp_code"].astype(str)
    return out


def _retry_get_json(url: str, params: dict, tries: int = 3, sleep_s: float = 0.6) -> dict:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            time.sleep(sleep_s * (2 ** i))
    raise RuntimeError(f"GET failed: {url} last_err={last}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)              # YYYY-MM-DD
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--bsns_year", type=int, default=None)  # default: asof_year-1
    ap.add_argument("--reprt_code", default="11011")        # 사업보고서(연간)
    ap.add_argument("--sleep", type=float, default=0.12)
    ap.add_argument("--out_v", type=int, required=True)
    ap.add_argument("--with_industry", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    key = os.getenv("DART_API_KEY")
    if not key:
        raise RuntimeError("DART_API_KEY env var not set. (PowerShell) $env:DART_API_KEY='...'; then rerun")

    asof = args.asof
    asof_year = int(_tag(asof)[:4])
    bsns_year = args.bsns_year if args.bsns_year is not None else (asof_year - 1)

    PRO.mkdir(parents=True, exist_ok=True)

    dart = ODR(key)
    uni = _load_universe(asof, metric=args.metric)

    cmap = _corp_map_from_odr(dart)
    df = uni.merge(cmap, on="ticker", how="left")
    df = df.dropna(subset=["corp_code"]).copy()

    if args.limit:
        df = df.head(args.limit)

    url_stock = "https://opendart.fss.or.kr/api/stockTotqySttus.json"
    url_comp = "https://opendart.fss.or.kr/api/company.json"

    rows = []
    for _, r in df.iterrows():
        corp_code = str(r["corp_code"])
        ticker = str(r["ticker"]).zfill(6)
        name = r.get("name")

        shares = None
        se_used = None
        induty_code = None

        # (A) shares
        try:
            js = _retry_get_json(
                url_stock,
                params={
                    "crtfc_key": key,
                    "corp_code": corp_code,
                    "bsns_year": str(bsns_year),
                    "reprt_code": args.reprt_code,
                },
                tries=3,
                sleep_s=0.6,
            )
            if js.get("status") == "000":
                lst = js.get("list", []) or []
                # prefer common stock
                for want in ["보통주", "합계", "우선주"]:
                    found = None
                    for it in lst:
                        if it.get("se") == want:
                            found = it
                            break
                    if found:
                        v = found.get("istc_totqy")
                        try:
                            shares = int(str(v).replace(",", "")) if v is not None else None
                            se_used = want if shares is not None else None
                            break
                        except Exception:
                            pass
        except Exception:
            # keep robust; leave shares as None
            pass

        # (B) industry (optional)
        if args.with_industry:
            try:
                jc = _retry_get_json(
                    url_comp,
                    params={"crtfc_key": key, "corp_code": corp_code},
                    tries=3,
                    sleep_s=0.6,
                )
                if jc.get("status") == "000":
                    induty_code = jc.get("induty_code") or jc.get("industy_code")
            except Exception:
                pass

        rows.append(
            {
                "ticker": ticker,
                "name": name,
                "corp_code": corp_code,
                "bsns_year": bsns_year,
                "reprt_code": args.reprt_code,
                "se": se_used,
                "shares": shares,
                "induty_code": induty_code,
            }
        )
        time.sleep(args.sleep)

    out = pd.DataFrame(rows)
    out["ticker"] = out["ticker"].astype(str).str.zfill(6)
    out["asof"] = asof
    out["created_at"] = datetime.now().isoformat(timespec="seconds")

    outp = PRO / f"shares_industry__asof={asof}__src=dart__y={bsns_year}__reprt={args.reprt_code}__v={args.out_v}.parquet"
    out.to_parquet(outp, index=False)

    print(f"[OK] saved: {outp}")
    print(f"[INFO] rows={len(out)} tickers={out['ticker'].nunique()} shares_null_ratio={out['shares'].isna().mean():.3f} induty_null_ratio={out['induty_code'].isna().mean():.3f}")


if __name__ == "__main__":
    main()