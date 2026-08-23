from __future__ import annotations

import html
import re
import warnings
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable
from zipfile import ZipFile

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning


def normalize_label(value: object) -> str:
    return re.sub(r"[\s()\-_/,:;]", "", str(value)).lower()


def normalize_metric_label(value: object) -> str:
    """Normalize a row label while excluding an appended disclosure unit."""

    normalized = normalize_label(value)
    return re.sub(r"(?:백만원|천원|억원|천주|원|주)$", "", normalized)


def _expanded_table_headers(table: Any) -> list[str]:
    """Expand multi-row HTML headers into one period label per data column."""

    head = table.find("thead")
    rows = head.find_all("tr", recursive=False) if head else table.find_all("tr")[:1]
    grid: list[list[str]] = []
    occupied: dict[tuple[int, int], str] = {}
    for row_index, row in enumerate(rows):
        cells = row.find_all(["th", "td"], recursive=False)
        column = 0
        for cell in cells:
            while (row_index, column) in occupied:
                column += 1
            label = cell.get_text(" ", strip=True)
            colspan = max(1, int(cell.get("colspan", 1)))
            rowspan = max(1, int(cell.get("rowspan", 1)))
            for row_offset in range(rowspan):
                for column_offset in range(colspan):
                    occupied[(row_index + row_offset, column + column_offset)] = label
            column += colspan
    if not occupied:
        return []
    width = max(column for _, column in occupied) + 1
    height = max(row for row, _ in occupied) + 1
    for row in range(height):
        grid.append([occupied.get((row, column), "") for column in range(width)])
    headers: list[str] = []
    for column in range(width):
        parts: list[str] = []
        for row in range(height):
            value = grid[row][column]
            if value and value not in parts:
                parts.append(value)
        headers.append(" ".join(parts))
    return headers


@dataclass(frozen=True)
class EpsNoteValue:
    field: str
    period_label: str
    value: float
    unit: str
    table_title: str
    row_label: str
    source_tier: str = "EPS_NOTE_TABLE"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_eps_note_tables(
    *,
    document_html: str,
    approved_titles: Iterable[str],
    approved_rows: dict[str, Iterable[str]],
) -> list[EpsNoteValue]:
    soup = BeautifulSoup(document_html, "html.parser")
    title_tokens = {normalize_label(item) for item in approved_titles}
    row_tokens = {
        field: {normalize_label(item) for item in labels} for field, labels in approved_rows.items()
    }
    results: list[EpsNoteValue] = []
    for table in soup.find_all("table"):
        preceding = table.find_previous(["h1", "h2", "h3", "h4", "p"])
        title = preceding.get_text(" ", strip=True) if preceding else ""
        if not any(token in normalize_label(title) for token in title_tokens):
            continue
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = _expanded_table_headers(table)
        body = table.find("tbody")
        data_rows = body.find_all("tr", recursive=False) if body else rows[1:]
        previous_table = table.find_previous("table")
        prior_unit_text = (
            previous_table.get_text(" ", strip=True)
            if previous_table is not None
            and len(previous_table.get_text(" ", strip=True)) <= 200
            else ""
        )
        for row in data_rows:
            cells = [cell.get_text(" ", strip=True) for cell in row.find_all(["th", "td"])]
            if len(cells) < 2:
                continue
            normalized = normalize_metric_label(cells[0])
            for field, approved in row_tokens.items():
                if normalized not in approved:
                    continue
                for index, raw in enumerate(cells[1:], start=1):
                    numeric = re.sub(r"[^0-9.\-]", "", raw.replace(",", ""))
                    if not numeric or numeric in {"-", "."}:
                        continue
                    unit_text = f"{prior_unit_text} {cells[0]} {raw}".lower()
                    if field == "weighted_average_ordinary_shares":
                        unit = "SHARES"
                        scale = (
                            1000.0
                            if "천주" in unit_text or "thousand share" in unit_text
                            else 1.0
                        )
                    else:
                        unit = "KRW"
                        if "백만원" in unit_text or "million krw" in unit_text:
                            scale = 1_000_000.0
                        elif "천원" in unit_text or "thousand krw" in unit_text:
                            scale = 1000.0
                        elif "억원" in unit_text:
                            scale = 100_000_000.0
                        else:
                            scale = 1.0
                    normalized_value = float(numeric) * scale
                    results.append(
                        EpsNoteValue(
                            field=field,
                            period_label=headers[index] if index < len(headers) else str(index),
                            value=normalized_value,
                            unit=unit,
                            table_title=title,
                            row_label=cells[0],
                        )
                    )
    return results


def parse_eps_notes_from_xbrl_archive(
    *, archive_path: Path, approved_titles: Iterable[str], approved_rows: dict[str, Iterable[str]]
) -> list[EpsNoteValue]:
    """Inspect report-owned XBRL/XML text blocks for approved EPS note tables."""
    values: list[EpsNoteValue] = []
    with ZipFile(archive_path) as archive:
        for member in archive.namelist():
            if not member.lower().endswith((".xbrl", ".xml")):
                continue
            raw = archive.read(member).decode("utf-8", errors="ignore")
            for document in (raw, html.unescape(raw), html.unescape(html.unescape(raw))):
                with warnings.catch_warnings():
                    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
                    values.extend(
                        parse_eps_note_tables(
                            document_html=document,
                            approved_titles=approved_titles,
                            approved_rows=approved_rows,
                        )
                    )
    unique: dict[tuple[str, str, float, str, str], EpsNoteValue] = {}
    for item in values:
        unique[(item.field, item.period_label, item.value, item.table_title, item.row_label)] = item
    return list(unique.values())


