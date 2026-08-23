#!/usr/bin/env python
"""Certified performance-cost-basis and contribution contracts.

This module deliberately does not infer a performance start date from a file
name.  Production inputs are selected in this fixed order:

    fill ledger > hash-bound user-confirmed broker-statement image bundle
    > post-trade snapshot > certified manifest > fail

The image-bundle tier never claims ``DIRECT_BROKER_EXPORT`` lineage.  It binds
the reviewed JPEGs, exact user attestation, full transcription, settlement
reconciliation, signed cash, and an exact-close derived snapshot.  If later
account activity is unknown, it remains a conditional shadow basis with
``production_ready=false``.  An invalid higher-priority input always fails
instead of quietly falling back to a lower-priority source.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from scripts.common.security_id import normalize_security_id_series


CONTRACT_VERSION = "performance-cost-basis/v1"
USER_CONFIRMED_STATEMENT_CONTRACT_VERSION = "user-confirmed-broker-statement/v1"
USER_CONFIRMED_STATEMENT_SOURCE_TYPE = "USER_CONFIRMED_BROKER_STATEMENT_IMAGES"
USER_CONFIRMED_STATEMENT_METHOD = "IMAGE_HASH_PLUS_EXPLICIT_USER_DATE_ATTESTATION"
USER_CONFIRMED_ATTESTATION_TEXT = "4월 1일에 매매한 것으로 하자."
UNKNOWN_BLANK_COST_POLICY = "UNKNOWN_NOT_ZERO"
STATIC_METHOD = "STATIC_POST_TRADE_SNAPSHOT"
TRANSACTION_METHOD = "TRANSACTION_AWARE_FILL_LEDGER"
RECONCILIATION_ATOL = 1e-10
RECONCILIATION_RTOL = 1e-10
ZERO_EXTERNAL_FLOW_POLICY = "VERIFIED_ZERO_EXTERNAL_CASH_FLOWS"
COMPLETE_TRANSACTION_HISTORY_POLICY = "CERTIFIED_COMPLETE_TRANSACTION_HISTORY"
BROKER_EXPORT_LINEAGE = "DIRECT_BROKER_EXPORT"
BROKER_ISSUER_TYPE = "REGULATED_BROKER"
BROKER_ISSUER_ASSURANCE = "BROKER_EXPORT_ISSUER_ATTESTED"
BROKER_ASSURANCE_METHOD = "DETACHED_BROKER_EXPORT_HASH_BINDING"
TRADING_COST_COLUMNS_POLICY = "BROKER_EXPORT_COST_COLUMNS"
EXPLICIT_ZERO_TRADING_COST_POLICY = "BROKER_EXPORT_EXPLICIT_ZERO"
SIGNED_SETTLEMENT_CASH_BASIS = "SIGNED_BROKER_SETTLEMENT_BALANCE"
BROKER_STATEMENT_SETTLEMENT_RECONCILIATION_POLICY = (
    "BROKER_STATEMENT_SETTLEMENT_RECONCILIATION"
)
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_RAW_ID_KEYS = frozenset(
    {
        "account",
        "account_id",
        "account_no",
        "account_number",
        "account_reference",
        "account_holder",
        "account_holder_name",
        "broker_account",
        "broker_account_id",
        "customer_id",
        "login_id",
        "user_id",
        "user_name",
        "username",
        "krx_id",
        "krx_pw",
        "password",
        "passwd",
        "api_key",
        "api_secret",
        "access_token",
    }
)
_TRADING_COST_COLUMNS = (
    "transaction_cost",
    "fee",
    "tax",
    "other_cost",
)
CANONICAL_CONTRIBUTION_COLUMNS = [
    "ticker",
    "name",
    "shares_at_start",
    "start_date",
    "start_price",
    "start_position_value",
    "end_date",
    "end_price",
    "end_position_value",
    "dividends",
    "net_intermediate_trade_cashflow",
    "trading_cost_allocated",
    "period_pnl",
    "position_period_return",
    "contribution_to_total_return",
    "weight_at_start_nav",
    "weight_at_end_nav",
    "performance_basis",
]
EXECUTION_WINDOW_ATTRIBUTION_COLUMNS = [
    "ticker",
    "name",
    "first_fill_date",
    "last_fill_date",
    "valuation_date",
    "fill_count",
    "buy_qty",
    "sell_qty",
    "gross_buy_value",
    "gross_sell_value",
    "net_execution_cashflow",
    "trading_cost",
    "gross_mark_to_last_fill_close_pnl",
    "net_mark_to_last_fill_close_pnl",
    "attribution_basis",
]


@dataclass
class PerformanceCostBasis:
    source_type: str
    calculation_method: str
    source_path: Path
    source_sha256: str
    basis_date: pd.Timestamp
    production_ready: bool
    frame: pd.DataFrame
    opening_nav: Optional[float] = None
    opening_cash: Optional[float] = None
    post_fill_cash: Optional[float] = None
    certification_path: Optional[Path] = None
    certification_sha256: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


@dataclass
class BrokerStatementExecutionEvidence:
    """Execution-date evidence that is deliberately not a performance basis.

    A user-supplied broker statement screenshot can prove when trades occurred
    when its immutable image hashes, a reviewed transcription, and an explicit
    user date attestation all agree.  Screenshots do not, by themselves, prove
    opening NAV/cash or complete per-fill trading costs, so this object can
    never be passed off as a production-ready ``PerformanceCostBasis``.
    """

    source_type: str
    calculation_method: str
    ledger_path: Path
    ledger_sha256: str
    manifest_path: Path
    manifest_sha256: str
    execution_date: pd.Timestamp
    first_fill_date: pd.Timestamp
    last_fill_date: pd.Timestamp
    source_image_sha256s: tuple[str, ...]
    frame: pd.DataFrame
    actual_execution_proven: bool = True
    production_ready: bool = False
    trading_costs_complete: bool = False
    metadata: Optional[dict[str, Any]] = None


def sha256_file(path: str | Path) -> str:
    source = Path(path)
    digest = hashlib.sha256()
    with source.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_table(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise FileNotFoundError(f"performance source not found: {path}")
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path, dtype={"ticker": "string"}, encoding="utf-8-sig")


def _one_iso_date(value: Any, field: str) -> pd.Timestamp:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"{field} must be an explicit valid date")
    parsed = pd.Timestamp(parsed).normalize()
    if str(value).strip()[:10] != parsed.strftime("%Y-%m-%d"):
        raise ValueError(f"{field} must use ISO YYYY-MM-DD format: {value!r}")
    return parsed


def _finite_nonnegative(value: Any, field: str) -> float:
    parsed = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(parsed) or not np.isfinite(float(parsed)) or float(parsed) < 0:
        raise ValueError(f"{field} must be a finite non-negative number")
    return float(parsed)


def _finite_certified_cash(
    value: Any,
    field: str,
    metadata: dict[str, Any],
) -> float:
    """Parse certified cash, permitting a broker-disclosed settlement liability.

    Negative cash is never inferred from holdings or used as a generic leverage
    switch.  It is accepted only when the detached certification explicitly
    binds the value to the broker's signed settlement-balance field.  The
    opening NAV reconciliation remains mandatory downstream.
    """

    parsed = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(parsed) or not np.isfinite(float(parsed)):
        raise ValueError(f"{field} must be a finite number")
    cash = float(parsed)
    if cash < 0:
        basis = str(metadata.get("opening_cash_basis", "")).strip().upper()
        if basis != SIGNED_SETTLEMENT_CASH_BASIS:
            raise ValueError(
                f"negative {field} requires opening_cash_basis="
                f"{SIGNED_SETTLEMENT_CASH_BASIS!r}"
            )
        if metadata.get("signed_settlement_balance_verified") is not True:
            raise ValueError(
                f"negative {field} requires signed_settlement_balance_verified=true"
            )
    return cash


def _finite_number(value: Any, field: str) -> float:
    parsed = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    if pd.isna(parsed) or not np.isfinite(float(parsed)):
        raise ValueError(f"{field} must be a finite number")
    return float(parsed)


def _load_certification(path: Path, explicit_path: str | Path | None) -> tuple[Path, dict[str, Any]]:
    if explicit_path is not None:
        candidates = [Path(explicit_path)]
    else:
        candidates = [
            path.with_suffix(path.suffix + ".meta.json"),
            path.with_suffix(".meta.json"),
        ]
    meta_path = next((candidate for candidate in candidates if candidate.exists()), None)
    if meta_path is None:
        raise ValueError(
            "certified source requires a sidecar containing an explicit date and source SHA-256; "
            f"checked={[str(candidate) for candidate in candidates]}"
        )
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid certification sidecar: {meta_path}") from exc
    if not isinstance(metadata, dict):
        raise ValueError("certification sidecar must contain one JSON object")
    return meta_path, metadata


def _normalized_metadata_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _reject_raw_sensitive_identifiers(payload: Any) -> None:
    """Reject raw account/login secrets while allowing SHA-256 fingerprints.

    Certification metadata can be copied into diagnostics by downstream code.
    Therefore an account identifier must never be carried in the contract at
    all; only ``account_fingerprint_sha256`` is accepted.
    """

    if isinstance(payload, dict):
        for key, value in payload.items():
            normalized = _normalized_metadata_key(key)
            if normalized in _FORBIDDEN_RAW_ID_KEYS:
                raise ValueError(
                    f"raw sensitive identifier field is forbidden in certification metadata: {normalized}"
                )
            _reject_raw_sensitive_identifiers(value)
    elif isinstance(payload, list):
        for value in payload:
            _reject_raw_sensitive_identifiers(value)


def _required_sha256_fingerprint(metadata: dict[str, Any], field: str) -> str:
    value = str(metadata.get(field, "")).strip().lower()
    if not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{field} must be a SHA-256 fingerprint")
    return value


def _validate_broker_export_assurance(
    metadata: dict[str, Any],
    *,
    source_sha256: str,
) -> dict[str, str]:
    """Require broker-export lineage rather than trusting a bare sidecar.

    This contract does not expose an account number or user identifier.  The
    broker, account and detached issuer attestation are represented only by
    SHA-256 fingerprints, and the declared broker export must be hash-bound to
    the exact source artifact supplied to the loader.
    """

    _reject_raw_sensitive_identifiers(metadata)
    required_values = {
        "broker_export_lineage": BROKER_EXPORT_LINEAGE,
        "issuer_type": BROKER_ISSUER_TYPE,
        "issuer_assurance": BROKER_ISSUER_ASSURANCE,
        "assurance_method": BROKER_ASSURANCE_METHOD,
    }
    for field, expected in required_values.items():
        if str(metadata.get(field, "")).strip().upper() != expected:
            raise ValueError(f"{field} must equal {expected!r}")

    export_fingerprint = _required_sha256_fingerprint(metadata, "broker_export_sha256")
    issuer_fingerprint = _required_sha256_fingerprint(
        metadata, "issuer_fingerprint_sha256"
    )
    attestation_fingerprint = _required_sha256_fingerprint(
        metadata, "issuer_attestation_sha256"
    )
    account_fingerprint = _required_sha256_fingerprint(
        metadata, "account_fingerprint_sha256"
    )
    if export_fingerprint != source_sha256:
        raise ValueError("broker_export_sha256 does not match the supplied source artifact")
    if len({issuer_fingerprint, attestation_fingerprint, account_fingerprint}) != 3:
        raise ValueError(
            "issuer, attestation, and account SHA-256 fingerprints must be distinct"
        )
    return {
        "broker_export_sha256": export_fingerprint,
        "issuer_fingerprint_sha256": issuer_fingerprint,
        "issuer_attestation_sha256": attestation_fingerprint,
        "account_fingerprint_sha256": account_fingerprint,
    }


def _validate_trading_cost_completeness(
    frame: pd.DataFrame,
    metadata: dict[str, Any],
    *,
    context: str,
) -> None:
    """Fail closed when a fill export contains no explicit cost evidence."""

    if metadata.get("trading_costs_complete") is not True:
        raise ValueError(f"{context} certification trading_costs_complete must be true")
    present = [column for column in _TRADING_COST_COLUMNS if column in frame.columns]
    declared_basis = str(metadata.get("trading_cost_verification_basis", "")).strip().upper()
    if not present:
        if metadata.get("explicit_zero_trading_costs_verified") is not True:
            raise ValueError(
                f"{context} has no trading-cost columns; "
                "explicit_zero_trading_costs_verified=true is required"
            )
        if declared_basis != EXPLICIT_ZERO_TRADING_COST_POLICY:
            raise ValueError(
                f"{context} without cost columns requires "
                f"trading_cost_verification_basis={EXPLICIT_ZERO_TRADING_COST_POLICY!r}"
            )
        return

    if declared_basis != TRADING_COST_COLUMNS_POLICY:
        raise ValueError(
            f"{context} with cost columns requires "
            f"trading_cost_verification_basis={TRADING_COST_COLUMNS_POLICY!r}"
        )
    if metadata.get("explicit_zero_trading_costs_verified") is True:
        for column in present:
            values = pd.to_numeric(frame[column], errors="coerce")
            if values.isna().any() or (~np.isfinite(values.astype(float))).any():
                raise ValueError(f"{context} {column} contains invalid cost values")
            if values.astype(float).ne(0.0).any():
                raise ValueError(
                    f"{context} declares explicit zero trading costs but {column} is nonzero"
                )


def _validate_certification(
    source_path: Path,
    meta_path: Path,
    metadata: dict[str, Any],
    *,
    expected_source_type: str,
) -> str:
    _reject_raw_sensitive_identifiers(metadata)
    if metadata.get("contract_version") != CONTRACT_VERSION:
        raise ValueError(
            f"certification contract_version must equal {CONTRACT_VERSION!r}"
        )
    if str(metadata.get("source_type", "")).upper() != expected_source_type:
        raise ValueError(
            f"certification source_type must equal {expected_source_type!r}"
        )
    if metadata.get("production_ready") is not True:
        raise ValueError("certification production_ready must be true")
    if metadata.get("actual_execution_proven") is not True:
        raise ValueError("certification actual_execution_proven must be true")
    if str(metadata.get("status", "")).upper() not in {"PASS", "CERTIFIED"}:
        raise ValueError("certification status must be PASS or CERTIFIED")
    _validate_cash_flow_and_transaction_history_declaration(
        metadata,
        expected_source_type=expected_source_type,
    )
    expected_sha = str(metadata.get("source_sha256", "")).strip().lower()
    if len(expected_sha) != 64:
        raise ValueError("certification source_sha256 is required")
    actual_sha = sha256_file(source_path)
    if actual_sha != expected_sha:
        raise ValueError(
            f"certified source SHA-256 mismatch: expected={expected_sha} actual={actual_sha}"
        )
    _validate_broker_export_assurance(metadata, source_sha256=actual_sha)
    declared_path = metadata.get("source_path")
    if declared_path:
        declared = Path(str(declared_path))
        if not declared.is_absolute():
            declared = (meta_path.parent / declared).resolve()
        if declared.resolve() != source_path.resolve():
            raise ValueError(
                "certification source_path does not identify the supplied source: "
                f"declared={declared} supplied={source_path.resolve()}"
            )
    return actual_sha


def _validate_cash_flow_and_transaction_history_declaration(
    metadata: dict[str, Any],
    *,
    expected_source_type: str,
) -> None:
    """Require evidence for assumptions that would otherwise change returns.

    Missing activity fields are never interpreted as zero activity.  The
    current engine supports only verified zero external cash flows.  A static
    snapshot additionally needs a complete transaction-history search that
    explicitly found no intermediate trades.
    """

    if metadata.get("external_cash_flows_verified") is not True:
        raise ValueError("external_cash_flows_verified must be true")
    external_cash_flows_raw = pd.to_numeric(
        pd.Series([metadata.get("external_cash_flows")]), errors="coerce"
    ).iloc[0]
    if pd.isna(external_cash_flows_raw) or not np.isfinite(float(external_cash_flows_raw)):
        raise ValueError("external_cash_flows must be a finite number")
    external_cash_flows = float(external_cash_flows_raw)
    if external_cash_flows != 0.0:
        raise ValueError(
            "nonzero external cash flows require cash-flow-aware TWR; "
            "only verified zero external cash flows are currently supported"
        )
    if metadata.get("transaction_history_complete") is not True:
        raise ValueError("transaction_history_complete must be true")
    _one_iso_date(
        metadata.get("transaction_history_through_date"),
        "transaction_history_through_date",
    )
    if expected_source_type == "POST_TRADE_SNAPSHOT" and metadata.get(
        "intermediate_trades_verified_none"
    ) is not True:
        raise ValueError(
            "snapshot performance requires intermediate_trades_verified_none=true; "
            "otherwise use a complete transaction ledger"
        )


def validate_certified_interval_contract(
    basis: PerformanceCostBasis,
    end_date: str | pd.Timestamp,
) -> dict[str, Any]:
    """Validate that certified account activity spans the evaluation window."""

    metadata = basis.metadata or {}
    embedded_source_type = str(
        metadata.get("embedded_source_type") or basis.source_type
    ).upper()
    _validate_cash_flow_and_transaction_history_declaration(
        metadata,
        expected_source_type=embedded_source_type,
    )
    through = _one_iso_date(
        metadata.get("transaction_history_through_date"),
        "transaction_history_through_date",
    )
    end = pd.Timestamp(end_date).normalize()
    if through < end:
        raise ValueError(
            "certified transaction history does not cover the full performance interval: "
            f"through={through.date()} end={end.date()}"
        )
    return {
        "external_cash_flows": 0.0,
        "external_cash_flow_treatment": ZERO_EXTERNAL_FLOW_POLICY,
        "external_cash_flows_verified": True,
        "transaction_history_complete": True,
        "transaction_history_through_date": str(through.date()),
        "transaction_history_policy": COMPLETE_TRANSACTION_HISTORY_POLICY,
        "intermediate_trades_verified_none": (
            metadata.get("intermediate_trades_verified_none") is True
            if embedded_source_type == "POST_TRADE_SNAPSHOT"
            else False
        ),
    }


def _normalize_snapshot(frame: pd.DataFrame, snapshot_date: pd.Timestamp) -> pd.DataFrame:
    required = {"ticker", "shares"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"post-trade snapshot missing columns: {missing}")
    out = frame.copy()
    out["ticker"] = normalize_security_id_series(out["ticker"])
    if out["ticker"].duplicated().any():
        duplicates = out.loc[out["ticker"].duplicated(False), "ticker"].astype(str).tolist()
        raise ValueError(f"post-trade snapshot contains duplicate tickers: {duplicates}")
    shares = pd.to_numeric(out["shares"], errors="coerce")
    if shares.isna().any() or (~np.isfinite(shares.astype(float))).any():
        raise ValueError("post-trade snapshot contains invalid shares")
    if shares.lt(0).any() or (shares % 1).ne(0).any():
        raise ValueError("post-trade snapshot shares must be non-negative integers")
    out["shares"] = shares.astype("int64")
    out = out.loc[out["shares"].gt(0)].copy()
    if out.empty:
        raise ValueError("post-trade snapshot has no positive holdings")
    if "snapshot_date" in out.columns:
        parsed = pd.to_datetime(out["snapshot_date"], errors="coerce").dt.normalize()
        if parsed.isna().any() or not parsed.eq(snapshot_date).all():
            raise ValueError("snapshot row dates disagree with certified snapshot_date")
    if "name" not in out.columns:
        out["name"] = pd.NA
    if "base_price" in out.columns:
        price = pd.to_numeric(out["base_price"], errors="coerce")
        if price.isna().any() or (~np.isfinite(price.astype(float))).any() or price.le(0).any():
            raise ValueError("snapshot base_price must be finite and positive when supplied")
        out["base_price"] = price.astype(float)
    return out.reset_index(drop=True)


def _normalize_fill_ledger(
    frame: pd.DataFrame,
    start_date: pd.Timestamp,
    *,
    certification_metadata: dict[str, Any],
) -> pd.DataFrame:
    _validate_trading_cost_completeness(
        frame,
        certification_metadata,
        context="fill ledger",
    )
    aliases = {
        "fill_date": ["fill_date", "trade_date", "date"],
        "side": ["side", "trade_side", "order_side"],
        "filled_qty": ["filled_qty", "trade_qty", "qty", "quantity"],
        "filled_price": ["filled_price", "fill_price", "price"],
    }
    rename: dict[str, str] = {}
    for canonical, candidates in aliases.items():
        match = next((column for column in candidates if column in frame.columns), None)
        if match is None:
            raise ValueError(f"fill ledger missing {canonical}; accepted={candidates}")
        rename[match] = canonical
    if "ticker" not in frame.columns:
        raise ValueError("fill ledger missing ticker")
    out = frame.rename(columns=rename).copy()
    out["ticker"] = normalize_security_id_series(out["ticker"])
    out["fill_date"] = pd.to_datetime(out["fill_date"], errors="coerce").dt.normalize()
    if out["fill_date"].isna().any():
        raise ValueError("fill ledger contains invalid fill_date")
    if out["fill_date"].lt(start_date).any():
        raise ValueError("fill ledger contains a transaction before certified performance_start_date")
    out["side"] = out["side"].astype(str).str.strip().str.upper()
    if not out["side"].isin({"OPEN", "BUY", "SELL"}).all():
        bad = sorted(out.loc[~out["side"].isin({"OPEN", "BUY", "SELL"}), "side"].unique())
        raise ValueError(f"fill ledger contains unsupported side values: {bad}")
    if out.loc[out["side"].eq("OPEN"), "fill_date"].ne(start_date).any():
        raise ValueError("OPEN rows are permitted only on certified performance_start_date")
    if out.loc[~out["side"].eq("OPEN"), "fill_date"].le(start_date).any():
        raise ValueError(
            "BUY/SELL rows must be strictly after performance_start_date; OPEN rows must encode "
            "the completed post-rebalance inventory so the first cumulative return is zero"
        )
    qty = pd.to_numeric(out["filled_qty"], errors="coerce")
    price = pd.to_numeric(out["filled_price"], errors="coerce")
    if qty.isna().any() or (~np.isfinite(qty.astype(float))).any() or qty.le(0).any() or (qty % 1).ne(0).any():
        raise ValueError("fill ledger filled_qty must be positive integers")
    if price.isna().any() or (~np.isfinite(price.astype(float))).any() or price.le(0).any():
        raise ValueError("fill ledger filled_price must be finite and positive")
    out["filled_qty"] = qty.astype("int64")
    out["filled_price"] = price.astype(float)

    component_columns = [
        column for column in ("fee", "tax", "other_cost")
        if column in out.columns
    ]
    components = pd.Series(0.0, index=out.index)
    for column in component_columns:
        values = pd.to_numeric(out[column], errors="coerce")
        if values.isna().any() or (~np.isfinite(values.astype(float))).any() or values.lt(0).any():
            raise ValueError(f"fill ledger {column} must be finite and non-negative")
        out[column] = values.astype(float)
        components = components + values
    if "transaction_cost" in out.columns:
        declared = pd.to_numeric(out["transaction_cost"], errors="coerce")
        if declared.isna().any() or (~np.isfinite(declared.astype(float))).any() or declared.lt(0).any():
            raise ValueError("fill ledger transaction_cost must be finite and non-negative")
        if component_columns and not np.allclose(
            declared.to_numpy(dtype=float), components.to_numpy(dtype=float), atol=1e-8, rtol=1e-12
        ):
            raise ValueError("transaction_cost disagrees with fee/tax/other components")
        out["transaction_cost"] = declared.astype(float)
    else:
        out["transaction_cost"] = components.astype(float)
    if out.loc[out["side"].eq("OPEN"), "transaction_cost"].gt(0).any():
        raise ValueError("OPEN inventory rows cannot contain transaction costs")

    if "fill_id" in out.columns:
        ids = out["fill_id"].astype("string").str.strip()
        if ids.isna().any() or ids.eq("").any() or ids.duplicated().any():
            raise ValueError("fill ledger fill_id must be present and unique")
        out["fill_id"] = ids
    else:
        duplicate_fields = [
            "ticker", "fill_date", "side", "filled_qty", "filled_price", "transaction_cost"
        ]
        if out.duplicated(duplicate_fields).any():
            raise ValueError("fill ledger contains duplicate fills without unique fill_id")
        out["fill_id"] = pd.Series(
            [f"ROW-{index + 1:06d}" for index in range(len(out))], dtype="string"
        )
    if "name" not in out.columns:
        out["name"] = pd.NA
    out["_source_order"] = np.arange(len(out), dtype="int64")
    out["_side_order"] = out["side"].map({"OPEN": 0, "BUY": 1, "SELL": 1}).astype("int64")
    sort_columns = ["fill_date", "_side_order"]
    if "fill_timestamp" in out.columns:
        timestamp = pd.to_datetime(out["fill_timestamp"], errors="coerce")
        if timestamp.isna().any():
            raise ValueError("fill ledger contains invalid fill_timestamp")
        out["fill_timestamp"] = timestamp
        sort_columns.append("fill_timestamp")
    sort_columns.append("_source_order")
    return out.sort_values(sort_columns, kind="stable").reset_index(drop=True)


def load_certified_fill_ledger(
    path: str | Path,
    certification_path: str | Path | None = None,
) -> PerformanceCostBasis:
    source = Path(path).resolve()
    meta_path, metadata = _load_certification(source, certification_path)
    source_sha = _validate_certification(
        source, meta_path, metadata, expected_source_type="FILL_LEDGER"
    )
    start_date = _one_iso_date(metadata.get("performance_start_date"), "performance_start_date")
    opening_nav = _finite_nonnegative(metadata.get("opening_nav"), "opening_nav")
    opening_cash = _finite_certified_cash(
        metadata.get("opening_cash"), "opening_cash", metadata
    )
    post_fill_cash = _finite_certified_cash(
        metadata.get("post_fill_cash"), "post_fill_cash", metadata
    )
    if opening_nav <= 0:
        raise ValueError("opening_nav must be positive")
    ledger = _normalize_fill_ledger(
        _read_table(source),
        start_date,
        certification_metadata=metadata,
    )
    return PerformanceCostBasis(
        source_type="FILL_LEDGER",
        calculation_method=TRANSACTION_METHOD,
        source_path=source,
        source_sha256=source_sha,
        basis_date=start_date,
        production_ready=True,
        frame=ledger,
        opening_nav=opening_nav,
        opening_cash=opening_cash,
        post_fill_cash=post_fill_cash,
        certification_path=meta_path.resolve(),
        certification_sha256=sha256_file(meta_path),
        metadata=metadata,
    )


def _normalize_execution_fill_ledger(
    frame: pd.DataFrame,
    *,
    certification_metadata: dict[str, Any],
) -> pd.DataFrame:
    """Normalize execution evidence without treating it as an opening-NAV ledger.

    A forensic rebalance ledger contains the BUY/SELL fills *through* the
    performance start date.  It is intentionally different from the
    transaction-aware performance ledger above, whose BUY/SELL rows must occur
    after an already-certified opening date.  Keeping the two parsers separate
    prevents execution-window fills from leaking into post-rebalance NAV.
    """

    _validate_trading_cost_completeness(
        frame,
        certification_metadata,
        context="execution fill ledger",
    )

    aliases = {
        "fill_date": ["fill_date", "execution_date", "trade_date", "date"],
        "side": ["side", "trade_side", "order_side"],
        "filled_qty": ["filled_qty", "actual_qty", "trade_qty", "qty", "quantity"],
        "filled_price": ["filled_price", "fill_price", "execution_price", "price"],
    }
    rename: dict[str, str] = {}
    for canonical, candidates in aliases.items():
        match = next((column for column in candidates if column in frame.columns), None)
        if match is None:
            raise ValueError(
                f"execution fill ledger missing {canonical}; accepted={candidates}"
            )
        rename[match] = canonical
    if "ticker" not in frame.columns:
        raise ValueError("execution fill ledger missing ticker")

    out = frame.rename(columns=rename).copy()
    if out.empty:
        raise ValueError("execution fill ledger cannot be empty")
    out["ticker"] = normalize_security_id_series(out["ticker"])
    out["fill_date"] = pd.to_datetime(out["fill_date"], errors="coerce").dt.normalize()
    if out[["ticker", "fill_date"]].isna().any(axis=None):
        raise ValueError("execution fill ledger contains invalid ticker or fill_date")
    out["side"] = out["side"].astype(str).str.strip().str.upper()
    if not out["side"].isin({"BUY", "SELL"}).all():
        bad = sorted(out.loc[~out["side"].isin({"BUY", "SELL"}), "side"].unique())
        raise ValueError(
            f"execution fill ledger permits only executed BUY/SELL rows: {bad}"
        )

    qty = pd.to_numeric(out["filled_qty"], errors="coerce")
    price = pd.to_numeric(out["filled_price"], errors="coerce")
    if (
        qty.isna().any()
        or (~np.isfinite(qty.astype(float))).any()
        or qty.le(0).any()
        or (qty % 1).ne(0).any()
    ):
        raise ValueError("execution fill ledger filled_qty must be positive integers")
    if price.isna().any() or (~np.isfinite(price.astype(float))).any() or price.le(0).any():
        raise ValueError("execution fill ledger filled_price must be finite and positive")
    out["filled_qty"] = qty.astype("int64")
    out["filled_price"] = price.astype(float)

    component_columns = [
        column
        for column in ("fee", "tax", "other_cost")
        if column in out.columns
    ]
    components = pd.Series(0.0, index=out.index)
    for column in component_columns:
        values = pd.to_numeric(out[column], errors="coerce")
        if (
            values.isna().any()
            or (~np.isfinite(values.astype(float))).any()
            or values.lt(0).any()
        ):
            raise ValueError(f"execution fill ledger {column} must be finite and non-negative")
        out[column] = values.astype(float)
        components = components + values
    if "transaction_cost" in out.columns:
        declared = pd.to_numeric(out["transaction_cost"], errors="coerce")
        if (
            declared.isna().any()
            or (~np.isfinite(declared.astype(float))).any()
            or declared.lt(0).any()
        ):
            raise ValueError(
                "execution fill ledger transaction_cost must be finite and non-negative"
            )
        if component_columns and not np.allclose(
            declared.to_numpy(dtype=float),
            components.to_numpy(dtype=float),
            atol=1e-8,
            rtol=1e-12,
        ):
            raise ValueError(
                "execution fill transaction_cost disagrees with fee/tax/other components"
            )
        out["transaction_cost"] = declared.astype(float)
    else:
        out["transaction_cost"] = components.astype(float)

    fill_id_column = next(
        (
            column
            for column in ("fill_id", "execution_id", "trade_id", "order_id")
            if column in out.columns
        ),
        None,
    )
    if fill_id_column is not None:
        if fill_id_column != "fill_id":
            out = out.rename(columns={fill_id_column: "fill_id"})
        ids = out["fill_id"].astype("string").str.strip()
        if ids.isna().any() or ids.eq("").any() or ids.duplicated().any():
            raise ValueError("execution fill ledger fill_id must be present and unique")
        out["fill_id"] = ids
    else:
        duplicate_fields = [
            "ticker", "fill_date", "side", "filled_qty", "filled_price", "transaction_cost"
        ]
        if out.duplicated(duplicate_fields).any():
            raise ValueError("execution fill ledger contains duplicate fills without unique fill_id")
        out["fill_id"] = pd.Series(
            [f"ROW-{index + 1:06d}" for index in range(len(out))], dtype="string"
        )
    if "name" not in out.columns:
        out["name"] = pd.NA
    out["_source_order"] = np.arange(len(out), dtype="int64")
    sort_columns = ["fill_date"]
    if "fill_timestamp" in out.columns:
        timestamp = pd.to_datetime(out["fill_timestamp"], errors="coerce")
        if timestamp.isna().any():
            raise ValueError("execution fill ledger contains invalid fill_timestamp")
        out["fill_timestamp"] = timestamp
        sort_columns.append("fill_timestamp")
    sort_columns.append("_source_order")
    return out.sort_values(sort_columns, kind="stable").reset_index(drop=True)


def _validate_statement_cell(
    frame: pd.DataFrame,
    *,
    value_column: str,
    status_column: str,
) -> pd.Series:
    status = frame[status_column].astype("string").str.strip().str.upper().replace(
        {"VALUE": "EXPLICIT_VALUE", "BLANK": "NOT_SHOWN"}
    )
    frame[status_column] = status
    allowed = {"EXPLICIT_VALUE", "NOT_SHOWN"}
    if not status.isin(allowed).all():
        bad = sorted(status.loc[~status.isin(allowed)].dropna().unique().tolist())
        raise ValueError(f"{status_column} must use {sorted(allowed)}; bad={bad}")
    values = pd.to_numeric(frame[value_column], errors="coerce")
    explicit = status.eq("EXPLICIT_VALUE")
    if values.loc[explicit].isna().any() or values.loc[explicit].lt(0).any():
        raise ValueError(
            f"{value_column} must be finite and non-negative for EXPLICIT_VALUE cells"
        )
    if values.loc[status.eq("NOT_SHOWN")].notna().any():
        raise ValueError(
            f"{value_column} must remain NA when the broker statement cell is NOT_SHOWN"
        )
    return values.astype(float)


def _load_user_confirmed_statement_bundle(
    *,
    ledger_path: str | Path,
    manifest_path: str | Path,
    source_image_paths: list[str | Path] | tuple[str | Path, ...],
    derived_snapshot_path: str | Path,
) -> tuple[BrokerStatementExecutionEvidence, pd.DataFrame, float, float]:
    """Validate immutable screenshots, reviewed transcription and derived snapshot.

    Blank fee/tax cells are retained as NA.  A row's total cost is calculated
    only from the independently visible trade and settlement amounts.  The
    calculation is accepted only when every row reconciles and successive
    visible deposit balances also reconcile to the signed settlement cashflow.
    """

    ledger_source = Path(ledger_path).resolve()
    manifest_source = Path(manifest_path).resolve()
    snapshot_source = Path(derived_snapshot_path).resolve()
    image_sources = tuple(Path(path).resolve() for path in source_image_paths)
    if not ledger_source.is_file():
        raise FileNotFoundError(f"statement transcription not found: {ledger_source}")
    if not manifest_source.is_file():
        raise FileNotFoundError(f"statement bundle manifest not found: {manifest_source}")
    if not snapshot_source.is_file():
        raise FileNotFoundError(f"derived post-trade snapshot not found: {snapshot_source}")
    if not image_sources or any(not path.is_file() for path in image_sources):
        raise ValueError("every source broker-statement image must exist")

    try:
        metadata = json.loads(manifest_source.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid statement bundle manifest: {manifest_source}") from exc
    if not isinstance(metadata, dict):
        raise ValueError("statement bundle manifest must contain one JSON object")
    _reject_raw_sensitive_identifiers(metadata)
    if metadata.get("contract_version") != USER_CONFIRMED_STATEMENT_CONTRACT_VERSION:
        raise ValueError(
            "statement bundle contract_version must equal "
            f"{USER_CONFIRMED_STATEMENT_CONTRACT_VERSION!r}"
        )
    if str(metadata.get("source_type", "")).upper() != USER_CONFIRMED_STATEMENT_SOURCE_TYPE:
        raise ValueError(
            f"statement bundle source_type must equal {USER_CONFIRMED_STATEMENT_SOURCE_TYPE!r}"
        )
    if str(metadata.get("evidence_lineage", "")).upper() != USER_CONFIRMED_STATEMENT_SOURCE_TYPE:
        raise ValueError(
            "statement bundle evidence_lineage must identify user-confirmed images, not a "
            "DIRECT_BROKER_EXPORT"
        )
    if str(metadata.get("assurance_method", "")).upper() != USER_CONFIRMED_STATEMENT_METHOD:
        raise ValueError(
            f"statement bundle assurance_method must equal {USER_CONFIRMED_STATEMENT_METHOD!r}"
        )
    if str(metadata.get("broker_export_lineage", "")).upper() == BROKER_EXPORT_LINEAGE:
        raise ValueError("statement screenshots must not claim DIRECT_BROKER_EXPORT lineage")
    if metadata.get("actual_execution_proven") is not True:
        raise ValueError("statement bundle actual_execution_proven must be true")
    if metadata.get("opening_basis_proven") is not True:
        raise ValueError("statement bundle opening_basis_proven must be true")
    if str(metadata.get("status", "")).upper() not in {
        "PASS",
        "CERTIFIED",
        "EXECUTION_AND_OPENING_BASIS_PROVEN",
    }:
        raise ValueError("statement bundle status does not certify its opening basis")
    if metadata.get("all_statement_pages_supplied") is not True:
        raise ValueError("all_statement_pages_supplied must be true")
    if metadata.get("transcription_reviewed") is not True:
        raise ValueError("transcription_reviewed must be true")
    if metadata.get("pii_absence_reviewed") is not True:
        raise ValueError("pii_absence_reviewed must be true")
    if metadata.get("credentials_present") is not False:
        raise ValueError("credentials_present must be explicitly false")
    if metadata.get("raw_account_identifier_emitted") is not False:
        raise ValueError("raw_account_identifier_emitted must be explicitly false")
    if str(metadata.get("blank_cost_cells_policy", "")).upper() != UNKNOWN_BLANK_COST_POLICY:
        raise ValueError(
            f"blank_cost_cells_policy must equal {UNKNOWN_BLANK_COST_POLICY!r}"
        )
    if metadata.get("settlement_reconciliation_complete") is not True:
        raise ValueError("settlement_reconciliation_complete must be true")
    if metadata.get("trading_costs_complete") is not True:
        raise ValueError("trading_costs_complete must be true after settlement reconciliation")
    if (
        str(metadata.get("trading_cost_verification_basis", "")).upper()
        != BROKER_STATEMENT_SETTLEMENT_RECONCILIATION_POLICY
    ):
        raise ValueError(
            "statement trading costs require verification basis "
            f"{BROKER_STATEMENT_SETTLEMENT_RECONCILIATION_POLICY!r}"
        )

    confirmation = metadata.get("user_confirmation")
    if not isinstance(confirmation, dict) or confirmation.get("confirmed") is not True:
        raise ValueError("explicit user confirmation is required")
    if str(confirmation.get("scope", "")).upper() != "ACTUAL_TRADE_DATE":
        raise ValueError("user confirmation scope must be ACTUAL_TRADE_DATE")
    if str(confirmation.get("source_channel", "")).upper() != "CODEX_USER_MESSAGE":
        raise ValueError("user confirmation source_channel must be CODEX_USER_MESSAGE")
    execution_date = _one_iso_date(
        confirmation.get("confirmed_execution_date"),
        "user confirmed_execution_date",
    )
    attestation_hash = str(confirmation.get("attestation_sha256", "")).strip().lower()
    if not _SHA256_PATTERN.fullmatch(attestation_hash):
        raise ValueError("user confirmation attestation_sha256 is required")
    attestation_text = confirmation.get("attestation_text")
    if not isinstance(attestation_text, str) or not attestation_text.strip():
        raise ValueError("user confirmation attestation_text is required")
    actual_attestation_hash = hashlib.sha256(attestation_text.encode("utf-8")).hexdigest()
    if actual_attestation_hash != attestation_hash:
        raise ValueError("user confirmation attestation_sha256 does not match attestation_text")
    if attestation_text != USER_CONFIRMED_ATTESTATION_TEXT:
        raise ValueError("user confirmation attestation_text does not match the exact user message")
    recorded_at = pd.to_datetime(confirmation.get("recorded_at"), errors="coerce")
    if pd.isna(recorded_at) or getattr(recorded_at, "tzinfo", None) is None:
        raise ValueError("user confirmation recorded_at must be a timezone-aware timestamp")
    chronology = metadata.get("chronology_override")
    if not isinstance(chronology, dict) or chronology.get("confirmed") is not True:
        raise ValueError("explicit chronology_override is required for this evidence class")
    if (
        str(chronology.get("reason_code", "")).upper()
        != "USER_CONFIRMED_ACTUAL_BROKER_STATEMENT_SUPERSEDES_PRIOR_ASSUMPTIONS"
    ):
        raise ValueError("chronology_override reason_code is invalid")
    if str(chronology.get("prior_pipeline_facts_status", "")).upper() not in {
        "NULL",
        "SUPERSEDED",
        "UNKNOWN_OR_SUPERSEDED",
    }:
        raise ValueError("prior pipeline chronology must be explicitly null or superseded")

    declared_images = metadata.get("source_images")
    if not isinstance(declared_images, list) or len(declared_images) != len(image_sources):
        raise ValueError("source_images must enumerate every supplied image")
    declared_page_count = int(metadata.get("statement_page_count", 0))
    if declared_page_count != len(image_sources):
        raise ValueError("statement_page_count disagrees with supplied images")
    image_records: list[tuple[int, str, str]] = []
    for item in declared_images:
        if not isinstance(item, dict):
            raise ValueError("each source_images entry must be an object")
        filename = str(item.get("filename", "")).strip()
        if not filename or Path(filename).name != filename:
            raise ValueError("source image manifest entries may contain filenames only")
        page_number = int(item.get("page_number", 0))
        declared_hash = str(item.get("sha256", "")).strip().lower()
        if not _SHA256_PATTERN.fullmatch(declared_hash):
            raise ValueError("source image sha256 must be a SHA-256 digest")
        image_records.append((page_number, filename, declared_hash))
    image_records.sort()
    if [item[0] for item in image_records] != list(range(1, len(image_sources) + 1)):
        raise ValueError("source image page numbers must be consecutive from 1")
    supplied_by_name = {path.name: path for path in image_sources}
    if len(supplied_by_name) != len(image_sources):
        raise ValueError("source image filenames must be unique")
    image_hashes: list[str] = []
    for _, filename, declared_hash in image_records:
        path = supplied_by_name.get(filename)
        if path is None:
            raise ValueError(f"declared source image was not supplied: {filename}")
        with path.open("rb") as handle:
            if handle.read(3) != b"\xff\xd8\xff":
                raise ValueError(f"source image is not a JPEG: {filename}")
        actual_hash = sha256_file(path)
        if actual_hash != declared_hash:
            raise ValueError(f"source image SHA-256 mismatch: {filename}")
        image_hashes.append(actual_hash)

    ledger_contract = metadata.get("derived_fill_ledger")
    snapshot_contract = metadata.get("derived_post_trade_snapshot")
    if not isinstance(ledger_contract, dict) or not isinstance(snapshot_contract, dict):
        raise ValueError("derived fill-ledger and post-trade snapshot contracts are required")
    for contract, source, label in (
        (ledger_contract, ledger_source, "derived fill ledger"),
        (snapshot_contract, snapshot_source, "derived post-trade snapshot"),
    ):
        filename = str(contract.get("filename", "")).strip()
        if filename != source.name:
            raise ValueError(f"{label} filename does not match the supplied artifact")
        declared_hash = str(contract.get("sha256", "")).strip().lower()
        if declared_hash != sha256_file(source):
            raise ValueError(f"{label} SHA-256 mismatch")

    ledger = _read_table(ledger_source)
    aliases = {
        "statement_transaction_number": "transaction_number",
        "execution_date": "trade_date",
        "filled_qty": "quantity",
        "filled_price": "unit_price",
    }
    rename_aliases = {
        alias: canonical
        for canonical, alias in aliases.items()
        if canonical not in ledger.columns and alias in ledger.columns
    }
    if rename_aliases:
        ledger = ledger.rename(columns=rename_aliases)
    allowed_columns = {
        "statement_transaction_number",
        "execution_date",
        "ticker",
        "instrument_id",
        "instrument_role",
        "valuation_status",
        "name",
        "trade_side",
        "filled_qty",
        "filled_price",
        "trade_amount",
        "settlement_amount",
        "deposit_balance",
        "fee",
        "tax",
        "fee_cell_status",
        "tax_cell_status",
        "tax_detail",
        "balance_qty",
        "source_page",
    }
    unexpected = sorted(set(ledger.columns) - allowed_columns)
    required = {
        "statement_transaction_number",
        "execution_date",
        "ticker",
        "instrument_id",
        "instrument_role",
        "valuation_status",
        "name",
        "trade_side",
        "filled_qty",
        "filled_price",
        "trade_amount",
        "settlement_amount",
        "deposit_balance",
        "fee",
        "tax",
        "fee_cell_status",
        "tax_cell_status",
        "balance_qty",
        "source_page",
    }
    if unexpected:
        raise ValueError(f"statement transcription has unexpected columns: {unexpected}")
    missing = sorted(required - set(ledger.columns))
    if missing:
        raise ValueError(f"statement transcription missing columns: {missing}")
    if ledger.empty:
        raise ValueError("statement transcription cannot be empty")
    if len(ledger) != int(ledger_contract.get("row_count", -1)):
        raise ValueError("statement transcription row_count disagrees with manifest")

    out = ledger.copy()
    tx_number = pd.to_numeric(out["statement_transaction_number"], errors="coerce")
    if (
        tx_number.isna().any()
        or tx_number.le(0).any()
        or (tx_number % 1).ne(0).any()
        or tx_number.duplicated().any()
    ):
        raise ValueError("statement transaction numbers must be unique positive integers")
    out["statement_transaction_number"] = tx_number.astype("int64")
    if metadata.get("complete_transaction_number_sequence") is not True:
        raise ValueError("complete_transaction_number_sequence must be true")
    ordered_numbers = sorted(out["statement_transaction_number"].tolist())
    if ordered_numbers != list(range(ordered_numbers[0], ordered_numbers[-1] + 1)):
        raise ValueError("statement transaction-number sequence has a gap")
    row_dates = pd.to_datetime(out["execution_date"], errors="coerce").dt.normalize()
    if row_dates.isna().any() or not row_dates.eq(execution_date).all():
        raise ValueError("every statement row must equal the explicitly confirmed execution date")
    out["execution_date"] = row_dates
    out["instrument_role"] = (
        out["instrument_role"].astype("string").str.strip().str.upper()
    )
    allowed_roles = {"EQUITY", "CASH_EQUIVALENT_FUNDING"}
    if not out["instrument_role"].isin(allowed_roles).all():
        raise ValueError(f"statement instrument_role must use {sorted(allowed_roles)}")
    equity = out["instrument_role"].eq("EQUITY")
    normalized_ticker = pd.Series(pd.NA, index=out.index, dtype="string")
    normalized_ticker.loc[equity] = normalize_security_id_series(out.loc[equity, "ticker"])
    if normalized_ticker.loc[equity].isna().any():
        raise ValueError("EQUITY statement rows require a valid six-digit ticker")
    funding_ticker_text = out.loc[~equity, "ticker"].astype("string").str.strip()
    if (funding_ticker_text.notna() & funding_ticker_text.ne("")).any():
        raise ValueError(
            "CASH_EQUIVALENT_FUNDING ticker must remain blank when the image shows no ticker"
        )
    funding_ids = out.loc[~equity, "instrument_id"].astype("string").str.strip()
    if funding_ids.isna().any() or funding_ids.eq("").any():
        raise ValueError("CASH_EQUIVALENT_FUNDING rows require a non-ticker instrument_id")
    out["ticker"] = normalized_ticker
    valuation_status = out["valuation_status"].astype("string").str.strip().str.upper()
    equity_buy = equity & out["trade_side"].astype("string").str.strip().str.upper().eq("BUY")
    equity_sell = equity & ~equity_buy
    if not valuation_status.loc[equity_buy].eq("VALUED_IN_DERIVED_SNAPSHOT").all():
        raise ValueError("BUY EQUITY rows must be VALUED_IN_DERIVED_SNAPSHOT")
    if not valuation_status.loc[equity_sell].eq("NOT_HELD_AT_PERFORMANCE_START").all():
        raise ValueError("SELL EQUITY rows must be NOT_HELD_AT_PERFORMANCE_START")
    if not valuation_status.loc[~equity].eq("NA_OFFICIAL_CLOSE_NOT_COLLECTED").all():
        raise ValueError(
            "CASH_EQUIVALENT_FUNDING rows must remain "
            "NA_OFFICIAL_CLOSE_NOT_COLLECTED"
        )
    out["valuation_status"] = valuation_status
    out["trade_side"] = out["trade_side"].astype("string").str.strip().str.upper()
    if not out["trade_side"].isin({"BUY", "SELL"}).all():
        raise ValueError("statement transcription permits only BUY/SELL rows")
    for column, integer_only in (("filled_qty", True), ("filled_price", False), ("trade_amount", False), ("settlement_amount", False)):
        values = pd.to_numeric(out[column], errors="coerce")
        if values.isna().any() or (~np.isfinite(values.astype(float))).any() or values.le(0).any():
            raise ValueError(f"statement {column} must be finite and positive")
        if integer_only and (values % 1).ne(0).any():
            raise ValueError(f"statement {column} must contain integers")
        out[column] = values.astype("int64" if integer_only else float)
    expected_trade = out["filled_qty"].astype(float) * out["filled_price"]
    if not np.allclose(
        expected_trade.to_numpy(), out["trade_amount"].to_numpy(dtype=float), atol=1e-8, rtol=0
    ):
        raise ValueError("statement trade_amount does not equal filled_qty * filled_price")
    out["fee"] = _validate_statement_cell(
        out, value_column="fee", status_column="fee_cell_status"
    )
    out["tax"] = _validate_statement_cell(
        out, value_column="tax", status_column="tax_cell_status"
    )
    explicit_components = out[["fee", "tax"]].fillna(0.0).sum(axis=1)
    implied_cost = np.where(
        out["trade_side"].eq("SELL"),
        out["trade_amount"] - out["settlement_amount"],
        out["settlement_amount"] - out["trade_amount"],
    ).astype(float)
    if (~np.isfinite(implied_cost)).any() or (implied_cost < -1e-8).any():
        raise ValueError("statement settlement direction implies a negative trading cost")
    implied_cost = np.maximum(implied_cost, 0.0)
    if (explicit_components.to_numpy(dtype=float) - implied_cost > 1e-8).any():
        raise ValueError("visible fee/tax exceeds the row settlement-derived total cost")
    all_components_explicit = out["fee_cell_status"].eq("EXPLICIT_VALUE") & out[
        "tax_cell_status"
    ].eq("EXPLICIT_VALUE")
    if not np.allclose(
        explicit_components.loc[all_components_explicit].to_numpy(dtype=float),
        implied_cost[all_components_explicit.to_numpy()],
        atol=1e-8,
        rtol=0,
    ):
        raise ValueError("explicit fee/tax does not reconcile to statement settlement")
    out["transaction_cost"] = implied_cost
    out["unclassified_settlement_cost"] = implied_cost - explicit_components.to_numpy(dtype=float)
    out["cost_derivation"] = BROKER_STATEMENT_SETTLEMENT_RECONCILIATION_POLICY

    page = pd.to_numeric(out["source_page"], errors="coerce")
    if page.isna().any() or (page % 1).ne(0).any() or not page.between(1, len(image_sources)).all():
        raise ValueError("statement source_page must identify a supplied image page")
    out["source_page"] = page.astype("int64")
    balance_qty = pd.to_numeric(out["balance_qty"], errors="coerce")
    if balance_qty.dropna().lt(0).any() or (balance_qty.dropna() % 1).ne(0).any():
        raise ValueError("visible balance_qty values must be non-negative integers")
    out["balance_qty"] = balance_qty.astype("Int64")
    deposit_balance = pd.to_numeric(out["deposit_balance"], errors="coerce")
    if (~np.isfinite(deposit_balance.dropna().astype(float))).any():
        raise ValueError("visible deposit_balance values must be finite")
    out["deposit_balance"] = deposit_balance.astype(float)
    out = out.sort_values("statement_transaction_number", kind="stable").reset_index(drop=True)
    cash_effect = np.where(
        out["trade_side"].eq("SELL"), out["settlement_amount"], -out["settlement_amount"]
    ).astype(float)
    visible_balance_rows = np.flatnonzero(out["deposit_balance"].notna().to_numpy())
    if len(visible_balance_rows) < 2:
        raise ValueError("at least two visible deposit balances are required for cash reconciliation")
    for prior_index, current_index in zip(visible_balance_rows[:-1], visible_balance_rows[1:]):
        expected_balance = float(out.loc[prior_index, "deposit_balance"]) + float(
            cash_effect[prior_index + 1 : current_index + 1].sum()
        )
        actual_balance = float(out.loc[current_index, "deposit_balance"])
        if not np.isclose(expected_balance, actual_balance, atol=1e-8, rtol=0):
            raise ValueError(
                "successive broker deposit balances do not reconcile to settlement cashflows"
            )
    final_balance_index = int(visible_balance_rows[-1])
    if final_balance_index != len(out) - 1:
        raise ValueError("the final transaction must display the final signed deposit balance")
    signed_cash = _finite_number(
        metadata.get("final_signed_settlement_balance"),
        "final_signed_settlement_balance",
    )
    declared_opening_cash = _finite_number(metadata.get("opening_cash"), "opening_cash")
    if not np.isclose(declared_opening_cash, signed_cash, atol=1e-8, rtol=0):
        raise ValueError("opening_cash must equal final_signed_settlement_balance")
    if metadata.get("opening_cash_basis") != "SIGNED_BROKER_SETTLEMENT_BALANCE":
        raise ValueError("opening_cash_basis must be SIGNED_BROKER_SETTLEMENT_BALANCE")
    if metadata.get("signed_settlement_balance_verified") is not True:
        raise ValueError("signed_settlement_balance_verified must be true")
    if not np.isclose(
        float(out.loc[final_balance_index, "deposit_balance"]), signed_cash, atol=1e-8, rtol=0
    ):
        raise ValueError("final signed settlement balance disagrees with the broker statement")

    snapshot_raw = _read_table(snapshot_source)
    snapshot_date = _one_iso_date(
        snapshot_contract.get("snapshot_date"), "derived snapshot_date"
    )
    if snapshot_date != execution_date:
        raise ValueError("derived post-trade snapshot_date must equal the confirmed last fill date")
    snapshot = _normalize_snapshot(snapshot_raw, snapshot_date)
    snapshot_required = {"name", "base_price", "position_value", "price_source"}
    snapshot_missing = sorted(snapshot_required - set(snapshot.columns))
    if snapshot_missing:
        raise ValueError(f"derived snapshot missing columns: {snapshot_missing}")
    base_price = pd.to_numeric(snapshot["base_price"], errors="coerce")
    position_value = pd.to_numeric(snapshot["position_value"], errors="coerce")
    if (
        base_price.isna().any()
        or base_price.le(0).any()
        or position_value.isna().any()
        or position_value.lt(0).any()
    ):
        raise ValueError("derived snapshot exact-close prices/values are invalid")
    if not np.allclose(
        (snapshot["shares"].astype(float) * base_price).to_numpy(),
        position_value.to_numpy(dtype=float),
        atol=1e-8,
        rtol=0,
    ):
        raise ValueError("derived snapshot position_value does not equal shares * exact close")
    if not snapshot["price_source"].astype(str).eq("KRX_OFFICIAL_EXACT_CLOSE").all():
        raise ValueError("every derived snapshot price must be KRX_OFFICIAL_EXACT_CLOSE")
    snapshot["base_price"] = base_price.astype(float)
    snapshot["position_value"] = position_value.astype(float)
    expected_position_count = int(snapshot_contract.get("position_count", -1))
    if len(snapshot) != expected_position_count:
        raise ValueError("derived snapshot position_count disagrees with manifest")

    buy_rows = out.loc[out["trade_side"].eq("BUY") & out["instrument_role"].eq("EQUITY")]
    terminal_buy_balances: dict[str, int] = {}
    for ticker, rows in buy_rows.groupby("ticker", sort=False):
        visible = rows.loc[rows["balance_qty"].notna(), "balance_qty"]
        if visible.empty:
            raise ValueError(f"BUY ticker lacks a visible terminal balance: {ticker}")
        terminal_buy_balances[str(ticker)] = int(visible.iloc[-1])
    snapshot_positions = dict(zip(snapshot["ticker"].astype(str), snapshot["shares"].astype(int)))
    for ticker, qty in terminal_buy_balances.items():
        if snapshot_positions.get(ticker) != qty:
            raise ValueError(f"derived snapshot does not match visible terminal balance: {ticker}")
    if (
        str(metadata.get("position_reconciliation_basis", "")).upper()
        != "HASH_BOUND_PRIOR_EQUITY_HOLDINGS_PLUS_STATEMENT_EQUITY_DELTAS"
    ):
        raise ValueError("position_reconciliation_basis is invalid")
    prior_contract = metadata.get("prior_equity_holdings")
    if not isinstance(prior_contract, dict):
        raise ValueError("hash-bound prior_equity_holdings contract is required")
    prior_filename = str(prior_contract.get("filename", "")).strip()
    if not prior_filename or Path(prior_filename).name != prior_filename:
        raise ValueError("prior holdings contract may contain a filename only")
    prior_path = (manifest_source.parent / prior_filename).resolve()
    if not prior_path.is_file() or manifest_source.parent.resolve() not in prior_path.parents:
        raise ValueError("hash-bound prior equity holdings artifact is missing")
    if str(prior_contract.get("sha256", "")).strip().lower() != sha256_file(prior_path):
        raise ValueError("prior equity holdings SHA-256 mismatch")
    prior_frame = _read_table(prior_path)
    prior_required = {"ticker", "shares"}
    if not prior_required.issubset(prior_frame.columns) or prior_frame.empty:
        raise ValueError("prior equity holdings schema is invalid")
    prior_tickers = normalize_security_id_series(prior_frame["ticker"])
    prior_shares = pd.to_numeric(prior_frame["shares"], errors="coerce")
    if (
        prior_tickers.isna().any()
        or prior_tickers.duplicated().any()
        or prior_shares.isna().any()
        or prior_shares.le(0).any()
        or (prior_shares % 1).ne(0).any()
    ):
        raise ValueError("prior equity holdings contain invalid positions")
    expected_snapshot = dict(zip(prior_tickers.astype(str), prior_shares.astype(int)))
    equity_rows = out.loc[out["instrument_role"].eq("EQUITY")]
    for row in equity_rows.itertuples(index=False):
        ticker = str(row.ticker)
        delta = int(row.filled_qty) if row.trade_side == "BUY" else -int(row.filled_qty)
        next_qty = expected_snapshot.get(ticker, 0) + delta
        if next_qty < 0:
            raise ValueError("statement equity fills create a negative position")
        if next_qty == 0:
            expected_snapshot.pop(ticker, None)
        else:
            expected_snapshot[ticker] = next_qty
    if expected_snapshot != snapshot_positions:
        raise ValueError(
            "derived snapshot does not reconcile from hash-bound prior holdings plus statement fills"
        )
    sold_tickers = set(
        out.loc[
            out["trade_side"].eq("SELL") & out["instrument_role"].eq("EQUITY"),
            "ticker",
        ].astype(str)
    )
    if sold_tickers & set(snapshot_positions):
        raise ValueError("a sold/exited statement ticker remains in the derived snapshot")

    computed_opening_nav = float(position_value.sum()) + signed_cash
    declared_opening_nav = _finite_number(metadata.get("opening_nav"), "opening_nav")
    if declared_opening_nav <= 0:
        raise ValueError("opening_nav must be positive")
    if not np.isclose(computed_opening_nav, declared_opening_nav, atol=1e-8, rtol=0):
        raise ValueError(
            "opening_nav must equal exact-close holdings value plus signed settlement balance"
        )
    if (
        str(metadata.get("opening_nav_basis", "")).upper()
        != "EXACT_CLOSE_HOLDINGS_PLUS_SIGNED_BROKER_SETTLEMENT_BALANCE"
    ):
        raise ValueError("opening_nav_basis is invalid for the statement bundle")
    if (
        str(metadata.get("performance_start_basis", "")).upper()
        != "USER_CONFIRMED_BROKER_STATEMENT_LAST_FILL"
    ):
        raise ValueError("performance_start_basis is invalid for the statement bundle")

    interval_activity_proven = (
        str(metadata.get("interval_activity_status", "")).upper() == "VERIFIED"
        and metadata.get("external_cash_flows_verified") is True
        and pd.to_numeric(
            pd.Series([metadata.get("external_cash_flows")]), errors="coerce"
        ).iloc[0]
        == 0.0
        and metadata.get("transaction_history_complete") is True
        and metadata.get("intermediate_trades_verified_none") is True
    )
    if interval_activity_proven:
        _validate_cash_flow_and_transaction_history_declaration(
            metadata,
            expected_source_type="POST_TRADE_SNAPSHOT",
        )
        if metadata.get("production_ready") is not True:
            raise ValueError("verified interval activity requires production_ready=true")
    else:
        if str(metadata.get("interval_activity_status", "")).upper() != "UNKNOWN":
            raise ValueError("unverified interval activity must be explicitly UNKNOWN")
        if metadata.get("production_ready") is not False:
            raise ValueError("UNKNOWN interval activity requires production_ready=false")
        if metadata.get("external_cash_flows_verified") is True:
            raise ValueError("UNKNOWN interval cannot self-certify external cash flows")
        if metadata.get("transaction_history_complete") is True:
            raise ValueError("UNKNOWN interval cannot self-certify complete transaction history")
        if metadata.get("intermediate_trades_verified_none") is True:
            raise ValueError("UNKNOWN interval cannot self-certify no intermediate trades")
        if (
            str(metadata.get("performance_calculation_status", "")).upper()
            != "CONDITIONAL_STATIC_SHADOW_LIABILITY"
        ):
            raise ValueError(
                "UNKNOWN interval must use CONDITIONAL_STATIC_SHADOW_LIABILITY"
            )
    evidence = BrokerStatementExecutionEvidence(
        source_type=USER_CONFIRMED_STATEMENT_SOURCE_TYPE,
        calculation_method=USER_CONFIRMED_STATEMENT_METHOD,
        ledger_path=ledger_source,
        ledger_sha256=sha256_file(ledger_source),
        manifest_path=manifest_source,
        manifest_sha256=sha256_file(manifest_source),
        execution_date=execution_date,
        first_fill_date=execution_date,
        last_fill_date=execution_date,
        source_image_sha256s=tuple(image_hashes),
        frame=out,
        actual_execution_proven=True,
        production_ready=interval_activity_proven,
        trading_costs_complete=True,
        metadata=metadata,
    )
    return evidence, snapshot, declared_opening_nav, signed_cash


def load_user_confirmed_broker_statement_performance_basis(
    *,
    ledger_path: str | Path,
    manifest_path: str | Path,
    source_image_paths: list[str | Path] | tuple[str | Path, ...],
    derived_snapshot_path: str | Path,
) -> PerformanceCostBasis:
    """Load a full image-bound statement bundle as a static opening basis."""

    evidence, snapshot, opening_nav, opening_cash = _load_user_confirmed_statement_bundle(
        ledger_path=ledger_path,
        manifest_path=manifest_path,
        source_image_paths=source_image_paths,
        derived_snapshot_path=derived_snapshot_path,
    )
    metadata = {
        **(evidence.metadata or {}),
        "embedded_source_type": "POST_TRADE_SNAPSHOT",
        "execution_evidence_only": False,
        "opening_basis_proven": True,
        "interval_activity_proven": evidence.production_ready,
        "exact_close_crosscheck_required": True,
        "statement_ledger_sha256": evidence.ledger_sha256,
        "statement_manifest_sha256": evidence.manifest_sha256,
        "source_image_sha256s": list(evidence.source_image_sha256s),
        "statement_execution_summary": {
            "row_count": int(len(evidence.frame)),
            "first_fill_date": str(evidence.first_fill_date.date()),
            "last_fill_date": str(evidence.last_fill_date.date()),
            "settlement_reconciled_trading_cost": float(
                evidence.frame["transaction_cost"].sum()
            ),
            "cash_equivalent_funding_row_count": int(
                evidence.frame["instrument_role"].eq("CASH_EQUIVALENT_FUNDING").sum()
            ),
            "mark_to_close_excluded_row_count": int(
                evidence.frame["valuation_status"]
                .eq("NA_OFFICIAL_CLOSE_NOT_COLLECTED")
                .sum()
            ),
            "funding_rows_included_in_cash_and_cost_reconciliation": True,
            "funding_rows_included_in_mark_to_close_pnl": False,
        },
    }
    return PerformanceCostBasis(
        source_type=USER_CONFIRMED_STATEMENT_SOURCE_TYPE,
        calculation_method=STATIC_METHOD,
        source_path=Path(derived_snapshot_path).resolve(),
        source_sha256=sha256_file(derived_snapshot_path),
        basis_date=evidence.execution_date,
        production_ready=evidence.production_ready,
        frame=snapshot,
        opening_nav=opening_nav,
        opening_cash=opening_cash,
        certification_path=evidence.manifest_path,
        certification_sha256=evidence.manifest_sha256,
        metadata=metadata,
    )


def load_user_confirmed_broker_statement_execution_evidence(
    *,
    ledger_path: str | Path,
    manifest_path: str | Path,
    source_image_paths: list[str | Path] | tuple[str | Path, ...],
    derived_snapshot_path: str | Path,
) -> BrokerStatementExecutionEvidence:
    """Return the reconciled statement rows for execution-window QA only."""

    evidence, _, _, _ = _load_user_confirmed_statement_bundle(
        ledger_path=ledger_path,
        manifest_path=manifest_path,
        source_image_paths=source_image_paths,
        derived_snapshot_path=derived_snapshot_path,
    )
    return evidence


def load_certified_execution_fill_ledger(
    path: str | Path,
    certification_path: str | Path | None = None,
) -> PerformanceCostBasis:
    """Load fill evidence solely for execution-window attribution.

    The detached certification's ``performance_start_date`` must equal the
    last fill date.  The returned basis must never be passed to the primary NAV
    calculator; callers use it only with
    :func:`calculate_execution_window_attribution`.
    """

    source = Path(path).resolve()
    meta_path, metadata = _load_certification(source, certification_path)
    source_sha = _validate_certification(
        source, meta_path, metadata, expected_source_type="FILL_LEDGER"
    )
    ledger = _normalize_execution_fill_ledger(
        _read_table(source),
        certification_metadata=metadata,
    )
    last_fill = pd.Timestamp(ledger["fill_date"].max()).normalize()
    declared_start = _one_iso_date(
        metadata.get("performance_start_date"), "performance_start_date"
    )
    if declared_start != last_fill:
        raise ValueError(
            "execution fill certification performance_start_date must equal last_fill_date: "
            f"declared={declared_start.date()} last_fill={last_fill.date()}"
        )
    basis = PerformanceCostBasis(
        source_type="FILL_LEDGER",
        calculation_method=TRANSACTION_METHOD,
        source_path=source,
        source_sha256=source_sha,
        basis_date=last_fill,
        production_ready=True,
        frame=ledger,
        opening_nav=(
            _finite_nonnegative(metadata.get("opening_nav"), "opening_nav")
            if metadata.get("opening_nav") is not None
            else None
        ),
        opening_cash=(
            _finite_certified_cash(
                metadata.get("opening_cash"), "opening_cash", metadata
            )
            if metadata.get("opening_cash") is not None
            else None
        ),
        certification_path=meta_path.resolve(),
        certification_sha256=sha256_file(meta_path),
        metadata={**metadata, "execution_attribution_only": True},
    )
    validate_certified_interval_contract(basis, last_fill)
    return basis


def load_certified_post_trade_snapshot(
    path: str | Path,
    certification_path: str | Path | None = None,
) -> PerformanceCostBasis:
    source = Path(path).resolve()
    meta_path, metadata = _load_certification(source, certification_path)
    source_sha = _validate_certification(
        source, meta_path, metadata, expected_source_type="POST_TRADE_SNAPSHOT"
    )
    snapshot_date = _one_iso_date(metadata.get("snapshot_date"), "snapshot_date")
    opening_nav = _finite_nonnegative(metadata.get("opening_nav"), "opening_nav")
    opening_cash = _finite_certified_cash(
        metadata.get("opening_cash"), "opening_cash", metadata
    )
    if opening_nav <= 0:
        raise ValueError("opening_nav must be positive")
    snapshot = _normalize_snapshot(_read_table(source), snapshot_date)
    return PerformanceCostBasis(
        source_type="POST_TRADE_SNAPSHOT",
        calculation_method=STATIC_METHOD,
        source_path=source,
        source_sha256=source_sha,
        basis_date=snapshot_date,
        production_ready=True,
        frame=snapshot,
        opening_nav=opening_nav,
        opening_cash=opening_cash,
        certification_path=meta_path.resolve(),
        certification_sha256=sha256_file(meta_path),
        metadata=metadata,
    )


def _resolve_manifest_source(manifest_path: Path, manifest: dict[str, Any], raw: Any) -> Path:
    path = Path(str(raw))
    if path.is_absolute():
        candidates = [path]
    else:
        candidates = [manifest_path.parent / path]
        root = manifest.get("root")
        if root:
            candidates.append(Path(str(root)) / path)
        candidates.append(Path(__file__).resolve().parents[2] / path)
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise FileNotFoundError(
        f"certified manifest source not found; candidates={[str(candidate) for candidate in candidates]}"
    )


def load_certified_manifest(path: str | Path) -> PerformanceCostBasis:
    manifest_path = Path(path).resolve()
    if not manifest_path.exists():
        raise FileNotFoundError(f"certified manifest not found: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid certified manifest: {manifest_path}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("certified manifest must contain one JSON object")
    _reject_raw_sensitive_identifiers(manifest)
    if str(manifest.get("status", "")).upper() != "PASS" or manifest.get("production_ready") is not True:
        raise ValueError("certified manifest must have status=PASS and production_ready=true")

    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    contract = manifest.get("performance_cost_basis")
    if not isinstance(contract, dict):
        contract = metadata.get("performance_cost_basis")
    if isinstance(contract, dict):
        interval_metadata = dict(contract)
        raw_source_type = str(contract.get("source_type", "POST_TRADE_SNAPSHOT")).upper()
        source_path_value = contract.get("source_path", contract.get("path"))
        expected_sha = contract.get("source_sha256", contract.get("sha256"))
        if contract.get("actual_execution_proven") is not True:
            raise ValueError("manifest performance_cost_basis actual_execution_proven must be true")
        basis_date_value = (
            contract.get("performance_start_date")
            if raw_source_type == "FILL_LEDGER"
            else contract.get("snapshot_date")
        )
        opening_nav_value = contract.get("opening_nav")
        opening_cash_value = contract.get("opening_cash")
        post_fill_cash_value = contract.get("post_fill_cash")
    else:
        input_contracts = metadata.get("input_contracts")
        holdings = input_contracts.get("holdings") if isinstance(input_contracts, dict) else None
        if not isinstance(holdings, dict):
            raise ValueError(
                "certified manifest has no performance_cost_basis or metadata.input_contracts.holdings"
            )
        interval_metadata = dict(holdings)
        raw_source_type = "POST_TRADE_SNAPSHOT"
        if holdings.get("actual_execution_proven") is not True:
            raise ValueError(
                "manifest holdings contract actual_execution_proven must be true"
            )
        source_path_value = holdings.get("path")
        expected_sha = holdings.get("sha256")
        basis_date_value = holdings.get("snapshot_date")
        opening_nav_value = holdings.get("opening_nav")
        opening_cash_value = holdings.get("opening_cash")
        post_fill_cash_value = holdings.get("post_fill_cash")

    _validate_cash_flow_and_transaction_history_declaration(
        interval_metadata,
        expected_source_type=raw_source_type,
    )

    if not source_path_value or not expected_sha:
        raise ValueError("certified manifest performance source path and SHA-256 are required")
    source = _resolve_manifest_source(manifest_path, manifest, source_path_value)
    actual_sha = sha256_file(source)
    if actual_sha != str(expected_sha).strip().lower():
        raise ValueError(
            f"certified manifest source SHA-256 mismatch: expected={expected_sha} actual={actual_sha}"
        )
    basis_date = _one_iso_date(basis_date_value, "certified manifest basis date")
    opening_nav = _finite_nonnegative(opening_nav_value, "certified manifest opening_nav")
    opening_cash = _finite_certified_cash(
        opening_cash_value, "certified manifest opening_cash", interval_metadata
    )
    if opening_nav <= 0:
        raise ValueError("certified manifest opening_nav must be positive")
    raw = _read_table(source)
    _validate_broker_export_assurance(interval_metadata, source_sha256=actual_sha)
    if raw_source_type == "FILL_LEDGER":
        post_fill_cash = _finite_certified_cash(
            post_fill_cash_value,
            "certified manifest post_fill_cash",
            interval_metadata,
        )
        normalized = _normalize_fill_ledger(
            raw,
            basis_date,
            certification_metadata=interval_metadata,
        )
        method = TRANSACTION_METHOD
    elif raw_source_type == "POST_TRADE_SNAPSHOT":
        post_fill_cash = None
        normalized = _normalize_snapshot(raw, basis_date)
        method = STATIC_METHOD
    else:
        raise ValueError(f"unsupported certified manifest source_type: {raw_source_type!r}")
    return PerformanceCostBasis(
        source_type="CERTIFIED_MANIFEST",
        calculation_method=method,
        source_path=source,
        source_sha256=actual_sha,
        basis_date=basis_date,
        production_ready=True,
        frame=normalized,
        opening_nav=opening_nav,
        opening_cash=opening_cash,
        post_fill_cash=post_fill_cash,
        certification_path=manifest_path,
        certification_sha256=sha256_file(manifest_path),
        metadata={
            **interval_metadata,
            "manifest_run_id": manifest.get("run_id"),
            "manifest_status": manifest.get("status"),
            "embedded_source_type": raw_source_type,
        },
    )


def resolve_performance_cost_basis(
    *,
    fill_ledger: str | Path | None = None,
    fill_ledger_meta: str | Path | None = None,
    user_statement_ledger: str | Path | None = None,
    user_statement_manifest: str | Path | None = None,
    user_statement_images: list[str | Path] | tuple[str | Path, ...] | None = None,
    user_statement_snapshot: str | Path | None = None,
    post_trade_snapshot: str | Path | None = None,
    post_trade_snapshot_meta: str | Path | None = None,
    certified_manifest: str | Path | None = None,
) -> PerformanceCostBasis:
    """Resolve one certified source without downgrading an invalid source."""

    if fill_ledger is None and fill_ledger_meta is not None:
        raise ValueError("fill-ledger certification was supplied without its fill ledger")
    if post_trade_snapshot is None and post_trade_snapshot_meta is not None:
        raise ValueError(
            "post-trade snapshot certification was supplied without its snapshot"
        )
    if fill_ledger is not None:
        return load_certified_fill_ledger(fill_ledger, fill_ledger_meta)
    statement_inputs = (
        user_statement_ledger,
        user_statement_manifest,
        user_statement_snapshot,
    )
    statement_requested = any(value is not None for value in statement_inputs) or bool(
        user_statement_images
    )
    if statement_requested:
        if not all(value is not None for value in statement_inputs) or not user_statement_images:
            raise ValueError(
                "user-confirmed statement source requires ledger, manifest, every source image, "
                "and derived post-trade snapshot together"
            )
        return load_user_confirmed_broker_statement_performance_basis(
            ledger_path=user_statement_ledger,
            manifest_path=user_statement_manifest,
            source_image_paths=user_statement_images,
            derived_snapshot_path=user_statement_snapshot,
        )
    if post_trade_snapshot is not None:
        return load_certified_post_trade_snapshot(
            post_trade_snapshot, post_trade_snapshot_meta
        )
    if certified_manifest is not None:
        return load_certified_manifest(certified_manifest)
    raise ValueError(
        "no certified performance cost basis: provide fill ledger, a complete user-confirmed "
        "broker-statement image bundle, post-trade snapshot, or certified manifest "
        "(filename dates and --target_date are not provenance)"
    )


def materialize_static_positions(
    basis: PerformanceCostBasis,
    prices: pd.DataFrame,
) -> pd.DataFrame:
    if basis.calculation_method != STATIC_METHOD:
        raise ValueError("static positions require a static cost-basis source")
    snapshot = basis.frame.copy()
    date = basis.basis_date
    exact = prices.loc[pd.to_datetime(prices["date"]).dt.normalize().eq(date)].copy()
    if exact.duplicated(["ticker"]).any():
        duplicate_tickers = exact.loc[exact.duplicated("ticker", False), "ticker"].tolist()
        raise ValueError(f"duplicate exact-date prices for snapshot: {duplicate_tickers}")
    exact = exact[["ticker", "date", "price"]].rename(
        columns={"date": "market_base_date", "price": "market_base_price"}
    )
    out = snapshot.merge(exact, on="ticker", how="left", validate="one_to_one")
    if out["market_base_price"].isna().any():
        missing = out.loc[out["market_base_price"].isna(), "ticker"].astype(str).tolist()
        raise ValueError(
            f"exact snapshot-date market prices are required; missing={missing} date={date.date()}"
        )
    if (basis.metadata or {}).get("exact_close_crosscheck_required") is True:
        if "base_price" not in out.columns:
            raise ValueError("image-bound statement snapshot must preserve certified exact closes")
        certified_close = pd.to_numeric(out["base_price"], errors="coerce")
        market_close = pd.to_numeric(out["market_base_price"], errors="coerce")
        if certified_close.isna().any() or not np.allclose(
            certified_close.to_numpy(dtype=float),
            market_close.to_numpy(dtype=float),
            atol=1e-8,
            rtol=0,
        ):
            raise ValueError(
                "derived statement snapshot exact closes disagree with the official price input"
            )
    # A broker snapshot's generic price field is commonly the historical
    # average acquisition price.  It is useful for account unrealized P&L, but
    # it must never become the rebalance-period performance basis.  Snapshot
    # performance therefore always resets cost basis to the official exact-date
    # close.  Exact executed cost belongs to the authenticated fill-ledger path.
    if "base_price" in out.columns:
        out["account_reference_price"] = out["base_price"]
    out["base_price"] = out["market_base_price"]
    out["base_price_source"] = "EXACT_MARKET_CLOSE"
    out["base_date"] = date
    out["post_trade_shares"] = out["shares"].astype("int64")
    out["base_value"] = out["post_trade_shares"] * out["base_price"]
    out["name_final"] = out["name"]
    out["action"] = "ACTUAL_HOLDING"
    out["reason"] = "certified post-trade snapshot"
    out["order_side"] = "HOLDING"
    keep = [
        "ticker", "name_final", "action", "reason", "order_side", "shares",
        "post_trade_shares", "base_date", "base_price", "base_price_source",
        "market_base_price", "base_value",
    ]
    if "account_reference_price" in out.columns:
        keep.append("account_reference_price")
    return out[keep].copy()


def _drawdown_metrics(daily: pd.DataFrame) -> tuple[pd.DataFrame, float, Optional[str]]:
    out = daily.sort_values("date").reset_index(drop=True).copy()
    out["daily_return"] = out["nav"].pct_change(fill_method=None).fillna(out["cum_return"])
    out["prior_peak_nav"] = pd.to_numeric(out["nav"], errors="raise").cummax()
    out["drawdown"] = out["nav"] / out["prior_peak_nav"] - 1.0
    peak_dates = out["date"].where(out["nav"].eq(out["prior_peak_nav"]))
    out["drawdown_peak_date"] = peak_dates.ffill()
    minimum_index = out["drawdown"].astype(float).idxmin()
    peak_date = out.loc[minimum_index, "drawdown_peak_date"]
    return (
        out,
        float(out["prior_peak_nav"].max()),
        str(pd.Timestamp(peak_date).date()) if pd.notna(peak_date) else None,
    )


def calculate_transaction_aware_performance(
    basis: PerformanceCostBasis,
    prices: pd.DataFrame,
    *,
    total_capital: float,
    end_date: str | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    if basis.calculation_method != TRANSACTION_METHOD:
        raise ValueError("transaction-aware performance requires a fill ledger")
    initial_nav = float(basis.opening_nav if basis.opening_nav is not None else np.nan)
    if not np.isfinite(initial_nav) or initial_nav <= 0:
        raise ValueError("certified fill ledger opening_nav must be finite and positive")
    supplied_capital = float(total_capital)
    if not np.isclose(supplied_capital, initial_nav, atol=1e-8, rtol=1e-12):
        raise ValueError(
            "--total_capital may only verify, never define, certified opening_nav: "
            f"argument={supplied_capital} certified={initial_nav}"
        )
    initial_cash = float(basis.opening_cash if basis.opening_cash is not None else np.nan)
    certified_post_fill_cash = float(
        basis.post_fill_cash if basis.post_fill_cash is not None else np.nan
    )
    if not np.isfinite(initial_cash) or not np.isfinite(certified_post_fill_cash):
        raise ValueError("certified fill ledger requires opening_cash and post_fill_cash")
    start = basis.basis_date
    end = pd.Timestamp(end_date).normalize() if end_date else pd.to_datetime(prices["date"]).max().normalize()
    if end < start:
        raise ValueError("end_date must be on or after certified performance_start_date")
    interval_contract = validate_certified_interval_contract(basis, end)
    ledger = basis.frame.copy()
    if ledger.empty:
        raise ValueError("certified fill ledger cannot be empty")
    if ledger["fill_date"].gt(end).any():
        future_ids = ledger.loc[ledger["fill_date"].gt(end), "fill_id"].astype(str).tolist()
        raise ValueError(f"fill ledger contains transactions after end_date: {future_ids}")

    all_prices = prices.copy()
    all_prices["date"] = pd.to_datetime(all_prices["date"], errors="coerce").dt.normalize()
    all_prices["price"] = pd.to_numeric(all_prices["price"], errors="coerce")
    if all_prices[["ticker", "date", "price"]].isna().any(axis=None) or all_prices["price"].le(0).any():
        raise ValueError("prices contain invalid ticker/date/price values")
    if all_prices.duplicated(["ticker", "date"]).any():
        raise ValueError("prices contain duplicate ticker/date rows")
    all_prices = all_prices.loc[all_prices["date"].between(start, end)].copy()
    dates = [pd.Timestamp(value) for value in sorted(all_prices["date"].unique())]
    if not dates or dates[0] != start:
        raise ValueError(
            "certified performance_start_date must be an exact trading date in prices; "
            "backfill and filename-date fallback are forbidden"
        )
    relevant_tickers = set(ledger["ticker"].astype(str))
    px = all_prices.loc[all_prices["ticker"].astype(str).isin(relevant_tickers)].copy()
    fill_dates = {pd.Timestamp(value) for value in ledger["fill_date"]}
    missing_fill_dates = sorted(fill_dates - set(dates))
    if missing_fill_dates:
        raise ValueError(
            "every fill date must be an exact trading date in prices: "
            f"missing={[pd.Timestamp(value).date().isoformat() for value in missing_fill_dates]}"
        )
    price_lookup = px.set_index(["date", "ticker"])["price"]
    opening_rows = ledger.loc[ledger["side"].eq("OPEN")]
    opening_inventory_value = float(
        (opening_rows["filled_qty"] * opening_rows["filled_price"]).sum()
    )
    opening_valuation = (
        opening_rows.assign(
            _opening_value=opening_rows["filled_qty"] * opening_rows["filled_price"]
        )
        .groupby("ticker", sort=False)
        .agg(_opening_qty=("filled_qty", "sum"), _opening_value=("_opening_value", "sum"))
    )
    opening_price_lookup = (
        opening_valuation["_opening_value"] / opening_valuation["_opening_qty"]
    ).to_dict()
    opening_tolerance = 1e-8 + 1e-12 * max(1.0, abs(initial_nav))
    if abs(initial_cash + opening_inventory_value - initial_nav) > opening_tolerance:
        raise ValueError(
            "OPEN inventory plus certified opening_cash must reconcile to certified opening_nav: "
            f"inventory={opening_inventory_value} cash={initial_cash} nav={initial_nav}"
        )
    quantities: dict[str, int] = {}
    cash = initial_cash
    aggregate: dict[str, dict[str, Any]] = {}
    names: dict[str, Any] = {}
    daily_rows: list[dict[str, Any]] = []

    for date_value in dates:
        date = pd.Timestamp(date_value)
        fills_today = ledger.loc[ledger["fill_date"].eq(date)]
        for row in fills_today.itertuples(index=False):
            ticker = str(row.ticker)
            side = str(row.side)
            qty = int(row.filled_qty)
            fill_price = float(row.filled_price)
            cost = float(row.transaction_cost)
            gross = qty * fill_price
            current = int(quantities.get(ticker, 0))
            stats = aggregate.setdefault(
                ticker,
                {
                    "opening_value": 0.0,
                    "opening_qty": 0,
                    "gross_buy_value": 0.0,
                    "gross_sell_value": 0.0,
                    "transaction_cost": 0.0,
                    "buy_qty": 0,
                    "sell_qty": 0,
                    "first_fill_date": date,
                },
            )
            if side == "OPEN":
                quantities[ticker] = current + qty
                stats["opening_value"] += gross
                stats["opening_qty"] += qty
            elif side == "BUY":
                cash -= gross + cost
                quantities[ticker] = current + qty
                stats["gross_buy_value"] += gross
                stats["buy_qty"] += qty
            else:
                if qty > current:
                    raise ValueError(
                        f"fill would create negative shares: ticker={ticker} qty={qty} current={current}"
                    )
                cash += gross - cost
                quantities[ticker] = current - qty
                stats["gross_sell_value"] += gross
                stats["sell_qty"] += qty
            stats["transaction_cost"] += cost
            if cash < -1e-8:
                raise ValueError(
                    f"fill ledger creates negative cash after fill_id={row.fill_id}: cash={cash}"
                )
            if pd.notna(row.name):
                names[ticker] = row.name

        invested = 0.0
        for ticker, qty in quantities.items():
            if qty <= 0:
                continue
            key = (date, ticker)
            if key not in price_lookup.index:
                raise ValueError(
                    f"missing exact close for an active position: ticker={ticker} date={date.date()}"
                )
            valuation_price = (
                float(opening_price_lookup[ticker])
                if date == start and ticker in opening_price_lookup
                else float(price_lookup.loc[key])
            )
            invested += qty * valuation_price
        nav = cash + invested
        daily_rows.append(
            {
                "date": date,
                "portfolio_value_ex_cash": invested,
                "cash": cash,
                "nav": nav,
                "cum_return": nav / initial_nav - 1.0,
            }
        )

    first_nav = float(daily_rows[0]["nav"])
    if abs(first_nav - initial_nav) > opening_tolerance:
        raise ValueError(
            "first portfolio NAV must equal certified opening_nav so cumulative return starts at zero: "
            f"first_nav={first_nav} opening_nav={initial_nav}"
        )

    post_fill_tolerance = 1e-8 + 1e-12 * max(1.0, abs(certified_post_fill_cash))
    if abs(cash - certified_post_fill_cash) > post_fill_tolerance:
        raise ValueError(
            "executions and transaction costs do not reconcile to certified post_fill_cash: "
            f"calculated={cash} certified={certified_post_fill_cash}"
        )

    daily, peak_nav, drawdown_peak_date = _drawdown_metrics(pd.DataFrame(daily_rows))
    last_date = pd.Timestamp(daily["date"].max())
    last_nav = float(daily.loc[daily["date"].eq(last_date), "nav"].iloc[0])
    contribution_rows: list[dict[str, Any]] = []
    position_rows: list[dict[str, Any]] = []
    for ticker, stats in aggregate.items():
        ending_qty = int(quantities.get(ticker, 0))
        ending_price = (
            float(price_lookup.loc[(last_date, ticker)]) if ending_qty > 0 else np.nan
        )
        ending_value = ending_qty * ending_price if ending_qty > 0 else 0.0
        pnl = (
            ending_value
            + float(stats["gross_sell_value"])
            - float(stats["gross_buy_value"])
            - float(stats["transaction_cost"])
            - float(stats["opening_value"])
        )
        denominator = float(stats["opening_value"] + stats["gross_buy_value"])
        acquired_qty = int(stats["opening_qty"] + stats["buy_qty"])
        base_price = denominator / acquired_qty if acquired_qty > 0 else np.nan
        common = {
            "ticker": ticker,
            "name": names.get(ticker, pd.NA),
            "shares_at_start": int(stats["opening_qty"]),
            "start_date": start,
            "start_price": (
                float(stats["opening_value"]) / int(stats["opening_qty"])
                if int(stats["opening_qty"]) > 0
                else np.nan
            ),
            "start_position_value": float(stats["opening_value"]),
            "end_date": last_date,
            "end_price": ending_price,
            "end_position_value": ending_value,
            "dividends": 0.0,
            # Position-directed convention: BUY is positive, SELL is negative.
            # Costs are intentionally excluded and reported in their own field.
            "net_intermediate_trade_cashflow": (
                float(stats["gross_buy_value"]) - float(stats["gross_sell_value"])
            ),
            "trading_cost_allocated": float(stats["transaction_cost"]),
            "period_pnl": pnl,
            "weight_at_start_nav": float(stats["opening_value"]) / initial_nav,
            "weight_at_end_nav": ending_value / last_nav,
            "performance_basis": TRANSACTION_METHOD,
            "date": last_date,
            "price": ending_price,
            "post_trade_shares": ending_qty,
            "base_price": base_price,
            "base_value": denominator,
            "position_value": ending_value,
            "opening_value": float(stats["opening_value"]),
            "gross_buy_value": float(stats["gross_buy_value"]),
            "gross_sell_value": float(stats["gross_sell_value"]),
            "transaction_cost": float(stats["transaction_cost"]),
            "pnl": pnl,
            # Canonical period return is defined only against value held at the
            # official performance start.  Securities first bought later have
            # start_position_value=0, so this field is explicitly NA rather
            # than a different denominator hidden under the same name.
            "position_period_return": (
                pnl / float(stats["opening_value"])
                if float(stats["opening_value"]) > 0
                else np.nan
            ),
            "contribution_to_total_return": pnl / initial_nav,
            "weight_at_last_nav": ending_value / last_nav,
            "contribution_method": TRANSACTION_METHOD,
        }
        contribution_rows.append(common)
        position_rows.append(
            {
                "ticker": ticker,
                "name_final": names.get(ticker, pd.NA),
                "action": "ACTUAL_FILL_LEDGER",
                "reason": "certified executed fills",
                "order_side": "FILLED",
                "shares": ending_qty,
                "post_trade_shares": ending_qty,
                "base_date": pd.Timestamp(stats["first_fill_date"]),
                "base_price": base_price,
                "base_value": denominator,
                "opening_value": float(stats["opening_value"]),
                "gross_buy_value": float(stats["gross_buy_value"]),
                "gross_sell_value": float(stats["gross_sell_value"]),
                "transaction_cost": float(stats["transaction_cost"]),
            }
        )
    contributions = pd.DataFrame(contribution_rows).sort_values(
        "contribution_to_total_return", ascending=False
    ).reset_index(drop=True)
    contributions = contributions[
        CANONICAL_CONTRIBUTION_COLUMNS
        + [column for column in contributions.columns if column not in CANONICAL_CONTRIBUTION_COLUMNS]
    ]
    positions = pd.DataFrame(position_rows).sort_values("ticker").reset_index(drop=True)
    summary: dict[str, Any] = {
        "start_date": str(pd.Timestamp(daily["date"].min()).date()),
        "end_date": str(last_date.date()),
        "initial_nav": initial_nav,
        "invested_base": opening_inventory_value,
        "opening_cash": initial_cash,
        "start_cash": initial_cash,
        "cash_after_rebalance": initial_cash,
        "certified_post_fill_cash": certified_post_fill_cash,
        "post_fill_cash_reconciliation_residual": float(cash - certified_post_fill_cash),
        "last_cash": float(daily.iloc[-1]["cash"]),
        "end_cash": float(daily.iloc[-1]["cash"]),
        "cash_pnl": 0.0,
        "cash_contribution": 0.0,
        "last_nav": last_nav,
        "cum_return": float(daily.iloc[-1]["cum_return"]),
        "peak_nav": peak_nav,
        "max_drawdown": float(daily["drawdown"].min()),
        "drawdown_peak_date": drawdown_peak_date,
        "max_drawdown_date": str(pd.Timestamp(daily.loc[daily["drawdown"].idxmin(), "date"]).date()),
        "num_positions": int(sum(qty > 0 for qty in quantities.values())),
        "num_traded_securities": int(len(aggregate)),
        "fill_count": int(len(ledger)),
        "total_transaction_cost": float(ledger["transaction_cost"].sum()),
        "total_trading_costs": float(ledger["transaction_cost"].sum()),
        "trading_cost_contribution": -float(ledger["transaction_cost"].sum()) / initial_nav,
        "nav_dividend_treatment": "EXCLUDED",
        "cash_dividends_included": False,
        "dividends": 0.0,
        **interval_contract,
        "contribution_method": TRANSACTION_METHOD,
    }
    validate_contribution_reconciliation(daily, contributions, summary)
    return positions, daily, contributions, summary


def calculate_execution_window_attribution(
    basis: PerformanceCostBasis,
    prices: pd.DataFrame,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Attribute a certified multi-day execution window at its last-fill close.

    This is deliberately separate from post-rebalance portfolio performance.
    For each BUY/SELL fill, the gross execution-window mark-to-market result is
    measured against the official close on the last fill date; transaction
    costs are then deducted explicitly.  OPEN inventory rows are excluded.
    """

    if basis.calculation_method != TRANSACTION_METHOD:
        raise ValueError("execution-window attribution requires a certified fill ledger")
    ledger = basis.frame.loc[basis.frame["side"].isin({"BUY", "SELL"})].copy()
    if ledger.empty:
        empty = pd.DataFrame(columns=EXECUTION_WINDOW_ATTRIBUTION_COLUMNS)
        return empty, {
            "status": "NOT_APPLICABLE_NO_EXECUTION_FILLS",
            "execution_window_type": "NOT_APPLICABLE",
            "first_fill_date": None,
            "last_fill_date": None,
            "execution_trading_day_count": 0,
            "fill_count": 0,
            "trading_cost": 0.0,
            "gross_mark_to_last_fill_close_pnl": 0.0,
            "net_mark_to_last_fill_close_pnl": 0.0,
            "attribution_basis": "LAST_FILL_DATE_OFFICIAL_CLOSE",
        }

    first_fill = pd.Timestamp(ledger["fill_date"].min()).normalize()
    last_fill = pd.Timestamp(ledger["fill_date"].max()).normalize()
    validate_certified_interval_contract(basis, last_fill)

    market = prices.copy()
    market["ticker"] = normalize_security_id_series(market["ticker"])
    market["date"] = pd.to_datetime(market["date"], errors="coerce").dt.normalize()
    market["price"] = pd.to_numeric(market["price"], errors="coerce")
    if market[["ticker", "date", "price"]].isna().any(axis=None) or market["price"].le(0).any():
        raise ValueError("execution attribution prices contain invalid ticker/date/price values")
    close = market.loc[market["date"].eq(last_fill), ["ticker", "price"]].copy()
    if close["ticker"].duplicated().any():
        raise ValueError("execution attribution contains duplicate last-fill-date prices")
    close = close.rename(columns={"price": "last_fill_close"})
    ledger = ledger.merge(close, on="ticker", how="left", validate="many_to_one")
    if ledger["last_fill_close"].isna().any():
        missing = sorted(ledger.loc[ledger["last_fill_close"].isna(), "ticker"].unique())
        raise ValueError(
            "execution attribution requires an exact official close for every traded security on "
            f"last_fill_date={last_fill.date()}: missing={missing}"
        )

    gross_fill_value = ledger["filled_qty"] * ledger["filled_price"]
    ledger["gross_buy_value"] = gross_fill_value.where(ledger["side"].eq("BUY"), 0.0)
    ledger["gross_sell_value"] = gross_fill_value.where(ledger["side"].eq("SELL"), 0.0)
    ledger["buy_qty"] = ledger["filled_qty"].where(ledger["side"].eq("BUY"), 0)
    ledger["sell_qty"] = ledger["filled_qty"].where(ledger["side"].eq("SELL"), 0)
    ledger["net_execution_cashflow"] = np.where(
        ledger["side"].eq("BUY"),
        -ledger["gross_buy_value"] - ledger["transaction_cost"],
        ledger["gross_sell_value"] - ledger["transaction_cost"],
    )
    ledger["gross_mark_to_last_fill_close_pnl"] = np.where(
        ledger["side"].eq("BUY"),
        ledger["filled_qty"] * (ledger["last_fill_close"] - ledger["filled_price"]),
        ledger["filled_qty"] * (ledger["filled_price"] - ledger["last_fill_close"]),
    )
    ledger["net_mark_to_last_fill_close_pnl"] = (
        ledger["gross_mark_to_last_fill_close_pnl"] - ledger["transaction_cost"]
    )

    grouped = ledger.groupby("ticker", sort=True, as_index=False).agg(
        name=("name", "last"),
        first_fill_date=("fill_date", "min"),
        last_fill_date=("fill_date", "max"),
        fill_count=("fill_id", "size"),
        buy_qty=("buy_qty", "sum"),
        sell_qty=("sell_qty", "sum"),
        gross_buy_value=("gross_buy_value", "sum"),
        gross_sell_value=("gross_sell_value", "sum"),
        net_execution_cashflow=("net_execution_cashflow", "sum"),
        trading_cost=("transaction_cost", "sum"),
        gross_mark_to_last_fill_close_pnl=("gross_mark_to_last_fill_close_pnl", "sum"),
        net_mark_to_last_fill_close_pnl=("net_mark_to_last_fill_close_pnl", "sum"),
    )
    grouped["valuation_date"] = last_fill
    grouped["attribution_basis"] = "LAST_FILL_DATE_OFFICIAL_CLOSE"
    grouped["buy_qty"] = grouped["buy_qty"].astype("int64")
    grouped["sell_qty"] = grouped["sell_qty"].astype("int64")
    grouped = grouped[EXECUTION_WINDOW_ATTRIBUTION_COLUMNS]

    gross_pnl = float(grouped["gross_mark_to_last_fill_close_pnl"].sum())
    costs = float(grouped["trading_cost"].sum())
    net_pnl = float(grouped["net_mark_to_last_fill_close_pnl"].sum())
    residual = net_pnl - (gross_pnl - costs)
    tolerance = 1e-8 + 1e-12 * max(1.0, abs(gross_pnl), abs(costs), abs(net_pnl))
    if abs(residual) > tolerance:
        raise ValueError("execution-window attribution cost reconciliation failed")
    summary = {
        "status": "PASS",
        "execution_window_type": (
            "SAME_DAY" if int(ledger["fill_date"].nunique()) == 1 else "MULTI_DAY"
        ),
        "first_fill_date": str(first_fill.date()),
        "last_fill_date": str(last_fill.date()),
        "execution_trading_day_count": int(ledger["fill_date"].nunique()),
        "fill_count": int(len(ledger)),
        "traded_security_count": int(grouped["ticker"].nunique()),
        "gross_buy_value": float(grouped["gross_buy_value"].sum()),
        "gross_sell_value": float(grouped["gross_sell_value"].sum()),
        "net_execution_cashflow": float(grouped["net_execution_cashflow"].sum()),
        "trading_cost": costs,
        "gross_mark_to_last_fill_close_pnl": gross_pnl,
        "net_mark_to_last_fill_close_pnl": net_pnl,
        "cost_reconciliation_residual": residual,
        "attribution_basis": "LAST_FILL_DATE_OFFICIAL_CLOSE",
        "included_in_post_rebalance_performance": False,
    }
    return grouped, summary


