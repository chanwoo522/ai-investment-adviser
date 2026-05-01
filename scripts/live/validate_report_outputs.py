from __future__ import annotations

import sys
from pathlib import Path
import pandas as pd

if len(sys.argv) < 4:
    print("usage: python validate_report_outputs.py <report_detail.csv> <latest_scores_full_universe.csv> <ticker>")
    sys.exit(2)

detail_path = Path(sys.argv[1])
scores_path = Path(sys.argv[2])
ticker = str(sys.argv[3]).zfill(6)

detail = pd.read_csv(detail_path, dtype={"ticker": str})
scores = pd.read_csv(scores_path, dtype={"ticker": str})

detail["ticker"] = detail["ticker"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
scores["ticker"] = scores["ticker"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)

d = detail.loc[detail["ticker"].eq(ticker)].copy()
s = scores.loc[scores["ticker"].eq(ticker)].copy()

print(f"[ticker] {ticker}")
print(f"[detail rows] {len(d)}")
print(f"[score rows] {len(s)}")

check_cols = [
    "score", "score_exec", "score_model", "score_base", "score_adj",
    "ai_overlay_active",
    "ai_pred_alpha__profit_accel", "ai_pred_alpha__revenue_support", "ai_pred_alpha__balance_sheet",
    "ai_dyn_share__profit_accel", "ai_dyn_share__revenue_support", "ai_dyn_share__balance_sheet",
    "ai_bucket_mult__profit_accel", "ai_bucket_mult__revenue_support", "ai_bucket_mult__balance_sheet",
    "OpIncome_acc2_log1p__ai_mult", "Revenue_acc2__ai_mult", "Debt_to_Equity_log__ai_mult",
    "period_q2", "period_q1", "period_q0",
    "revenue_q2", "revenue_q1", "revenue_q0",
    "op_q2", "op_q1", "op_q0",
]

print("\n[detail]")
if len(d):
    print(d[[c for c in check_cols if c in d.columns]].to_string(index=False))
else:
    print("missing")

print("\n[scores]")
if len(s):
    print(s[[c for c in check_cols if c in s.columns]].head(1).to_string(index=False))
else:
    print("missing")

print("\n[global AI status]")
if "ai_overlay_active" in scores.columns:
    print(scores["ai_overlay_active"].value_counts(dropna=False).to_string())
else:
    print("scores file has no ai_overlay_active")
