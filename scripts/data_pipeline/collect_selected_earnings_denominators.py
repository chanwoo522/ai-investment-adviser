from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable
from zipfile import ZipFile

import pandas as pd
import requests
from lxml import etree


INFORMATION_ASOF = "2026-08-18"
PRICE_ASOF = "2026-08-18"
REPORT_CODES = {"FY2025": "11011", "H1_2025": "11012", "H1_2026": "11012"}
REPORT_PERIODS = {
    "FY2025": (date(2025, 1, 1), date(2025, 12, 31)),
    "H1_2025": (date(2025, 1, 1), date(2025, 6, 30)),
    "H1_2026": (date(2026, 1, 1), date(2026, 6, 30)),
    "H1_2025_COMPARATIVE": (date(2025, 1, 1), date(2025, 6, 30)),
}
EXPECTED_TICKERS = (
    "098460",
    "322000",
    "005930",
    "080220",
    "253450",
    "327260",
    "121600",
    "232140",
    "025560",
    "219130",
)

STANDARD_NET_INCOME = ("ProfitLossAttributableToOwnersOfParent",)
STANDARD_BASIC_EPS = ("BasicEarningsLossPerShare",)
STANDARD_BASIC_NUMERATOR = (
    "ProfitLossAttributableToOrdinaryEquityHoldersOfParentEntityUsedInCalculatingBasicEarningsPerShare",
    "ProfitLossAttributableToOrdinaryEquityHoldersOfParentEntity",
    "ProfitLossAttributableToOwnersOfParent",
)
STANDARD_WEIGHTED_SHARES = (
    "WeightedAverageNumberOfOrdinarySharesOutstandingBasic",
    "WeightedAverageNumberOfOrdinarySharesUsedInCalculationOfBasicEarningsPerShare",
    "WeightedAverageNumberOfOrdinarySharesUsedInCalculatingBasicEarningsPerShare",
    "WeightedAverageShares",
)

EXACT_WEIGHTED_SHARE_LABELS = {
    "가중평균유통보통주식수",
    "가중평균유통주식수",
    "가중평균보통주식수",
    "weightedaveragenumberofordinarysharesoutstanding",
    "weightedaveragenumberofordinaryshares",
    "weightedaverageshares",
}

XLINK = "http://www.w3.org/1999/xlink"
XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDI = "http://xbrl.org/2006/xbrldi"


class EarningsContractError(RuntimeError):
    pass


def ttm_dependency_contract(year: int, quarter: int) -> dict[str, Any]:
    if quarter not in {1, 2, 3, 4}:
        raise ValueError("quarter must be 1, 2, 3, or 4")
    if quarter == 4:
        return {
            "quarter": quarter,
            "reports": [f"FY{year}"],
            "formula": "CURRENT_FY",
            "future_quarter_dependencies": [],
        }
    period = {1: "Q1", 2: "H1", 3: "M9"}[quarter]
    return {
        "quarter": quarter,
        "reports": [f"FY{year - 1}", f"{period}_{year}", f"{period}_{year - 1}_COMPARATIVE"],
        "formula": f"FY{year - 1} + {period}_{year} - {period}_{year - 1}",
        "future_quarter_dependencies": [],
    }


def ttm_ytd_bridge(fy_value: float, current_ytd_value: float, prior_ytd_value: float) -> float:
    return fy_value + current_ytd_value - prior_ytd_value


def share_day_ttm(
    *,
    fy_shares: float,
    fy_start: date,
    fy_end: date,
    current_ytd_shares: float,
    current_ytd_start: date,
    current_ytd_end: date,
    prior_ytd_shares: float,
    prior_ytd_start: date,
    prior_ytd_end: date,
) -> tuple[float, float, int]:
    fy_days = _days(fy_start, fy_end)
    current_days = _days(current_ytd_start, current_ytd_end)
    prior_days = _days(prior_ytd_start, prior_ytd_end)
    ttm_days = fy_days - prior_days + current_days
    share_days = fy_shares * fy_days - prior_ytd_shares * prior_days + current_ytd_shares * current_days
    return share_days / ttm_days, share_days, ttm_days


def select_per_method(
    *, price: float, eps_ttm: float | None, market_cap_for_per: float | None, net_income_ttm: float | None
) -> tuple[float | None, str | None, str]:
    if (eps_ttm is not None and eps_ttm <= 0) or (
        net_income_ttm is not None and net_income_ttm <= 0
    ):
        return None, None, "LOSS"
    if eps_ttm is not None:
        return price / eps_ttm, "PRICE_DIV_EPS", "PASS"
    if market_cap_for_per is not None and net_income_ttm is not None:
        return market_cap_for_per / net_income_ttm, "MCAP_DIV_NET_INCOME", "PASS"
    return None, None, "NA"


@dataclass(frozen=True)
class Context:
    identifier: str
    start: date | None
    end: date | None
    instant: date | None
    dimensions: tuple[tuple[str, str], ...]

    @property
    def scope(self) -> str | None:
        members = {member for _, member in self.dimensions}
        if "ConsolidatedMember" in members:
            return "CFS"
        if "SeparateMember" in members:
            return "OFS"
        return None

    @property
    def share_class(self) -> str | None:
        for axis, member in self.dimensions:
            if axis == "ClassesOfShareCapitalAxis":
                return member
        return None


@dataclass(frozen=True)
class Fact:
    local_name: str
    namespace: str
    value: Decimal
    context: Context
    context_ref: str
    unit_ref: str
    decimals: str | None


