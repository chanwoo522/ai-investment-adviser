from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description="Wrapper to run 10Y rolling quarterly backtest with current D strategy logic.")
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--feat_v", type=int, required=True)
    ap.add_argument("--ret_v", type=int, required=True)
    ap.add_argument("--ret_src", default="pykrx")
    ap.add_argument("--strategy", default="D_quality_filter_debt_profitaccel_liq")
    ap.add_argument("--k", type=int, default=10)
    ap.add_argument("--out_v", type=int, default=9001)
    ap.add_argument("--tcost_bps", type=float, default=30.0)
    ap.add_argument("--max_per_group", type=int, default=0)
    ap.add_argument("--entry_gap", type=float, default=0.0)
    ap.add_argument("--filter_fallback", choices=["full", "error"], default="error")
    ap.add_argument("--save_holdings", action="store_true")
    ap.add_argument("--save_inputs", action="store_true")
    args = ap.parse_args()

    script = Path(__file__).resolve().parent / "backtest_quarterly_rebalance_v2.py"
    cmd = [
        sys.executable,
        str(script),
        "--asof", args.asof,
        "--metric", args.metric,
        "--feat_v", str(args.feat_v),
        "--ret_v", str(args.ret_v),
        "--ret_src", args.ret_src,
        "--k", str(args.k),
        "--strategy", args.strategy,
        "--out_v", str(args.out_v),
        "--tcost_bps", str(args.tcost_bps),
        "--max_per_group", str(args.max_per_group),
        "--entry_gap", str(args.entry_gap),
        "--filter_fallback", args.filter_fallback,
    ]
    if args.save_holdings:
        cmd.append("--save_holdings")
    if args.save_inputs:
        cmd.append("--save_inputs")

    print("[RUN]", " ".join(cmd))
    subprocess.run(cmd, check=True)


if __name__ == "__main__":
    main()
