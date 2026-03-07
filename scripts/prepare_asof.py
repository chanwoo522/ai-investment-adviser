#!/usr/bin/env python
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


def _run(script_name: str, args: list[str]) -> None:
    """Run one repo-local script with current venv python."""
    py = Path(".venv/Scripts/python.exe")
    if not py.exists():
        # fallback (e.g. linux/mac or different layout)
        py = Path(".venv/bin/python")
    cmd = [str(py), str(Path("scripts") / script_name)] + args
    print("[RUN]", " ".join(cmd))
    r = subprocess.run(cmd, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"Command failed ({r.returncode}): {' '.join(cmd)}")


def _phase1_universe_path(asof: str, mcap_top: int, trd_bot: float, universe_v: int = 1) -> Path:
    # Keep formatting consistent with build_universe.py outputs
    return Path(
        f"data/processed/universe__asof={asof}__src=phase1__mcap_top={mcap_top}__trd_bot={trd_bot}__v={universe_v}.parquet"
    )


def main() -> None:
    ap = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter,
        description=r"""
Prepare all artifacts for a given asof:
  1) KRX master / marketdata
  2) Universe (phase1)
  3) Prices daily + monthly returns
  4) Fundamentals quarterly (uses the phase1 universe path)

Notes:
- If collect_fundamentals_quarterly.py has a different default universe naming,
  we explicitly pass --universe to avoid mismatches.
""",
    )
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--universe_v", type=int, default=1)
    ap.add_argument("--mcap_top", type=int, default=800, help="Universe market-cap cutoff (top N by mcap).")
    ap.add_argument("--trd_bot", type=float, default=0.1, help="Universe liquidity filter (bottom quantile).")
    ap.add_argument("--px_v", type=int, default=1)
    ap.add_argument("--ret_v", type=int, default=1)
    ap.add_argument("--lookback_years", type=int, default=15)
    ap.add_argument("--start", default="20160101")
    args = ap.parse_args()

    asof = args.asof
    metric = args.metric

    # 1) master / market
    _run("collect_krx_master.py", ["--asof", asof])
    _run("collect_krx_marketdata.py", ["--asof", asof])

    # 2) universe (phase1)  ✅ passthrough
    _run(
        "build_universe.py",
        ["--asof", asof, "--mcap_top", str(args.mcap_top), "--trd_bot", str(args.trd_bot)],
    )

    # (optional) export csv for inspection
    exp = Path("scripts/ops/export_universe_csv.py")
    if exp.exists():
        _run("ops/export_universe_csv.py", ["--asof", asof, "--metric", metric, "--out_v", str(args.universe_v)])

    # 3) prices + returns
    _run("collect_prices.py", ["--asof", asof, "--freq", "d", "--lookback_years", str(args.lookback_years)])
    _run(
        "make_prices_daily.py",
        ["--asof", asof, "--metric", metric, "--start", args.start, "--universe_v", str(args.universe_v), "--out_v", str(args.px_v)],
    )
    _run("make_returns_monthly.py", ["--asof", asof, "--metric", metric, "--px_v", str(args.px_v), "--out_v", str(args.ret_v)])

    # 4) fundamentals (quarterly) ✅ same mcap_top/trd_bot reflected in path
    uni = _phase1_universe_path(asof, args.mcap_top, args.trd_bot, args.universe_v)
    _run("collect_fundamentals_quarterly.py", ["--asof", asof, "--universe", str(uni)])

    print("[OK] prepare_asof done:", asof)


if __name__ == "__main__":
    main()