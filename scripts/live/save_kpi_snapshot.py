#!/usr/bin/env python
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd


def _load_summary(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"performance summary not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_actions(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"actions csv not found: {path}")
    df = pd.read_csv(path)
    if "ticker" in df.columns:
        df["ticker"] = df["ticker"].astype(str).str.extract(r"(\d+)")[0].str.zfill(6)
    return df


def _load_execution_plan(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"execution plan not found: {path}")
    return pd.read_csv(path)


def _calc_turnover(exec_df: pd.DataFrame, total_capital: float) -> float:
    if "trade_value" not in exec_df.columns or total_capital <= 0:
        return float("nan")
    trade = pd.to_numeric(exec_df["trade_value"], errors="coerce").fillna(0.0).abs()
    return float(trade.sum() / (2.0 * float(total_capital)))


def _calc_topk_keep_rate(actions_df: pd.DataFrame) -> float:
    if "is_target" not in actions_df.columns:
        return float("nan")
    is_target = pd.to_numeric(actions_df["is_target"], errors="coerce").fillna(0).astype(int)
    is_current = pd.to_numeric(actions_df.get("is_current", 0), errors="coerce").fillna(0).astype(int)
    denom = int(is_target.sum())
    if denom <= 0:
        return float("nan")
    numer = int(((is_target == 1) & (is_current == 1)).sum())
    return float(numer / denom)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--target", required=True)
    ap.add_argument("--metric", required=True)
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--total_capital", required=True, type=float)
    ap.add_argument("--performance_summary", required=True)
    ap.add_argument("--actions_csv", required=True)
    ap.add_argument("--execution_plan", required=True)
    ap.add_argument("--output_csv", required=True)
    ap.add_argument("--provisional", action="store_true")
    ap.add_argument("--provisional_source", default="")
    args = ap.parse_args()

    summary = _load_summary(Path(args.performance_summary))
    actions_df = _load_actions(Path(args.actions_csv))
    exec_df = _load_execution_plan(Path(args.execution_plan))

    row = {
        "asof": args.asof,
        "target": args.target,
        "metric": args.metric,
        "strategy": args.strategy,
        "nav": float(summary.get("last_nav")) if summary.get("last_nav") is not None else float("nan"),
        "cum_return": float(summary.get("cum_return")) if summary.get("cum_return") is not None else float("nan"),
        "benchmark_return": float(summary.get("benchmark_cum_return")) if summary.get("benchmark_cum_return") is not None else float("nan"),
        "active_return": float(summary.get("active_return")) if summary.get("active_return") is not None else float("nan"),
        "turnover": _calc_turnover(exec_df, args.total_capital),
        "topk_keep_rate": _calc_topk_keep_rate(actions_df),
        "provisional": bool(args.provisional),
        "provisional_source": args.provisional_source or "",
    }

    out_path = Path(args.output_csv)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame([row]).to_csv(out_path, index=False, encoding="utf-8-sig")

    print(f"[OK] kpi snapshot: {out_path}")
    for k, v in row.items():
        print(f"[INFO] {k}: {v}")


if __name__ == "__main__":
    main()
