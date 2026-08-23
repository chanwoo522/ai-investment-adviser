from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any, Iterable

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.advisor.full_reset_v2 import (  # noqa: E402
    build_account_valuation_snapshot,
    build_common_reference_price_snapshot,
    build_current_vs_target_v2,
    build_top_k_boundary_watchlist,
    build_v2_target_portfolio,
    calculate_account_asof_liquidation,
)
from scripts.advisor.immutable_run import (  # noqa: E402
    build_private_audit_bundle,
    publish_staging,
    relative_artifact_manifest,
    sha256_file,
)
from scripts.advisor.quarterly_pipeline_contract import (  # noqa: E402
    PipelineLedger,
    QuarterlyPipelineInputs,
    load_strategy_contract,
    validate_pipeline_inputs,
)
from scripts.data_pipeline.make_holdings_clean import import_broker_account_export  # noqa: E402
from scripts.live.build_benchmark_krx300 import (  # noqa: E402
    BENCHMARK_NAME,
    collect_official_index_master_response,
    fetch_official_index_series,
)
from scripts.qa.render_public_report import render_public_report  # noqa: E402
from scripts.subscriber_report.subscriber_components import (  # noqa: E402
    build_full_security_details,
    build_monthly_portfolio_performance,
    build_public_distribution_bundle,
    collect_and_score_fresh_start_model,
    generate_subscriber_quarterly_report,
    run_financial_metrics_for_report_universe,
    validate_subscriber_report,
)


def _write_json(path: Path, payload: Any) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_csv(path: Path, frame: pd.DataFrame) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _tree_digest(root: Path) -> dict[str, Any]:
    if not root.exists():
        return {"exists": False, "file_count": 0, "tree_sha256": None}
    records = []
    for path in sorted((item for item in root.rglob("*") if item.is_file()), key=lambda item: item.as_posix().lower()):
        records.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": int(path.stat().st_size),
                "sha256": sha256_file(path),
            }
        )
    encoded = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "exists": True,
        "file_count": len(records),
        "tree_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _discover_repo_root(path: Path) -> Path:
    for candidate in (path, *path.parents):
        if (candidate / "scripts").is_dir() and (candidate / "data").exists():
            return candidate
    raise RuntimeError(f"could not discover source repository root from {path}")


def _resolve_path(value: str | Path, *, config_path: Path, source_repo: Path | None = None) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    candidates = [config_path.parent / path, REPO_ROOT / path]
    if source_repo is not None:
        candidates.insert(0, source_repo / path)
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve()


def _manifest_role(source_run: Path, role: str, *, source_repo: Path) -> Path:
    manifest = json.loads((source_run / "run_manifest.json").read_text(encoding="utf-8"))
    matches = [row for row in manifest.get("input_artifacts", []) if row.get("role") == role and row.get("path")]
    if len(matches) != 1:
        raise ValueError(f"source run role is not unique: {role}")
    path = Path(str(matches[0]["path"]))
    return path if path.is_absolute() else (source_repo / path).resolve()


def _normalise_ticker(series: pd.Series) -> pd.Series:
    return series.astype(str).str.replace(r"\.0$", "", regex=True).str.replace(r"\D", "", regex=True).str.zfill(6)


def _strategy_definition(config: dict[str, Any], *, config_path: Path, source_repo: Path | None) -> dict[str, Any] | Path:
    value = config.get("strategy_definition") or config.get("strategy") or config
    if isinstance(value, str):
        return _resolve_path(value, config_path=config_path, source_repo=source_repo)
    if not isinstance(value, dict):
        raise ValueError("strategy_definition must be an object or a file path")
    result = dict(value)
    nested = result.get("strategy_config_path") or result.get("path")
    if nested:
        result["strategy_config_path"] = str(
            _resolve_path(str(nested), config_path=config_path, source_repo=source_repo)
        )
        result.pop("path", None)
    return result


def _source_protection_roots(
    inputs: QuarterlyPipelineInputs,
    config: dict[str, Any],
    *,
    source_repo: Path | None,
) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    if inputs.source_model_run_dir is not None:
        roots["source_model_run"] = inputs.source_model_run_dir
    configured_financial = config.get("source_financial_run")
    if configured_financial:
        roots["source_financial_run"] = _resolve_path(
            configured_financial, config_path=inputs.strategy_config, source_repo=source_repo
        )
    if source_repo is not None:
        for relative in ("data/production/latest", "data/production/latest.json"):
            candidate = source_repo / relative
            if candidate.exists():
                roots[f"production_{Path(relative).name}"] = candidate
    return roots


def _capture_protection(roots: dict[str, Path]) -> dict[str, Any]:
    captured = {}
    for name, path in roots.items():
        if path.is_file():
            captured[name] = {
                "kind": "file",
                "size": int(path.stat().st_size),
                "sha256": sha256_file(path),
            }
        else:
            captured[name] = {"kind": "tree", **_tree_digest(path)}
    return captured


