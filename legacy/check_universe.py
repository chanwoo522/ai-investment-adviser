import pandas as pd

asofs = ["2025-05-31","2025-08-31","2025-11-30","2026-02-18"]

for a in asofs:
    df = pd.read_parquet(
        f"data/processed/universe__asof={a}__src=phase1__mcap_top=800__trd_bot=0.1__v=1.parquet"
    )
    print(a, len(df), df["ticker"].nunique())