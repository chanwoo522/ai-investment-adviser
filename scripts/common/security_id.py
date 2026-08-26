from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import pandas as pd

_VALID_KRX_ID = re.compile(r"^[A-Z0-9]{6}$")
_CSV_FLOAT = re.compile(r"^(\d{1,6})\.0$")
_DIGITS = re.compile(r"^\d{1,6}$")


class InvalidSecurityId(ValueError):
    pass


class SecurityIdCollision(ValueError):
    pass


@dataclass(frozen=True)
class SecurityIdAudit:
    rows: int
    non_null_before: int
    non_null_after: int
    unique_before: int
    unique_after: int
    invalid_count: int
    collision_count: int


def normalize_security_id(value: Any, *, allow_missing: bool = False) -> str | pd.NA:
    """Normalize a KRX security identifier without destroying its identity.

    Numeric codes are zero-padded to six characters. A trailing ``.0`` is
    removed only for an otherwise numeric CSV artifact. Alphanumeric codes are
    uppercased and preserved. No digit extraction is performed.
    """
    if value is None or pd.isna(value):
        if allow_missing:
            return pd.NA
        raise InvalidSecurityId("missing security identifier")
    raw = str(value).strip().upper()
    if not raw:
        if allow_missing:
            return pd.NA
        raise InvalidSecurityId("empty security identifier")
    match = _CSV_FLOAT.fullmatch(raw)
    if match:
        raw = match.group(1)
    if _DIGITS.fullmatch(raw):
        return raw.zfill(6)
    if _VALID_KRX_ID.fullmatch(raw) and any(c.isalpha() for c in raw):
        return raw
    raise InvalidSecurityId(f"invalid KRX security identifier: {value!r}")


def normalize_security_id_series(series: pd.Series, *, allow_missing: bool = False,
                                 detect_collisions: bool = True) -> pd.Series:
    normalized: list[str | pd.NA] = []
    invalid: list[tuple[object, str]] = []
    origins: dict[str, set[str]] = {}
    for idx, value in series.items():
        try:
            result = normalize_security_id(value, allow_missing=allow_missing)
        except InvalidSecurityId as exc:
            invalid.append((idx, str(exc)))
            result = pd.NA
        normalized.append(result)
        if not pd.isna(result):
            raw = str(value).strip().upper()
            origins.setdefault(str(result), set()).add(raw)
    if invalid and not allow_missing:
        sample = "; ".join(f"index={i}: {reason}" for i, reason in invalid[:5])
        raise InvalidSecurityId(f"{len(invalid)} invalid security identifiers; {sample}")
    if detect_collisions:
        collisions = {key: vals for key, vals in origins.items() if len(vals) > 1 and not _equivalent_numeric_forms(vals)}
        if collisions:
            sample = list(collisions.items())[:5]
            raise SecurityIdCollision(f"security identifier normalization collision(s): {sample}")
    return pd.Series(normalized, index=series.index, dtype="string", name=series.name)


def _equivalent_numeric_forms(values: set[str]) -> bool:
    canonical = set()
    for raw in values:
        match = _CSV_FLOAT.fullmatch(raw)
        if match:
            raw = match.group(1)
        if not _DIGITS.fullmatch(raw):
            return False
        canonical.add(raw.lstrip("0") or "0")
    return len(canonical) == 1


def audit_security_ids(before: pd.Series, after: pd.Series) -> SecurityIdAudit:
    return SecurityIdAudit(
        rows=len(before),
        non_null_before=int(before.notna().sum()),
        non_null_after=int(after.notna().sum()),
        unique_before=int(before.dropna().astype(str).str.strip().str.upper().nunique()),
        unique_after=int(after.dropna().nunique()),
        invalid_count=int(after.isna().sum() - before.isna().sum()),
        collision_count=max(0, int(before.dropna().astype(str).str.strip().str.upper().nunique() - after.dropna().nunique())),
    )
