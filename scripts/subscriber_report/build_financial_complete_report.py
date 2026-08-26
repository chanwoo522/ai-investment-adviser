from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pandas as pd
from bs4 import BeautifulSoup

from scripts.financials.build_public_financial_metrics import round_half_up


PUBLIC_FORBIDDEN_METHOD_TERMS = (
    "직접 공시",
    "재구성",
    "공시기반",
    "계산 단계",
    "source tier",
    "source type",
    "formula used",
    "xbrl fact",
    "note table",
    "corporate-action reconstruction",
    "price_div_eps",
    "mcap_div_net_income",
    "산출기준",
    "산식:",
    "최신 비교수치 반영",
    "receipt number",
    "taxonomy version",
    "na reason",
)


def _format_eps(value: float) -> str:
    return f"{round_half_up(value):,}원"


def _format_net_income(value: float) -> str:
    return f"{round_half_up(value / 100_000_000):,}억원"


def _format_per(value: object, status: str) -> str:
    if status == "LOSS":
        return "적자"
    return f"{float(value):,.2f}배"


def build_financial_complete_html(
    *, parent_html: Path, private_metrics: pd.DataFrame, output_path: Path
) -> dict[str, Any]:
    if output_path.exists():
        raise FileExistsError(output_path)
    metrics = private_metrics.copy()
    metrics["ticker"] = metrics["ticker"].astype(str).str.zfill(6)
    metrics = metrics.set_index("ticker", verify_integrity=True)
    soup = BeautifulSoup(parent_html.read_text(encoding="utf-8"), "html.parser")
    cards = soup.select("#selected-details article.security-card")
    card_tickers = []
    for card in cards:
        ticker_node = card.select_one(".security-heading h3 small")
        if ticker_node is None:
            raise ValueError("selected security card ticker is missing")
        ticker = ticker_node.get_text(strip=True).zfill(6)
        if ticker not in metrics.index:
            raise ValueError(f"report card is outside selected metric set: {ticker}")
        card_tickers.append(ticker)
        row = metrics.loc[ticker]
        values = {
            "EPS": _format_eps(float(row["eps_ttm"])),
            "당기순이익": _format_net_income(float(row["net_income_ttm"])),
            "PER": _format_per(row["per_ttm"], str(row["per_status"])),
        }
        for metric in card.select(".metrics-grid .metric"):
            label = metric.select_one("span")
            strong = metric.select_one("strong")
            if label and strong and label.get_text(strip=True) in values:
                strong.string = values[label.get_text(strip=True)]
        for note in card.select(".formula-note"):
            note.decompose()
    if card_tickers != list(metrics.sort_values("model_rank").index):
        raise ValueError("selected card order differs from model rank order")
    document = "<!doctype html>\n" + str(soup)
    lowered = document.lower()
    leaked = [term for term in PUBLIC_FORBIDDEN_METHOD_TERMS if term.lower() in lowered]
    if leaked:
        raise ValueError(f"private method term leaked into public report: {leaked}")
    if re.search(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]", document):
        raise ValueError("absolute path leaked into public HTML")
    parsed = BeautifulSoup(document, "html.parser")
    incomplete = []
    for card in parsed.select("#selected-details article.security-card"):
        ticker = card.select_one(".security-heading h3 small").get_text(strip=True)
        metric_values = {
            item.select_one("span").get_text(strip=True): item.select_one("strong").get_text(strip=True)
            for item in card.select(".metrics-grid .metric")
        }
        for field in ("EPS", "당기순이익", "PER"):
            value = metric_values.get(field, "")
            if not value or value in {"NA", "-", "null", "자료 없음", "자료 미확보", "DENOMINATOR_MISSING", "계약 미충족"}:
                incomplete.append({"ticker": ticker, "field": field, "value": value})
    if incomplete:
        raise ValueError(f"public card financial metrics incomplete: {incomplete}")
    copied_assets: list[str] = []
    for node in parsed.find_all(["img", "source"]):
        source_ref = str(node.get("src", "")).strip().split("?", 1)[0]
        if not source_ref or source_ref.startswith(("data:", "http://", "https://", "//")):
            continue
        relative = Path(source_ref)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe public asset reference: {source_ref}")
        source = parent_html.parent / relative
        target = output_path.parent / relative
        if not source.is_file():
            raise FileNotFoundError(source)
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        copied_assets.append(relative.as_posix())
    output_path.write_text(document, encoding="utf-8")
    return {
        "selected_card_count": len(cards),
        "selected_card_order": card_tickers,
        "financial_metric_blank_count": 0,
        "forbidden_method_term_count": 0,
        "absolute_path_count": 0,
        "copied_public_assets": copied_assets,
        "status": "PASS",
    }
