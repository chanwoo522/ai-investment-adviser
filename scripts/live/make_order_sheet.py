from __future__ import annotations

import argparse
from pathlib import Path
import pandas as pd
from common.industry_map import attach_industry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execution_plan", required=True)
    ap.add_argument("--day", required=True, choices=["1", "2", "3"])
    ap.add_argument("--output", required=True)
    args = ap.parse_args()

    df = pd.read_csv(args.execution_plan)
    qty_col = f"day{args.day}_qty"
    val_col = f"day{args.day}_order_value"

    if qty_col not in df.columns:
        raise ValueError(f"{qty_col} not found in execution plan")

    orders = df.loc[df[qty_col] > 0].copy()
    try:
        orders = attach_industry(orders, prefer_reference=True)
    except Exception as e:
        print(f"[WARN] attach_industry failed: {e}")

    keep = [
        "ticker",
        "name_final",
        "order_side",
        "action",
        "reason",
        "price",
        qty_col,
        val_col,
        "score",
        "score_rank",
        "industry_code",
        "industry_name",
        "industry4",
    ]
    keep = [c for c in keep if c in orders.columns]
    orders = orders[keep].copy()

    orders = orders.rename(columns={
        qty_col: "order_qty",
        val_col: "order_value",
    })

    orders["order_type"] = "MKT"
    orders["execution_day"] = int(args.day)

    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    orders.to_csv(args.output, index=False, encoding="utf-8-sig")
    print(f"[OK] saved: {args.output} rows={len(orders)}")

    print("\n[PREVIEW]")
    print(orders.head(30).to_string(index=False))


if __name__ == "__main__":
    main()