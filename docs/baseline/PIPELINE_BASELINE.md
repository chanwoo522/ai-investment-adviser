# AI_INV_ADV PIPELINE BASELINE (2026-03-30)

## LIVE BASELINE
Entry: scripts/live/run_live_rebalance.ps1

Flow:
NAV → score → actions → execution → report → validation

Inputs:
- holdings
- features_live
- krx_marketdata
- execution.yaml

Outputs:
- scores
- actions
- execution_plan
- reports

## CORE ENGINE
- scripts/backtest/backtest_quarterly_rebalance_v2.py

## REBUILD BASELINE
Entry: scripts/data_pipeline/prepare_asof.py

⚠ archive fallback 존재

## ACTIVE FALLBACK (DO NOT REMOVE)
- scripts/archive/collect_prices.py
- scripts/archive/make_returns_monthly.py
- scripts/archive/build_features_phase1.py

## RULE
- archive 삭제 금지
- live 기준선 절대 보호
