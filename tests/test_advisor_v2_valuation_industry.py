from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

from scripts.advisor.v2_industry import (
    IndustryContractError,
    build_advisor_sector_map_from_exact_ticker_policy,
    build_industry_artifacts,
    build_industry_mapping_qa,
    build_security_registry_from_exact_sources,
    build_sector_exposure,
    canonicalize_official_industry_reference,
    canonicalize_security_registry,
    write_industry_artifacts,
)
from scripts.advisor.v2_valuation import (
    ValuationContractError,
    build_exact_quarterly_financials_from_dart_cache,
    build_valuation_artifacts,
    write_valuation_artifacts,
)


ASOF = "2026-08-18"


def _selected() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ["005930"],
            "name": ["삼성전자"],
            "model_rank": [1],
            "model_score": [3.5],
            "quality_penalty_total": [0.0],
            "quality_penalty_reason": [pd.NA],
            "Revenue_acc2__contrib": [0.4],
        }
    )


def _market() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ["005930"],
            "market_cap": [1000.0],
            "used_px_date": [ASOF],
            "per": [999.0],
            "pbr": [888.0],
        }
    )


def _quarterly(**overrides: object) -> pd.DataFrame:
    periods = [(2025, 3), (2025, 4), (2026, 1), (2026, 2)]
    frame = pd.DataFrame(
        {
            "ticker": ["005930"] * 4,
            "year": [year for year, _ in periods],
            "quarter": [quarter for _, quarter in periods],
            "financial_information_asof": [ASOF] * 4,
            "statement_scope": ["CFS"] * 4,
            "source_received_date": [
                "2025-11-14",
                "2026-03-31",
                "2026-05-15",
                "2026-08-14",
            ],
            "revenue_quarter": [100.0] * 4,
            "operating_income_quarter": [20.0] * 4,
            "parent_net_income_quarter": [10.0] * 4,
            "cfo_quarter": [15.0] * 4,
            "parent_equity_latest": [170.0, 180.0, 190.0, 200.0],
            "interest_bearing_debt_latest": [50.0] * 4,
            "preferred_equity_latest": [2.0] * 4,
            "noncontrolling_interest_latest": [5.0] * 4,
            "cash_and_cash_equivalents_latest": [20.0] * 4,
        }
    )
    for column, value in overrides.items():
        frame[column] = value
    return frame


def _security_registry() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": ["322000", "005930"],
            "name": ["HD현대에너지솔루션", "삼성전자"],
            "corp_code": pd.Series(["01199550", "00126380"], dtype="string"),
            "security_id": pd.Series(["322000", "005930"], dtype="string"),
        }
    )


def _official_reference() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": pd.Series(["322000", "005930"], dtype="string"),
            "industry_code": pd.Series(["032601", "032604"], dtype="string"),
            "industry_name": ["반도체 제조업", "통신 및 방송 장비 제조업"],
        }
    )


def _override() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": pd.Series(["322000"], dtype="string"),
            "corp_code": pd.Series(["01199550"], dtype="string"),
            "security_id": pd.Series(["322000"], dtype="string"),
            "official_industry_code": ["ISSUER_PRIMARY_SOLAR_MODULE"],
            "official_industry_name": ["태양광 모듈 제조 및 판매"],
            "official_industry_source": [
                "HD현대에너지솔루션 2024 지속가능경영보고서 기업개요 대표업종"
            ],
            "taxonomy_namespace": ["ISSUER_DISCLOSED_PRIMARY_BUSINESS"],
            "taxonomy_version": ["HD_ESG_2024_REPORT_PUBLISHED_2025"],
            "override_reason": ["KNOWN_INVALID_KRX_SEMICONDUCTOR_MAPPING"],
        }
    )


def _advisor_map() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": pd.Series(["322000", "005930"], dtype="string"),
            "corp_code": pd.Series(["01199550", "00126380"], dtype="string"),
            "security_id": pd.Series(["322000", "005930"], dtype="string"),
            "advisor_sector": ["재생에너지·태양광", "반도체·전자"],
            "advisor_sector_source": ["ADVISOR_SECTOR_POLICY_V2"] * 2,
        }
    )


