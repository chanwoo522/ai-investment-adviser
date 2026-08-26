from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from scripts.data_pipeline.collect_selected_earnings_denominators import (
    Context,
    Fact,
    Instance,
    _comparative_restatement,
    _period_facts,
    select_per_method,
    share_day_ttm,
    ttm_dependency_contract,
    ttm_ytd_bridge,
)


def _instance(*, scope: str, concept: str) -> Instance:
    scope_member = "ConsolidatedMember" if scope == "CFS" else "SeparateMember"
    context = Context(
        identifier="ctx",
        start=date(2025, 1, 1),
        end=date(2025, 12, 31),
        instant=None,
        dimensions=(("ConsolidatedAndSeparateFinancialStatementsAxis", scope_member),),
    )
    fact = Fact(
        local_name=concept,
        namespace="https://xbrl.ifrs.org/taxonomy/2025-03-27/ifrs-full",
        value=Decimal("100"),
        context=context,
        context_ref="ctx",
        unit_ref="KRW",
        decimals="0",
    )
    return Instance(
        receipt_no="20260818000000",
        receipt_date="2026-08-18",
        report_key="FY2025",
        business_year=2025,
        report_code="11011",
        taxonomy_version="2025",
        schema_refs=(),
        source_sha256="0" * 64,
        source_zip=Path("source.zip"),
        contexts={"ctx": context},
        facts=(fact,),
        units={"KRW": ("iso4217:KRW",)},
        labels={},
        presentation_roles={},
    )


class SelectedEarningsDenominatorTests(unittest.TestCase):
    def test_2026_q2_dependency_has_no_future_quarter(self) -> None:
        contract = ttm_dependency_contract(2026, 2)
        self.assertEqual(contract["reports"], ["FY2025", "H1_2026", "H1_2025_COMPARATIVE"])
        self.assertEqual(contract["future_quarter_dependencies"], [])
        self.assertNotIn("Q3", "|".join(contract["reports"]))
        self.assertNotIn("Q4", "|".join(contract["reports"]))

    def test_q2_ttm_uses_annual_plus_current_ytd_minus_prior_ytd(self) -> None:
        self.assertEqual(ttm_ytd_bridge(1_000, 600, 450), 1_150)

    def test_share_day_ttm_uses_actual_period_days(self) -> None:
        weighted, share_days, days = share_day_ttm(
            fy_shares=100,
            fy_start=date(2025, 1, 1),
            fy_end=date(2025, 12, 31),
            current_ytd_shares=120,
            current_ytd_start=date(2026, 1, 1),
            current_ytd_end=date(2026, 6, 30),
            prior_ytd_shares=90,
            prior_ytd_start=date(2025, 1, 1),
            prior_ytd_end=date(2025, 6, 30),
        )
        self.assertEqual(days, 365)
        self.assertEqual(share_days, 100 * 365 - 90 * 181 + 120 * 181)
        self.assertAlmostEqual(weighted, share_days / 365)

    def test_per_price_eps_has_priority_and_fallback_is_eps_missing_only(self) -> None:
        value, formula, status = select_per_method(
            price=20_000, eps_ttm=1_000, market_cap_for_per=2_000_000, net_income_ttm=100_000
        )
        self.assertEqual((value, formula, status), (20.0, "PRICE_DIV_EPS", "PASS"))
        value, formula, status = select_per_method(
            price=20_000, eps_ttm=None, market_cap_for_per=2_000_000, net_income_ttm=100_000
        )
        self.assertEqual((value, formula, status), (20.0, "MCAP_DIV_NET_INCOME", "PASS"))

    def test_non_positive_earnings_is_loss(self) -> None:
        self.assertEqual(
            select_per_method(
                price=20_000, eps_ttm=-1, market_cap_for_per=2_000_000, net_income_ttm=100_000
            ),
            (None, None, "LOSS"),
        )

    def test_cfs_never_falls_back_to_total_profit_loss(self) -> None:
        values, _ = _period_facts(
            _instance(scope="CFS", concept="ProfitLoss"),
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            scope="CFS",
        )
        self.assertEqual(values["net_income"]["status"], "NO_EXACT_FACT")

    def test_ofs_uses_profit_loss_only_when_scope_is_ofs(self) -> None:
        values, _ = _period_facts(
            _instance(scope="OFS", concept="ProfitLoss"),
            period_start=date(2025, 1, 1),
            period_end=date(2025, 12, 31),
            scope="OFS",
        )
        self.assertEqual(values["net_income"]["status"], "PASS")

    def test_restatement_is_unresolved_without_original_exact_fact_set(self) -> None:
        original = {
            "basic_numerator": {"status": "PASS", "value": 10},
            "weighted_shares": {"status": "NO_EXACT_FACT", "value": None},
            "basic_eps": {"status": "PASS", "value": 1},
        }
        latest = {
            "basic_numerator": {"status": "PASS", "value": 10},
            "weighted_shares": {"status": "PASS", "value": 10},
            "basic_eps": {"status": "PASS", "value": 1},
        }
        restated, status = _comparative_restatement(original, latest)
        self.assertIsNone(restated)
        self.assertIn("UNAVAILABLE", status)


if __name__ == "__main__":
    unittest.main()