def select_eps_note_value(
    values: Iterable[EpsNoteValue],
    *,
    field: str,
    period_start: str,
    period_end: str,
    period_role: str | None = None,
) -> EpsNoteValue | None:
    """Select a unique field/period value without guessing among conflicts."""
    candidates = [item for item in values if item.field == field]
    if not candidates:
        return None
    end_tokens = {
        normalize_label(period_end),
        normalize_label(period_end[:4]),
        normalize_label(period_end.replace("-", ".")),
        normalize_label(f"{period_start}/{period_end}"),
    }
    matching = [
        item
        for item in candidates
        if any(token and token in normalize_label(item.period_label) for token in end_tokens)
    ]
    role_matching: list[EpsNoteValue] = []
    if period_role:
        role = period_role.upper()
        markers = (
            ("당기", "current", "currentperiod")
            if role == "CURRENT"
            else ("전기", "prior", "comparative", "previous")
        )
        role_matching = [
            item
            for item in candidates
            if any(marker in normalize_label(item.period_label) for marker in markers)
        ]
    pool = matching or role_matching or candidates
    return pool[0] if len({(item.value, item.unit) for item in pool}) == 1 else None


def select_eps_note_pair(
    values: Iterable[EpsNoteValue],
    *,
    period_start: str,
    period_end: str,
    disclosed_eps: float,
    period_role: str,
    numerator_anchor: float | None = None,
    shares_anchor: float | None = None,
) -> tuple[EpsNoteValue | None, EpsNoteValue | None]:
    """Resolve an EPS-note pair by reproducing the report's disclosed basic EPS.

    Some filings place consolidated and separate EPS tables in the same official
    document.  A field-by-field choice is ambiguous in that case.  This resolver
    keeps only the numerator/share pairing whose quotient agrees with the exact
    basic EPS fact for the requested statement scope.
    """

    items = list(values)
    numerator_items = [item for item in items if item.field == "basic_eps_profit"]
    shares_items = [
        item for item in items if item.field == "weighted_average_ordinary_shares"
    ]

    def period_pool(candidates: list[EpsNoteValue]) -> list[EpsNoteValue]:
        if not candidates:
            return []
        dated = [
            item
            for item in candidates
            if normalize_label(period_end) in normalize_label(item.period_label)
            or normalize_label(period_end.replace("-", "."))
            in normalize_label(item.period_label)
        ]
        if dated:
            base_pool = dated
        else:
            markers = (
                ("당기", "current", "currentperiod")
                if period_role.upper() == "CURRENT"
                else ("전기", "prior", "comparative", "previous")
            )
            role_items = [
                item
                for item in candidates
                if any(marker in normalize_label(item.period_label) for marker in markers)
            ]
            base_pool = role_items or candidates
        try:
            span_days = (date.fromisoformat(period_end) - date.fromisoformat(period_start)).days
        except ValueError:
            span_days = 0
        if span_days >= 120:
            cumulative = [
                item
                for item in base_pool
                if any(
                    marker in normalize_label(item.period_label)
                    for marker in ("누적", "cumulative", "yeartodate", "ytd")
                )
            ]
            if cumulative:
                return cumulative
        return base_pool

    numerator_pool: list[EpsNoteValue | None] = (
        [None] if numerator_anchor is not None else period_pool(numerator_items)
    )
    shares_pool: list[EpsNoteValue | None] = (
        [None] if shares_anchor is not None else period_pool(shares_items)
    )
    if not numerator_pool or not shares_pool:
        return None, None

    ranked: list[tuple[float, EpsNoteValue | None, EpsNoteValue | None]] = []
    for numerator_note in numerator_pool:
        numerator = (
            float(numerator_anchor)
            if numerator_anchor is not None
            else float(numerator_note.value)  # type: ignore[union-attr]
        )
        for shares_note in shares_pool:
            shares = (
                float(shares_anchor)
                if shares_anchor is not None
                else float(shares_note.value)  # type: ignore[union-attr]
            )
            if shares <= 0:
                continue
            ranked.append((abs(numerator / shares - disclosed_eps), numerator_note, shares_note))
    if not ranked:
        return None, None
    ranked.sort(key=lambda item: item[0])
    best_error = ranked[0][0]
    tolerance = max(0.55, abs(disclosed_eps) * 0.0001)
    if best_error > tolerance:
        return None, None
    best_pairs = {
        (
            None if numerator is None else numerator.value,
            None if shares is None else shares.value,
        )
        for error, numerator, shares in ranked
        if abs(error - best_error) <= 1e-9
    }
    if len(best_pairs) != 1:
        return None, None
    return ranked[0][1], ranked[0][2]
