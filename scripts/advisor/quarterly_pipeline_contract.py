from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


PIPELINE_STEPS = (
    "preflight",
    "input_validation",
    "capital_basis_validation",
    "broker_account_import",
    "current_equity_portfolio",
    "monthly_portfolio_performance",
    "krx300_benchmark",
    "model_input_loading",
    "fresh_start_scoring",
    "top_k",
    "target_portfolio",
    "current_vs_target",
    "report_analysis_universe",
    "financial_metrics",
    "selected_details",
    "dropped_details",
    "subscriber_html",
    "pdf_render",
    "subscriber_qa",
    "public_bundle",
    "private_audit_bundle",
    "manifest",
    "immutable_publish",
)
STAGE_STATUSES = {"PENDING", "RUNNING", "PASS", "BLOCKED", "FAIL"}


@dataclass
class PipelineStage:
    name: str
    status: str = "PENDING"
    detail: str | None = None

    def set(self, status: str, detail: str | None = None) -> None:
        if status not in STAGE_STATUSES:
            raise ValueError(f"invalid pipeline stage status: {status}")
        self.status = status
        self.detail = detail


@dataclass
class PipelineLedger:
    stages: dict[str, PipelineStage] = field(
        default_factory=lambda: {name: PipelineStage(name) for name in PIPELINE_STEPS}
    )

    def start(self, name: str) -> None:
        self.stages[name].set("RUNNING")

    def pass_stage(self, name: str, detail: str | None = None) -> None:
        self.stages[name].set("PASS", detail)

    def fail_stage(self, name: str, detail: str, *, blocked: bool = False) -> None:
        self.stages[name].set("BLOCKED" if blocked else "FAIL", detail)

    def to_dict(self) -> dict[str, Any]:
        return {"steps": [asdict(self.stages[name]) for name in PIPELINE_STEPS]}


@dataclass(frozen=True)
class QuarterlyPipelineInputs:
    broker_account_file: Path
    account_asof: str
    model_information_asof: str
    reference_price_asof: str
    performance_start: str
    performance_end: str
    quarter_label: str
    report_title: str
    previous_portfolio_evidence: Path
    output_root: Path
    run_id: str
    strategy_config: Path
    target_cash_weight: float
    top_k: int
    model_input_mode: str
    validate_only: bool
    advisory_capital_krw: float | None = None
    capital_basis_file: Path | None = None
    source_model_run_dir: Path | None = None
    use_network_if_missing: bool = False

    @property
    def staging_root(self) -> Path:
        return self.output_root / f".run_id={self.run_id}.staging"

    @property
    def final_root(self) -> Path:
        return self.output_root / f"run_id={self.run_id}"


def load_strategy_contract(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(path)
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
    else:
        import yaml

        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError("strategy config root must be an object")
    return payload


def validate_pipeline_inputs(inputs: QuarterlyPipelineInputs) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", inputs.run_id):
        raise ValueError("run_id contains unsafe characters")
    for path in (
        inputs.broker_account_file,
        inputs.previous_portfolio_evidence,
        inputs.strategy_config,
    ):
        if not path.is_file():
            raise FileNotFoundError(path)
    dates = {
        name: __import__("pandas").Timestamp(value).normalize()
        for name, value in {
            "performance_start": inputs.performance_start,
            "performance_end": inputs.performance_end,
            "model_information_asof": inputs.model_information_asof,
            "reference_price_asof": inputs.reference_price_asof,
            "account_asof": inputs.account_asof,
        }.items()
    }
    if dates["performance_start"] > dates["performance_end"]:
        raise ValueError("performance_start must be on or before performance_end")
    if dates["model_information_asof"] > dates["reference_price_asof"]:
        raise ValueError("model_information_asof must be on or before reference_price_asof")
    if dates["reference_price_asof"] > dates["account_asof"]:
        raise ValueError("reference_price_asof must be on or before account_asof")
    if not 0 < float(inputs.target_cash_weight) < 1:
        raise ValueError("target_cash_weight must be between zero and one")
    if int(inputs.top_k) <= 0:
        raise ValueError("top_k must be positive")
    if inputs.model_input_mode not in {"collect", "existing-certified-run"}:
        raise ValueError("model_input_mode must be collect or existing-certified-run")
    if inputs.model_input_mode == "existing-certified-run":
        if inputs.source_model_run_dir is None or not inputs.source_model_run_dir.is_dir():
            raise FileNotFoundError("source_model_run_dir is required for existing-certified-run")
    if (inputs.advisory_capital_krw is None) == (inputs.capital_basis_file is None):
        raise ValueError("provide exactly one of advisory_capital_krw or capital_basis_file")
    if inputs.advisory_capital_krw is not None and float(inputs.advisory_capital_krw) <= 0:
        raise ValueError("advisory_capital_krw must be positive")
    if inputs.capital_basis_file is not None and not inputs.capital_basis_file.is_file():
        raise FileNotFoundError(inputs.capital_basis_file)
    if inputs.staging_root.exists() or inputs.final_root.exists():
        raise FileExistsError("staging or immutable final run already exists")
    strategy = load_strategy_contract(inputs.strategy_config)
    return {
        "status": "PASS",
        "dates": {name: str(value.date()) for name, value in dates.items()},
        "strategy_config_keys": sorted(strategy),
        "staging_collision": False,
        "final_collision": False,
    }