class AdvisorV2ValuationTests(unittest.TestCase):
    def test_valuation_formulas_reproduce_from_internal_inputs(self) -> None:
        artifacts = build_valuation_artifacts(
            _selected(),
            _market(),
            _quarterly(),
            model_information_asof=ASOF,
            valuation_asof=ASOF,
        )
        row = artifacts.valuation_qa.iloc[0]
        self.assertEqual(row["revenue_ttm"], 400.0)
        self.assertEqual(row["operating_income_ttm"], 80.0)
        self.assertEqual(row["parent_net_income_ttm"], 40.0)
        self.assertEqual(row["cfo_ttm"], 60.0)
        self.assertAlmostEqual(row["per_ttm"], 25.0)
        self.assertAlmostEqual(row["pbr"], 5.0)
        self.assertAlmostEqual(row["psr_ttm"], 2.5)
        self.assertAlmostEqual(row["enterprise_value"], 1037.0)
        self.assertAlmostEqual(row["ev_to_operating_income_ttm"], 1037.0 / 80.0)
        self.assertAlmostEqual(row["cfo_conversion_ttm"], 0.75)
        self.assertEqual(artifacts.qa["status"], "PASS")
        self.assertIsNone(artifacts.qa["advisor_report_blocker"])

    def test_nonpositive_denominators_are_reason_coded_na_not_blocker(self) -> None:
        quarterly = _quarterly()
        quarterly["revenue_quarter"] = 0.0
        quarterly["operating_income_quarter"] = -1.0
        quarterly["parent_net_income_quarter"] = 0.0
        quarterly["parent_equity_latest"] = -1.0
        artifacts = build_valuation_artifacts(
            _selected(), _market(), quarterly, model_information_asof=ASOF, valuation_asof=ASOF
        )
        row = artifacts.valuation_qa.iloc[0]
        self.assertTrue(pd.isna(row["per_ttm"]))
        self.assertTrue(pd.isna(row["pbr"]))
        self.assertTrue(pd.isna(row["psr_ttm"]))
        self.assertTrue(pd.isna(row["cfo_conversion_ttm"]))
        self.assertEqual(row["per_ttm_na_reason"], "DENOMINATOR_NONPOSITIVE")
        self.assertEqual(artifacts.qa["status"], "PASS_WITH_CONTRACTUAL_NA")
        self.assertIsNone(artifacts.qa["advisor_report_blocker"])

    def test_missing_ev_component_stays_na_without_proxy(self) -> None:
        quarterly = _quarterly()
        quarterly["preferred_equity_latest"] = pd.NA
        quarterly["Liabilities"] = 999999.0
        artifacts = build_valuation_artifacts(
            _selected(), _market(), quarterly, model_information_asof=ASOF, valuation_asof=ASOF
        )
        row = artifacts.valuation_qa.iloc[0]
        self.assertTrue(pd.isna(row["enterprise_value"]))
        self.assertTrue(pd.isna(row["ev_to_operating_income_ttm"]))
        self.assertEqual(row["enterprise_value_na_reason"], "PREFERRED_EQUITY_MISSING")
        self.assertFalse(row["ev_proxy_used"])
        self.assertEqual(artifacts.qa["status"], "PASS_WITH_CONTRACTUAL_NA")

    def test_provider_per_pbr_and_total_fields_are_not_used(self) -> None:
        artifacts = build_valuation_artifacts(
            _selected(), _market(), _quarterly(), model_information_asof=ASOF, valuation_asof=ASOF
        )
        row = artifacts.valuation_qa.iloc[0]
        self.assertNotEqual(row["per_ttm"], 999.0)
        self.assertNotEqual(row["pbr"], 888.0)
        self.assertFalse(row["provider_multiples_used"])
        self.assertFalse(row["total_net_income_fallback_used"])
        self.assertFalse(row["total_equity_fallback_used"])

    def test_future_filing_is_contract_violation_and_report_blocker(self) -> None:
        quarterly = _quarterly()
        quarterly.loc[quarterly.index[-1], "source_received_date"] = "2026-08-19"
        artifacts = build_valuation_artifacts(
            _selected(), _market(), quarterly, model_information_asof=ASOF, valuation_asof=ASOF
        )
        self.assertEqual(artifacts.valuation_qa.iloc[0]["valuation_status"], "INVALID_INPUT")
        self.assertEqual(artifacts.qa["status"], "FAIL_CONTRACT_VIOLATION")
        self.assertEqual(artifacts.qa["advisor_report_blocker"], "VALUATION_INPUT_CONTRACT_VIOLATION")

    def test_selected_outputs_include_recent_quarters_and_factor_contribution(self) -> None:
        artifacts = build_valuation_artifacts(
            _selected(), _market(), _quarterly(), model_information_asof=ASOF, valuation_asof=ASOF
        )
        financials = artifacts.selected_security_financials.iloc[0]
        self.assertEqual(financials["quarter_minus_2_period"], "2025Q4")
        self.assertEqual(financials["quarter_minus_1_period"], "2026Q1")
        self.assertEqual(financials["latest_quarter_period"], "2026Q2")
        self.assertEqual(financials["revenue_latest_q"], 100.0)
        self.assertIn("Revenue_acc2__contrib", artifacts.selected_security_diagnostics.columns)
        self.assertEqual(artifacts.selected_security_diagnostics.iloc[0]["quality_penalty"], 0.0)

    def test_artifact_writer_is_no_overwrite(self) -> None:
        artifacts = build_valuation_artifacts(
            _selected(), _market(), _quarterly(), model_information_asof=ASOF, valuation_asof=ASOF
        )
        with TemporaryDirectory() as tmp:
            paths = write_valuation_artifacts(artifacts, tmp)
            self.assertEqual(set(paths), {
                "valuation_qa_csv",
                "valuation_qa_json",
                "selected_security_financials",
                "selected_security_diagnostics",
            })
            qa = json.loads(paths["valuation_qa_json"].read_text(encoding="utf-8"))
            self.assertEqual(qa["contract_version"], "ADVISOR_FULL_RESET_V2_VALUATION_V1")
            with self.assertRaises(FileExistsError):
                write_valuation_artifacts(artifacts, tmp)

    def test_exact_raw_cache_quarterization_and_pit_lineage(self) -> None:
        def report_rows(corp: str, report: str, receipt: str, flows: tuple[float, ...]) -> pd.DataFrame:
            rows = []
            for (target, account_id), amount in zip(
                {
                    "revenue": "ifrs-full_Revenue",
                    "op": "dart_OperatingIncomeLoss",
                    "parent": "ifrs-full_ProfitLossAttributableToOwnersOfParent",
                    "cfo": "ifrs-full_CashFlowsFromUsedInOperatingActivities",
                }.items(),
                flows,
            ):
                rows.append(
                    {
                        "rcept_no": receipt + "000001",
                        "reprt_code": report,
                        "corp_code": corp,
                        "sj_div": "CF" if target == "cfo" else "IS",
                        "account_id": account_id,
                        "thstrm_amount": amount,
                        "thstrm_add_amount": amount,
                    }
                )
            stocks = {
                "ifrs-full_EquityAttributableToOwnersOfParent": 200.0,
                "dart_InterestBearingDebt": 50.0,
                "ifrs-full_NoncontrollingInterests": 5.0,
                "ifrs-full_CashAndCashEquivalents": 20.0,
            }
            for account_id, amount in stocks.items():
                rows.append(
                    {
                        "rcept_no": receipt + "000001",
                        "reprt_code": report,
                        "corp_code": corp,
                        "sj_div": "BS",
                        "account_id": account_id,
                        "thstrm_amount": amount,
                        "thstrm_add_amount": pd.NA,
                    }
                )
            return pd.DataFrame(rows)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            corp = "00126380"
            definitions = [
                (2025, "11012", "20250814", (200.0, 40.0, 20.0, 30.0)),
                (2025, "11014", "20251114", (300.0, 60.0, 30.0, 45.0)),
                (2025, "11011", "20260331", (400.0, 80.0, 40.0, 60.0)),
                (2026, "11013", "20260515", (100.0, 20.0, 10.0, 15.0)),
                (2026, "11012", "20260814", (200.0, 40.0, 20.0, 30.0)),
            ]
            for year, report, receipt, flows in definitions:
                path = root / (
                    f"finstate_all__corp={corp}__year={year}__reprt={report}"
                    f"__fs=CFS__asof={ASOF}.parquet"
                )
                report_rows(corp, report, receipt, flows).to_parquet(path, index=False)
            securities = pd.DataFrame(
                {
                    "ticker": ["005930"],
                    "name": ["삼성전자"],
                    "corp_code": pd.Series([corp], dtype="string"),
                }
            )
            result = build_exact_quarterly_financials_from_dart_cache(
                securities, root, information_asof=ASOF
            )
            ttm = result.panel.tail(4)
            self.assertEqual(ttm["revenue_quarter"].tolist(), [100.0] * 4)
            self.assertEqual(ttm["operating_income_quarter"].tolist(), [20.0] * 4)
            self.assertEqual(ttm["source_received_date"].max(), "2026-08-14")
            self.assertFalse(result.qa["network_used"])
            self.assertFalse(result.qa["raw_cache_modified"])


