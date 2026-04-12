# scripts/run_batch.py
from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path
import traceback

import pandas as pd

from engine.run_asof import RunAsOfConfig, run_asof
from strategies.B_growth_plus_quality import B_growth_plus_quality


STRATEGY_MAP = {
    "B_growth_plus_quality": B_growth_plus_quality,
    # 나중에 여기만 늘리면 됨:
    # "A_value_quality": A_value_quality,
    # "C_momentum": C_momentum,
}


def parse_list(s: str, typ=int):
    # "10,20,30" -> [10,20,30]
    items = [x.strip() for x in s.split(",") if x.strip()]
    return [typ(x) for x in items]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", required=True)

    ap.add_argument("--strategies", default="B_growth_plus_quality")
    ap.add_argument("--ks", default="20")
    ap.add_argument("--out_v", type=int, default=1)

    ap.add_argument("--features_v", type=int, default=2)
    ap.add_argument("--returns_v", type=int, default=1)
    ap.add_argument("--universe_v", type=int, default=1)
    ap.add_argument("--master_v", type=int, default=1)

    ap.add_argument("--marketdata_v", type=int, default=2)
    ap.add_argument("--marketdata_src", default="synthetic_dart")
    ap.add_argument("--marketdata_lookback", default="365d")

    ap.add_argument("--root", default=".")
    ap.add_argument("--soft_fail", action="store_true")
    args = ap.parse_args()

    root = Path(args.root)

    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    ks = parse_list(args.ks, int)

    rows = []
    for strat in strategies:
        if strat not in STRATEGY_MAP:
            raise ValueError(f"Unknown strategy: {strat}. Known: {list(STRATEGY_MAP.keys())}")

        impl = STRATEGY_MAP[strat]()

        for k in ks:
            cfg = RunAsOfConfig(
                asof=args.asof,
                metric=args.metric,
                strategy=strat,
                k=int(k),
                features_v=args.features_v,
                returns_v=args.returns_v,
                universe_v=args.universe_v,
                master_v=args.master_v,
                marketdata_v=args.marketdata_v,
                marketdata_src=args.marketdata_src,
                marketdata_lookback=args.marketdata_lookback,
                out_v=args.out_v,
                root=root,
            )

            try:
                out = run_asof(impl, cfg, prev_holdings=None)
                paths = out.get("paths", {})
                rows.append(
                    {
                        "ok": True,
                        "strategy": strat,
                        "k": int(k),
                        "asof": args.asof,
                        "metric": args.metric,
                        "out_v": args.out_v,
                        "orders_path": paths.get("orders", ""),
                        "summary_path": paths.get("summary", ""),
                    }
                )
                print(f"[OK] batch: strategy={strat} k={k}")
            except Exception as e:
                tb = traceback.format_exc()
                print(f"[FAIL] batch: strategy={strat} k={k}: {e}")
                if not args.soft_fail:
                    raise
                rows.append(
                    {
                        "ok": False,
                        "strategy": strat,
                        "k": int(k),
                        "asof": args.asof,
                        "metric": args.metric,
                        "out_v": args.out_v,
                        "orders_path": "",
                        "summary_path": "",
                        "error": str(e),
                        "traceback": tb,
                    }
                )

    df = pd.DataFrame(rows)
    outp = root / "data/processed/explainability" / f"batch__asof={args.asof}__metric={args.metric}__v={args.out_v}.csv"
    outp.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(outp, index=False, encoding="utf-8-sig")
    print(f"[OK] batch report saved: {outp}")


if __name__ == "__main__":
    main()