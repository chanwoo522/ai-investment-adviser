from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from scripts.financials.run_financial_metrics_complete import (
    run_financial_metrics_for_report_universe,
)
from scripts.live.calc_live_performance import build_monthly_portfolio_performance
from scripts.live.score_latest_rebalance import run_fresh_start_scoring_pipeline
from scripts.subscriber_report.build_subscriber_26q3_report import (
    generate_subscriber_quarterly_report,
)
from scripts.subscriber_report.subscriber_bundle import build_public_distribution_bundle


def _monthly_inputs() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    positions = pd.DataFrame(
        [
            {"ticker": "000001", "name": "A", "shares": 10, "position_value": 1_000},
            {"ticker": "000002", "name": "B", "shares": 5, "position_value": 1_000},
        ]
    )
    dates = pd.to_datetime(["2027-01-15", "2027-01-29", "2027-02-26", "2027-03-12"])
    prices = pd.DataFrame(
        [
            {"date": date, "ticker": ticker, "close": close + offset}
            for offset, date in enumerate(dates)
            for ticker, close in (("000001", 100), ("000002", 200))
        ]
    )
    benchmark = pd.DataFrame(
        {"date": dates, "krx300_price_index": [1000.0, 1010.0, 1020.0, 1030.0]}
    )
    return positions, prices, benchmark


def test_monthly_performance_is_dynamic_and_partial_month() -> None:
    positions, prices, benchmark = _monthly_inputs()
    result = build_monthly_portfolio_performance(
        starting_positions=positions,
        official_equity_prices=prices,
        official_krx300=benchmark,
        performance_start="2027-01-15",
        performance_end="2027-03-12",
    )
    assert result.monthly_performance["date"].tolist() == [
        "2027-01-15",
        "2027-01-29",
        "2027-02-26",
        "2027-03-12(부분월)",
    ]
    assert result.qa["equity_count"] == 2
    assert result.qa["fill_used"] is False
    assert result.qa["reconciliation_status"] == "PASS"


def test_monthly_performance_blocks_unexplained_quantity_change() -> None:
    positions, prices, benchmark = _monthly_inputs()
    positions["end_quantity"] = [9, 5]
    with pytest.raises(RuntimeError, match="ACTIVITY_LEDGER_REQUIRED"):
        build_monthly_portfolio_performance(
            starting_positions=positions,
            official_equity_prices=prices,
            official_krx300=benchmark,
            performance_start="2027-01-15",
            performance_end="2027-03-12",
        )


def _features() -> pd.DataFrame:
    rows = []
    for index in range(12):
        rows.append(
            {
                "ticker": f"{index + 1:06d}",
                "name": f"Security {index + 1}",
                "rebalance_month": "2027-03-31",
                "OpIncome_acc2_log1p": float(index),
                "Revenue_acc2": float(index % 5),
                "Debt_to_Equity_log": float(index) / 20,
                "op_growth_streak2": float(index % 2),
                "rev_growth_streak2": float((index + 1) % 2),
                "op_cur_q": 100.0,
                "OpIncome_ttm": 400.0,
                "traded_value": 2_000_000_000.0,
                "mcap": 500_000_000_000.0,
                "op_qoq": 0.1,
                "CFO_isnull": 0,
                "CFO_warn": 0,
                "NetIncome_ttm": 100.0,
                "NetIncome_acc2": 0.2,
            }
        )
    return pd.DataFrame(rows)


def test_fresh_start_selection_ignores_current_membership(tmp_path: Path) -> None:
    config = {
        "weights": {
            "OpIncome_acc2_log1p": 1.0,
            "Revenue_acc2": 0.25,
            "Debt_to_Equity_log": -0.35,
            "op_growth_streak2": 0.15,
            "rev_growth_streak2": 0.05,
        },
        "filters": {"min_OpIncome_ttm": 0, "min_op_cur_q": 0},
        "scoring": {
            "clip_z": 4.0,
            "use_robust_z": True,
            "raw_factors": ["op_growth_streak2", "rev_growth_streak2"],
        },
        "selection": {"portfolio_size": 3, "keep_current_top_n": 0},
    }
    features = _features()
    universe = features[["ticker"]]
    first = run_fresh_start_scoring_pipeline(
        prepared_features=features,
        certified_universe=universe,
        marketdata=None,
        strategy_config=config,
        information_asof="2027-03-15",
        target_date="2027-03-31",
        output_dir=tmp_path / "first",
        current_holdings=pd.DataFrame({"ticker": ["000001"]}),
    )
    second = run_fresh_start_scoring_pipeline(
        prepared_features=features,
        certified_universe=universe,
        marketdata=None,
        strategy_config=config,
        information_asof="2027-03-15",
        target_date="2027-03-31",
        output_dir=tmp_path / "second",
        current_holdings=pd.DataFrame({"ticker": ["000012"]}),
    )
    assert first.fresh_start_top_k[["ticker", "model_rank"]].to_dict("records") == second.fresh_start_top_k[["ticker", "model_rank"]].to_dict("records")
    assert first.full_universe_scores["hold_bonus_applied"].eq(0).all()
    assert first.metadata["keep_current_top_n"] == 0
    assert first.metadata["current_membership_used"] is False


