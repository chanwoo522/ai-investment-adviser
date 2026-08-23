from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import pandas as pd

from scripts.common.security_id import normalize_security_id_series


INDUSTRY_CONTRACT_VERSION = "ADVISOR_FULL_RESET_V2_INDUSTRY_V1"
KNOWN_INVALID_TICKER = "322000"
KNOWN_INVALID_INDUSTRY_TERMS = ("반도체", "SEMICONDUCTOR")


class IndustryContractError(ValueError):
    """Raised when exact identifier/taxonomy lineage cannot be preserved."""


@dataclass(frozen=True)
class IndustryArtifacts:
    industry_mapping_qa: pd.DataFrame
    sector_exposure_current: pd.DataFrame
    sector_exposure_target: pd.DataFrame
    qa: dict[str, Any]


def _text(value: object) -> str | None:
    if value is None or value is pd.NA or pd.isna(value):
        return None
    text = str(value).strip()
    return text if text and text not in {"nan", "None", "<NA>"} else None


def _require_string_identifiers(values: pd.Series, pattern: str, field: str) -> pd.Series:
    # Loading codes as numbers can irreversibly collapse leading zeroes and
    # taxonomy namespaces.  Reject rather than guessing the intended string.
    if pd.api.types.is_numeric_dtype(values.dtype):
        raise IndustryContractError(f"{field} must be loaded as a string identifier")
    normalized = values.astype("string").str.strip()
    if ~normalized.str.fullmatch(pattern, na=False).all():
        raise IndustryContractError(f"{field} contains malformed identifiers")
    return normalized


def canonicalize_security_registry(
    securities: pd.DataFrame | Sequence[Mapping[str, Any]],
) -> pd.DataFrame:
    """Validate one-to-one ticker/corp/security identifiers without fuzzy keys."""

    registry = pd.DataFrame(securities).copy()
    required = {"ticker", "corp_code", "security_id"}
    missing = sorted(required - set(registry.columns))
    if missing:
        raise IndustryContractError(f"security registry missing exact keys: {missing}")
    registry["ticker"] = normalize_security_id_series(registry["ticker"])
    if registry["ticker"].isna().any():
        raise IndustryContractError("security registry contains invalid ticker")
    registry["corp_code"] = _require_string_identifiers(
        registry["corp_code"], r"\d{8}", "corp_code"
    )
    registry["security_id"] = _require_string_identifiers(
        registry["security_id"], r"[0-9A-Za-z]{6,32}", "security_id"
    )
    for key in ("ticker", "corp_code", "security_id"):
        if registry[key].duplicated().any():
            duplicates = registry.loc[registry[key].duplicated(keep=False), key].tolist()
            raise IndustryContractError(f"{key} is not one-to-one: {duplicates[:5]}")
    if "name" not in registry.columns:
        registry["name"] = pd.NA
    return registry.reset_index(drop=True)


def build_security_registry_from_exact_sources(
    securities: pd.DataFrame | Sequence[Mapping[str, Any]],
    identifier_sources: Sequence[pd.DataFrame | Sequence[Mapping[str, Any]]],
) -> pd.DataFrame:
    """Resolve corp codes from exact-ticker sources and reject conflicts.

    ``security_id`` is the canonical KRX security identifier and is explicitly
    copied from the normalized ticker.  No company-name matching is attempted.
    """

    requested = pd.DataFrame(securities).copy()
    if "ticker" not in requested.columns:
        raise IndustryContractError("securities must contain ticker")
    requested["ticker"] = normalize_security_id_series(requested["ticker"])
    if requested["ticker"].isna().any() or requested["ticker"].duplicated().any():
        raise IndustryContractError("requested security tickers must be unique and valid")
    if "name" not in requested.columns:
        requested["name"] = pd.NA

    evidence: list[pd.DataFrame] = []
    for source_index, source in enumerate(identifier_sources):
        frame = pd.DataFrame(source).copy()
        if not {"ticker", "corp_code"}.issubset(frame.columns):
            raise IndustryContractError(
                f"identifier source {source_index} must contain ticker and corp_code"
            )
        frame["ticker"] = normalize_security_id_series(frame["ticker"])
        frame = frame.loc[frame["ticker"].isin(set(requested["ticker"]))].copy()
        if len(frame) == 0:
            continue
        frame["corp_code"] = _require_string_identifiers(
            frame["corp_code"], r"\d{8}", f"identifier source {source_index} corp_code"
        )
        frame = frame[["ticker", "corp_code"]].drop_duplicates()
        if frame["ticker"].duplicated().any():
            raise IndustryContractError(
                f"identifier source {source_index} maps one ticker to multiple corp codes"
            )
        frame["identifier_source_index"] = source_index
        evidence.append(frame)
    combined = (
        pd.concat(evidence, ignore_index=True)
        if evidence
        else pd.DataFrame(columns=["ticker", "corp_code", "identifier_source_index"])
    )
    conflicts = combined.groupby("ticker")["corp_code"].nunique()
    conflicts = conflicts.loc[conflicts.gt(1)]
    if len(conflicts):
        raise IndustryContractError(
            f"exact ticker/corp crosswalk conflict: {conflicts.index.tolist()[:5]}"
        )
    crosswalk = combined[["ticker", "corp_code"]].drop_duplicates("ticker")
    registry = requested[["ticker", "name"]].merge(
        crosswalk, on="ticker", how="left", validate="one_to_one"
    )
    if registry["corp_code"].isna().any():
        missing = registry.loc[registry["corp_code"].isna(), "ticker"].tolist()
        raise IndustryContractError(f"corp_code unavailable for exact ticker keys: {missing}")
    registry["security_id"] = registry["ticker"].astype("string")
    return canonicalize_security_registry(registry)


