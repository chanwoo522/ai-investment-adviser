import pandas as pd

def build_universe(master_df, market_df, asof):

    df = master_df.copy()

    # 시총 상위 70%
    df = df.sort_values("market_cap", ascending=False)
    cutoff = int(len(df) * 0.7)
    df = df.iloc[:cutoff]

    # 거래대금 하위 30% 제거
    df = df[df["avg_trading_value_rank"] <= 0.7]

    # 흑자 기업만
    df = df[df["operating_income"] > 0]

    return df.reset_index(drop=True)
