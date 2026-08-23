from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from bs4 import BeautifulSoup


@dataclass(frozen=True)
class SubscriberValidationResult:
    status: str
    checks: dict[str, bool]
    details: dict[str, Any]


def _card_metric(card: Any, label: str) -> str | None:
    for metric in card.select(".metric"):
        name = metric.find("span")
        value = metric.find("strong")
        if name is not None and value is not None and name.get_text(" ", strip=True) == label:
            return value.get_text(" ", strip=True)
    return None


def validate_subscriber_report(
    *,
    html_path: str | Path,
    pdf_path: str | Path,
    render_metadata_path: str | Path,
    expected_title: str,
    expected_selected_count: int,
    expected_dropped_count: int,
    expected_performance_start: str,
    expected_performance_end: str,
    expected_target_weight_total: float,
    public_forbidden_terms: Iterable[str],
) -> SubscriberValidationResult:
    """Validate a subscriber report without quarter- or ticker-specific assumptions."""

    html_file = Path(html_path)
    pdf_file = Path(pdf_path)
    metadata_file = Path(render_metadata_path)
    for path in (html_file, pdf_file, metadata_file):
        if not path.is_file():
            raise FileNotFoundError(path)
    document = html_file.read_text(encoding="utf-8")
    soup = BeautifulSoup(document, "html.parser")
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_file))
    pdf_pages = [page.extract_text() or "" for page in reader.pages]
    pdf_text = "\n".join(pdf_pages)
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    combined = document + "\n" + pdf_text
    selected_cards = soup.select("#selected-details article.security-card")
    dropped_cards = soup.select(
        "#not-selected-details article.not-selected-security-card"
    )
    cards = [*selected_cards, *dropped_cards]
    dropped_rows = soup.select("#not-selected-table tbody tr")
    if not dropped_rows:
        dropped_rows = soup.select("#dropped-table tbody tr")
    missing_markers = {"", "-", "NA", "N/A", "NULL", "자료 없음", "미산출"}
    eps_missing = sum((_card_metric(card, "EPS") or "").upper() in missing_markers for card in cards)
    net_missing = sum(
        ((_card_metric(card, "당기순이익") or "").split("·", 1)[0].strip().upper() in missing_markers)
        for card in cards
    )
    per_missing = sum((_card_metric(card, "PER") or "").upper() in missing_markers for card in cards)
    target_table = soup.select_one("#target-table")
    displayed_target_total = 0.0
    if target_table is not None:
        headers = [node.get_text(" ", strip=True) for node in target_table.select("thead th")]
        if "목표비중" in headers:
            index = headers.index("목표비중")
            for row in target_table.select("tbody tr"):
                cells = row.select("td")
                if index < len(cells):
                    text = cells[index].get_text(" ", strip=True).replace("%", "").replace(",", "")
                    try:
                        displayed_target_total += float(text) / 100.0
                    except ValueError:
                        pass
    render_pdf = metadata.get("pdf", {})
    rendered_page_count = int(render_pdf.get("rendered_page_count", 0) or 0)
    page_count = int(render_pdf.get("page_count", 0) or 0)
    page_dir = metadata_file.parent / "pdf_pages"
    rendered_pages = sorted(page_dir.glob("page-*.png"))
    clipping = metadata.get("clipping", {})
    clipping_count = int(clipping.get("clipped_table_count", 0) or 0) + int(clipping.get("clipped_card_count", 0) or 0)
    required_sections = {
        "current-account", "monthly-performance", "security-returns", "market-model",
        "topk", "target", "comparison", "selected-details",
    }
    section_ids = {node.get("id") for node in soup.select("section[id]")}
    full_detail_sections = (
        "not-selected-summary" in section_ids
        and (
            int(expected_dropped_count) == 0
            or "not-selected-details" in section_ids
        )
    )
    internal_enum = re.compile(r"\b(?:BUY|SELL|HOLD|ADVISOR_FULL_RESET|execution-ready)\b", re.IGNORECASE)
    receipt = re.compile(r"(?<!\d)(?:19|20)\d{12}(?!\d)")
    checks = {
        "title": soup.title is not None and soup.title.get_text() == expected_title and soup.h1 is not None and soup.h1.get_text(" ", strip=True) == expected_title,
        "compliance_notice": "Compliance Notice" in soup.get_text(" ", strip=True),
        "required_sections": required_sections.issubset(section_ids)
        and ("dropped" in section_ids or full_detail_sections),
        "selected_card_count": len(selected_cards) == int(expected_selected_count),
        "dropped_count": len(dropped_rows) == int(expected_dropped_count),
        "dropped_card_count": (
            not full_detail_sections
            or len(dropped_cards) == int(expected_dropped_count)
        ),
        "eps_complete": eps_missing == 0,
        "net_income_complete": net_missing == 0,
        "per_complete": per_missing == 0,
        "target_weight_total": abs(displayed_target_total - float(expected_target_weight_total)) <= 0.00011,
        "performance_dates": expected_performance_start in combined and expected_performance_end in combined,
        "krx300_present": "KRX300" in combined.replace(" ", ""),
        "internal_enum_absent": not bool(internal_enum.search(combined)),
        "forbidden_terms_absent": all(str(term).lower() not in combined.lower() for term in public_forbidden_terms),
        "absolute_paths_absent": not bool(re.search(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]", combined)),
        "receipt_numbers_absent": not bool(receipt.search(combined)),
        "hashes_absent": not bool(re.search(r"\b[a-fA-F0-9]{64}\b", combined)),
        "continuation_tables_absent": "(계속)" not in combined,
        "all_pdf_pages_rendered": page_count == rendered_page_count == len(reader.pages) == len(rendered_pages) and page_count > 0,
        "empty_pdf_pages_absent": all(len(text.strip()) >= 20 for text in pdf_pages),
        "table_card_clipping_absent": clipping_count == 0,
    }
    details = {
        "selected_cards": len(selected_cards),
        "dropped_cards": len(dropped_cards),
        "dropped_rows": len(dropped_rows),
        "eps_missing": eps_missing,
        "net_income_missing": net_missing,
        "per_missing": per_missing,
        "displayed_target_weight_total": displayed_target_total,
        "pdf_page_count": len(reader.pages),
        "rendered_page_count": rendered_page_count,
        "failed_checks": [name for name, passed in checks.items() if not passed],
    }
    return SubscriberValidationResult(
        status="PASS" if all(checks.values()) else "FAIL",
        checks=checks,
        details=details,
    )