def _load_capital_basis(inputs: QuarterlyPipelineInputs) -> tuple[float, str]:
    if inputs.advisory_capital_krw is not None:
        return float(inputs.advisory_capital_krw), "CLI_ADVISORY_CAPITAL_KRW"
    path = inputs.capital_basis_file
    if path is None:
        raise ValueError("capital basis is required")
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        for key in ("advisory_rebalance_capital", "advisory_capital_krw", "total_capital_krw"):
            if key in payload:
                return float(payload[key]), f"CAPITAL_BASIS_FILE:{key}"
    elif path.suffix.lower() in {".csv", ".txt"}:
        try:
            frame = pd.read_csv(path)
            for key in ("advisory_rebalance_capital", "advisory_capital_krw", "total_capital_krw"):
                if key in frame.columns and len(frame):
                    return float(frame.iloc[0][key]), f"CAPITAL_BASIS_FILE:{key}"
        except Exception:
            pass
        return float(path.read_text(encoding="utf-8").replace(",", "").strip()), "CAPITAL_BASIS_FILE:TEXT"
    raise ValueError("capital basis file does not contain a supported capital field")


def _load_benchmark(
    config: dict[str, Any],
    inputs: QuarterlyPipelineInputs,
    *,
    source_repo: Path | None,
) -> pd.DataFrame:
    path_value = config.get("official_krx300_path")
    if path_value:
        path = _resolve_path(path_value, config_path=inputs.strategy_config, source_repo=source_repo)
        frame = pd.read_csv(path)
    elif inputs.use_network_if_missing:
        with open(os.devnull, "w", encoding="utf-8") as sink:
            prior_stdout, prior_stderr = sys.stdout, sys.stderr
            try:
                sys.stdout = sink
                sys.stderr = sink
                from pykrx import stock
            finally:
                sys.stdout = prior_stdout
                sys.stderr = prior_stderr
        identifier, _, _ = collect_official_index_master_response(
            stock, inputs.performance_end, BENCHMARK_NAME
        )
        frame = fetch_official_index_series(
            stock,
            index_identifier=identifier,
            benchmark_name=BENCHMARK_NAME,
            start_date=inputs.performance_start,
            end_date=inputs.performance_end,
        )
    else:
        raise RuntimeError("official KRX300 input is required when network collection is disabled")
    if "krx300_price_index" not in frame.columns:
        price_column = next((column for column in ("price", "close", "index_level") if column in frame.columns), None)
        if price_column is None:
            raise ValueError("official KRX300 input lacks price-index values")
        frame = frame.rename(columns={price_column: "krx300_price_index"})
    frame["date"] = pd.to_datetime(frame["date"], errors="raise").dt.normalize()
    return frame


def _load_performance_prices(
    source_prices: Path,
    evidence: pd.DataFrame,
    config: dict[str, Any],
    inputs: QuarterlyPipelineInputs,
    *,
    source_repo: Path | None,
) -> pd.DataFrame:
    raw = pd.read_parquet(source_prices)
    close_column = next((column for column in ("Close", "close", "price") if column in raw.columns), None)
    if close_column is None:
        raise ValueError("certified prices lack official close")
    raw["ticker"] = _normalise_ticker(raw["ticker"])
    raw["date"] = pd.to_datetime(raw["date"], errors="raise").dt.normalize()
    raw = raw.rename(columns={close_column: "close"})
    raw = raw[["date", "ticker", "close"]]
    override_value = config.get("official_equity_price_overrides")
    if override_value:
        override_path = _resolve_path(
            override_value, config_path=inputs.strategy_config, source_repo=source_repo
        )
        override = pd.read_csv(override_path, dtype={"ticker": str})
        override_close = next((column for column in ("close", "official_close", "price", "reference_price") if column in override.columns), None)
        if override_close is None:
            raise ValueError("official equity price override lacks close")
        override["ticker"] = _normalise_ticker(override["ticker"])
        override["date"] = pd.to_datetime(override["date"], errors="raise").dt.normalize()
        override = override.rename(columns={override_close: "close"})[["date", "ticker", "close"]]
        raw = pd.concat([raw, override], ignore_index=True).drop_duplicates(["date", "ticker"], keep="last")
    tickers = set(evidence["ticker"])
    result = raw.loc[
        raw["ticker"].isin(tickers)
        & raw["date"].between(pd.Timestamp(inputs.performance_start), pd.Timestamp(inputs.performance_end))
    ].copy()
    end_coverage = set(result.loc[result["date"].eq(pd.Timestamp(inputs.performance_end)), "ticker"])
    if end_coverage != tickers:
        raise RuntimeError("official equity performance prices do not cover every ticker at performance_end")
    return result


