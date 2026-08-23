from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd
import yaml

from scripts.advisor.fresh_start_backtest import (
    DEFAULT_COMMISSION_RATE,
    DEFAULT_SELL_TAX_RATE,
    FRESH_START_VARIANT,
    LEGACY_VARIANT,
    run_fresh_start_comparison,
)


class FreshStartBacktestTests(unittest.TestCase):
    def _inputs(self, root: Path) -> dict[str, Path]:
        feature_rows = []
        scores = {
            "2020-01-31": {"000001": 10, "000002": 9, "000003": 8, "000004": 7, "437350": 100},
            "2020-04-30": {"000001": 10, "000002": 1, "000003": 9, "000004": 8, "437350": 100},
        }
        for rebalance_month, cross_section in scores.items():
            for ticker, signal in cross_section.items():
                feature_rows.append(
                    {
                        "rebalance_month": rebalance_month,
                        "ticker": ticker,
                        "signal": signal,
                        "NetIncome_ttm": 1.0,
                        "NetIncome_acc2": 1.0,
                        "CFO_warn": 0,
                        "CFO_isnull": 0,
                    }
                )
        features = root / "features.csv"
        pd.DataFrame(feature_rows).to_csv(features, index=False)

        return_rows = []
        monthly_returns = {
            "000001": [1.00, 0.10, 0.02, 0.03, 0.04, -0.01],
            "000002": [1.00, 0.00, 0.02, 0.01, -0.02, 0.01],
            "000003": [1.00, -0.10, 0.01, 0.02, 0.06, 0.03],
            "000004": [1.00, 0.05, -0.01, 0.02, 0.01, 0.02],
            "437350": [0.001] * 6,
        }
        months = pd.date_range("2020-01-31", "2020-06-30", freq="ME")
        for ticker, values in monthly_returns.items():
            for month, value in zip(months, values):
                return_rows.append({"ticker": ticker, "month_end": month, "ret_1m": value})
        returns = root / "returns.csv"
        pd.DataFrame(return_rows).to_csv(returns, index=False)

        master = root / "master.csv"
        pd.DataFrame(
            [
                {"ticker": "000001", "name": "A", "sector": "Tech", "asset_class": "EQUITY", "top_k_eligible": True},
                {"ticker": "000002", "name": "B", "sector": "Finance", "asset_class": "EQUITY", "top_k_eligible": True},
                {"ticker": "000003", "name": "C", "sector": "Tech", "asset_class": "EQUITY", "top_k_eligible": True},
                {"ticker": "000004", "name": "D", "sector": "Industrial", "asset_class": "EQUITY", "top_k_eligible": True},
                {
                    "ticker": "437350",
                    "name": "RISE 미국단기투자등급회사채액티브",
                    "sector": "Cash",
                    "asset_class": "CASH_EQUIVALENT",
                    "top_k_eligible": False,
                },
            ]
        ).to_csv(master, index=False)

        config = root / "strategies.yaml"
        config.write_text(
            yaml.safe_dump(
                {
                    "strategies": {
                        LEGACY_VARIANT: {
                            "weights": {"signal": 1.0},
                            "filters": {},
                            "scoring": {
                                "use_robust_z": False,
                                "clip_z": 4.0,
                                "hold_bonus": 0.5,
                                "quality_soft_penalty": {
                                    "enabled": True,
                                    "netincome_ttm_nonpositive_penalty": -0.25,
                                    "netincome_acc2_negative_penalty": -0.15,
                                    "cfo_warn_penalty": -0.15,
                                    "cfo_isnull_penalty": -0.10,
                                },
                                "expectation_overlay": {"enabled": False},
                            },
                            "selection": {"portfolio_size": 2, "keep_current_top_n": 3},
                        }
                    }
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        benchmark = root / "krx300.csv"
        pd.DataFrame({"month_end": months, "ret_1m": [0.01] * len(months)}).to_csv(
            benchmark, index=False
        )
        return {
            "features": features,
            "returns": returns,
            "master": master,
            "config": config,
            "benchmark": benchmark,
        }

    def _run(self, root: Path):
        paths = self._inputs(root)
        return run_fresh_start_comparison(
            paths["features"],
            paths["returns"],
            paths["config"],
            paths["master"],
            paths["benchmark"],
            root / "immutable_run",
            "2020-06-30",
        )

    def test_overlap_is_still_fully_sold_and_rebought(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self._run(Path(temp))
            periods = result["periods"]
            fresh_second = periods.loc[
                periods["variant"].eq(FRESH_START_VARIANT)
                & periods["rebalance_month"].eq(pd.Timestamp("2020-04-30"))
            ].iloc[0]

            self.assertEqual(fresh_second["overlap_count"], 1)
            self.assertAlmostEqual(fresh_second["overlap_ratio"], 0.5)
            fresh_first = periods.loc[
                periods["variant"].eq(FRESH_START_VARIANT)
                & periods["rebalance_month"].eq(pd.Timestamp("2020-01-31"))
            ].iloc[0]
            # Every pre-reset equity is sold, including the overlapping A position.
            # The notional is the drifted ending equity weight, not stale 90%.
            self.assertAlmostEqual(
                fresh_second["sell_ratio"], fresh_first["ending_equity_weight"]
            )
            expected_after_sale = 1.0 - fresh_second["sell_ratio"] * (
                DEFAULT_COMMISSION_RATE + DEFAULT_SELL_TAX_RATE
            )
            expected_buy_principal = (
                0.9 * expected_after_sale / (1.0 + DEFAULT_COMMISSION_RATE)
            )
            self.assertAlmostEqual(fresh_second["buy_ratio"], expected_buy_principal)
            expected_cost = (
                (fresh_second["buy_ratio"] + fresh_second["sell_ratio"])
                * DEFAULT_COMMISSION_RATE
                + fresh_second["sell_ratio"] * DEFAULT_SELL_TAX_RATE
            )
            self.assertAlmostEqual(fresh_second["total_cost_ratio"], expected_cost)

            expected_initial_buy = 0.9 / (1.0 + DEFAULT_COMMISSION_RATE)
            self.assertAlmostEqual(fresh_first["buy_ratio"], expected_initial_buy)
            self.assertAlmostEqual(fresh_first["sell_ratio"], 0.0)
            self.assertAlmostEqual(fresh_first["turnover"], expected_initial_buy)
            self.assertGreaterEqual(fresh_first["post_cost_cash_equivalent_weight"], 0.1)
            self.assertTrue(fresh_first["post_cost_cash_floor_satisfied"])
            self.assertAlmostEqual(
                fresh_first["buy_principal_ratio_on_pre_reset_nav"]
                + fresh_first["buy_commission_ratio_on_pre_reset_nav"],
                fresh_first["equity_budget_including_buy_commission_ratio"],
            )
            self.assertAlmostEqual(
                fresh_first["post_cost_component_capital_ratio"]
                + fresh_first["total_cost_ratio"],
                1.0,
            )
            self.assertLess(fresh_first["reset_accounting_identity_error"], 1e-12)

            # Legacy retains both names but trades the drift back to equal weights.
            legacy_second = periods.loc[
                periods["variant"].eq(LEGACY_VARIANT)
                & periods["rebalance_month"].eq(pd.Timestamp("2020-04-30"))
            ].iloc[0]
            self.assertGreater(legacy_second["gross_traded_ratio"], 0.0)

    def test_month_two_uses_drifted_weights_without_interim_rebalance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self._inputs(root)
            returns = pd.read_csv(paths["returns"], dtype={"ticker": "string"})
            returns["ticker"] = returns["ticker"].str.zfill(6)
            february = returns["month_end"].eq("2020-02-29")
            march = returns["month_end"].eq("2020-03-31")
            returns.loc[february & returns["ticker"].eq("000001"), "ret_1m"] = 1.0
            returns.loc[february & returns["ticker"].eq("000002"), "ret_1m"] = 0.0
            returns.loc[march & returns["ticker"].eq("000001"), "ret_1m"] = 0.0
            returns.loc[march & returns["ticker"].eq("000002"), "ret_1m"] = 1.0
            returns.to_csv(paths["returns"], index=False)

            result = run_fresh_start_comparison(
                paths["features"],
                paths["returns"],
                paths["config"],
                paths["master"],
                paths["benchmark"],
                root / "drift_run",
                "2020-06-30",
            )
            fresh = result["monthly"].loc[
                result["monthly"]["variant"].eq(FRESH_START_VARIANT)
            ]
            feb = fresh.loc[fresh["month_end"].eq(pd.Timestamp("2020-02-29"))].iloc[0]
            mar = fresh.loc[fresh["month_end"].eq(pd.Timestamp("2020-03-31"))].iloc[0]

            # Post-cost equity is split equally and cash is slightly above 10%.
            # After A doubles, March must use the resulting drifted B weight.
            a_start = feb["equity_weight_start"] / 2.0
            self.assertAlmostEqual(feb["gross_return"], a_start)
            expected_march_b_weight = a_start / (1.0 + a_start)
            self.assertAlmostEqual(mar["gross_return"], expected_march_b_weight)
            self.assertAlmostEqual(
                mar["cash_equivalent_weight"],
                feb["cash_equivalent_weight"] / (1.0 + a_start),
            )
            self.assertNotAlmostEqual(mar["gross_return"], 0.45)

    def test_timing_cash_bucket_selection_contract_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = self._run(Path(temp))
            monthly = result["monthly"]
            selection = result["selection"]
            sector = result["sector_exposure"]
            summary = result["comparison_summary"]

            fresh_monthly = monthly.loc[monthly["variant"].eq(FRESH_START_VARIANT)]
            self.assertEqual(fresh_monthly["month_end"].min(), pd.Timestamp("2020-02-29"))
            self.assertNotIn(pd.Timestamp("2020-01-31"), set(fresh_monthly["month_end"]))
            february = fresh_monthly.loc[
                fresh_monthly["month_end"].eq(pd.Timestamp("2020-02-29"))
            ].iloc[0]
            # Half of the post-commission equity weight earns A's 10%; the
            # rebalance-month 100% returns are excluded.
            expected_gross = february["equity_weight_start"] / 2.0 * 0.10
            self.assertAlmostEqual(february["gross_return"], expected_gross)
            self.assertGreaterEqual(february["cash_equivalent_weight"], 0.10)
            self.assertAlmostEqual(
                february["net_return"],
                february["reset_nav_factor"] * (1.0 + february["gross_return"]) - 1.0,
            )
            self.assertAlmostEqual(february["nav"], 1.0 + february["net_return"])

            fresh_selection = selection.loc[selection["variant"].eq(FRESH_START_VARIANT)]
            self.assertTrue(fresh_selection["holding_bonus_applied"].eq(0).all())
            self.assertTrue(fresh_selection["keep_current_top_n_contract"].eq(0).all())
            self.assertTrue(fresh_selection["kept_from_previous"].eq(0).all())
            self.assertNotIn("437350", set(fresh_selection["ticker"]))
            weights = fresh_selection.groupby("rebalance_month")["target_weight"].sum()
            self.assertTrue(weights.map(lambda value: abs(value - 0.9) < 1e-12).all())

            fresh_sector = sector.loc[sector["variant"].eq(FRESH_START_VARIANT)]
            totals = fresh_sector.groupby("rebalance_month")["target_weight"].sum()
            self.assertTrue(totals.map(lambda value: abs(value - 1.0) < 1e-12).all())
            cash = fresh_sector.loc[fresh_sector["asset_bucket"].eq("CASH_EQUIVALENT")]
            self.assertTrue(cash["target_weight"].eq(0.1).all())

            fresh_summary = summary.loc[summary["variant"].eq(FRESH_START_VARIANT)].iloc[0]
            for metric in [
                "CAGR",
                "MDD",
                "Sharpe",
                "positive_quarter_hit_rate",
                "average_turnover",
                "total_trading_cost_ratio",
                "average_overlap_ratio",
                "average_total_concentration_hhi",
                "krx300_excess_total_return",
            ]:
                self.assertTrue(pd.notna(fresh_summary[metric]), metric)
            self.assertEqual(fresh_summary["krx300_status"], "FULL")
            self.assertGreaterEqual(
                fresh_summary["minimum_post_cost_cash_equivalent_weight"], 0.10
            )
            self.assertTrue(fresh_summary["post_cost_cash_floor_satisfied_all_rebalances"])
            self.assertLess(
                fresh_summary["maximum_reset_accounting_identity_error"], 1e-12
            )
            self.assertIn(
                "CASH_EQUIVALENT_RETURN_PLACEHOLDER_437350",
                fresh_summary["promotion_blockers"],
            )
            self.assertEqual(
                fresh_summary["pit_master_universe_status"],
                "CURRENT_SNAPSHOT_NOT_POINT_IN_TIME",
            )
            self.assertIn(
                "HISTORICAL_POINT_IN_TIME_SECURITY_MASTER_UNIVERSE_UNAVAILABLE",
                fresh_summary["promotion_blockers"],
            )
            summary_columns = [str(column) for column in result["comparison_summary"].columns]
            self.assertEqual(
                len(summary_columns),
                len({column.casefold() for column in summary_columns}),
            )

            for output_path in result["paths"].values():
                self.assertTrue(output_path.is_file(), output_path)

    def test_incomplete_returns_and_benchmark_suppress_production_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self._inputs(root)

            returns = pd.read_csv(paths["returns"], dtype={"ticker": "string"})
            returns["ticker"] = returns["ticker"].str.zfill(6)
            missing_selected = returns["ticker"].eq("000001") & returns["month_end"].eq(
                "2020-03-31"
            )
            returns.loc[~missing_selected].to_csv(paths["returns"], index=False)

            benchmark = pd.read_csv(paths["benchmark"])
            benchmark = benchmark.loc[benchmark["month_end"].ne("2020-06-30")]
            benchmark.to_csv(paths["benchmark"], index=False)

            result = run_fresh_start_comparison(
                paths["features"],
                paths["returns"],
                paths["config"],
                paths["master"],
                paths["benchmark"],
                root / "provisional_run",
                "2020-06-30",
            )
            fresh = result["comparison_summary"].loc[
                result["comparison_summary"]["variant"].eq(FRESH_START_VARIANT)
            ].iloc[0]

            self.assertEqual(fresh["evaluation_status"], "PROVISIONAL_INCOMPLETE_DATA")
            self.assertEqual(
                fresh["production_comparison_metrics_status"],
                "UNAVAILABLE_INCOMPLETE_RETURN_DATA",
            )
            self.assertEqual(fresh["expected_calendar_months"], 5)
            self.assertEqual(fresh["calendar_months_with_complete_portfolio_return"], 4)
            self.assertEqual(fresh["calendar_month_gap_count"], 1)
            self.assertAlmostEqual(fresh["full_calendar_return_coverage"], 0.8)
            self.assertAlmostEqual(fresh["selected_security_return_coverage"], 0.9)
            for metric in [
                "total_return",
                "CAGR",
                "MDD",
                "Sharpe",
                "positive_quarter_hit_rate",
                "drawdown_recovery_months",
                "krx300_excess_total_return",
                "krx300_excess_CAGR",
                "krx300_excess_quarter_hit_rate",
            ]:
                self.assertTrue(pd.isna(fresh[metric]), metric)
            self.assertTrue(pd.notna(fresh["observed_sample_CAGR"]))
            self.assertTrue(pd.notna(fresh["observed_sample_MDD"]))

            self.assertEqual(fresh["krx300_status"], "PARTIAL")
            self.assertEqual(fresh["krx300_expected_months"], 5)
            self.assertEqual(fresh["krx300_covered_months"], 4)
            self.assertEqual(fresh["krx300_gap_months"], 1)
            self.assertAlmostEqual(fresh["krx300_monthly_coverage"], 0.8)
            self.assertTrue(pd.isna(fresh["krx300_total_return"]))
            self.assertTrue(pd.isna(fresh["krx300_CAGR"]))
            self.assertEqual(fresh["promotion_status"], "BLOCKED")
            self.assertIn(
                "FULL_CALENDAR_RETURN_COVERAGE_INCOMPLETE", fresh["promotion_blockers"]
            )
            self.assertIn(
                "SELECTED_SECURITY_RETURN_COVERAGE_INCOMPLETE",
                fresh["promotion_blockers"],
            )
            self.assertIn("KRX300_HISTORY_PARTIAL", fresh["promotion_blockers"])

    def test_master_unmatched_feature_rows_are_ineligible_and_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self._inputs(root)
            master = pd.read_csv(paths["master"], dtype={"ticker": "string"})
            master["ticker"] = master["ticker"].str.zfill(6)
            master = master.loc[master["ticker"].ne("000001")]
            master.to_csv(paths["master"], index=False)

            result = run_fresh_start_comparison(
                paths["features"],
                paths["returns"],
                paths["config"],
                paths["master"],
                paths["benchmark"],
                root / "master_gap_run",
                "2020-06-30",
            )
            fresh_selection = result["selection"].loc[
                result["selection"]["variant"].eq(FRESH_START_VARIANT)
            ]
            self.assertNotIn("000001", set(fresh_selection["ticker"]))

            fresh = result["comparison_summary"].loc[
                result["comparison_summary"]["variant"].eq(FRESH_START_VARIANT)
            ].iloc[0]
            self.assertEqual(fresh["security_master_feature_rows"], 10)
            self.assertEqual(fresh["security_master_matched_feature_rows"], 8)
            self.assertEqual(fresh["security_master_unmatched_feature_rows"], 2)
            self.assertAlmostEqual(fresh["security_master_match_coverage"], 0.8)
            self.assertEqual(fresh["security_master_unmatched_unique_tickers"], 1)
            self.assertIn(
                "HISTORICAL_SECURITY_MASTER_UNMATCHED_FEATURE_ROWS",
                fresh["promotion_blockers"],
            )

    def test_missing_historical_mcap_and_traded_value_filters_block_formal_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self._inputs(root)
            config = yaml.safe_load(paths["config"].read_text(encoding="utf-8"))
            config["strategies"][LEGACY_VARIANT]["filters"] = {
                "min_mcap": 100_000_000_000,
                "min_traded_value": 1_000_000_000,
            }
            paths["config"].write_text(
                yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
            )

            result = run_fresh_start_comparison(
                paths["features"],
                paths["returns"],
                paths["config"],
                paths["master"],
                paths["benchmark"],
                root / "missing_historical_filter_run",
                "2020-06-30",
            )
            fresh = result["comparison_summary"].loc[
                result["comparison_summary"]["variant"].eq(FRESH_START_VARIANT)
            ].iloc[0]

            self.assertEqual(
                fresh["historical_filter_input_status"], "UNAVAILABLE_MISSING_COLUMNS"
            )
            self.assertFalse(fresh["historical_filter_inputs_complete"])
            self.assertEqual(fresh["historical_required_filter_columns"], "mcap|traded_value")
            self.assertEqual(fresh["historical_missing_filter_columns"], "mcap|traded_value")
            self.assertEqual(fresh["evaluation_status"], "PROVISIONAL_INCOMPLETE_DATA")
            self.assertEqual(
                fresh["production_comparison_metrics_status"],
                "UNAVAILABLE_HISTORICAL_FILTER_INPUTS",
            )
            for metric in [
                "total_return",
                "CAGR",
                "MDD",
                "Sharpe",
                "positive_quarter_hit_rate",
            ]:
                self.assertTrue(pd.isna(fresh[metric]), metric)
            self.assertTrue(pd.notna(fresh["observed_sample_CAGR"]))
            self.assertIn(
                "HISTORICAL_MCAP_TRADED_VALUE_FILTER_INPUTS_UNAVAILABLE",
                fresh["promotion_blockers"],
            )

    def test_existing_output_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = self._inputs(root)
            output = root / "immutable_run"
            run_fresh_start_comparison(
                paths["features"],
                paths["returns"],
                paths["config"],
                paths["master"],
                paths["benchmark"],
                output,
                "2020-06-30",
            )
            with self.assertRaises(FileExistsError):
                run_fresh_start_comparison(
                    paths["features"],
                    paths["returns"],
                    paths["config"],
                    paths["master"],
                    paths["benchmark"],
                    output,
                    "2020-06-30",
                )


if __name__ == "__main__":
    unittest.main()
