from __future__ import annotations

import argparse
from pathlib import Path
import pandas as pd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    args = ap.parse_args()
    asof = args.asof

    # universe 경로(우리가 실제로 쓰는 파일명 그대로)
    uni_path = Path(rf".\data\processed\universe__asof={asof}__src=phase1__mcapTop=0.7__trdBot=0.3__capQ=4__v=1.parquet")
    if not uni_path.exists():
        raise FileNotFoundError(f"Universe not found: {uni_path}")

    uni = pd.read_parquet(uni_path)

    # ticker 정규화
    uni["ticker"] = (
        uni["ticker"].astype(str)
        .str.strip()
        .str.replace(r"\.0$", "", regex=True)
        .str.zfill(6)
    )

    # ✅ industry4 생성 (industry_code 기반)
    if "industry_code" in uni.columns and not uni["industry_code"].isna().all():
        code = (
            uni["industry_code"].astype(str)
            .str.replace(r"\.0$", "", regex=True)
            .str.strip()
            .str.replace("nan", "", regex=False)
        )
        # 길이가 4보다 짧으면 zfill로 채우고 4자리 절단
        uni["industry4"] = code.str.zfill(4).str.slice(0, 4)
        uni.loc[uni["industry4"].isin(["", "0000"]), "industry4"] = None
    else:
        # industry_code가 없으면 그대로 None (근데 이 경우 universe 단계부터 수정해야 함)
        uni["industry4"] = None

    # runner가 찾는 파일명 규칙과 정확히 일치시키기
    out_dir = Path(r".\data\features")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"features_phase1__asof={asof}__src=phase1__v=1.parquet"

    feats = uni[["ticker", "name", "market", "industry4", "cap_bucket"]].copy()

    print("[CHECK] features rows:", len(feats))
    print("[CHECK] industry4 null ratio:", feats["industry4"].isna().mean())
    print(feats.head(5).to_string(index=False))

    feats.to_parquet(out_path, index=False)
    print("[OK] saved:", out_path)


if __name__ == "__main__":
    main()
