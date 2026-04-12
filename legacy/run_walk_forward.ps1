$ASOF = "2026-02-18"
$METRIC = "revenue_op"
python .\backtest_quarterly_rebalance_v2.py --asof $ASOF --metric $METRIC --feat_v 2 --ret_v 1 --ret_src pykrx --k 10 --strategy D_quality_filter_debt_profitaccel --out_v 4001 --tcost_bps 30 --filter_fallback error
python .\walk_forward_validate.py --asof $ASOF --metric $METRIC --feat_v 2 --ret_v 1 --ret_src pykrx --out_v 4001 --strategies B_growth_plus_quality,D_quality_filter_debt_profitaccel --ks 10,15,20 --objective sharpe_net --train_months 36 --test_months 12 --step_months 12 --filter_fallback error
