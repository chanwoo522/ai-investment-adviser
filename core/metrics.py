import numpy as np

def compute_growth_cagr(series):
    if len(series) < 2:
        return None
    return (series.iloc[-1] / series.iloc[0]) ** (1/(len(series)-1)) - 1


def compute_financial_metrics(df):
    df["rev_cagr"] = df.groupby("ticker")["Revenue"].transform(compute_growth_cagr)
    df["fcf_margin"] = df["FCF"] / df["Revenue"]
    return df
