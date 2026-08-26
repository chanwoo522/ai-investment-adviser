from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from scripts.live.build_benchmark_krx300 import (
    BENCHMARK_ASSET_TYPE,
    BENCHMARK_NAME,
    BENCHMARK_RETURN_TYPE,
    BENCHMARK_SOURCE,
    collect_official_index_master_response,
    fetch_official_index_series,
    main as build_benchmark_main,
    require_krx_credentials,
    resolve_official_index_identifier,
)
from scripts.live.generate_corrected_rebalancing_report import (
    _validate_benchmark_artifact_hash,
)
from scripts.live.calc_live_performance import (
    align_benchmark_to_portfolio_dates,
    maybe_load_benchmark,
)


class FakeIndexStockApi:
    def __init__(self, names: dict[str, str], frame: pd.DataFrame | None = None):
        self.names = names
        self.frame = frame
        self.index_calls: list[tuple[str, str, str, bool]] = []

    def get_index_ticker_list(self, date: str, market: str) -> list[str]:
        if market != "KRX":
            raise AssertionError("resolver must use the KRX index master")
        return list(self.names)

    def get_index_ticker_name(self, identifier: str) -> str:
        return self.names[identifier]

    def get_index_ohlcv_by_date(
        self,
        start: str,
        end: str,
        identifier: str,
        *,
        name_display: bool,
    ) -> pd.DataFrame:
        self.index_calls.append((start, end, identifier, name_display))
        if self.frame is None:
            raise AssertionError("fixture frame is required")
        return self.frame.copy()

    def get_etf_ohlcv_by_date(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("ETF adapter must never be called")

    def get_market_ohlcv_by_date(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("listed-security adapter must never be called")


def official_benchmark_frame(dates: list[str], levels: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": dates,
            "index_level": levels,
            "benchmark_name": BENCHMARK_NAME,
            "benchmark_identifier": "5300",
            "asset_type": BENCHMARK_ASSET_TYPE,
            "return_type": BENCHMARK_RETURN_TYPE,
            "source": BENCHMARK_SOURCE,
            "index_master_market": "KRX",
        }
    )


class Krx300BenchmarkTests(unittest.TestCase):
    def test_exact_name_is_resolved_from_official_index_master(self) -> None:
        api = FakeIndexStockApi({"5100": "KRX 100", "5300": "KRX 300"})
        identifier, matches = resolve_official_index_identifier(
            api, "2026-08-18", BENCHMARK_NAME
        )
        self.assertEqual(identifier, "5300")
        self.assertEqual(matches, [{"index_identifier": "5300", "index_name": "KRX 300"}])

    def test_full_official_index_master_adapter_response_is_preserved(self) -> None:
        api = FakeIndexStockApi({"5100": "KRX 100", "5300": "KRX 300"})
        identifier, matches, response = collect_official_index_master_response(
            api, "2026-08-18", BENCHMARK_NAME
        )
        self.assertEqual(identifier, "5300")
        self.assertEqual(matches, [{"index_identifier": "5300", "index_name": "KRX 300"}])
        self.assertEqual(
            response["rows"],
            [
                {"index_identifier": "5100", "index_name": "KRX 100"},
                {"index_identifier": "5300", "index_name": "KRX 300"},
            ],
        )
        self.assertEqual(response["artifact_type"], "OFFICIAL_KRX_INDEX_MASTER_ADAPTER_RESPONSE")

    def test_collection_seals_distinct_raw_master_series_and_normalized_artifacts(self) -> None:
        frame = pd.DataFrame(
            {"종가": [1000.0, 1010.0], "거래량": [123, 456]},
            index=pd.to_datetime(["2026-08-17", "2026-08-18"]),
        )
        frame.index.name = "날짜"
        api = FakeIndexStockApi({"5100": "KRX 100", "5300": "KRX 300"}, frame)
        fake_pykrx = types.ModuleType("pykrx")
        fake_pykrx.stock = api
        with tempfile.TemporaryDirectory() as tmp:
            normalized = Path(tmp) / "benchmark_qa.csv"
            with patch.dict(sys.modules, {"pykrx": fake_pykrx}), patch.object(
                sys,
                "argv",
                [
                    "build_benchmark_krx300.py",
                    "--start", "2026-08-17",
                    "--end", "2026-08-18",
                    "--output_csv", str(normalized),
                ],
            ):
                build_benchmark_main()

            meta_path = normalized.with_suffix(".meta.json")
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            raw_master = Path(meta["raw_index_master_artifact_path"])
            raw_series = Path(meta["raw_index_series_artifact_path"])
            self.assertTrue(raw_master.is_file())
            self.assertTrue(raw_series.is_file())
            self.assertEqual(len({normalized.resolve(), raw_master.resolve(), raw_series.resolve()}), 3)
            self.assertIn("거래량", pd.read_csv(raw_series).columns)
            master_payload = json.loads(raw_master.read_text(encoding="utf-8"))
            self.assertEqual(len(master_payload["rows"]), 2)
            _validate_benchmark_artifact_hash(meta, normalized, raw_master, raw_series)

            raw_series.write_text(raw_series.read_text(encoding="utf-8-sig") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "raw artifact SHA-256 mismatch"):
                _validate_benchmark_artifact_hash(meta, normalized, raw_master, raw_series)

    def test_missing_or_ambiguous_exact_name_fails_closed(self) -> None:
        with self.assertRaises(RuntimeError):
            resolve_official_index_identifier(
                FakeIndexStockApi({"5100": "KRX 100"}), "2026-08-18"
            )
        with self.assertRaises(RuntimeError):
            resolve_official_index_identifier(
                FakeIndexStockApi({"5300": "KRX 300", "5999": "KRX 300"}),
                "2026-08-18",
            )

    def test_etf_identifier_is_rejected_even_if_master_fixture_is_bad(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "ETF identifier"):
            resolve_official_index_identifier(
                FakeIndexStockApi({"229200": "KRX 300"}), "2026-08-18"
            )

    def test_fetch_uses_only_official_index_ohlcv_adapter(self) -> None:
        frame = pd.DataFrame(
            {"종가": [1000.0, 1010.0]},
            index=pd.to_datetime(["2026-08-17", "2026-08-18"]),
        )
        frame.index.name = "날짜"
        api = FakeIndexStockApi({"5300": "KRX 300"}, frame)
        result = fetch_official_index_series(
            api,
            index_identifier="5300",
            benchmark_name=BENCHMARK_NAME,
            start_date="2026-08-17",
            end_date="2026-08-18",
        )
        self.assertEqual(api.index_calls, [("20260817", "20260818", "5300", False)])
        self.assertEqual(result["asset_type"].unique().tolist(), ["INDEX"])
        self.assertEqual(result["return_type"].unique().tolist(), ["PRICE"])
        self.assertEqual(result["source"].unique().tolist(), ["KRX_OFFICIAL_INDEX"])
        self.assertEqual(result["index_level"].tolist(), [1000.0, 1010.0])
        self.assertAlmostEqual(float(result.iloc[0]["benchmark_cum_return"]), 0.0)
        self.assertAlmostEqual(float(result.iloc[-1]["benchmark_cum_return"]), 0.01)
        self.assertAlmostEqual(
            float(result.iloc[-1]["benchmark_cum_return_compounded"]), 0.01
        )

    def test_official_metadata_and_return_formula_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "benchmark.csv"
            official_benchmark_frame(
                ["2026-08-17", "2026-08-18"], [1000.0, 1010.0]
            ).to_csv(path, index=False)
            benchmark = maybe_load_benchmark(
                str(path),
                "2026-08-17",
                "2026-08-18",
                require_official_index=True,
            )
        assert benchmark is not None
        self.assertEqual(benchmark.attrs["benchmark_metadata"]["asset_type"], "INDEX")
        self.assertEqual(benchmark.attrs["benchmark_metadata"]["return_type"], "PRICE")
        self.assertAlmostEqual(float(benchmark.iloc[0]["benchmark_cum_return"]), 0.0)
        self.assertAlmostEqual(float(benchmark.iloc[1]["benchmark_cum_return"]), 0.01)
        self.assertAlmostEqual(
            float(benchmark.iloc[1]["benchmark_cum_return_compounded"]), 0.01
        )

    def test_exact_portfolio_calendar_alignment_has_no_fill(self) -> None:
        portfolio = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-17", "2026-08-18"]),
                "nav": [100.0, 101.0],
                "cum_return": [0.0, 0.01],
            }
        )
        benchmark = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-17", "2026-08-18"]),
                "benchmark_price": [1000.0, 1005.0],
                "benchmark_daily_return": [0.0, 0.005],
                "benchmark_cum_return": [0.0, 0.005],
            }
        )
        aligned = align_benchmark_to_portfolio_dates(portfolio, benchmark)
        self.assertEqual(len(aligned), 2)
        self.assertFalse(aligned["benchmark_cum_return"].isna().any())
        self.assertEqual(float(aligned.iloc[0]["benchmark_cum_return"]), 0.0)

        missing_middle = benchmark.iloc[[0]].copy()
        with self.assertRaisesRegex(ValueError, "no-fill policy"):
            align_benchmark_to_portfolio_dates(portfolio, missing_middle)

    def test_extra_index_date_and_future_backfill_are_forbidden(self) -> None:
        portfolio = pd.DataFrame(
            {"date": pd.to_datetime(["2026-08-17"]), "nav": [100.0], "cum_return": [0.0]}
        )
        benchmark = pd.DataFrame(
            {
                "date": pd.to_datetime(["2026-08-17", "2026-08-18"]),
                "benchmark_price": [1000.0, 1005.0],
                "benchmark_daily_return": [0.0, 0.005],
                "benchmark_cum_return": [0.0, 0.005],
            }
        )
        with self.assertRaisesRegex(ValueError, "extra_benchmark_dates"):
            align_benchmark_to_portfolio_dates(portfolio, benchmark)

    def test_total_return_index_is_rejected_for_price_only_nav(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "benchmark.csv"
            frame = official_benchmark_frame(["2026-08-18"], [1000.0])
            frame["return_type"] = "TOTAL_RETURN"
            frame.to_csv(path, index=False)
            with self.assertRaisesRegex(ValueError, "must be PRICE"):
                maybe_load_benchmark(
                    str(path),
                    "2026-08-18",
                    "2026-08-18",
                    require_official_index=True,
                )

    def test_production_requires_krx_credentials_without_reading_values(self) -> None:
        clean_env = {key: value for key, value in os.environ.items() if key not in {"KRX_ID", "KRX_PW"}}
        with patch.dict(os.environ, clean_env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "missing"):
                require_krx_credentials()

    def test_performance_cli_preserves_official_metadata_and_recalculates_active_return(self) -> None:
        repo = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            holdings = root / "holdings.csv"
            prices = root / "prices.csv"
            benchmark = root / "benchmark.csv"
            output = root / "performance"
            pd.DataFrame(
                {"ticker": ["005930"], "name": ["fixture"], "shares": [1]}
            ).to_csv(holdings, index=False)
            pd.DataFrame(
                {
                    "ticker": ["005930", "005930"],
                    "date": ["2026-08-17", "2026-08-18"],
                    "close": [100.0, 110.0],
                }
            ).to_csv(prices, index=False)
            official_benchmark_frame(
                ["2026-08-17", "2026-08-18"], [1000.0, 1010.0]
            ).to_csv(benchmark, index=False)

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/live/calc_live_performance.py",
                    "--holdings_csv",
                    str(holdings),
                    "--non-production-fixture",
                    "--prices_daily",
                    str(prices),
                    "--total_capital",
                    "100",
                    "--capital_mode",
                    "total",
                    "--target_date",
                    "2026-08-17",
                    "--end_date",
                    "2026-08-18",
                    "--benchmark",
                    str(benchmark),
                    "--benchmark_name",
                    BENCHMARK_NAME,
                    "--require-official-index",
                    "--output_dir",
                    str(output),
                    "--tag",
                    "fixture",
                ],
                cwd=repo,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            summary = json.loads(
                (output / "live_performance_summary__fixture.json").read_text(encoding="utf-8")
            )
            daily = pd.read_csv(output / "live_performance_daily__fixture.csv")

        self.assertEqual(summary["benchmark_name"], "KRX 300")
        self.assertEqual(summary["benchmark_asset_type"], "INDEX")
        self.assertEqual(summary["benchmark_return_type"], "PRICE")
        self.assertEqual(summary["benchmark_source"], "KRX_OFFICIAL_INDEX")
        self.assertEqual(summary["benchmark_alignment_policy"], "EXACT_PORTFOLIO_TRADING_DATES_NO_FILL")
        self.assertFalse(summary["cash_dividends_included"])
        self.assertAlmostEqual(summary["benchmark_cum_return"], 0.01)
        self.assertAlmostEqual(summary["active_return"], 0.09)
        self.assertAlmostEqual(float(daily.iloc[0]["benchmark_cum_return"]), 0.0)
        self.assertAlmostEqual(float(daily.iloc[0]["cum_return"]), 0.0)
        self.assertAlmostEqual(float(daily.iloc[-1]["active_return"]), 0.09)
        self.assertEqual(summary["benchmark_daily_compounding_reconciliation_status"], "PASS")
        self.assertEqual(summary["benchmark_aligned_row_count"], 2)


if __name__ == "__main__":
    unittest.main()
