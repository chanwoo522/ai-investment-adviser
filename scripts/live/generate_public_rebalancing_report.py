from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.common.security_id import normalize_security_id_series


PUBLIC_REPORT_CONTRACT = "PUBLIC_REBALANCING_REPORT"
MODEL_EXECUTION_CONTRACT = "MODEL_ONLY_EXECUTION_PLAN"
MODEL_EXECUTION_SCHEMA_VERSION = "model-only-execution-plan/v1"
MODEL_EXECUTION_READY_SCHEMA_VERSION = "model-only-execution/v1"
MAX_HTML_BYTES = 150_000
PUBLIC_SECTIONS = (
    "한눈에 보기",
    "지난 운용 성과",
    "모델이 선택한 다음 포트폴리오",
    "모델 기준 매매계획",
    "데이터 기준과 유의사항",
)
COST_DISCLOSURE = (
    "예상 거래비용은 매매수수료와 법정 매도세금만 반영했습니다. "
    "슬리피지와 시장충격 비용은 계산하지 않습니다. "
    "실제 비용은 증권사와 체결조건에 따라 달라질 수 있습니다."
)
EXECUTION_BLOCKED_TEXT = "계좌 다운로드 파일 입력 후 산출"

MODEL_REQUIRED_COLUMNS = {
    "ticker",
    "name",
    "model_action",
    "model_score_adj",
    "model_score_adj_rank",
    "selection_bucket",
    "kept_from_previous",
    "asof",
    "target_date",
}
FACTOR_REQUIRED_COLUMNS = {
    "ticker",
    "name",
    "score",
    "score_adj",
    "score_rank",
    "score_adj_rank",
}
VALUATION_REQUIRED_COLUMNS = {
    "ticker",
    "name",
    "financial_period",
    "statement_scope",
    "per_ttm",
    "pbr",
    "psr_ttm",
    "ev_to_opincome_ttm",
}
DAILY_REQUIRED_COLUMNS = {
    "date",
    "cum_return",
    "drawdown",
    "benchmark_cum_return",
}
CONTRIBUTION_REQUIRED_COLUMNS = {
    "ticker",
    "name",
    "period_pnl",
    "position_period_return",
    "contribution_to_total_return",
    "weight_at_start_nav",
}
BENCHMARK_REQUIRED_COLUMNS = {
    "date",
    "index_level",
    "benchmark_cum_return",
    "benchmark_name",
    "benchmark_identifier",
    "asset_type",
    "return_type",
}
FORBIDDEN_MODEL_EXECUTION_COLUMNS = {
    "final_action",
    "override_flag",
    "override_reason",
    "position_rule",
    "residual_allocation_amount",
}
FORBIDDEN_PUBLIC_INPUT_KEY_PARTS = (
    "overlay",
    "user_",
    "slippage",
    "market_impact",
)
PUBLIC_OUTPUT_FORBIDDEN_TOKENS = (
    "run_id",
    "sha256",
    "credential",
    "account",
    "user_",
    "overlay",
)
FACTOR_CONTRIBUTION_LABELS = {
    "OpIncome_acc2_log1p__contrib": "영업이익 가속",
    "Revenue_acc2__contrib": "매출 가속",
    "Debt_to_Equity_log__contrib": "재무건전성",
    "op_growth_streak2__contrib": "영업이익 연속성",
    "rev_growth_streak2__contrib": "매출 연속성",
}


def _read_csv(path: str | Path) -> pd.DataFrame:
    frame = pd.read_csv(path, dtype={"ticker": "string"})
    if "ticker" in frame:
        frame["ticker"] = normalize_security_id_series(frame["ticker"])
    return frame


