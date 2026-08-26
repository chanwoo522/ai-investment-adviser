from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.financials.build_public_financial_metrics import build_public_financial_metrics
from scripts.financials.build_share_count_history import (
    CapitalEvent,
    implied_weighted_shares_from_disclosed_eps,
    infer_retroactive_adjustment_factor,
    listed_shares_from_market_cap,
    weighted_average_from_events,
)
from scripts.financials.calculate_per import (
    calculate_per_ttm,
    select_official_close_at_or_before,
)
from scripts.financials.calculate_ttm_eps import calculate_basic_eps_ttm
from scripts.financials.calculate_ttm_net_income import calculate_ttm_net_income
from scripts.financials.collect_dart_financial_facts import load_selected_equities
from scripts.financials.parse_eps_notes import parse_eps_note_tables
from scripts.financials.resolve_reporting_periods import (
    ReportingPeriodResolution,
    actual_days,
    ttm_dependency_keys,
    validate_period_bridge,
)
from scripts.financials.resolve_share_classes import (
    allocate_profit_to_participating_classes,
    resolve_share_classes,
)
from scripts.subscriber_report.build_financial_complete_report import (
    PUBLIC_FORBIDDEN_METHOD_TERMS,
    build_financial_complete_html,
)


class GenericFinancialMetricsEngineTest(unittest.TestCase):
    def test_selected_equities_are_dynamic_and_ordered(self) -> None:
        frame = pd.DataFrame(
            [
                {"ticker": "100003", "name": "감마", "model_rank": 3, "model_score": 1.0, "target_weight": .2, "target_value": 20, "model_selected": True, "asset_class": "EQUITY"},
                {"ticker": "100001", "name": "알파", "model_rank": 1, "model_score": 3.0, "target_weight": .4, "target_value": 40, "model_selected": True, "asset_class": "EQUITY"},
                {"ticker": "100002", "name": "베타", "model_rank": 2, "model_score": 2.0, "target_weight": .4, "target_value": 40, "model_selected": False, "asset_class": "EQUITY"},
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "targets.csv"
            frame.to_csv(path, index=False)
            selected = load_selected_equities(path)
        self.assertEqual(selected["ticker"].tolist(), ["100001", "100003"])

    def test_period_dependencies_cover_q1_h1_q3_and_fy(self) -> None:
        self.assertEqual(ttm_dependency_keys(fiscal_year=2031, report_type="Q1"), ("FY2030", "Q1_2031", "Q1_2030_COMPARATIVE"))
        self.assertEqual(ttm_dependency_keys(fiscal_year=2031, report_type="H1"), ("FY2030", "H1_2031", "H1_2030_COMPARATIVE"))
        self.assertEqual(ttm_dependency_keys(fiscal_year=2031, report_type="Q3"), ("FY2030", "M9_2031", "M9_2030_COMPARATIVE"))
        self.assertEqual(ttm_dependency_keys(fiscal_year=2031, report_type="FY"), ("FY2031", "FY2031", None))

    def test_actual_days_support_non_calendar_fiscal_year(self) -> None:
        self.assertEqual(actual_days("2030-04-01", "2031-03-31"), 365)
        self.assertEqual(actual_days("2030-01-01", "2031-03-31"), 455)

    def test_cfs_and_ofs_each_validate_without_scope_mixing(self) -> None:
        for scope in ("CFS", "OFS"):
            resolution = ReportingPeriodResolution(
                security_id="100001", statement_scope=scope, latest_financial_period="2031FY",
                fiscal_year_end="2031-03-31", latest_report_type="FY",
                current_ytd_period="2030-04-01/2031-03-31", prior_comparable_ytd_period="",
                prior_fiscal_year=2031, prior_fy_key="FY2031", current_key="FY2031",
                prior_comparable_key=None, latest_receipt_no="R", latest_receipt_date="2031-05-01",
            )
            facts = pd.DataFrame([
                {"period_key": "FY2031", "statement_scope": scope, "period_start": "2030-04-01", "period_end": "2031-03-31"},
            ])
            validate_period_bridge(facts, resolution)

    def test_period_bridge_rejects_scope_mixing(self) -> None:
        resolution = ReportingPeriodResolution(
            security_id="100001", statement_scope="CFS", latest_financial_period="2031H1",
            fiscal_year_end="2030-12-31", latest_report_type="H1",
            current_ytd_period="2031-01-01/2031-06-30",
            prior_comparable_ytd_period="2030-01-01/2030-06-30", prior_fiscal_year=2030,
            prior_fy_key="FY2030", current_key="H1_2031",
            prior_comparable_key="H1_2030_COMPARATIVE", latest_receipt_no="R", latest_receipt_date="2031-08-10",
        )
        facts = pd.DataFrame([
            {"period_key": "FY2030", "statement_scope": "CFS", "period_start": "2030-01-01", "period_end": "2030-12-31"},
            {"period_key": "H1_2031", "statement_scope": "CFS", "period_start": "2031-01-01", "period_end": "2031-06-30"},
            {"period_key": "H1_2030_COMPARATIVE", "statement_scope": "OFS", "period_start": "2030-01-01", "period_end": "2030-06-30"},
        ])
        with self.assertRaisesRegex(ValueError, "scope mixing"):
            validate_period_bridge(facts, resolution)

    def test_ttm_net_income_bridge_and_fy(self) -> None:
        for report_type in ("Q1", "H1", "Q3"):
            with self.subTest(report_type=report_type):
                value, meta = calculate_ttm_net_income(report_type=report_type, prior_fy_value=100, current_value=70, prior_comparable_value=40)
                self.assertEqual(value, 130)
                self.assertEqual(meta["formula"], "PRIOR_FY_PLUS_CURRENT_YTD_MINUS_PRIOR_COMPARABLE_YTD")
        fy, _ = calculate_ttm_net_income(report_type="FY", prior_fy_value=0, current_value=140, prior_comparable_value=None)
        self.assertEqual(fy, 140)

    def test_latest_comparative_retroactive_factor_is_accepted(self) -> None:
        factor, status = infer_retroactive_adjustment_factor(
            original_eps=500, latest_comparative_eps=100,
            original_numerator=5_000_000, latest_comparative_numerator=5_000_000,
        )
        self.assertEqual(factor, 5)
        self.assertTrue(status.startswith("PASS"))

    def test_disclosed_eps_inverse_respects_rounding_interval(self) -> None:
        resolved = implied_weighted_shares_from_disclosed_eps(numerator=1_000_000, disclosed_eps=10)
        self.assertGreaterEqual(resolved.selected_integer_candidate, resolved.integer_candidate_min)
        self.assertLessEqual(resolved.selected_integer_candidate, resolved.integer_candidate_max)
        self.assertEqual(resolved.source_tier, "DISCLOSED_EPS_INVERSE_INTEGER_WITH_ROUNDING_INTERVAL")

    def test_eps_note_table_parser(self) -> None:
        parsed = parse_eps_note_tables(
            document_html="""<h3>주당이익</h3><table><tr><th>구분</th><th>2031H1</th></tr><tr><td>가중평균유통보통주식수</td><td>1,234주</td></tr></table>""",
            approved_titles=("주당이익",),
            approved_rows={"weighted_shares": ("가중평균유통보통주식수",)},
        )
        self.assertEqual(parsed[0].value, 1234)

    def test_listed_shares_from_market_cap(self) -> None:
        self.assertEqual(listed_shares_from_market_cap(market_cap=12_300_000, close_price=1_230), 10_000)

    def test_share_day_bridge_and_generic_action_labels(self) -> None:
        event_types = (
            "PAID_INCREASE", "BONUS_ISSUE", "CAPITAL_REDUCTION", "STOCK_SPLIT", "REVERSE_SPLIT",
            "CONVERSION_RIGHT_EXERCISE", "TREASURY_ACQUISITION", "TREASURY_DISPOSAL", "TREASURY_CANCELLATION",
        )
        for event_type in event_types:
            with self.subTest(event_type=event_type):
                event = CapitalEvent("X", event_type, None, "2031-01-03", None, 100, 200, 100)
                self.assertGreater(weighted_average_from_events(
                    period_start="2031-01-01", period_end="2031-01-04",
                    opening_outstanding_shares=100, events=[event],
                ), 100)

    def test_ttm_eps_uses_actual_days_and_retroactive_factor(self) -> None:
        eps, meta = calculate_basic_eps_ttm(
            report_type="H1", prior_fy_profit=3650, current_profit=1810, prior_comparable_profit=1800,
            prior_fy_shares=100, current_shares=500, prior_comparable_shares=500,
            prior_fy_start="2030-01-01", prior_fy_end="2030-12-31",
            current_start="2031-01-01", current_end="2031-06-30",
            prior_comparable_start="2030-01-01", prior_comparable_end="2030-06-30",
            prior_fy_retroactive_factor=5,
        )
        self.assertAlmostEqual(meta["weighted_average_ordinary_shares_ttm"], 500)
        self.assertAlmostEqual(eps, (3650 + 1810 - 1800) / 500)

    def test_share_class_resolution_single_and_active_preferred(self) -> None:
        ordinary_only = pd.DataFrame([{"ticker": "100001", "name": "알파", "security_type": "common"}])
        single = resolve_share_classes(ticker="100001", name="알파", security_master=ordinary_only)
        self.assertTrue(single.market_cap_fallback_compatible)
        two_classes = pd.concat([
            ordinary_only,
            pd.DataFrame([{"ticker": "100011", "name": "알파우", "security_type": "preferred"}]),
        ], ignore_index=True)
        status = pd.DataFrame([
            {"se": "보통주", "distb_stock_co": "1000"},
            {"se": "우선주", "distb_stock_co": "100"},
        ])
        multiple = resolve_share_classes(ticker="100001", name="알파", security_master=two_classes, stock_status=status)
        self.assertFalse(multiple.market_cap_fallback_compatible)

    def test_inactive_preferred_does_not_block_single_ordinary_contract(self) -> None:
        master = pd.DataFrame([
            {"ticker": "100001", "name": "알파", "security_type": "common"},
            {"ticker": "100011", "name": "알파우", "security_type": "preferred"},
        ])
        status = pd.DataFrame([{"se": "우선주", "distb_stock_co": "0"}])
        resolved = resolve_share_classes(ticker="100001", name="알파", security_master=master, stock_status=status)
        self.assertTrue(resolved.numerator_parent_profit_fallback_allowed)

    def test_two_class_profit_allocation(self) -> None:
        allocated = allocate_profit_to_participating_classes(
            profit_available_to_common_and_participating_classes=220,
            weighted_shares_by_class={"ordinary": 100, "preferred": 10},
            dividend_rights_by_class={"ordinary": 1, "preferred": 2},
            preferred_dividends_by_class={"ordinary": 0, "preferred": 20},
        )
        self.assertAlmostEqual(allocated["ordinary"], 200 * 100 / 120)
        self.assertAlmostEqual(sum(allocated.values()), 220)

    def test_participating_convertible_and_dormant_classes(self) -> None:
        master = pd.DataFrame([
            {"ticker": "100001", "name": "알파", "security_type": "common"},
            {"ticker": "100011", "name": "알파우", "security_type": "preferred"},
        ])
        participating = pd.DataFrame([
            {"se": "참가적 전환우선주", "istc_totqy": "100", "distb_stock_co": "90", "rights_effective": "true"},
        ])
        active = resolve_share_classes(ticker="100001", name="알파", security_master=master, stock_status=participating)
        self.assertEqual(active.active_participating_class_count, 1)
        self.assertEqual(active.active_convertible_preferred_class_count, 1)
        dormant = pd.DataFrame([
            {"se": "참가적 전환우선주", "istc_totqy": "0", "distb_stock_co": "0", "rights_effective": "true"},
        ])
        inactive = resolve_share_classes(ticker="100001", name="알파", security_master=master, stock_status=dormant)
        self.assertEqual(inactive.active_preferred_class_count, 0)

    def test_share_classes_use_latest_cutoff_eligible_snapshot_only(self) -> None:
        master = pd.DataFrame([
            {"ticker": "100001", "name": "알파", "security_type": "common"},
            {"ticker": "100011", "name": "알파우", "security_type": "preferred"},
        ])
        history = pd.DataFrame([
            {"business_year": "2030", "report_code": "11011", "se": "우선주", "istc_totqy": "100", "distb_stock_co": "100"},
            {"business_year": "2031", "report_code": "11013", "se": "우선주", "istc_totqy": "100", "distb_stock_co": "100"},
            {"business_year": "2031", "report_code": "11012", "se": "우선주", "istc_totqy": "0", "distb_stock_co": "0"},
        ])
        resolved = resolve_share_classes(ticker="100001", name="알파", security_master=master, stock_status=history)
        self.assertEqual(resolved.active_preferred_class_count, 0)

    def test_per_priority_loss_fallback_and_recent_prior_close(self) -> None:
        primary = calculate_per_ttm(official_close_price=100, price_observation_date="2031-08-18", basic_eps_ttm=10, compatible_market_cap=1000, compatible_net_income_ttm=50)
        self.assertEqual(primary.method, "PRICE_DIV_EPS")
        fallback = calculate_per_ttm(official_close_price=100, price_observation_date="2031-08-18", basic_eps_ttm=None, compatible_market_cap=1000, compatible_net_income_ttm=50)
        self.assertEqual(fallback.method, "MCAP_DIV_NET_INCOME")
        loss = calculate_per_ttm(official_close_price=100, price_observation_date="2031-08-18", basic_eps_ttm=-1, compatible_market_cap=1000, compatible_net_income_ttm=-50)
        self.assertEqual(loss.per_status, "LOSS")
        observed_date, close = select_official_close_at_or_before(
            pd.DataFrame({"date": ["2031-08-15", "2031-08-19"], "close": [90, 110]}),
            price_asof="2031-08-18", maximum_lookback_days=5,
        )
        self.assertEqual((observed_date, close), ("2031-08-15", 90))

    def test_public_metrics_schema_and_report_have_no_method_text(self) -> None:
        private = pd.DataFrame([
            {"ticker": "100001", "name": "알파", "model_rank": 1, "eps_ttm": 123.4, "net_income_ttm": 1_200_000_000, "per_ttm": 8.125, "per_status": "PASS"},
        ])
        public = build_public_financial_metrics(private)
        self.assertEqual(list(public), ["ticker", "name", "eps_ttm", "net_income_ttm", "per_ttm"])
        html = """<html><body><section id='selected-details'><article class='security-card'><div class='security-heading'><h3>1. 알파 <small>100001</small></h3></div><div class='metrics-grid'><div class='metric'><span>EPS</span><strong>NA</strong></div><div class='metric'><span>당기순이익</span><strong>NA</strong></div><div class='metric'><span>PER</span><strong>NA</strong></div></div><p class='formula-note'>method formula source</p></article></section></body></html>"""
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp) / "parent.html"
            output = Path(temp) / "output.html"
            parent.write_text(html, encoding="utf-8")
            build_financial_complete_html(parent_html=parent, private_metrics=private, output_path=output)
            rendered = output.read_text(encoding="utf-8")
        self.assertIn("123원", rendered)
        self.assertTrue(all(term.lower() not in rendered.lower() for term in PUBLIC_FORBIDDEN_METHOD_TERMS))

    def test_engine_source_has_no_security_or_selected_count_literals(self) -> None:
        root = Path(__file__).resolve().parents[1] / "scripts" / "financials"
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    self.assertNotRegex(node.value, r"(?<!\d)\d{6}(?!\d)")
                if isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and "selected" in node.left.id.lower() and "count" in node.left.id.lower():
                    self.fail(f"selected count comparison is hard-coded in {path.name}")


if __name__ == "__main__":
    unittest.main()
