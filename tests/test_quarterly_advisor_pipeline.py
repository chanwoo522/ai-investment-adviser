from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

import scripts.advisor.run_quarterly_advisor_pipeline as runner
from scripts.advisor.quarterly_pipeline_contract import (
    PIPELINE_STEPS,
    QuarterlyPipelineInputs,
    validate_pipeline_inputs,
)


def _inputs(tmp_path: Path, *, mode: str = "existing-certified-run") -> QuarterlyPipelineInputs:
    broker = tmp_path / "broker.xlsx"
    broker.write_bytes(b"fixture")
    evidence = tmp_path / "previous.csv"
    evidence.write_text("ticker,shares,position_value\n000001,1,100\n", encoding="utf-8")
    config = tmp_path / "pipeline.json"
    config.write_text(json.dumps({"weights": {"factor": 1.0}}), encoding="utf-8")
    source_repo = tmp_path / "source-repo"
    (source_repo / "scripts").mkdir(parents=True)
    source_run = source_repo / "data" / "model" / "run_id=fixture"
    source_run.mkdir(parents=True)
    (source_run / "run_manifest.json").write_text(
        json.dumps({"development_status": "PASS_CERTIFIED", "input_artifacts": []}),
        encoding="utf-8",
    )
    return QuarterlyPipelineInputs(
        broker_account_file=broker,
        account_asof="2027-05-14",
        model_information_asof="2027-05-13",
        reference_price_asof="2027-05-14",
        performance_start="2027-04-01",
        performance_end="2027-05-14",
        quarter_label="27Q2",
        report_title="Quarterly report",
        previous_portfolio_evidence=evidence,
        output_root=tmp_path / "runs",
        run_id="advisor_27Q2",
        strategy_config=config,
        target_cash_weight=0.10,
        top_k=10,
        model_input_mode=mode,
        validate_only=True,
        advisory_capital_krw=1000.0,
        source_model_run_dir=source_run if mode == "existing-certified-run" else None,
    )


def test_pipeline_has_exact_23_stage_contract() -> None:
    assert len(PIPELINE_STEPS) == 23
    assert PIPELINE_STEPS[0] == "preflight"
    assert PIPELINE_STEPS[-1] == "immutable_publish"


def test_cli_help_contains_required_modes() -> None:
    help_text = runner._parser().format_help()
    assert "--broker-account-file" in help_text
    assert "--model-input-mode" in help_text
    assert "--validate-only" in help_text
    assert "--source-model-run-dir" in help_text


def test_validate_only_creates_no_output(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    result = runner.validate_only(inputs)
    assert result["status"] == "PASS_VALIDATE_ONLY"
    assert result["output_created"] is False
    assert not inputs.staging_root.exists()
    assert not inputs.final_root.exists()


def test_collect_mode_validate_only_does_not_require_source_run(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path, mode="collect")
    result = runner.validate_only(inputs)
    assert result["model_input_mode"] == "collect"
    assert not inputs.output_root.exists()


def test_output_collision_fails_closed(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    inputs.final_root.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        validate_pipeline_inputs(inputs)


def test_capital_basis_is_mandatory_and_exclusive(tmp_path: Path) -> None:
    inputs = _inputs(tmp_path)
    missing = replace(inputs, advisory_capital_krw=None, capital_basis_file=None)
    with pytest.raises(ValueError, match="exactly one"):
        validate_pipeline_inputs(missing)
    basis = tmp_path / "capital.json"
    basis.write_text(json.dumps({"advisory_rebalance_capital": 1000}), encoding="utf-8")
    duplicate = replace(inputs, capital_basis_file=basis)
    with pytest.raises(ValueError, match="exactly one"):
        validate_pipeline_inputs(duplicate)


def test_failure_removes_only_created_staging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inputs = _inputs(tmp_path)
    source_repo = runner._discover_repo_root(inputs.source_model_run_dir)
    artifacts = source_repo / "data" / "fixtures"
    artifacts.mkdir(parents=True)
    roles = []
    for role, name in (
        ("certified_security_master", "master.parquet"),
        ("prices_daily", "prices.parquet"),
        ("marketdata", "market.parquet"),
    ):
        path = artifacts / name
        path.write_bytes(b"fixture")
        roles.append({"role": role, "path": path.relative_to(source_repo).as_posix()})
    (inputs.source_model_run_dir / "run_manifest.json").write_text(
        json.dumps({"development_status": "PASS_CERTIFIED", "input_artifacts": roles}),
        encoding="utf-8",
    )

    def fail_import(**_: object) -> None:
        raise RuntimeError("fixture importer failure")

    monkeypatch.setattr(runner, "import_broker_account_export", fail_import)
    with pytest.raises(RuntimeError, match="fixture importer failure"):
        runner.run_pipeline(inputs)
    assert not inputs.staging_root.exists()
    assert not inputs.final_root.exists()
    assert inputs.broker_account_file.is_file()
