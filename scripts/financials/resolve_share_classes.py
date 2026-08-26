from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class ShareClassResolution:
    ticker: str
    selected_share_class: str
    active_listed_share_classes: tuple[str, ...]
    active_preferred_class_count: int
    active_participating_class_count: int
    active_convertible_preferred_class_count: int
    numerator_parent_profit_fallback_allowed: bool
    price_eps_class_match: bool
    market_cap_fallback_compatible: bool
    method: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["active_listed_share_classes"] = "|".join(self.active_listed_share_classes)
        return payload


def _issuer_base(name: object) -> str:
    text = re.sub(r"\s+", "", str(name))
    text = re.sub(r"(?:제\d+)?우(?:B)?$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"(?:1우|2우|3우)$", "", text)
    return text


def _latest_stock_status_snapshot(stock_status: pd.DataFrame) -> pd.DataFrame:
    frame = stock_status.copy()
    if frame.empty or not {"business_year", "report_code"}.issubset(frame.columns):
        return frame
    frame["_business_year"] = pd.to_numeric(frame["business_year"], errors="coerce")
    report_order = {"11013": 1, "11012": 2, "11014": 3, "11011": 4}
    frame["_report_order"] = frame["report_code"].astype(str).str.replace(r"\.0$", "", regex=True).map(report_order)
    eligible = frame.loc[frame["_business_year"].notna() & frame["_report_order"].notna()]
    if eligible.empty:
        return frame.drop(columns=["_business_year", "_report_order"])
    latest = eligible[["_business_year", "_report_order"]].drop_duplicates().sort_values(
        ["_business_year", "_report_order"]
    ).iloc[-1]
    return eligible.loc[
        eligible["_business_year"].eq(latest["_business_year"])
        & eligible["_report_order"].eq(latest["_report_order"])
    ].drop(columns=["_business_year", "_report_order"])


def allocate_profit_to_participating_classes(
    *,
    profit_available_to_common_and_participating_classes: float,
    weighted_shares_by_class: dict[str, float],
    dividend_rights_by_class: dict[str, float] | None = None,
    preferred_dividends_by_class: dict[str, float] | None = None,
) -> dict[str, float]:
    """Allocate preferred dividends first, then residual profit by participating rights."""
    if not weighted_shares_by_class:
        raise ValueError("at least one participating share class is required")
    rights = dividend_rights_by_class or {key: 1.0 for key in weighted_shares_by_class}
    if set(rights) != set(weighted_shares_by_class):
        raise ValueError("share-class and dividend-right keys must match")
    preferred = preferred_dividends_by_class or {key: 0.0 for key in weighted_shares_by_class}
    if set(preferred) != set(weighted_shares_by_class):
        raise ValueError("share-class and preferred-dividend keys must match")
    if any(float(value) < 0 for value in preferred.values()):
        raise ValueError("preferred dividends cannot be negative")
    weights = {
        key: float(weighted_shares_by_class[key]) * float(rights[key])
        for key in weighted_shares_by_class
    }
    if any(value < 0 for value in weights.values()) or sum(weights.values()) <= 0:
        raise ValueError("participating class weights must be non-negative and non-zero")
    total = sum(weights.values())
    residual = float(profit_available_to_common_and_participating_classes) - sum(
        float(value) for value in preferred.values()
    )
    return {
        key: float(preferred[key]) + residual * value / total
        for key, value in weights.items()
    }


def resolve_share_classes(
    *, ticker: str, name: str, security_master: pd.DataFrame, stock_status: pd.DataFrame | None = None
) -> ShareClassResolution:
    master = security_master.copy()
    master["ticker"] = master["ticker"].astype(str).str.zfill(6)
    selected = master.loc[master["ticker"].eq(str(ticker).zfill(6))]
    if len(selected) != 1:
        raise ValueError("selected security must map to exactly one security-master row")
    selected_type = str(selected.iloc[0].get("security_type", "")).lower()
    selected_class = "PREFERRED" if "preferred" in selected_type or "우선" in selected_type else "ORDINARY"
    base = _issuer_base(name)
    related = master.loc[master["name"].map(_issuer_base).eq(base)].copy()
    preferred = related.loc[
        related["security_type"].astype(str).str.lower().str.contains("preferred|우선", regex=True)
    ]
    active_preferred = len(preferred)
    active_participating = 0
    active_convertible = 0
    if stock_status is not None and not stock_status.empty and "se" in stock_status:
        stock_status = _latest_stock_status_snapshot(stock_status)
        preferred_rows = stock_status.loc[
            stock_status["se"].astype(str).str.lower().str.contains("우선|preferred", regex=True)
        ].copy()
        active_preferred = 0
        if not preferred_rows.empty and "distb_stock_co" in preferred_rows:
            outstanding = pd.to_numeric(
                preferred_rows["distb_stock_co"].astype(str).str.replace(",", "", regex=False), errors="coerce"
            ).fillna(0)
            issued_source = preferred_rows.get("istc_totqy", preferred_rows["distb_stock_co"])
            issued = pd.to_numeric(
                issued_source.astype(str).str.replace(",", "", regex=False), errors="coerce"
            ).fillna(0)
            if "rights_effective" in preferred_rows:
                rights_effective = preferred_rows["rights_effective"].astype(str).str.lower().isin(
                    {"true", "1", "yes", "유효"}
                )
            else:
                rights_effective = pd.Series(True, index=preferred_rows.index)
            active_mask = issued.gt(0) & outstanding.gt(0) & rights_effective
            active_preferred = int(active_mask.sum())
            rights_text = preferred_rows.astype(str).agg(" ".join, axis=1).str.lower()
            active_participating = int(
                (active_mask & rights_text.str.contains("참가|participating") & ~rights_text.str.contains("비참가|nonparticipating")).sum()
            )
            active_convertible = int((active_mask & rights_text.str.contains("전환|convertible")).sum())
    classes = ["ORDINARY"]
    if active_preferred:
        classes.append("PREFERRED_ACTIVE_ECONOMIC_RIGHTS")
    if active_participating:
        classes.append("PREFERRED_PARTICIPATING")
    if active_convertible:
        classes.append("PREFERRED_CONVERTIBLE")
    single_ordinary = selected_class == "ORDINARY" and active_preferred == 0
    return ShareClassResolution(
        ticker=str(ticker).zfill(6),
        selected_share_class=selected_class,
        active_listed_share_classes=tuple(classes),
        active_preferred_class_count=active_preferred,
        active_participating_class_count=active_participating,
        active_convertible_preferred_class_count=active_convertible,
        numerator_parent_profit_fallback_allowed=single_ordinary,
        price_eps_class_match=selected_class in {"ORDINARY", "PREFERRED"},
        market_cap_fallback_compatible=single_ordinary,
        method=(
            "SINGLE_ORDINARY_SECURITY_MASTER_AND_DART_STOCK_STATUS"
            if single_ordinary
            else "SELECTED_CLASS_PRICE_EPS_WITH_OTHER_LISTED_CLASS"
        ),
    )
