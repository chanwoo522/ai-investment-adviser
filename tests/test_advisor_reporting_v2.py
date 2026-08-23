from __future__ import annotations

import re
import unittest

import pandas as pd

from scripts.advisor.reporting_v2 import (
    ADVISOR_MODE,
    BASE_SECTION_TITLES,
    PHILOSOPHY_TEXT,
    UNVERIFIED_ACCOUNT_SECTION_TITLE,
    generate_advisor_v2_html,
    validate_advisor_v2_html,
)


def _inputs() -> dict:
    tickers = [
        "098460",
        "322000",
        "005930",
        "080220",
        "253450",
        "327260",
        "121600",
        "232140",
        "025560",
        "219130",
    ]
    names = [
        "고영",
        "HD현대에너지솔루션",
        "삼성전자",
        "제주반도체",
        "스튜디오드래곤",
        "RF머트리얼즈",
        "나노신소재",
        "와이씨",
        "미래산업",
        "타이거일렉",
    ]
    sectors = [
        "반도체 장비",
        "태양광 에너지",
        "정보기술 하드웨어",
        "반도체",
        "미디어",
        "전자부품",
        "첨단소재",
        "반도체 장비",
        "산업재",
        "전자부품",
    ]
    top_k = pd.DataFrame(
        {
            "model_rank": range(1, 11),
            "ticker": tickers,
            "name": names,
            "model_score": [4.0 - index * 0.07 for index in range(10)],
            "official_industry_name": [
                "특수 목적용 기계 제조업",
                "전동기, 발전기 및 전기 변환·공급·제어 장치 제조업",
                "통신 및 방송 장비 제조업",
                "반도체 제조업",
                "영화, 비디오물, 방송프로그램 제작 및 배급업",
                "전자부품 제조업",
                "기타 화학제품 제조업",
                "반도체 제조업",
                "특수 목적용 기계 제조업",
                "전자부품 제조업",
            ],
            "advisor_sector": sectors,
            "quality_penalty": [0.0] * 10,
            "selection_status": ["NEW_SELECTION"] * 9 + ["RESELECTED"],
        }
    )
    boundary = pd.DataFrame(
        {
            "rank": range(8, 16),
            "ticker": ["232140", "025560", "219130", "111111", "222222", "333333", "444444", "555555"],
            "name": ["와이씨", "미래산업", "타이거일렉", "후보11", "후보12", "후보13", "후보14", "후보15"],
            "model_score": [3.51, 3.44, 3.370, 3.368, 3.31, 3.25, 3.20, 3.15],
            "score_gap_vs_k": [0.14, 0.07, 0.0, -0.002, -0.06, -0.12, -0.17, -0.22],
            "score_gap_vs_previous": [0.06, 0.07, 0.07, 0.002, 0.058, 0.06, 0.05, 0.05],
            "quality_penalty": [0.0] * 8,
            "selection_status": ["SELECTED", "SELECTED", "SELECTED"] + ["NOT_SELECTED"] * 5,
        }
    )
    selected_details = top_k[["model_rank", "ticker", "name", "model_score", "advisor_sector"]].copy()
    selected_details["official_industry_source"] = "KRX certified industry master"
    selected_details["advisor_sector_source"] = "advisor sector policy v2"
    selected_details["statement_scope"] = "CFS"
    selected_details["quarter_minus_2_period"] = "2025-Q4"
    selected_details["quarter_minus_1_period"] = "2026-Q1"
    selected_details["latest_quarter_period"] = "2026-Q2"
    selected_details["revenue_q_minus_2"] = 100_000_000_000
    selected_details["revenue_q_minus_1"] = 110_000_000_000
    selected_details["revenue_latest_q"] = 120_000_000_000
    selected_details["operating_income_q_minus_2"] = 10_000_000_000
    selected_details["operating_income_q_minus_1"] = 11_000_000_000
    selected_details["operating_income_latest_q"] = 12_000_000_000
    selected_details["quality_penalty_reason"] = "NONE"
    selected_details["Debt_to_Equity_log__contrib"] = 0.42
    selected_details["Revenue_acc2__contrib"] = 0.31

    valuation = pd.DataFrame(
        {
            "ticker": tickers,
            "name": names,
            "valuation_asof": "2026-08-20",
            "revenue_ttm": 440_000_000_000,
            "operating_income_ttm": 44_000_000_000,
            "parent_net_income_ttm": 32_000_000_000,
            "cfo_ttm": 48_000_000_000,
            "cfo_conversion_ttm": 1.09,
            "market_cap_asof": 800_000_000_000,
            "per_ttm": 25.0,
            "pbr": 3.2,
            "psr_ttm": 1.82,
            "ev_to_operating_income_ttm": 17.5,
        }
    )
    target = pd.DataFrame(
        {
            "ticker": tickers + ["CASH"],
            "name": names + ["현금성 자산"],
            "asset_class": ["EQUITY"] * 10 + ["CASH_EQUIVALENT_BUCKET"],
            "target_weight": [0.09] * 10 + [0.10],
            "illustrative_target_value": [7_188_000] * 10 + [7_987_792],
            "advisor_sector": sectors + ["현금성 자산"],
            "reference_price_asof": ["2026-08-20"] * 11,
            "reference_price": [30_000 + index * 1_000 for index in range(10)] + [None],
            "reference_target_qty": [239 - index for index in range(10)] + [None],
        }
    )
    comparison = pd.DataFrame(
        {
            "ticker": tickers + ["437350", "999999"],
            "name": names + ["RISE 미국단기투자등급회사채액티브", "기존 보유종목"],
            "transition_status": ["NEW_SELECTION"] * 8 + ["RESELECTED"] * 2 + ["CASH_EQUIVALENT", "DROPPED"],
            "current_value": [0] * 8 + [10_000_000, 5_000_000, 13_093_500, 2_000_000],
            "current_weight": [0.0] * 8 + [0.1264, 0.0632, 0.1655, 0.0253],
            "target_weight": [0.09] * 10 + [0.10, 0.0],
            "illustrative_target_value": [7_188_000] * 10 + [7_987_792, 0],
        }
    )
    current = pd.DataFrame(
        {
            "ticker": ["005930", "437350"],
            "name": ["삼성전자", "RISE 미국단기투자등급회사채액티브"],
            "asset_class": ["EQUITY", "CASH_EQUIVALENT"],
            "current_qty": [30, 1300],
            "broker_market_price": [82_000, 10_072],
            "broker_market_value": [2_460_000, 13_093_600],
            "current_weight": [0.0311, 0.1655],
        }
    )
    liquidation = pd.DataFrame(
        {
            "ticker": tickers[:9],
            "name": names[:9],
            "asset_class": ["EQUITY"] * 9,
            "broker_market_value": [7_000_000] * 9,
            "hypothetical_sell_commission": [1_050] * 9,
            "hypothetical_sell_tax": [14_000] * 9,
            "hypothetical_net_proceeds": [6_984_950] * 9,
        }
    )
    sector_current = pd.DataFrame(
        {
            "advisor_sector": ["정보기술 하드웨어", "현금성 자산"],
            "position_count": [1, 1],
            "sector_value": [2_460_000, 13_093_600],
            "sector_weight": [0.0311, 0.1655],
            "advisor_sector_source": ["advisor sector policy v2"] * 2,
        }
    )
    sector_target = pd.DataFrame(
        {
            "advisor_sector": ["반도체 장비", "전자부품", "기타 주식 섹터"],
            "position_count": [2, 2, 6],
            "sector_value": [14_376_000, 14_376_000, 43_128_000],
            "sector_weight": [0.18, 0.18, 0.54],
            "advisor_sector_source": ["advisor sector policy v2"] * 3,
        }
    )
    summary = {
        "mode": ADVISOR_MODE,
        "development_status": "PASS_CONTRACT_CORRECTION",
        "production_promotion_status": "BLOCKED_PENDING_HISTORICAL_VALIDATION",
        "score_parity_status": "PASS",
        "model_information_asof": "2026-08-18",
        "account_valuation_asof": "2026-08-20",
        "reference_price_asof": "2026-08-20",
        "gross_account_nav": 79_113_563,
        "equity_market_value": 65_458_400,
        "cash_equivalent_value": 13_093_500,
        "cash_value": 1_466_753,
        "liability_value": 0,
        "hypothetical_equity_net_proceeds": 65_317_664.44,
        "advisory_rebalance_capital": 79_877_917.44,
        "top_k": 10,
        "target_equity_weight": 0.90,
        "target_cash_equivalent_weight": 0.10,
        "selection_boundary_status": "FRAGILE",
        "validation_status": "INCOMPLETE",
        "coverage_status": "INCOMPLETE",
        "backtest": {
            "CAGR": 0.2069,
            "MDD": -0.3228,
            "Sharpe": 1.1,
            "quarterly_hit_rate": 0.61,
            "turnover": 0.9014,
            "total_trading_costs": 0.0833,
        },
        "prior_model_performance": {
            "performance_status": "PROVISIONAL_LEGACY_ARTIFACT",
            "target_date": "2026-03-31",
            "evaluation_end": "2026-08-18",
            "model_price_return": -0.0379,
            "model_mdd": -0.4385,
            "benchmark_status": "PARTIAL_PERIOD_MISMATCH",
        },
        "actual_account_performance": {
            "performance_status": "PROVISIONAL_ACCOUNT_PERFORMANCE",
            "activity_ledger_status": "NOT_AVAILABLE",
            "prior_gross_account_nav": 72_274_312,
            "current_gross_account_nav": 79_113_563,
            "endpoint_nav_change": 6_839_251,
            "endpoint_nav_change_ratio_unadjusted": 0.09462907,
            "account_performance_return": 0.09462907,
        },
        "MODEL_PROMOTION_BLOCKERS": [
            "HISTORICAL_PIT_UNIVERSE_UNAVAILABLE",
            "HISTORICAL_MCAP_TRADED_VALUE_UNAVAILABLE",
            "RETURN_COVERAGE_INCOMPLETE",
            "KRX300_HISTORY_INCOMPLETE",
            "HISTORICAL_SELL_TAX_SCHEDULE_UNAVAILABLE",
            "FRESH_START_BACKTEST_NOT_VALIDATED",
        ],
        "ADVISOR_REPORT_BLOCKERS": [],
        "PERFORMANCE_CERTIFICATION_WARNINGS": [
            "ACTIVITY_LEDGER_UNAVAILABLE",
            "PRIOR_MODEL_ARTIFACT_PROVISIONAL",
            "PRIOR_MODEL_BENCHMARK_PERIOD_MISMATCH",
            "ACTUAL_ACCOUNT_RETURN_NOT_CASHFLOW_ADJUSTED",
        ],
    }
    return {
        "summary": summary,
        "current_portfolio": current,
        "liquidation": liquidation,
        "top_k": top_k,
        "boundary_watchlist": boundary,
        "target_portfolio": target,
        "current_vs_target": comparison,
        "selected_details": selected_details,
        "valuation_qa": valuation,
        "sector_exposure": {"current": sector_current, "target": sector_target},
        "prior_model_performance": pd.DataFrame(),
        "actual_account_performance": pd.DataFrame(),
        "qa": {},
    }