def _prepare_imported_holdings(
    imported: dict[str, Any],
    security_master_path: Path,
    *,
    output_dir: Path,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    holdings = pd.read_csv(imported["holdings_path"], dtype={"ticker": str})
    holdings["ticker"] = _normalise_ticker(holdings["ticker"])
    master = pd.read_parquet(security_master_path) if security_master_path.suffix.lower() == ".parquet" else pd.read_csv(security_master_path, dtype={"ticker": str})
    master["ticker"] = _normalise_ticker(master["ticker"])
    market_column = next((column for column in ("market", "market_name", "시장구분") if column in master.columns), None)
    market = master[["ticker"]].copy()
    market["market"] = master[market_column] if market_column else "UNKNOWN"
    holdings = holdings.merge(market.drop_duplicates("ticker"), on="ticker", how="left", validate="one_to_one")
    asset_type = holdings.get("asset_type", pd.Series("EQUITY", index=holdings.index)).astype(str)
    holdings["asset_class"] = asset_type.map(
        lambda value: "CASH_EQUIVALENT" if "CASH_EQUIVALENT" in value else ("EQUITY" if value in {"EQUITY", "STOCK"} else "UNSUPPORTED")
    )
    reserve = holdings["ticker"].eq("437350")
    holdings.loc[reserve, "asset_class"] = "CASH_EQUIVALENT"
    holdings["current_qty"] = pd.to_numeric(holdings["shares"], errors="raise")
    holdings["equity_universe_eligible"] = holdings["asset_class"].eq("EQUITY")
    holdings["factor_eligible"] = holdings["asset_class"].eq("EQUITY")
    holdings["top_k_eligible"] = holdings["asset_class"].eq("EQUITY")
    holdings["holding_bonus_eligible"] = False
    holdings["keep_current_eligible"] = False
    holdings["equity_position_count_excluded"] = ~holdings["asset_class"].eq("EQUITY")
    holdings["counts_toward_cash_target"] = holdings["asset_class"].eq("CASH_EQUIVALENT")
    canonical_path = output_dir / "holdings_clean.csv"
    _write_csv(canonical_path, holdings)
    classification_columns = [
        "ticker",
        "name",
        "asset_class",
        "equity_universe_eligible",
        "factor_eligible",
        "top_k_eligible",
        "holding_bonus_eligible",
        "keep_current_eligible",
        "equity_position_count_excluded",
        "counts_toward_cash_target",
    ]
    _write_csv(
        output_dir / "asset_classification.csv",
        holdings[classification_columns],
    )
    snapshot = dict(imported["snapshot"])
    _write_json(output_dir / "account_snapshot.json", snapshot)
    _write_json(output_dir / "account_import_manifest.json", imported["manifest"])
    return holdings, snapshot


def validate_only(inputs: QuarterlyPipelineInputs) -> dict[str, Any]:
    validation = validate_pipeline_inputs(inputs)
    config = load_strategy_contract(inputs.strategy_config)
    source_repo = (
        _discover_repo_root(inputs.source_model_run_dir)
        if inputs.source_model_run_dir is not None
        else None
    )
    roots = _source_protection_roots(inputs, config, source_repo=source_repo)
    protection = _capture_protection(roots)
    capital, capital_source = _load_capital_basis(inputs)
    callables = (
        import_broker_account_export,
        build_monthly_portfolio_performance,
        collect_and_score_fresh_start_model,
        run_financial_metrics_for_report_universe,
        build_full_security_details,
        generate_subscriber_quarterly_report,
        validate_subscriber_report,
        build_public_distribution_bundle,
        render_public_report,
        publish_staging,
    )
    return {
        "status": "PASS_VALIDATE_ONLY",
        "input_validation": validation,
        "capital_basis": {"amount": capital, "source": capital_source},
        "model_input_mode": inputs.model_input_mode,
        "generic_callable_count": len(callables),
        "generic_callables_available": all(callable(item) for item in callables),
        "network_key_state": {
            "DART_API_KEY": bool(os.getenv("DART_API_KEY")),
            "KRX_ID": bool(os.getenv("KRX_ID")),
            "KRX_PW": bool(os.getenv("KRX_PW")),
        },
        "protected_source_state": protection,
        "output_created": False,
    }


def run_pipeline(inputs: QuarterlyPipelineInputs) -> Path:
    ledger = PipelineLedger()
    staging = inputs.staging_root
    final = inputs.final_root
    active_stage = "preflight"
    try:
        ledger.start("preflight")
        validation = validate_pipeline_inputs(inputs)
        config = load_strategy_contract(inputs.strategy_config)
        source_repo = (
            _discover_repo_root(inputs.source_model_run_dir)
            if inputs.source_model_run_dir is not None
            else None
        )
        protection_roots = _source_protection_roots(inputs, config, source_repo=source_repo)
        protected_before = _capture_protection(protection_roots)
        ledger.pass_stage("preflight")

        active_stage = "input_validation"
        ledger.start(active_stage)
        source_manifest = inputs.source_model_run_dir / "run_manifest.json" if inputs.source_model_run_dir else None
        if source_manifest is not None and not source_manifest.is_file():
            raise FileNotFoundError(source_manifest)
        ledger.pass_stage(active_stage)

        active_stage = "capital_basis_validation"
        ledger.start(active_stage)
        certified_capital, capital_basis = _load_capital_basis(inputs)
        if certified_capital <= 0:
            raise ValueError("certified advisory capital must be positive")
        ledger.pass_stage(active_stage, capital_basis)

        staging.mkdir(parents=True, exist_ok=False)
        active_stage = "broker_account_import"
        ledger.start(active_stage)
        if inputs.model_input_mode == "existing-certified-run":
            assert inputs.source_model_run_dir is not None and source_repo is not None
            security_master_path = _manifest_role(inputs.source_model_run_dir, "certified_security_master", source_repo=source_repo)
            prices_path = _manifest_role(inputs.source_model_run_dir, "prices_daily", source_repo=source_repo)
            marketdata_path = _manifest_role(inputs.source_model_run_dir, "marketdata", source_repo=source_repo)
        else:
            security_master_path = _resolve_path(config["certified_security_master"], config_path=inputs.strategy_config, source_repo=source_repo)
            prices_path = _resolve_path(config["prices_daily"], config_path=inputs.strategy_config, source_repo=source_repo)
            marketdata_path = _resolve_path(config["marketdata"], config_path=inputs.strategy_config, source_repo=source_repo)
        classification_value = config.get("security_classification_path")
        classification_path = (
            _resolve_path(classification_value, config_path=inputs.strategy_config, source_repo=source_repo)
            if classification_value
            else None
        )
        imported = import_broker_account_export(
            broker_account_file=inputs.broker_account_file,
            output_dir=staging / "account_import/canonical",
            account_asof=inputs.account_asof,
            master_path=security_master_path,
            security_classification_path=classification_path,
        )
        holdings, snapshot = _prepare_imported_holdings(
            imported, security_master_path, output_dir=staging / "account_import"
        )
        if snapshot.get("account_snapshot_status") != "VERIFIED":
            raise RuntimeError("broker account snapshot is not VERIFIED")
        ledger.pass_stage(active_stage)

        active_stage = "current_equity_portfolio"
        ledger.start(active_stage)
        liability = float(snapshot.get("credit_or_loan_amount") or 0.0)
        valuation_holdings = holdings.copy()
        if liability > 0:
            liability_row = {column: pd.NA for column in valuation_holdings.columns}
            liability_row.update(
                {
                    "ticker": "999999",
                    "name": "명시적 계좌 부채",
                    "asset_class": "LIABILITY",
                    "market": "NOT_APPLICABLE",
                    "broker_market_value": -liability,
                    "broker_market_price": pd.NA,
                    "shares": pd.NA,
                }
            )
            valuation_holdings = pd.concat([valuation_holdings, pd.DataFrame([liability_row])], ignore_index=True)
        account_valuation, valuation_summary = build_account_valuation_snapshot(
            valuation_holdings, snapshot
        )
        current_equity = account_valuation.loc[account_valuation["asset_class"].eq("EQUITY")].copy()
        current_public = current_equity[
            ["ticker", "name", "current_qty", "account_asof_price", "account_asof_value", "current_weight", "account_valuation_asof"]
        ].rename(
            columns={
                "current_qty": "quantity",
                "account_asof_price": "price_per_share",
                "account_asof_value": "market_value",
                "account_valuation_asof": "valuation_date",
                "current_weight": "equity_weight",
            }
        )
        current_public["equity_weight"] = current_public["market_value"] / float(current_public["market_value"].sum())
        _write_csv(staging / "current_portfolio.csv", current_public)
        ledger.pass_stage(active_stage)

        active_stage = "monthly_portfolio_performance"
        ledger.start(active_stage)
        evidence = pd.read_csv(inputs.previous_portfolio_evidence, dtype={"ticker": str})
        if "shares" not in evidence.columns and "quantity" in evidence.columns:
            evidence = evidence.rename(columns={"quantity": "shares"})
        if "position_value" not in evidence.columns:
            source_column = next((column for column in ("start_value", "target_value", "illustrative_target_value") if column in evidence.columns), None)
            if source_column is None:
                raise ValueError("previous portfolio evidence lacks position_value")
            evidence = evidence.rename(columns={source_column: "position_value"})
        evidence["ticker"] = _normalise_ticker(evidence["ticker"])
        evidence = evidence.loc[evidence["ticker"].str.fullmatch(r"\d{6}")].copy()
        if "name" not in evidence.columns:
            names = pd.read_parquet(security_master_path) if security_master_path.suffix.lower() == ".parquet" else pd.read_csv(security_master_path)
            names["ticker"] = _normalise_ticker(names["ticker"])
            name_column = next((column for column in ("name", "name_final", "한글종목명", "종목명") if column in names.columns), None)
            evidence = evidence.merge(names[["ticker", name_column]].rename(columns={name_column: "name"}), on="ticker", how="left")
        performance_prices = _load_performance_prices(
            prices_path, evidence, config, inputs, source_repo=source_repo
        )
        ledger.pass_stage(active_stage, "official equity inputs prepared")

        active_stage = "krx300_benchmark"
        ledger.start(active_stage)
        benchmark = _load_benchmark(config, inputs, source_repo=source_repo)
        performance = build_monthly_portfolio_performance(
            starting_positions=evidence,
            official_equity_prices=performance_prices,
            official_krx300=benchmark,
            performance_start=inputs.performance_start,
            performance_end=inputs.performance_end,
            activity_ledger=None,
            require_quantity_parity=True,
        )
        _write_csv(staging / "prior_model_performance.csv", performance.monthly_performance)
        _write_csv(staging / "security_return_evaluation.csv", performance.security_returns)
        actual_performance = pd.DataFrame(
            [
                {
                    "performance_status": "PROVISIONAL_ACCOUNT_PERFORMANCE",
                    "account_asof": inputs.account_asof,
                    "activity_ledger_status": "NOT_AVAILABLE",
                    "new_advisory_generation_blocked": False,
                }
            ]
        )
        _write_csv(staging / "actual_account_performance.csv", actual_performance)
        ledger.pass_stage(active_stage)

        active_stage = "model_input_loading"
        ledger.start(active_stage)
        strategy = _strategy_definition(config, config_path=inputs.strategy_config, source_repo=source_repo)
        model_result = collect_and_score_fresh_start_model(
            information_asof=inputs.model_information_asof,
            target_date=config.get("target_date", inputs.reference_price_asof),
            metric=str(config.get("metric", "revenue_op")),
            feature_versions={**dict(config.get("feature_versions", {})), "top_k": inputs.top_k},
            strategy_config=strategy,
            output_dir=staging / "model",
            collection_mode=inputs.model_input_mode,
            existing_certified_run=inputs.source_model_run_dir,
        )
        ledger.pass_stage(active_stage)

        active_stage = "fresh_start_scoring"
        ledger.start(active_stage)
        scores = model_result.fresh_start_scores.copy()
        top_k = model_result.fresh_start_top_k.copy().sort_values("model_rank").head(inputs.top_k)
        if len(top_k) != inputs.top_k:
            raise RuntimeError("fresh-start Top-K count mismatch")
        if "holding_bonus" in scores.columns and not pd.to_numeric(scores["holding_bonus"], errors="coerce").fillna(0).eq(0).all():
            raise RuntimeError("fresh-start holding bonus is nonzero")
        _write_csv(staging / "fresh_start_scores.csv", scores)
        _write_csv(staging / "fresh_start_top_k.csv", top_k)
        ledger.pass_stage(active_stage)

        active_stage = "top_k"
        ledger.start(active_stage)
        certified_boundary = (
            inputs.source_model_run_dir / "top_k_boundary_watchlist.csv"
            if inputs.model_input_mode == "existing-certified-run"
            and inputs.source_model_run_dir is not None
            else None
        )
        boundary_path = model_result.artifact_paths.get("top_k_boundary_watchlist")
        if certified_boundary is not None and certified_boundary.is_file():
            boundary = pd.read_csv(certified_boundary, dtype={"ticker": str})
        elif boundary_path is not None and Path(boundary_path).is_file():
            boundary = pd.read_csv(boundary_path, dtype={"ticker": str})
        else:
            boundary, _ = build_top_k_boundary_watchlist(
                scores,
                top_k=inputs.top_k,
                fragile_threshold=float(config.get("boundary_fragile_threshold", 0.05)),
            )
        _write_csv(staging / "top_k_boundary_watchlist.csv", boundary)
        ledger.pass_stage(active_stage)

        active_stage = "target_portfolio"
        ledger.start(active_stage)
        liquidation, capital_summary = calculate_account_asof_liquidation(
            account_valuation,
            commission_rate=float(config.get("commission_rate", 0.00015)),
            sell_tax_rate_by_market=dict(config.get("sell_tax_rate_by_market", {"KOSPI": 0.002, "KOSDAQ": 0.002})),
            default_sell_tax_rate=float(config.get("default_sell_tax_rate", 0.002)),
            liability_value=liability,
        )
        tolerance = float(config.get("capital_reconciliation_tolerance_krw", 1.0))
        calculated_capital = float(capital_summary["advisory_rebalance_capital"])
        if abs(certified_capital - calculated_capital) > tolerance:
            raise RuntimeError(
                f"BLOCKED_CAPITAL_BASIS_MISMATCH: certified={certified_capital} calculated={calculated_capital}"
            )
        capital_summary["capital_basis"] = capital_basis
        capital_summary["certified_advisory_capital_krw"] = certified_capital
        _write_csv(staging / "hypothetical_full_liquidation.csv", liquidation)
        _write_json(staging / "advisory_capital_summary.json", capital_summary)
        reference_prices, reference_summary = build_common_reference_price_snapshot(
            prices_path,
            top_k["ticker"].tolist(),
            requested_asof=inputs.reference_price_asof,
        )
        if not reference_summary["all_selected_prices_same_cutoff"]:
            raise RuntimeError("reference price common-cutoff contract failed")
        target, target_summary = build_v2_target_portfolio(
            top_k,
            reference_prices,
            advisory_rebalance_capital=certified_capital,
            target_cash_equivalent_weight=inputs.target_cash_weight,
            buy_commission_rate=float(config.get("buy_commission_rate", 0.00015)),
        )
        _write_csv(staging / "target_portfolio.csv", target)
        _write_json(staging / "target_portfolio_summary.json", {**target_summary, **reference_summary})
        ledger.pass_stage(active_stage)

        active_stage = "current_vs_target"
        ledger.start(active_stage)
        comparison = build_current_vs_target_v2(account_valuation, target)
        _write_csv(staging / "current_vs_target.csv", comparison)
        ledger.pass_stage(active_stage)

        active_stage = "report_analysis_universe"
        ledger.start(active_stage)
        current_tickers = set(current_equity["ticker"])
        analysis_tickers = list(top_k["ticker"]) + [ticker for ticker in current_equity["ticker"] if ticker not in set(top_k["ticker"])]
        analysis = pd.DataFrame({"ticker": analysis_tickers})
        analysis = analysis.merge(scores, on="ticker", how="left")
        analysis = analysis.merge(
            comparison.loc[comparison["asset_class"].eq("EQUITY")],
            on="ticker",
            how="left",
            suffixes=("", "_comparison"),
        )
        analysis["model_selected"] = analysis["ticker"].isin(set(top_k["ticker"]))
        analysis["selection_status"] = analysis["model_selected"].map({True: "선정", False: "미선발"})
        if "name" not in analysis.columns:
            analysis["name"] = analysis.get("name_comparison", analysis["ticker"])
        analysis["selection_group"] = analysis["model_selected"].map({True: "SELECTED", False: "DROPPED_EXISTING"})
        _write_csv(staging / "report_analysis_equity_universe.csv", analysis)
        ledger.pass_stage(active_stage)

        active_stage = "financial_metrics"
        ledger.start(active_stage)
        source_financial_value = config.get("source_financial_run")
        source_financial = (
            _resolve_path(source_financial_value, config_path=inputs.strategy_config, source_repo=source_repo)
            if source_financial_value
            else None
        )
        financial_result = run_financial_metrics_for_report_universe(
            report_analysis_equities=analysis,
            information_asof=inputs.model_information_asof,
            price_asof=inputs.reference_price_asof,
            output_root=staging / "financial_metrics",
            source_financial_run=source_financial,
            source_marketdata=marketdata_path,
            source_security_master=security_master_path,
            dart_cache_root=(
                _resolve_path(config["dart_cache_root"], config_path=inputs.strategy_config, source_repo=source_repo)
                if config.get("dart_cache_root")
                else None
            ),
            use_network_if_missing=inputs.use_network_if_missing,
        )
        ledger.pass_stage(active_stage)

        active_stage = "selected_details"
        ledger.start(active_stage)
        details = build_full_security_details(
            selected_equities=top_k,
            current_equities=current_equity,
            fresh_start_scores=scores,
            current_vs_target=comparison.loc[comparison["asset_class"].eq("EQUITY")],
            financial_metrics=financial_result.artifact_paths["report_analysis_metrics"],
            information_asof=inputs.model_information_asof,
            price_asof=inputs.reference_price_asof,
            output_root=staging,
        )
        ledger.pass_stage(active_stage)
        active_stage = "dropped_details"
        ledger.start(active_stage)
        if len(details.dropped_details) != len(current_tickers.difference(set(top_k["ticker"]))):
            raise RuntimeError("dropped existing equity count mismatch")
        ledger.pass_stage(active_stage)

        active_stage = "subscriber_html"
        ledger.start(active_stage)
        sector_columns = details.selected_details[["ticker", "advisor_sector"]].drop_duplicates("ticker") if "advisor_sector" in details.selected_details.columns else pd.DataFrame({"ticker": top_k["ticker"], "advisor_sector": "미분류"})
        report_top_k = top_k.merge(sector_columns, on="ticker", how="left", suffixes=("", "_detail"))
        if "advisor_sector_detail" in report_top_k.columns:
            report_top_k["advisor_sector"] = report_top_k.get("advisor_sector").fillna(report_top_k["advisor_sector_detail"])
        report_target = target.merge(sector_columns, on="ticker", how="left")
        report_result = generate_subscriber_quarterly_report(
            quarter_label=inputs.quarter_label,
            report_title=inputs.report_title,
            model_information_asof=inputs.model_information_asof,
            account_asof=inputs.account_asof,
            performance_start=inputs.performance_start,
            performance_end=inputs.performance_end,
            benchmark_name="KRX300",
            current_portfolio=current_public,
            monthly_performance=performance.monthly_performance,
            security_returns=performance.security_returns,
            fresh_start_top_k=report_top_k,
            boundary_watchlist=boundary,
            target_portfolio=report_target,
            current_vs_target=comparison,
            selected_details=details.selected_details,
            dropped_summary=details.dropped_details,
            dropped_details=details.dropped_details,
            output_html=staging / "quarterly_advisor_report.html",
        )
        ledger.pass_stage(active_stage)

        active_stage = "pdf_render"
        ledger.start(active_stage)
        render_public_report(
            html_path=report_result.html_path,
            pdf_path=staging / "quarterly_advisor_report.pdf",
            desktop_path=staging / "visual_qa/desktop_1440.png",
            mobile_path=staging / "visual_qa/mobile_390.png",
            pdf_pages_dir=staging / "visual_qa/pdf_pages",
            render_metadata_path=staging / "visual_qa/render_metadata.json",
            pdf_page_dpi=144,
        )
        ledger.pass_stage(active_stage)

        active_stage = "subscriber_qa"
        ledger.start(active_stage)
        validation_result = validate_subscriber_report(
            html_path=staging / "quarterly_advisor_report.html",
            pdf_path=staging / "quarterly_advisor_report.pdf",
            render_metadata_path=staging / "visual_qa/render_metadata.json",
            expected_title=inputs.report_title,
            expected_selected_count=len(top_k),
            expected_dropped_count=len(details.dropped_details),
            expected_performance_start=inputs.performance_start,
            expected_performance_end=inputs.performance_end,
            expected_target_weight_total=1.0,
            public_forbidden_terms=tuple(config.get("public_forbidden_terms", [])),
        )
        if validation_result.status != "PASS":
            raise RuntimeError(f"subscriber QA failed: {validation_result.details['failed_checks']}")
        qa_markdown = (
            "# ADVISOR REPORT QA\n\n"
            "- status: PASS\n"
            f"- selected equities: {len(top_k)}\n"
            f"- dropped existing equities: {len(details.dropped_details)}\n"
            "- EPS/net income/PER coverage: PASS\n"
            "- target weight reconciliation: PASS\n"
            "- privacy and PDF page render: PASS\n"
            "- order submission/fill reconciliation: NOT APPLICABLE\n"
        )
        (staging / "ADVISOR_REPORT_QA.md").write_text(qa_markdown, encoding="utf-8")
        _write_json(staging / "subscriber_validation.json", {"status": validation_result.status, "checks": validation_result.checks, "details": validation_result.details})
        ledger.pass_stage(active_stage)

        active_stage = "public_bundle"
        ledger.start(active_stage)
        public_members = {
            "quarterly_advisor_report.html": staging / "quarterly_advisor_report.html",
            "quarterly_advisor_report.pdf": staging / "quarterly_advisor_report.pdf",
            "selected_and_dropped_security_public_financials.csv": details.artifact_paths["public_financials"],
            "selected_and_dropped_security_public_valuation.csv": details.artifact_paths["public_valuation"],
            "dropped_existing_security_summary.csv": details.artifact_paths["dropped_summary"],
            **{
                f"charts/{name}": path for name, path in report_result.chart_paths.items()
            },
        }
        build_public_distribution_bundle(
            output_path=staging / "public_distribution_bundle.zip",
            public_files=public_members,
            allowed_suffixes=(".html", ".pdf", ".csv", ".svg"),
            forbidden_patterns=tuple(config.get("public_forbidden_patterns", [])),
        )
        ledger.pass_stage(active_stage)

        active_stage = "private_audit_bundle"
        ledger.start(active_stage)
        private_allowlist = (
            "current_portfolio.csv",
            "hypothetical_full_liquidation.csv",
            "advisory_capital_summary.json",
            "fresh_start_scores.csv",
            "fresh_start_top_k.csv",
            "target_portfolio.csv",
            "current_vs_target.csv",
            "prior_model_performance.csv",
            "actual_account_performance.csv",
            "report_analysis_equity_metrics.csv",
            "ADVISOR_REPORT_QA.md",
            "subscriber_validation.json",
        )
        build_private_audit_bundle(
            run_root=staging,
            output_path=staging / "private_audit_bundle.zip",
            allowlist=private_allowlist,
            forbidden_source_sha256=str(snapshot["source_file_sha256"]),
        )
        ledger.pass_stage(active_stage)

        active_stage = "manifest"
        ledger.start(active_stage)
        protected_after = _capture_protection(protection_roots)
        if protected_after != protected_before:
            raise RuntimeError("protected production/latest or authoritative source state changed")
        ledger.pass_stage(active_stage)
        active_stage = "immutable_publish"
        ledger.start(active_stage)
        ledger.pass_stage(active_stage, "atomic publish gate passed; final directory presence certifies commit")
        manifest = {
            "contract": "PARAMETERIZED_QUARTERLY_ADVISOR_PIPELINE_V1",
            "mode": "ADVISOR_FULL_RESET",
            "run_id": inputs.run_id,
            "quarter_label": inputs.quarter_label,
            "development_status": "PASS_PARAMETERIZED_QUARTERLY_ADVISOR_RUNNER",
            "component_parameterization_status": "PASS_QUARTERLY_COMPONENT_PARAMETERIZATION",
            "holding_bonus": 0.0,
            "keep_current_top_n": 0,
            "current_account_membership_used_for_selection": False,
            "order_submission_enabled": False,
            "execution_phase_enabled": False,
            "fill_reconciliation_enabled": False,
            "raw_broker_source_copied": False,
            "activity_ledger_status": "NOT_AVAILABLE",
            "actual_account_performance_status": "PROVISIONAL_ACCOUNT_PERFORMANCE",
            "protected_source_state_before": protected_before,
            "protected_source_state_after": protected_after,
            "pipeline": ledger.to_dict(),
            "artifacts": relative_artifact_manifest(staging, exclude=("run_manifest.json",)),
        }
        _write_json(staging / "run_manifest.json", manifest)
        publish_staging(staging, final)
        return final
    except Exception as exc:
        if active_stage in ledger.stages and ledger.stages[active_stage].status == "RUNNING":
            ledger.fail_stage(active_stage, f"{type(exc).__name__}: {exc}", blocked="BLOCKED" in str(exc))
        if staging.exists():
            resolved = staging.resolve()
            expected_parent = inputs.output_root.resolve()
            if resolved.parent != expected_parent or not resolved.name.startswith(".run_id=") or not resolved.name.endswith(".staging"):
                raise RuntimeError(f"refusing unsafe staging cleanup: {resolved}") from exc
            shutil.rmtree(resolved)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an immutable parameterized quarterly ADVISOR_FULL_RESET pipeline"
    )
    parser.add_argument("--broker-account-file", required=True)
    parser.add_argument("--account-asof", required=True)
    parser.add_argument("--model-information-asof", required=True)
    parser.add_argument("--reference-price-asof", required=True)
    parser.add_argument("--performance-start", required=True)
    parser.add_argument("--performance-end", required=True)
    parser.add_argument("--quarter-label", required=True)
    parser.add_argument("--report-title", required=True)
    parser.add_argument("--previous-portfolio-evidence", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--strategy-config", required=True)
    parser.add_argument("--target-cash-weight", required=True, type=float)
    parser.add_argument("--top-k", required=True, type=int)
    parser.add_argument("--model-input-mode", required=True, choices=("collect", "existing-certified-run"))
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--advisory-capital-krw", type=float)
    parser.add_argument("--capital-basis-file")
    parser.add_argument("--source-model-run-dir")
    parser.add_argument("--use-network-if-missing", action="store_true")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    inputs = QuarterlyPipelineInputs(
        broker_account_file=Path(args.broker_account_file).resolve(),
        account_asof=args.account_asof,
        model_information_asof=args.model_information_asof,
        reference_price_asof=args.reference_price_asof,
        performance_start=args.performance_start,
        performance_end=args.performance_end,
        quarter_label=args.quarter_label,
        report_title=args.report_title,
        previous_portfolio_evidence=Path(args.previous_portfolio_evidence).resolve(),
        output_root=Path(args.output_root).resolve(),
        run_id=args.run_id,
        strategy_config=Path(args.strategy_config).resolve(),
        target_cash_weight=args.target_cash_weight,
        top_k=args.top_k,
        model_input_mode=args.model_input_mode,
        validate_only=bool(args.validate_only),
        advisory_capital_krw=args.advisory_capital_krw,
        capital_basis_file=Path(args.capital_basis_file).resolve() if args.capital_basis_file else None,
        source_model_run_dir=Path(args.source_model_run_dir).resolve() if args.source_model_run_dir else None,
        use_network_if_missing=bool(args.use_network_if_missing),
    )
    if inputs.validate_only:
        print(json.dumps(validate_only(inputs), ensure_ascii=False, indent=2))
        return 0
    print(run_pipeline(inputs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