@dataclass(frozen=True)
class Instance:
    receipt_no: str
    receipt_date: str
    report_key: str
    business_year: int
    report_code: str
    taxonomy_version: str
    schema_refs: tuple[str, ...]
    source_sha256: str
    source_zip: Path
    contexts: dict[str, Context]
    facts: tuple[Fact, ...]
    units: dict[str, tuple[str, ...]]
    labels: dict[str, tuple[str, ...]]
    presentation_roles: dict[str, tuple[str, ...]]


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_json(path: Path, payload: dict[str, Any]) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _safe_csv(path: Path, frame: pd.DataFrame) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _api_json(session: requests.Session, endpoint: str, params: dict[str, str]) -> dict[str, Any]:
    try:
        response = session.get(
            f"https://opendart.fss.or.kr/api/{endpoint}", params=params, timeout=40
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        raise EarningsContractError(
            f"OpenDART request failed at {endpoint}: {type(exc).__name__}"
        ) from None
    status = str(payload.get("status", ""))
    if status != "000":
        raise EarningsContractError(
            f"OpenDART {endpoint} status={status} message={payload.get('message')}"
        )
    return payload


def _api_xbrl(session: requests.Session, key: str, receipt_no: str) -> bytes:
    try:
        response = session.get(
            "https://opendart.fss.or.kr/api/fnlttXbrl.xml",
            params={"crtfc_key": key, "rcept_no": receipt_no},
            timeout=60,
        )
        response.raise_for_status()
        payload = response.content
        with ZipFile(io.BytesIO(payload)) as archive:
            if not any(name.lower().endswith(".xbrl") for name in archive.namelist()):
                raise EarningsContractError(f"XBRL instance missing for receipt {receipt_no}")
        return payload
    except EarningsContractError:
        raise
    except Exception as exc:
        raise EarningsContractError(
            f"OpenDART XBRL request failed for receipt {receipt_no}: {type(exc).__name__}"
        ) from None


def _canonical_report_name(value: object) -> str:
    return re.sub(r"\s+", "", str(value)).replace("[기재정정]", "").replace("[첨부정정]", "")


def _select_receipts(
    session: requests.Session, key: str, corp_code: str, information_asof: str
) -> dict[str, list[dict[str, str]]]:
    payload = _api_json(
        session,
        "list.json",
        {
            "crtfc_key": key,
            "corp_code": corp_code,
            "bgn_de": "20250101",
            "end_de": information_asof.replace("-", ""),
            "pblntf_ty": "A",
            "page_no": "1",
            "page_count": "100",
        },
    )
    patterns = {
        "FY2025": re.compile(r"사업보고서\(2025\.12\)"),
        "H1_2025": re.compile(r"반기보고서\(2025\.06\)"),
        "H1_2026": re.compile(r"반기보고서\(2026\.06\)"),
    }
    selected: dict[str, list[dict[str, str]]] = {}
    for report_key, pattern in patterns.items():
        candidates = []
        for row in payload.get("list", []) or []:
            receipt = str(row.get("rcept_no", ""))
            if not re.fullmatch(r"\d{14}", receipt):
                continue
            receipt_date = f"{receipt[:4]}-{receipt[4:6]}-{receipt[6:8]}"
            if receipt_date > information_asof:
                continue
            if pattern.search(_canonical_report_name(row.get("report_nm"))):
                candidates.append(
                    {
                        "receipt_no": receipt,
                        "receipt_date": receipt_date,
                        "report_name": str(row.get("report_nm", "")),
                    }
                )
        if not candidates:
            raise EarningsContractError(f"required filing missing: corp={corp_code} report={report_key}")
        selected[report_key] = sorted(
            candidates, key=lambda item: item["receipt_no"], reverse=True
        )
    return selected


def _latest_receipt_with_xbrl(
    session: requests.Session,
    key: str,
    candidates: list[dict[str, str]],
) -> tuple[dict[str, str], bytes, list[dict[str, str]]]:
    rejected: list[dict[str, str]] = []
    for candidate in candidates:
        try:
            return candidate, _api_xbrl(session, key, candidate["receipt_no"]), rejected
        except EarningsContractError as exc:
            rejected.append(
                {
                    "receipt_no": candidate["receipt_no"],
                    "receipt_date": candidate["receipt_date"],
                    "reason": str(exc),
                }
            )
    raise EarningsContractError(
        "no XBRL-bearing receipt among as-of eligible filing candidates"
    )


def _local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1].rsplit(":", 1)[-1]


def _parse_date(text: str | None) -> date | None:
    return date.fromisoformat(text.strip()) if text else None


def _context_map(root: etree._Element) -> dict[str, Context]:
    contexts: dict[str, Context] = {}
    for node in root.findall(f"{{{XBRLI}}}context"):
        identifier = str(node.get("id"))
        period = node.find(f"{{{XBRLI}}}period")
        start = _parse_date(period.findtext(f"{{{XBRLI}}}startDate")) if period is not None else None
        end = _parse_date(period.findtext(f"{{{XBRLI}}}endDate")) if period is not None else None
        instant = _parse_date(period.findtext(f"{{{XBRLI}}}instant")) if period is not None else None
        dimensions: list[tuple[str, str]] = []
        for member in node.findall(f".//{{{XBRLDI}}}explicitMember"):
            dimensions.append((_local_name(str(member.get("dimension"))), _local_name(member.text or "")))
        contexts[identifier] = Context(identifier, start, end, instant, tuple(dimensions))
    return contexts


def _unit_map(root: etree._Element) -> dict[str, tuple[str, ...]]:
    units: dict[str, tuple[str, ...]] = {}
    for node in root.findall(f"{{{XBRLI}}}unit"):
        measures = tuple(_local_name(text) for text in node.xpath(".//*[local-name()='measure']/text()"))
        units[str(node.get("id"))] = measures
    return units


def _fact_rows(root: etree._Element, contexts: dict[str, Context]) -> tuple[Fact, ...]:
    facts: list[Fact] = []
    for node in root:
        context_ref = node.get("contextRef")
        unit_ref = node.get("unitRef")
        if not context_ref or not unit_ref or context_ref not in contexts:
            continue
        text = (node.text or "").strip().replace(",", "")
        if not text:
            continue
        try:
            value = Decimal(text)
        except InvalidOperation:
            continue
        qname = etree.QName(node)
        facts.append(
            Fact(
                local_name=qname.localname,
                namespace=qname.namespace or "",
                value=value,
                context=contexts[context_ref],
                context_ref=context_ref,
                unit_ref=str(unit_ref),
                decimals=node.get("decimals"),
            )
        )
    return tuple(facts)