class AdvisorV2IndustryTests(unittest.TestCase):
    def test_registry_can_resolve_multiple_exact_sources_without_name_join(self) -> None:
        requested = _security_registry()[["ticker", "name"]]
        source_one = pd.DataFrame(
            {
                "ticker": pd.Series(["322000"], dtype="string"),
                "corp_code": pd.Series(["01199550"], dtype="string"),
            }
        )
        source_two = pd.DataFrame(
            {
                "ticker": pd.Series(["005930"], dtype="string"),
                "corp_code": pd.Series(["00126380"], dtype="string"),
            }
        )
        registry = build_security_registry_from_exact_sources(
            requested, [source_one, source_two]
        )
        self.assertEqual(registry["corp_code"].tolist(), ["01199550", "00126380"])
        self.assertEqual(registry["security_id"].tolist(), ["322000", "005930"])

    def test_exact_ticker_policy_expands_to_three_key_sector_map(self) -> None:
        result = build_advisor_sector_map_from_exact_ticker_policy(
            _security_registry(),
            {"322000": "재생에너지·태양광", "005930": "반도체·전자"},
            advisor_sector_source="ADVISOR_EXACT_TICKER_RISK_SECTOR_POLICY_V2",
        )
        self.assertEqual(result.loc[result["ticker"].eq("322000"), "corp_code"].iloc[0], "01199550")
        self.assertEqual(result.loc[result["ticker"].eq("322000"), "advisor_sector"].iloc[0], "재생에너지·태양광")

    def test_numeric_industry_code_is_rejected_to_prevent_collision(self) -> None:
        reference = pd.DataFrame(
            {"ticker": ["005930"], "industry_code": [32604], "industry_name": ["전자"]}
        )
        with self.assertRaisesRegex(IndustryContractError, "must be strings"):
            canonicalize_official_industry_reference(
                reference,
                official_industry_source="source",
                taxonomy_namespace="KRX",
                taxonomy_version="2026-03-29",
            )

    def test_registry_requires_one_to_one_exact_keys(self) -> None:
        registry = _security_registry()
        registry.loc[1, "corp_code"] = registry.loc[0, "corp_code"]
        with self.assertRaisesRegex(IndustryContractError, "not one-to-one"):
            canonicalize_security_registry(registry)

    def test_322000_semiconductor_mapping_fails_without_exact_override(self) -> None:
        mapping = build_industry_mapping_qa(
            _security_registry(),
            _official_reference(),
            _advisor_map(),
            official_industry_source="KRX_DATA_3133",
            taxonomy_namespace="KRX_DATA_3133_INDUSTRY6",
            taxonomy_version="2026-03-29",
        )
        row = mapping.loc[mapping["ticker"].eq("322000")].iloc[0]
        self.assertTrue(row["known_invalid_mapping"])
        self.assertEqual(row["official_join_status"], "KNOWN_INVALID_MAPPING")
        self.assertEqual(row["qa_status"], "FAIL")

    def test_322000_override_requires_all_three_exact_identifiers(self) -> None:
        wrong = _override()
        wrong.loc[0, "corp_code"] = "99999999"
        mapping = build_industry_mapping_qa(
            _security_registry(),
            _official_reference(),
            _advisor_map(),
            official_industry_source="KRX_DATA_3133",
            taxonomy_namespace="KRX_DATA_3133_INDUSTRY6",
            taxonomy_version="2026-03-29",
            issuer_primary_business_overrides=wrong,
        )
        row = mapping.loc[mapping["ticker"].eq("322000")].iloc[0]
        self.assertEqual(row["qa_status"], "FAIL")
        self.assertFalse(row["issuer_override_applied"])

    def test_exact_issuer_override_preserves_rejected_original_and_advisor_sector(self) -> None:
        mapping = build_industry_mapping_qa(
            _security_registry(),
            _official_reference(),
            _advisor_map(),
            official_industry_source="KRX_DATA_3133",
            taxonomy_namespace="KRX_DATA_3133_INDUSTRY6",
            taxonomy_version="2026-03-29",
            issuer_primary_business_overrides=_override(),
        )
        row = mapping.loc[mapping["ticker"].eq("322000")].iloc[0]
        self.assertEqual(row["official_industry_code"], "ISSUER_PRIMARY_SOLAR_MODULE")
        self.assertEqual(row["official_industry_name"], "태양광 모듈 제조 및 판매")
        self.assertEqual(row["rejected_original_official_industry_code"], "032601")
        self.assertEqual(row["rejected_original_official_industry_name"], "반도체 제조업")
        self.assertEqual(row["taxonomy_namespace"], "ISSUER_DISCLOSED_PRIMARY_BUSINESS")
        self.assertEqual(row["advisor_sector"], "재생에너지·태양광")
        self.assertFalse(row["known_invalid_mapping"])
        self.assertEqual(row["qa_status"], "PASS")

    def test_sector_weight_reconciles_to_equity_target(self) -> None:
        mapping = build_industry_mapping_qa(
            _security_registry(),
            _official_reference(),
            _advisor_map(),
            official_industry_source="KRX_DATA_3133",
            taxonomy_namespace="KRX_DATA_3133_INDUSTRY6",
            taxonomy_version="2026-03-29",
            issuer_primary_business_overrides=_override(),
        )
        positions = pd.DataFrame(
            {
                "ticker": ["322000", "005930", "CASH_BUCKET"],
                "asset_class": ["EQUITY", "EQUITY", "CASH_EQUIVALENT_BUCKET"],
                "target_weight": [0.45, 0.45, 0.10],
                "target_value": [45.0, 45.0, 10.0],
            }
        )
        exposure = build_sector_exposure(
            positions,
            mapping,
            weight_column="target_weight",
            value_column="target_value",
            weight_basis="ADVISOR_TARGET_EQUITY_WEIGHT",
            expected_equity_weight=0.90,
        )
        self.assertAlmostEqual(exposure["sector_weight"].sum(), 0.90)
        self.assertTrue(exposure["reconciliation_status"].eq("PASS").all())

    def test_industry_artifact_builder_and_no_overwrite_writer(self) -> None:
        current = pd.DataFrame(
            {
                "ticker": ["322000", "005930"],
                "asset_class": ["EQUITY", "EQUITY"],
                "current_weight": [0.20, 0.30],
                "current_value": [20.0, 30.0],
            }
        )
        target = pd.DataFrame(
            {
                "ticker": ["322000", "005930"],
                "asset_class": ["EQUITY", "EQUITY"],
                "target_weight": [0.45, 0.45],
                "target_value": [45.0, 45.0],
            }
        )
        artifacts = build_industry_artifacts(
            _security_registry(),
            _official_reference(),
            _advisor_map(),
            official_industry_source="KRX_DATA_3133",
            taxonomy_namespace="KRX_DATA_3133_INDUSTRY6",
            taxonomy_version="2026-03-29",
            current_positions=current,
            target_positions=target,
            issuer_primary_business_overrides=_override(),
        )
        self.assertEqual(artifacts.qa["status"], "PASS")
        self.assertAlmostEqual(artifacts.qa["target_sector_weight_total"], 0.90)
        with TemporaryDirectory() as tmp:
            paths = write_industry_artifacts(artifacts, tmp)
            self.assertTrue(all(path.is_file() for path in paths.values()))
            with self.assertRaises(FileExistsError):
                write_industry_artifacts(artifacts, tmp)


if __name__ == "__main__":
    unittest.main()
