from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable

from scripts.financials.resolve_reporting_periods import actual_days


@dataclass(frozen=True)
class ShareCountResolution:
    weighted_average_shares: float
    source_tier: str
    disclosed_eps: float | None
    implied_unrounded: float | None
    integer_candidate_min: int | None
    integer_candidate_max: int | None
    selected_integer_candidate: int | None
    disclosed_eps_reproduction_error: float | None
    listed_shares_reference: float | None
    listed_shares_relative_difference: float | None
    reconciliation_status: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CapitalEvent:
    security_id: str
    event_type: str
    announcement_date: str | None
    effective_date: str
    record_date: str | None
    old_share_count: float
    new_share_count: float
    ordinary_share_change: float
    preferred_share_change: float = 0.0
    treasury_share_change: float = 0.0
    retroactive_adjustment_factor: float = 1.0
    source_receipt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def listed_shares_from_market_cap(*, market_cap: float, close_price: float) -> int:
    if market_cap <= 0 or close_price <= 0:
        raise ValueError("positive official market cap and close price are required")
    raw = market_cap / close_price
    candidate = int(Decimal(str(raw)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if abs(candidate * close_price - market_cap) > max(close_price, market_cap * 1e-6):
        raise ValueError("market-cap divided by close does not reconcile to an integer share count")
    return candidate


def _eps_quantum(disclosed_eps: float, decimals: str | int | None) -> Decimal:
    if decimals is not None and str(decimals).lstrip("-").isdigit():
        places = int(decimals)
        return Decimal(10) ** Decimal(-places)
    text = format(float(disclosed_eps), ".12f").rstrip("0").rstrip(".")
    places = len(text.split(".", 1)[1]) if "." in text else 0
    return Decimal(10) ** Decimal(-places)


def implied_weighted_shares_from_disclosed_eps(
    *, numerator: float, disclosed_eps: float, eps_decimals: str | int | None = None
) -> ShareCountResolution:
    if numerator == 0 or disclosed_eps == 0 or numerator * disclosed_eps <= 0:
        raise ValueError("numerator and disclosed EPS must be non-zero with the same sign")
    numerator_abs = Decimal(str(abs(numerator)))
    eps_abs = Decimal(str(abs(disclosed_eps)))
    quantum = _eps_quantum(disclosed_eps, eps_decimals)
    half = quantum / Decimal(2)
    lower_eps = eps_abs - half
    upper_eps = eps_abs + half
    if lower_eps <= 0:
        raise ValueError("disclosed EPS rounding interval crosses zero")
    lower_shares = numerator_abs / upper_eps
    upper_shares = numerator_abs / lower_eps
    candidate_min = math.ceil(float(lower_shares))
    candidate_max = math.floor(float(upper_shares))
    if candidate_min > candidate_max:
        raise ValueError("no integer weighted-share candidate reproduces disclosed EPS")
    implied = float(numerator_abs / eps_abs)
    selected = min(max(int(Decimal(str(implied)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)), candidate_min), candidate_max)
    reproduced = float(numerator) / selected
    displayed = float(
        Decimal(str(reproduced)).quantize(quantum, rounding=ROUND_HALF_UP)
    )
    error = abs(displayed - float(disclosed_eps))
    if error > float(quantum) / 100:
        raise ValueError("selected weighted-share candidate does not reproduce disclosed EPS")
    return ShareCountResolution(
        weighted_average_shares=float(selected),
        source_tier="DISCLOSED_EPS_INVERSE_INTEGER_WITH_ROUNDING_INTERVAL",
        disclosed_eps=float(disclosed_eps),
        implied_unrounded=implied,
        integer_candidate_min=candidate_min,
        integer_candidate_max=candidate_max,
        selected_integer_candidate=selected,
        disclosed_eps_reproduction_error=error,
        listed_shares_reference=None,
        listed_shares_relative_difference=None,
        reconciliation_status="PASS_DISCLOSED_EPS_ROUNDING_INTERVAL",
    )


def exact_weighted_shares(
    *, shares: float, listed_shares_reference: float | None = None,
    source_tier: str = "XBRL_NUMERIC_FACT",
) -> ShareCountResolution:
    if shares <= 0:
        raise ValueError("weighted-average shares must be positive")
    relative = (
        abs(float(shares) - listed_shares_reference) / listed_shares_reference
        if listed_shares_reference and listed_shares_reference > 0
        else None
    )
    return ShareCountResolution(
        weighted_average_shares=float(shares),
        source_tier=source_tier,
        disclosed_eps=None,
        implied_unrounded=None,
        integer_candidate_min=None,
        integer_candidate_max=None,
        selected_integer_candidate=None,
        disclosed_eps_reproduction_error=None,
        listed_shares_reference=listed_shares_reference,
        listed_shares_relative_difference=relative,
        reconciliation_status="PASS_XBRL_NUMERIC_FACT",
    )


def attach_listed_share_reconciliation(
    resolution: ShareCountResolution, *, listed_shares_reference: float | None
) -> ShareCountResolution:
    relative = (
        abs(resolution.weighted_average_shares - listed_shares_reference) / listed_shares_reference
        if listed_shares_reference and listed_shares_reference > 0
        else None
    )
    payload = resolution.to_dict()
    payload["listed_shares_reference"] = listed_shares_reference
    payload["listed_shares_relative_difference"] = relative
    payload["reconciliation_status"] = (
        resolution.reconciliation_status
        + ("_AND_OFFICIAL_LISTED_SHARES_CHECKED" if listed_shares_reference else "_NO_LISTED_SHARE_REFERENCE")
    )
    return ShareCountResolution(**payload)


def infer_retroactive_adjustment_factor(
    *,
    original_eps: float | None,
    latest_comparative_eps: float | None,
    original_numerator: float | None,
    latest_comparative_numerator: float | None,
    tolerance: float = 0.01,
) -> tuple[float, str]:
    if not all(value is not None for value in (original_eps, latest_comparative_eps, original_numerator, latest_comparative_numerator)):
        return 1.0, "NOT_OBSERVABLE"
    if original_eps == 0 or latest_comparative_eps == 0:
        return 1.0, "ZERO_EPS_NO_FACTOR"
    numerator_scale = max(abs(float(original_numerator)), abs(float(latest_comparative_numerator)), 1.0)
    if abs(float(original_numerator) - float(latest_comparative_numerator)) / numerator_scale > tolerance:
        return 1.0, "NUMERATOR_CHANGED_NO_PURE_SHARE_FACTOR"
    raw = abs(float(original_eps) / float(latest_comparative_eps))
    if abs(raw - 1.0) <= tolerance:
        return 1.0, "NO_RETROACTIVE_CHANGE"
    canonical = (0.1, 0.2, 0.25, 0.5, 2.0, 3.0, 4.0, 5.0, 10.0)
    nearest = min(canonical, key=lambda value: abs(value - raw))
    if abs(raw - nearest) / nearest > tolerance:
        return 1.0, "NON_CANONICAL_FACTOR_UNRESOLVED"
    return nearest, "PASS_COMPARATIVE_EPS_RETROACTIVE_FACTOR"


def weighted_average_from_events(
    *, period_start: str, period_end: str, opening_outstanding_shares: float, events: Iterable[CapitalEvent]
) -> float:
    start = date.fromisoformat(period_start)
    end = date.fromisoformat(period_end)
    if opening_outstanding_shares <= 0:
        raise ValueError("opening outstanding shares must be positive")
    current_date = start
    current_shares = float(opening_outstanding_shares)
    share_days = 0.0
    for event in sorted(events, key=lambda item: item.effective_date):
        effective = date.fromisoformat(event.effective_date)
        if effective < start or effective > end:
            continue
        segment_days = (effective - current_date).days
        share_days += current_shares * segment_days
        current_shares = float(event.new_share_count) - float(event.treasury_share_change)
        current_date = effective
    share_days += current_shares * ((end - current_date).days + 1)
    return share_days / actual_days(start, end)