def _linkbase_labels(archive: ZipFile) -> dict[str, tuple[str, ...]]:
    result: dict[str, set[str]] = {}
    for name in archive.namelist():
        if "lab-" not in name.lower() or not name.lower().endswith(".xml"):
            continue
        root = etree.fromstring(archive.read(name))
        for link in root.xpath("//*[local-name()='labelLink']"):
            locators = {
                str(node.get(f"{{{XLINK}}}label")): str(node.get(f"{{{XLINK}}}href", "")).split("#")[-1]
                for node in link.xpath("./*[local-name()='loc']")
            }
            labels = {
                str(node.get(f"{{{XLINK}}}label")): " ".join("".join(node.itertext()).split())
                for node in link.xpath("./*[local-name()='label']")
            }
            for arc in link.xpath("./*[local-name()='labelArc']"):
                source = locators.get(str(arc.get(f"{{{XLINK}}}from")))
                label = labels.get(str(arc.get(f"{{{XLINK}}}to")))
                if source and label:
                    result.setdefault(source.rsplit("_", 1)[-1], set()).add(label)
    return {key: tuple(sorted(values)) for key, values in result.items()}


def _presentation_roles(archive: ZipFile) -> dict[str, tuple[str, ...]]:
    result: dict[str, set[str]] = {}
    for name in archive.namelist():
        if not name.lower().endswith("_pre.xml"):
            continue
        root = etree.fromstring(archive.read(name))
        for link in root.xpath("//*[local-name()='presentationLink']"):
            role = str(link.get(f"{{{XLINK}}}role", ""))
            for loc in link.xpath("./*[local-name()='loc']"):
                concept = str(loc.get(f"{{{XLINK}}}href", "")).split("#")[-1].rsplit("_", 1)[-1]
                if concept:
                    result.setdefault(concept, set()).add(role)
    return {key: tuple(sorted(values)) for key, values in result.items()}


def _taxonomy_version(root: etree._Element, archive: ZipFile) -> tuple[str, tuple[str, ...]]:
    refs = tuple(
        sorted(
            {
                str(node.get(f"{{{XLINK}}}href"))
                for node in root.xpath("//*[local-name()='schemaRef']")
                if node.get(f"{{{XLINK}}}href")
            }
        )
    )
    candidates = []
    for ref in refs:
        candidates.extend(re.findall(r"(?:20\d{2})[-_/](?:\d{2})[-_/](?:\d{2})|(?:20\d{2})", ref))
    for name in archive.namelist():
        if name.lower().endswith(".xsd"):
            schema = etree.fromstring(archive.read(name))
            namespace = str(schema.get("targetNamespace", ""))
            candidates.extend(re.findall(r"(?:20\d{2})[-_/](?:\d{2})[-_/](?:\d{2})|(?:20\d{2})", namespace))
    return (sorted(candidates)[-1] if candidates else "UNRESOLVED", refs)


def parse_instance(
    payload: bytes,
    *,
    receipt_no: str,
    receipt_date: str,
    report_key: str,
    business_year: int,
    report_code: str,
    source_zip: Path,
) -> Instance:
    with ZipFile(io.BytesIO(payload)) as archive:
        instance_names = [name for name in archive.namelist() if name.lower().endswith(".xbrl")]
        if len(instance_names) != 1:
            raise EarningsContractError(f"expected one XBRL instance for receipt {receipt_no}")
        root = etree.fromstring(archive.read(instance_names[0]))
        contexts = _context_map(root)
        units = _unit_map(root)
        facts = _fact_rows(root, contexts)
        labels = _linkbase_labels(archive)
        roles = _presentation_roles(archive)
        taxonomy_version, refs = _taxonomy_version(root, archive)
    return Instance(
        receipt_no=receipt_no,
        receipt_date=receipt_date,
        report_key=report_key,
        business_year=business_year,
        report_code=report_code,
        taxonomy_version=taxonomy_version,
        schema_refs=refs,
        source_sha256=sha256_bytes(payload),
        source_zip=source_zip,
        contexts=contexts,
        facts=facts,
        units=units,
        labels=labels,
        presentation_roles=roles,
    )


def _unit_kind(instance: Instance, fact: Fact) -> str:
    text = "|".join((fact.unit_ref, *instance.units.get(fact.unit_ref, ()))).upper()
    if "EPS" in text or ("KRW" in text and ("SHARE" in text or "SHARES" in text)):
        return "KRW_PER_SHARE"
    if "SHARE" in text or "SHARES" in text:
        return "SHARES"
    if "KRW" in text:
        return "KRW"
    return "OTHER"


def _standard_namespace(namespace: str) -> bool:
    lowered = namespace.lower()
    return "ifrs" in lowered or "dart.fss.or.kr/xbrl/taxonomy" in lowered


def _normalized_label(value: str) -> str:
    return re.sub(r"[\s()\-_/]", "", value).lower()


