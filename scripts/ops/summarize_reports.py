# scripts/ops/summarize_reports.py
import argparse
import glob
import os
import re
from pathlib import Path

import numpy as np
import pandas as pd


def _parse_kv_from_filename(fname: str):
    """
    Extract k, v, tcost_bps from filename like:
    report__asof=YYYY-MM-DD__metric=...__k=20__strat=...__v=999.csv
    """
    k = v = None
    tcost = np.nan
    m = re.search(r"__k=(\d+)", fname)
    if m:
        k = int(m.group(1))
    m = re.search(r"__v=(\d+)", fname)
    if m:
        v = int(m.group(1))
    # tcost may exist only inside file; keep filename parse as fallback only
    return k, v, tcost


def _pick_first_present(row: pd.Series, candidates: list[str]):
    for c in candidates:
        if c in row.index and pd.notna(row[c]):
            return row[c]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asof", required=True)
    ap.add_argument("--metric", default=None)

    # Optional filters (avoid mixing old runs like v=999 into a new sweep)
    ap.add_argument("--v_min", type=int, default=None, help="Keep only reports with v >= v_min")
    ap.add_argument("--v_max", type=int, default=None, help="Keep only reports with v <= v_max")
    ap.add_argument(
        "--v_in",
        type=str,
        default=None,
        help="Comma-separated allowlist for v (e.g., 900,901,902). If set, v_min/v_max are ignored.",
    )

    args = ap.parse_args()

    asof = args.asof
    metric = args.metric

    v_min = args.v_min
    v_max = args.v_max
    v_in = None
    if args.v_in:
        v_in = {int(x.strip()) for x in args.v_in.split(",") if x.strip()}

    patt = rf"data\processed\report__asof={asof}__"
    if metric:
        patt += rf"metric={metric}__"
    patt += r"*.csv"

    paths = sorted(glob.glob(patt))
    if not paths:
        print(f"No report files matched: {patt}")
        return

    rows = []
    matched = 0
    kept = 0

    for p in paths:
        fname = os.path.basename(p)
        k_from_name, v_from_name, _ = _parse_kv_from_filename(fname)

        # --- filters (by v) ---
        matched += 1
        if v_in is not None:
            if v_from_name is None or v_from_name not in v_in:
                continue
        else:
            if v_min is not None:
                if v_from_name is None or v_from_name < v_min:
                    continue
            if v_max is not None:
                if v_from_name is None or v_from_name > v_max:
                    continue

        df = pd.read_csv(p)
        if df.empty:
            continue
        # report files are 1-row summary; take last row just in case
        r = df.iloc[-1]

        rows.append(
            {
                "file": fname,
                "strategy": _pick_first_present(r, ["strategy", "strat"]),
                "k": int(_pick_first_present(r, ["k"])) if _pick_first_present(r, ["k"]) is not None else k_from_name,
                "v": v_from_name,
                "tcost_bps": _pick_first_present(r, ["tcost_bps", "tcost"]),
                # NAV
                "gross_nav": _pick_first_present(
                    r,
                    ["final_nav_gross", "gross_nav", "nav_gross", "final_gross_nav"],
                ),
                "net_nav": _pick_first_present(
                    r,
                    ["final_nav_net", "net_nav", "nav_net", "final_net_nav"],
                ),
                # CAGR / Sharpe
                "cagr_gross": _pick_first_present(r, ["cagr_gross"]),
                "cagr_net": _pick_first_present(r, ["cagr_net"]),
                "sharpe_net": _pick_first_present(r, ["sharpe_net"]),
                # turnover
                "avg_turnover": _pick_first_present(
                    r,
                    ["avg_turnover_per_rebalance", "avg_turnover", "turnover"],
                ),
            }
        )
        kept += 1

    out = pd.DataFrame(rows)

    if out.empty:
        print(f"[WARN] matched files: {matched} but kept(after v filter): 0")
        print(f"[HINT] Try adjusting --v_min/--v_max or --v_in. Pattern was: {patt}")
        return

    # nice ordering / types
    out["k"] = pd.to_numeric(out["k"], errors="coerce").astype("Int64")
    out["v"] = pd.to_numeric(out["v"], errors="coerce").astype("Int64")
    out["tcost_bps"] = pd.to_numeric(out["tcost_bps"], errors="coerce")
    for c in ["gross_nav", "net_nav", "cagr_gross", "cagr_net", "sharpe_net", "avg_turnover"]:
        out[c] = pd.to_numeric(out[c], errors="coerce")

    out = out.sort_values(["strategy", "k", "tcost_bps", "v"], na_position="last")

    out_path = Path(rf"data\processed\summary_reports__asof={asof}__metric={metric or 'ALL'}.csv")
    out.to_csv(out_path, index=False)

    print(f"[OK] matched: {matched}  kept: {len(out)}")
    if v_in is not None:
        print(f"[OK] v filter: v_in={sorted(v_in)}")
    else:
        print(f"[OK] v filter: v_min={v_min} v_max={v_max}")
    print(f"[OK] saved: {out_path}")
    print(out.to_string(index=False))


if __name__ == "__main__":
    main()