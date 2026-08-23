from __future__ import annotations

import html
import math
import re
from html.parser import HTMLParser
from typing import Any, Mapping, Sequence

import pandas as pd


REPORT_CONTRACT = "QUARTERLY_ADVISOR_REPORT_V2"
ADVISOR_MODE = "ADVISOR_FULL_RESET_V2"

PHILOSOPHY_TEXT = (
    "본 보고서는 현재 보유 주식을 모두 현금화한 뒤 다음 분기 포트폴리오를 "
    "처음부터 다시 구성한다는 가정으로 산출된 투자판단 보조자료입니다. "
    "기존 보유 여부는 종목 점수와 편입 여부에 영향을 주지 않습니다. "
    "제시된 수량은 기준가격에 따른 참고수량이며 주문지시가 아닙니다."
)

BASE_SECTION_TITLES = (
    "핵심 결론",
    "현재 계좌 요약",
    "지난 분기 모델 성과",
    "실제 계좌 잔고 변화",
    "가상 전량청산 및 재구성 자본",
    "신규 Top-K와 경계 watchlist",
    "목표 포트폴리오",
    "현재 구성과 목표 구성 비교",
    "선발 종목 정량 상세",
    "포트폴리오 섹터·품질·밸류에이션 위험",
    "한계와 사용자 판단사항",
)
UNVERIFIED_ACCOUNT_SECTION_TITLE = "실제 계좌 잔고 변화 — 성과 미인증"

_TRANSITION_LABELS = {
    "NEW_SELECTION": "신규 선발",
    "RESELECTED": "재선발",
    "DROPPED": "미선발",
    "NOT_HELD_NOT_SELECTED": "비보유·미선발",
    "CASH_EQUIVALENT": "현금성 자산",
    "ACCOUNT_CASH": "현금",
    "EQUITY_BUCKET": "주식",
}

_VALUE_LABELS = {
    **_TRANSITION_LABELS,
    "CASH_EQUIVALENT_BUCKET": "현금성 자산",
    "CASH_EQUIVALENT": "현금성 자산",
    "EQUITY": "주식",
    "CASH": "현금",
    "LIABILITY": "부채",
    "UNSUPPORTED": "지원 제외 자산",
    "SELECTED": "선발",
    "NOT_SELECTED": "미선발",
    "WATCHLIST_NOT_SELECTED": "경계 관찰·미선발",
    "PASS": "통과",
    "VERIFIED": "확인 완료",
    "COMPLETE": "완전",
    "INCOMPLETE": "불완전",
    "PARTIAL": "일부만 확보",
    "FRAGILE": "경계 취약",
    "STABLE": "경계 안정",
    "PASS_CONTRACT_CORRECTION": "계약 정정 검증 통과",
    "BLOCKED_PENDING_HISTORICAL_VALIDATION": "역사 검증 완료 전 production 승격 차단",
    "PROVISIONAL_ACCOUNT_PERFORMANCE": "활동원장 부재로 성과 미인증",
    "PROVISIONAL_LEGACY_ARTIFACT": "기존 자료의 인증 근거 불충분",
    "PROVISIONAL_EXPLICIT_LEGACY_ARTIFACT": "기존 자료의 인증 근거 불충분",
    "PROVISIONAL_INCOMPLETE_DATA": "역사 검증 미완료",
    "UNAVAILABLE_HISTORICAL_FILTER_INPUTS": "역사 필터 입력 미확보",
    "BLOCKED": "차단",
    "CERTIFIED_ACCOUNT_PERFORMANCE": "외부 현금흐름 조정 성과 인증",
    "ZERO_NOMINAL_RETURN_CONSERVATIVE": "명목수익률 0% 보수적 가정",
    "ALIGNED_PERIOD_AVAILABLE": "동일 기간 비교 가능",
    "PARTIAL_PERIOD_MISMATCH": "비교 기간 불일치",
    "UNAVAILABLE": "자료 없음",
    "DATA_NOT_AVAILABLE": "자료 없음",
    "NOT_AVAILABLE": "자료 없음",
    "NA": "자료 없음",
    "NONE": "해당 없음",
}

_BLOCKER_LABELS = {
    "SCORE_PARITY_FAILURE": "기존 모델 점수와 fresh-start 점수의 동일성 미확인",
    "HISTORICAL_PIT_UNIVERSE_UNAVAILABLE": "과거 시점별 투자대상군 자료 미확보",
    "HISTORICAL_MCAP_TRADED_VALUE_UNAVAILABLE": "과거 시가총액·거래대금 자료 미확보",
    "RETURN_COVERAGE_INCOMPLETE": "종목 수익률 이력 범위 불완전",
    "FULL_CALENDAR_RETURN_COVERAGE_INCOMPLETE": "전체 월별 수익률 이력 범위 불완전",
    "KRX300_HISTORY_INCOMPLETE": "동일 기간 KRX300 이력 불완전",
    "KRX300_ALIGNED_HISTORY_INCOMPLETE": "동일 기간 KRX300 이력 불완전",
    "HISTORICAL_SELL_TAX_SCHEDULE_UNAVAILABLE": "과거 매도세율 일정 미확보",
    "FRESH_START_BACKTEST_NOT_VALIDATED": "fresh-start 역사 검증 미완료",
    "VALUATION_LAYER_MISSING": "내부 재무자료 기반 밸류에이션 검증 미완료",
    "ACCOUNT_SIZING_DATE_MISMATCH": "계좌 평가일과 목표 수량 가격일 계약 불일치",
    "INVALID_INDUSTRY_MAPPING": "업종 매핑 검증 실패",
    "NAV_IDENTITY_FAILURE": "계좌 NAV 항등식 검증 실패",
    "TARGET_WEIGHT_FAILURE": "목표 비중 합계 검증 실패",
    "ACTIVITY_LEDGER_UNAVAILABLE": "활동원장 미제공",
    "PRIOR_MODEL_ARTIFACT_PROVISIONAL": "직전 모델 자료의 인증 수준 제한",
    "PRIOR_MODEL_BENCHMARK_PERIOD_MISMATCH": "직전 모델과 비교지수의 기간 불일치",
    "ACTUAL_ACCOUNT_RETURN_NOT_CASHFLOW_ADJUSTED": "실제 계좌 변화가 외부 현금흐름에 맞춰 조정되지 않음",
    "437350_CUTOFF_PRICE_NON_KRX_PRIMARY_SOURCE": "437350 계좌 평가가격의 KRX 1차 출처 확인 필요",
}

_COLUMN_LABELS = {
    "rank": "순위",
    "model_rank": "순위",
    "ticker": "종목코드",
    "security_id": "종목코드",
    "name": "종목명",
    "asset_class": "자산구분",
    "market": "시장",
    "broker_qty": "현재 수량",
    "current_qty": "현재 수량",
    "quantity": "현재 수량",
    "broker_market_price": "증권사 현재가",
    "broker_price": "증권사 현재가",
    "broker_market_value": "계좌 평가금액",
    "market_value": "평가금액",
    "current_value": "현재 평가금액",
    "current_weight": "현재 비중",
    "account_valuation_asof": "계좌 평가일",
    "model_score": "모델 점수",
    "quality_penalty": "품질 패널티",
    "quality_penalty_total": "품질 패널티",
    "quality_penalty_reason": "품질 패널티 사유",
    "official_industry_name": "공식 업종",
    "official_industry_source": "공식 업종 출처",
    "advisor_sector": "투자위험 섹터",
    "advisor_sector_source": "투자위험 섹터 출처",
    "selection_status": "선정 상태",
    "score_gap_vs_k": "10위 대비 점수차",
    "score_gap_vs_previous": "직전 순위 대비 점수차",
    "target_weight": "목표 비중",
    "target_value": "목표금액",
    "illustrative_target_value": "목표금액",
    "reference_price": "참고가격",
    "reference_price_asof": "참고가격 기준일",
    "reference_target_qty": "참고수량(주문 아님)",
    "transition_status": "구성 변화",
    "previously_held": "기존 보유",
    "hypothetical_sell_market_value": "가상 매도금액",
    "hypothetical_sell_value": "가상 매도금액",
    "hypothetical_sell_commission": "가상 매도수수료",
    "hypothetical_sell_tax": "가상 매도세금",
    "hypothetical_net_proceeds": "순청산대금",
    "advisor_sector_source": "섹터 분류 근거",
    "position_count": "종목 수",
    "sector_value": "평가·목표금액",
    "sector_weight": "섹터 비중",
    "weight_basis": "비중 기준",
}