def _read_json(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON input must be an object: {Path(path).name}")
    return payload


def _read_mapping(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if source.suffix.lower() in {".yaml", ".yml"}:
        payload = yaml.safe_load(source.read_text(encoding="utf-8-sig"))
    else:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"configuration input must be an object: {source.name}")
    return payload


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _escape(value: Any) -> str:
    if value is None or value is pd.NA:
        return "NA"
    try:
        if pd.isna(value):
            return "NA"
    except (TypeError, ValueError):
        pass
    return html.escape(str(value), quote=True)


def _pct(value: Any, digits: int = 2) -> str:
    parsed = _number(value)
    return "NA" if parsed is None else f"{parsed * 100:.{digits}f}%"


def _decimal(value: Any, digits: int = 3) -> str:
    parsed = _number(value)
    return "NA" if parsed is None else f"{parsed:.{digits}f}"


def _multiple(value: Any) -> str:
    parsed = _number(value)
    return "NA" if parsed is None else f"{parsed:.2f}"


def _krw(value: Any) -> str:
    parsed = _number(value)
    return "NA" if parsed is None else f"{parsed:,.0f}원"


def _qty(value: Any) -> str:
    parsed = _number(value)
    return "NA" if parsed is None else f"{int(parsed):,}주"


def _boolish(value: Any) -> bool:
    if value is True or value == 1:
        return True
    return isinstance(value, str) and value.strip().lower() in {"true", "yes", "1"}


def _model_selection_label(kept_from_previous: Any) -> str:
    return "기존선정" if _boolish(kept_from_previous) else "신규선정"


def _leading_factor_label(row: pd.Series) -> str:
    available: list[tuple[float, str]] = []
    for column, label in FACTOR_CONTRIBUTION_LABELS.items():
        value = _number(row.get(column))
        if value is not None:
            available.append((value, label))
    if not available:
        raise ValueError(f"top-k factor contributions are missing for {row.get('ticker', 'unknown')}")
    return max(available, key=lambda item: item[0])[1]


def _trade_action_label(current_qty: Any, target_qty: Any) -> str:
    current = _number(current_qty)
    target = _number(target_qty)
    if current is None or target is None:
        return "산출 대기"
    if current == 0 and target > 0:
        return "신규"
    if target > current:
        return "추가"
    if target == 0 and current > 0:
        return "전량"
    if 0 < target < current:
        return "일부"
    if target == current:
        return "유지"
    return "산출 대기"


def _require_columns(frame: pd.DataFrame, required: set[str], label: str) -> None:
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{label} schema missing: {missing}")


def _normalise_tickers(frame: pd.DataFrame, label: str) -> pd.DataFrame:
    result = frame.copy()
    if "ticker" not in result:
        raise ValueError(f"{label} schema missing: ['ticker']")
    result["ticker"] = normalize_security_id_series(result["ticker"])
    if result["ticker"].isna().any() or result["ticker"].duplicated().any():
        raise ValueError(f"{label} tickers must be non-missing and unique")
    return result


def _numeric_series(frame: pd.DataFrame, column: str, label: str) -> pd.Series:
    values = pd.to_numeric(frame[column], errors="coerce")
    if values.isna().any():
        raise ValueError(f"{label}.{column} contains missing/non-numeric values")
    return values.astype(float)


def _close(left: pd.Series, right: pd.Series, tolerance: float = 1e-10) -> bool:
    lhs = pd.to_numeric(left, errors="coerce")
    rhs = pd.to_numeric(right, errors="coerce")
    if len(lhs) != len(rhs) or lhs.isna().any() or rhs.isna().any():
        return False
    return bool((lhs - rhs).abs().le(tolerance + tolerance * rhs.abs()).all())


def _safe_mapping_keys(payload: dict[str, Any], label: str) -> None:
    def walk(value: Any, prefix: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = str(key).lower()
                if any(part in lowered for part in FORBIDDEN_PUBLIC_INPUT_KEY_PARTS):
                    raise ValueError(f"{label} contains forbidden private key: {prefix}{key}")
                walk(child, f"{prefix}{key}.")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{prefix}{index}.")

    walk(payload, "")


def _allocation_contract(config: dict[str, Any]) -> tuple[str, int]:
    _safe_mapping_keys(config, "execution_config")
    raw_policy: Any = config.get("allocation_policy")
    raw_top_k: Any = config.get("top_k", config.get("k"))
    execution = config.get("execution")
    if isinstance(execution, dict):
        raw_policy = raw_policy or execution.get("portfolio_construction") or execution.get("allocation_policy")
        raw_top_k = raw_top_k if raw_top_k is not None else execution.get("top_k", execution.get("k", 10))
    construction = config.get("portfolio_construction")
    if isinstance(construction, dict):
        raw_policy = raw_policy or construction.get("allocation_policy") or construction.get("policy")
        raw_top_k = raw_top_k if raw_top_k is not None else construction.get("top_k", construction.get("k"))
    elif isinstance(construction, str):
        raw_policy = raw_policy or construction
    policy = str(raw_policy or "UNSPECIFIED").strip().upper()
    try:
        top_k = int(raw_top_k)
    except (TypeError, ValueError) as exc:
        raise ValueError("execution_config must declare integer top_k") from exc
    if top_k != 10:
        raise ValueError("public report contract requires top_k=10")
    return policy, top_k


def _false_attestation(value: Any) -> bool:
    if value is False or value == 0:
        return True
    return isinstance(value, str) and value.strip().lower() in {
        "false", "no", "0", "not_loaded", "legacy_user_overlay_not_applied"
    }


def _validate_private_source_attestations(summary: dict[str, Any]) -> None:
    """Allow only negative attestations about private decision-layer loading."""

    def walk(value: Any, prefix: str = "", private_context: bool = False) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                lowered = str(key).lower()
                qualified = f"{prefix}{key}"
                if "slippage" in lowered or "market_impact" in lowered:
                    raise ValueError(f"model_execution_summary contains forbidden cost key: {qualified}")
                if "overlay" in lowered or lowered.startswith("user_"):
                    if isinstance(child, dict):
                        walk(child, f"{qualified}.", True)
                    elif not _false_attestation(child):
                        raise ValueError(f"private decision-layer attestation must be false: {qualified}")
                else:
                    walk(child, f"{qualified}.", private_context)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{prefix}{index}.", private_context)
        elif private_context and not _false_attestation(value):
            raise ValueError(f"private decision-layer attestation must be false: {prefix.rstrip('.')}")

    walk(summary)


def _summary_execution_ready(summary: dict[str, Any]) -> bool:
    status = str(summary.get("execution_plan_status", "")).strip().upper()
    snapshot = str(summary.get("account_snapshot_status", "")).strip().upper()
    return status == "VALID_MODEL_ONLY" and snapshot == "VERIFIED"


def _prepare_model_and_factors(
    model_target: pd.DataFrame,
    topk_factors: pd.DataFrame,
    *,
    allocation_policy: str,
    top_k: int,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    model = _normalise_tickers(model_target, "model_target")
    _require_columns(model, MODEL_REQUIRED_COLUMNS, "model_target")
    if len(model) != top_k:
        raise ValueError(f"model_target must contain exactly {top_k} rows")
    if model["asof"].astype(str).nunique() != 1 or model["target_date"].astype(str).nunique() != 1:
        raise ValueError("model_target must contain one ASOF and one target_date")

    factors = topk_factors.copy()
    if "selected_topk" in factors:
        selected = factors["selected_topk"].astype(str).str.lower().isin({"1", "true", "yes"})
        factors = factors.loc[selected].copy()
    factors = _normalise_tickers(factors, "topk_factors")
    _require_columns(factors, FACTOR_REQUIRED_COLUMNS, "topk_factors")
    if len(factors) != top_k or set(factors["ticker"]) != set(model["ticker"]):
        raise ValueError("topk_factors must contain exactly the model target ticker set")

    aligned = model[["ticker", "model_score_adj", "model_score_adj_rank"]].merge(
        factors[["ticker", "score_adj", "score_adj_rank"]],
        on="ticker",
        how="left",
        validate="one_to_one",
    )
    if not _close(aligned["model_score_adj"], aligned["score_adj"]):
        raise ValueError("model_target adjusted scores do not match source top-k factors")
    if not _close(aligned["model_score_adj_rank"], aligned["score_adj_rank"]):
        raise ValueError("model_target adjusted ranks do not match source top-k factors")

    model = model.sort_values(["model_score_adj_rank", "ticker"], kind="stable").reset_index(drop=True)
    model["public_target_weight"] = 1.0 / top_k if allocation_policy == "EQUAL_WEIGHT_TOP_K" else pd.NA
    factors = model[["ticker"]].merge(factors, on="ticker", how="left", validate="one_to_one")
    return model, factors


def _prepare_valuation(valuation: pd.DataFrame, model_tickers: set[str]) -> pd.DataFrame:
    frame = _normalise_tickers(valuation, "valuation")
    _require_columns(frame, VALUATION_REQUIRED_COLUMNS, "valuation")
    frame = frame.loc[frame["ticker"].isin(model_tickers)].copy()
    if len(frame) != len(model_tickers) or set(frame["ticker"]) != model_tickers:
        raise ValueError("valuation must cover every model target ticker exactly once")
    return frame


def _prepare_performance(
    summary: dict[str, Any],
    daily: pd.DataFrame,
    contribution: pd.DataFrame,
    benchmark_qa: pd.DataFrame,
) -> tuple[dict[str, Any], pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    required_summary = {
        "start_date",
        "end_date",
        "cum_return",
        "max_drawdown",
        "performance_interval_activity_proven",
        "benchmark_name",
        "benchmark_asset_type",
        "benchmark_return_type",
    }
    missing = sorted(required_summary - set(summary))
    if missing:
        raise ValueError(f"performance_summary schema missing: {missing}")

    public_summary = {
        "start_date": str(summary["start_date"]),
        "end_date": str(summary["end_date"]),
        "cum_return": _number(summary["cum_return"]),
        "max_drawdown": _number(summary["max_drawdown"]),
        "drawdown_peak_date": summary.get("drawdown_peak_date"),
        "max_drawdown_date": summary.get("max_drawdown_date"),
        "benchmark_cum_return": _number(summary.get("benchmark_cum_return")),
        "active_return": _number(summary.get("active_return")),
        "performance_interval_activity_proven": summary["performance_interval_activity_proven"] is True,
        "benchmark_name": str(summary["benchmark_name"]),
        "benchmark_identifier": str(summary.get("benchmark_identifier", "")),
        "benchmark_asset_type": str(summary["benchmark_asset_type"]),
        "benchmark_return_type": str(summary["benchmark_return_type"]),
        "dividend_treatment": str(summary.get("nav_dividend_treatment", "NA")),
    }
    if any(public_summary[key] is None for key in ("cum_return", "max_drawdown")):
        raise ValueError("performance_summary returns must be numeric")

    daily_frame = daily.copy()
    _require_columns(daily_frame, DAILY_REQUIRED_COLUMNS, "performance_daily")
    daily_frame["date"] = pd.to_datetime(daily_frame["date"], errors="coerce").dt.normalize()
    if daily_frame["date"].isna().any() or daily_frame["date"].duplicated().any():
        raise ValueError("performance_daily dates must be valid and unique")
    daily_frame = daily_frame.sort_values("date", kind="stable").reset_index(drop=True)
    for column in ("cum_return", "drawdown", "benchmark_cum_return"):
        daily_frame[column] = _numeric_series(daily_frame, column, "performance_daily")
    if daily_frame.empty:
        raise ValueError("performance_daily cannot be empty")
    if daily_frame.iloc[0]["date"].strftime("%Y-%m-%d") != public_summary["start_date"]:
        raise ValueError("performance start date mismatch")
    if daily_frame.iloc[-1]["date"].strftime("%Y-%m-%d") != public_summary["end_date"]:
        raise ValueError("performance end date mismatch")
    if abs(float(daily_frame.iloc[0]["cum_return"])) > 1e-12:
        raise ValueError("portfolio cumulative return must start at zero")
    if abs(float(daily_frame.iloc[0]["benchmark_cum_return"])) > 1e-12:
        raise ValueError("benchmark cumulative return must start at zero")
    if abs(float(daily_frame.iloc[-1]["cum_return"]) - float(public_summary["cum_return"])) > 1e-9:
        raise ValueError("performance summary return does not match daily series")
    if abs(float(daily_frame["drawdown"].min()) - float(public_summary["max_drawdown"])) > 1e-9:
        raise ValueError("performance summary MDD does not match daily series")

    contrib = _normalise_tickers(contribution, "performance_contribution")
    _require_columns(contrib, CONTRIBUTION_REQUIRED_COLUMNS, "performance_contribution")
    for column in (
        "period_pnl", "position_period_return", "contribution_to_total_return", "weight_at_start_nav"
    ):
        contrib[column] = _numeric_series(contrib, column, "performance_contribution")
    if contrib.empty:
        raise ValueError("performance_contribution cannot be empty")
    if abs(float(contrib["contribution_to_total_return"].sum()) - float(public_summary["cum_return"])) > 1e-8:
        raise ValueError("contribution total does not reconcile to portfolio return")

    benchmark = benchmark_qa.copy()
    _require_columns(benchmark, BENCHMARK_REQUIRED_COLUMNS, "benchmark_qa")
    benchmark["date"] = pd.to_datetime(benchmark["date"], errors="coerce").dt.normalize()
    if benchmark["date"].isna().any() or benchmark["date"].duplicated().any():
        raise ValueError("benchmark_qa dates must be valid and unique")
    benchmark = benchmark.sort_values("date", kind="stable").reset_index(drop=True)
    benchmark["index_level"] = _numeric_series(benchmark, "index_level", "benchmark_qa")
    benchmark["benchmark_cum_return"] = _numeric_series(
        benchmark, "benchmark_cum_return", "benchmark_qa"
    )
    if not benchmark["date"].equals(daily_frame["date"]):
        raise ValueError("portfolio and benchmark dates must align exactly")
    if not _close(benchmark["benchmark_cum_return"], daily_frame["benchmark_cum_return"]):
        raise ValueError("daily benchmark returns do not match benchmark QA")
    if set(benchmark["benchmark_name"].astype(str)) != {"KRX 300"}:
        raise ValueError("public benchmark must be KRX 300")
    if set(benchmark["asset_type"].astype(str)) != {"INDEX"}:
        raise ValueError("public benchmark asset type must be INDEX")
    if set(benchmark["return_type"].astype(str)) != {"PRICE"}:
        raise ValueError("public benchmark return type must be PRICE")
    if public_summary["benchmark_name"] != "KRX 300":
        raise ValueError("performance summary benchmark must be KRX 300")
    if public_summary["benchmark_asset_type"] != "INDEX":
        raise ValueError("performance summary benchmark asset type must be INDEX")
    if public_summary["benchmark_return_type"] != "PRICE":
        raise ValueError("performance summary benchmark return type must be PRICE")
    benchmark_last = float(benchmark.iloc[-1]["benchmark_cum_return"])
    if public_summary["benchmark_cum_return"] is None:
        public_summary["benchmark_cum_return"] = benchmark_last
    if abs(float(public_summary["benchmark_cum_return"]) - benchmark_last) > 1e-9:
        raise ValueError("benchmark cumulative return mismatch")
    active = float(public_summary["cum_return"]) - benchmark_last
    if public_summary["active_return"] is None:
        public_summary["active_return"] = active
    if abs(float(public_summary["active_return"]) - active) > 1e-9:
        raise ValueError("active return mismatch")
    return public_summary, daily_frame, contrib, benchmark


def _execution_contract(
    plan: pd.DataFrame,
    summary: dict[str, Any],
    config: dict[str, Any],
    model: pd.DataFrame,
    allocation_policy: str,
    execution_ready: bool,
) -> pd.DataFrame:
    _validate_private_source_attestations(summary)
    contract = summary.get("schema_version", summary.get("plan_contract", summary.get("contract")))
    accepted_contracts = {
        MODEL_EXECUTION_CONTRACT,
        MODEL_EXECUTION_SCHEMA_VERSION.upper(),
        MODEL_EXECUTION_READY_SCHEMA_VERSION.upper(),
    }
    if str(contract or "").strip().upper() not in accepted_contracts:
        raise ValueError(
            f"model_execution_summary contract must be {MODEL_EXECUTION_SCHEMA_VERSION}"
        )
    if summary.get("actual_orders_submitted") not in (None, False):
        raise ValueError("public report cannot describe submitted orders")
    forbidden = sorted(set(plan.columns) & FORBIDDEN_MODEL_EXECUTION_COLUMNS)
    dynamic_forbidden = sorted(
        column
        for column in plan.columns
        if any(part in column.lower() for part in FORBIDDEN_PUBLIC_INPUT_KEY_PARTS)
    )
    if forbidden or dynamic_forbidden:
        raise ValueError(f"private/final execution schema is forbidden: {sorted(set(forbidden + dynamic_forbidden))}")
    frame = _normalise_tickers(plan, "model_execution_plan")
    if "reference_price" not in frame and "price" in frame:
        frame["reference_price"] = frame["price"]
    if "target_weight" not in frame and "model_target_weight" in frame:
        frame["target_weight"] = frame["model_target_weight"]
    if "plan_contract" in frame:
        row_contracts = set(frame["plan_contract"].dropna().astype(str).str.upper())
        if row_contracts and not row_contracts <= accepted_contracts:
            raise ValueError("model_execution_plan rows must use the model-only contract")
    if not execution_ready:
        _require_columns(frame, {"ticker", "name", "target_weight"}, "model_execution_plan")
        if len(frame) != len(model) or set(frame["ticker"]) != set(model["ticker"]):
            raise ValueError("blocked model_execution_plan must contain the ten model target rows")
        frame["target_weight"] = _numeric_series(frame, "target_weight", "model_execution_plan")
        if allocation_policy == "EQUAL_WEIGHT_TOP_K":
            if not frame["target_weight"].sub(0.1).abs().le(1e-12).all():
                raise ValueError("blocked equal-weight rows must each target 10%")
        elif frame["target_weight"].sub(0.1).abs().le(1e-12).all():
            raise ValueError("10% equal weights require EQUAL_WEIGHT_TOP_K execution config")
        execution_numeric_columns = {
            "reference_price", "price", "planning_price", "current_qty", "target_qty",
            "delta_qty", "trade_qty", "current_value", "target_value", "order_value",
            "realized_target_weight", "estimated_commission", "securities_transaction_tax",
            "rural_special_tax", "estimated_sell_tax", "estimated_total_cost_row",
            "estimated_total_cost", "final_cash", "execution_planning_nav",
        }
        populated = sorted(
            column
            for column in execution_numeric_columns & set(frame.columns)
            if pd.to_numeric(frame[column], errors="coerce").notna().any()
        )
        if populated:
            raise ValueError(
                f"blocked model_execution_plan must leave execution numerics blank: {populated}"
            )
        return frame.sort_values("ticker", kind="stable").reset_index(drop=True)
    required = {
        "ticker",
        "name",
        "target_weight",
        "reference_price",
        "current_qty",
        "target_qty",
        "trade_side",
        "trade_qty",
        "order_value",
        "estimated_commission",
        "estimated_sell_tax",
    }
    _require_columns(frame, required, "model_execution_plan")
    for column in (
        "target_weight",
        "reference_price",
        "current_qty",
        "target_qty",
        "trade_qty",
        "order_value",
        "estimated_commission",
        "estimated_sell_tax",
    ):
        frame[column] = _numeric_series(frame, column, "model_execution_plan")
    if (frame[["target_weight", "reference_price", "current_qty", "target_qty", "trade_qty", "order_value", "estimated_commission", "estimated_sell_tax"]] < 0).any().any():
        raise ValueError("model_execution_plan quantities, prices, weights and costs must be nonnegative")
    allowed_sides = {"BUY", "SELL", "NONE"}
    if not set(frame["trade_side"].astype(str).str.upper()) <= allowed_sides:
        raise ValueError("model_execution_plan trade_side must be BUY, SELL or NONE")

    selected = frame.loc[frame["ticker"].isin(set(model["ticker"]))].copy()
    if execution_ready:
        if frame.empty:
            raise ValueError("model_execution_plan cannot be empty when explicit holdings are certified")
        if set(selected["ticker"]) != set(model["ticker"]):
            raise ValueError("model_execution_plan must cover all model target tickers")
        if allocation_policy == "EQUAL_WEIGHT_TOP_K":
            if not selected["target_weight"].sub(0.1).abs().le(1e-12).all():
                raise ValueError("equal-weight model execution rows must each target 10%")
        elif selected["target_weight"].sub(0.1).abs().le(1e-12).all():
            raise ValueError("10% equal weights require EQUAL_WEIGHT_TOP_K execution config")
    frame["estimated_public_cost"] = frame["estimated_commission"] + frame["estimated_sell_tax"]
    return frame.sort_values(["trade_side", "ticker"], kind="stable").reset_index(drop=True)


def _table(
    caption: str,
    headers: Sequence[str],
    rows: Iterable[Sequence[str]],
    *,
    table_id: str,
    row_attribute: str | None = None,
) -> str:
    if len(headers) > 8:
        raise ValueError("public tables may contain at most eight columns")
    header_html = "".join(f'<th scope="col">{_escape(label)}</th>' for label in headers)
    body: list[str] = []
    for row in rows:
        values = list(row)
        if len(values) != len(headers):
            raise ValueError(f"table {table_id} row width mismatch")
        attr = f" {row_attribute}" if row_attribute else ""
        cells = "".join(
            f'<td data-label="{html.escape(str(label), quote=True)}">{value}</td>'
            for label, value in zip(headers, values)
        )
        body.append(f"<tr{attr}>" + cells + "</tr>")
    return (
        f'<table id="{html.escape(table_id, quote=True)}">'
        f"<caption>{html.escape(caption)}</caption>"
        f"<thead><tr>{header_html}</tr></thead><tbody>{''.join(body)}</tbody></table>"
    )


def _line_path(values: pd.Series, x_values: list[float], y_map: Any) -> str:
    return " ".join(
        ("M" if index == 0 else "L") + f"{x_values[index]:.2f},{y_map(float(value)):.2f}"
        for index, value in enumerate(values)
    )


def _performance_chart(daily: pd.DataFrame, summary: dict[str, Any]) -> str:
    width, height = 820.0, 300.0
    left, right, top, bottom = 58.0, 22.0, 24.0, 44.0
    plot_width, plot_height = width - left - right, height - top - bottom
    combined = pd.concat([daily["cum_return"], daily["benchmark_cum_return"], pd.Series([0.0])])
    low, high = float(combined.min()), float(combined.max())
    padding = max((high - low) * 0.12, 0.02)
    low, high = low - padding, high + padding
    span = high - low
    y_map = lambda value: top + (high - value) / span * plot_height
    if len(daily) == 1:
        x_values = [left + plot_width / 2]
    else:
        x_values = [left + plot_width * index / (len(daily) - 1) for index in range(len(daily))]
    portfolio_path = _line_path(daily["cum_return"], x_values, y_map)
    benchmark_path = _line_path(daily["benchmark_cum_return"], x_values, y_map)
    zero_y = y_map(0.0)

    def marker(date_value: Any, css_class: str, label: str, series: str = "cum_return") -> str:
        parsed = pd.to_datetime(date_value, errors="coerce")
        if pd.isna(parsed):
            return ""
        matches = daily.index[daily["date"].eq(parsed.normalize())]
        if not len(matches):
            return ""
        index = int(matches[0])
        x, y = x_values[index], y_map(float(daily.iloc[index][series]))
        anchor = "end" if x > width * 0.7 else "start"
        dx = -7 if anchor == "end" else 7
        return (
            f'<g class="chart-marker {css_class}"><circle cx="{x:.2f}" cy="{y:.2f}" r="4"/>'
            f'<text x="{x + dx:.2f}" y="{max(14.0, y - 8):.2f}" text-anchor="{anchor}">{_escape(label)}</text></g>'
        )

    start_date = daily.iloc[0]["date"].strftime("%Y-%m-%d")
    end_date = daily.iloc[-1]["date"].strftime("%Y-%m-%d")
    performance_label = (
        "확정" if summary.get("performance_interval_activity_proven") is True else "잠정"
    )
    markers = "".join(
        [
            marker(start_date, "start-marker", "시작"),
            marker(end_date, "end-marker", "종료"),
            marker(summary.get("drawdown_peak_date"), "peak-marker", "고점"),
            marker(summary.get("max_drawdown_date"), "mdd-marker", "최대 낙폭"),
        ]
    )
    return f"""
<figure class="chart-card">
  <figcaption>포트폴리오와 KRX 300 누적수익률</figcaption>
  <svg id="performance-line-chart" class="line-chart" viewBox="0 0 820 300" role="img" aria-label="포트폴리오와 KRX 300 {performance_label} 누적수익률 선 차트">
    <line class="chart-axis x-axis" x1="{left}" y1="{height-bottom}" x2="{width-right}" y2="{height-bottom}"/>
    <line class="chart-axis y-axis" x1="{left}" y1="{top}" x2="{left}" y2="{height-bottom}"/>
    <line class="zero-line" x1="{left}" y1="{zero_y:.2f}" x2="{width-right}" y2="{zero_y:.2f}"/>
    <text class="axis-label" x="{left}" y="{height-13}">{start_date}</text>
    <text class="axis-label" x="{width-right}" y="{height-13}" text-anchor="end">{end_date}</text>
    <text class="axis-label" x="{left-8}" y="{y_map(high):.2f}" text-anchor="end">{high*100:.1f}%</text>
    <text class="axis-label" x="{left-8}" y="{zero_y+4:.2f}" text-anchor="end">0%</text>
    <text class="axis-label" x="{left-8}" y="{y_map(low):.2f}" text-anchor="end">{low*100:.1f}%</text>
    <path class="portfolio-line" d="{portfolio_path}" fill="none" stroke="#2457d6" stroke-width="3" vector-effect="non-scaling-stroke"/>
    <path class="benchmark-line" d="{benchmark_path}" fill="none" stroke="#ef7b45" stroke-width="2.5" vector-effect="non-scaling-stroke"/>
    {markers}
  </svg>
  <div class="legend"><span><i class="portfolio-key"></i>포트폴리오</span><span><i class="benchmark-key"></i>KRX 300</span></div>
</figure>"""


def _contribution_chart(contribution: pd.DataFrame) -> str:
    rows = contribution.sort_values("contribution_to_total_return", ascending=False, kind="stable")
    width, row_height, left, right = 820.0, 31.0, 125.0, 82.0
    height = 42.0 + row_height * len(rows)
    plot_width = width - left - right
    max_abs = max(float(rows["contribution_to_total_return"].abs().max()), 1e-12)
    zero_x = left + plot_width / 2
    elements = [
        f'<line class="contribution-zero" x1="{zero_x:.2f}" y1="15" x2="{zero_x:.2f}" y2="{height-14:.2f}"/>'
    ]
    for order, (_, row) in enumerate(rows.iterrows()):
        value = float(row["contribution_to_total_return"])
        bar_width = abs(value) / max_abs * (plot_width / 2 - 12)
        x = zero_x if value >= 0 else zero_x - bar_width
        y = 23 + order * row_height
        css_class = "positive-bar" if value >= 0 else "negative-bar"
        elements.append(
            f'<text class="bar-label" x="{left-8}" y="{y+14}" text-anchor="end">{_escape(row["ticker"])}</text>'
            f'<rect class="contribution-bar {css_class}" x="{x:.2f}" y="{y:.2f}" width="{bar_width:.2f}" height="18">'
            f'<title>{_escape(row["name"])}: {_pct(value)}</title></rect>'
            f'<text class="bar-value" x="{width-8:.2f}" y="{y+14}" text-anchor="end">{_pct(value)}</text>'
        )
    return f"""
<figure class="chart-card">
  <figcaption>종목별 총수익 기여도</figcaption>
  <svg id="contribution-bar-chart" class="bar-chart" viewBox="0 0 820 {height:.0f}" role="img" aria-label="종목별 잠정 총수익 기여도 막대 차트">
    {''.join(elements)}
  </svg>
</figure>"""


def _assert_public_output(html_text: str) -> None:
    encoded_size = len(html_text.encode("utf-8"))
    if encoded_size >= MAX_HTML_BYTES:
        raise ValueError(f"public report must be smaller than {MAX_HTML_BYTES} bytes")
    lowered = html_text.lower()
    for token in PUBLIC_OUTPUT_FORBIDDEN_TOKENS:
        if token in lowered:
            raise ValueError(f"public report contains forbidden token: {token}")
    if "<script" in lowered or "application/json" in lowered:
        raise ValueError("public report must not embed scripts or machine JSON")
    if re.search(r"(?i)(?:[a-z]:[\\/]|file://|https?://)", html_text):
        raise ValueError("public report contains a path or external URL")
    if re.search(r"(?i)\b[0-9a-f]{64}\b", html_text):
        raise ValueError("public report contains a fingerprint/hash")
    if re.search(
        r"\b(?:READY|BLOCKED|CERTIFIED|PROVISIONAL|BUY|SELL|HOLD)\b",
        html_text,
        re.IGNORECASE,
    ):
        raise ValueError("public report contains a raw technical status/action code")
    if html_text.count("<h2>") != len(PUBLIC_SECTIONS):
        raise ValueError("public report must contain exactly five h2 sections")


def build_public_report(
    model_target: pd.DataFrame,
    topk_factors: pd.DataFrame,
    valuation: pd.DataFrame,
    performance_summary: dict[str, Any],
    performance_daily: pd.DataFrame,
    performance_contribution: pd.DataFrame,
    benchmark_qa: pd.DataFrame,
    model_execution_plan: pd.DataFrame,
    model_execution_summary: dict[str, Any],
    execution_config: dict[str, Any],
    *,
    broker_account_file: str | Path | None = None,
) -> str:
    """Build a self-contained, public allowlist-only report.

    ``broker_account_file`` is an optional explicit presence gate for direct
    calls.  The CLI normally consumes the sealed model-only summary status.
    File contents and paths are never read, selected automatically, or rendered.
    """

    allocation_policy, top_k = _allocation_contract(execution_config)
    model, factors = _prepare_model_and_factors(
        model_target,
        topk_factors,
        allocation_policy=allocation_policy,
        top_k=top_k,
    )
    valuation_public = _prepare_valuation(valuation, set(model["ticker"]))
    summary, daily, contribution, benchmark = _prepare_performance(
        performance_summary,
        performance_daily,
        performance_contribution,
        benchmark_qa,
    )

    explicit_broker_file = broker_account_file is not None
    if explicit_broker_file and not Path(broker_account_file).is_file():
        raise FileNotFoundError("explicit broker download file not found")
    summary_execution_ready = _summary_execution_ready(model_execution_summary)
    execution_ready = summary_execution_ready
    execution = _execution_contract(
        model_execution_plan,
        model_execution_summary,
        execution_config,
        model,
        allocation_policy,
        execution_ready,
    )

    asof = str(model["asof"].iloc[0])
    target_date = str(model["target_date"].iloc[0])
    provisional = not summary["performance_interval_activity_proven"]
    performance_status = "tentative" if provisional else "verified"
    performance_label = "잠정 성과" if provisional else "확정 성과"
    execution_status = "available" if execution_ready else "unavailable"

    factor_lookup = factors.set_index("ticker")
    valuation_lookup = valuation_public.set_index("ticker")
    model_rows = []
    for _, model_row in model.iterrows():
        factor_row = factor_lookup.loc[model_row["ticker"]]
        valuation_row = valuation_lookup.loc[model_row["ticker"]]
        model_rows.append(
            [
                f"{_escape(model_row['name'])}<small>{_escape(model_row['ticker'])}</small>",
                _model_selection_label(model_row["kept_from_previous"]),
                _escape(int(float(model_row["model_score_adj_rank"]))),
                _pct(model_row["public_target_weight"]),
                _escape(_leading_factor_label(factor_row)),
                _multiple(valuation_row["per_ttm"]),
                _multiple(valuation_row["pbr"]),
                _multiple(valuation_row["psr_ttm"]),
            ]
        )
    model_table = _table(
        "모델 목표 포트폴리오 10종목",
        ("종목", "모델 구분", "모델 순위", "목표비중", "핵심 팩터", "PER(TTM)", "PBR", "PSR(TTM)"),
        model_rows,
        table_id="model-target-table",
        row_attribute='data-public-model-row="true"',
    )

    contribution_rows = [
        [
            f"{_escape(row['name'])}<small>{_escape(row['ticker'])}</small>",
            _pct(row["weight_at_start_nav"]),
            _pct(row["position_period_return"]),
            _krw(row["period_pnl"]),
            _pct(row["contribution_to_total_return"]),
            "목표 포함" if row["ticker"] in set(model["ticker"]) else "목표 제외",
        ]
        for _, row in contribution.sort_values("contribution_to_total_return", ascending=False).iterrows()
    ]
    contribution_table = _table(
        f"{performance_label} 종목별 손익과 기여도",
        ("종목", "시작비중", "종목수익률", "손익", "수익기여도", "모델의 다음 판단"),
        contribution_rows,
        table_id="contribution-table",
    )

    policy_label = "상위 10종목 동일비중" if allocation_policy == "EQUAL_WEIGHT_TOP_K" else "설정값 사용"
    account_date = model_execution_summary.get("account_asof") or "미입력"
    price_date = model_execution_summary.get("price_date") or "미입력"
    execution_target_date = model_execution_summary.get("target_date") or target_date
    nav_value = model_execution_summary.get("execution_planning_nav")
    final_cash_value = model_execution_summary.get("final_cash")
    selected_status = {
        row["ticker"]: _model_selection_label(row["kept_from_previous"])
        for _, row in model.iterrows()
    }
    execution_markup: str
    if execution_ready:
        execution_rows = [
            [
                f"{_escape(row['name'])}<small>{_escape(row['ticker'])}</small>",
                selected_status.get(row["ticker"], "목표 제외"),
                _qty(row["current_qty"]),
                _qty(row["target_qty"]),
                _trade_action_label(row["current_qty"], row["target_qty"]),
                _qty(row["trade_qty"]),
                _krw(row["order_value"]),
                _krw(row["estimated_public_cost"]),
            ]
            for _, row in execution.iterrows()
        ]
        execution_table = _table(
            "모델 기준 매매계획",
            ("종목", "모델 상태", "현재수량", "목표수량", "매매구분", "매매수량", "예상 주문금액", "예상 비용"),
            execution_rows,
            table_id="model-execution-table",
            row_attribute='data-public-execution-row="true"',
        )
        commission = float(execution["estimated_commission"].sum())
        sell_tax = float(execution["estimated_sell_tax"].sum())
        execution_facts = (
            ("모델", policy_label), ("계좌일", _escape(account_date)),
            ("가격일", _escape(price_date)), ("목표일", _escape(execution_target_date)),
            ("계획 기준자산", _krw(nav_value)), ("예상 매매수수료", _krw(commission)),
            ("예상 법정 매도세금", _krw(sell_tax)), ("예상 잔여현금", _krw(final_cash_value)),
            ("주문 제출", "미제출"),
        )
    else:
        execution_rows = [
            [
                f"{_escape(row['name'])}<small>{_escape(row['ticker'])}</small>",
                _model_selection_label(row["kept_from_previous"]),
                "미입력",
                EXECUTION_BLOCKED_TEXT,
                "산출 대기",
                EXECUTION_BLOCKED_TEXT,
                EXECUTION_BLOCKED_TEXT,
                EXECUTION_BLOCKED_TEXT,
            ]
            for _, row in model.iterrows()
        ]
        execution_table = _table(
            "모델 기준 매매계획",
            ("종목", "모델 상태", "현재수량", "목표수량", "매매구분", "매매수량", "예상 주문금액", "예상 비용"),
            execution_rows,
            table_id="model-execution-table",
            row_attribute='data-public-execution-row="true"',
        )
        execution_facts = (
            ("모델", policy_label), ("계좌일", _escape(account_date)),
            ("가격일", _escape(price_date)), ("목표일", _escape(execution_target_date)),
            ("계획 기준자산", EXECUTION_BLOCKED_TEXT), ("예상 매매수수료", EXECUTION_BLOCKED_TEXT),
            ("예상 법정 매도세금", EXECUTION_BLOCKED_TEXT), ("예상 잔여현금", EXECUTION_BLOCKED_TEXT),
            ("주문 제출", "미제출"),
        )
    execution_facts_markup = "".join(
        f"<div><dt>{_escape(label)}</dt><dd>{value}</dd></div>" for label, value in execution_facts
    )
    execution_markup = f"""
<div class="execution-heading"><span class="badge {execution_status}">{'산출 가능' if execution_ready else EXECUTION_BLOCKED_TEXT}</span></div>
<dl class="facts execution-facts">{execution_facts_markup}</dl>
{execution_table}"""

    benchmark_start = benchmark.iloc[0]
    benchmark_end = benchmark.iloc[-1]
    css = """
:root{--ink:#172033;--muted:#5f6b7d;--line:#dce3ed;--blue:#2457d6;--orange:#ef7b45;--green:#16845b;--red:#c74343;--paper:#fff;--wash:#f4f7fb}
*{box-sizing:border-box}html{background:#eef2f7;color:var(--ink);font-family:"Malgun Gothic","Apple SD Gothic Neo",sans-serif;font-size:15px}
body{margin:0}.report{width:min(1080px,100%);margin:0 auto;background:var(--paper);padding:34px 42px 52px}
h1{font-size:2rem;line-height:1.22;margin:0 0 8px}h2{font-size:1.42rem;margin:0 0 16px;padding-bottom:8px;border-bottom:2px solid var(--ink)}h3{font-size:1.08rem;margin:26px 0 10px}
p{margin:8px 0 12px}.subtitle,.note{color:var(--muted)}.report-section{margin-top:38px;break-inside:avoid-page}.badge{display:inline-block;border-radius:999px;padding:4px 10px;font-size:.78rem;font-weight:700}.badge.tentative{background:#fff1d9;color:#8a5600}.badge.verified{background:#dbf5e9;color:#0d6a48}.badge.unavailable{background:#fde6e6;color:#992f2f}.badge.available{background:#dbf5e9;color:#0d6a48}
.metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:16px 0}.metric-grid.compact{grid-template-columns:repeat(3,minmax(0,1fr))}.metric{background:var(--wash);border:1px solid var(--line);border-radius:10px;padding:12px}.metric span{display:block;color:var(--muted);font-size:.78rem;margin-bottom:4px}.metric strong{font-size:1.05rem}
.notice{border-left:4px solid #d69d27;background:#fff7e8;padding:11px 13px;margin:14px 0}
table{border-collapse:collapse;width:100%;max-width:100%;table-layout:fixed;margin:12px 0 24px;font-size:.79rem}caption{text-align:left;font-weight:700;font-size:.94rem;padding:7px 0}th,td{border-bottom:1px solid var(--line);padding:7px 5px;text-align:right;vertical-align:middle;overflow-wrap:anywhere;word-break:keep-all}th{background:#f0f4f9;color:#354158}th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}td small{display:block;color:var(--muted);font-size:.72rem;margin-top:2px}
.chart-card{margin:16px 0 22px;border:1px solid var(--line);border-radius:12px;padding:12px;background:#fff;break-inside:avoid-page}.chart-card figcaption{font-weight:700;margin-bottom:8px}.line-chart,.bar-chart{display:block;width:100%;height:auto}.chart-axis{stroke:#8390a3;stroke-width:1}.zero-line,.contribution-zero{stroke:#9aa5b5;stroke-width:1;stroke-dasharray:5 4}.axis-label,.bar-label,.bar-value,.chart-marker text{font-size:11px;fill:#596579}.chart-marker circle{fill:#fff;stroke:#172033;stroke-width:2}.portfolio-key,.benchmark-key{display:inline-block;width:18px;height:3px;margin-right:6px;vertical-align:middle}.portfolio-key{background:var(--blue)}.benchmark-key{background:var(--orange)}.legend{display:flex;gap:18px;justify-content:center;color:var(--muted);font-size:.82rem}.positive-bar{fill:var(--green)}.negative-bar{fill:var(--red)}
.facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px 24px;margin:12px 0}.facts dt{font-size:.78rem;color:var(--muted)}.facts dd{margin:2px 0 8px;font-weight:700}.fineprint{font-size:.8rem;color:var(--muted)}.public-notes{margin:0;padding-left:1.3rem}.public-notes li{margin:0 0 10px;line-height:1.55}
@media(max-width:720px){html{font-size:14px}.report{padding:22px 14px 38px;overflow:hidden}h1{font-size:1.42rem;line-height:1.28;max-width:100%;white-space:normal;overflow-wrap:anywhere;word-break:keep-all}.metric-grid,.metric-grid.compact{grid-template-columns:repeat(2,minmax(0,1fr))}.facts{grid-template-columns:1fr}.chart-card{padding:8px 4px}.axis-label,.bar-label,.bar-value,.chart-marker text{font-size:16px}table{display:block;width:100%;font-size:.78rem}caption{display:block;width:100%;max-width:100%;box-sizing:border-box;white-space:normal;word-break:keep-all;padding:7px 0 10px}thead{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}tbody,tr,td{display:block;width:100%}tbody tr{box-sizing:border-box;border:1px solid var(--line);border-radius:9px;margin:0 0 10px;padding:5px 8px;background:#fff;break-inside:avoid}tbody td{box-sizing:border-box;display:grid;grid-template-columns:minmax(92px,38%) minmax(0,1fr);gap:8px;text-align:left!important;border-bottom:1px dotted var(--line);padding:6px 2px;overflow-wrap:anywhere;word-break:break-word}tbody td:last-child{border-bottom:0}tbody td::before{content:attr(data-label);font-weight:700;color:#4e5b70}.report-section{margin-top:28px}}
@media print{@page{size:A4;margin:12mm}html{background:#fff;font-size:10.5pt}.report{width:100%;padding:0}.report-section{break-inside:auto}.model,.execution,.methodology{break-before:page;padding-top:1px}h2{white-space:nowrap;break-inside:avoid;break-after:avoid-page}caption{break-after:avoid-page}table,figure,.metric-grid{break-inside:avoid}thead{display:table-header-group}a{color:inherit;text-decoration:none}.chart-card{box-shadow:none}}
"""
    report = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Investment Adviser 리밸런싱 공개 보고서</title><style>{css}</style></head>
<body><main class="report" data-report-contract="{PUBLIC_REPORT_CONTRACT}" data-allocation-policy="{_escape(allocation_policy)}">
<header><h1>AI Investment Adviser 리밸런싱 공개 보고서</h1>
<p class="subtitle">기준일 {_escape(asof)} · 목표일 {_escape(target_date)} · 모델 선정 10종목</p></header>

<section class="report-section overview" data-execution-status="{execution_status}">
<h2>{PUBLIC_SECTIONS[0]}</h2>
<div class="metric-grid">
  <div class="metric"><span>운용기간</span><strong>{_escape(summary['start_date'])} ~ {_escape(summary['end_date'])}</strong></div>
  <div class="metric"><span>포트폴리오 수익률</span><strong>{_pct(summary['cum_return'])}</strong></div>
  <div class="metric"><span>KRX 300</span><strong>{_pct(summary['benchmark_cum_return'])}</strong></div>
  <div class="metric"><span>초과수익률</span><strong>{_pct(summary['active_return'])}</strong></div>
  <div class="metric"><span>최대 낙폭</span><strong>{_pct(summary['max_drawdown'])}</strong></div>
  <div class="metric"><span>모델 실행계획</span><strong>{'산출 가능' if execution_ready else EXECUTION_BLOCKED_TEXT}</strong></div>
</div>
<p>성과는 <strong>{performance_label}</strong>이며, 다음 포트폴리오는 모델의 원본 순위와 선정 결과를 그대로 요약했습니다.</p>
</section>

<section class="report-section performance" data-performance-status="{performance_status}">
<h2>{PUBLIC_SECTIONS[1]}</h2>
<p><span class="badge {performance_status}">{performance_label}</span></p>
{('<div class="notice"><strong>잠정 성과</strong> — 시작 보유내역은 확인됐지만 운용기간 전체의 입출금·중간매매 내역은 확인되지 않았습니다. 아래 수치는 확인된 보유내역을 고정한 조건부 계산입니다.</div>' if provisional else '')}
{_performance_chart(daily, summary)}
{_contribution_chart(contribution)}
{contribution_table}
</section>

<section class="report-section model">
<h2>{PUBLIC_SECTIONS[2]}</h2>
{model_table}
</section>

<section class="report-section execution" data-execution-status="{execution_status}">
<h2>{PUBLIC_SECTIONS[3]}</h2>
{execution_markup}
<p class="fineprint">{COST_DISCLOSURE}</p>
<p class="fineprint">실제 주문은 제출하지 않았습니다.</p>
</section>

<section class="report-section methodology">
<h2>{PUBLIC_SECTIONS[4]}</h2>
<ul class="public-notes">
  <li data-public-note="true">계좌 기준일은 {_escape(account_date)}입니다. 파일이 명시적으로 제공되지 않으면 수량 기반 계획을 산출하지 않습니다.</li>
  <li data-public-note="true">가격 기준일은 {_escape(price_date)}이며 목표 리밸런싱일은 {_escape(execution_target_date)}입니다.</li>
  <li data-public-note="true">벤치마크는 KRX 300 가격지수(PRICE)이며 {benchmark_start['date'].strftime('%Y-%m-%d')}부터 {benchmark_end['date'].strftime('%Y-%m-%d')}까지 {len(benchmark)}개 공통 거래일을 사용했습니다.</li>
  <li data-public-note="true">포트폴리오 성과와 KRX 300 가격지수는 배당을 제외했습니다.</li>
  <li data-public-note="true">{COST_DISCLOSURE}</li>
  <li data-public-note="true">{('운용기간 전체 활동이 확인되지 않아 성과 수치는 잠정 성과입니다.' if provisional else '운용기간 활동이 확인되어 성과 수치는 확정 성과입니다.')}</li>
  <li data-public-note="true">이 보고서로 실제 주문을 제출하지 않았습니다.</li>
  <li data-public-note="true">본 자료는 투자 판단 참고용이며 투자 결과와 의사결정의 책임은 투자자에게 있습니다.</li>
</ul>
</section>
</main></body></html>"""
    _assert_public_output(report)
    return report


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a five-section privacy-safe public report")
    parser.add_argument("--model-target", required=True)
    parser.add_argument(
        "--scores-topk", "--topk-factors", "--source-topk-factors",
        dest="topk_factors", required=True,
    )
    parser.add_argument("--valuation", required=True)
    parser.add_argument("--performance-summary", required=True)
    parser.add_argument("--performance-daily", required=True)
    parser.add_argument("--performance-contribution", required=True)
    parser.add_argument("--benchmark-qa", required=True)
    parser.add_argument("--execution-plan", "--model-execution-plan", dest="model_execution_plan", required=True)
    parser.add_argument("--execution-summary", "--model-execution-summary", dest="model_execution_summary", required=True)
    parser.add_argument("--execution-config", required=True)
    parser.add_argument(
        "--broker-account-file",
        default=None,
        help="Explicit broker download file presence gate; never auto-discovered or rendered",
    )
    parser.add_argument("--output", required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    plan_path = Path(args.model_execution_plan)
    lowered_plan_name = plan_path.name.lower()
    if "final_execution" in lowered_plan_name or "overlay" in lowered_plan_name:
        raise ValueError("final/private execution artifacts are forbidden public-report inputs")
    output = Path(args.output)
    if output.exists():
        raise FileExistsError(f"public report output already exists: {output}")
    report = build_public_report(
        _read_csv(args.model_target),
        _read_csv(args.topk_factors),
        _read_csv(args.valuation),
        _read_json(args.performance_summary),
        _read_csv(args.performance_daily),
        _read_csv(args.performance_contribution),
        _read_csv(args.benchmark_qa),
        _read_csv(plan_path),
        _read_json(args.model_execution_summary),
        _read_mapping(args.execution_config),
        broker_account_file=args.broker_account_file,
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report, encoding="utf-8", errors="strict")
    print(f"[OK] public report: {output}")


if __name__ == "__main__":
    main()
