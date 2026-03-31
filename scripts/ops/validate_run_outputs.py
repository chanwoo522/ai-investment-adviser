from __future__ import annotations

import argparse
from pathlib import Path
import pandas as pd


def check_file(path: Path, name: str):
    if not path.exists():
        raise FileNotFoundError(f"[FAIL] {name} not found: {path}")
    print(f"[OK] {name}: {path}")


def check_actions(actions_path: Path):
    df = pd.read_csv(actions_path)

    if "action" not in df.columns:
        raise ValueError("[FAIL] actions missing 'action' column")

    counts = df["action"].value_counts().to_dict()
    print(f"[INFO] action counts: {counts}")

    if len(df) == 0:
        raise ValueError("[FAIL] actions empty")

    return df


def check_execution_plan(exec_path: Path, total_amount: int):
    df = pd.read_csv(exec_path)

    required_cols = {"target_value", "trade_value", "planned_total_value", "unplanned_value"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"[FAIL] execution_plan missing columns: {sorted(missing)}")

    if len(df) == 0:
        raise ValueError("[FAIL] execution_plan empty")

    target_sum = pd.to_numeric(df["target_value"], errors="coerce").fillna(0).sum()
    trade_abs_sum = pd.to_numeric(df["trade_value"], errors="coerce").fillna(0).abs().sum()
    planned_sum = pd.to_numeric(df["planned_total_value"], errors="coerce").fillna(0).sum()
    unplanned_sum = pd.to_numeric(df["unplanned_value"], errors="coerce").fillna(0).sum()

    print(f"[INFO] target_value total      : {target_sum}")
    print(f"[INFO] abs(trade_value) total : {trade_abs_sum}")
    print(f"[INFO] planned_total_value    : {planned_sum}")
    print(f"[INFO] unplanned_value total  : {unplanned_sum}")
    print(f"[INFO] reference NAV          : {total_amount}")

    # 목표 포트 총액은 NAV 근처여야 함
    if abs(target_sum - total_amount) > max(total_amount * 0.05, 1):
        raise ValueError(
            f"[FAIL] target_value total mismatch: got={target_sum}, expected~={total_amount}"
        )

    # 실제 계획 주문금액은 총 거래필요금액(abs trade)을 넘으면 안 됨
    if planned_sum - trade_abs_sum > max(trade_abs_sum * 0.01, 1):
        raise ValueError(
            f"[FAIL] planned_total_value exceeds abs(trade_value): planned={planned_sum}, trade_abs={trade_abs_sum}"
        )

    print("[OK] execution_plan valid")


def check_holdings(holdings_path: Path):
    df = pd.read_csv(holdings_path, dtype={"ticker": str})

    required = {"ticker", "name", "shares"}
    missing = required - set(df.columns)

    if missing:
        raise ValueError(f"[FAIL] holdings missing columns: {missing}")

    df["ticker"] = (
        df["ticker"]
        .astype(str)
        .str.extract(r"(\d+)", expand=False)
        .str.zfill(6)
    )

    if df["ticker"].isna().any():
        raise ValueError("[FAIL] holdings has NaN ticker")

    if (df["ticker"].str.len() != 6).any():
        bad = df.loc[df["ticker"].str.len() != 6, ["ticker", "name"]]
        raise ValueError(f"[FAIL] ticker format invalid\n{bad.to_string(index=False)}")

    df["shares"] = pd.to_numeric(df["shares"], errors="coerce")
    if df["shares"].isna().any():
        bad = df.loc[df["shares"].isna(), ["ticker", "name", "shares"]]
        raise ValueError(f"[FAIL] holdings has invalid shares\n{bad.to_string(index=False)}")

    print("[OK] holdings valid")


def main():
    ap = argparse.ArgumentParser()

    ap.add_argument("--asof", required=True)
    ap.add_argument("--target_date", required=True)
    ap.add_argument("--metric", required=True)
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--feat_v", required=True)
    ap.add_argument("--action_v", required=True)
    ap.add_argument("--report_v", required=True)
    ap.add_argument("--total_amount", type=int, required=True)
    ap.add_argument("--holdings_csv", required=True)

    args = ap.parse_args()

    base = Path("data")

    # ===== 핵심 파일 경로 =====
    features_live = base / "features" / "features_live" / f"features_live__asof={args.asof}__metric={args.metric}__v={args.feat_v}.parquet"

    scores_full = base / "live" / "scores" / f"latest_scores__asof={args.asof}__metric={args.metric}__strat={args.strategy}__featv={args.feat_v}__target={args.target_date}__full_universe.csv"

    actions = base / "live" / "actions" / f"live_actions__asof={args.asof}__metric={args.metric}__strat={args.strategy}__target={args.target_date}__v={args.action_v}.csv"

    execution_plan = base / "processed" / f"execution_plan__total={args.total_amount}__v={args.report_v}.csv"

    report_md = base / "live" / "reports" / f"rebalance_report__asof={args.asof}__target={args.target_date}__metric={args.metric}__strat={args.strategy}__v={args.report_v}.md"

    report_html = base / "live" / "reports" / f"rebalance_report__asof={args.asof}__target={args.target_date}__metric={args.metric}__strat={args.strategy}__v={args.report_v}.html"

    report_detail = base / "live" / "reports" / f"rebalance_report_detail__asof={args.asof}__target={args.target_date}__metric={args.metric}__strat={args.strategy}__v={args.report_v}.csv"

    holdings = Path(args.holdings_csv)

    print("\n=== VALIDATION START ===\n")

    # ===== 존재 체크 =====
    check_file(features_live, "features_live")
    check_file(scores_full, "latest_scores")
    check_file(actions, "actions")
    check_file(execution_plan, "execution_plan")
    check_file(report_md, "report_md")
    check_file(report_html, "report_html")
    check_file(report_detail, "report_detail")
    check_file(holdings, "holdings")

    # ===== 내용 검증 =====
    check_actions(actions)
    check_execution_plan(execution_plan, args.total_amount)
    check_holdings(holdings)

    print("\n=== ALL CHECKS PASSED ===\n")

if __name__ == "__main__":
    main()