class AdvisorReportingV2Tests(unittest.TestCase):
    def test_complete_unverified_report_passes_validator(self) -> None:
        report = generate_advisor_v2_html(**_inputs())
        result = validate_advisor_v2_html(report)

        self.assertTrue(result["pass"], result["failed_checks"])
        expected = list(BASE_SECTION_TITLES)
        expected[3] = UNVERIFIED_ACCOUNT_SECTION_TITLE
        self.assertEqual(result["h2_sections"], expected)
        self.assertIn(PHILOSOPHY_TEXT, report)

    def test_unverified_endpoint_is_not_presented_as_investment_return(self) -> None:
        report = generate_advisor_v2_html(**_inputs())

        self.assertIn("endpoint NAV change", report)
        self.assertIn("external-cashflow-unadjusted", report)
        self.assertIn("not an investment return", report)
        self.assertIn("endpoint_nav_change_ratio_unadjusted", report)
        self.assertIn("9.46%", report)
        self.assertNotIn("account_performance_return", report)
        self.assertNotIn("실제 수익률", report)
        self.assertNotIn("성과수익률", report)

    def test_provisional_backtest_numbers_do_not_leak_into_advisor_body(self) -> None:
        report = generate_advisor_v2_html(**_inputs())

        for term in ("CAGR", "MDD", "Sharpe", "turnover", "20.69%", "-32.28%", "90.14%", "8.33%"):
            self.assertNotIn(term, report)
        self.assertIn("역사 검증 상태", report)
        self.assertIn("데이터 범위", report)
        self.assertIn("production 승격의 주요 차단사유", report)
        self.assertIn("과거 시점별 투자대상군 자료 미확보", report)

    def test_boundary_and_selected_detail_contracts_are_visible(self) -> None:
        report = generate_advisor_v2_html(**_inputs())

        self.assertIn("10위 점수", report)
        self.assertIn("11위 점수", report)
        self.assertIn("10위−11위 차이", report)
        self.assertIn("경계 취약", report)
        self.assertIn('id="boundary-watchlist-table"', report)
        self.assertEqual(report.count('<article class="security-card">'), 10)
        for label in (
            "최근 3개 분기",
            "TTM 매출",
            "TTM 영업이익",
            "지배주주순이익 TTM",
            "CFO TTM",
            "CFO/영업이익",
            "시가총액",
            "밸류에이션 기준일",
            "PER(TTM)",
            "PBR",
            "PSR(TTM)",
            "EV/영업이익(TTM)",
            "품질 패널티",
            "팩터",
        ):
            self.assertIn(label, report)

    def test_user_facing_enums_are_translated(self) -> None:
        inputs = _inputs()
        inputs["target_portfolio"].loc[
            inputs["target_portfolio"]["asset_class"].eq("CASH_EQUIVALENT_BUCKET"),
            "ticker",
        ] = "CASH_EQUIVALENT_BUCKET"
        report = generate_advisor_v2_html(**inputs)

        for raw in (
            "CASH_EQUIVALENT_BUCKET",
            "NEW_SELECTION",
            "RESELECTED",
            "DROPPED",
            "PROVISIONAL_ACCOUNT_PERFORMANCE",
            "BLOCKED_PENDING_HISTORICAL_VALIDATION",
        ):
            self.assertNotIn(raw, re.sub(r'data-report-(?:contract|mode)="[^"]+"', "", report))
        for translated in ("현금성 자산", "신규 선발", "재선발", "미선발", "주문·체결 기능 미사용"):
            self.assertIn(translated, report)

    def test_accepts_base_mode_and_canonical_financial_diagnostic_pair(self) -> None:
        inputs = _inputs()
        inputs["summary"]["mode"] = "ADVISOR_FULL_RESET"
        combined = inputs.pop("selected_details")
        combined["quarter_minus_2_period"] = "2025Q4"
        combined["quarter_minus_1_period"] = "2026Q1"
        combined["latest_quarter_period"] = "2026Q2"
        diagnostic_columns = [
            "ticker",
            "name",
            "model_rank",
            "model_score",
            "quality_penalty_reason",
            "Debt_to_Equity_log__contrib",
            "Revenue_acc2__contrib",
            "official_industry_source",
            "advisor_sector",
            "advisor_sector_source",
        ]
        inputs["selected_details"] = {
            "selected_security_financials": combined.drop(
                columns=[column for column in diagnostic_columns if column in combined.columns and column not in {"ticker", "name", "model_rank"}]
            ),
            "selected_security_diagnostics": combined.loc[:, diagnostic_columns],
        }

        report = generate_advisor_v2_html(**inputs)
        self.assertTrue(validate_advisor_v2_html(report)["pass"])
        self.assertIn("2025Q4", report)

    def test_known_invalid_322000_industry_mapping_fails_closed(self) -> None:
        inputs = _inputs()
        mask = inputs["top_k"]["ticker"].eq("322000")
        inputs["top_k"].loc[mask, "official_industry_name"] = "반도체 제조업"

        with self.assertRaisesRegex(ValueError, "322000 known-invalid"):
            generate_advisor_v2_html(**inputs)

    def test_print_tables_are_balanced_and_narrow(self) -> None:
        report = generate_advisor_v2_html(**_inputs())
        result = validate_advisor_v2_html(report)

        self.assertIn("orphans:3", report)
        self.assertIn("widows:3", report)
        self.assertIn("break-inside:avoid-page", report)
        self.assertIn("table-layout:fixed", report)
        self.assertTrue(all(count >= 3 for count in result["continuation_row_counts"]))
        self.assertLessEqual(result["maximum_table_column_count"], 8)

    def test_performance_warnings_do_not_block_report_but_report_blockers_do(self) -> None:
        inputs = _inputs()
        report = generate_advisor_v2_html(**inputs)
        self.assertIn("활동원장 미제공", report)

        blocked = _inputs()
        blocked["summary"]["ADVISOR_REPORT_BLOCKERS"] = ["INVALID_INDUSTRY_MAPPING"]
        with self.assertRaisesRegex(ValueError, "업종 매핑 검증 실패"):
            generate_advisor_v2_html(**blocked)

    def test_validator_rejects_privacy_section_and_provisional_metric_leaks(self) -> None:
        report = generate_advisor_v2_html(**_inputs())

        leaked_path = report.replace("분기별 투자판단", r"C:\Users\person\private 분기별 투자판단", 1)
        self.assertFalse(validate_advisor_v2_html(leaked_path)["checks"]["privacy_and_paths_absent"])

        leaked_metric = report.replace("역사 검증 상태", "CAGR 20.69% · 역사 검증 상태", 1)
        self.assertFalse(validate_advisor_v2_html(leaked_metric)["checks"]["provisional_statistics_absent"])

        wrong_section = report.replace(UNVERIFIED_ACCOUNT_SECTION_TITLE, "실제 계좌 성과", 1)
        self.assertFalse(validate_advisor_v2_html(wrong_section)["checks"]["exact_eleven_sections_in_order"])


if __name__ == "__main__":
    unittest.main()
