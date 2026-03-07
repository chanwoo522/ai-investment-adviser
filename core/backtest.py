import pandas as pd
from core.portfolio import select_top_n, equal_weight_portfolio, compute_portfolio_return

def run_backtest(score_df, top_n=20):

    portfolio = select_top_n(score_df, top_n)
    portfolio = equal_weight_portfolio(portfolio)

    port_return = compute_portfolio_return(portfolio)

    return port_return, portfolio
