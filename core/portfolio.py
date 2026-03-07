import numpy as np

def select_top_n(df, n=20):
    return df.sort_values("total_score", ascending=False).head(n)


def equal_weight_portfolio(df):
    n = len(df)
    df["weight"] = 1.0 / n
    return df


def compute_portfolio_return(df):
    return np.sum(df["weight"] * df["forward_return"])