def build_advisor_sector_map_from_exact_ticker_policy(
    securities: pd.DataFrame | Sequence[Mapping[str, Any]],
    exact_ticker_policy: Mapping[str, str],
    *,
    advisor_sector_source: str,
) -> pd.DataFrame:
    """Expand a reviewed exact-ticker policy onto the three-key registry."""

    registry = canonicalize_security_registry(securities)
    policy = pd.DataFrame(
        {"ticker": list(exact_ticker_policy.keys()), "advisor_sector": list(exact_ticker_policy.values())}
    )
    policy["ticker"] = normalize_security_id_series(policy["ticker"])
    if policy["ticker"].isna().any() or policy["ticker"].duplicated().any():
        raise IndustryContractError("advisor exact-ticker policy contains invalid/duplicate keys")
    policy["advisor_sector"] = policy["advisor_sector"].astype("string").str.strip()
    if policy["advisor_sector"].isna().any() or policy["advisor_sector"].eq("").any():
        raise IndustryContractError("advisor exact-ticker policy contains blank sectors")
    result = registry[["ticker", "corp_code", "security_id"]].merge(
        policy, on="ticker", how="left", validate="one_to_one"
    )
    if result["advisor_sector"].isna().any():
        missing = result.loc[result["advisor_sector"].isna(), "ticker"].tolist()
        raise IndustryContractError(f"advisor sector policy is incomplete: {missing}")
    result["advisor_sector_source"] = str(advisor_sector_source)
    return result


def canonicalize_official_industry_reference(
    reference: pd.DataFrame | Sequence[Mapping[str, Any]],
    *,
    official_industry_source: str,
    taxonomy_namespace: str,
    taxonomy_version: str,
) -> pd.DataFrame:
    """Canonicalize an official ticker reference while preserving code text."""

    frame = pd.DataFrame(reference).copy()
    if "ticker" not in frame.columns:
        raise IndustryContractError("official industry reference missing ticker")
    code_column = next(
        (column for column in ("official_industry_code", "industry_code") if column in frame.columns),
        None,
    )
    name_column = next(
        (column for column in ("official_industry_name", "industry_name") if column in frame.columns),
        None,
    )
    if code_column is None or name_column is None:
        raise IndustryContractError("official industry reference missing code or name")
    frame["ticker"] = normalize_security_id_series(frame["ticker"])
    if frame["ticker"].isna().any() or frame["ticker"].duplicated().any():
        raise IndustryContractError("official industry reference ticker must be unique")
    if pd.api.types.is_numeric_dtype(frame[code_column].dtype):
        raise IndustryContractError(
            "official industry codes must be strings; numeric conversion can cause collisions"
        )
    frame["official_industry_code"] = frame[code_column].astype("string").str.strip()
    frame["official_industry_name"] = frame[name_column].astype("string").str.strip()
    missing_value = (
        frame["official_industry_code"].isna()
        | frame["official_industry_code"].eq("")
        | frame["official_industry_name"].isna()
        | frame["official_industry_name"].eq("")
    )
    if missing_value.any():
        raise IndustryContractError("official industry reference contains blank code/name")
    frame["official_industry_source"] = str(official_industry_source)
    frame["taxonomy_namespace"] = str(taxonomy_namespace)
    frame["taxonomy_version"] = str(taxonomy_version)
    return frame[
        [
            "ticker",
            "official_industry_code",
            "official_industry_name",
            "official_industry_source",
            "taxonomy_namespace",
            "taxonomy_version",
        ]
    ].reset_index(drop=True)