def validate_contribution_reconciliation(
    daily: pd.DataFrame,
    contributions: pd.DataFrame,
    summary: dict[str, Any],
    *,
    atol: float = RECONCILIATION_ATOL,
    rtol: float = RECONCILIATION_RTOL,
) -> None:
    if daily.empty or contributions.empty:
        raise ValueError("daily NAV and contributions must be non-empty for reconciliation")
    initial_nav = float(summary.get("initial_nav", np.nan))
    last_nav = float(summary.get("last_nav", np.nan))
    reported_return = float(summary.get("cum_return", np.nan))
    if not all(np.isfinite(value) for value in (initial_nav, last_nav, reported_return)) or initial_nav <= 0:
        raise ValueError("summary contains invalid reconciliation values")
    nav_return = last_nav / initial_nav - 1.0
    contribution_sum = float(
        pd.to_numeric(contributions["contribution_to_total_return"], errors="raise").sum()
    )
    canonical = {
        "start_position_value",
        "end_position_value",
        "net_intermediate_trade_cashflow",
        "dividends",
        "trading_cost_allocated",
        "period_pnl",
    }
    position_identity_max_residual: float | None = None
    position_identity_passed = True
    accounting_reconciliation: dict[str, Any] = {}
    accounting_reconciliation_passed = True
    if canonical.issubset(contributions.columns):
        canonical_values = contributions[list(canonical)].apply(pd.to_numeric, errors="raise")
        reproduced = (
            canonical_values["end_position_value"]
            - canonical_values["start_position_value"]
            - canonical_values["net_intermediate_trade_cashflow"]
            + canonical_values["dividends"]
            - canonical_values["trading_cost_allocated"]
        )
        residuals = canonical_values["period_pnl"] - reproduced
        position_identity_max_residual = float(residuals.abs().max())
        position_identity_passed = bool(
            np.allclose(
                canonical_values["period_pnl"].to_numpy(dtype=float),
                reproduced.to_numpy(dtype=float),
                atol=atol,
                rtol=rtol,
            )
        )
        start_holdings = float(canonical_values["start_position_value"].sum())
        end_holdings = float(canonical_values["end_position_value"].sum())
        allocated_costs = float(canonical_values["trading_cost_allocated"].sum())
        net_position_pnl = float(canonical_values["period_pnl"].sum())
        gross_position_pnl_before_costs = net_position_pnl + allocated_costs
        start_cash = float(summary.get("start_cash", summary.get("opening_cash", np.nan)))
        end_cash = float(summary.get("end_cash", summary.get("last_cash", np.nan)))
        cash_pnl = float(summary.get("cash_pnl", 0.0))
        total_portfolio_pnl = last_nav - initial_nav
        start_nav_residual = start_holdings + start_cash - initial_nav
        end_nav_residual = end_holdings + end_cash - last_nav
        pnl_residual = (
            gross_position_pnl_before_costs
            + cash_pnl
            - allocated_costs
            - total_portfolio_pnl
        )
        accounting_tolerance = float(atol + rtol * max(1.0, abs(initial_nav), abs(last_nav)))
        accounting_reconciliation_passed = bool(
            all(np.isfinite(value) for value in (start_cash, end_cash))
            and abs(start_nav_residual) <= accounting_tolerance
            and abs(end_nav_residual) <= accounting_tolerance
            and abs(pnl_residual) <= accounting_tolerance
        )
        accounting_reconciliation = {
            "start_holdings_value": start_holdings,
            "start_cash": start_cash,
            "start_nav": initial_nav,
            "start_nav_reconciliation_residual": start_nav_residual,
            "end_holdings_value": end_holdings,
            "end_cash": end_cash,
            "end_nav": last_nav,
            "end_nav_reconciliation_residual": end_nav_residual,
            "gross_position_pnl_before_costs": gross_position_pnl_before_costs,
            "cash_pnl": cash_pnl,
            "allocated_trading_costs": allocated_costs,
            "net_position_pnl": net_position_pnl,
            "total_portfolio_pnl": total_portfolio_pnl,
            "pnl_reconciliation_residual": pnl_residual,
            "accounting_reconciliation_tolerance": accounting_tolerance,
            "accounting_reconciliation_status": (
                "PASS" if accounting_reconciliation_passed else "FAIL"
            ),
        }
    return_residual = reported_return - nav_return
    contribution_residual = reported_return - contribution_sum
    tolerance = float(atol + rtol * max(1.0, abs(reported_return)))
    passed = (
        abs(return_residual) <= tolerance
        and abs(contribution_residual) <= tolerance
        and position_identity_passed
        and accounting_reconciliation_passed
    )
    summary.update(
        {
            "portfolio_cum_return_recomputed": nav_return,
            "contribution_sum": contribution_sum,
            "nav_return_reconciliation_residual": return_residual,
            "contribution_reconciliation_residual": contribution_residual,
            "contribution_reconciliation_tolerance": tolerance,
            "position_pnl_identity_max_residual": position_identity_max_residual,
            "position_pnl_identity_status": "PASS" if position_identity_passed else "FAIL",
            **accounting_reconciliation,
            "contribution_reconciliation_status": "PASS" if passed else "FAIL",
        }
    )
    if not passed:
        raise ValueError(
            "performance contribution reconciliation failed: "
            f"reported={reported_return} nav={nav_return} contribution_sum={contribution_sum} "
            f"tolerance={tolerance}"
        )


