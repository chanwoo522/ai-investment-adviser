from __future__ import annotations

import argparse
from pathlib import Path
import pandas as pd


def normalize_ticker(s: pd.Series) -> pd.Series:
    return s.astype(str).str.extract(r"(\d+)")[0].str.zfill(6)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--execution_plan", required=True)
    ap.add_argument("--fills_csv", required=True)
    ap.add_argument("--holdings_csv", required=True)
    ap.add_argument("--output_holdings", required=True)
    ap.add_argument("--output_fills_merged", required=True)
    args = ap.parse_args()

    exec_path = Path(args.execution_plan)
    fills_path = Path(args.fills_csv)
    hold_path = Path(args.holdings_csv)

    if not exec_path.exists():
        raise FileNotFoundError(f"execution_plan not found: {exec_path}")
    if not fills_path.exists():
        raise FileNotFoundError(f"fills_csv not found: {fills_path}")
    if not hold_path.exists():
        raise FileNotFoundError(f"holdings_csv not found: {hold_path}")

    plan = pd.read_csv(exec_path)
    fills = pd.read_csv(fills_path)
    hold = pd.read_csv(hold_path)

    for df, name in [(plan, "execution_plan"), (fills, "fills_csv"), (hold, "holdings_csv")]:
        if "ticker" not in df.columns:
            raise ValueError(f"{name} must contain 'ticker'")

    if "order_side" not in plan.columns:
        raise ValueError("execution_plan must contain 'order_side'")

    if "name_final" not in plan.columns:
        plan["name_final"] = pd.NA

    if "name" not in hold.columns:
        hold["name"] = pd.NA
    if "shares" not in hold.columns:
        raise ValueError("holdings_csv must contain 'shares'")

    if "filled_qty" not in fills.columns or "filled_price" not in fills.columns:
        raise ValueError("fills_csv must contain 'filled_qty' and 'filled_price'")

    plan["ticker"] = normalize_ticker(plan["ticker"])
    fills["ticker"] = normalize_ticker(fills["ticker"])
    hold["ticker"] = normalize_ticker(hold["ticker"])

    hold["shares"] = pd.to_numeric(hold["shares"], errors="coerce").fillna(0)
    fills["filled_qty"] = pd.to_numeric(fills["filled_qty"], errors="coerce").fillna(0)
    fills["filled_price"] = pd.to_numeric(fills["filled_price"], errors="coerce")

    base = plan[["ticker", "name_final", "order_side"]].drop_duplicates("ticker", keep="last").copy()
    merged = base.merge(fills, on="ticker", how="left")

    merged["filled_qty"] = pd.to_numeric(merged["filled_qty"], errors="coerce").fillna(0)
    merged["signed_fill_qty"] = merged["filled_qty"]

    merged.loc[merged["order_side"] == "SELL", "signed_fill_qty"] = -merged.loc[
        merged["order_side"] == "SELL", "filled_qty"
    ]

    hold2 = hold.merge(
        merged[["ticker", "filled_qty", "filled_price", "signed_fill_qty"]],
        on="ticker",
        how="outer",
    )

    # 이름 보강
    name_map = base[["ticker", "name_final"]].drop_duplicates("ticker", keep="last")
    hold2 = hold2.merge(name_map, on="ticker", how="left")

    hold2["name"] = hold2["name"].fillna(hold2["name_final"])
    hold2["shares"] = pd.to_numeric(hold2["shares"], errors="coerce").fillna(0)
    hold2["signed_fill_qty"] = pd.to_numeric(hold2["signed_fill_qty"], errors="coerce").fillna(0)

    hold2["shares_after"] = hold2["shares"] + hold2["signed_fill_qty"]
    hold2["shares_after"] = hold2["shares_after"].clip(lower=0)

    # 0주 종목은 제거
    out_hold = hold2.loc[hold2["shares_after"] > 0, ["ticker", "name", "shares_after"]].copy()
    out_hold = out_hold.rename(columns={"shares_after": "shares"})
    out_hold = out_hold.sort_values("ticker").reset_index(drop=True)

    # fill 결과 기록용
    out_fill = hold2[[
        "ticker", "name", "shares", "filled_qty", "filled_price", "signed_fill_qty", "shares_after"
    ]].copy()
    out_fill = out_fill.sort_values("ticker").reset_index(drop=True)

    Path(args.output_holdings).parent.mkdir(parents=True, exist_ok=True)
    out_hold.to_csv(args.output_holdings, index=False, encoding="utf-8-sig")
    out_fill.to_csv(args.output_fills_merged, index=False, encoding="utf-8-sig")

    print(f"[OK] saved holdings: {args.output_holdings}")
    print(f"[OK] saved fills merged: {args.output_fills_merged}")

    print("\n[UPDATED HOLDINGS]")
    print(out_hold.to_string(index=False))

    print("\n[FILL MERGED PREVIEW]")
    print(out_fill.head(30).to_string(index=False))


if __name__ == "__main__":
    main()