def _known_invalid(ticker: object, industry_name: object) -> bool:
    if _text(ticker) != KNOWN_INVALID_TICKER:
        return False
    name = (_text(industry_name) or "").upper()
    return any(term.upper() in name for term in KNOWN_INVALID_INDUSTRY_TERMS)


def _canonical_override_table(
    overrides: pd.DataFrame | Sequence[Mapping[str, Any]] | None,
) -> pd.DataFrame:
    columns = [
        "ticker",
        "corp_code",
        "security_id",
        "official_industry_code",
        "official_industry_name",
        "official_industry_source",
        "taxonomy_namespace",
        "taxonomy_version",
        "override_reason",
    ]
    if overrides is None:
        return pd.DataFrame(columns=columns)
    frame = pd.DataFrame(overrides).copy()
    missing = sorted(set(columns) - set(frame.columns))
    if missing:
        raise IndustryContractError(f"issuer override missing fields: {missing}")
    frame["ticker"] = normalize_security_id_series(frame["ticker"])
    frame["corp_code"] = _require_string_identifiers(frame["corp_code"], r"\d{8}", "override corp_code")
    frame["security_id"] = _require_string_identifiers(
        frame["security_id"], r"[0-9A-Za-z]{6,32}", "override security_id"
    )
    if frame.duplicated(["ticker", "corp_code", "security_id"]).any():
        raise IndustryContractError("issuer overrides contain duplicate exact keys")
    for column in columns[3:]:
        frame[column] = frame[column].astype("string").str.strip()
        if frame[column].isna().any() or frame[column].eq("").any():
            raise IndustryContractError(f"issuer override contains blank {column}")
    return frame[columns].reset_index(drop=True)