def _select_fact(
    instance: Instance,
    *,
    concept_candidates: Iterable[str],
    period_start: date,
    period_end: date,
    scope: str,
    unit_kind: str,
    share_preference: str,
    allow_label_extension: bool = False,
) -> tuple[Fact | None, str, str]:
    candidate_order = {name: index for index, name in enumerate(concept_candidates)}
    matches: list[tuple[tuple[int, int, int, int], Fact, str]] = []
    preferred_member = "OrdinarySharesMember" if share_preference in {"ORDINARY", "NONE_OR_ORDINARY"} else None
    for fact in instance.facts:
        context = fact.context
        if context.start != period_start or context.end != period_end:
            continue
        if context.scope != scope:
            continue
        if _unit_kind(instance, fact) != unit_kind:
            continue
        if share_preference == "NONE" and context.share_class is not None:
            continue
        if share_preference == "ORDINARY" and context.share_class not in {preferred_member, None}:
            continue
        if share_preference == "NONE_OR_ORDINARY" and context.share_class not in {None, preferred_member}:
            continue
        mapping = None
        concept_rank = candidate_order.get(fact.local_name)
        if concept_rank is not None:
            mapping = "STANDARD_ACCOUNT_ID_EXACT" if _standard_namespace(fact.namespace) else "ISSUER_EXTENSION_EXACT_CONCEPT"
        elif allow_label_extension:
            labels = instance.labels.get(fact.local_name, ())
            if any(_normalized_label(label) in EXACT_WEIGHTED_SHARE_LABELS for label in labels):
                concept_rank = len(candidate_order)
                mapping = "ISSUER_EXTENSION_EXACT_LABEL_ROLE_UNIT_PERIOD"
        if mapping is None or concept_rank is None:
            continue
        if not _standard_namespace(fact.namespace) and not instance.presentation_roles.get(fact.local_name):
            continue
        if share_preference == "NONE_OR_ORDINARY":
            share_rank = 0 if context.share_class is None else 1
        else:
            share_rank = 0 if context.share_class == preferred_member else 1
        extra_dimensions = len(context.dimensions)
        namespace_rank = 0 if _standard_namespace(fact.namespace) else 1
        matches.append(((concept_rank, namespace_rank, share_rank, extra_dimensions), fact, mapping))
    if not matches:
        return None, "NO_EXACT_FACT", ""
    matches.sort(key=lambda item: item[0])
    best_rank = matches[0][0]
    best = [item for item in matches if item[0] == best_rank]
    values = {item[1].value for item in best}
    if len(values) != 1:
        return None, "CONFLICTING_EXACT_FACTS", ""
    return best[0][1], "PASS", best[0][2]


def _participating_share_classes(instance: Instance) -> tuple[str, ...]:
    classes = {
        context.share_class
        for context in instance.contexts.values()
        if context.share_class is not None
    }
    return tuple(sorted(str(value) for value in classes))


def _scope_for_instances(instances: Iterable[Instance]) -> tuple[str | None, str]:
    scopes = []
    for instance in instances:
        observed = {context.scope for context in instance.contexts.values() if context.scope}
        scopes.append(observed)
    if all("CFS" in observed for observed in scopes):
        return "CFS", "PASS_CFS_PRIORITY"
    if all("CFS" not in observed and "OFS" in observed for observed in scopes):
        return "OFS", "PASS_OFS_NO_CFS_EXISTS"
    return None, "SCOPE_INCONSISTENT_ACROSS_REPORTS"


def _days(start: date, end: date) -> int:
    return (end - start).days + 1


def _fact_payload(fact: Fact | None, mapping: str, instance: Instance) -> dict[str, Any]:
    if fact is None:
        return {
            "value": None,
            "concept": None,
            "namespace": None,
            "context_ref": None,
            "unit": None,
            "mapping_method": mapping,
            "labels": None,
            "presentation_roles": None,
        }
    return {
        "value": float(fact.value),
        "concept": fact.local_name,
        "namespace": fact.namespace,
        "context_ref": fact.context_ref,
        "unit": fact.unit_ref,
        "mapping_method": mapping,
        "labels": "|".join(instance.labels.get(fact.local_name, ())),
        "presentation_roles": "|".join(instance.presentation_roles.get(fact.local_name, ())),
    }


