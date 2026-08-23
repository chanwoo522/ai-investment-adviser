from __future__ import annotations

import inspect
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from scripts.advisor.score_parity_v2 import (
    BOUNDARY_WATCHLIST_COLUMNS,
    PARITY_AUDIT_COLUMNS,
    PARITY_TOLERANCE,
    REQUIRED_RAW_FACTOR_COLUMNS,
    ScoreParityError,
    assert_no_holdings_parameter,
    audit_score_parity,
    build_score_parity_bundle,
    build_score_parity_bundle_from_paths,
    build_top_k_boundary_watchlist,
    project_fresh_start_scores,
    write_score_parity_artifacts,
)


FILTERS = {
    "max_Debt_to_Equity_log": 2.398,
    "min_traded_value": 1_000_000_000,
    "min_mcap": 100_000_000_000,
    "min_OpIncome_ttm": 0,
    "min_op_cur_q": 0,
}


def source_scores(rows: int = 16) -> pd.DataFrame:
    records: list[dict[str, object]] = []
    for rank in range(1, rows + 1):
        base = float(20 - rank)
        penalty = -0.1 if rank % 4 == 0 else 0.0
        record: dict[str, object] = {
            "ticker": f"{rank:06d}",
            "name": f"종목{rank}",
            "passed_filters": True,
            "score_base": base,
            "quality_penalty_total": penalty,
            "score": base + penalty,
            # Deliberately reverse the holding-adjusted ordering. It must be ignored.
            "score_adj": float(rank * 10_000),
            "score_adj_rank": rows - rank + 1,
            "hold_bonus_applied": float(rank * 10_000) - (base + penalty),
            "previously_held": rank % 2 == 0,
            "current_qty": rank * 999,
            "Debt_to_Equity_log": 1.0,
            "OpIncome_ttm": 100.0,
            "op_cur_q": 10.0,
            "op_qoq": 0.1,
            "filter_status": "passed",
            "filter_failure_reason": pd.NA,
        }
        for factor_number, column in enumerate(REQUIRED_RAW_FACTOR_COLUMNS, start=1):
            record[column] = float(rank * factor_number)
            record[column.replace("__raw", "__contrib")] = float(
                rank * factor_number / 100
            )
        records.append(record)
    return pd.DataFrame(records)


def drifted_scores(rows: int = 16, passed: int = 12) -> pd.DataFrame:
    source = source_scores(rows)
    return pd.DataFrame(
        {
            "ticker": source["ticker"],
            "name": source["name"],
            "passed_filters": [rank <= passed for rank in range(1, rows + 1)],
            "score_base": source["score_base"] + 0.01,
            "model_score": source["score"] + 0.01,
        }
    )


def diagnostic_filter_inputs(rows: int = 16, passed: int = 12) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ticker": [f"{rank:06d}" for rank in range(1, rows + 1)],
            "traded_value": [
                2_000_000_000 if rank <= passed else 500_000_000
                for rank in range(1, rows + 1)
            ],
            "mcap": [200_000_000_000] * rows,
        }
    )