def _canonical_advisor_sector_map(
    sector_map: pd.DataFrame | Sequence[Mapping[str, Any]],
) -> pd.DataFrame:
    frame = pd.DataFrame(sector_map).copy()
    required = {
        "ticker",
        "corp_code",
        "security_id",
        "advisor_sector",
        "advisor_sector_source",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        raise IndustryContractError(f"advisor sector map missing exact fields: {missing}")
    frame["ticker"] = normalize_security_id_series(frame["ticker"])
    frame["corp_code"] = _require_string_identifiers(
        frame["corp_code"], r"\d{8}", "advisor sector corp_code"
    )
    frame["security_id"] = _require_string_identifiers(
        frame["security_id"], r"[0-9A-Za-z]{6,32}", "advisor sector security_id"
    )
    if frame.duplicated(["ticker", "corp_code", "security_id"]).any():
        raise IndustryContractError("advisor sector map contains duplicate exact keys")
    for column in ("advisor_sector", "advisor_sector_source"):
        frame[column] = frame[column].astype("string").str.strip()
        if frame[column].isna().any() or frame[column].eq("").any():
            raise IndustryContractError(f"advisor sector map contains blank {column}")
    return frame[
        ["ticker", "corp_code", "security_id", "advisor_sector", "advisor_sector_source"]
    ].reset_index(drop=True)


def build_industry_mapping_qa(
    securities: pd.DataFrame | Sequence[Mapping[str, Any]],
    official_reference: pd.DataFrame | Sequence[Mapping[str, Any]],
    advisor_sector_map: pd.DataFrame | Sequence[Mapping[str, Any]],
    *,
    official_industry_source: str,
    taxonomy_namespace: str,
    taxonomy_version: str,
    issuer_primary_business_overrides: pd.DataFrame | Sequence[Mapping[str, Any]] | None = None,
) -> pd.DataFrame:
    """Join industry data only by exact identifiers and audit rejected source rows."""

    registry = canonicalize_security_registry(securities)
    official = canonicalize_official_industry_reference(
        official_reference,
        official_industry_source=official_industry_source,
        taxonomy_namespace=taxonomy_namespace,
        taxonomy_version=taxonomy_version,
    )
    mapping = registry.merge(official, on="ticker", how="left", validate="one_to_one")
    mapping["exact_join_key"] = "ticker"
    mapping["identity_registry_status"] = "PASS_ONE_TO_ONE_TICKER_CORP_CODE_SECURITY_ID"
    mapping["official_join_status"] = "PASS_EXACT_TICKER"
    missing_official = mapping["official_industry_code"].isna() | mapping["official_industry_name"].isna()
    mapping.loc[missing_official, "official_join_status"] = "OFFICIAL_INDUSTRY_MISSING"
    mapping["rejected_original_official_industry_code"] = pd.NA
    mapping["rejected_original_official_industry_name"] = pd.NA
    mapping["rejected_original_official_industry_source"] = pd.NA
    mapping["rejected_original_taxonomy_namespace"] = pd.NA
    mapping["rejected_original_taxonomy_version"] = pd.NA
    mapping["issuer_override_applied"] = False
    mapping["issuer_override_reason"] = pd.NA

    overrides = _canonical_override_table(issuer_primary_business_overrides)
    override_by_key = {
        (row["ticker"], row["corp_code"], row["security_id"]): row
        for row in overrides.to_dict(orient="records")
    }
    for index, row in mapping.iterrows():
        source_invalid = _known_invalid(row["ticker"], row["official_industry_name"])
        if not source_invalid:
            continue
        key = (row["ticker"], row["corp_code"], row["security_id"])
        override = override_by_key.get(key)
        if override is None:
            mapping.loc[index, "official_join_status"] = "KNOWN_INVALID_MAPPING"
            continue
        mapping.loc[index, "rejected_original_official_industry_code"] = row[
            "official_industry_code"
        ]
        mapping.loc[index, "rejected_original_official_industry_name"] = row[
            "official_industry_name"
        ]
        mapping.loc[index, "rejected_original_official_industry_source"] = row[
            "official_industry_source"
        ]
        mapping.loc[index, "rejected_original_taxonomy_namespace"] = row["taxonomy_namespace"]
        mapping.loc[index, "rejected_original_taxonomy_version"] = row["taxonomy_version"]
        for column in (
            "official_industry_code",
            "official_industry_name",
            "official_industry_source",
            "taxonomy_namespace",
            "taxonomy_version",
        ):
            mapping.loc[index, column] = override[column]
        mapping.loc[index, "issuer_override_applied"] = True
        mapping.loc[index, "issuer_override_reason"] = override["override_reason"]
        mapping.loc[index, "official_join_status"] = "PASS_EXACT_ISSUER_PRIMARY_BUSINESS_OVERRIDE"
        mapping.loc[index, "exact_join_key"] = "ticker+corp_code+security_id"

    advisors = _canonical_advisor_sector_map(advisor_sector_map)
    mapping = mapping.merge(
        advisors,
        on=["ticker", "corp_code", "security_id"],
        how="left",
        validate="one_to_one",
    )
    mapping["advisor_sector_join_status"] = "PASS_EXACT_TICKER_CORP_SECURITY_ID"
    mapping.loc[mapping["advisor_sector"].isna(), "advisor_sector_join_status"] = (
        "ADVISOR_SECTOR_MISSING"
    )
    mapping["known_invalid_mapping"] = [
        _known_invalid(ticker, industry_name)
        for ticker, industry_name in zip(mapping["ticker"], mapping["official_industry_name"])
    ]
    mapping["qa_status"] = "PASS"
    failure = (
        ~mapping["official_join_status"].astype("string").str.startswith("PASS", na=False)
        | ~mapping["advisor_sector_join_status"].astype("string").str.startswith("PASS", na=False)
        | mapping["known_invalid_mapping"].astype(bool)
    )
    mapping.loc[failure, "qa_status"] = "FAIL"
    return mapping.reset_index(drop=True)


def build_sector_exposure(
    positions: pd.DataFrame | Sequence[Mapping[str, Any]],
    industry_mapping: pd.DataFrame | Sequence[Mapping[str, Any]],
    *,
    weight_column: str,
    value_column: str | None,
    weight_basis: str,
    expected_equity_weight: float | None = None,
    tolerance: float = 1e-12,
) -> pd.DataFrame:
    """Aggregate equity exposure by advisor sector and reconcile its weight."""

    position_frame = pd.DataFrame(positions).copy()
    if "ticker" not in position_frame.columns or weight_column not in position_frame.columns:
        raise IndustryContractError(f"positions require ticker and {weight_column}")
    if "asset_class" in position_frame.columns:
        position_frame = position_frame.loc[
            position_frame["asset_class"].astype("string").eq("EQUITY")
        ].copy()
    position_frame["ticker"] = normalize_security_id_series(position_frame["ticker"])
    if position_frame["ticker"].isna().any() or position_frame["ticker"].duplicated().any():
        raise IndustryContractError("equity positions require unique valid tickers")
    position_frame[weight_column] = pd.to_numeric(position_frame[weight_column], errors="coerce")
    if position_frame[weight_column].isna().any():
        raise IndustryContractError(f"positions contain missing {weight_column}")
    if value_column is not None:
        if value_column not in position_frame.columns:
            raise IndustryContractError(f"positions missing {value_column}")
        position_frame[value_column] = pd.to_numeric(position_frame[value_column], errors="coerce")
        if position_frame[value_column].isna().any():
            raise IndustryContractError(f"positions contain missing {value_column}")

    mapping = pd.DataFrame(industry_mapping).copy()
    required_mapping = {"ticker", "advisor_sector", "advisor_sector_source", "qa_status"}
    missing_mapping = sorted(required_mapping - set(mapping.columns))
    if missing_mapping:
        raise IndustryContractError(f"industry mapping missing columns: {missing_mapping}")
    mapping["ticker"] = normalize_security_id_series(mapping["ticker"])
    if mapping["ticker"].duplicated().any():
        raise IndustryContractError("industry mapping contains duplicate tickers")
    joined = position_frame.merge(
        mapping[["ticker", "advisor_sector", "advisor_sector_source", "qa_status"]],
        on="ticker",
        how="left",
        validate="one_to_one",
    )
    invalid = (
        joined["advisor_sector"].isna()
        | joined["advisor_sector_source"].isna()
        | joined["qa_status"].ne("PASS")
    )
    if invalid.any():
        tickers = joined.loc[invalid, "ticker"].tolist()
        raise IndustryContractError(f"sector exposure has invalid industry mappings: {tickers}")

    aggregations: dict[str, tuple[str, str]] = {
        "position_count": ("ticker", "count"),
        "sector_weight": (weight_column, "sum"),
    }
    if value_column is not None:
        aggregations["sector_value"] = (value_column, "sum")
    exposure = (
        joined.groupby(["advisor_sector", "advisor_sector_source"], dropna=False)
        .agg(**aggregations)
        .reset_index()
        .sort_values(["sector_weight", "advisor_sector"], ascending=[False, True], kind="mergesort")
        .reset_index(drop=True)
    )
    if "sector_value" not in exposure.columns:
        exposure["sector_value"] = pd.NA
    observed = float(exposure["sector_weight"].sum())
    expected = (
        float(expected_equity_weight)
        if expected_equity_weight is not None
        else float(position_frame[weight_column].sum())
    )
    difference = observed - expected
    reconciliation = "PASS" if math.isclose(observed, expected, rel_tol=0.0, abs_tol=tolerance) else "FAIL"
    exposure["weight_basis"] = str(weight_basis)
    exposure["equity_weight_expected"] = expected
    exposure["sector_weight_total"] = observed
    exposure["sector_weight_reconciliation_diff"] = difference
    exposure["reconciliation_status"] = reconciliation
    if reconciliation != "PASS":
        raise IndustryContractError(
            f"advisor-sector weight reconciliation failed: observed={observed} expected={expected}"
        )
    return exposure[
        [
            "advisor_sector",
            "advisor_sector_source",
            "position_count",
            "sector_value",
            "sector_weight",
            "weight_basis",
            "equity_weight_expected",
            "sector_weight_total",
            "sector_weight_reconciliation_diff",
            "reconciliation_status",
        ]
    ]


def build_industry_artifacts(
    securities: pd.DataFrame | Sequence[Mapping[str, Any]],
    official_reference: pd.DataFrame | Sequence[Mapping[str, Any]],
    advisor_sector_map: pd.DataFrame | Sequence[Mapping[str, Any]],
    *,
    official_industry_source: str,
    taxonomy_namespace: str,
    taxonomy_version: str,
    current_positions: pd.DataFrame | Sequence[Mapping[str, Any]],
    target_positions: pd.DataFrame | Sequence[Mapping[str, Any]],
    current_weight_column: str = "current_weight",
    current_value_column: str = "current_value",
    target_weight_column: str = "target_weight",
    target_value_column: str = "target_value",
    target_equity_weight: float = 0.90,
    issuer_primary_business_overrides: pd.DataFrame | Sequence[Mapping[str, Any]] | None = None,
) -> IndustryArtifacts:
    mapping = build_industry_mapping_qa(
        securities,
        official_reference,
        advisor_sector_map,
        official_industry_source=official_industry_source,
        taxonomy_namespace=taxonomy_namespace,
        taxonomy_version=taxonomy_version,
        issuer_primary_business_overrides=issuer_primary_business_overrides,
    )
    if mapping["qa_status"].ne("PASS").any():
        failures = mapping.loc[mapping["qa_status"].ne("PASS"), "ticker"].tolist()
        raise IndustryContractError(f"industry mapping QA failed: {failures}")
    current = build_sector_exposure(
        current_positions,
        mapping,
        weight_column=current_weight_column,
        value_column=current_value_column,
        weight_basis="BROKER_ACCOUNT_ASOF_EQUITY_WEIGHT",
    )
    target = build_sector_exposure(
        target_positions,
        mapping,
        weight_column=target_weight_column,
        value_column=target_value_column,
        weight_basis="ADVISOR_TARGET_EQUITY_WEIGHT",
        expected_equity_weight=target_equity_weight,
    )
    qa = {
        "contract_version": INDUSTRY_CONTRACT_VERSION,
        "status": "PASS",
        "security_count": int(len(mapping)),
        "industry_source_complete": bool(mapping["official_industry_source"].notna().all()),
        "advisor_sector_complete": bool(mapping["advisor_sector"].notna().all()),
        "ticker_corp_security_id_one_to_one": True,
        "known_invalid_322000_mapping_absent": bool(
            not mapping.loc[mapping["ticker"].eq(KNOWN_INVALID_TICKER), "known_invalid_mapping"].any()
        ),
        "issuer_primary_business_override_count": int(mapping["issuer_override_applied"].sum()),
        "target_sector_weight_total": float(target["sector_weight"].sum()),
        "target_equity_weight_expected": float(target_equity_weight),
        "target_sector_weight_reconciliation": "PASS",
    }
    return IndustryArtifacts(
        industry_mapping_qa=mapping,
        sector_exposure_current=current,
        sector_exposure_target=target,
        qa=qa,
    )


def _write_new_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        handle.write(text)


def write_industry_artifacts(artifacts: IndustryArtifacts, output_dir: str | Path) -> dict[str, Path]:
    root = Path(output_dir)
    paths = {
        "industry_mapping_qa": root / "industry_mapping_qa.csv",
        "sector_exposure_current": root / "sector_exposure_current.csv",
        "sector_exposure_target": root / "sector_exposure_target.csv",
        "industry_sector_qa": root / "industry_sector_qa.json",
    }
    _write_new_text(paths["industry_mapping_qa"], artifacts.industry_mapping_qa.to_csv(index=False))
    _write_new_text(
        paths["sector_exposure_current"], artifacts.sector_exposure_current.to_csv(index=False)
    )
    _write_new_text(
        paths["sector_exposure_target"], artifacts.sector_exposure_target.to_csv(index=False)
    )
    _write_new_text(
        paths["industry_sector_qa"],
        json.dumps(artifacts.qa, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
    )
    return paths


__all__ = [
    "INDUSTRY_CONTRACT_VERSION",
    "IndustryArtifacts",
    "IndustryContractError",
    "KNOWN_INVALID_INDUSTRY_TERMS",
    "KNOWN_INVALID_TICKER",
    "build_industry_artifacts",
    "build_advisor_sector_map_from_exact_ticker_policy",
    "build_industry_mapping_qa",
    "build_security_registry_from_exact_sources",
    "build_sector_exposure",
    "canonicalize_official_industry_reference",
    "canonicalize_security_registry",
    "write_industry_artifacts",
]
