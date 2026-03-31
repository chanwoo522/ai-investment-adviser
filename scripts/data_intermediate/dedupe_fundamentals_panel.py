import pandas as pd
from pathlib import Path
import argparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", required=True)
    parser.add_argument("--in_v", type=int, default=1)
    parser.add_argument("--out_v", type=int, default=2)
    args = parser.parse_args()

    in_path = Path(
        f"data/intermediate/fundamentals_panel/"
        f"fundamentals_panel__asof={args.asof}__src=dart_raw__v={args.in_v}.parquet"
    )

    out_path = Path(
        f"data/intermediate/fundamentals_panel/"
        f"fundamentals_panel__asof={args.asof}__src=dart_raw_canonical__v={args.out_v}.parquet"
    )

    df = pd.read_parquet(in_path)
    print(f"[INFO] loaded rows: {len(df)}")

    if df.empty:
        raise ValueError("Input panel is empty")

    key_cols = ["corp_code", "year", "reprt_code", "fs"]
    missing = [c for c in key_cols + ["asof"] if c not in df.columns]
    if missing:
        raise ValueError(f"Missing required columns: {missing}")

    # asof를 날짜형으로 변환
    df["asof_dt"] = pd.to_datetime(df["asof"], errors="coerce")

    before_dupe = len(df)

    # 같은 corp_code/year/reprt_code/fs 내에서 가장 최신 asof만 남김
    df = (
        df.sort_values(key_cols + ["asof_dt"])
          .drop_duplicates(subset=key_cols, keep="last")
          .copy()
    )

    after_dupe = len(df)

    # 보조 컬럼 정리
    df = df.drop(columns=["asof_dt"], errors="ignore")

    # 저장 전 정렬
    sort_cols = [c for c in ["corp_code", "year", "quarter", "reprt_code", "fs"] if c in df.columns]
    if sort_cols:
        df = df.sort_values(sort_cols).reset_index(drop=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out_path, index=False)

    print(f"[OK] saved: {out_path}")
    print(f"[INFO] rows before dedupe: {before_dupe}")
    print(f"[INFO] rows after  dedupe: {after_dupe}")
    print(f"[INFO] removed rows: {before_dupe - after_dupe}")
    print(f"[INFO] corp_codes: {df['corp_code'].nunique(dropna=True)}")

    if "year" in df.columns and len(df):
        print(f"[INFO] years: {df['year'].min()} -> {df['year'].max()}")
    if "quarter" in df.columns and len(df):
        qs = sorted(df["quarter"].dropna().unique().tolist())
        print(f"[INFO] quarters: {qs}")


if __name__ == "__main__":
    main()