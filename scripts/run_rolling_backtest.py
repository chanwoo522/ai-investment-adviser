import pandas as pd
from pathlib import Path

ASOF = "2026-02-18"
DATA = Path("data")

def main():
    # 실제 생성되는 파일명 규칙에 맞춤
    feat_path = DATA / "features" / f"features_phase1__asof={ASOF}__src=phase1__v=1.parquet"

    if not feat_path.exists():
        print("Features file not found:", feat_path)
        return

    df = pd.read_parquet(feat_path)
    print("Loaded features:", feat_path)
    print("Rows:", len(df))
    print("Cols:", list(df.columns)[:30], "..." if len(df.columns) > 30 else "")

    if len(df) == 0:
        print("[WARN] features rows=0. Universe filtering likely too strict or merge mismatch.")
        return

    # TODO: 이후 core 파이프라인 연결 (일단 파일 로딩 성공부터 확인)
    print(df.head(5).to_string(index=False))

if __name__ == "__main__":
    main()
