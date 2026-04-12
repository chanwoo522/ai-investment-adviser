This patch package contains:
- strategies.yaml : adds D_quality_filter_debt_profitaccel
- backtest_quarterly_rebalance_v2.py : optional strict filter handling + maxdd_net in report
- walk_forward_validate.py : selects candidate strategy/k on rolling train windows, evaluates next test window out-of-sample
- run_walk_forward.ps1 : example commands

Recommended first run:
  python .\backtest_quarterly_rebalance_v2.py --asof 2026-02-18 --metric revenue_op --feat_v 2 --ret_v 1 --ret_src pykrx --k 10 --strategy D_quality_filter_debt_profitaccel --out_v 4001 --tcost_bps 30 --filter_fallback error
  python .\walk_forward_validate.py --asof 2026-02-18 --metric revenue_op --feat_v 2 --ret_v 1 --ret_src pykrx --out_v 4001 --strategies B_growth_plus_quality,D_quality_filter_debt_profitaccel --ks 10,15,20 --objective sharpe_net --train_months 36 --test_months 12 --step_months 12 --filter_fallback error
