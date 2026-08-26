from __future__ import annotations

import hashlib
import inspect
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from scripts.common.security_id import normalize_security_id_series


PARITY_TOLERANCE = 1e-12
DEFAULT_BOUNDARY_FRAGILE_THRESHOLD = 0.05
SOURCE_NORMALIZATION_POPULATION = "IMMUTABLE_SOURCE_PRODUCTION_PASSED_FILTERS"

REQUIRED_SOURCE_COLUMNS = (
    "ticker",
    "name",
    "passed_filters",
    "score_base",
    "quality_penalty_total",
    "score",
)

REQUIRED_RAW_FACTOR_COLUMNS = (
    "OpIncome_acc2_log1p__raw",
    "Revenue_acc2__raw",
    "Debt_to_Equity_log__raw",
    "op_growth_streak2__raw",
    "rev_growth_streak2__raw",
)

PARITY_AUDIT_COLUMNS = (
    "ticker",
    "name",
    "source_passed_filters",
    "fresh_passed_filters",
    "source_score_base",
    "fresh_score_base",
    "score_base_diff",
    "source_quality_penalty",
    "fresh_quality_penalty",
    "quality_penalty_diff",
    "source_score",
    "fresh_model_score",
    "model_score_diff",
    "raw_factor_parity",
    "normalization_population_source",
    "normalization_population_fresh",
    "filter_stage_source",
    "filter_stage_fresh",
    "parity_status",
)

BOUNDARY_WATCHLIST_COLUMNS = (
    "rank",
    "ticker",
    "name",
    "model_score",
    "score_gap_vs_k",
    "score_gap_vs_previous",
    "quality_penalty",
    "selection_status",
)

_FORBIDDEN_FRESH_SCORE_COLUMNS = frozenset(
    {
        "score_adj",
        "score_adj_rank",
        "hold_bonus_applied",
        "kept_from_previous",
        "selection_bucket",
        "previously_held",
        "current_qty",
        "current_value",
        "current_weight",
    }
)


class ScoreParityError(RuntimeError):
    """Raised when source-score projection cannot prove exact parity."""


@dataclass(frozen=True)
class ScoreParityBundle:
    fresh_scores: pd.DataFrame
    fresh_top_k: pd.DataFrame
    parity_audit: pd.DataFrame
    parity_summary: dict[str, Any]
    scoring_population_audit: dict[str, Any]
    filter_order_audit_markdown: str
    top_k_boundary_watchlist: pd.DataFrame
    top_k_boundary_qa: dict[str, Any]


def _safe_artifact_label(path: str | Path | None) -> str | None:
    if path is None:
        return None
    candidate = Path(path)
    try:
        return candidate.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except (OSError, ValueError):
        return candidate.name


def _sha256(path: str | Path | None) -> str | None:
    if path is None:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        return None
    digest = hashlib.sha256()
    with candidate.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_frame(path: str | Path) -> pd.DataFrame:
    candidate = Path(path)
    if not candidate.is_file():
        raise FileNotFoundError(f"score-parity input is missing: {candidate}")
    suffix = candidate.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(candidate, dtype={"ticker": "string"}, encoding="utf-8-sig")
    if suffix in {".parquet", ".pq"}:
        return pd.read_parquet(candidate)
    raise ValueError(f"unsupported score-parity input format: {candidate.name}")


def _strict_bool_series(series: pd.Series, *, field: str) -> pd.Series:
    if pd.api.types.is_bool_dtype(series.dtype):
        return series.astype(bool)
    normalized = series.astype("string").str.strip().str.lower()
    mapped = normalized.map(
        {
            "true": True,
            "t": True,
            "yes": True,
            "y": True,
            "1": True,
            "false": False,
            "f": False,
            "no": False,
            "n": False,
            "0": False,
        }
    )
    if mapped.isna().any():
        bad = sorted(normalized.loc[mapped.isna()].dropna().unique().tolist())
        raise ScoreParityError(f"{field} contains invalid boolean values: {bad[:10]}")
    return mapped.astype(bool)


def _normalise_source_scores(source_scores: pd.DataFrame) -> pd.DataFrame:
    missing = sorted(set(REQUIRED_SOURCE_COLUMNS) - set(source_scores.columns))
    if missing:
        raise ScoreParityError(f"immutable source scores are missing columns: {missing}")
    missing_raw = sorted(set(REQUIRED_RAW_FACTOR_COLUMNS) - set(source_scores.columns))
    if missing_raw:
        raise ScoreParityError(
            "eligible raw factor parity cannot be proven; source columns missing: "
            f"{missing_raw}"
        )

    source = source_scores.copy(deep=True)
    source["ticker"] = normalize_security_id_series(source["ticker"])
    if source["ticker"].isna().any():
        raise ScoreParityError("immutable source scores contain missing ticker")
    if source["ticker"].duplicated().any():
        duplicates = sorted(
            source.loc[source["ticker"].duplicated(False), "ticker"].unique().tolist()
        )
        raise ScoreParityError(f"immutable source scores contain duplicate tickers: {duplicates[:10]}")
    source["passed_filters"] = _strict_bool_series(
        source["passed_filters"], field="passed_filters"
    )

    eligible = source["passed_filters"]
    if not eligible.any():
        raise ScoreParityError("immutable source scores have no passed-filter population")
    for column in ("score_base", "quality_penalty_total", "score"):
        values = pd.to_numeric(source[column], errors="coerce")
        invalid = eligible & (~values.map(lambda value: math.isfinite(float(value)) if pd.notna(value) else False))
        if invalid.any():
            tickers = source.loc[invalid, "ticker"].head(10).tolist()
            raise ScoreParityError(
                f"eligible source rows have non-finite {column}: {tickers}"
            )
        source[column] = values
    return source


