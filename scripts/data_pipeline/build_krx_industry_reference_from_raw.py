from __future__ import annotations

import argparse
from pathlib import Path
import pandas as pd


def _pick_col(cols: list[str], candidates: list[str]) -> str | None:
    norm = {str(c).strip(): c for c in cols}
    for cand in candidates:
        if cand in norm:
            return norm[cand]
    return None


def load_raw_table(path: Path) -> pd.DataFrame:
    suf = path.suffix.lower()
    if suf in [".xlsx", ".xls"]:
        return pd.read_excel(path)
    if suf in [".csv", ".txt"]:
        return pd.read_csv(path)
    raise ValueError(f"unsupported raw file type: {path.suffix}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, help="KRX raw file path supplied manually by user")
    ap.add_argument(
        "--out",
        default="reference/krx_industry_code_name_by_ticker_latest.csv",
        help="canonical output csv path",
    )
    args = ap.parse_args()

    raw_path = Path(args.raw)
    if not raw_path.exists():
        raise FileNotFoundError(f"raw file not found: {raw_path}")

    df = load_raw_table(raw_path)
    cols = list(df.columns)

    ticker_col = _pick_col(cols, ["종목코드", "ticker"])
    code_col = _pick_col(cols, ["업종코드", "industry_code"])
    name_col = _pick_col(cols, ["업종명", "industry_name"])

    if not ticker_col or not code_col or not name_col:
        raise ValueError(
            f"required columns missing. got={cols}, need one of "
            f"ticker=['종목코드','ticker'], code=['업종코드','industry_code'], name=['업종명','industry_name']"
        )

    out = df[[ticker_col, code_col, name_col]].copy()
    out.columns = ["ticker", "industry_code", "industry_name"]

    out["ticker"] = (
        out["ticker"]
        .astype(str)
        .str.extract(r"(\d+)")[0]
        .str.zfill(6)
    )
    out["industry_code"] = (
        out["industry_code"]
        .astype(str)
        .str.extract(r"(\d+)")[0]
        .str.zfill(6)
    )
    out["industry_name"] = out["industry_name"].astype(str).str.strip()

    out = out.dropna(subset=["ticker", "industry_code", "industry_name"])
    out = out.drop_duplicates(subset=["ticker"], keep="last").copy()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False, encoding="utf-8-sig")

    print(f"[OK] saved canonical industry reference: {out_path}")
    print(f"[INFO] rows={len(out)}")
    print(out.head(10).to_string(index=False))


if __name__ == "__main__":
    main()