def test_financial_metrics_dynamic_union_and_output_root(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "run_manifest.json").write_text(
        json.dumps({"development_status": "PASS_CERTIFIED"}), encoding="utf-8"
    )
    rows = []
    for index in range(4):
        rows.append(
            {
                "ticker": f"{index + 1:06d}",
                "name": f"S{index}",
                "selection_status": "SELECTED" if index < 2 else "DROPPED",
                "eps_ttm": 100 + index,
                "net_income_ttm": 1_000 + index,
                "per_ttm": 10 + index,
                "per_status": "PASS",
                "pbr": 1.0,
                "psr": 2.0,
                "market_cap": 10_000,
                "revenue_ttm": 20_000,
                "operating_income_ttm": 2_000,
                "cfo_ttm": 1_500,
                "cfo_to_operating_income": 0.75,
                "period_minus_2": "2026Q3",
                "period_minus_1": "2026Q4",
                "period_latest": "2027Q1",
            }
        )
    pd.DataFrame(rows).to_csv(source / "report_analysis_equity_metrics.csv", index=False)
    universe = pd.DataFrame({"ticker": ["000001", "000002", "000004"]})
    result = run_financial_metrics_for_report_universe(
        report_analysis_equities=universe,
        information_asof="2027-05-15",
        price_asof="2027-05-14",
        output_root=tmp_path / "out",
        source_financial_run=source,
    )
    assert len(result.report_analysis_metrics) == 3
    assert result.completeness["eps_coverage"] == 3
    assert result.completeness["financial_metrics_complete"] is True