_MONEY_COLUMNS = frozenset(
    {
        "broker_market_price",
        "broker_price",
        "broker_market_value",
        "market_value",
        "current_value",
        "target_value",
        "illustrative_target_value",
        "reference_price",
        "hypothetical_sell_market_value",
        "hypothetical_sell_value",
        "hypothetical_sell_commission",
        "hypothetical_sell_tax",
        "hypothetical_net_proceeds",
        "sector_value",
        "revenue_q_minus_2",
        "revenue_q_minus_1",
        "revenue_latest_q",
        "operating_income_q_minus_2",
        "operating_income_q_minus_1",
        "operating_income_latest_q",
        "revenue_ttm",
        "operating_income_ttm",
        "parent_net_income_ttm",
        "cfo_ttm",
        "market_cap_asof",
        "parent_equity_latest",
        "enterprise_value",
    }
)
_WEIGHT_COLUMNS = frozenset(
    {
        "current_weight",
        "target_weight",
        "sector_weight",
        "prior_target_weight",
        "weight_gap",
    }
)
_RATIO_COLUMNS = frozenset(
    {
        "endpoint_nav_change_ratio_unadjusted",
        "model_price_return",
        "benchmark_return",
        "excess_return",
        "model_mdd",
    }
)

_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:account[_\s-]*(?:id|number|no)|계좌\s*(?:번호|id)|"
    r"broker[_\s-]*account[_\s-]*file|raw[_\s-]*(?:file|xlsx)|"
    r"source[_\s-]*(?:file|path)|file[_\s-]*(?:name|path)|"
    r"absolute[_\s-]*path|\bsha(?:1|256|512)?\b|\bchecksum\b|\bdigest\b|\bhash\b)"
)
_ABSOLUTE_PATH_RE = re.compile(r"(?i)(?:\b[a-z]:[\\/]|\\\\[^\\\s]+[\\/]|file://|/(?:users|home|mnt|var|tmp)/)")
_SPREADSHEET_RE = re.compile(r"(?i)\b[^\s<>\"']+\.xls(?:x|m|b)?\b")
_HASH_RE = re.compile(r"(?i)\b[0-9a-f]{32,128}\b")
_ACCOUNT_LABEL_RE = re.compile(r"(?i)(?:account\s*(?:id|number|no)|계좌\s*번호)\s*[:=]?\s*[\w-]+")
_URL_RE = re.compile(r"(?i)\b(?:https?|ftp)://")
_INTERNAL_ENUM_RE = re.compile(r"\b[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+\b")
_FORBIDDEN_QTY_RE = re.compile(r"(?i)\b(?:planned_qty|order_qty|trade_qty|executable_qty)\b")
_UNVALIDATED_METRIC_RE = re.compile(
    r"(?i)(?:\bCAGR\b|\bMDD\b|\bSharpe\b|샤프|적중률|\bturnover\b|회전율|누적\s*거래비용|total_trading_costs)"
)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _is_missing(value: Any) -> bool:
    if value is None or value is pd.NA:
        return True
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    if isinstance(missing, bool):
        return missing
    if not hasattr(missing, "__len__"):
        try:
            return bool(missing)
        except (TypeError, ValueError):
            return False
    return False


