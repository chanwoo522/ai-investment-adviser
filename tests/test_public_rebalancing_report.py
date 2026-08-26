from __future__ import annotations

import json
import re
import sys
import types
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from scripts.live.generate_public_rebalancing_report import (
    COST_DISCLOSURE,
    EXECUTION_BLOCKED_TEXT,
    MAX_HTML_BYTES,
    PUBLIC_SECTIONS,
    build_public_report,
    main as generate_main,
)
from scripts.qa.validate_public_rebalancing_report import (
    main as validate_main,
    validate,
    validate_html,
)


class PublicRebalancingReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tickers = [f"{index:06d}" for index in range(1, 11)]
        self.model = pd.DataFrame(
            {
                "ticker": self.tickers,
                "name": [f"테스트종목{index}" for index in range(1, 11)],
                "model_action": ["HOLD"] * 3 + ["BUY"] * 7,
                "model_score": [20.0 - index for index in range(10)],
                "model_score_rank": list(range(1, 11)),
                "model_score_adj": [10.0 - index / 10 for index in range(10)],
                "model_score_adj_rank": list(range(1, 11)),
                "selection_bucket": ["keep_current_top_n"] * 3 + ["topk"] * 7,
                "kept_from_previous": [True] * 3 + [False] * 7,
                "asof": ["2026-08-18"] * 10,
                "target_date": ["2026-08-31"] * 10,
            }
        )
        self.factors = pd.DataFrame(
            {
                "ticker": self.tickers,
                "name": self.model["name"],
                "score": self.model["model_score"],
                "score_adj": self.model["model_score_adj"],
                "score_rank": self.model["model_score_rank"],
                "score_adj_rank": self.model["model_score_adj_rank"],
                "selected_topk": [1] * 10,
                "OpIncome_acc2_log1p__contrib": [1.0 - index / 100 for index in range(10)],
                "Revenue_acc2__contrib": [0.8] * 10,
                "Debt_to_Equity_log__contrib": [0.7] * 10,
                "op_growth_streak2__contrib": [0.15] * 10,
                "rev_growth_streak2__contrib": [0.05] * 10,
            }
        )
        self.valuation = pd.DataFrame(
            {
                "ticker": self.tickers,
                "name": self.model["name"],
                "financial_period": ["2026Q2"] * 10,
                "statement_scope": ["CFS"] * 10,
                "per_ttm": [10.0 + index for index in range(10)],
                "pbr": [1.0 + index / 10 for index in range(10)],
                "psr_ttm": [2.0 + index / 10 for index in range(10)],
                "ev_to_opincome_ttm": [8.0 + index for index in range(10)],
            }
        )
        dates = pd.to_datetime(
            ["2026-04-01", "2026-04-02", "2026-04-03", "2026-04-06", "2026-08-18"]
        )
        portfolio_returns = [0.0, 0.05, 0.12, 0.08, 0.10]
        benchmark_returns = [0.0, 0.02, 0.06, 0.05, 0.07]
        drawdowns = [0.0, 0.0, 0.0, -0.0357142857142857, -0.0178571428571429]
        self.daily = pd.DataFrame(
            {
                "date": dates,
                "cum_return": portfolio_returns,
                "drawdown": drawdowns,
                "benchmark_cum_return": benchmark_returns,
            }
        )
        self.summary = {
            "start_date": "2026-04-01",
            "end_date": "2026-08-18",
            "cum_return": 0.10,
            "max_drawdown": min(drawdowns),
            "drawdown_peak_date": "2026-04-03",
            "max_drawdown_date": "2026-04-06",
            "performance_interval_activity_proven": False,
            "benchmark_cum_return": 0.07,
            "active_return": 0.03,
            "benchmark_name": "KRX 300",
            "benchmark_identifier": "5300",
            "benchmark_asset_type": "INDEX",
            "benchmark_return_type": "PRICE",
            "nav_dividend_treatment": "EXCLUDED",
        }
        self.contribution = pd.DataFrame(
            {
                "ticker": [self.tickers[0], self.tickers[1], "999999"],
                "name": ["테스트종목1", "테스트종목2", "이전보유종목"],
                "weight_at_start_nav": [0.4, 0.35, 0.25],
                "period_pnl": [4_000.0, 3_000.0, 3_000.0],
                "position_period_return": [0.10, 0.08, 0.12],
                "contribution_to_total_return": [0.04, 0.03, 0.03],
            }
        )
        self.benchmark = pd.DataFrame(
            {
                "date": dates,
                "index_level": [100.0 * (1 + value) for value in benchmark_returns],
                "benchmark_cum_return": benchmark_returns,
                "benchmark_name": ["KRX 300"] * len(dates),
                "benchmark_identifier": ["5300"] * len(dates),
                "asset_type": ["INDEX"] * len(dates),
                "return_type": ["PRICE"] * len(dates),
            }
        )
        self.config = {
            "execution": {
                "portfolio_construction": "EQUAL_WEIGHT_TOP_K",
                "top_k": 10,
                "commission_rate": 0.00015,
                "sell_levy_schedule": [{"market": "KOSPI", "effective_from": "2026-01-01"}],
            }
        }

    def _summary(self, *, ready: bool) -> dict:
        return {
            "schema_version": (
                "model-only-execution/v1" if ready else "model-only-execution-plan/v1"
            ),
            "execution_plan_status": "VALID_MODEL_ONLY" if ready else "BLOCKED_EXPLICIT_INPUT_REQUIRED",
            "account_snapshot_status": "VERIFIED" if ready else "NOT_PROVIDED",
            "account_asof": "2026-08-18" if ready else None,
            "price_date": "2026-08-18",
            "target_date": "2026-08-31",
            "execution_planning_nav": 100_000_000.0 if ready else None,
            "estimated_commission": 1_500.0 if ready else None,
            "estimated_sell_tax": 0.0 if ready else None,
            "estimated_total_cost": 1_500.0 if ready else None,
            "final_cash": 100_000.0 if ready else None,
            "actual_orders_submitted": False,
            "legacy_user_overlay": {
                "status": "LEGACY_USER_OVERLAY_NOT_APPLIED",
                "loaded": False,
                "affects_model": False,
                "affects_execution_plan": False,
                "affects_public_report": False,
            },
        }

    def _plan(self, *, ready: bool) -> pd.DataFrame:
        if not ready:
            return pd.DataFrame(
                {
                    "ticker": self.tickers,
                    "name": self.model["name"],
                    "model_status": ["SELECTED"] * 10,
                    "target_weight": [0.1] * 10,
                    "reference_price": [pd.NA] * 10,
                    "current_qty": [pd.NA] * 10,
                    "target_qty": [pd.NA] * 10,
                    "trade_qty": [pd.NA] * 10,
                    "order_value": [pd.NA] * 10,
                    "estimated_commission": [pd.NA] * 10,
                    "estimated_sell_tax": [pd.NA] * 10,
                    "estimated_total_cost_row": [pd.NA] * 10,
                    "actual_orders_submitted": [False] * 10,
                }
            )
        return pd.DataFrame(
            {
                "ticker": self.tickers,
                "name": self.model["name"],
                "model_status": ["SELECTED"] * 10,
                "target_weight": [0.1] * 10,
                "current_qty": [0, 5, 10, 10, 10, 10, 10, 10, 10, 10],
                "target_qty": [10] * 10,
                "trade_side": ["BUY", "BUY"] + ["NONE"] * 8,
                "trade_qty": [10, 5] + [0] * 8,
                "order_value": [100_000.0, 50_000.0] + [0.0] * 8,
                "realized_target_weight": [0.099] * 10,
                "reference_price": [10_000.0] * 10,
                "price_date": ["2026-08-18"] * 10,
                "estimated_commission": [15.0, 7.5] + [0.0] * 8,
                "estimated_sell_tax": [0.0] * 10,
                "estimated_total_cost_row": [15.0, 7.5] + [0.0] * 8,
                "actual_orders_submitted": [False] * 10,
            }
        )

    def _build(self, *, ready: bool = False, config: dict | None = None) -> str:
        return build_public_report(
            self.model,
            self.factors,
            self.valuation,
            self.summary,
            self.daily,
            self.contribution,
            self.benchmark,
            self._plan(ready=ready),
            self._summary(ready=ready),
            config or self.config,
        )

    def test_blocked_public_report_has_exact_five_sections_and_provisional_performance(self) -> None:
        report = self._build(ready=False)
        self.assertLess(len(report.encode("utf-8")), MAX_HTML_BYTES)
        self.assertEqual(re.findall(r"<h2>(.*?)</h2>", report), list(PUBLIC_SECTIONS))
        self.assertIn("잠정 성과", report)
        self.assertIn("4,000원", report)
        self.assertIn("목표 제외", report)
        self.assertEqual(report.count('data-public-model-row="true"'), 10)
        self.assertEqual(report.count('data-public-execution-row="true"'), 10)
        self.assertIn(EXECUTION_BLOCKED_TEXT, report)
        self.assertIn(COST_DISCLOSURE, report)
        self.assertNotIn("<script", report.lower())
        self.assertNotIn("application/json", report.lower())
        self.assertIsNone(
            re.search(
                r"\b(?:READY|BLOCKED|CERTIFIED|PROVISIONAL|BUY|SELL|HOLD)\b",
                report,
                re.IGNORECASE,
            )
        )
        payload = validate_html(report, expected_execution_status="BLOCKED")
        self.assertEqual(payload["status"], "PASS", payload["failed_checks"])

    def test_ready_report_uses_model_only_quantities_and_korean_trade_labels(self) -> None:
        report = self._build(ready=True)
        self.assertIn("신규", report)
        self.assertIn("추가", report)
        self.assertIn("유지", report)
        self.assertIn("100,000,000원", report)
        visible = re.sub(r"<style.*?</style>", "", report, flags=re.DOTALL)
        visible = re.sub(r"<[^>]+>", " ", visible)
        for raw in ("READY", "BLOCKED", "CERTIFIED", "PROVISIONAL", "BUY", "SELL", "HOLD"):
            self.assertIsNone(re.search(rf"\b{raw}\b", visible, re.IGNORECASE))
        payload = validate_html(report, expected_execution_status="READY")
        self.assertEqual(payload["status"], "PASS", payload["failed_checks"])

    def test_model_table_is_one_eight_column_table_with_friendly_factor(self) -> None:
        report = self._build()
        model_table = re.search(
            r'<table id="model-target-table">(.*?)</table>', report, re.DOTALL
        )
        self.assertIsNotNone(model_table)
        table = model_table.group(1)
        self.assertEqual(table.count('<th scope="col">'), 8)
        for heading in (
            "종목", "모델 구분", "모델 순위", "목표비중", "핵심 팩터", "PER(TTM)", "PBR", "PSR(TTM)"
        ):
            self.assertIn(heading, table)
        self.assertIn("영업이익 가속", table)
        self.assertNotIn("BUY", table)
        self.assertNotIn("HOLD", table)

    def test_charts_have_explicit_axes_zero_markers_and_unfilled_paths(self) -> None:
        report = self._build()
        for css_class in ("x-axis", "y-axis", "zero-line", "start-marker", "end-marker", "peak-marker", "mdd-marker"):
            self.assertIn(css_class, report)
        paths = re.findall(r'<path class="(?:portfolio|benchmark)-line"([^>]+)>', report)
        self.assertEqual(len(paths), 2)
        self.assertTrue(all('fill="none"' in attrs and 'stroke="' in attrs for attrs in paths))
        self.assertIn('id="contribution-bar-chart"', report)
        self.assertIn('class="contribution-bar ', report)

    def test_blocked_plan_rejects_any_populated_execution_numeric(self) -> None:
        plan = self._plan(ready=False)
        plan.loc[0, "current_qty"] = 1
        with self.assertRaisesRegex(ValueError, "leave execution numerics blank"):
            build_public_report(
                self.model, self.factors, self.valuation, self.summary, self.daily,
                self.contribution, self.benchmark, plan, self._summary(ready=False), self.config,
            )

    def test_explicit_broker_file_can_still_produce_blocked_execution(self) -> None:
        with TemporaryDirectory() as temporary:
            broker_file = Path(temporary) / "broker-download.csv"
            broker_file.write_text("fixture", encoding="utf-8")
            report = build_public_report(
                self.model, self.factors, self.valuation, self.summary, self.daily,
                self.contribution, self.benchmark, self._plan(ready=False),
                self._summary(ready=False), self.config, broker_account_file=broker_file,
            )
        payload = validate_html(report, expected_execution_status="BLOCKED")
        self.assertEqual(payload["status"], "PASS", payload["failed_checks"])
        self.assertIn(EXECUTION_BLOCKED_TEXT, report)

    def test_private_final_execution_schema_is_rejected(self) -> None:
        plan = self._plan(ready=True)
        plan["override_flag"] = False
        with self.assertRaisesRegex(ValueError, "private/final execution schema"):
            build_public_report(
                self.model, self.factors, self.valuation, self.summary, self.daily,
                self.contribution, self.benchmark, plan, self._summary(ready=True), self.config,
            )

    def test_private_decision_layer_attestation_must_be_negative(self) -> None:
        summary = self._summary(ready=False)
        summary["legacy_user_overlay"]["loaded"] = True
        with self.assertRaisesRegex(ValueError, "attestation must be false"):
            build_public_report(
                self.model, self.factors, self.valuation, self.summary, self.daily,
                self.contribution, self.benchmark, self._plan(ready=False), summary, self.config,
            )

    def test_equal_weight_is_only_inferred_for_exact_policy(self) -> None:
        config = {"execution": {"portfolio_construction": "CUSTOM", "top_k": 10}}
        plan = self._plan(ready=False)
        plan["target_weight"] = 0.08
        report = build_public_report(
            self.model, self.factors, self.valuation, self.summary, self.daily,
            self.contribution, self.benchmark, plan, self._summary(ready=False), config,
        )
        model_table = re.search(
            r'<table id="model-target-table">(.*?)</table>', report, re.DOTALL
        ).group(1)
        self.assertNotIn("10.00%", model_table)
        self.assertEqual(model_table.count("NA"), 10)

    def test_validator_detects_public_privacy_and_section_regressions(self) -> None:
        report = self._build()
        leaked = report.replace("</main>", "<p>C:\\Users\\example\\secret.csv</p></main>")
        payload = validate_html(leaked)
        self.assertEqual(payload["status"], "FAIL")
        self.assertFalse(payload["checks"]["paths_and_urls_absent"])
        extra = report.replace("</main>", "<h2>추가 장</h2></main>")
        payload = validate_html(extra)
        self.assertFalse(payload["checks"]["exact_five_h2_sections"])

    def test_optional_pdf_validation_requires_content_not_five_pages(self) -> None:
        report = self._build()
        visible = re.sub(r"<style.*?</style>", "", report, flags=re.DOTALL)
        visible = re.sub(r"<[^>]+>", " ", visible)

        class FakePage:
            def extract_text(self) -> str:
                return visible

        class FakeReader:
            def __init__(self, _: str) -> None:
                self.pages = [FakePage()]

        fake_module = types.ModuleType("pypdf")
        fake_module.PdfReader = FakeReader
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            html_path, pdf_path = root / "report.html", root / "report.pdf"
            html_path.write_text(report, encoding="utf-8")
            pdf_path.write_bytes(b"fixture")
            with patch.dict(sys.modules, {"pypdf": fake_module}):
                payload = validate(html_path, pdf_path=pdf_path)
        self.assertEqual(payload["status"], "PASS", payload["failed_checks"])
        self.assertEqual(payload["pdf_validation"]["page_count"], 1)

    def test_cli_contract_generates_and_validates_html_json_and_markdown(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = {
                "model": root / "model.csv",
                "factors": root / "factors.csv",
                "valuation": root / "valuation.csv",
                "daily": root / "daily.csv",
                "contribution": root / "contribution.csv",
                "benchmark": root / "benchmark.csv",
                "plan": root / "model_plan.csv",
                "summary": root / "performance.json",
                "execution_summary": root / "execution_summary.json",
                "config": root / "execution.json",
            }
            for key, frame in (
                ("model", self.model), ("factors", self.factors),
                ("valuation", self.valuation), ("daily", self.daily),
                ("contribution", self.contribution), ("benchmark", self.benchmark),
                ("plan", self._plan(ready=False)),
            ):
                frame.to_csv(paths[key], index=False, encoding="utf-8-sig")
            paths["summary"].write_text(json.dumps(self.summary), encoding="utf-8")
            paths["execution_summary"].write_text(
                json.dumps(self._summary(ready=False)), encoding="utf-8"
            )
            paths["config"].write_text(json.dumps(self.config), encoding="utf-8")
            html_output = root / "public.html"
            argv = [
                "generate_public_rebalancing_report.py",
                "--model-target", str(paths["model"]),
                "--scores-topk", str(paths["factors"]),
                "--valuation", str(paths["valuation"]),
                "--performance-summary", str(paths["summary"]),
                "--performance-daily", str(paths["daily"]),
                "--performance-contribution", str(paths["contribution"]),
                "--benchmark-qa", str(paths["benchmark"]),
                "--execution-plan", str(paths["plan"]),
                "--execution-summary", str(paths["execution_summary"]),
                "--execution-config", str(paths["config"]),
                "--output", str(html_output),
            ]
            with patch.object(sys, "argv", argv):
                generate_main()
            qa_json, qa_md = root / "qa.json", root / "qa.md"
            with patch.object(
                sys,
                "argv",
                [
                    "validate_public_rebalancing_report.py", "--html", str(html_output),
                    "--output-json", str(qa_json), "--output-md", str(qa_md),
                    "--expected-execution-status", "BLOCKED",
                ],
            ):
                validate_main()
            payload = json.loads(qa_json.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "PASS")
            self.assertTrue(qa_md.is_file())
            self.assertEqual(validate(html_output)["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
