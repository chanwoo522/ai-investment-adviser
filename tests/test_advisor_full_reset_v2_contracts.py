from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.advisor.full_reset_v2 import (
    CASH_RETURN_CONTRACT,
    build_account_valuation_snapshot,
    build_blocker_taxonomy,
    build_common_reference_price_snapshot,
    build_current_vs_target_v2,
    build_sector_exposure,
    build_top_k_boundary_watchlist,
    build_v2_target_portfolio,
    calculate_account_asof_liquidation,
)


class AdvisorFullResetV2ContractTests(unittest.TestCase):
    def test_account_values_and_liquidation_use_broker_account_asof(self) -> None:
        holdings = pd.DataFrame(
            [
                {
                    "ticker": "000001",
                    "name": "A",
                    "asset_class": "EQUITY",
                    "market": "KOSPI",
                    "current_qty": 2,
                    "broker_market_price": 100,
                    "broker_market_value": 200,
                    "official_price": 50,
                },
                {
                    "ticker": "437350",
                    "name": "현금성 구현수단",
                    "asset_class": "CASH_EQUIVALENT",
                    "market": "ETF",
                    "current_qty": 1,
                    "broker_market_price": 50,
                    "broker_market_value": 50,
                    "official_price": 60,
                },
            ]
        )
        snapshot = {
            "account_asof": "2026-08-20",
            "gross_account_nav": 260,
            "cash_balance_signed": 10,
        }
        current, current_summary = build_account_valuation_snapshot(holdings, snapshot)
        self.assertEqual(float(current.loc[current.ticker.eq("000001"), "account_asof_value"].iloc[0]), 200)
        self.assertEqual(current_summary["gross_account_nav"], 260)
        detail, summary = calculate_account_asof_liquidation(
            current,
            commission_rate=0.001,
            sell_tax_rate_by_market={"KOSPI": 0.002},
            default_sell_tax_rate=0.003,
        )
        self.assertAlmostEqual(summary["advisory_rebalance_capital"], 259.4)
        self.assertTrue(summary["all_equities_liquidated"])
        cash_equivalent = detail.loc[detail.ticker.eq("437350")].iloc[0]
        self.assertEqual(cash_equivalent.hypothetical_sell_tax, 0)

    def test_reference_price_date_is_latest_common_cutoff(self) -> None:
        prices = pd.DataFrame(
            {
                "date": pd.to_datetime(
                    ["2026-08-18", "2026-08-20", "2026-08-18", "2026-08-19"]
                ),
                "Close": [100, 110, 200, 210],
                "ticker": ["000001", "000001", "000002", "000002"],
            }
        )
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / "prices.parquet"
            prices.to_parquet(path, index=False)
            result, summary = build_common_reference_price_snapshot(
                path, ["000001", "000002"], requested_asof="2026-08-20"
            )
        self.assertEqual(summary["reference_price_asof"], "2026-08-18")
        self.assertEqual(set(result.reference_price), {100, 200})

    def test_target_floor_preserves_ten_percent_cash(self) -> None:
        top = pd.DataFrame(
            {
                "ticker": [f"{number:06d}" for number in range(1, 11)],
                "name": [f"S{number}" for number in range(1, 11)],
                "model_rank": range(1, 11),
                "model_score": [11 - number for number in range(1, 11)],
                "model_selected": True,
            }
        )
        prices = pd.DataFrame(
            {
                "ticker": top.ticker,
                "reference_price_asof": "2026-08-20",
                "reference_price": 101.0,
                "reference_price_source": "OFFICIAL_DAILY_CLOSE",
                "reference_price_status": "AVAILABLE_COMMON_CUTOFF",
            }
        )
        portfolio, summary = build_v2_target_portfolio(
            top,
            prices,
            advisory_rebalance_capital=10_000,
            target_cash_equivalent_weight=0.10,
            buy_commission_rate=0.001,
        )
        self.assertAlmostEqual(float(portfolio.target_weight.sum()), 1.0)
        self.assertGreaterEqual(summary["resulting_cash_equivalent_weight"], 0.10)
        cash = portfolio.loc[portfolio.ticker.eq("CASH_EQUIVALENT_BUCKET")].iloc[0]
        self.assertEqual(cash.cash_return_contract, CASH_RETURN_CONTRACT)
        self.assertEqual(cash.implementation_vehicle, "437350")
        self.assertTrue(pd.isna(cash.reference_target_qty))

    def test_missing_common_price_keeps_weights_and_sets_reference_qty_na(self) -> None:
        top = pd.DataFrame(
            {
                "ticker": ["000001", "000002"],
                "name": ["A", "B"],
                "model_rank": [1, 2],
                "model_score": [2.0, 1.0],
            }
        )
        prices = pd.DataFrame(
            {
                "ticker": ["000001", "000002"],
                "reference_price_asof": [pd.NA, pd.NA],
                "reference_price": [pd.NA, pd.NA],
                "reference_price_source": "OFFICIAL_DAILY_CLOSE",
                "reference_price_status": "NA_NO_COMMON_OFFICIAL_DATE",
            }
        )
        portfolio, summary = build_v2_target_portfolio(
            top,
            prices,
            advisory_rebalance_capital=1000,
            target_cash_equivalent_weight=0.10,
            buy_commission_rate=0.001,
        )
        equity = portfolio.loc[portfolio.asset_class.eq("EQUITY")]
        self.assertTrue(equity.reference_target_qty.isna().all())
        self.assertAlmostEqual(float(portfolio.target_weight.sum()), 1.0)
        self.assertEqual(summary["missing_reference_price_count"], 2)

    def test_boundary_window_and_taxonomy(self) -> None:
        scores = pd.DataFrame(
            {
                "ticker": [f"{number:06d}" for number in range(1, 17)],
                "name": [f"S{number}" for number in range(1, 17)],
                "model_rank": range(1, 17),
                "model_score": [10.0 - number / 100 for number in range(1, 17)],
                "quality_penalty_total": 0.0,
            }
        )
        watch, summary = build_top_k_boundary_watchlist(
            scores, top_k=10, fragile_threshold=0.02
        )
        self.assertEqual(watch["rank"].tolist(), list(range(8, 16)))
        self.assertEqual(summary["selection_boundary_status"], "FRAGILE")
        blockers = build_blocker_taxonomy(
            score_parity_pass=True,
            advisor_contract_checks={"VALUATION_LAYER_MISSING": True},
            activity_ledger_available=False,
        )
        self.assertNotIn(
            "437350_HISTORY_INCOMPLETE", blockers["MODEL_PROMOTION_BLOCKERS"]
        )
        self.assertIn(
            "437350_CUTOFF_PRICE_NON_KRX_PRIMARY_SOURCE",
            blockers["ACCOUNT_VALUATION_WARNINGS"],
        )

    def test_current_vs_target_is_compare_only_and_cash_is_one_bucket(self) -> None:
        current = pd.DataFrame(
            [
                {
                    "ticker": "000001",
                    "name": "A",
                    "asset_class": "EQUITY",
                    "current_qty": 2,
                    "account_asof_price": 100,
                    "account_asof_value": 200,
                    "current_weight": 0.4,
                },
                {
                    "ticker": "437350",
                    "name": "vehicle",
                    "asset_class": "CASH_EQUIVALENT",
                    "current_qty": 10,
                    "account_asof_price": 20,
                    "account_asof_value": 200,
                    "current_weight": 0.4,
                },
                {
                    "ticker": "ACCOUNT_CASH",
                    "name": "cash",
                    "asset_class": "CASH",
                    "current_qty": pd.NA,
                    "account_asof_price": pd.NA,
                    "account_asof_value": 100,
                    "current_weight": 0.2,
                },
            ]
        )
        target = pd.DataFrame(
            [
                {
                    "ticker": "000001",
                    "name": "A",
                    "asset_class": "EQUITY",
                    "model_rank": 1,
                    "model_score": 1.0,
                    "target_weight": 0.9,
                    "target_value": 450,
                    "illustrative_target_value": 400,
                    "reference_target_qty": 4,
                },
                {
                    "ticker": "CASH_EQUIVALENT_BUCKET",
                    "name": "현금성 자산",
                    "asset_class": "CASH_EQUIVALENT_BUCKET",
                    "model_rank": pd.NA,
                    "model_score": pd.NA,
                    "target_weight": 0.1,
                    "target_value": 50,
                    "illustrative_target_value": 100,
                    "reference_target_qty": pd.NA,
                },
            ]
        )
        result = build_current_vs_target_v2(current, target)
        equity = result.loc[result.ticker.eq("000001")].iloc[0]
        self.assertEqual(equity.transition_status, "RESELECTED")
        cash = result.loc[result.ticker.eq("CASH_EQUIVALENT_BUCKET")].iloc[0]
        self.assertEqual(float(cash.current_value), 300)
        self.assertEqual(cash.implementation_vehicle, "437350")

    def test_target_sector_weights_equal_equity_target_weight(self) -> None:
        positions = pd.DataFrame(
            {
                "ticker": ["000001", "000002", "CASH_EQUIVALENT_BUCKET"],
                "asset_class": ["EQUITY", "EQUITY", "CASH_EQUIVALENT_BUCKET"],
                "target_value": [45, 45, 10],
                "target_weight": [0.45, 0.45, 0.10],
            }
        )
        mapping = pd.DataFrame(
            {
                "ticker": ["000001", "000002"],
                "advisor_sector": ["S1", "S2"],
                "advisor_sector_source": ["EXACT", "EXACT"],
            }
        )
        exposure = build_sector_exposure(
            positions,
            mapping,
            scope="TARGET",
            value_column="target_value",
            weight_column="target_weight",
        )
        self.assertAlmostEqual(float(exposure.sector_weight.sum()), 0.9)


if __name__ == "__main__":
    unittest.main()
