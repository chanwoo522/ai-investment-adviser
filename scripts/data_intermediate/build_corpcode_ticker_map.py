import pandas as pd
from pathlib import Path
import argparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--asof", required=True)
    parser.add_argument("--metric", default="revenue_op")
    parser.add_argument("--feat_v", type=int, default=10)
    args = parser.parse_args()

    feat_path = Path(
        f"data/processed/features_live__asof={args.asof}__metric={args.metric}__v={args.feat_v}.parquet"
    )

    if not feat_path.exists():
        raise FileNotFoundError(f"features file not found: {feat_path}")

    df = pd.read_parquet(feat_path)
    print(f"[INFO] features rows: {len(df)}")

    # 핵심 컬럼 체크
    required = ["corp_code", "ticker"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"Missing columns in features: {missing}")

    # 매핑 생성
    map_df = (
        df[["corp_code", "ticker"]]
        .dropna()
        .drop_duplicates()
        .copy()
    )

    print(f"[INFO] unique mappings: {len(map_df)}")

    # corp_code 중복 체크
    dup = map_df.duplicated(subset=["corp_code"]).sum()
    print(f"[INFO] duplicated corp_code: {dup}")

    out_path = Path(
        f"data/intermediate/corpcode_ticker_map__asof={args.asof}.parquet"
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)

    map_df.to_parquet(out_path, index=False)

    print(f"[OK] saved: {out_path}")


if __name__ == "__main__":
    main()
    