def _factor_diagnostic_columns(source: pd.DataFrame) -> list[str]:
    suffixes = (
        "__raw",
        "__signal",
        "__z",
        "__rank",
        "__mult",
        "__contrib_base",
        "__contrib",
        "__ai_mult",
        "__bucket",
    )
    return [column for column in source.columns if column.endswith(suffixes)]


def project_fresh_start_scores(
    source_scores: pd.DataFrame,
    *,
    top_k: int = 10,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Project immutable production base scores without accepting holdings.

    ``score_adj`` and every holding/keep-current field are deliberately absent
    from both the selection expression and the returned schema.  The only
    model change is therefore the constant holding bonus of zero and the
    disabled keep-current reservation.
    """

    if int(top_k) <= 0:
        raise ValueError("top_k must be positive")
    source = _normalise_source_scores(source_scores)
    eligible = source.loc[source["passed_filters"]].copy()
    if len(eligible) < int(top_k) + 5:
        raise ScoreParityError(
            "source passed-filter population must contain at least top_k + 5 rows "
            "for the required boundary watchlist"
        )

    ranked = eligible.sort_values(
        ["score", "ticker"], ascending=[False, True], kind="stable"
    ).reset_index(drop=True)
    ranked["model_rank"] = pd.Series(
        range(1, len(ranked) + 1), dtype="Int64"
    )
    rank_map = ranked.set_index("ticker")["model_rank"]
    score_map = ranked.set_index("ticker")["score"]

    fresh = pd.DataFrame(
        {
            "ticker": source["ticker"],
            "name": source["name"],
            "model_selected": source["ticker"].isin(set(ranked.head(int(top_k))["ticker"])),
            "model_rank": source["ticker"].map(rank_map).astype("Int64"),
            "model_score": source["ticker"].map(score_map),
            "score_base": source["score_base"].where(source["passed_filters"]),
            "quality_penalty_total": source["quality_penalty_total"].where(
                source["passed_filters"]
            ),
            "holding_bonus": 0.0,
            "keep_current_top_n": 0,
            "current_account_membership_used": False,
            "passed_filters": source["passed_filters"],
        }
    )

    for column in (
        "filter_status",
        "filter_failure_reason",
        "corp_code",
        "industry_code",
        "industry_name",
        "industry4",
        "quality_soft_penalty_active",
        "quality_penalty_netincome_ttm_nonpositive",
        "quality_penalty_netincome_acc2_negative",
        "quality_penalty_cfo_warn",
        "quality_penalty_cfo_isnull",
    ):
        if column in source.columns:
            fresh[column] = source[column]
    for column in _factor_diagnostic_columns(source):
        fresh[column] = source[column]

    fresh["normalization_population_contract"] = SOURCE_NORMALIZATION_POPULATION
    forbidden = sorted(_FORBIDDEN_FRESH_SCORE_COLUMNS.intersection(fresh.columns))
    if forbidden:
        raise ScoreParityError(f"fresh score projection leaked holding fields: {forbidden}")

    fresh = fresh.sort_values(
        ["model_selected", "model_rank", "ticker"],
        ascending=[False, True, True],
        na_position="last",
        kind="stable",
    ).reset_index(drop=True)
    top = fresh.loc[fresh["model_selected"]].sort_values(
        ["model_rank", "ticker"], kind="stable"
    ).reset_index(drop=True)
    if len(top) != int(top_k) or top["ticker"].nunique() != int(top_k):
        raise ScoreParityError("fresh-start Top-K cardinality contract failed")
    return fresh, top


def _numeric_parity(
    source: pd.Series,
    fresh: pd.Series,
    *,
    tolerance: float,
) -> tuple[pd.Series, pd.Series]:
    left = pd.to_numeric(source, errors="coerce")
    right = pd.to_numeric(fresh, errors="coerce")
    both_missing = left.isna() & right.isna()
    both_present = left.notna() & right.notna()
    diff = right - left
    matches = both_missing | (both_present & diff.abs().le(float(tolerance)))
    return diff, matches


def audit_score_parity(
    source_scores: pd.DataFrame,
    fresh_scores: pd.DataFrame,
    *,
    tolerance: float = PARITY_TOLERANCE,
    source_artifact: str | Path | None = None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Compare source and fresh fields and fail closed above ``tolerance``."""

    if float(tolerance) < 0:
        raise ValueError("tolerance must be non-negative")
    source = _normalise_source_scores(source_scores)
    required_fresh = {
        "ticker",
        "name",
        "passed_filters",
        "score_base",
        "quality_penalty_total",
        "model_score",
        "holding_bonus",
        "keep_current_top_n",
        "current_account_membership_used",
    }.union(REQUIRED_RAW_FACTOR_COLUMNS)
    missing = sorted(required_fresh - set(fresh_scores.columns))
    if missing:
        raise ScoreParityError(f"fresh scores are missing parity columns: {missing}")

    fresh = fresh_scores.copy(deep=True)
    fresh["ticker"] = normalize_security_id_series(fresh["ticker"])
    if fresh["ticker"].duplicated().any():
        raise ScoreParityError("fresh scores contain duplicate tickers")
    fresh["passed_filters"] = _strict_bool_series(
        fresh["passed_filters"], field="fresh passed_filters"
    )

    source_indexed = source.set_index("ticker", drop=False)
    fresh_indexed = fresh.set_index("ticker", drop=False)
    if set(source_indexed.index) != set(fresh_indexed.index):
        missing_fresh = sorted(set(source_indexed.index) - set(fresh_indexed.index))
        extra_fresh = sorted(set(fresh_indexed.index) - set(source_indexed.index))
        raise ScoreParityError(
            "source/fresh ticker population mismatch: "
            f"missing_fresh={missing_fresh[:10]} extra_fresh={extra_fresh[:10]}"
        )
    fresh_indexed = fresh_indexed.reindex(source_indexed.index)

    base_diff, base_match = _numeric_parity(
        source_indexed["score_base"], fresh_indexed["score_base"], tolerance=tolerance
    )
    quality_diff, quality_match = _numeric_parity(
        source_indexed["quality_penalty_total"],
        fresh_indexed["quality_penalty_total"],
        tolerance=tolerance,
    )
    score_diff, score_match = _numeric_parity(
        source_indexed["score"], fresh_indexed["model_score"], tolerance=tolerance
    )
    filter_match = source_indexed["passed_filters"].eq(
        fresh_indexed["passed_filters"]
    )
    holding_bonus_match = pd.to_numeric(
        fresh_indexed["holding_bonus"], errors="coerce"
    ).eq(0.0)
    keep_current_match = pd.to_numeric(
        fresh_indexed["keep_current_top_n"], errors="coerce"
    ).eq(0)
    membership_used = _strict_bool_series(
        fresh_indexed["current_account_membership_used"],
        field="current_account_membership_used",
    )
    forbidden_fresh_columns = sorted(
        _FORBIDDEN_FRESH_SCORE_COLUMNS.intersection(fresh.columns)
    )
    independence_match = (
        holding_bonus_match & keep_current_match & ~membership_used
    )

    raw_match = pd.Series(True, index=source_indexed.index, dtype=bool)
    for column in REQUIRED_RAW_FACTOR_COLUMNS:
        _, column_match = _numeric_parity(
            source_indexed[column], fresh_indexed[column], tolerance=tolerance
        )
        raw_match &= column_match

    source_filter_stage = source_indexed.get(
        "filter_failure_reason", pd.Series(pd.NA, index=source_indexed.index)
    ).astype("string")
    source_filter_stage = source_filter_stage.where(
        ~source_indexed["passed_filters"], "PASSED_ALL_SOURCE_FILTER_STAGES"
    ).fillna("EXCLUDED_BY_SOURCE_FILTERS")

    row_pass = (
        base_match
        & quality_match
        & score_match
        & filter_match
        & raw_match
        & independence_match
    )
    audit = pd.DataFrame(
        {
            "ticker": source_indexed["ticker"],
            "name": source_indexed["name"],
            "source_passed_filters": source_indexed["passed_filters"],
            "fresh_passed_filters": fresh_indexed["passed_filters"],
            "source_score_base": source_indexed["score_base"],
            "fresh_score_base": fresh_indexed["score_base"],
            "score_base_diff": base_diff,
            "source_quality_penalty": source_indexed["quality_penalty_total"],
            "fresh_quality_penalty": fresh_indexed["quality_penalty_total"],
            "quality_penalty_diff": quality_diff,
            "source_score": source_indexed["score"],
            "fresh_model_score": fresh_indexed["model_score"],
            "model_score_diff": score_diff,
            "raw_factor_parity": raw_match.map({True: "PASS", False: "FAIL"}),
            "normalization_population_source": SOURCE_NORMALIZATION_POPULATION,
            "normalization_population_fresh": SOURCE_NORMALIZATION_POPULATION,
            "filter_stage_source": source_filter_stage,
            "filter_stage_fresh": source_filter_stage,
            "parity_status": row_pass.map({True: "PASS", False: "FAIL"}),
        }
    ).reset_index(drop=True)
    audit = audit[list(PARITY_AUDIT_COLUMNS)]

    eligible = audit["source_passed_filters"]
    eligible_indexed = source_indexed["passed_filters"]

    def _max_abs(column: str) -> float:
        values = pd.to_numeric(audit.loc[eligible, column], errors="coerce").abs()
        return float(values.max()) if values.notna().any() else 0.0

    all_rows_pass = bool(audit["parity_status"].eq("PASS").all())
    independence_pass = bool(independence_match.all()) and not forbidden_fresh_columns
    summary = {
        "contract_version": "ADVISOR_FULL_RESET_V2_SCORE_PARITY_V1",
        "status": "PASS" if all_rows_pass and independence_pass else "FAIL",
        "tolerance": float(tolerance),
        "source_score_artifact": _safe_artifact_label(source_artifact),
        "source_score_artifact_sha256": _sha256(source_artifact),
        "source_rows": int(len(source)),
        "eligible_rows": int(eligible.sum()),
        "checks": {
            "eligible_raw_factor_parity": "PASS"
            if bool(audit.loc[eligible, "raw_factor_parity"].eq("PASS").all())
            else "FAIL",
            "base_score_parity": "PASS"
            if bool(base_match.loc[eligible_indexed].all())
            else "FAIL",
            "quality_penalty_parity": "PASS"
            if bool(quality_match.loc[eligible_indexed].all())
            else "FAIL",
            "model_score_parity": "PASS"
            if bool(score_match.loc[eligible_indexed].all())
            else "FAIL",
            "passed_filter_population_parity": "PASS"
            if bool(filter_match.all())
            else "FAIL",
            "current_holdings_independence": "PASS_BY_CONSTRUCTION_NO_HOLDINGS_INPUT"
            if independence_pass
            else "FAIL",
            "forbidden_fresh_columns": forbidden_fresh_columns,
            "score_adj_used": "score_adj" in forbidden_fresh_columns,
            "holding_bonus_used": not bool(holding_bonus_match.all()),
            "keep_current_used": not bool(keep_current_match.all()),
        },
        "max_abs_score_base_diff": _max_abs("score_base_diff"),
        "max_abs_quality_penalty_diff": _max_abs("quality_penalty_diff"),
        "max_abs_model_score_diff": _max_abs("model_score_diff"),
    }
    if summary["status"] != "PASS":
        failures = audit.loc[audit["parity_status"].eq("FAIL"), "ticker"].head(10).tolist()
        raise ScoreParityError(
            "fresh-start score parity exceeds the fail-closed tolerance "
            f"{tolerance} or violates holdings independence: {failures}"
        )
    return audit, summary


def _filter_column(filter_key: str) -> str | None:
    for prefix in (
        "require_notnull_",
        "maxq_",
        "minq_",
        "max_",
        "min_",
    ):
        if filter_key.startswith(prefix):
            column = filter_key[len(prefix) :]
            return column or None
    return None


def _apply_one_legacy_filter(
    frame: pd.DataFrame,
    key: str,
    value: Any,
) -> tuple[pd.DataFrame, str, str | None]:
    """Replay the production helper's ordered skip-if-empty semantics."""

    column = _filter_column(str(key))
    if column is None:
        return frame.copy(), "SKIPPED_UNSUPPORTED_FILTER_KEY", None
    if column not in frame.columns:
        return frame.copy(), "SKIPPED_MISSING_SOURCE_COLUMN", column

    candidate = frame
    if key.startswith("require_notnull_"):
        candidate = frame.loc[frame[column].notna()]
    elif key.startswith("maxq_") or key.startswith("minq_"):
        numeric = pd.to_numeric(frame[column], errors="coerce")
        valid = numeric.dropna()
        if valid.empty:
            return frame.copy(), "SKIPPED_NO_VALID_DATA", column
        quantile = min(max(float(value), 0.0), 1.0)
        threshold = float(valid.quantile(quantile))
        if key.startswith("maxq_"):
            candidate = frame.loc[numeric.notna() & numeric.le(threshold)]
        else:
            candidate = frame.loc[numeric.notna() & numeric.ge(threshold)]
    elif key.startswith("max_"):
        numeric = pd.to_numeric(frame[column], errors="coerce")
        candidate = frame.loc[numeric.isna() | numeric.le(float(value))]
    elif key.startswith("min_"):
        numeric = pd.to_numeric(frame[column], errors="coerce")
        candidate = frame.loc[numeric.isna() | numeric.ge(float(value))]

    if candidate.empty:
        return frame.copy(), "SKIPPED_WOULD_EMPTY_POPULATION", column
    return candidate.copy(), "APPLIED", column


def replay_production_filter_order(
    source_population: pd.DataFrame,
    filters: Mapping[str, Any],
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Replay the exact ordered production filter contract for audit only."""

    if "ticker" not in source_population.columns:
        raise ScoreParityError("filter replay population is missing ticker")
    working = source_population.copy(deep=True)
    working["ticker"] = normalize_security_id_series(working["ticker"])
    stages: list[dict[str, Any]] = []
    for order, (key, value) in enumerate(filters.items(), start=1):
        before = len(working)
        next_frame, status, column = _apply_one_legacy_filter(
            working, str(key), value
        )
        stages.append(
            {
                "order": order,
                "filter": str(key),
                "input_column": column,
                "configured_value": value,
                "status": status,
                "rows_before": int(before),
                "rows_after": int(len(next_frame)),
                "rows_removed": int(before - len(next_frame)),
            }
        )
        working = next_frame

    strict_order = len(stages) + 1
    if "op_qoq" not in working.columns:
        strict_status = "SKIPPED_MISSING_SOURCE_COLUMN"
        strict = working
    else:
        numeric = pd.to_numeric(working["op_qoq"], errors="coerce")
        candidate = working.loc[numeric.notna() & numeric.gt(0)]
        if candidate.empty:
            strict_status = "SKIPPED_WOULD_EMPTY_POPULATION"
            strict = working
        else:
            strict_status = "APPLIED"
            strict = candidate
    stages.append(
        {
            "order": strict_order,
            "filter": "strict_op_qoq_gt_0",
            "input_column": "op_qoq",
            "configured_value": 0,
            "status": strict_status,
            "rows_before": int(len(working)),
            "rows_after": int(len(strict)),
            "rows_removed": int(len(working) - len(strict)),
        }
    )
    return strict.copy(), stages


def _attach_diagnostic_missing_filter_inputs(
    source: pd.DataFrame,
    authoritative_filter_inputs: pd.DataFrame,
    filters: Mapping[str, Any],
) -> tuple[pd.DataFrame, list[str]]:
    if "ticker" not in authoritative_filter_inputs.columns:
        raise ScoreParityError("diagnostic filter inputs are missing ticker")
    diagnostic = authoritative_filter_inputs.copy(deep=True)
    diagnostic["ticker"] = normalize_security_id_series(diagnostic["ticker"])
    if diagnostic["ticker"].duplicated().any():
        raise ScoreParityError("diagnostic filter inputs contain duplicate tickers")
    required = [
        column
        for key in filters
        for column in [_filter_column(str(key))]
        if column is not None and column not in source.columns
    ]
    attached = sorted(set(required))
    unavailable = sorted(set(attached) - set(diagnostic.columns))
    if unavailable:
        raise ScoreParityError(
            f"diagnostic filter inputs are missing required columns: {unavailable}"
        )
    merged = source.merge(
        diagnostic[["ticker", *attached]],
        on="ticker",
        how="left",
        validate="one_to_one",
    )
    return merged, attached


def diagnose_scoring_population(
    source_scores: pd.DataFrame,
    *,
    filters: Mapping[str, Any],
    source_artifact: str | Path | None = None,
    drifted_fresh_scores: pd.DataFrame | None = None,
    drifted_artifact: str | Path | None = None,
    authoritative_filter_inputs: pd.DataFrame | None = None,
    authoritative_filter_inputs_artifact: str | Path | None = None,
    require_drift_diagnosis: bool = True,
) -> tuple[dict[str, Any], str]:
    """Prove the production 405 vs V1 355 population/normalization drift."""

    source = _normalise_source_scores(source_scores)
    replayed, source_stages = replay_production_filter_order(source, filters)
    replayed_set = set(replayed["ticker"])
    source_passed_set = set(source.loc[source["passed_filters"], "ticker"])
    if replayed_set != source_passed_set:
        raise ScoreParityError(
            "ordered source filter replay does not reproduce immutable source passed_filters"
        )

    audit: dict[str, Any] = {
        "contract_version": "ADVISOR_FULL_RESET_V2_SCORING_POPULATION_AUDIT_V1",
        "status": "PASS",
        "source_score_artifact": _safe_artifact_label(source_artifact),
        "source_score_artifact_sha256": _sha256(source_artifact),
        "source_population_rows": int(len(source)),
        "source_passed_filter_rows": int(len(source_passed_set)),
        "fresh_normalization_population_rows": int(len(source_passed_set)),
        "fresh_normalization_population_contract": SOURCE_NORMALIZATION_POPULATION,
        "source_filter_replay_status": "PASS",
        "source_filter_stages": source_stages,
        "drift_diagnosis": {
            "status": "NOT_REQUESTED",
            "cause": None,
        },
    }

    if drifted_fresh_scores is None and authoritative_filter_inputs is None:
        if require_drift_diagnosis:
            raise ScoreParityError(
                "V2 contract requires the drifted V1 scores and authoritative "
                "filter inputs for the 405 vs 355 diagnosis"
            )
    else:
        if drifted_fresh_scores is None or authoritative_filter_inputs is None:
            raise ScoreParityError(
                "drift diagnosis requires both drifted fresh scores and authoritative filter inputs"
            )
        drifted = drifted_fresh_scores.copy(deep=True)
        required_drifted = {"ticker", "passed_filters", "score_base", "model_score"}
        missing = sorted(required_drifted - set(drifted.columns))
        if missing:
            raise ScoreParityError(f"drifted fresh scores are missing columns: {missing}")
        drifted["ticker"] = normalize_security_id_series(drifted["ticker"])
        drifted["passed_filters"] = _strict_bool_series(
            drifted["passed_filters"], field="drifted passed_filters"
        )
        if drifted["ticker"].duplicated().any():
            raise ScoreParityError("drifted fresh scores contain duplicate tickers")

        diagnostic_population, attached = _attach_diagnostic_missing_filter_inputs(
            source, authoritative_filter_inputs, filters
        )
        experimental, experimental_stages = replay_production_filter_order(
            diagnostic_population, filters
        )
        drifted_set = set(drifted.loc[drifted["passed_filters"], "ticker"])
        experimental_set = set(experimental["ticker"])
        common = source_passed_set.intersection(drifted_set)
        overlap = source.loc[source["ticker"].isin(common), ["ticker", "score_base"]].merge(
            drifted.loc[drifted["ticker"].isin(common), ["ticker", "score_base", "model_score"]],
            on="ticker",
            how="inner",
            suffixes=("_source", "_drifted"),
            validate="one_to_one",
        )
        base_drift = (
            pd.to_numeric(overlap["score_base_drifted"], errors="coerce")
            - pd.to_numeric(overlap["score_base_source"], errors="coerce")
        ).abs()
        exact_population_match = experimental_set == drifted_set
        skipped_source_columns = sorted(
            {
                stage["input_column"]
                for stage in source_stages
                if stage["status"] == "SKIPPED_MISSING_SOURCE_COLUMN"
                and stage["input_column"] is not None
            }
        )
        expected_new_columns = sorted(set(attached))
        if exact_population_match and set(expected_new_columns) == {
            "mcap",
            "traded_value",
        }:
            cause_status = (
                "EXACT_MATCH_MCAP_TRADED_VALUE_FILTERS_BEFORE_STANDARDIZATION"
            )
        elif exact_population_match:
            cause_status = "EXACT_MATCH_NEW_FILTER_INPUTS_BEFORE_STANDARDIZATION"
        else:
            cause_status = "UNRESOLVED_POPULATION_MISMATCH"
        audit["drift_diagnosis"] = {
            "status": "PASS" if exact_population_match else "FAIL",
            "cause": cause_status,
            "drifted_fresh_artifact": _safe_artifact_label(drifted_artifact),
            "drifted_fresh_artifact_sha256": _sha256(drifted_artifact),
            "authoritative_filter_inputs_artifact": _safe_artifact_label(
                authoritative_filter_inputs_artifact
            ),
            "authoritative_filter_inputs_artifact_sha256": _sha256(
                authoritative_filter_inputs_artifact
            ),
            "drifted_passed_filter_rows": int(len(drifted_set)),
            "source_minus_drifted_rows": int(len(source_passed_set - drifted_set)),
            "drifted_minus_source_rows": int(len(drifted_set - source_passed_set)),
            "common_eligible_rows": int(len(common)),
            "diagnostic_attached_filter_columns": expected_new_columns,
            "source_skipped_missing_filter_columns": skipped_source_columns,
            "diagnostic_population_exactly_matches_drifted_population": bool(
                exact_population_match
            ),
            "max_abs_score_base_drift_on_common_rows": float(base_drift.max())
            if base_drift.notna().any()
            else None,
            "mean_abs_score_base_drift_on_common_rows": float(base_drift.mean())
            if base_drift.notna().any()
            else None,
            "normalization_drift_mechanism": (
                "The drifted scorer attached previously absent filter inputs, reduced the "
                "passed-filter population, and recomputed robust-z within that reduced population."
            ),
            "diagnostic_filter_stages": experimental_stages,
        }
        if not exact_population_match:
            audit["status"] = "FAIL"
            raise ScoreParityError(
                "diagnostic missing-filter replay does not exactly reproduce the drifted V1 population"
            )

    markdown = render_filter_order_audit_markdown(audit)
    return audit, markdown


def render_filter_order_audit_markdown(population_audit: Mapping[str, Any]) -> str:
    stages = list(population_audit.get("source_filter_stages") or [])
    diagnosis = dict(population_audit.get("drift_diagnosis") or {})
    lines = [
        "# Filter order audit",
        "",
        "## 기본 fresh-start 계약",
        "",
        "기본 fresh-start는 immutable production score의 `score_base`, "
        "`quality_penalty_total`, `score`를 그대로 사용합니다. `score_adj`, "
        "보유 보너스와 keep-current는 입력 또는 순위 계산에 사용하지 않습니다.",
        "",
        "## Production filter replay",
        "",
        "| 순서 | 필터 | 입력 열 | 상태 | 전 | 후 | 제외 |",
        "|---:|---|---|---|---:|---:|---:|",
    ]
    for stage in stages:
        lines.append(
            "| {order} | `{filter}` | `{column}` | {status} | {before} | {after} | {removed} |".format(
                order=stage.get("order"),
                filter=stage.get("filter"),
                column=stage.get("input_column") or "N/A",
                status=stage.get("status"),
                before=stage.get("rows_before"),
                after=stage.get("rows_after"),
                removed=stage.get("rows_removed"),
            )
        )

    lines.extend(
        [
            "",
            "## 405 vs 355 원인",
            "",
        ]
    )
    if diagnosis.get("status") == "PASS":
        attached = ", ".join(
            f"`{column}`"
            for column in diagnosis.get("diagnostic_attached_filter_columns", [])
        ) or "N/A"
        skipped = ", ".join(
            f"`{column}`"
            for column in diagnosis.get("source_skipped_missing_filter_columns", [])
        ) or "N/A"
        lines.extend(
            [
                f"- Source production passed-filter population: **{population_audit.get('source_passed_filter_rows')}**",
                f"- Drifted V1 passed-filter population: **{diagnosis.get('drifted_passed_filter_rows')}**",
                f"- Production에서 입력 열 부재로 건너뛴 필터 열: {skipped}",
                f"- V1 diagnostic replay에서 새로 붙인 열: {attached}",
                "- 위 열을 production filter 순서에 넣은 diagnostic population이 "
                "V1 355개 집합과 ticker 단위로 정확히 일치했습니다.",
                "- 따라서 score drift는 보유편향 제거 때문이 아니라, 405개에서 355개로 "
                "모집단을 먼저 축소한 뒤 robust-z를 다시 계산한 데서 발생했습니다.",
                "- 이 filter-first 동작은 기본 fresh-start에서 제거되며, 실험 시 별도 전략으로만 격리해야 합니다.",
            ]
        )
        diagnostic_stages = list(diagnosis.get("diagnostic_filter_stages") or [])
        if diagnostic_stages:
            lines.extend(
                [
                    "",
                    "### V1 diagnostic filter-first replay",
                    "",
                    "| 순서 | 필터 | 입력 열 | 상태 | 전 | 후 | 제외 |",
                    "|---:|---|---|---|---:|---:|---:|",
                ]
            )
            for stage in diagnostic_stages:
                lines.append(
                    "| {order} | `{filter}` | `{column}` | {status} | {before} | {after} | {removed} |".format(
                        order=stage.get("order"),
                        filter=stage.get("filter"),
                        column=stage.get("input_column") or "N/A",
                        status=stage.get("status"),
                        before=stage.get("rows_before"),
                        after=stage.get("rows_after"),
                        removed=stage.get("rows_removed"),
                    )
                )
    else:
        lines.append(
            "Drift reference가 제공되지 않아 405 vs 355 진단은 실행되지 않았습니다. "
            "V2 correction run에서는 drifted V1 score와 certified filter inputs를 함께 제공해야 합니다."
        )
    lines.extend(
        [
            "",
            "## 결론",
            "",
            "기본 fresh-start의 normalization population은 source production의 "
            "passed-filter population과 동일하며, 별도 factor standardization은 수행하지 않습니다.",
            "",
        ]
    )
    return "\n".join(lines)


def build_top_k_boundary_watchlist(
    fresh_scores: pd.DataFrame,
    *,
    top_k: int = 10,
    fragile_threshold: float = DEFAULT_BOUNDARY_FRAGILE_THRESHOLD,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    if int(top_k) < 3:
        raise ValueError("top_k must be at least 3 for K-2 boundary coverage")
    if float(fragile_threshold) < 0:
        raise ValueError("fragile_threshold must be non-negative")
    required = {
        "ticker",
        "name",
        "passed_filters",
        "model_score",
        "quality_penalty_total",
    }
    missing = sorted(required - set(fresh_scores.columns))
    if missing:
        raise ScoreParityError(f"fresh scores are missing boundary fields: {missing}")
    scores = fresh_scores.copy(deep=True)
    scores["ticker"] = normalize_security_id_series(scores["ticker"])
    scores["passed_filters"] = _strict_bool_series(
        scores["passed_filters"], field="boundary passed_filters"
    )
    ranked = scores.loc[scores["passed_filters"]].copy()
    ranked["model_score"] = pd.to_numeric(ranked["model_score"], errors="coerce")
    if ranked["model_score"].isna().any():
        raise ScoreParityError("eligible boundary population has missing model_score")
    ranked = ranked.sort_values(
        ["model_score", "ticker"], ascending=[False, True], kind="stable"
    ).reset_index(drop=True)
    if len(ranked) < int(top_k) + 5:
        raise ScoreParityError(
            "eligible population does not cover required boundary ranks K-2 through K+5"
        )
    ranked["rank"] = range(1, len(ranked) + 1)
    kth_score = float(ranked.iloc[int(top_k) - 1]["model_score"])
    next_score = float(ranked.iloc[int(top_k)]["model_score"])
    gap = kth_score - next_score

    start_rank = int(top_k) - 2
    end_rank = int(top_k) + 5
    watch = ranked.loc[
        ranked["rank"].between(start_rank, end_rank),
        ["rank", "ticker", "name", "model_score", "quality_penalty_total"],
    ].copy()
    watch["score_gap_vs_k"] = watch["model_score"] - kth_score
    previous_scores = ranked["model_score"].shift(1)
    previous_gap = previous_scores - ranked["model_score"]
    gap_map = pd.Series(previous_gap.values, index=ranked["rank"]).to_dict()
    watch["score_gap_vs_previous"] = watch["rank"].map(gap_map)
    watch["quality_penalty"] = pd.to_numeric(
        watch.pop("quality_penalty_total"), errors="coerce"
    )
    watch["selection_status"] = watch["rank"].le(int(top_k)).map(
        {True: "SELECTED", False: "NOT_SELECTED"}
    )
    watch = watch[list(BOUNDARY_WATCHLIST_COLUMNS)].reset_index(drop=True)

    qa = {
        "contract_version": "ADVISOR_FULL_RESET_V2_TOP_K_BOUNDARY_V1",
        "status": "PASS",
        "top_k": int(top_k),
        "watchlist_rank_start": start_rank,
        "watchlist_rank_end": end_rank,
        "watchlist_rows": int(len(watch)),
        "rank_k_score": kth_score,
        "rank_k_plus_1_score": next_score,
        "rank_k_minus_k_plus_1_gap": gap,
        "fragile_threshold": float(fragile_threshold),
        "selection_boundary_status": "FRAGILE"
        if gap < float(fragile_threshold)
        else "STABLE",
        "automatic_selection_change_applied": False,
    }
    return watch, qa


def build_score_parity_bundle(
    source_scores: pd.DataFrame,
    *,
    filters: Mapping[str, Any],
    top_k: int = 10,
    tolerance: float = PARITY_TOLERANCE,
    fragile_threshold: float = DEFAULT_BOUNDARY_FRAGILE_THRESHOLD,
    source_artifact: str | Path | None = None,
    drifted_fresh_scores: pd.DataFrame | None = None,
    drifted_artifact: str | Path | None = None,
    authoritative_filter_inputs: pd.DataFrame | None = None,
    authoritative_filter_inputs_artifact: str | Path | None = None,
    require_drift_diagnosis: bool = True,
) -> ScoreParityBundle:
    fresh, top = project_fresh_start_scores(source_scores, top_k=top_k)
    parity_audit, parity_summary = audit_score_parity(
        source_scores,
        fresh,
        tolerance=tolerance,
        source_artifact=source_artifact,
    )
    population_audit, filter_markdown = diagnose_scoring_population(
        source_scores,
        filters=filters,
        source_artifact=source_artifact,
        drifted_fresh_scores=drifted_fresh_scores,
        drifted_artifact=drifted_artifact,
        authoritative_filter_inputs=authoritative_filter_inputs,
        authoritative_filter_inputs_artifact=authoritative_filter_inputs_artifact,
        require_drift_diagnosis=require_drift_diagnosis,
    )
    boundary, boundary_qa = build_top_k_boundary_watchlist(
        fresh,
        top_k=top_k,
        fragile_threshold=fragile_threshold,
    )
    return ScoreParityBundle(
        fresh_scores=fresh,
        fresh_top_k=top,
        parity_audit=parity_audit,
        parity_summary=parity_summary,
        scoring_population_audit=population_audit,
        filter_order_audit_markdown=filter_markdown,
        top_k_boundary_watchlist=boundary,
        top_k_boundary_qa=boundary_qa,
    )


def build_score_parity_bundle_from_paths(
    source_scores_path: str | Path,
    *,
    filters: Mapping[str, Any],
    top_k: int = 10,
    tolerance: float = PARITY_TOLERANCE,
    fragile_threshold: float = DEFAULT_BOUNDARY_FRAGILE_THRESHOLD,
    drifted_fresh_scores_path: str | Path | None = None,
    authoritative_filter_inputs_path: str | Path | None = None,
    require_drift_diagnosis: bool = True,
) -> ScoreParityBundle:
    """File-backed runner API; none of its arguments can contain holdings."""

    source = _read_frame(source_scores_path)
    drifted = (
        _read_frame(drifted_fresh_scores_path)
        if drifted_fresh_scores_path is not None
        else None
    )
    diagnostic_inputs = (
        _read_frame(authoritative_filter_inputs_path)
        if authoritative_filter_inputs_path is not None
        else None
    )
    return build_score_parity_bundle(
        source,
        filters=filters,
        top_k=top_k,
        tolerance=tolerance,
        fragile_threshold=fragile_threshold,
        source_artifact=source_scores_path,
        drifted_fresh_scores=drifted,
        drifted_artifact=drifted_fresh_scores_path,
        authoritative_filter_inputs=diagnostic_inputs,
        authoritative_filter_inputs_artifact=authoritative_filter_inputs_path,
        require_drift_diagnosis=require_drift_diagnosis,
    )


def write_score_parity_artifacts(
    bundle: ScoreParityBundle,
    output_dir: str | Path,
) -> dict[str, Path]:
    """Write the score/population/boundary contract artifacts for a V2 run."""

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    paths = {
        "fresh_start_scores": root / "fresh_start_scores.csv",
        "fresh_start_top_k": root / "fresh_start_top_k.csv",
        "score_parity_audit_csv": root / "score_parity_audit.csv",
        "score_parity_audit_json": root / "score_parity_audit.json",
        "scoring_population_audit": root / "scoring_population_audit.json",
        "filter_order_audit": root / "filter_order_audit.md",
        "top_k_boundary_watchlist": root / "top_k_boundary_watchlist.csv",
        "top_k_boundary_qa": root / "top_k_boundary_qa.json",
    }
    bundle.fresh_scores.to_csv(
        paths["fresh_start_scores"], index=False, encoding="utf-8-sig"
    )
    bundle.fresh_top_k.to_csv(
        paths["fresh_start_top_k"], index=False, encoding="utf-8-sig"
    )
    bundle.parity_audit.to_csv(
        paths["score_parity_audit_csv"], index=False, encoding="utf-8-sig"
    )
    paths["score_parity_audit_json"].write_text(
        json.dumps(bundle.parity_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths["scoring_population_audit"].write_text(
        json.dumps(bundle.scoring_population_audit, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    paths["filter_order_audit"].write_text(
        bundle.filter_order_audit_markdown, encoding="utf-8"
    )
    bundle.top_k_boundary_watchlist.to_csv(
        paths["top_k_boundary_watchlist"], index=False, encoding="utf-8-sig"
    )
    paths["top_k_boundary_qa"].write_text(
        json.dumps(bundle.top_k_boundary_qa, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return paths


def assert_no_holdings_parameter() -> None:
    """Guard the public projection APIs against future holdings coupling."""

    forbidden_tokens = ("holding", "current_position", "previous_position")
    for function in (
        project_fresh_start_scores,
        build_score_parity_bundle,
        build_score_parity_bundle_from_paths,
    ):
        parameter_names = tuple(inspect.signature(function).parameters)
        offending = [
            parameter
            for parameter in parameter_names
            if any(token in parameter.lower() for token in forbidden_tokens)
        ]
        if offending:
            raise ScoreParityError(
                f"{function.__name__} accepts forbidden holdings inputs: {offending}"
            )


assert_no_holdings_parameter()
