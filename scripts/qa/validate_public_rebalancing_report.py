from __future__ import annotations

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.live.generate_public_rebalancing_report import (
    COST_DISCLOSURE,
    EXECUTION_BLOCKED_TEXT,
    MAX_HTML_BYTES,
    PUBLIC_REPORT_CONTRACT,
    PUBLIC_SECTIONS,
)


class _PublicReportParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.h2_values: list[str] = []
        self._h2_parts: list[str] | None = None
        self.text_parts: list[str] = []
        self.style_parts: list[str] = []
        self._inside_style = False
        self._inside_script = False
        self.script_count = 0
        self.external_resource_count = 0
        self.html_lang = ""
        self.has_viewport = False
        self.report_contract = ""
        self.allocation_policy = ""
        self.performance_statuses: list[str] = []
        self.execution_statuses: list[str] = []
        self.model_row_count = 0
        self.execution_row_count = 0
        self.tables: list[dict[str, Any]] = []
        self._table_index: int | None = None
        self._row_cells = 0
        self._inside_row = False
        self._cell_parts: list[str] | None = None
        self.svg_ids: set[str] = set()
        self.performance_paths: dict[str, dict[str, str]] = {}
        self.chart_line_classes: set[str] = set()
        self.marker_classes: set[str] = set()
        self.contribution_bar_count = 0
        self.public_note_count = 0

    @staticmethod
    def _attrs(attrs: list[tuple[str, str | None]]) -> dict[str, str]:
        return {key: value or "" for key, value in attrs}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = self._attrs(attrs)
        if tag == "html":
            self.html_lang = attributes.get("lang", "")
        elif tag == "meta" and attributes.get("name", "").lower() == "viewport":
            self.has_viewport = True
        elif tag == "main":
            self.report_contract = attributes.get("data-report-contract", "")
            self.allocation_policy = attributes.get("data-allocation-policy", "")
        elif tag == "h2":
            self._h2_parts = []
        elif tag == "style":
            self._inside_style = True
        elif tag == "script":
            self.script_count += 1
            self._inside_script = True
        elif tag in {"link", "iframe"}:
            self.external_resource_count += 1
        elif tag in {"img", "source"} and attributes.get("src"):
            self.external_resource_count += 1
        elif tag == "a" and attributes.get("href"):
            self.external_resource_count += 1
        if "data-performance-status" in attributes:
            self.performance_statuses.append(attributes["data-performance-status"])
        if "data-execution-status" in attributes:
            self.execution_statuses.append(attributes["data-execution-status"])
        if "data-public-model-row" in attributes:
            self.model_row_count += 1
        if "data-public-execution-row" in attributes:
            self.execution_row_count += 1
        if "data-public-note" in attributes:
            self.public_note_count += 1

        if tag == "table":
            self.tables.append(
                {
                    "id": attributes.get("id", ""),
                    "caption": False,
                    "max_columns": 0,
                    "missing_th_scope": 0,
                    "cell_text": [],
                }
            )
            self._table_index = len(self.tables) - 1
        elif tag == "caption" and self._table_index is not None:
            self.tables[self._table_index]["caption"] = True
        elif tag == "tr" and self._table_index is not None:
            self._inside_row = True
            self._row_cells = 0
        elif tag in {"th", "td"} and self._table_index is not None:
            self._row_cells += 1
            self._cell_parts = []
            if tag == "th" and attributes.get("scope") not in {"col", "row"}:
                self.tables[self._table_index]["missing_th_scope"] += 1

        if tag == "svg" and attributes.get("id"):
            self.svg_ids.add(attributes["id"])
        elif tag == "path":
            css_classes = set(attributes.get("class", "").split())
            for css_class in ("portfolio-line", "benchmark-line"):
                if css_class in css_classes:
                    self.performance_paths[css_class] = attributes
        elif tag == "line":
            self.chart_line_classes.update(attributes.get("class", "").split())
        elif tag == "g":
            self.marker_classes.update(attributes.get("class", "").split())
        elif tag == "rect" and "contribution-bar" in attributes.get("class", "").split():
            self.contribution_bar_count += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self._h2_parts is not None:
            self.h2_values.append("".join(self._h2_parts).strip())
            self._h2_parts = None
        elif tag == "style":
            self._inside_style = False
        elif tag == "script":
            self._inside_script = False
        elif tag in {"th", "td"} and self._table_index is not None and self._cell_parts is not None:
            self.tables[self._table_index]["cell_text"].append("".join(self._cell_parts).strip())
            self._cell_parts = None
        elif tag == "tr" and self._table_index is not None and self._inside_row:
            self.tables[self._table_index]["max_columns"] = max(
                self.tables[self._table_index]["max_columns"], self._row_cells
            )
            self._inside_row = False
        elif tag == "table":
            self._table_index = None

    def handle_data(self, data: str) -> None:
        if not self._inside_style and not self._inside_script:
            self.text_parts.append(data)
        if self._h2_parts is not None:
            self._h2_parts.append(data)
        if self._inside_style:
            self.style_parts.append(data)
        if self._cell_parts is not None:
            self._cell_parts.append(data)