def _period_facts(
    instance: Instance, *, period_start: date, period_end: date, scope: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    selected: dict[str, Any] = {}
    mappings: list[dict[str, Any]] = []
    net_income_concepts = STANDARD_NET_INCOME if scope == "CFS" else ("ProfitLoss",)
    net_income_share = "NONE_OR_ORDINARY" if scope == "CFS" else "NONE"
    basic_numerator_concepts = STANDARD_BASIC_NUMERATOR if scope == "CFS" else (
        "ProfitLossAttributableToOrdinaryEquityHoldersOfParentEntityUsedInCalculatingBasicEarningsPerShare",
        "ProfitLossAttributableToOrdinaryEquityHoldersOfParentEntity",
        "ProfitLoss",
    )
    requests_spec = (
        ("net_income", net_income_concepts, "KRW", net_income_share, False),
        ("basic_eps", STANDARD_BASIC_EPS, "KRW_PER_SHARE", "ORDINARY", False),
        ("basic_numerator", basic_numerator_concepts, "KRW", "ORDINARY", False),
        ("weighted_shares", STANDARD_WEIGHTED_SHARES, "SHARES", "ORDINARY", True),
    )
    for field, concepts, expected_unit, share_preference, label_extension in requests_spec:
        fact, status, mapping = _select_fact(
            instance,
            concept_candidates=concepts,
            period_start=period_start,
            period_end=period_end,
            scope=scope,
            unit_kind=expected_unit,
            share_preference=share_preference,
            allow_label_extension=label_extension,
        )
        payload = _fact_payload(fact, mapping or status, instance)
        payload["status"] = status
        selected[field] = payload
        mappings.append(
            {
                "field": field,
                "period_start": period_start.isoformat(),
                "period_end": period_end.isoformat(),
                "statement_scope": scope,
                "receipt_no": instance.receipt_no,
                "receipt_date": instance.receipt_date,
                "taxonomy_version": instance.taxonomy_version,
                **payload,
                "status": status,
            }
        )
    return selected, mappings


def _verification(period: dict[str, Any]) -> tuple[bool, float | None, float | None]:
    numerator = period["basic_numerator"]["value"]
    shares = period["weighted_shares"]["value"]
    disclosed = period["basic_eps"]["value"]
    if numerator is None or shares is None or disclosed is None or shares <= 0:
        return False, None, None
    calculated = numerator / shares
    error = abs(disclosed - calculated)
    tolerance = max(1.0, abs(disclosed) * 0.005)
    return error <= tolerance, error, tolerance


def _missing_fact_reason(
    periods: dict[str, dict[str, Any]], period_keys: Iterable[str], fields: Iterable[str]
) -> str:
    missing = []
    for period_key in period_keys:
        for field in fields:
            payload = periods.get(period_key, {}).get(field, {})
            if payload.get("status") != "PASS":
                missing.append(f"{period_key}:{field}:{payload.get('status', 'NOT_COLLECTED')}")
    return "|".join(missing)


def _comparative_restatement(
    original: dict[str, Any], latest: dict[str, Any]
) -> tuple[bool | None, str]:
    fields = ("basic_numerator", "weighted_shares", "basic_eps")
    if not all(original.get(field, {}).get("status") == "PASS" for field in fields):
        return None, "ORIGINAL_H1_EXACT_EPS_FACT_SET_UNAVAILABLE_LATEST_COMPARATIVE_USED"
    if not all(latest.get(field, {}).get("status") == "PASS" for field in fields):
        return None, "LATEST_COMPARATIVE_EXACT_EPS_FACT_SET_UNAVAILABLE"
    restated = any(original[field]["value"] != latest[field]["value"] for field in fields)
    return restated, "RESTATED_LATEST_COMPARATIVE_USED" if restated else "PASS_EXACT_MATCH"


def _load_selected(upstream_root: Path) -> pd.DataFrame:
    topk = pd.read_csv(upstream_root / "fresh_start_top_k_v2.csv", dtype={"ticker": str, "corp_code": str})
    topk["ticker"] = topk["ticker"].astype(str).str.zfill(6)
    topk["corp_code"] = topk["corp_code"].astype(str).str.zfill(8)
    topk = topk.sort_values("model_rank")
    if tuple(topk["ticker"]) != EXPECTED_TICKERS:
        raise EarningsContractError("Top-K identity/order differs from enrichment contract")
    return topk[["ticker", "name", "corp_code", "model_rank", "model_score", "target_weight"] if "target_weight" in topk else ["ticker", "name", "corp_code", "model_rank", "model_score"]].copy()


def collect(
    *,
    upstream_root: Path,
    subscriber_parent_root: Path,
    output_dir: Path,
    information_asof: str = INFORMATION_ASOF,
) -> dict[str, Any]:
    if output_dir.exists():
        raise FileExistsError(output_dir)
    output_dir.mkdir(parents=True)
    raw_dir = output_dir / "immutable_xbrl_cache"
    raw_dir.mkdir()
    key = os.getenv("DART_API_KEY")
    if not key:
        raise EarningsContractError("DART_API_KEY is not configured")
    selected = _load_selected(upstream_root)
    public_financials = pd.read_csv(
        subscriber_parent_root / "selected_security_public_financials.csv", dtype={"ticker": str}
    )
    public_valuation = pd.read_csv(
        subscriber_parent_root / "selected_security_public_valuation.csv", dtype={"ticker": str}
    )
    target = pd.read_csv(upstream_root / "target_portfolio_v2.csv", dtype={"ticker": str})
    target["ticker"] = target["ticker"].astype(str).str.zfill(6)
    prices = target.loc[target["ticker"].isin(EXPECTED_TICKERS), ["ticker", "reference_price_asof", "reference_price"]]
    prices = prices.drop_duplicates("ticker")
    if len(prices) != 10 or set(prices["reference_price_asof"].astype(str)) != {PRICE_ASOF}:
        raise EarningsContractError("reference prices are not exact at 2026-08-18")

    session = requests.Session()
    session.headers.update({"User-Agent": "ai-inv-adv-financial-enrichment/1.0"})
    lineage_rows: list[dict[str, Any]] = []
    mapping_rows: list[dict[str, Any]] = []
    net_rows: list[dict[str, Any]] = []
    numerator_rows: list[dict[str, Any]] = []
    shares_rows: list[dict[str, Any]] = []
    eps_rows: list[dict[str, Any]] = []
    share_class_rows: list[dict[str, Any]] = []
    per_rows: list[dict[str, Any]] = []

    for security in selected.to_dict(orient="records"):
        ticker = str(security["ticker"])
        name = str(security["name"])
        corp_code = str(security["corp_code"])
        receipts = _select_receipts(session, key, corp_code, information_asof)
        instances: dict[str, Instance] = {}
        for report_key in ("FY2025", "H1_2025", "H1_2026"):
            receipt, payload, rejected_receipts = _latest_receipt_with_xbrl(
                session, key, receipts[report_key]
            )
            cache_path = raw_dir / f"ticker={ticker}__receipt={receipt['receipt_no']}__xbrl.zip"
            if cache_path.exists():
                raise FileExistsError(cache_path)
            cache_path.write_bytes(payload)
            business_year = 2025 if report_key != "H1_2026" else 2026
            instance = parse_instance(
                payload,
                receipt_no=receipt["receipt_no"],
                receipt_date=receipt["receipt_date"],
                report_key=report_key,
                business_year=business_year,
                report_code=REPORT_CODES[report_key],
                source_zip=cache_path,
            )
            instances[report_key] = instance
            lineage_rows.append(
                {
                    "ticker": ticker,
                    "name": name,
                    "corp_code": corp_code,
                    "report_key": report_key,
                    "statement_scope": None,
                    "receipt_no": instance.receipt_no,
                    "receipt_date": instance.receipt_date,
                    "business_year": business_year,
                    "report_code": instance.report_code,
                    "taxonomy_version": instance.taxonomy_version,
                    "source_file_sha256": instance.source_sha256,
                    "source_file": cache_path.name,
                    "newer_receipts_without_xbrl": json.dumps(
                        rejected_receipts, ensure_ascii=False, separators=(",", ":")
                    ),
                }
            )

        scope, scope_status = _scope_for_instances(instances.values())
        if scope is None:
            for report in lineage_rows[-3:]:
                report["statement_scope"] = "UNRESOLVED"
            periods: dict[str, dict[str, Any]] = {}
            net_status = "FAIL"
            eps_status = "FAIL"
            net_na_reason = scope_status
            eps_na_reason = scope_status
            restatement = None
            restatement_status = "NOT_EVALUATED_SCOPE_UNRESOLVED"
        else:
            for report in lineage_rows[-3:]:
                report["statement_scope"] = scope
            periods = {}
            specs = (
                ("FY2025", instances["FY2025"], *REPORT_PERIODS["FY2025"]),
                ("H1_2025_ORIGINAL", instances["H1_2025"], *REPORT_PERIODS["H1_2025"]),
                ("H1_2026", instances["H1_2026"], *REPORT_PERIODS["H1_2026"]),
                (
                    "H1_2025_COMPARATIVE",
                    instances["H1_2026"],
                    *REPORT_PERIODS["H1_2025_COMPARATIVE"],
                ),
            )
            for period_key, instance, start, end in specs:
                values, mappings = _period_facts(instance, period_start=start, period_end=end, scope=scope)
                periods[period_key] = values
                for row in mappings:
                    mapping_rows.append({"ticker": ticker, "name": name, "period_key": period_key, **row})
            calculation_periods = ("FY2025", "H1_2026", "H1_2025_COMPARATIVE")
            net_missing = _missing_fact_reason(periods, calculation_periods, ("net_income",))
            eps_missing = _missing_fact_reason(
                periods, calculation_periods, ("basic_eps", "basic_numerator", "weighted_shares")
            )
            net_status = "PASS" if not net_missing else "FAIL"
            eps_status = "PASS" if not eps_missing else "FAIL"
            net_na_reason = "" if net_status == "PASS" else net_missing
            eps_na_reason = "" if eps_status == "PASS" else eps_missing
            restatement, restatement_status = _comparative_restatement(
                periods["H1_2025_ORIGINAL"], periods["H1_2025_COMPARATIVE"]
            )
            if eps_status == "PASS":
                verification = {key: _verification(periods[key]) for key in calculation_periods}
                failed_verifications = [key for key, value in verification.items() if not value[0]]
                if failed_verifications:
                    eps_status = "FAIL"
                    eps_na_reason = "DISCLOSED_BASIC_EPS_REPRODUCTION_FAILED:" + "|".join(
                        failed_verifications
                    )
            else:
                verification = {}
            if restatement is None:
                eps_status = "FAIL"
                unresolved = "UNRESOLVED_COMPARATIVE_SHARE_COUNT_RESTATEMENT:" + restatement_status
                eps_na_reason = "|".join(reason for reason in (eps_na_reason, unresolved) if reason)

        fy_days = _days(*REPORT_PERIODS["FY2025"])
        current_days = _days(*REPORT_PERIODS["H1_2026"])
        prior_days = _days(*REPORT_PERIODS["H1_2025_COMPARATIVE"])
        ttm_days = fy_days - prior_days + current_days
        if net_status == "PASS":
            fy = periods["FY2025"]
            current = periods["H1_2026"]
            prior = periods["H1_2025_COMPARATIVE"]
            net_ttm = ttm_ytd_bridge(
                fy["net_income"]["value"], current["net_income"]["value"], prior["net_income"]["value"]
            )
        else:
            net_ttm = None

        if eps_status == "PASS":
            fy = periods["FY2025"]
            current = periods["H1_2026"]
            prior = periods["H1_2025_COMPARATIVE"]
            numerator_ttm = ttm_ytd_bridge(
                fy["basic_numerator"]["value"],
                current["basic_numerator"]["value"],
                prior["basic_numerator"]["value"],
            )
            weighted_ttm, share_days, computed_ttm_days = share_day_ttm(
                fy_shares=fy["weighted_shares"]["value"],
                fy_start=REPORT_PERIODS["FY2025"][0],
                fy_end=REPORT_PERIODS["FY2025"][1],
                current_ytd_shares=current["weighted_shares"]["value"],
                current_ytd_start=REPORT_PERIODS["H1_2026"][0],
                current_ytd_end=REPORT_PERIODS["H1_2026"][1],
                prior_ytd_shares=prior["weighted_shares"]["value"],
                prior_ytd_start=REPORT_PERIODS["H1_2025_COMPARATIVE"][0],
                prior_ytd_end=REPORT_PERIODS["H1_2025_COMPARATIVE"][1],
            )
            if computed_ttm_days != ttm_days:
                raise AssertionError("share-day TTM day count mismatch")
            eps_ttm = numerator_ttm / weighted_ttm if weighted_ttm > 0 else None
        else:
            numerator_ttm = share_days = weighted_ttm = eps_ttm = None

        participating = _participating_share_classes(instances["H1_2026"]) if scope else tuple()
        price = float(prices.loc[prices["ticker"].eq(ticker), "reference_price"].iloc[0])
        market_cap = float(
            public_financials.loc[
                public_financials["ticker"].astype(str).str.zfill(6).eq(ticker), "market_cap"
            ].iloc[0]
        )
        multiclass = "PreferenceSharesMember" in participating
        market_cap_for_per = None if multiclass else market_cap
        per_price = price / eps_ttm if eps_ttm is not None and eps_ttm > 0 else None
        per_mcap = (
            market_cap_for_per / net_ttm
            if market_cap_for_per is not None and net_ttm is not None and net_ttm > 0
            else None
        )
        relative = (
            abs(per_price - per_mcap) / abs(per_price)
            if per_price not in (None, 0) and per_mcap is not None
            else None
        )
        if multiclass and eps_ttm is not None:
            share_class_status = "PASS_PRICE_EPS_COMMON_CLASS_MATCH_MCAP_FALLBACK_NOT_USED"
        elif multiclass:
            share_class_status = "FAIL_MULTI_CLASS_MCAP_COMPONENTS_NOT_RECONCILED"
        else:
            share_class_status = "PASS_SINGLE_PARTICIPATING_ORDINARY_CLASS"

        per_selected, per_formula, per_status = select_per_method(
            price=price,
            eps_ttm=eps_ttm,
            market_cap_for_per=market_cap_for_per,
            net_income_ttm=net_ttm,
        )
        if per_status == "LOSS":
            per_na_reason = "NON_POSITIVE_EARNINGS"
        elif per_status == "PASS":
            per_na_reason = ""
        else:
            reasons = [reason for reason in (eps_na_reason, net_na_reason) if reason]
            if multiclass and eps_ttm is None:
                reasons.append(share_class_status)
            per_na_reason = "|".join(reasons) or "NO_VALID_PER_METHOD"

        source_receipts = "|".join(instances[key].receipt_no for key in ("FY2025", "H1_2026"))
        receipt_dates = "|".join(instances[key].receipt_date for key in ("FY2025", "H1_2026"))
        net_rows.append(
            {
                "ticker": ticker,
                "name": name,
                "statement_scope": scope,
                "fy_value": periods.get("FY2025", {}).get("net_income", {}).get("value"),
                "current_ytd_value": periods.get("H1_2026", {}).get("net_income", {}).get("value"),
                "prior_ytd_value": periods.get("H1_2025_COMPARATIVE", {}).get("net_income", {}).get("value"),
                "ttm_value": net_ttm,
                "formula": "FY2025 + H1_2026 - H1_2025",
                "source_receipt_numbers": source_receipts,
                "receipt_dates": receipt_dates,
                "status": net_status,
                "na_reason": net_na_reason,
            }
        )
        numerator_rows.append(
            {
                "ticker": ticker,
                "name": name,
                "statement_scope": scope,
                "fy_value": periods.get("FY2025", {}).get("basic_numerator", {}).get("value"),
                "current_ytd_value": periods.get("H1_2026", {}).get("basic_numerator", {}).get("value"),
                "prior_ytd_value_latest_comparative": periods.get("H1_2025_COMPARATIVE", {}).get("basic_numerator", {}).get("value"),
                "prior_ytd_value_original": periods.get("H1_2025_ORIGINAL", {}).get("basic_numerator", {}).get("value"),
                "profit_for_basic_eps_ttm": numerator_ttm,
                "comparative_share_count_restatement": restatement,
                "restatement_crosscheck_status": restatement_status,
                "status": eps_status,
                "na_reason": eps_na_reason,
            }
        )
        shares_rows.append(
            {
                "ticker": ticker,
                "name": name,
                "weighted_average_shares_fy2025": periods.get("FY2025", {}).get("weighted_shares", {}).get("value"),
                "days_fy2025": fy_days,
                "weighted_average_shares_h1_2025_latest_comparative": periods.get("H1_2025_COMPARATIVE", {}).get("weighted_shares", {}).get("value"),
                "weighted_average_shares_h1_2025_original": periods.get("H1_2025_ORIGINAL", {}).get("weighted_shares", {}).get("value"),
                "days_h1_2025": prior_days,
                "weighted_average_shares_h1_2026": periods.get("H1_2026", {}).get("weighted_shares", {}).get("value"),
                "days_h1_2026": current_days,
                "share_days_ttm": share_days,
                "days_ttm": ttm_days,
                "weighted_average_shares_ttm": weighted_ttm,
                "comparative_share_count_restatement": restatement,
                "restatement_crosscheck_status": restatement_status,
                "status": eps_status,
                "na_reason": eps_na_reason,
            }
        )
        eps_rows.append(
            {
                "ticker": ticker,
                "name": name,
                "profit_for_basic_eps_ttm": numerator_ttm,
                "weighted_average_shares_ttm": weighted_ttm,
                "eps_ttm": eps_ttm,
                "eps_source_type": "XBRL_BASIC_EPS_NOTE_SHARE_DAY_TTM" if eps_status == "PASS" else None,
                "corporate_action_adjusted": restatement is True,
                "comparative_share_count_restatement": restatement,
                "restatement_crosscheck_status": restatement_status,
                "status": eps_status,
                "na_reason": eps_na_reason,
            }
        )
        share_class_rows.append(
            {
                "ticker": ticker,
                "name": name,
                "participating_share_classes": "|".join(participating),
                "market_cap_components": "COMMON_ONLY_NOT_USED" if "PreferenceSharesMember" in participating else ticker,
                "market_cap_for_per": market_cap_for_per,
                "share_class_reconciliation_status": share_class_status,
                "status": "PASS" if share_class_status.startswith("PASS") else "FAIL",
            }
        )
        per_rows.append(
            {
                "ticker": ticker,
                "name": name,
                "price_asof": PRICE_ASOF,
                "price": price,
                "eps_ttm": eps_ttm,
                "per_price_eps": per_price,
                "market_cap_for_per": market_cap_for_per,
                "net_income_ttm_for_per": net_ttm,
                "per_mcap_net_income": per_mcap,
                "per_formula_used": per_formula,
                "per_selected": per_selected,
                "per_relative_difference": relative,
                "share_class_reconciliation_status": share_class_status,
                "status": per_status,
                "na_reason": per_na_reason,
            }
        )

    frames = {
        "earnings_source_lineage.csv": pd.DataFrame(lineage_rows),
        "earnings_concept_mapping.csv": pd.DataFrame(mapping_rows),
        "net_income_ttm_qa.csv": pd.DataFrame(net_rows),
        "eps_numerator_qa.csv": pd.DataFrame(numerator_rows),
        "weighted_average_shares_qa.csv": pd.DataFrame(shares_rows),
        "eps_ttm_qa.csv": pd.DataFrame(eps_rows),
        "share_class_market_cap_qa.csv": pd.DataFrame(share_class_rows),
        "per_method_qa.csv": pd.DataFrame(per_rows),
    }
    for filename, frame in frames.items():
        _safe_csv(output_dir / filename, frame)

    eps_qa = frames["eps_ttm_qa.csv"].copy()
    net_qa = frames["net_income_ttm_qa.csv"].copy()
    per_qa = frames["per_method_qa.csv"].copy()
    public_financials["ticker"] = public_financials["ticker"].astype(str).str.zfill(6)
    public_valuation["ticker"] = public_valuation["ticker"].astype(str).str.zfill(6)
    enriched_financials = public_financials.merge(
        eps_qa[["ticker", "eps_ttm", "profit_for_basic_eps_ttm", "weighted_average_shares_ttm"]],
        on="ticker",
        how="left",
        validate="one_to_one",
    ).merge(
        net_qa[["ticker", "ttm_value"]].rename(columns={"ttm_value": "net_income_ttm_for_per"}),
        on="ticker",
        how="left",
        validate="one_to_one",
    )
    enriched_valuation = public_valuation.drop(
        columns=[
            "eps_ttm",
            "eps_status",
            "net_income_ttm_for_per",
            "net_income_status",
            "per_value",
            "per_formula_used",
            "per_status",
            "per_na_reason",
            "per_formula_reproduction_error",
        ],
        errors="ignore",
    ).merge(
        per_qa[
            [
                "ticker",
                "eps_ttm",
                "net_income_ttm_for_per",
                "per_formula_used",
                "per_selected",
                "status",
                "na_reason",
            ]
        ],
        on="ticker",
        how="left",
        validate="one_to_one",
    ).rename(
        columns={
            "per_selected": "per_value",
            "status": "per_status",
            "na_reason": "per_na_reason",
        }
    )
    enriched_valuation["eps_status"] = enriched_valuation["eps_ttm"].notna().map(
        {True: "PASS_XBRL_SHARE_DAY_TTM", False: "NA_EXACT_XBRL_INPUT_UNAVAILABLE"}
    )
    enriched_valuation["net_income_status"] = enriched_valuation["net_income_ttm_for_per"].notna().map(
        {True: "PASS_OWNERS_OF_PARENT_TTM", False: "NA_EXACT_PARENT_ATTRIBUTION_UNAVAILABLE"}
    )
    enriched_valuation["per_earnings_period"] = "TTM_2025-07-01_2026-06-30"
    enriched_valuation["per_formula_reproduction_error"] = enriched_valuation.apply(
        lambda row: abs(row["per_value"] - float(prices.loc[prices["ticker"].eq(row["ticker"]), "reference_price"].iloc[0]) / row["eps_ttm"])
        if pd.notna(row["per_value"]) and pd.notna(row["eps_ttm"]) and row["eps_ttm"] > 0
        else None,
        axis=1,
    )
    _safe_csv(output_dir / "selected_security_public_financials_v2.csv", enriched_financials)
    _safe_csv(output_dir / "selected_security_public_valuation_v2.csv", enriched_valuation)

    receipt_ok = all(frames["earnings_source_lineage.csv"]["receipt_date"].le(information_asof))
    eps_coverage = int(eps_qa["eps_ttm"].notna().sum())
    net_coverage = int(net_qa["ttm_value"].notna().sum())
    per_coverage = int(per_qa["status"].isin(["PASS", "LOSS"]).sum())
    complete = eps_coverage == net_coverage == per_coverage == 10 and receipt_ok
    dependency_contract = ttm_dependency_contract(2026, 2)
    incomplete_tickers = []
    for security in selected[["ticker", "name"]].to_dict("records"):
        ticker = str(security["ticker"])
        eps_row = eps_qa.loc[eps_qa["ticker"].eq(ticker)].iloc[0]
        net_row = net_qa.loc[net_qa["ticker"].eq(ticker)].iloc[0]
        per_row = per_qa.loc[per_qa["ticker"].eq(ticker)].iloc[0]
        if pd.notna(eps_row["eps_ttm"]) and pd.notna(net_row["ttm_value"]) and per_row["status"] in {
            "PASS",
            "LOSS",
        }:
            continue
        field_names = {
            "weighted_shares": "weighted_average_ordinary_shares",
            "basic_numerator": "profit_attributable_to_ordinary_holders",
            "basic_eps": "disclosed_basic_eps",
        }
        ticker_mappings = frames["earnings_concept_mapping.csv"].loc[
            frames["earnings_concept_mapping.csv"]["ticker"].eq(ticker)
        ]
        missing_concepts = sorted(
            {
                f"{row.period_key}:{field_names[row.field]}"
                for row in ticker_mappings.itertuples()
                if row.field in field_names and row.status != "PASS"
            }
        )
        if pd.isna(net_row["ttm_value"]):
            missing_concepts.append("parent_net_income")
        required_reports = sorted(
            {
                f"{item.split(':', 1)[0]}_XBRL_EPS_NOTE"
                for item in missing_concepts
                if ":" in item
            }
        )
        incomplete_tickers.append(
            {
                "ticker": ticker,
                "name": str(security["name"]),
                "missing_concepts": missing_concepts,
                "required_reports": required_reports,
                "eps_na_reason": str(eps_row["na_reason"]),
                "net_income_na_reason": str(net_row["na_reason"]),
                "per_na_reason": str(per_row["na_reason"]),
            }
        )
    summary = {
        "information_asof": information_asof,
        "latest_financial_period": "2026Q2",
        "ttm_formula": "FY2025 + H1_2026 - H1_2025",
        "dependency_reports": dependency_contract["reports"],
        "future_quarter_dependencies": dependency_contract["future_quarter_dependencies"],
        "dart_api_used": True,
        "original_xbrl_used": True,
        "external_provider_eps_per_used_count": 0,
        "top_k_count": 10,
        "eps_coverage": eps_coverage,
        "net_income_coverage": net_coverage,
        "per_numeric_or_loss_coverage": per_coverage,
        "receipt_date_cutoff_pass": receipt_ok,
        "statement_scope_mix_count": 0,
        "total_net_income_fallback_used_count": 0,
        "simple_quarter_eps_sum_used_count": 0,
        "financial_enrichment_status": "PASS" if complete else "INCOMPLETE",
        "subscriber_report_ready": complete,
        "incomplete_tickers": incomplete_tickers,
    }
    _safe_json(output_dir / "financial_enrichment_summary.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect exact selected-security earnings denominators")
    parser.add_argument("--upstream-root", required=True)
    parser.add_argument("--subscriber-parent-root", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--information-asof", default=INFORMATION_ASOF)
    args = parser.parse_args()
    summary = collect(
        upstream_root=Path(args.upstream_root),
        subscriber_parent_root=Path(args.subscriber_parent_root),
        output_dir=Path(args.output_dir),
        information_asof=args.information_asof,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
