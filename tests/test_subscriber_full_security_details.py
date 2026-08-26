from __future__ import annotations

import json
import math
import re
import unittest
from pathlib import Path

import pandas as pd
from bs4 import BeautifulSoup

from scripts.subscriber_report.run_full_security_details import (
    PUBLIC_FORBIDDEN,
    PUBLIC_HTML,
    REPO_ROOT,
    RUNS_ROOT,
    _growth,
    _operating_growth,
    _ticker,
    _truth,
)


def _prepared_run() -> Path:
    candidates = [
        path
        for path in [*RUNS_ROOT.glob(".run_id=*.staging"), *RUNS_ROOT.glob("run_id=*")]
        if (path / "prepare_state.json").exists()
        and (path / "report_analysis_equity_metrics.csv").exists()
    ]
    if not candidates:
        raise unittest.SkipTest("no prepared full-security-details run")
    return max(candidates, key=lambda path: path.stat().st_mtime_ns)


class FullSecurityDetailsRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.prepared = _prepared_run()
        cls.metrics = pd.read_csv(
            cls.prepared / "report_analysis_equity_metrics.csv", dtype={"ticker": str}
        )

    def test_dynamic_population_is_current_held_not_selected_equity(self) -> None:
        state = json.loads(
            (self.prepared / "prepare_state.json").read_text(encoding="utf-8")
        )
        advisor = REPO_ROOT / state["advisor_parent"]
        comparison = pd.read_csv(advisor / "current_vs_target_v2.csv", dtype={"ticker": str})
        expected = set(
            comparison.loc[
                comparison["previously_held"].map(_truth)
                & ~comparison["model_selected"].map(_truth)
                & comparison["asset_class"].eq("EQUITY"),
                "ticker",
            ].map(_ticker)
        )
        actual = set(
            pd.read_csv(
                self.prepared / "dropped_existing_equity_universe.csv",
                dtype={"ticker": str},
            )["ticker"].map(_ticker)
        )
        self.assertEqual(actual, expected)
        excluded = set(
            comparison.loc[comparison["asset_class"].ne("EQUITY"), "ticker"].map(_ticker)
        )
        self.assertTrue(excluded.isdisjoint(actual))

    def test_no_security_or_population_override_in_builder(self) -> None:
        source = (
            REPO_ROOT / "scripts/subscriber_report/run_full_security_details.py"
        ).read_text(encoding="utf-8")
        universe = pd.read_csv(
            self.prepared / "report_analysis_equity_universe.csv", dtype={"ticker": str}
        )
        for ticker in universe["ticker"].map(_ticker):
            self.assertNotIn(ticker, source)
        for name in universe["name"].astype(str):
            self.assertNotIn(name, source)
        completeness = json.loads(
            (self.prepared / "full_security_metrics_completeness.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(completeness["ticker_specific_override_count"], 0)
        self.assertEqual(completeness["company_specific_override_count"], 0)
        self.assertEqual(completeness["population_size_hardcode_count"], 0)

    def test_financial_completeness_and_reproduction(self) -> None:
        metrics = self.metrics
        self.assertTrue(metrics["eps_ttm"].notna().all())
        self.assertTrue(metrics["net_income_ttm"].notna().all())
        self.assertTrue(metrics["per_status"].isin(["PASS", "LOSS"]).all())
        self.assertTrue(
            metrics.loc[metrics["per_status"].eq("PASS"), "per_ttm"].notna().all()
        )
        for row in metrics.itertuples():
            self.assertTrue(
                math.isclose(
                    row.pbr,
                    row.market_cap / row.equity_denominator,
                    rel_tol=0,
                    abs_tol=1e-12,
                )
            )
            self.assertTrue(
                math.isclose(
                    row.psr,
                    row.market_cap / row.revenue_ttm,
                    rel_tol=0,
                    abs_tol=1e-12,
                )
            )
            if pd.notna(row.cfo_ttm) and row.operating_income_ttm != 0:
                self.assertTrue(
                    math.isclose(
                        row.cfo_to_operating_income,
                        row.cfo_ttm / row.operating_income_ttm,
                        rel_tol=0,
                        abs_tol=1e-12,
                    )
                )
        recent = [
            "revenue_minus_2",
            "revenue_minus_1",
            "revenue_latest",
            "operating_income_minus_2",
            "operating_income_minus_1",
            "operating_income_latest",
        ]
        self.assertTrue(metrics[recent].notna().all().all())

    def test_growth_display_contract(self) -> None:
        self.assertEqual(_growth(120, 100), "20.0%")
        self.assertEqual(_growth(1, 0), "산출 불가")
        self.assertEqual(_operating_growth(120, 100), "20.0%")
        self.assertEqual(_operating_growth(1, -1), "흑자전환")
        self.assertEqual(_operating_growth(-1, 1), "적자전환")
        self.assertEqual(_operating_growth(-1, -2), "적자축소")
        self.assertEqual(_operating_growth(-3, -2), "적자확대")
        self.assertEqual(_operating_growth(1, 0), "산출 불가")

    def test_report_dom_cards_order_labels_and_public_safety(self) -> None:
        html = (self.prepared / PUBLIC_HTML).read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")
        dropped = pd.read_csv(
            self.prepared / "dropped_existing_equity_universe.csv",
            dtype={"ticker": str},
        )
        cards = soup.select(
            "#not-selected-details article.not-selected-security-card"
        )
        self.assertEqual(len(cards), len(dropped))
        self.assertEqual(
            [card["data-ticker"] for card in cards],
            dropped["ticker"].map(_ticker).tolist(),
        )
        self.assertEqual(len(soup.select("#not-selected-summary table")), 1)
        self.assertNotIn("(계속)", html)
        required = (
            "EPS",
            "당기순이익",
            "PER",
            "최근 3개 분기",
            "매출 증가율",
            "영업이익 증가율",
            "미선발 사유",
        )
        for card in cards:
            card_text = card.get_text(" ", strip=True)
            self.assertTrue(all(label in card_text for label in required))
        lowered = html.lower()
        self.assertFalse(
            [term for term in PUBLIC_FORBIDDEN if term.lower() in lowered]
        )
        self.assertIsNone(re.search(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]", html))
        self.assertIsNone(re.search(r"(?<![A-Za-z0-9_])NA(?![A-Za-z0-9_])", html))

    def test_model_target_performance_and_privacy_preflight(self) -> None:
        qa = json.loads(
            (self.prepared / "public_pre_render_qa.json").read_text(encoding="utf-8")
        )
        self.assertEqual(qa["status"], "PASS")
        self.assertTrue(qa["checks"]["topk_identity_rank_score_parity"])
        self.assertTrue(qa["checks"]["target_artifact_untouched"])
        self.assertTrue(qa["checks"]["performance_parity"])
        self.assertTrue(qa["checks"]["privacy"])
        self.assertTrue(
            all(value == 0 for value in qa["privacy"].values() if isinstance(value, int))
        )


if __name__ == "__main__":
    unittest.main()
