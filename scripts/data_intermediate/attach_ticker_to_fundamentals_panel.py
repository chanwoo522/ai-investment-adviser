import pandas as pd
from pathlib import Path
import argparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", required=True)
    parser.add_argument("--in_v", type=int, default=2)
    parser.add_argument("--out_v", type=int, default=3)
    args = parser.parse_args()

    panel_path = Path(
        f"data/intermediate/fundamentals_panel/"
        f"fundamentals_panel__asof={args.asof}__src=dart_raw_canonical__v={args.in_v}.parquet"
    )

    map_path = Path(
        f"data/intermediate/corpcode_ticker_map__asof={args.asof}.parquet"
    )

    if not panel_path.exists():
        raise FileNotFoundError(panel_path)
    if not map_path.exists():
        raise FileNotFoundError(map_path)

    df = pd.read_parquet(panel_path)
    map_df = pd.read_parquet(map_path)

    print(f"[INFO] panel rows: {len(df)}")
    print(f"[INFO] map rows: {len(map_df)}")

    # merge
    df = df.merge(map_df, on="corp_code", how="left")

    # 매핑 성공률 체크
    matched = df["ticker"].notnull().sum()
    print(f"[INFO] matched ticker: {matched}/{len(df)}")

    # 정렬
    sort_cols = ["ticker", "year", "quarter"]
    sort_cols = [c for c in sort_cols if c in df.columns]
    if sort_cols:
        df = df.sort_values(sort_cols).reset_index(drop=True)

    out_path = Path(
        f"data/intermediate/fundamentals_panel/"
        f"fundamentals_panel__asof={args.asof}__src=dart_raw_canonical_ticker__v={args.out_v}.parquet"
    )

    df.to_parquet(out_path, index=False)

    print(f"[OK] saved: {out_path}")

    # 추가 확인
    print("\n[CHECK]")
    print("unique tickers:", df["ticker"].nunique(dropna=True))
    print("missing ticker rows:", df["ticker"].isnull().sum())


if __name__ == "__main__":
    main()