def validate_html(
    html_text: str,
    *,
    expected_execution_status: str | None = None,
) -> dict[str, Any]:
    parser = _PublicReportParser()
    parser.feed(html_text)
    parser.close()
    lowered = html_text.lower()
    visible_text = " ".join(parser.text_parts)
    css = "\n".join(parser.style_parts).lower()
    model_table = next((table for table in parser.tables if table["id"] == "model-target-table"), None)
    contribution_table = next(
        (table for table in parser.tables if table["id"] == "contribution-table"), None
    )
    execution_table = next(
        (table for table in parser.tables if table["id"] == "model-execution-table"), None
    )
    model_weights = [] if model_table is None else [
        value for value in model_table["cell_text"] if re.fullmatch(r"(?:NA|\d+\.\d{2}%)", value)
    ]
    execution_statuses = set(parser.execution_statuses)
    effective_execution_status = (
        "BLOCKED" if "unavailable" in execution_statuses
        else "READY" if "available" in execution_statuses else ""
    )
    performance_statuses = set(parser.performance_statuses)

    forbidden_token_absent = all(
        token not in lowered
        for token in ("run_id", "sha256", "credential", "account", "user_", "overlay")
    )
    path_absent = not bool(
        re.search(r"(?i)(?:[a-z]:[\\/]|file://|https?://|\\\\users\\)", html_text)
    )
    hash_absent = not bool(re.search(r"(?i)\b[0-9a-f]{64}\b", html_text))
    path_style_ok = all(
        attributes.get("d", "").strip().startswith("M")
        and attributes.get("fill") == "none"
        and bool(attributes.get("stroke"))
        for attributes in parser.performance_paths.values()
    ) and set(parser.performance_paths) == {"portfolio-line", "benchmark-line"}
    table_contract_ok = bool(parser.tables) and all(
        table["caption"] and table["max_columns"] <= 8 and table["missing_th_scope"] == 0
        for table in parser.tables
    )
    allocation_ok = True
    if parser.allocation_policy == "EQUAL_WEIGHT_TOP_K":
        allocation_ok = model_weights.count("10.00%") == 10
    else:
        allocation_ok = "10.00%" not in (model_table or {}).get("cell_text", [])

    raw_technical_status_absent = not bool(
        re.search(
            r"\b(?:READY|BLOCKED|CERTIFIED|PROVISIONAL|BUY|SELL|HOLD)\b",
            html_text,
            re.IGNORECASE,
        )
    )
    overview_labels = (
        "운용기간", "포트폴리오 수익률", "KRX 300", "초과수익률", "최대 낙폭", "모델 실행계획"
    )
    exact_table_shapes = (
        model_table is not None and model_table["max_columns"] == 8
        and contribution_table is not None and contribution_table["max_columns"] == 6
        and execution_table is not None and execution_table["max_columns"] == 8
    )
    checks: dict[str, bool] = {
        "html_under_150kb": len(html_text.encode("utf-8")) < MAX_HTML_BYTES,
        "document_language_ko": parser.html_lang == "ko",
        "viewport_present": parser.has_viewport,
        "public_contract": parser.report_contract == PUBLIC_REPORT_CONTRACT,
        "exact_five_h2_sections": parser.h2_values == list(PUBLIC_SECTIONS),
        "no_scripts_or_embedded_json": parser.script_count == 0 and "application/json" not in lowered,
        "no_external_resources": parser.external_resource_count == 0,
        "privacy_tokens_absent": forbidden_token_absent,
        "raw_technical_status_and_actions_absent": raw_technical_status_absent,
        "paths_and_urls_absent": path_absent,
        "hashes_absent": hash_absent,
        "ten_model_rows": parser.model_row_count == 10,
        "allocation_policy_weight_contract": allocation_ok,
        "all_tables_captioned_scoped_max_eight_columns": table_contract_ok,
        "required_table_shapes": exact_table_shapes,
        "overview_six_required_metrics": all(label in visible_text for label in overview_labels),
        "performance_chart_present": "performance-line-chart" in parser.svg_ids,
        "performance_chart_axes_and_zero_line": {"x-axis", "y-axis", "zero-line"} <= parser.chart_line_classes,
        "performance_paths_are_explicit_unfilled_strokes": path_style_ok,
        "performance_start_end_peak_mdd_markers": {
            "start-marker", "end-marker", "peak-marker", "mdd-marker"
        } <= parser.marker_classes,
        "contribution_bar_chart_present": (
            "contribution-bar-chart" in parser.svg_ids and parser.contribution_bar_count > 0
        ),
        "print_css_present": "@media print" in css and "@page" in css,
        "mobile_css_present": "@media(max-width:" in css or "@media (max-width:" in css,
        "no_horizontal_scroll_css": (
            "overflow-x" not in css and "overflow:auto" not in css and "overflow: auto" not in css
        ),
        "cost_disclosure_exact": COST_DISCLOSURE in visible_text,
        "execution_status_present": effective_execution_status in {"BLOCKED", "READY"},
        "blocked_execution_contract": (
            effective_execution_status != "BLOCKED"
            or (EXECUTION_BLOCKED_TEXT in visible_text and parser.execution_row_count == 10)
        ),
        "ready_execution_contract": (
            effective_execution_status != "READY" or parser.execution_row_count > 0
        ),
        "provisional_performance_disclosed": (
            "tentative" not in performance_statuses or "잠정 성과" in visible_text
        ),
        "exact_eight_methodology_notes": parser.public_note_count == 8,
    }
    if expected_execution_status is not None:
        checks["expected_execution_status"] = (
            effective_execution_status == expected_execution_status.strip().upper()
        )
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "schema_version": 1,
        "report_contract": PUBLIC_REPORT_CONTRACT,
        "status": "PASS" if not failed else "FAIL",
        "checks": checks,
        "failed_checks": failed,
        "h2_sections": parser.h2_values,
        "execution_status": effective_execution_status,
    }