def test_report_accepts_dynamic_quarter_dates_and_tickers(tmp_path: Path) -> None:
    current = pd.DataFrame(
        [{"ticker": "111111", "name": "Current", "quantity": 2, "price_per_share": 100, "market_value": 200, "equity_weight": 1.0, "valuation_date": "2027-05-14"}]
    )
    monthly = pd.DataFrame(
        [
            {"date": "2027-04-01", "date_iso": "2027-04-01", "portfolio_nav": 200, "portfolio_change_amount": 0, "portfolio_monthly_change_rate": 0.0, "portfolio_cumulative_change_rate": 0.0, "krx300_price_index": 1000.0, "krx300_equivalent_nav": 200, "krx300_change_amount": 0, "krx300_monthly_change_rate": 0.0, "krx300_cumulative_change_rate": 0.0, "cumulative_excess_return": 0.0},
            {"date": "2027-05-14(부분월)", "date_iso": "2027-05-14", "portfolio_nav": 220, "portfolio_change_amount": 20, "portfolio_monthly_change_rate": 0.1, "portfolio_cumulative_change_rate": 0.1, "krx300_price_index": 1050.0, "krx300_equivalent_nav": 210, "krx300_change_amount": 10, "krx300_monthly_change_rate": 0.05, "krx300_cumulative_change_rate": 0.05, "cumulative_excess_return": 0.05},
        ]
    )
    returns = pd.DataFrame([{"ticker": "111111", "name": "Current", "quantity": 2, "start_value": 200, "end_value": 220, "change_amount": 20, "change_rate": 0.1}])
    top = pd.DataFrame([{"ticker": "222222", "name": "Selected", "model_rank": 1, "model_score": 2.0, "advisor_sector": "Sector", "quality_penalty": 0.0, "transition_status": "NEW_SELECTION"}])
    boundary = pd.DataFrame([{"rank": 1, "ticker": "222222", "name": "Selected", "model_score": 2.0, "score_gap_vs_k": 0.0, "selection_status": "SELECTED"}, {"rank": 2, "ticker": "333333", "name": "Watch", "model_score": 1.0, "score_gap_vs_k": -1.0, "selection_status": "WATCHLIST_NOT_SELECTED"}])
    target = pd.DataFrame([
        {"ticker": "222222", "name": "Selected", "asset_class": "EQUITY", "model_rank": 1, "model_score": 2.0, "advisor_sector": "Sector", "target_weight": 0.9, "target_value": 900, "reference_price_asof": "2027-05-14", "reference_price": 100, "reference_target_qty": 9, "illustrative_target_value": 900},
        {"ticker": "CASH_EQUIVALENT_BUCKET", "name": "Cash", "asset_class": "CASH_EQUIVALENT_BUCKET", "model_rank": pd.NA, "model_score": pd.NA, "advisor_sector": pd.NA, "target_weight": 0.1, "target_value": 100, "reference_price_asof": pd.NA, "reference_price": pd.NA, "reference_target_qty": pd.NA, "illustrative_target_value": 100},
    ])
    comparison = pd.DataFrame([
        {"ticker": "222222", "name": "Selected", "asset_class": "EQUITY", "model_selected": True, "model_rank": 1, "current_value": 0, "target_weight": 0.9, "transition_status": "NEW_SELECTION"},
        {"ticker": "111111", "name": "Current", "asset_class": "EQUITY", "model_selected": False, "model_rank": pd.NA, "current_value": 220, "target_weight": 0.0, "transition_status": "DROPPED"},
    ])
    details = pd.DataFrame([{"ticker": "222222", "name": "Selected", "model_rank": 1, "industry": "Industry", "sector": "Sector", "period_1": "2026Q3", "period_2": "2026Q4", "period_3": "2027Q1", "revenue_1": 1, "revenue_2": 2, "revenue_3": 3, "revenue_growth_1": "-", "revenue_growth_2": "100%", "revenue_growth_3": "50%", "operating_income_1": 1, "operating_income_2": 2, "operating_income_3": 3, "operating_income_growth_1": "-", "operating_income_growth_2": "100%", "operating_income_growth_3": "50%", "market_cap": 1000, "cfo_ttm": 10, "cfo_to_operating_income": 0.5, "quality_penalty": 0.0, "pbr": 1.0, "psr": 2.0}])
    dropped = pd.DataFrame([{"ticker": "111111"}])
    output = tmp_path / "custom-report.html"
    result = generate_subscriber_quarterly_report(
        quarter_label="27Q2",
        report_title="Custom quarterly report",
        model_information_asof="2027-05-13",
        account_asof="2027-05-14",
        performance_start="2027-04-01",
        performance_end="2027-05-14",
        benchmark_name="KRX300",
        current_portfolio=current,
        monthly_performance=monthly,
        security_returns=returns,
        fresh_start_top_k=top,
        boundary_watchlist=boundary,
        target_portfolio=target,
        current_vs_target=comparison,
        selected_details=details,
        dropped_summary=dropped,
        dropped_details=dropped,
        output_html=output,
    )
    assert "Custom quarterly report" in result.html
    assert "2027년 2분기" in result.html
    assert result.metadata["quarter_label"] == "27Q2"
    assert "2027-04-01 ~ 2027-05-14" in result.html
    assert "222222" in result.html
    assert "26Q3 분기 리밸런싱" not in result.html


def test_public_bundle_dynamic_allowlist_and_private_rejection(tmp_path: Path) -> None:
    public = tmp_path / "report.html"
    public.write_text("<html><body>public report</body></html>", encoding="utf-8")
    result = build_public_distribution_bundle(
        output_path=tmp_path / "public.zip",
        public_files={"report.html": public},
        allowed_suffixes=(".html",),
        forbidden_patterns=("account_id",),
    )
    assert result.members == ("report.html",)
    assert result.crc_status == "PASS"
    broker = tmp_path / "broker.xlsx"
    broker.write_bytes(b"private")
    with pytest.raises(ValueError):
        build_public_distribution_bundle(
            output_path=tmp_path / "bad.zip",
            public_files=[broker],
            allowed_suffixes=(".xlsx",),
            forbidden_patterns=(),
        )