def _flatten(mapping: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for key, value in mapping.items():
        key_text = str(key)
        path = f"{prefix}.{key_text}" if prefix else key_text
        if isinstance(value, Mapping):
            flattened.update(_flatten(value, path))
        else:
            flattened[path] = value
    return flattened


def _find(mapping: Mapping[str, Any], aliases: Sequence[str], default: Any = None) -> Any:
    flattened = _flatten(mapping)
    for alias in aliases:
        if alias in mapping:
            return mapping[alias]
        if alias in flattened:
            return flattened[alias]
    for alias in aliases:
        for key, value in flattened.items():
            if key.rsplit(".", 1)[-1] == alias:
                return value
    return default


def _coerce_frame(value: Any) -> pd.DataFrame:
    if value is None:
        return pd.DataFrame()
    if isinstance(value, pd.DataFrame):
        return value.copy()
    if isinstance(value, pd.Series):
        return value.to_frame().T
    if isinstance(value, Mapping):
        try:
            return pd.DataFrame(value)
        except ValueError:
            return pd.DataFrame([dict(value)])
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return pd.DataFrame(value)
    raise TypeError(f"표 입력은 DataFrame 또는 레코드여야 합니다: {type(value).__name__}")


def _coerce_selected_details(value: Any) -> pd.DataFrame:
    """Accept a combined frame or the canonical financial/diagnostic pair."""

    if not isinstance(value, Mapping) or not any(
        key in value
        for key in (
            "financials",
            "diagnostics",
            "selected_security_financials",
            "selected_security_diagnostics",
        )
    ):
        return _coerce_frame(value)
    financials = _coerce_frame(
        value.get("financials", value.get("selected_security_financials"))
    )
    diagnostics = _coerce_frame(
        value.get("diagnostics", value.get("selected_security_diagnostics"))
    )
    if financials.empty:
        return diagnostics
    if diagnostics.empty:
        return financials
    if "ticker" not in financials.columns or "ticker" not in diagnostics.columns:
        raise ValueError("selected financials and diagnostics require ticker")
    diagnostic_columns = [
        column
        for column in diagnostics.columns
        if column == "ticker" or column not in financials.columns
    ]
    return financials.merge(
        diagnostics.loc[:, diagnostic_columns],
        on="ticker",
        how="outer",
        validate="one_to_one",
    )


def _safe_text(value: Any) -> str:
    if _is_missing(value):
        return "자료 없음"
    text = str(value).strip()
    if not text:
        return "자료 없음"
    if (
        _ABSOLUTE_PATH_RE.search(text)
        or _SPREADSHEET_RE.search(text)
        or _HASH_RE.search(text)
        or _ACCOUNT_LABEL_RE.search(text)
        or _URL_RE.search(text)
        or _FORBIDDEN_QTY_RE.search(text)
    ):
        return "비공개 값"
    return text[:800]


def _display_value(value: Any) -> str:
    if _is_missing(value):
        return "자료 없음"
    text = str(value).strip()
    if text in _VALUE_LABELS:
        return _VALUE_LABELS[text]
    if text in _BLOCKER_LABELS:
        return _BLOCKER_LABELS[text]
    if _INTERNAL_ENUM_RE.fullmatch(text):
        normalized = text.replace("_", " ").lower()
        return _safe_text(normalized)
    return _safe_text(text)


def _format_value(column: str, value: Any) -> str:
    if _is_missing(value):
        return "자료 없음"
    if isinstance(value, bool):
        return "예" if value else "아니요"
    if column in {"ticker", "security_id"}:
        text = str(value).strip()
        if text in _VALUE_LABELS:
            return _VALUE_LABELS[text]
        if text.endswith(".0") and text[:-2].isdigit():
            text = text[:-2]
        return text.zfill(6) if text.isdigit() else _safe_text(text)
    if column.endswith("_period"):
        return _safe_text(value)
    if column in {
        "model_information_asof",
        "account_valuation_asof",
        "reference_price_asof",
        "valuation_asof",
        "start_date",
        "end_date",
    } or column.endswith("_date"):
        parsed = pd.to_datetime(value, errors="coerce")
        if not pd.isna(parsed):
            return parsed.date().isoformat()
    if column in {"transition_status", "selection_status", "asset_class", "status"}:
        return _display_value(value)
    number = _number(value)
    if number is None:
        return _display_value(value)
    if column in _WEIGHT_COLUMNS or column in _RATIO_COLUMNS:
        return f"{number * 100:.2f}%"
    if column in _MONEY_COLUMNS or any(
        token in column.lower() for token in ("capital", "nav", "cash_value", "liability_value")
    ):
        return f"{number:,.0f}원"
    if column in {"current_qty", "broker_qty", "quantity", "reference_target_qty"}:
        return f"{int(math.floor(number)):,}주"
    if column in {"rank", "model_rank", "position_count"}:
        return f"{int(number):,}"
    if column in {"per_ttm", "pbr", "psr_ttm", "ev_to_operating_income_ttm", "cfo_conversion_ttm"}:
        return f"{number:,.2f}배"
    return f"{number:,.6f}".rstrip("0").rstrip(".")


def _label(column: str) -> str:
    if column in _COLUMN_LABELS:
        return _COLUMN_LABELS[column]
    text = column
    text = re.sub(r"(?:__)?(?:contrib|contribution)$", " 기여도", text, flags=re.I)
    return text.replace("_", " ").strip()


def _balanced_chunk_sizes(length: int, maximum: int) -> list[int]:
    if length <= 0:
        return []
    if length <= maximum:
        return [length]
    groups = math.ceil(length / maximum)
    base, remainder = divmod(length, groups)
    return [base + (1 if index < remainder else 0) for index in range(groups)]


def _render_table(
    value: Any,
    *,
    table_id: str,
    caption: str,
    columns: Sequence[str],
    max_rows_per_chunk: int = 8,
    max_total_rows: int = 100,
) -> str:
    frame = _coerce_frame(value)
    safe_columns = [
        column
        for column in columns
        if column in frame.columns and not _SENSITIVE_KEY_RE.search(str(column))
    ][:8]
    if frame.empty or not safe_columns:
        return f'<div class="empty-state"><strong>{html.escape(caption)}</strong><span>자료 없음</span></div>'

    shown = frame.loc[:, safe_columns].head(max_total_rows).reset_index(drop=True)
    sizes = _balanced_chunk_sizes(len(shown), max_rows_per_chunk)
    offset = 0
    chunks: list[str] = []
    for index, size in enumerate(sizes):
        part = shown.iloc[offset : offset + size]
        offset += size
        continuation = index > 0
        headers = "".join(
            f'<th scope="col">{html.escape(_label(str(column)))}</th>'
            for column in safe_columns
        )
        rows = []
        for _, row in part.iterrows():
            cells = "".join(
                f"<td>{html.escape(_format_value(str(column), row[column]))}</td>"
                for column in safe_columns
            )
            rows.append(f"<tr>{cells}</tr>")
        suffix = " (계속)" if continuation else ""
        chunks.append(
            '<div class="table-chunk" '
            f'data-row-count="{size}" data-continuation="{str(continuation).lower()}">'
            '<div class="table-wrap">'
            f'<table id="{table_id}{"-" + str(index + 1) if continuation else ""}" '
            f'data-column-count="{len(safe_columns)}">'
            f"<caption>{html.escape(caption + suffix)}</caption>"
            f"<thead><tr>{headers}</tr></thead><tbody>{''.join(rows)}</tbody>"
            "</table></div></div>"
        )
    omitted = len(frame) - len(shown)
    if omitted > 0:
        chunks.append(
            f'<p class="table-note">가독성을 위해 나머지 {omitted:,}개 행은 private audit 자료에 보존했습니다.</p>'
        )
    return "".join(chunks)


def _card(label: str, value: Any, column: str = "status") -> str:
    rendered = _format_value(column, value)
    return (
        '<div class="metric-card">'
        f'<span class="metric-label">{html.escape(label)}</span>'
        f'<strong>{html.escape(rendered)}</strong></div>'
    )


def _cards(specs: Sequence[tuple[str, Any, str]]) -> str:
    return '<div class="metric-grid">' + "".join(
        _card(label, value, column) for label, value, column in specs
    ) + "</div>"


def _as_list(value: Any) -> list[str]:
    if value is None or _is_missing(value):
        return []
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped in {"[]", "()", "NONE", "None"}:
            return []
        return [item.strip() for item in re.split(r"[;,|]", stripped) if item.strip()]
    if isinstance(value, Mapping):
        return [str(key) for key, enabled in value.items() if bool(enabled)]
    if isinstance(value, Sequence):
        return [str(item).strip() for item in value if not _is_missing(item) and str(item).strip()]
    return [str(value).strip()]


def _taxonomy_value(summary: Mapping[str, Any], qa: Any, category: str) -> list[str]:
    aliases = (
        category,
        category.lower(),
        f"blockers.{category}",
        f"blocker_taxonomy.{category}",
        f"blocker_taxonomy.{category.lower()}",
    )
    result = _as_list(_find(summary, aliases))
    if not result and isinstance(qa, Mapping):
        result = _as_list(_find(qa, aliases))
    return result


def _blocker_list(items: Sequence[str], *, empty_text: str) -> str:
    if not items:
        return f'<p class="status-note positive">{html.escape(empty_text)}</p>'
    rendered = "".join(f"<li>{html.escape(_display_value(item))}</li>" for item in items[:10])
    return f'<ul class="notice-list">{rendered}</ul>'


def _row_lookup(frame: pd.DataFrame) -> dict[str, Mapping[str, Any]]:
    if frame.empty:
        return {}
    identifier = "ticker" if "ticker" in frame.columns else "security_id" if "security_id" in frame.columns else None
    if identifier is None:
        return {}
    lookup: dict[str, Mapping[str, Any]] = {}
    for _, row in frame.iterrows():
        raw = str(row.get(identifier, "")).replace(".0", "").strip()
        key = raw.zfill(6) if raw.isdigit() else raw
        lookup.setdefault(key, row.to_dict())
    return lookup


def _pick(row: Mapping[str, Any], aliases: Sequence[str]) -> Any:
    for alias in aliases:
        if alias in row and not _is_missing(row[alias]):
            return row[alias]
    return None


def _merge_security_rows(
    top_k: pd.DataFrame,
    selected_details: pd.DataFrame,
    valuation_qa: pd.DataFrame,
) -> list[dict[str, Any]]:
    detail_lookup = _row_lookup(selected_details)
    valuation_lookup = _row_lookup(valuation_qa)
    source = top_k.copy() if not top_k.empty else selected_details.copy()
    if "model_rank" in source.columns:
        source = source.sort_values("model_rank", kind="mergesort")
    records: list[dict[str, Any]] = []
    for _, source_row in source.head(10).iterrows():
        base = source_row.to_dict()
        ticker_value = _pick(base, ("ticker", "security_id"))
        ticker = _format_value("ticker", ticker_value)
        merged = dict(base)
        merged.update({key: value for key, value in detail_lookup.get(ticker, {}).items() if not _is_missing(value)})
        merged.update({key: value for key, value in valuation_lookup.get(ticker, {}).items() if not _is_missing(value)})
        merged["ticker"] = ticker
        records.append(merged)
    return records


def _validate_selected_industry_contract(
    top_k: pd.DataFrame,
    selected_details: pd.DataFrame,
    valuation_qa: pd.DataFrame,
) -> None:
    records = _merge_security_rows(top_k, selected_details, valuation_qa)
    for row in records:
        ticker = _format_value("ticker", row.get("ticker"))
        industry_name = _pick(row, ("official_industry_name",))
        industry_source = _pick(row, ("official_industry_source",))
        advisor_sector = _pick(row, ("advisor_sector",))
        advisor_sector_source = _pick(row, ("advisor_sector_source",))
        if any(
            _is_missing(value)
            for value in (
                industry_name,
                industry_source,
                advisor_sector,
                advisor_sector_source,
            )
        ):
            raise ValueError(
                f"Top-K industry/advisor sector lineage is incomplete: {ticker}"
            )
        if ticker == "322000" and (
            "반도체" in str(industry_name)
            or "SEMICONDUCTOR" in str(industry_name).upper()
        ):
            raise ValueError("322000 known-invalid industry mapping detected")


def _security_detail_cards(
    top_k: pd.DataFrame,
    selected_details: pd.DataFrame,
    valuation_qa: pd.DataFrame,
) -> str:
    records = _merge_security_rows(top_k, selected_details, valuation_qa)
    if not records:
        return '<div class="empty-state"><strong>선발 종목 정량 상세</strong><span>자료 없음</span></div>'
    articles: list[str] = []
    for row in records:
        rank = _format_value("model_rank", _pick(row, ("model_rank", "rank")))
        ticker = _format_value("ticker", row.get("ticker"))
        name = _safe_text(_pick(row, ("name", "security_name")))
        header = f"{rank}위 · {ticker} {name}"
        overview = _cards(
            (
                ("모델 점수", _pick(row, ("model_score", "score")), "model_score"),
                ("공식 업종", row.get("official_industry_name"), "status"),
                ("공식 업종 근거", row.get("official_industry_source"), "status"),
                ("투자위험 섹터", row.get("advisor_sector"), "status"),
                ("섹터 분류 근거", row.get("advisor_sector_source"), "status"),
                ("재무제표 범위", row.get("statement_scope"), "status"),
                ("밸류에이션 기준일", row.get("valuation_asof"), "valuation_asof"),
            )
        )
        q_periods = (
            _format_value("quarter_minus_2_period", row.get("quarter_minus_2_period")),
            _format_value("quarter_minus_1_period", row.get("quarter_minus_1_period")),
            _format_value("latest_quarter_period", row.get("latest_quarter_period")),
        )
        quarterly = (
            '<div class="quarter-panel"><h4>최근 3개 분기</h4><div class="table-wrap">'
            '<table class="quarter-table" data-column-count="4"><thead><tr><th>항목</th>'
            + "".join(f"<th>{html.escape(period)}</th>" for period in q_periods)
            + "</tr></thead><tbody>"
            + "<tr><th>매출</th>"
            + "".join(
                f"<td>{html.escape(_format_value(column, row.get(column)))}</td>"
                for column in ("revenue_q_minus_2", "revenue_q_minus_1", "revenue_latest_q")
            )
            + "</tr><tr><th>영업이익</th>"
            + "".join(
                f"<td>{html.escape(_format_value(column, row.get(column)))}</td>"
                for column in (
                    "operating_income_q_minus_2",
                    "operating_income_q_minus_1",
                    "operating_income_latest_q",
                )
            )
            + "</tr></tbody></table></div></div>"
        )
        fundamentals = _cards(
            (
                ("TTM 매출", row.get("revenue_ttm"), "revenue_ttm"),
                ("TTM 영업이익", row.get("operating_income_ttm"), "operating_income_ttm"),
                ("지배주주순이익 TTM", row.get("parent_net_income_ttm"), "parent_net_income_ttm"),
                ("CFO TTM", row.get("cfo_ttm"), "cfo_ttm"),
                ("CFO/영업이익", row.get("cfo_conversion_ttm"), "cfo_conversion_ttm"),
                ("시가총액", row.get("market_cap_asof"), "market_cap_asof"),
                ("PER(TTM)", row.get("per_ttm"), "per_ttm"),
                ("PBR", row.get("pbr"), "pbr"),
                ("PSR(TTM)", row.get("psr_ttm"), "psr_ttm"),
                ("EV/영업이익(TTM)", row.get("ev_to_operating_income_ttm"), "ev_to_operating_income_ttm"),
            )
        )
        factor_columns = [
            str(column)
            for column in row
            if re.search(r"(?:__)?(?:contrib|contribution)$", str(column), flags=re.I)
            and not _is_missing(row[column])
        ][:8]
        factor_items = "".join(
            f'<li><span>{html.escape(_label(column))}</span><strong>{html.escape(_format_value(column, row[column]))}</strong></li>'
            for column in factor_columns
        )
        if not factor_items:
            factor_items = "<li><span>팩터별 기여도</span><strong>자료 없음</strong></li>"
        quality_reason = _display_value(
            _pick(row, ("quality_penalty_reason", "quality_penalty_reasons", "quality_reason"))
        )
        quality_penalty = _format_value(
            "quality_penalty", _pick(row, ("quality_penalty", "quality_penalty_total"))
        )
        diagnostics = (
            '<div class="factor-panel"><h4>품질 및 팩터 진단</h4>'
            f'<p><strong>품질 패널티:</strong> {html.escape(quality_penalty)} · '
            f'<strong>사유:</strong> {html.escape(quality_reason)}</p>'
            f'<ul class="factor-list">{factor_items}</ul>'
            '<p class="table-note">CFO/영업이익은 비점수 진단값이며 향후 품질 overlay 백테스트 후보입니다.</p>'
            "</div>"
        )
        articles.append(
            '<article class="security-card">'
            f"<h3>{html.escape(header)}</h3>{overview}{quarterly}{fundamentals}{diagnostics}</article>"
        )
    return "".join(articles)


def _extract_sector_frames(value: Any) -> tuple[pd.DataFrame, pd.DataFrame]:
    if isinstance(value, Mapping):
        current_value = value.get("current")
        if current_value is None:
            current_value = value.get("actual")
        current = _coerce_frame(current_value)
        target = _coerce_frame(value.get("target"))
        return current, target
    frame = _coerce_frame(value)
    if frame.empty:
        return frame.copy(), frame.copy()
    scope_column = next(
        (column for column in ("portfolio_scope", "scope", "weight_basis") if column in frame.columns),
        None,
    )
    if scope_column is None:
        return pd.DataFrame(columns=frame.columns), frame
    scopes = frame[scope_column].astype("string").str.lower()
    current = frame.loc[scopes.str.contains("current|actual|account", regex=True, na=False)].copy()
    target = frame.loc[scopes.str.contains("target|model", regex=True, na=False)].copy()
    return current, target


def _boundary_values(summary: Mapping[str, Any], boundary: pd.DataFrame, k: int) -> tuple[Any, Any, Any, Any]:
    score_column = "model_score" if "model_score" in boundary.columns else "score" if "score" in boundary.columns else None
    rank_column = "rank" if "rank" in boundary.columns else "model_rank" if "model_rank" in boundary.columns else None
    kth = _find(summary, ("boundary.rank_k_score", "rank_k_score", "top_k_boundary.rank_k_score", "rank_10_score"))
    next_score = _find(summary, ("boundary.rank_k_plus_1_score", "rank_k_plus_1_score", "top_k_boundary.rank_k_plus_1_score", "rank_11_score"))
    if score_column and rank_column and not boundary.empty:
        ranks = pd.to_numeric(boundary[rank_column], errors="coerce")
        if _is_missing(kth) and ranks.eq(k).any():
            kth = boundary.loc[ranks.eq(k), score_column].iloc[0]
        if _is_missing(next_score) and ranks.eq(k + 1).any():
            next_score = boundary.loc[ranks.eq(k + 1), score_column].iloc[0]
    gap = _find(
        summary,
        (
            "boundary.score_gap_k_vs_k_plus_1",
            "score_gap_k_vs_k_plus_1",
            "top_k_boundary.score_gap_k_vs_k_plus_1",
            "rank_10_11_score_gap",
            "rank_k_minus_k_plus_1_gap",
            "rank_k_minus_rank_k_plus_1_gap",
        ),
    )
    if _is_missing(gap):
        kth_number, next_number = _number(kth), _number(next_score)
        gap = kth_number - next_number if kth_number is not None and next_number is not None else None
    status = _find(summary, ("boundary.selection_boundary_status", "selection_boundary_status", "top_k_boundary.selection_boundary_status"))
    return kth, next_score, gap, status


def generate_advisor_v2_html(
    summary: Mapping[str, Any],
    *,
    current_portfolio: Any = None,
    liquidation: Any = None,
    top_k: Any = None,
    boundary_watchlist: Any = None,
    target_portfolio: Any = None,
    current_vs_target: Any = None,
    selected_details: Any = None,
    valuation_qa: Any = None,
    sector_exposure: Any = None,
    prior_model_performance: Any = None,
    actual_account_performance: Any = None,
    qa: Any = None,
) -> str:
    """Render the standalone ADVISOR_FULL_RESET_V2 user report.

    The renderer accepts only in-memory values, never reads a broker workbook,
    and deliberately leaves raw QA enums and provisional backtest statistics in
    the private audit bundle.  ``qa`` is used only to enforce blocker taxonomy.
    """

    if not isinstance(summary, Mapping):
        raise TypeError("summary는 mapping이어야 합니다")
    supplied_mode = _find(summary, ("mode", "run_mode"), ADVISOR_MODE)
    if str(supplied_mode).strip() not in {"", ADVISOR_MODE, "ADVISOR_FULL_RESET"}:
        raise ValueError(f"지원하지 않는 advisor mode: {supplied_mode}")

    model_blockers = _taxonomy_value(summary, qa, "MODEL_PROMOTION_BLOCKERS")
    report_blockers = _taxonomy_value(summary, qa, "ADVISOR_REPORT_BLOCKERS")
    performance_warnings = _taxonomy_value(summary, qa, "PERFORMANCE_CERTIFICATION_WARNINGS")
    account_warnings = _taxonomy_value(summary, qa, "ACCOUNT_VALUATION_WARNINGS")
    if report_blockers:
        labels = "; ".join(_display_value(item) for item in report_blockers)
        raise ValueError(f"advisor report blocker가 해소되지 않았습니다: {labels}")

    current = _coerce_frame(current_portfolio)
    liquidation_frame = _coerce_frame(liquidation)
    top = _coerce_frame(top_k)
    boundary = _coerce_frame(boundary_watchlist)
    target = _coerce_frame(target_portfolio)
    comparison = _coerce_frame(current_vs_target)
    details = _coerce_selected_details(selected_details)
    valuation = _coerce_frame(valuation_qa)
    prior = _coerce_frame(prior_model_performance)
    actual = _coerce_frame(actual_account_performance)
    sector_current, sector_target = _extract_sector_frames(sector_exposure)
    _validate_selected_industry_contract(top, details, valuation)

    actual_summary_raw = _find(summary, ("actual_account_performance",), {})
    actual_summary: Mapping[str, Any] = (
        actual_summary_raw if isinstance(actual_summary_raw, Mapping) else {}
    )
    actual_row: Mapping[str, Any] = actual.iloc[0].to_dict() if not actual.empty else {}
    actual_context = dict(actual_row)
    actual_context.update(actual_summary)
    ledger_status = _find(
        actual_context,
        ("activity_ledger_status", "ledger_status", "activity_ledger_certification_status"),
        "NOT_AVAILABLE",
    )
    performance_status = _find(
        actual_context,
        ("performance_status", "account_performance_status"),
        "PROVISIONAL_ACCOUNT_PERFORMANCE",
    )
    ledger_verified = str(ledger_status).upper() in {"VERIFIED", "CERTIFIED", "AVAILABLE_VERIFIED"}
    performance_certified = (
        ledger_verified
        and str(performance_status).upper() == "CERTIFIED_ACCOUNT_PERFORMANCE"
    )
    account_section_title = (
        BASE_SECTION_TITLES[3] if performance_certified else UNVERIFIED_ACCOUNT_SECTION_TITLE
    )

    model_information_asof = _find(
        summary,
        ("model_information_asof", "date_basis.model_information_asof"),
    )
    account_valuation_asof = _find(
        summary,
        ("account_valuation_asof", "date_basis.account_valuation_asof", "account_asof"),
    )
    reference_price_asof = _find(
        summary,
        ("reference_price_asof", "date_basis.reference_price_asof"),
    )
    score_parity_status = _find(
        summary,
        (
            "score_parity_status",
            "score_parity.status",
            "score_parity.parity_status",
            "score_parity.overall_status",
        ),
    )
    development_status = _find(summary, ("development_status",), "PASS_CONTRACT_CORRECTION")
    promotion_status = _find(
        summary,
        ("production_promotion_status", "promotion_status", "backtest.production_promotion_status"),
        "BLOCKED_PENDING_HISTORICAL_VALIDATION",
    )
    validation_status = _find(
        summary,
        ("validation_status", "backtest.validation_status", "backtest.evaluation_status"),
        "INCOMPLETE",
    )
    coverage_status = _find(
        summary,
        ("coverage_status", "backtest.coverage_status", "backtest.full_calendar_coverage_status"),
        "INCOMPLETE",
    )

    k_number = _number(_find(summary, ("top_k", "policy.top_k"), 10)) or 10
    k = int(k_number)
    kth_score, next_score, boundary_gap, boundary_status = _boundary_values(summary, boundary, k)

    prior_summary_raw = _find(summary, ("prior_model_performance",), {})
    prior_summary: Mapping[str, Any] = (
        prior_summary_raw if isinstance(prior_summary_raw, Mapping) else {}
    )
    prior_row: Mapping[str, Any] = prior.iloc[0].to_dict() if not prior.empty else {}
    prior_context = dict(prior_row)
    prior_context.update(prior_summary)
    prior_status = _find(prior_context, ("performance_status", "certification_status"), "DATA_NOT_AVAILABLE")
    prior_certified = (
        "CERTIFIED" in str(prior_status).upper()
        and "PROVISIONAL" not in str(prior_status).upper()
    )

    prior_start = _find(
        prior_context,
        ("target_date", "start_date", "start_price_date", "period_start"),
    )
    prior_end = _find(
        prior_context,
        ("evaluation_end", "end_date", "end_price_date", "period_end"),
    )
    prior_benchmark_status = _find(prior_context, ("benchmark_status", "krx300_status"))

    prior_cards: list[tuple[str, Any, str]] = [
        ("자료 인증 상태", prior_status, "status"),
        ("평가 시작일", prior_start, "start_date"),
        ("평가 종료일", prior_end, "end_date"),
        ("KRX300 비교", prior_benchmark_status, "status"),
    ]
    if prior_certified:
        prior_cards.extend(
            [
                ("모델 가격수익률", _find(prior_context, ("model_price_return",)), "model_price_return"),
                ("KRX300 가격수익률", _find(prior_context, ("benchmark_return", "krx300_return")), "benchmark_return"),
                ("초과수익률", _find(prior_context, ("excess_return", "krx300_excess_return")), "excess_return"),
                ("최대 낙폭", _find(prior_context, ("model_mdd", "max_drawdown")), "model_mdd"),
            ]
        )

    prior_note = (
        "인증된 동일 기간 자료만 성과 통계로 표시했습니다."
        if prior_certified
        else "직전 모델 자료의 인증 또는 비교기간 정렬이 충분하지 않아 성과 통계를 본문에 싣지 않았습니다."
    )

    prior_nav = _find(
        actual_context,
        ("prior_gross_account_nav", "start_nav", "prior_nav", "prior_value"),
    )
    current_nav = _find(
        actual_context,
        (
            "current_gross_account_nav",
            "end_nav",
            "current_nav",
            "gross_account_nav",
            "current_value",
        ),
    )
    endpoint_change = _find(
        actual_context,
        ("endpoint_nav_change", "endpoint_value_change", "nav_change", "value_change"),
    )
    if _is_missing(endpoint_change):
        prior_nav_number, current_nav_number = _number(prior_nav), _number(current_nav)
        endpoint_change = (
            current_nav_number - prior_nav_number
            if prior_nav_number is not None and current_nav_number is not None
            else None
        )
    endpoint_ratio = _find(
        actual_context,
        (
            "endpoint_nav_change_ratio_unadjusted",
            "endpoint_change_ratio_not_cashflow_adjusted",
        ),
    )
    if _is_missing(endpoint_ratio):
        prior_nav_number, endpoint_change_number = _number(prior_nav), _number(endpoint_change)
        endpoint_ratio = (
            endpoint_change_number / prior_nav_number
            if prior_nav_number not in {None, 0} and endpoint_change_number is not None
            else None
        )

    capital_value = _find(
        summary,
        (
            "advisory_rebalance_capital",
            "advisory_rebalance_capital_account_asof",
            "capital.advisory_rebalance_capital",
        ),
    )
    gross_nav = _find(summary, ("gross_account_nav", "account.gross_account_nav"), current_nav)
    equity_value = _find(summary, ("equity_market_value", "capital.equity_market_value"))
    cash_equivalent_value = _find(summary, ("cash_equivalent_value", "capital.cash_equivalent_value"))
    cash_value = _find(summary, ("cash_value", "signed_cash", "capital.cash_value"))
    liability_value = _find(summary, ("liability_value", "capital.liability_value"))
    net_equity_proceeds = _find(
        summary,
        ("hypothetical_equity_net_proceeds", "capital.hypothetical_equity_net_proceeds"),
    )
    target_cash_weight = _find(
        summary,
        ("target_cash_equivalent_weight", "policy.target_cash_equivalent_weight"),
        0.10,
    )
    target_equity_weight = _find(
        summary,
        ("target_equity_weight", "policy.target_equity_weight"),
        1.0 - (_number(target_cash_weight) or 0.10),
    )

    current_table_columns = (
        "ticker",
        "name",
        "asset_class",
        "current_qty",
        "broker_market_price",
        "broker_market_value",
        "current_weight",
    )
    liquidation_columns = (
        "ticker",
        "name",
        "asset_class",
        "broker_market_value",
        "hypothetical_sell_commission",
        "hypothetical_sell_tax",
        "hypothetical_net_proceeds",
    )
    top_columns = (
        "model_rank",
        "ticker",
        "name",
        "model_score",
        "official_industry_name",
        "advisor_sector",
        "quality_penalty",
    )
    boundary_columns = (
        "rank",
        "ticker",
        "name",
        "model_score",
        "score_gap_vs_k",
        "score_gap_vs_previous",
        "quality_penalty",
        "selection_status",
    )
    target_allocation_columns = (
        "ticker",
        "name",
        "asset_class",
        "target_weight",
        "illustrative_target_value",
        "advisor_sector",
    )
    target_reference_columns = (
        "ticker",
        "name",
        "reference_price_asof",
        "reference_price",
        "reference_target_qty",
    )
    comparison_columns = (
        "ticker",
        "name",
        "transition_status",
        "current_value",
        "current_weight",
        "target_weight",
        "illustrative_target_value",
    )
    sector_columns = (
        "advisor_sector",
        "position_count",
        "sector_value",
        "sector_weight",
        "advisor_sector_source",
    )

    valuation_unavailable = 0
    if not valuation.empty:
        valuation_columns = [
            column
            for column in ("per_ttm", "pbr", "psr_ttm", "ev_to_operating_income_ttm")
            if column in valuation.columns
        ]
        if valuation_columns:
            valuation_unavailable = int(valuation[valuation_columns].isna().any(axis=1).sum())
    top_sector = None
    top_sector_weight = None
    if not sector_target.empty and "sector_weight" in sector_target.columns:
        weights = pd.to_numeric(sector_target["sector_weight"], errors="coerce")
        if weights.notna().any():
            row = sector_target.loc[weights.idxmax()]
            top_sector = row.get("advisor_sector")
            top_sector_weight = weights.loc[weights.idxmax()]
    target_sector_weight_total = (
        _pick(sector_target.iloc[0].to_dict(), ("sector_weight_total",))
        if not sector_target.empty
        else None
    )
    if _is_missing(target_sector_weight_total) and not sector_target.empty and "sector_weight" in sector_target.columns:
        target_sector_weight_total = pd.to_numeric(
            sector_target["sector_weight"], errors="coerce"
        ).sum(min_count=1)
    sector_reconciliation = (
        _pick(sector_target.iloc[0].to_dict(), ("reconciliation_status",))
        if not sector_target.empty
        else None
    )

    css = """
    :root{--ink:#172033;--muted:#5d6878;--line:#d8dee8;--panel:#f5f7fa;--accent:#163c71;--soft:#eaf1fb;--warn:#8a4b08;--positive:#17623a}
    *{box-sizing:border-box}html{font-family:"Noto Sans KR","Malgun Gothic",Arial,sans-serif;font-size:15px;color:var(--ink);background:#eef1f5}body{margin:0}
    main{max-width:1080px;margin:24px auto;padding:42px 48px;background:#fff;box-shadow:0 8px 28px rgba(23,32,51,.10)}
    header{padding-bottom:28px;border-bottom:3px solid var(--accent)}h1{margin:0 0 9px;font-size:2rem;letter-spacing:-.035em}h2{margin:0 0 16px;font-size:1.5rem;color:var(--accent);letter-spacing:-.025em}h3{margin:22px 0 11px;font-size:1.13rem}h4{margin:16px 0 8px;font-size:.98rem}
    p{line-height:1.68}.subtitle{margin:0;color:var(--muted)}.philosophy{margin:22px 0 0;padding:16px 18px;border-left:4px solid var(--accent);background:var(--soft);line-height:1.72}
    .report-section{padding-top:34px;break-before:page;page-break-before:always}.report-section.first{break-before:auto;page-break-before:auto}.section-lead{font-size:1.03rem}.status-note{padding:11px 13px;border-radius:7px;background:var(--panel);color:var(--muted)}.status-note.positive{color:var(--positive)}
    .metric-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:13px 0 20px}.metric-card{min-height:78px;padding:12px 13px;border:1px solid var(--line);border-radius:8px;background:#fff;break-inside:avoid-page;page-break-inside:avoid}.metric-label{display:block;margin-bottom:8px;color:var(--muted);font-size:.82rem}.metric-card strong{font-size:1.02rem;overflow-wrap:anywhere}
    .table-chunk{margin:12px 0 18px;break-inside:avoid-page;page-break-inside:avoid}.table-wrap{width:100%;overflow-x:auto;border:1px solid var(--line);border-radius:8px}table{width:100%;border-collapse:collapse;background:#fff}caption{text-align:left;padding:10px 12px;background:var(--panel);font-weight:800}thead{display:table-header-group}th,td{padding:7px 8px;border-top:1px solid var(--line);text-align:right;vertical-align:top;white-space:nowrap}th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}thead th{background:#fafbfd;color:#364256;font-size:.80rem}tbody tr{break-inside:avoid-page;page-break-inside:avoid}.table-note{color:var(--muted);font-size:.84rem}
    .empty-state{display:flex;justify-content:space-between;gap:14px;margin:12px 0;padding:16px;border:1px dashed #aeb8c5;border-radius:8px;color:var(--muted)}.notice-list{margin:10px 0 20px;padding-left:22px}.notice-list li{margin:7px 0;line-height:1.56}.callout{padding:15px 17px;border:1px solid #c9d7eb;border-radius:8px;background:var(--soft);line-height:1.65}.date-contract{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin:14px 0}.date-contract>div{padding:13px;border:1px solid var(--line);border-radius:7px}.date-contract span{display:block;color:var(--muted);font-size:.82rem;margin-bottom:7px}
    .security-card{margin:18px 0 28px;padding:18px;border:1px solid var(--line);border-radius:10px;background:#fff}.security-card>h3{margin-top:0;padding-bottom:10px;border-bottom:2px solid var(--soft);break-after:avoid-page;page-break-after:avoid}.quarter-panel,.factor-panel{break-inside:avoid-page;page-break-inside:avoid}.quarter-table{table-layout:fixed}.factor-list{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:7px 16px;padding:0;list-style:none}.factor-list li{display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-bottom:1px solid var(--line)}
    h2,h3,h4,caption{break-after:avoid-page;page-break-after:avoid}p,li{orphans:3;widows:3}.avoid-break{break-inside:avoid-page;page-break-inside:avoid}
    @page{size:A4;margin:13mm 11mm 14mm}
    @media(max-width:760px){main{margin:0;padding:26px 18px;box-shadow:none}.metric-grid,.date-contract{grid-template-columns:repeat(2,minmax(0,1fr))}.factor-list{grid-template-columns:1fr}h1{font-size:1.65rem}}
    @media print{html{font-size:9.4pt;background:#fff}body{background:#fff}main{width:auto;max-width:none;margin:0;padding:0;box-shadow:none}.report-section{break-before:page;page-break-before:always}.report-section.first{break-before:auto;page-break-before:auto}.table-wrap{overflow:visible}table{table-layout:fixed}th,td{white-space:normal;overflow-wrap:anywhere;word-break:keep-all;padding:5px 5px}.table-chunk{break-inside:avoid-page;page-break-inside:avoid}.metric-grid{gap:7px}.metric-card{min-height:58px;padding:9px}.security-card{padding:12px;margin:12px 0 20px}.quarter-panel,.factor-panel,.date-contract{break-inside:avoid-page;page-break-inside:avoid}}
    """

    sections: list[str] = []
    sections.append(
        '<section id="core-conclusion" class="report-section first">'
        f"<h2>{BASE_SECTION_TITLES[0]}</h2>"
        '<p class="section-lead">이번 권고는 기존 보유 여부를 점수와 순위에 넣지 않고, 계좌 평가시점의 자산을 가상 청산한 자본으로 다음 분기 구성을 새로 제시합니다.</p>'
        + _cards(
            (
                ("개발 검증", development_status, "status"),
                ("점수 동일성", score_parity_status, "status"),
                ("재구성 기준자본", capital_value, "capital"),
                ("production 승격", promotion_status, "status"),
            )
        )
        + '<p class="callout">이 자료에는 주문 제출이나 체결 관리 기능이 없습니다. 주문·체결 기능 미사용 상태이며 실제 구성은 사용자가 판단합니다.</p>'
        + "</section>"
    )
    sections.append(
        '<section id="current-account" class="report-section">'
        f"<h2>{BASE_SECTION_TITLES[1]}</h2>"
        '<p>현재 자산의 평가금액과 비중은 증권사 snapshot의 계좌 평가일 값을 사용합니다. 모델 정보일 가격으로 현재 계좌를 역재평가하지 않습니다.</p>'
        '<div class="date-contract" data-date-basis-contract="separated">'
        f'<div><span>모델 정보 기준일</span><strong>{html.escape(_format_value("model_information_asof", model_information_asof))}</strong></div>'
        f'<div><span>계좌 평가일</span><strong>{html.escape(_format_value("account_valuation_asof", account_valuation_asof))}</strong></div>'
        f'<div><span>참고가격 기준일</span><strong>{html.escape(_format_value("reference_price_asof", reference_price_asof))}</strong></div>'
        "</div>"
        + _cards(
            (
                ("증권사 계좌 NAV", gross_nav, "nav"),
                ("주식", equity_value, "capital"),
                ("현금성 자산", cash_equivalent_value, "capital"),
                ("현금", cash_value, "capital"),
            )
        )
        + _render_table(
            current,
            table_id="current-portfolio-table",
            caption="현재 계좌 구성",
            columns=current_table_columns,
        )
        + "</section>"
    )
    sections.append(
        '<section id="prior-model-performance" class="report-section" data-performance-scope="prior-model" '
        f'data-performance-certified="{str(prior_certified).lower()}">'
        f"<h2>{BASE_SECTION_TITLES[2]}</h2>"
        f'<p class="status-note">{html.escape(prior_note)}</p>'
        + _cards(prior_cards)
        + "</section>"
    )

    if performance_certified:
        actual_body = (
            '<p>확인된 활동원장과 외부 현금흐름 조정을 기준으로 인증된 계좌 성과를 표시합니다.</p>'
            + _cards(
                (
                    ("이전 계좌 NAV", prior_nav, "nav"),
                    ("현재 계좌 NAV", current_nav, "nav"),
                    ("외부 현금흐름 조정 성과", _find(actual_context, ("account_performance_return",)), "model_price_return"),
                    ("순외부현금흐름", _find(actual_context, ("net_external_cash_flow",)), "capital"),
                )
            )
        )
    else:
        actual_body = (
            '<p class="callout">활동원장이 없어 아래 값은 <strong>endpoint NAV change</strong>이며 '
            '<strong>external-cashflow-unadjusted</strong>, <strong>not an investment return</strong>입니다. '
            '입출금과 사용자 재량의 영향이 섞일 수 있으므로 투자성과로 해석하지 않습니다.</p>'
            + _cards(
                (
                    ("이전 계좌 NAV", prior_nav, "nav"),
                    ("현재 계좌 NAV", current_nav, "nav"),
                    ("endpoint NAV change", endpoint_change, "nav"),
                    ("endpoint_nav_change_ratio_unadjusted", endpoint_ratio, "endpoint_nav_change_ratio_unadjusted"),
                )
            )
        )
    sections.append(
        '<section id="actual-account-change" class="report-section" data-performance-scope="actual-account" '
        f'data-performance-certified="{str(performance_certified).lower()}">'
        f"<h2>{account_section_title}</h2>{actual_body}</section>"
    )
    sections.append(
        '<section id="full-liquidation" class="report-section">'
        f"<h2>{BASE_SECTION_TITLES[4]}</h2>"
        '<p>계좌 평가일의 증권사 주식 평가금액에 매도수수료와 시장별 세금을 적용합니다. 다시 선발된 주식도 가상 청산비용을 부담하며 현금성 자산에는 일반 주식 거래세를 적용하지 않습니다.</p>'
        + _cards(
            (
                ("계좌 평가일", account_valuation_asof, "account_valuation_asof"),
                ("주식 순청산대금", net_equity_proceeds, "capital"),
                ("부채 차감", liability_value, "capital"),
                ("재구성 기준자본", capital_value, "capital"),
            )
        )
        + _render_table(
            liquidation_frame,
            table_id="liquidation-table",
            caption="종목별 가상 전량청산",
            columns=liquidation_columns,
        )
        + "</section>"
    )
    sections.append(
        '<section id="top-k-boundary" class="report-section">'
        f"<h2>{BASE_SECTION_TITLES[5]}</h2>"
        '<p>현재 계좌 구성은 점수·순위·Top-K 선정에 전달하지 않았습니다. 경계가 취약해도 종목을 자동 교체하거나 선발 수를 늘리지 않습니다.</p>'
        + _cards(
            (
                (f"{k}위 점수", kth_score, "model_score"),
                (f"{k + 1}위 점수", next_score, "model_score"),
                (f"{k}위−{k + 1}위 차이", boundary_gap, "model_score"),
                ("경계 안정성", boundary_status, "status"),
            )
        )
        + _render_table(
            top,
            table_id="top-k-table",
            caption="신규 Top-K",
            columns=top_columns,
            max_rows_per_chunk=5,
            max_total_rows=k,
        )
        + _render_table(
            boundary,
            table_id="boundary-watchlist-table",
            caption=f"선정 경계 watchlist ({max(1, k - 2)}위~{k + 5}위)",
            columns=boundary_columns,
            max_rows_per_chunk=8,
            max_total_rows=8,
        )
        + "</section>"
    )
    sections.append(
        '<section id="target-portfolio" class="report-section">'
        f"<h2>{BASE_SECTION_TITLES[6]}</h2>"
        '<p>현금성 자산 전략 버킷과 현재 구현수단 437350을 구분합니다. 역사 검증의 현금성 자산은 명목수익률 0% 보수적 가정을 사용하며, 437350의 과거 이력 부재는 모델 승격 차단사유가 아닙니다.</p>'
        + _cards(
            (
                ("주식 목표비중", target_equity_weight, "target_weight"),
                ("현금성 자산 목표비중", target_cash_weight, "target_weight"),
                ("모델 정보 기준일", model_information_asof, "model_information_asof"),
                ("참고가격 기준일", reference_price_asof, "reference_price_asof"),
            )
        )
        + _render_table(
            target,
            table_id="target-allocation-table",
            caption="목표 비중과 목표금액",
            columns=target_allocation_columns,
            max_rows_per_chunk=6,
        )
        + _render_table(
            target,
            table_id="target-reference-table",
            caption="참고가격과 참고수량",
            columns=target_reference_columns,
            max_rows_per_chunk=6,
        )
        + '<p class="table-note">참고수량은 주문수량이 아닙니다. 공통 참고가격 기준일에 공식가격이 없으면 수량만 자료 없음으로 두고 목표비중과 목표금액은 유지합니다.</p>'
        + "</section>"
    )
    sections.append(
        '<section id="current-vs-target" class="report-section">'
        f"<h2>{BASE_SECTION_TITLES[7]}</h2>"
        '<p>구성 변화 명칭은 설명용이며 현재 보유 여부가 목표비중이나 모델 점수를 바꾸지 않습니다.</p>'
        + _render_table(
            comparison,
            table_id="current-vs-target-table",
            caption="현재 구성과 목표 구성",
            columns=comparison_columns,
            max_rows_per_chunk=7,
        )
        + "</section>"
    )
    sections.append(
        '<section id="selected-details" class="report-section">'
        f"<h2>{BASE_SECTION_TITLES[8]}</h2>"
        '<p>밸류에이션은 미래 공시나 외부 provider 배수를 사용하지 않고 내부 재무자료와 명시된 가격 기준일로 계산합니다. 분모가 비양수이거나 EV 구성요소가 불완전하면 값을 비워 둡니다.</p>'
        + _security_detail_cards(top, details, valuation)
        + "</section>"
    )
    sections.append(
        '<section id="portfolio-risks" class="report-section">'
        f"<h2>{BASE_SECTION_TITLES[9]}</h2>"
        '<p>공식 업종은 원본 분류를 보존하고, 포트폴리오 집중도는 별도 투자위험 섹터로 계산합니다. 종목명 유사도에 의한 업종 연결은 사용하지 않습니다.</p>'
        + _cards(
            (
                ("목표 최대 섹터", top_sector, "status"),
                ("목표 최대 섹터 비중", top_sector_weight, "sector_weight"),
                ("목표 주식 섹터 비중 합계", target_sector_weight_total, "sector_weight"),
                ("섹터 비중 합계 점검", sector_reconciliation, "status"),
                ("밸류에이션 일부 누락 종목", valuation_unavailable, "position_count"),
                ("섹터 비중 기준", "주식 목표비중", "status"),
            )
        )
        + _render_table(
            sector_current,
            table_id="sector-current-table",
            caption="현재 계좌 섹터 노출",
            columns=sector_columns,
        )
        + _render_table(
            sector_target,
            table_id="sector-target-table",
            caption="목표 포트폴리오 섹터 노출",
            columns=sector_columns,
        )
        + "</section>"
    )
    sections.append(
        '<section id="limitations" class="report-section" data-backtest-numerics-published="false">'
        f"<h2>{BASE_SECTION_TITLES[10]}</h2>"
        + _cards(
            (
                ("역사 검증 상태", validation_status, "status"),
                ("데이터 범위", coverage_status, "status"),
                ("production 승격", promotion_status, "status"),
                ("현재 권고서", "생성 가능", "status"),
            )
        )
        + '<h3>production 승격의 주요 차단사유</h3>'
        + _blocker_list(model_blockers, empty_text="확인된 모델 승격 차단사유가 없습니다.")
        + '<h3>성과 인증 관련 주의사항</h3>'
        + _blocker_list(performance_warnings, empty_text="추가 성과 인증 주의사항이 없습니다.")
        + '<h3>계좌 평가 관련 참고사항</h3>'
        + _blocker_list(account_warnings, empty_text="추가 계좌 평가 참고사항이 없습니다.")
        + '<ul class="notice-list">'
        '<li>역사 검증이 완료되지 않은 성과·회전·비용 통계는 사용자 본문에서 제외하고 private technical audit에만 보존합니다.</li>'
        '<li>활동원장 부재는 신규 권고 생성을 막지 않지만 실제 계좌의 투자성과 인증을 허용하지 않습니다.</li>'
        '<li>현금성 자산의 실제 조정과 각 종목의 실제 매매 여부는 사용자가 결정합니다.</li>'
        '<li>본 correction run은 production promotion이 아니며 기존 production latest와 immutable run을 변경하지 않습니다.</li>'
        "</ul></section>"
    )

    return (
        '<!doctype html><html lang="ko"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<title>분기별 투자판단 보조 보고서 V2</title>'
        f"<style>{css}</style></head><body>"
        f'<main data-report-contract="{REPORT_CONTRACT}" data-report-mode="{ADVISOR_MODE}">'
        '<header><h1>분기별 투자판단 보조 보고서</h1>'
        '<p class="subtitle">계좌 평가일 전량청산 가정에 따른 다음 분기 fresh-start 권고</p>'
        f'<blockquote class="philosophy">{html.escape(PHILOSOPHY_TEXT)}</blockquote></header>'
        + "".join(sections)
        + "</main></body></html>"
    )


class _ReportParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.lang = ""
        self.viewport = False
        self.contract = ""
        self.mode = ""
        self.h2: list[str] = []
        self.text: list[str] = []
        self.styles: list[str] = []
        self._h2_parts: list[str] | None = None
        self._inside_style = False
        self.scripts = 0
        self.external_resources = 0
        self.performance_certifications: dict[str, str] = {}
        self.table_ids: set[str] = set()
        self.table_column_counts: list[int] = []
        self.continuation_row_counts: list[int] = []
        self.date_basis_contract = ""
        self.backtest_numerics_published = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "html":
            self.lang = attributes.get("lang", "")
        elif tag == "meta" and attributes.get("name", "").lower() == "viewport":
            self.viewport = True
        elif tag == "main":
            self.contract = attributes.get("data-report-contract", "")
            self.mode = attributes.get("data-report-mode", "")
        elif tag == "h2":
            self._h2_parts = []
        elif tag == "style":
            self._inside_style = True
        elif tag == "script":
            self.scripts += 1
            if attributes.get("src"):
                self.external_resources += 1
        elif tag in {"img", "iframe", "video", "audio", "source", "link"}:
            if attributes.get("src") or attributes.get("href"):
                self.external_resources += 1
        if tag == "section" and attributes.get("data-performance-scope"):
            self.performance_certifications[attributes["data-performance-scope"]] = attributes.get(
                "data-performance-certified", ""
            )
        if attributes.get("data-date-basis-contract"):
            self.date_basis_contract = attributes["data-date-basis-contract"]
        if attributes.get("data-backtest-numerics-published"):
            self.backtest_numerics_published = attributes["data-backtest-numerics-published"]
        if tag == "table":
            if attributes.get("id"):
                self.table_ids.add(attributes["id"])
            if attributes.get("data-column-count", "").isdigit():
                self.table_column_counts.append(int(attributes["data-column-count"]))
        if tag == "div" and attributes.get("data-continuation") == "true":
            row_count = attributes.get("data-row-count", "")
            if row_count.isdigit():
                self.continuation_row_counts.append(int(row_count))

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self._h2_parts is not None:
            self.h2.append("".join(self._h2_parts).strip())
            self._h2_parts = None
        elif tag == "style":
            self._inside_style = False

    def handle_data(self, data: str) -> None:
        if self._inside_style:
            self.styles.append(data)
        else:
            self.text.append(data)
        if self._h2_parts is not None:
            self._h2_parts.append(data)


def validate_advisor_v2_html(html_text: str) -> dict[str, Any]:
    """Validate privacy, user-facing terminology and print-layout contracts."""

    if not isinstance(html_text, str):
        raise TypeError("html_text는 문자열이어야 합니다")
    parser = _ReportParser()
    parser.feed(html_text)
    parser.close()
    visible = " ".join(" ".join(parser.text).split())
    css = "\n".join(parser.styles).replace(" ", "").lower()
    actual_certified = parser.performance_certifications.get("actual-account") == "true"
    expected_sections = list(BASE_SECTION_TITLES)
    if not actual_certified:
        expected_sections[3] = UNVERIFIED_ACCOUNT_SECTION_TITLE

    visible_internal_enums = sorted(set(_INTERNAL_ENUM_RE.findall(visible)))
    allowed_visible_enum = {"CFO"}
    visible_internal_enums = [item for item in visible_internal_enums if item not in allowed_visible_enum]
    unverified_actual_contract = (
        actual_certified
        or (
            UNVERIFIED_ACCOUNT_SECTION_TITLE in parser.h2
            and "endpoint NAV change" in visible
            and "external-cashflow-unadjusted" in visible
            and "not an investment return" in visible
            and "endpoint_nav_change_ratio_unadjusted" in visible
            and "실제 수익률" not in visible
            and "성과수익률" not in visible
            and "account_performance_return" not in visible
        )
    )
    provisional_body = not actual_certified or parser.performance_certifications.get("prior-model") != "true"
    provisional_metrics_absent = not provisional_body or not _UNVALIDATED_METRIC_RE.search(visible)
    continuation_rows_balanced = all(count >= 3 for count in parser.continuation_row_counts)

    checks: dict[str, bool] = {
        "document_language_ko": parser.lang == "ko",
        "viewport_present": parser.viewport,
        "v2_contract": parser.contract == REPORT_CONTRACT and parser.mode == ADVISOR_MODE,
        "exact_eleven_sections_in_order": parser.h2 == expected_sections,
        "philosophy_present": PHILOSOPHY_TEXT in visible,
        "date_bases_separated_and_visible": (
            parser.date_basis_contract == "separated"
            and "모델 정보 기준일" in visible
            and "계좌 평가일" in visible
            and "참고가격 기준일" in visible
        ),
        "unverified_account_endpoint_contract": unverified_actual_contract,
        "provisional_statistics_absent": provisional_metrics_absent,
        "backtest_body_limited_to_status": (
            parser.backtest_numerics_published == "false"
            and "역사 검증 상태" in visible
            and "데이터 범위" in visible
            and "production 승격" in visible
            and "production 승격의 주요 차단사유" in visible
        ),
        "boundary_summary_and_watchlist_present": (
            "10위 점수" in visible
            and "11위 점수" in visible
            and "10위−11위 차이" in visible
            and "경계 안정성" in visible
            and "boundary-watchlist-table" in parser.table_ids
        ),
        "selected_quantitative_details_present": (
            "최근 3개 분기" in visible
            and "TTM 매출" in visible
            and "TTM 영업이익" in visible
            and "지배주주순이익 TTM" in visible
            and "CFO TTM" in visible
            and "CFO/영업이익" in visible
            and "PER(TTM)" in visible
            and "PBR" in visible
            and "PSR(TTM)" in visible
            and "EV/영업이익(TTM)" in visible
            and "품질 패널티" in visible
            and "팩터" in visible
        ),
        "cash_bucket_and_vehicle_separated": (
            "현금성 자산 전략 버킷과 현재 구현수단 437350을 구분합니다" in visible
            and "명목수익률 0% 보수적 가정" in visible
            and "과거 이력 부재는 모델 승격 차단사유가 아닙니다" in visible
        ),
        "user_facing_names_translated": (
            "주문·체결 기능 미사용" in visible
            and not visible_internal_enums
            and "CASH_EQUIVALENT_BUCKET" not in visible
            and "NEW_SELECTION" not in visible
            and "RESELECTED" not in visible
            and "DROPPED" not in visible
        ),
        "no_scripts_or_external_assets": (
            parser.scripts == 0
            and parser.external_resources == 0
            and not _URL_RE.search(html_text)
        ),
        "privacy_and_paths_absent": (
            not _ABSOLUTE_PATH_RE.search(html_text)
            and not _SPREADSHEET_RE.search(html_text)
            and not _HASH_RE.search(html_text)
            and not _ACCOUNT_LABEL_RE.search(html_text)
            and not _SENSITIVE_KEY_RE.search(visible)
        ),
        "forbidden_quantity_names_absent": not _FORBIDDEN_QTY_RE.search(html_text),
        "print_css_avoids_clipping": (
            "@page{size:a4" in css
            and "@mediaprint" in css
            and "table{table-layout:fixed}" in css
            and "overflow-wrap:anywhere" in css
            and all(count <= 8 for count in parser.table_column_counts)
        ),
        "print_css_avoids_orphan_rows": (
            "orphans:3" in css
            and "widows:3" in css
            and "break-inside:avoid-page" in css
            and continuation_rows_balanced
        ),
    }
    failed = [name for name, passed in checks.items() if not passed]
    return {
        "schema_version": 2,
        "report_contract": REPORT_CONTRACT,
        "mode": parser.mode,
        "pass": not failed,
        "status": "PASS" if not failed else "FAIL",
        "checks": checks,
        "failed_checks": failed,
        "h2_sections": parser.h2,
        "visible_internal_enums": visible_internal_enums,
        "continuation_row_counts": parser.continuation_row_counts,
        "maximum_table_column_count": max(parser.table_column_counts, default=0),
    }


__all__ = [
    "ADVISOR_MODE",
    "BASE_SECTION_TITLES",
    "PHILOSOPHY_TEXT",
    "REPORT_CONTRACT",
    "UNVERIFIED_ACCOUNT_SECTION_TITLE",
    "generate_advisor_v2_html",
    "validate_advisor_v2_html",
]