def validate_static_capital_contract(
    positions: pd.DataFrame,
    basis: PerformanceCostBasis,
    total_capital: float,
    *,
    end_date: str | pd.Timestamp | None = None,
    allow_conditional_interval: bool = False,
) -> None:
    conditional_allowed = (
        allow_conditional_interval
        and basis.source_type == USER_CONFIRMED_STATEMENT_SOURCE_TYPE
        and basis.production_ready is False
        and (basis.metadata or {}).get("opening_basis_proven") is True
        and str((basis.metadata or {}).get("interval_activity_status", "")).upper()
        == "UNKNOWN"
        and str(
            (basis.metadata or {}).get("performance_calculation_status", "")
        ).upper()
        == "CONDITIONAL_STATIC_SHADOW_LIABILITY"
    )
    if not conditional_allowed:
        validate_certified_interval_contract(
            basis,
            end_date if end_date is not None else basis.basis_date,
        )
    invested = float(pd.to_numeric(positions["base_value"], errors="raise").sum())
    cash = float(basis.opening_cash if basis.opening_cash is not None else np.nan)
    opening_nav = float(basis.opening_nav if basis.opening_nav is not None else np.nan)
    supplied_capital = float(total_capital)
    if not all(np.isfinite(value) for value in (invested, cash, opening_nav, supplied_capital)) or opening_nav <= 0:
        raise ValueError("invalid static capital contract")
    if not np.isclose(supplied_capital, opening_nav, atol=1e-8, rtol=1e-12):
        raise ValueError(
            "--total_capital may only verify, never define, certified opening_nav: "
            f"argument={supplied_capital} certified={opening_nav}"
        )
    expected = invested + cash
    tolerance = 1e-8 + 1e-12 * max(1.0, abs(opening_nav))
    if abs(expected - opening_nav) > tolerance:
        raise ValueError(
            "certified snapshot market value plus certified opening_cash must equal opening_nav: "
            f"invested={invested} cash={cash} expected={expected} opening_nav={opening_nav}"
        )