class AdvisorScoreParityV2Tests(unittest.TestCase):
    def test_projection_uses_source_base_quality_and_score_only(self) -> None:
        source = source_scores()
        fresh, top = project_fresh_start_scores(source, top_k=10)

        aligned = source.set_index("ticker").loc[fresh["ticker"]]
        self.assertTrue(
            fresh["score_base"].reset_index(drop=True).equals(
                aligned["score_base"].reset_index(drop=True)
            )
        )
        self.assertTrue(
            fresh["quality_penalty_total"].reset_index(drop=True).equals(
                aligned["quality_penalty_total"].reset_index(drop=True)
            )
        )
        self.assertTrue(
            fresh["model_score"].reset_index(drop=True).equals(
                aligned["score"].reset_index(drop=True)
            )
        )
        self.assertEqual(top.iloc[0]["ticker"], "000001")
        self.assertTrue(fresh["holding_bonus"].eq(0).all())
        self.assertTrue(fresh["keep_current_top_n"].eq(0).all())
        self.assertFalse(fresh["current_account_membership_used"].any())
        for forbidden in (
            "score_adj",
            "score_adj_rank",
            "hold_bonus_applied",
            "previously_held",
            "current_qty",
        ):
            self.assertNotIn(forbidden, fresh.columns)

    def test_score_adj_and_current_like_fields_cannot_change_selection(self) -> None:
        first_source = source_scores()
        second_source = source_scores()
        second_source["score_adj"] = list(reversed(second_source["score_adj"].tolist()))
        second_source["hold_bonus_applied"] *= -999
        second_source["previously_held"] = ~second_source["previously_held"]
        second_source["current_qty"] = list(reversed(second_source["current_qty"].tolist()))

        _, first_top = project_fresh_start_scores(first_source, top_k=10)
        _, second_top = project_fresh_start_scores(second_source, top_k=10)
        self.assertEqual(first_top["ticker"].tolist(), second_top["ticker"].tolist())
        self.assertEqual(
            first_top["model_rank"].tolist(), second_top["model_rank"].tolist()
        )

    def test_parity_audit_is_exact_and_fails_above_one_e_minus_twelve(self) -> None:
        source = source_scores()
        fresh, _ = project_fresh_start_scores(source, top_k=10)
        audit, summary = audit_score_parity(source, fresh)
        self.assertEqual(list(audit.columns), list(PARITY_AUDIT_COLUMNS))
        self.assertEqual(summary["status"], "PASS")
        self.assertEqual(summary["max_abs_score_base_diff"], 0.0)
        self.assertEqual(summary["max_abs_quality_penalty_diff"], 0.0)
        self.assertEqual(summary["max_abs_model_score_diff"], 0.0)

        within = fresh.copy()
        within.loc[within["ticker"].eq("000001"), "score_base"] += PARITY_TOLERANCE / 2
        _, within_summary = audit_score_parity(source, within)
        self.assertEqual(within_summary["status"], "PASS")

        outside = fresh.copy()
        outside.loc[outside["ticker"].eq("000001"), "score_base"] += PARITY_TOLERANCE * 2
        with self.assertRaisesRegex(ScoreParityError, "fail-closed tolerance"):
            audit_score_parity(source, outside)

        holdings_coupled = fresh.copy()
        holdings_coupled.loc[
            holdings_coupled["ticker"].eq("000001"), "holding_bonus"
        ] = 0.5
        with self.assertRaisesRegex(ScoreParityError, "holdings independence"):
            audit_score_parity(source, holdings_coupled)

    def test_missing_raw_factor_fails_closed(self) -> None:
        source = source_scores().drop(columns=[REQUIRED_RAW_FACTOR_COLUMNS[0]])
        with self.assertRaisesRegex(ScoreParityError, "raw factor parity cannot be proven"):
            project_fresh_start_scores(source)

    def test_population_diagnosis_reproduces_filter_first_drift_exactly(self) -> None:
        bundle = build_score_parity_bundle(
            source_scores(),
            filters=FILTERS,
            drifted_fresh_scores=drifted_scores(),
            authoritative_filter_inputs=diagnostic_filter_inputs(),
        )
        population = bundle.scoring_population_audit
        diagnosis = population["drift_diagnosis"]
        self.assertEqual(population["source_passed_filter_rows"], 16)
        self.assertEqual(diagnosis["drifted_passed_filter_rows"], 12)
        self.assertEqual(diagnosis["source_minus_drifted_rows"], 4)
        self.assertTrue(
            diagnosis["diagnostic_population_exactly_matches_drifted_population"]
        )
        self.assertEqual(
            diagnosis["cause"],
            "EXACT_MATCH_MCAP_TRADED_VALUE_FILTERS_BEFORE_STANDARDIZATION",
        )
        self.assertEqual(
            diagnosis["source_skipped_missing_filter_columns"],
            ["mcap", "traded_value"],
        )
        self.assertEqual(
            diagnosis["diagnostic_attached_filter_columns"],
            ["mcap", "traded_value"],
        )
        self.assertIn("405 vs 355 원인", bundle.filter_order_audit_markdown)
        self.assertIn("robust-z", bundle.filter_order_audit_markdown)
        self.assertIn("V1 diagnostic filter-first replay", bundle.filter_order_audit_markdown)

    def test_population_diagnosis_inputs_are_required_by_default(self) -> None:
        with self.assertRaisesRegex(ScoreParityError, "405 vs 355 diagnosis"):
            build_score_parity_bundle(source_scores(), filters=FILTERS)

    def test_boundary_watchlist_covers_rank_k_minus_two_through_k_plus_five(self) -> None:
        fresh, _ = project_fresh_start_scores(source_scores(), top_k=10)
        watch, qa = build_top_k_boundary_watchlist(
            fresh, top_k=10, fragile_threshold=1.1
        )
        self.assertEqual(list(watch.columns), list(BOUNDARY_WATCHLIST_COLUMNS))
        self.assertEqual(watch["rank"].tolist(), list(range(8, 16)))
        self.assertEqual(qa["watchlist_rows"], 8)
        self.assertEqual(qa["selection_boundary_status"], "FRAGILE")
        self.assertFalse(qa["automatic_selection_change_applied"])
        self.assertAlmostEqual(
            qa["rank_k_minus_k_plus_1_gap"],
            qa["rank_k_score"] - qa["rank_k_plus_1_score"],
        )

    def test_writer_emits_all_score_parity_and_boundary_artifacts(self) -> None:
        bundle = build_score_parity_bundle(
            source_scores(),
            filters=FILTERS,
            drifted_fresh_scores=drifted_scores(),
            authoritative_filter_inputs=diagnostic_filter_inputs(),
        )
        with tempfile.TemporaryDirectory() as tmp:
            paths = write_score_parity_artifacts(bundle, tmp)
            expected_names = {
                "fresh_start_scores.csv",
                "fresh_start_top_k.csv",
                "score_parity_audit.csv",
                "score_parity_audit.json",
                "scoring_population_audit.json",
                "filter_order_audit.md",
                "top_k_boundary_watchlist.csv",
                "top_k_boundary_qa.json",
            }
            self.assertEqual({path.name for path in paths.values()}, expected_names)
            self.assertTrue(all(path.is_file() for path in paths.values()))
            summary = json.loads(
                paths["score_parity_audit_json"].read_text(encoding="utf-8")
            )
            self.assertEqual(summary["status"], "PASS")

    def test_file_backed_api_has_no_holdings_input(self) -> None:
        assert_no_holdings_parameter()
        parameters = inspect.signature(build_score_parity_bundle_from_paths).parameters
        self.assertFalse(any("holding" in name.lower() for name in parameters))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_path = root / "source.csv"
            drifted_path = root / "drifted.csv"
            diagnostic_path = root / "universe.csv"
            source_scores().to_csv(source_path, index=False, encoding="utf-8-sig")
            drifted_scores().to_csv(drifted_path, index=False, encoding="utf-8-sig")
            diagnostic_filter_inputs().to_csv(
                diagnostic_path, index=False, encoding="utf-8-sig"
            )
            bundle = build_score_parity_bundle_from_paths(
                source_path,
                filters=FILTERS,
                drifted_fresh_scores_path=drifted_path,
                authoritative_filter_inputs_path=diagnostic_path,
            )
            self.assertEqual(bundle.parity_summary["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
