import pandas as pd
import numpy as np
from pathlib import Path


# =========================
# LOAD EXECUTION PLAN
# =========================
def load_execution_plan(path):
    df = pd.read_csv(path, dtype={'ticker': str})
    df['ticker'] = df['ticker'].str.zfill(6)

    # SELL은 0 처리
    df['shares_final'] = df['planned_total_qty']
    df.loc[df['action'] == 'SELL', 'shares_final'] = 0

    return df[['ticker', 'shares_final']]


# =========================
# LOAD PRICE DATA
# =========================
def load_prices(path):
    path = Path(path)

    if path.suffix == ".parquet":
        df = pd.read_parquet(path)
    else:
        df = pd.read_csv(path)

    # ---------------------------------
    # CASE 1: long format
    # (date, ticker, close)
    # ---------------------------------
    if 'ticker' in df.columns and 'date' in df.columns:

        df['ticker'] = df['ticker'].astype(str).str.zfill(6)
        df['date'] = pd.to_datetime(df['date'])

        price_candidates = [
            'close', 'adj_close', '종가',
            'Close', 'Adj Close',
            'price', 'PRICE',
            '종가(원)', '수정종가'
        ]

        found = None
        for col in price_candidates:
            if col in df.columns:
                found = col
                break

        if found is None:
            print("\n[DEBUG] Available columns:")
            print(df.columns.tolist())
            raise ValueError("price column not found")

        df = df.rename(columns={found: 'price'})
        return df[['date', 'ticker', 'price']]

    # ---------------------------------
    # CASE 2: wide format
    # index=date, columns=ticker
    # ---------------------------------
    else:
        print("[INFO] Detected wide format price table")

        df.index = pd.to_datetime(df.index)

        df = df.reset_index().rename(columns={'index': 'date'})

        df_long = df.melt(id_vars='date', var_name='ticker', value_name='price')

        df_long['ticker'] = df_long['ticker'].astype(str).str.zfill(6)

        return df_long[['date', 'ticker', 'price']]


# =========================
# BUILD NAV
# =========================
def build_nav(exec_df, price_df, start_date=None, end_date=None):

    df = price_df.merge(exec_df, on='ticker', how='inner')

    if start_date:
        df = df[df['date'] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df['date'] <= pd.to_datetime(end_date)]

    df['value'] = df['price'] * df['shares_final']

    nav = (
        df.groupby('date')['value']
        .sum()
        .reset_index()
        .sort_values('date')
    )

    nav['return'] = nav['value'].pct_change().fillna(0)
    nav['cum_return'] = (1 + nav['return']).cumprod()

    nav['cum_max'] = nav['cum_return'].cummax()
    nav['drawdown'] = nav['cum_return'] / nav['cum_max'] - 1

    return nav


# =========================
# CONTRIBUTION
# =========================
def calc_contribution(exec_df, price_df, start_date=None, end_date=None):

    df = price_df.merge(exec_df, on='ticker', how='inner')

    if start_date:
        df = df[df['date'] >= pd.to_datetime(start_date)]
    if end_date:
        df = df[df['date'] <= pd.to_datetime(end_date)]

    df = df.sort_values(['ticker', 'date'])

    df['return'] = df.groupby('ticker')['price'].pct_change()

    first = df.groupby('ticker').first().reset_index()
    first['value'] = first['price'] * first['shares_final']
    total = first['value'].sum()

    first['weight'] = first['value'] / total

    weight_map = dict(zip(first['ticker'], first['weight']))

    df['weight'] = df['ticker'].map(weight_map)
    df['contribution'] = df['weight'] * df['return']

    contrib = (
        df.groupby('ticker')['contribution']
        .sum()
        .sort_values(ascending=False)
        .reset_index()
    )

    return contrib


# =========================
# MAIN
# =========================
def main(
    execution_plan,
    prices,
    out_nav,
    out_contrib,
    start_date=None,
    end_date=None
):

    exec_df = load_execution_plan(execution_plan)
    price_df = load_prices(prices)

    nav = build_nav(exec_df, price_df, start_date, end_date)
    contrib = calc_contribution(exec_df, price_df, start_date, end_date)

    Path(out_nav).parent.mkdir(parents=True, exist_ok=True)
    Path(out_contrib).parent.mkdir(parents=True, exist_ok=True)

    nav.to_csv(out_nav, index=False)
    contrib.to_csv(out_contrib, index=False)

    print("[OK] NAV saved:", out_nav)
    print("[OK] Contribution saved:", out_contrib)


# =========================
# CLI
# =========================
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()

    parser.add_argument("--execution_plan", required=True)
    parser.add_argument("--prices", required=True)
    parser.add_argument("--out_nav", required=True)
    parser.add_argument("--out_contrib", required=True)
    parser.add_argument("--start_date", default=None)
    parser.add_argument("--end_date", default=None)

    args = parser.parse_args()

    main(
        args.execution_plan,
        args.prices,
        args.out_nav,
        args.out_contrib,
        args.start_date,
        args.end_date
    )