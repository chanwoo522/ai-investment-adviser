from __future__ import annotations
import argparse
import pandas as pd
from pathlib import Path
from _utils import FEAT, PRO, parquet_path, log_path, write_log

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    args = ap.parse_args()
    asof = args.asof
    lp = log_path("report_snapshot", asof)

    feat_p = FEAT / f"features_phase1__asof={asof}__src=phase1__v=1.parquet"
    if not feat_p.exists():
        write_log("[WARN] features not found; run build_features_phase1.py", lp)
        return

    df = pd.read_parquet(feat_p).sort_values("total_score", ascending=False)

    # Top lists
    top20 = df.head(20).copy()

    # Classys ticker = 214150 (오빠가 쓰는 코드가 다를 수 있어, 필요 시 수정)
    classys = df[df["ticker"] == "214150"].copy()

    out_csv = PRO / f"snapshot_report__asof={asof}.csv"
    top20.to_csv(out_csv, index=False, encoding="utf-8-sig")
    write_log(f"[OK] Top20 saved: {out_csv}", lp)

    if not classys.empty:
        write_log(f"[INFO] CLASSYS found. rank={classys.index[0]+1} total={classys['total_score'].iloc[0]:.3f}", lp)
    else:
        write_log("[INFO] CLASSYS not found in universe/features (maybe filtered out or ticker mismatch).", lp)

if __name__ == "__main__":
    main()
