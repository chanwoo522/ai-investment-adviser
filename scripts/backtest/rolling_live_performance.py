import pandas as pd
from pathlib import Path

def compute_metrics(nav):
    nav = nav.copy()
    nav["cum_return"] = nav["cum_return"]

    total_return = nav["cum_return"].iloc[-1] - 1
    mdd = (nav["cum_return"] / nav["cum_return"].cummax() - 1).min()

    return {
        "total_return": total_return,
        "max_drawdown": mdd
    }

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--strategy")
    args = parser.parse_args()

    base = Path("data/sandbox")

    nav_files = list(base.rglob("nav__*.csv"))

    results = []

    for f in nav_files:
        try:
            df = pd.read_csv(f)
            metrics = compute_metrics(df)
            results.append({
                "file": str(f),
                **metrics
            })
        except:
            continue

    res = pd.DataFrame(results)
    res = res.sort_values("total_return", ascending=False)

    out = base / "rolling_summary.csv"
    res.to_csv(out, index=False)

    print("[OK] saved:", out)
    print(res.head())

if __name__ == "__main__":
    main()