# scripts/run_strategy.py
from __future__ import annotations

import argparse
from pathlib import Path

from engine.run_asof import RunAsOfConfig, run_asof
from strategies.B_growth_plus_quality import B_growth_plus_quality

STRATEGIES = {
    "B_growth_plus_quality": B_growth_plus_quality,
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default="revenue_op")
    ap.add_argument("--strategy", required=True)
    ap.add_argument("--k", type=int, required=True)

    ap.add_argument("--features_v", type=int, default=2)
    ap.add_argument("--returns_v", type=int, default=1)
    ap.add_argument("--universe_v", type=int, default=1)
    ap.add_argument("--master_v", type=int, default=1)

    ap.add_argument("--master_src", default="pykrx")
    ap.add_argument("--returns_src", default="pykrx")

    ap.add_argument("--marketdata_v", type=int, default=2)         # <-- 우리가 만들 synthetic_dart v=2를 기본으로
    ap.add_argument("--marketdata_src", default="synthetic_dart")  # <-- mcap 실값 버전
    ap.add_argument("--marketdata_lookback", default="365d")

    ap.add_argument("--out_v", type=int, default=1)
    ap.add_argument("--root", default=".")

    args = ap.parse_args()

    if args.strategy not in STRATEGIES:
        raise ValueError(f"Unknown strategy: {args.strategy}. Available={list(STRATEGIES)}")

    impl = STRATEGIES[args.strategy]()

    cfg = RunAsOfConfig(
        asof=args.asof,
        metric=args.metric,
        strategy=args.strategy,
        k=args.k,
        features_v=args.features_v,
        returns_v=args.returns_v,
        universe_v=args.universe_v,
        master_v=args.master_v,
        master_src=args.master_src,
        returns_src=args.returns_src,
        marketdata_v=args.marketdata_v,
        marketdata_src=args.marketdata_src,
        marketdata_lookback=args.marketdata_lookback,
        out_v=args.out_v,
        root=Path(args.root),
    )

    run_asof(impl, cfg, prev_holdings=None)


if __name__ == "__main__":
    main()