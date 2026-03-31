from __future__ import annotations
from pathlib import Path
import pandas as pd
from common.industry_map import load_industry_reference, attach_industry

REF = Path("data/reference/krx_industry_code_name_by_ticker.csv")

def norm(s):
    return s.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)

def main():
    ref = load_industry_reference(REF)
    print(f"[REF] rows={len(ref)} unique_ticker={ref['ticker'].nunique()} null_name={ref['industry_name'].isna().sum()}")
    print(ref.head(10).to_string(index=False))

    sample = Path("data/processed/shares_industry__asof=2026-03-29__src=dart__y=2025__reprt=11011__v=1.parquet")
    if sample.exists():
        df = pd.read_parquet(sample)
        if "ticker" in df.columns:
            df["ticker"] = norm(df["ticker"])
            df = attach_industry(df, reference_csv=REF, prefer_reference=True)
            focus = df[df["ticker"].isin(["005930","041920"])][["ticker","industry_code","industry_name"]].drop_duplicates()
            print("\n[FOCUS]")
            print(focus.to_string(index=False))
    else:
        print(f"[WARN] sample parquet not found: {sample}")

if __name__ == "__main__":
    main()