def validate(
    html_path: str | Path,
    *,
    expected_execution_status: str | None = None,
    pdf_path: str | Path | None = None,
) -> dict[str, Any]:
    html_text = Path(html_path).read_text(encoding="utf-8")
    payload = validate_html(
        html_text,
        expected_execution_status=expected_execution_status,
    )
    if pdf_path is None:
        return payload

    pdf_text = ""
    page_count = 0
    backend = "UNAVAILABLE"
    try:
        from pypdf import PdfReader  # type: ignore

        reader = PdfReader(str(pdf_path))
        page_count = len(reader.pages)
        pdf_text = "\n".join(page.extract_text() or "" for page in reader.pages)
        backend = "PYPDF"
    except (ImportError, ModuleNotFoundError):
        try:
            import pdfplumber  # type: ignore

            with pdfplumber.open(str(pdf_path)) as document:
                page_count = len(document.pages)
                pdf_text = "\n".join(page.extract_text() or "" for page in document.pages)
            backend = "PDFPLUMBER"
        except (ImportError, ModuleNotFoundError):
            pass
    except Exception:
        backend = "EXTRACTION_FAILED"

    html_parser = _PublicReportParser()
    html_parser.feed(html_text)
    html_visible = " ".join(html_parser.text_parts)
    lowered_pdf = pdf_text.lower()
    pdf_privacy_ok = all(
        token not in lowered_pdf
        for token in ("run_id", "sha256", "credential", "account", "user_", "overlay")
    ) and not bool(
        re.search(r"(?i)(?:[a-z]:[\\/]|file://|https?://|\b[0-9a-f]{64}\b)", pdf_text)
    )
    html_core_tokens = set(
        re.findall(r"-?\d[\d,]*(?:\.\d{2})?(?:%|원)", html_visible)
    )
    normalized_pdf = re.sub(r"\s+", "", pdf_text).replace(",", "")
    compact_pdf_text = re.sub(r"\s+", "", pdf_text)
    numeric_match = all(token.replace(",", "") in normalized_pdf for token in html_core_tokens)
    pdf_checks = {
        "pdf_text_extraction_available": backend in {"PYPDF", "PDFPLUMBER"},
        "pdf_page_count_positive": page_count > 0,
        "pdf_contains_all_five_sections": all(
            re.sub(r"\s+", "", section) in compact_pdf_text for section in PUBLIC_SECTIONS
        ),
        "pdf_privacy_contract": pdf_privacy_ok,
        "html_pdf_core_percent_and_won_values_match": bool(html_core_tokens) and numeric_match,
    }
    payload["checks"].update(pdf_checks)
    payload["failed_checks"] = [
        name for name, passed in payload["checks"].items() if not passed
    ]
    payload["status"] = "PASS" if not payload["failed_checks"] else "FAIL"
    payload["pdf_validation"] = {
        "backend": backend,
        "page_count": page_count,
    }
    return payload


def _markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Public Rebalancing Report QA",
        "",
        f"- Status: **{payload['status']}**",
        f"- Execution section: **{payload['execution_status']}**",
        "",
        "## Checks",
        "",
    ]
    lines.extend(
        f"- {'PASS' if passed else 'FAIL'} — `{name}`"
        for name, passed in payload["checks"].items()
    )
    lines.append("")
    return "\n".join(lines)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate the public rebalancing HTML contract")
    parser.add_argument("--html", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-md", required=True)
    parser.add_argument("--pdf", default=None)
    parser.add_argument("--expected-execution-status", choices=["BLOCKED", "READY"], default=None)
    return parser


def main() -> None:
    args = _parser().parse_args()
    payload = validate(
        args.html,
        expected_execution_status=args.expected_execution_status,
        pdf_path=args.pdf,
    )
    json_output = Path(args.output_json)
    markdown_output = Path(args.output_md)
    for output in (json_output, markdown_output):
        if output.exists():
            raise FileExistsError(f"QA output already exists: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_output.write_text(_markdown(payload), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False))
    if payload["status"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
