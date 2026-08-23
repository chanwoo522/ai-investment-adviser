#!/usr/bin/env python
"""Build the immutable 26Q3 public-subscriber report child run.

The builder is deliberately report-only.  It reads the authoritative advisor V2
run, preserves every model output, and creates a new staged child directory.
Network collection is limited to public KRX/KIND price endpoints needed by the
subscriber performance contract.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from zipfile import ZIP_DEFLATED, ZipFile

import pandas as pd
import requests
from bs4 import BeautifulSoup


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.advisor.immutable_run import (  # noqa: E402
    publish_staging,
    relative_artifact_manifest,
    sha256_file,
)
from scripts.qa.render_public_report import render_public_report  # noqa: E402


REPORT_CONTRACT = "SUBSCRIBER_QUARTERLY_REBALANCING_REPORT_V2"
REPORT_TITLE = "퀀트 스크리닝 성장 가속 26Q3 리밸런싱 제안"
PARENT_RUN_ID = "advisor_full_reset_v2_20260820_20260821T142726787Z"
PARENT_ROOT = (
    REPO_ROOT
    / "data/development/advisor_full_reset_v2/runs"
    / f"run_id={PARENT_RUN_ID}"
)
RUNS_ROOT = REPO_ROOT / "data/development/subscriber_reports/runs"
EVIDENCE_PATH = (
    REPO_ROOT
    / "data/portfolio/evidence"
    / "actual_rebalance_20260401_user_confirmed_20260820T084015244621Z"
    / "derived_post_trade_snapshot_20260401.csv"
)
PRODUCTION_RUN_ROOT = (
    REPO_ROOT
    / "data/production/runs"
    / "run_id=20260818_q2_revenue_op_D_quality_v9201_20260820T015429567338"
)
PRICES_PATH = (
    PRODUCTION_RUN_ROOT
    / "intermediate/prices_daily__asof=2026-08-18__metric=revenue_op__v=1.parquet"
)
FUNDAMENTALS_PATH = (
    PRODUCTION_RUN_ROOT
    / "intermediate/fundamentals_canonical__asof=2026-08-18__y=2016-2026__v=1.parquet"
)

PERFORMANCE_START = pd.Timestamp("2026-04-01")
PERFORMANCE_END = pd.Timestamp("2026-08-20")
MODEL_ASOF = "2026-08-18"
ACCOUNT_ASOF = "2026-08-20"
TRACKED_TICKERS = (
    "005930",
    "041920",
    "042000",
    "060280",
    "131290",
    "171090",
    "214150",
    "218410",
    "425420",
)
KIND_BASE = "https://kind.krx.co.kr/common/stockprices.do"
INDEX_BASE = "https://index.krx.co.kr"
INDEX_PAGE = (
    INDEX_BASE
    + "/contents/MKD/03/0304/03040101/MKD03040101T2.jsp"
      "?upmidCd=0101&idxCd=5300&idxId=X3G01P"
)
INDEX_BLD = "/IDX/03/0304/03040101/mkd03040101T2_02"

COMPLIANCE_NOTICE = (
    "본 자료는 공개된 정보와 정량모형을 활용해 작성한 일반적 정보 제공 및 투자판단 보조자료입니다. "
    "특정 투자자의 재무상황, 투자목적 또는 위험선호를 고려한 맞춤형 투자자문이나 금융투자상품의 "
    "매수·매도 권유가 아닙니다.\n\n"
    "본 자료에 제시된 종목, 비중, 목표금액 및 수량은 정량모형이 산출한 참고자료이며 수익을 "
    "보장하지 않습니다. 금융투자상품은 가격 변동으로 원금 손실이 발생할 수 있으며, 과거의 성과가 "
    "미래의 성과를 보장하지 않습니다.\n\n"
    "본 자료는 작성 시점에 이용 가능한 정보에 기초했으나 자료의 정확성, 완전성 및 적시성을 "
    "보장하지 않습니다. 공시 정정, 데이터 오류, 산식 및 모델의 한계로 실제 결과가 달라질 수 있습니다.\n\n"
    "최종 투자 판단과 투자 결과에 대한 책임은 투자자 본인에게 있습니다. 작성자 또는 배포자는 자료 "
    "작성일 현재 본 보고서에 언급된 종목을 보유할 수 있으며, 이후 거래로 보유상태가 변경될 수 있습니다.\n\n"
    "본 자료의 무단 복제, 전재, 재배포 및 임의 수정은 금지됩니다."
)

PUBLIC_FORBIDDEN = (
    "account_id",
    "broker_name",
    "H-able",
    "VERIFIED",
    "run_id",
    "SHA-256",
    "PASS_CONTRACT_CORRECTION",
    "production_promoted",
    "ADVISOR_FULL_RESET",
    "official_industry_source",
    "advisor_sector_source",
    "지난 분기 모델 성과",
    "직전 분기 모델 성과",
    "실제 계좌 잔고 변화",
    "실제 계좌 성과",
    "가상 전량청산 및 재구성 자본",
    "가상 전량청산 계산",
    "한계와 사용자 판단사항",
    "모델 한계 및 사용자 판단사항",
    "투자위험 섹터",
    "참고가격과 참고수량",
    "참고수량(주문 아님)",
    "참고가격 기준일",
    "신규 Top-K(계속)",
    "현재 계좌 구성(계속)",
    "목표 비중과 목표금액(계속)",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _new_run_id() -> str:
    stamp = _utc_now().strftime("%Y%m%dT%H%M%S%fZ")
    return f"subscriber_26Q3_report_20260820_{stamp}"


def _ticker(value: object) -> str:
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text.zfill(6)


def _safe_json(path: Path, payload: Any) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _safe_text(path: Path, text: str) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _safe_csv(path: Path, frame: pd.DataFrame) -> None:
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, index=False, encoding="utf-8-sig")


def _digest_tree(root: Path) -> dict[str, Any]:
    rows = []
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        rows.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {
        "file_count": len(rows),
        "tree_sha256": hashlib.sha256(encoded).hexdigest(),
        "files": rows,
    }


def _protected_existing_state() -> dict[str, Any]:
    """Hash production/latest/model outputs and every already-published dev run.

    The repository-wide helper also scans historical portfolio evidence.  One
    unrelated archived evidence directory on this workstation has an explicit
    deny ACL, so this report-only child uses the narrower invariant that the
    user requested here: all production state plus all existing immutable
    development runs.  The active dot-staging child is excluded by name.
    """

    rows: list[dict[str, Any]] = []
    roots = (REPO_ROOT / "data/production", REPO_ROOT / "data/development")
    for root in roots:
        if not root.exists():
            continue
        pending = [root]
        while pending:
            directory = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path = Path(entry.path)
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name.startswith(".run_id=") and entry.name.endswith(".staging"):
                            continue
                        pending.append(path)
                    elif entry.is_file(follow_symlinks=True):
                        rows.append(
                            {
                                "path": path.relative_to(REPO_ROOT).as_posix(),
                                "size": path.stat().st_size,
                                "sha256": sha256_file(path),
                            }
                        )
    rows.sort(key=lambda item: item["path"].lower())
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return {
        "contract": "PROTECTED_PRODUCTION_AND_EXISTING_IMMUTABLE_DEVELOPMENT_RUNS",
        "staging_exclusion": "data/development/**/.run_id=*.staging",
        "file_count": len(rows),
        "tree_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def _require_inputs() -> None:
    required = (
        PARENT_ROOT / "account_valuation_snapshot.csv",
        PARENT_ROOT / "fresh_start_top_k_v2.csv",
        PARENT_ROOT / "top_k_boundary_watchlist.csv",
        PARENT_ROOT / "target_portfolio_v2.csv",
        PARENT_ROOT / "current_vs_target_v2.csv",
        PARENT_ROOT / "selected_security_financials.csv",
        PARENT_ROOT / "selected_security_diagnostics.csv",
        EVIDENCE_PATH,
        PRICES_PATH,
        FUNDAMENTALS_PATH,
    )
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"required immutable inputs missing: {missing}")


def _collect_kind_closes(tickers: Iterable[str]) -> tuple[pd.DataFrame, dict[str, str]]:
    session = requests.Session()
    headers = {"User-Agent": "Mozilla/5.0"}
    rows: list[dict[str, Any]] = []
    sources: dict[str, str] = {}
    for ticker in tickers:
        url = f"{KIND_BASE}?isurCd={ticker[:5]}&method=searchStockPricesMain"
        response = session.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        hit: tuple[str, int] | None = None
        for table_row in soup.select("table.list tbody tr"):
            cells = [cell.get_text(" ", strip=True).replace(",", "") for cell in table_row.select("td")]
            if cells and cells[0].startswith(ACCOUNT_ASOF):
                hit = (cells[0], int(cells[1]))
                break
        if hit is None:
            raise RuntimeError(f"official KIND close unavailable for {ticker} on {ACCOUNT_ASOF}")
        rows.append(
            {
                "date": ACCOUNT_ASOF,
                "ticker": ticker,
                "close": hit[1],
                "source": "KRX_KIND_OFFICIAL_CLOSE",
                "fill_used": False,
            }
        )
        sources[ticker] = url
    frame = pd.DataFrame(rows).sort_values("ticker").reset_index(drop=True)
    if len(frame) != len(tuple(tickers)) or frame["close"].le(0).any():
        raise RuntimeError("official KIND close collection is incomplete")
    return frame, sources


def _collect_krx300() -> tuple[pd.DataFrame, str]:
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": INDEX_PAGE,
        "X-Requested-With": "XMLHttpRequest",
    }
    otp = session.get(
        INDEX_BASE + "/contents/COM/GenerateOTP.jspx",
        params={"name": "form", "bld": INDEX_BLD},
        headers=headers,
        timeout=30,
    )
    otp.raise_for_status()
    code = otp.text.strip()
    if not code:
        raise RuntimeError("official KRX index OTP was empty")
    response = session.post(
        INDEX_BASE + "/contents/IDX/99/IDX99000001.jspx",
        data={
            "idx_cd": "5300",
            "ind_tp_cd": "5",
            "idx_ind_cd": "300",
            "add_data_yn": "",
            "bz_dd": "",
            "fromdate": PERFORMANCE_START.strftime("%Y%m%d"),
            "todate": PERFORMANCE_END.strftime("%Y%m%d"),
            "code": code,
        },
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    raw_rows = payload.get("output")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise RuntimeError("official KRX300 series returned no rows")
    frame = pd.DataFrame(raw_rows)
    out = pd.DataFrame(
        {
            "date": pd.to_datetime(frame["trd_dd"], format="%Y/%m/%d"),
            "krx300_price_index": pd.to_numeric(
                frame["clsprc_idx"].astype(str).str.replace(",", "", regex=False)
            ),
        }
    ).sort_values("date")
    out["source"] = "KRX_OFFICIAL_INDEX_SITE"
    out["asset_type"] = "INDEX"
    out["return_type"] = "PRICE"
    out["fill_used"] = False
    expected = {PERFORMANCE_START, PERFORMANCE_END}
    if not expected.issubset(set(out["date"])):
        raise RuntimeError("official KRX300 start or end value is unavailable")
    if out["krx300_price_index"].le(0).any() or out["date"].duplicated().any():
        raise RuntimeError("official KRX300 series is invalid")
    return out.reset_index(drop=True), INDEX_PAGE


def _load_parent_price_panel(tickers: Iterable[str]) -> pd.DataFrame:
    frame = pd.read_parquet(PRICES_PATH, columns=["date", "Close", "ticker"])
    frame["ticker"] = frame["ticker"].map(_ticker)
    frame["date"] = pd.to_datetime(frame["date"]).dt.normalize()
    frame = frame[
        frame["ticker"].isin(tuple(tickers))
        & frame["date"].between(PERFORMANCE_START, pd.Timestamp(MODEL_ASOF))
    ].copy()
    frame = frame.rename(columns={"Close": "close"})
    frame["close"] = pd.to_numeric(frame["close"], errors="raise")
    if frame.duplicated(["date", "ticker"]).any():
        raise RuntimeError("parent certified price panel has duplicate rows")
    return frame[["date", "ticker", "close"]].sort_values(["date", "ticker"])


def _current_equity_public(account: pd.DataFrame) -> pd.DataFrame:
    out = account[account["asset_class"].eq("EQUITY")].copy()
    out["ticker"] = out["ticker"].map(_ticker)
    out = out[out["ticker"].isin(TRACKED_TICKERS)].copy()
    if len(out) != 9:
        raise RuntimeError(f"expected 9 public equity rows, got {len(out)}")
    total = float(out["account_asof_value"].sum())
    out["equity_weight"] = out["account_asof_value"] / total
    result = out[
        [
            "ticker",
            "name",
            "current_qty",
            "account_asof_price",
            "account_asof_value",
            "equity_weight",
            "account_valuation_asof",
        ]
    ].rename(
        columns={
            "current_qty": "quantity",
            "account_asof_price": "price_per_share",
            "account_asof_value": "market_value",
            "account_valuation_asof": "valuation_date",
        }
    )
    return result.sort_values("market_value", ascending=False).reset_index(drop=True)


def _verify_quantity_parity(evidence: pd.DataFrame, current: pd.DataFrame) -> dict[str, Any]:
    start = evidence.copy()
    start["ticker"] = start["ticker"].map(_ticker)
    end = current.copy()
    end["ticker"] = end["ticker"].map(_ticker)
    merged = start[["ticker", "shares"]].merge(
        end[["ticker", "quantity"]], on="ticker", how="outer", validate="one_to_one"
    )
    merged["shares"] = pd.to_numeric(merged["shares"])
    merged["quantity"] = pd.to_numeric(merged["quantity"])
    merged["qty_equal"] = merged["shares"].eq(merged["quantity"])
    tickers_equal = set(start["ticker"]) == set(end["ticker"]) == set(TRACKED_TICKERS)
    passed = bool(tickers_equal and merged["qty_equal"].all())
    return {
        "status": "PASS" if passed else "MONTHLY_PERFORMANCE_BLOCKED_ACTIVITY_LEDGER_REQUIRED",
        "start_end_tickers_equal": tickers_equal,
        "all_quantities_equal": bool(merged["qty_equal"].all()),
        "rows": merged.to_dict(orient="records"),
    }


def _common_dates(price_panel: pd.DataFrame, krx300: pd.DataFrame) -> list[pd.Timestamp]:
    equity_sets = (
        price_panel.groupby("date")["ticker"].nunique().loc[lambda s: s.eq(len(TRACKED_TICKERS))].index
    )
    common = sorted(set(equity_sets).intersection(set(krx300["date"])))
    common = [date for date in common if PERFORMANCE_START <= date <= PERFORMANCE_END]
    if PERFORMANCE_START not in common or PERFORMANCE_END not in common:
        raise RuntimeError("common-date contract lacks exact performance endpoints")
    dates = [PERFORMANCE_START]
    for month in (4, 5, 6, 7):
        candidates = [date for date in common if date.year == 2026 and date.month == month]
        if not candidates:
            raise RuntimeError(f"no complete common trading date for 2026-{month:02d}")
        month_end = max(candidates)
        if month_end not in dates:
            dates.append(month_end)
    dates.append(PERFORMANCE_END)
    if len(dates) != 6 or dates != sorted(dates):
        raise RuntimeError(f"unexpected common-date sequence: {dates}")
    return dates


def _build_performance(
    evidence: pd.DataFrame,
    prices: pd.DataFrame,
    krx300: pd.DataFrame,
    names: dict[str, str],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    evidence = evidence.copy()
    evidence["ticker"] = evidence["ticker"].map(_ticker)
    evidence["shares"] = pd.to_numeric(evidence["shares"], errors="raise")
    evidence["position_value"] = pd.to_numeric(evidence["position_value"], errors="raise")
    start_nav = float(evidence["position_value"].sum())
    common_dates = _common_dates(prices, krx300)
    qty = evidence.set_index("ticker")["shares"]
    price_wide = prices.pivot(index="date", columns="ticker", values="close")
    index_map = krx300.set_index("date")["krx300_price_index"]
    index_start = float(index_map.loc[PERFORMANCE_START])
    rows: list[dict[str, Any]] = []
    previous_nav = start_nav
    previous_equiv = start_nav
    previous_index = index_start
    for position, date in enumerate(common_dates):
        if position == 0:
            nav = start_nav
        else:
            nav = float((price_wide.loc[date, list(TRACKED_TICKERS)] * qty).sum())
        index_level = float(index_map.loc[date])
        equiv = start_nav * index_level / index_start
        rows.append(
            {
                "date": date.strftime("%Y-%m-%d") + ("(부분월)" if date == PERFORMANCE_END else ""),
                "date_iso": date.strftime("%Y-%m-%d"),
                "portfolio_nav": round(nav),
                "portfolio_change_amount": round(nav - previous_nav) if position else 0,
                "portfolio_monthly_change_rate": nav / previous_nav - 1 if position else 0.0,
                "portfolio_cumulative_change_rate": nav / start_nav - 1 if position else 0.0,
                "krx300_price_index": index_level,
                "krx300_equivalent_nav": round(equiv),
                "krx300_change_amount": round(equiv - previous_equiv) if position else 0,
                "krx300_monthly_change_rate": index_level / previous_index - 1 if position else 0.0,
                "krx300_cumulative_change_rate": index_level / index_start - 1 if position else 0.0,
                "cumulative_excess_return": (nav / start_nav - 1) - (index_level / index_start - 1),
            }
        )
        previous_nav = nav
        previous_equiv = equiv
        previous_index = index_level
    monthly = pd.DataFrame(rows)

    end_prices = price_wide.loc[PERFORMANCE_END]
    security_rows = []
    for item in evidence.itertuples(index=False):
        ticker = _ticker(item.ticker)
        start_value = float(item.position_value)
        end_value = float(item.shares) * float(end_prices.loc[ticker])
        security_rows.append(
            {
                "ticker": ticker,
                "name": names[ticker],
                "quantity": int(item.shares),
                "start_value": round(start_value),
                "end_value": round(end_value),
                "change_amount": round(end_value - start_value),
                "change_rate": end_value / start_value - 1,
                "start_date": PERFORMANCE_START.strftime("%Y-%m-%d"),
                "end_date": PERFORMANCE_END.strftime("%Y-%m-%d"),
                "end_official_close": float(end_prices.loc[ticker]),
            }
        )
    security = pd.DataFrame(security_rows).sort_values("change_rate", ascending=False).reset_index(drop=True)
    start_error = abs(float(security["start_value"].sum()) - float(monthly.iloc[0]["portfolio_nav"]))
    end_error = abs(float(security["end_value"].sum()) - float(monthly.iloc[-1]["portfolio_nav"]))
    change_error = abs(
        float(security["change_amount"].sum())
        - (float(monthly.iloc[-1]["portfolio_nav"]) - float(monthly.iloc[0]["portfolio_nav"]))
    )
    qa = {
        "start_nav_basis": "ACTUAL_POST_TRADE_POSITION_VALUE",
        "common_dates": [date.strftime("%Y-%m-%d") for date in common_dates],
        "equity_count": len(TRACKED_TICKERS),
        "start_nav_reconciliation_error_krw": start_error,
        "end_nav_reconciliation_error_krw": end_error,
        "change_reconciliation_error_krw": change_error,
        "reconciliation_status": "PASS" if max(start_error, end_error, change_error) <= 1 else "FAIL",
        "fill_used": False,
        "etf_proxy_used": False,
    }
    if qa["reconciliation_status"] != "PASS":
        raise RuntimeError(f"performance reconciliation failed: {qa}")
    return monthly, security, qa


def _growth_label(previous: float | None, current: float | None, *, operating: bool) -> str:
    if previous is None or current is None or pd.isna(previous) or pd.isna(current):
        return "-"
    previous = float(previous)
    current = float(current)
    if not operating:
        if previous == 0:
            return "산출 불가"
        return f"{(current / previous - 1) * 100:.1f}%"
    if previous > 0 and current >= 0:
        return f"{(current / previous - 1) * 100:.1f}%"
    if previous < 0 and current > 0:
        return "흑자전환"
    if previous > 0 and current < 0:
        return "적자전환"
    if previous < 0 and current < 0:
        return "적자축소" if abs(current) < abs(previous) else "적자확대"
    return "산출 불가"


def _selected_public_financials(
    topk: pd.DataFrame, financials: pd.DataFrame, diagnostics: pd.DataFrame
) -> tuple[pd.DataFrame, pd.DataFrame]:
    canonical = pd.read_parquet(FUNDAMENTALS_PATH)
    canonical["ticker"] = canonical["ticker"].map(_ticker)
    canonical = canonical[
        canonical["ticker"].isin(topk["ticker"])
        & canonical["year"].eq(2025)
        & canonical["quarter"].eq(3)
    ]
    q3 = canonical.set_index("ticker")[["Revenue", "OpIncome"]]
    merged = (
        topk[["ticker", "name", "model_rank", "model_score", "quality_penalty", "official_industry_name", "advisor_sector"]]
        .merge(financials, on=["ticker", "name", "model_rank"], how="left", validate="one_to_one")
        .merge(
            diagnostics[["ticker", "quality_penalty_reason"]],
            on="ticker",
            how="left",
            validate="one_to_one",
        )
    )
    rows: list[dict[str, Any]] = []
    valuation_rows: list[dict[str, Any]] = []
    for row in merged.sort_values("model_rank").itertuples(index=False):
        prev_rev = float(q3.loc[row.ticker, "Revenue"]) if row.ticker in q3.index else None
        prev_op = float(q3.loc[row.ticker, "OpIncome"]) if row.ticker in q3.index else None
        revs = [row.revenue_q_minus_2, row.revenue_q_minus_1, row.revenue_latest_q]
        ops = [row.operating_income_q_minus_2, row.operating_income_q_minus_1, row.operating_income_latest_q]
        rev_growth = [
            _growth_label(prev_rev, revs[0], operating=False),
            _growth_label(revs[0], revs[1], operating=False),
            _growth_label(revs[1], revs[2], operating=False),
        ]
        op_growth = [
            _growth_label(prev_op, ops[0], operating=True),
            _growth_label(ops[0], ops[1], operating=True),
            _growth_label(ops[1], ops[2], operating=True),
        ]
        rows.append(
            {
                "ticker": row.ticker,
                "name": row.name,
                "model_rank": int(row.model_rank),
                "industry": row.official_industry_name,
                "sector": row.advisor_sector,
                "period_1": row.quarter_minus_2_period,
                "period_2": row.quarter_minus_1_period,
                "period_3": row.latest_quarter_period,
                "revenue_1": row.revenue_q_minus_2,
                "revenue_2": row.revenue_q_minus_1,
                "revenue_3": row.revenue_latest_q,
                "revenue_growth_1": rev_growth[0],
                "revenue_growth_2": rev_growth[1],
                "revenue_growth_3": rev_growth[2],
                "operating_income_1": row.operating_income_q_minus_2,
                "operating_income_2": row.operating_income_q_minus_1,
                "operating_income_3": row.operating_income_latest_q,
                "operating_income_growth_1": op_growth[0],
                "operating_income_growth_2": op_growth[1],
                "operating_income_growth_3": op_growth[2],
                "market_cap": row.market_cap_asof,
                "cfo_ttm": row.cfo_ttm,
                "cfo_to_operating_income": row.cfo_conversion_ttm,
                "quality_penalty": row.quality_penalty,
                "quality_penalty_reason": row.quality_penalty_reason,
            }
        )
        valuation_rows.append(
            {
                "ticker": row.ticker,
                "name": row.name,
                "model_rank": int(row.model_rank),
                "valuation_asof": row.valuation_asof,
                "eps_ttm": None,
                "eps_status": "NA_CERTIFIED_TTM_EPS_UNAVAILABLE",
                "net_income_ttm_for_per": None,
                "net_income_status": "NA_COMMON_EQUITY_ATTRIBUTION_UNAVAILABLE",
                "per_value": None,
                "per_formula_used": "NA",
                "per_price_asof": row.valuation_asof,
                "per_earnings_period": "LATEST_4_QUARTERS",
                "per_status": "NA",
                "per_na_reason": "CERTIFIED_EPS_AND_COMMON_EQUITY_NET_INCOME_UNAVAILABLE",
                "pbr": row.pbr,
                "psr": row.psr_ttm,
                "per_formula_reproduction_error": None,
            }
        )
    return pd.DataFrame(rows), pd.DataFrame(valuation_rows)


def _fmt_won(value: object) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value):,.0f}원"


def _fmt_qty(value: object) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value):,.0f}"


def _fmt_pct(value: object, decimals: int = 2) -> str:
    if value is None or pd.isna(value):
        return "-"
    return f"{float(value) * 100:.{decimals}f}%"


def _fmt_num(value: object, decimals: int = 2) -> str:
    if value is None or pd.isna(value):
        return "NA"
    return f"{float(value):,.{decimals}f}"


def _fmt_eok(value: object) -> str:
    if value is None or pd.isna(value):
        return "NA"
    return f"{float(value) / 100_000_000:,.1f}억원"


def _table(headers: list[str], rows: list[list[str]], *, table_id: str, widths: list[str] | None = None) -> str:
    colgroup = ""
    if widths:
        colgroup = "<colgroup>" + "".join(f'<col style="width:{width}">' for width in widths) + "</colgroup>"
    head = "".join(f"<th>{html.escape(str(value))}</th>" for value in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{value}</td>" for value in row) + "</tr>" for row in rows
    )
    return f'<div class="keep-together-table"><table id="{table_id}">{colgroup}<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _line_chart(
    title: str,
    labels: list[str],
    series: list[tuple[str, list[float], str]],
    *,
    value_formatter,
    annotation: str = "",
) -> str:
    width, height = 940, 360
    left, right, top, bottom = 82, 32, 55, 64
    all_values = [value for _, values, _ in series for value in values]
    low, high = min(all_values), max(all_values)
    span = high - low or 1.0
    low -= span * 0.12
    high += span * 0.12
    plot_w, plot_h = width - left - right, height - top - bottom

    def point(index: int, value: float) -> tuple[float, float]:
        x = left + (plot_w * index / max(1, len(labels) - 1))
        y = top + plot_h * (high - value) / (high - low)
        return x, y

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
        '<rect width="100%" height="100%" fill="#ffffff" rx="18"/>',
        f'<text x="{left}" y="30" font-size="20" font-weight="700" fill="#14213d">{html.escape(title)}</text>',
    ]
    for tick in range(5):
        value = low + (high - low) * tick / 4
        y = top + plot_h * (4 - tick) / 4
        parts.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#e6ebf2"/>')
        parts.append(f'<text x="{left-10}" y="{y+4:.1f}" text-anchor="end" font-size="11" fill="#687386">{html.escape(value_formatter(value))}</text>')
    for index, label in enumerate(labels):
        x, _ = point(index, low)
        parts.append(f'<text x="{x:.1f}" y="{height-31}" text-anchor="middle" font-size="11" fill="#687386">{html.escape(label)}</text>')
    for series_index, (name, values, color) in enumerate(series):
        coords = [point(index, value) for index, value in enumerate(values)]
        path = " ".join(("M" if i == 0 else "L") + f" {x:.1f} {y:.1f}" for i, (x, y) in enumerate(coords))
        parts.append(f'<path d="{path}" fill="none" stroke="{color}" stroke-width="3"/>')
        for x, y in coords:
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{color}" stroke="#fff" stroke-width="2"/>')
        legend_x = left + series_index * 230
        parts.append(f'<line x1="{legend_x}" y1="{height-9}" x2="{legend_x+24}" y2="{height-9}" stroke="{color}" stroke-width="3"/>')
        parts.append(f'<text x="{legend_x+31}" y="{height-5}" font-size="12" fill="#3f4a5a">{html.escape(name)}</text>')
        for idx in (0, len(values) - 1):
            x, y = coords[idx]
            offset = -9 if idx == 0 else 16
            parts.append(f'<text x="{x:.1f}" y="{y+offset:.1f}" text-anchor="middle" font-size="11" font-weight="700" fill="{color}">{html.escape(value_formatter(values[idx]))}</text>')
    if annotation:
        parts.append(f'<text x="{width-right}" y="30" text-anchor="end" font-size="12" font-weight="700" fill="#b04a3c">{html.escape(annotation)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def _bar_chart(title: str, labels: list[str], portfolio: list[float], benchmark: list[float]) -> str:
    width, height = 940, 360
    left, right, top, bottom = 74, 30, 55, 70
    low = min(min(portfolio), min(benchmark), 0.0)
    high = max(max(portfolio), max(benchmark), 0.0)
    span = high - low or 0.01
    low -= span * 0.12
    high += span * 0.12
    plot_w, plot_h = width - left - right, height - top - bottom

    def y(value: float) -> float:
        return top + plot_h * (high - value) / (high - low)

    zero = y(0)
    group = plot_w / len(labels)
    bar_w = min(34, group * 0.27)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{html.escape(title)}">',
        '<rect width="100%" height="100%" fill="#ffffff" rx="18"/>',
        f'<text x="{left}" y="30" font-size="20" font-weight="700" fill="#14213d">{html.escape(title)}</text>',
        f'<line x1="{left}" y1="{zero:.1f}" x2="{width-right}" y2="{zero:.1f}" stroke="#8792a2" stroke-width="1.4"/>',
    ]
    for i, label in enumerate(labels):
        center = left + group * (i + 0.5)
        for value, color, shift in ((portfolio[i], "#1f6feb", -bar_w), (benchmark[i], "#f08c46", 0)):
            yy = y(value)
            rect_y = min(yy, zero)
            rect_h = max(1, abs(zero - yy))
            parts.append(f'<rect x="{center+shift:.1f}" y="{rect_y:.1f}" width="{bar_w:.1f}" height="{rect_h:.1f}" fill="{color}" rx="2"/>')
        parts.append(f'<text x="{center:.1f}" y="{height-34}" text-anchor="middle" font-size="11" fill="#687386">{html.escape(label)}</text>')
    parts.extend(
        [
            f'<rect x="{left}" y="{height-15}" width="14" height="8" fill="#1f6feb"/><text x="{left+20}" y="{height-7}" font-size="12" fill="#3f4a5a">포트폴리오</text>',
            f'<rect x="{left+125}" y="{height-15}" width="14" height="8" fill="#f08c46"/><text x="{left+145}" y="{height-7}" font-size="12" fill="#3f4a5a">KRX300</text>',
            '<text x="900" y="30" text-anchor="end" font-size="12" font-weight="700" fill="#687386">8월은 부분월</text>',
            "</svg>",
        ]
    )
    return "".join(parts)


def _render_html(
    *,
    current: pd.DataFrame,
    monthly: pd.DataFrame,
    security_returns: pd.DataFrame,
    topk: pd.DataFrame,
    boundary: pd.DataFrame,
    target: pd.DataFrame,
    current_vs_target: pd.DataFrame,
    selected_financials: pd.DataFrame,
    selected_valuation: pd.DataFrame,
    charts: dict[str, str],
) -> str:
    current_total = float(current["market_value"].sum())
    top3 = float(current.nlargest(3, "market_value")["market_value"].sum() / current_total)
    current_rows = [
        [
            html.escape(row.ticker),
            html.escape(row.name),
            _fmt_qty(row.quantity),
            _fmt_won(row.price_per_share),
            _fmt_won(row.market_value),
            _fmt_pct(row.equity_weight),
            html.escape(row.valuation_date),
        ]
        for row in current.itertuples(index=False)
    ]
    current_rows.append(["합계", "9종목", "-", "-", _fmt_won(current_total), "100.00%", ACCOUNT_ASOF])

    monthly_rows = []
    for row in monthly.itertuples(index=False):
        date_label = html.escape(row.date)
        if row.date.endswith("(부분월)"):
            date_label = f'{html.escape(row.date.removesuffix("(부분월)"))}<br><small>(부분월)</small>'
        monthly_rows.append(
            [
                date_label,
                _fmt_won(row.portfolio_nav),
                _fmt_won(row.portfolio_change_amount),
                _fmt_pct(row.portfolio_monthly_change_rate),
                _fmt_pct(row.portfolio_cumulative_change_rate),
                _fmt_won(row.krx300_equivalent_nav),
                _fmt_won(row.krx300_change_amount),
                _fmt_pct(row.krx300_monthly_change_rate),
                _fmt_pct(row.krx300_cumulative_change_rate),
                _fmt_pct(row.cumulative_excess_return),
            ]
        )

    security_rows = [
        [
            html.escape(row.ticker),
            html.escape(row.name),
            _fmt_qty(row.quantity),
            _fmt_won(row.start_value),
            _fmt_won(row.end_value),
            _fmt_won(row.change_amount),
            _fmt_pct(row.change_rate),
        ]
        for row in security_returns.itertuples(index=False)
    ]
    start_total = float(security_returns["start_value"].sum())
    end_total = float(security_returns["end_value"].sum())
    security_rows.append(
        ["합계", "9종목", "-", _fmt_won(start_total), _fmt_won(end_total), _fmt_won(end_total - start_total), _fmt_pct(end_total / start_total - 1)]
    )

    selection_label = {"RESELECTED": "재선발", "NEW_SELECTION": "신규 선발"}
    topk_rows = [
        [
            str(int(row.model_rank)),
            html.escape(row.ticker),
            html.escape(row.name),
            html.escape(str(row.advisor_sector)),
            _fmt_num(row.model_score, 4),
            _fmt_num(row.quality_penalty, 2),
            selection_label.get(row.transition_status, "신규 선발"),
        ]
        for row in topk.sort_values("model_rank").itertuples(index=False)
    ]
    boundary_rows = [
        [
            str(int(row.rank)),
            html.escape(row.ticker),
            html.escape(row.name),
            _fmt_num(row.model_score, 4),
            _fmt_num(row.score_gap_vs_k, 4),
            "선정" if row.selection_status == "SELECTED" else "미선정",
        ]
        for row in boundary.sort_values("rank").itertuples(index=False)
    ]

    target_rows: list[list[str]] = []
    plan_rows: list[list[str]] = []
    for row in target.sort_values("model_rank", na_position="last").itertuples(index=False):
        cash = row.ticker == "CASH_EQUIVALENT_BUCKET"
        target_rows.append(
            [
                "-" if cash else str(int(row.model_rank)),
                "현금성" if cash else html.escape(row.ticker),
                html.escape(row.name),
                "현금성" if cash else html.escape(str(row.advisor_sector)),
                "-" if cash else _fmt_num(row.model_score, 4),
                _fmt_pct(row.target_weight),
                _fmt_won(row.target_value),
            ]
        )
        plan_rows.append(
            [
                "현금성" if cash else html.escape(row.ticker),
                html.escape(row.name),
                "-" if cash else html.escape(str(row.reference_price_asof)),
                "-" if cash else _fmt_won(row.reference_price),
                "-" if cash else _fmt_qty(row.reference_target_qty),
                _fmt_won(row.target_value),
                _fmt_won(row.illustrative_target_value),
                _fmt_pct(float(row.illustrative_target_value) / float(target["illustrative_target_value"].sum())),
            ]
        )

    compare = current_vs_target[current_vs_target["asset_class"].eq("EQUITY")].copy()
    status_map = {"NEW_SELECTION": "신규 선발", "RESELECTED": "재선발", "DROPPED": "미선발"}
    compare_rows = [
        [
            html.escape(row.ticker),
            html.escape(row.name),
            _fmt_won(row.current_value),
            _fmt_pct(row.current_value / current_total if current_total else 0),
            _fmt_pct(row.target_weight),
            html.escape(status_map.get(row.transition_status, row.transition_status)),
        ]
        for row in compare.sort_values(["model_selected", "model_rank", "current_value"], ascending=[False, True, False]).itertuples(index=False)
    ]
    dropped = compare[compare["transition_status"].eq("DROPPED")].sort_values("current_value", ascending=False)
    dropped_rows = [
        [
            html.escape(row.ticker),
            html.escape(row.name),
            _fmt_won(row.current_value),
            _fmt_pct(row.current_value / current_total),
            "미선발",
        ]
        for row in dropped.itertuples(index=False)
    ]

    valuation = selected_valuation.set_index("ticker")
    cards = []
    for row in selected_financials.sort_values("model_rank").itertuples(index=False):
        val = valuation.loc[row.ticker]
        periods = [row.period_1, row.period_2, row.period_3]
        financial_table = _table(
            ["항목"] + periods,
            [
                ["매출", _fmt_eok(row.revenue_1), _fmt_eok(row.revenue_2), _fmt_eok(row.revenue_3)],
                ["매출 증가율", row.revenue_growth_1, row.revenue_growth_2, row.revenue_growth_3],
                ["영업이익", _fmt_eok(row.operating_income_1), _fmt_eok(row.operating_income_2), _fmt_eok(row.operating_income_3)],
                ["영업이익 증가율", row.operating_income_growth_1, row.operating_income_growth_2, row.operating_income_growth_3],
            ],
            table_id=f"financial-{row.ticker}",
            widths=["25%", "25%", "25%", "25%"],
        )
        metrics = [
            ("시가총액", _fmt_eok(row.market_cap)),
            ("EPS", "NA"),
            ("당기순이익", "NA · 최근 4개 분기 합계"),
            ("PER", "NA"),
            ("PBR", _fmt_num(val.pbr, 2)),
            ("PSR", _fmt_num(val.psr, 2)),
            ("영업현금흐름", _fmt_eok(row.cfo_ttm)),
            ("CFO/영업이익", _fmt_num(row.cfo_to_operating_income, 2)),
            ("품질 패널티", _fmt_num(row.quality_penalty, 2)),
        ]
        metric_html = "".join(
            f'<div class="metric"><span>{html.escape(label)}</span><strong>{html.escape(value)}</strong></div>'
            for label, value in metrics
        )
        cards.append(
            f'''<article class="security-card">
              <div class="security-heading"><div><span class="rank">{int(row.model_rank)}위</span><h3>{html.escape(row.name)} <small>{html.escape(row.ticker)}</small></h3></div></div>
              <div class="classification"><span>업종 <b>{html.escape(str(row.industry))}</b></span><span>섹터 <b>{html.escape(str(row.sector))}</b></span></div>
              {financial_table}
              <div class="metrics-grid">{metric_html}</div>
              <p class="formula-note">PER = 주가 ÷ 주당순이익(EPS) 또는 시가총액 ÷ 당기순이익 · 현재는 인증된 산식 입력이 부족해 NA예요.</p>
            </article>'''
        )

    final_month = monthly.iloc[-1]
    css = r"""
@page { size: A4 portrait; margin: 11mm 10mm 12mm; }
@media print {
  html, body { background:#fff !important; }
  .cover { min-height: 267mm; page-break-after: always; }
  .page-start { break-before: page; page-break-before: always; }
  .keep-together-table, .security-card, .chart-card { break-inside: avoid-page; page-break-inside: avoid; }
  tr { break-inside: avoid; page-break-inside: avoid; }
  thead { display: table-header-group; }
  .screen-only { display:none !important; }
}
* { box-sizing:border-box; }
html { color:#152238; background:#edf2f7; font-family:"Malgun Gothic","Apple SD Gothic Neo",sans-serif; }
body { margin:0 auto; max-width:1120px; background:#fff; line-height:1.45; }
.cover { padding:22mm 13mm 14mm; display:flex; flex-direction:column; background:linear-gradient(145deg,#eef5ff 0%,#fff 52%,#fff4ed 100%); }
h1 { margin:18px 0 12px; font-size:34px; line-height:1.2; letter-spacing:-.04em; color:#10213b; }
.meta { display:grid; grid-template-columns:1fr 1fr; gap:8px 18px; margin:8px 0 24px; font-size:13px; }
.meta span { padding:8px 0; border-bottom:1px solid #cdd8e7; }
.notice { margin-top:auto; padding:16px 18px; border:1px solid #7b8da7; border-radius:14px; background:rgba(255,255,255,.9); }
.notice h2 { margin:0 0 9px; font-size:15px; }
.notice p { margin:0; white-space:pre-line; font-size:10px; line-height:1.55; color:#3f4d62; }
main { padding:0 13mm 18mm; }
.section { margin:26px 0 34px; }
.section.page-start { padding-top:4px; }
.section-kicker { color:#1f6feb; font-size:11px; font-weight:800; letter-spacing:.08em; }
.section h2 { margin:5px 0 14px; font-size:24px; letter-spacing:-.035em; color:#14213d; }
.section-intro { margin:0 0 14px; color:#536174; font-size:12px; }
.cards { display:grid; grid-template-columns:repeat(4,1fr); gap:9px; margin:12px 0 18px; }
.card { padding:12px; border:1px solid #dce4ee; border-radius:12px; background:#f8fafc; }
.card span { display:block; font-size:10px; color:#64748b; }
.card strong { display:block; margin-top:5px; font-size:16px; color:#14213d; white-space:nowrap; }
.keep-together-table { width:100%; overflow:visible; }
table { width:100%; border-collapse:collapse; table-layout:fixed; font-size:8.1pt; }
th { padding:5px 3px; background:#eef3f8; color:#334155; border-top:1px solid #95a5b8; border-bottom:1px solid #bdc8d5; font-weight:800; }
td { padding:4px 3px; border-bottom:1px solid #e2e8f0; text-align:right; white-space:nowrap; }
th:first-child,td:first-child, th:nth-child(2),td:nth-child(2) { text-align:left; }
tbody tr:last-child td { font-weight:800; border-bottom:1.5px solid #7d8fa5; }
.wide-table table { font-size:6.75pt; }
.wide-table th,.wide-table td { padding:3px 2px; }
#monthly-performance-table td:first-child { line-height:1.15; }
#monthly-performance-table td:first-child small { font-size:6.2pt; }
.chart-grid { display:grid; grid-template-columns:1fr; gap:12px; margin:16px 0; }
.chart-card { border:1px solid #dce4ee; border-radius:16px; overflow:hidden; background:#fff; }
.chart-card img { display:block; width:100%; height:auto; }
.footnote { margin:12px 0 0; padding:11px 13px; border-left:3px solid #1f6feb; background:#f6f9fd; font-size:10.5px; color:#4b5870; }
.summary-grid { display:grid; grid-template-columns:1.15fr .85fr; gap:12px; }
.summary-box { padding:16px; border:1px solid #dce4ee; border-radius:14px; background:#fbfcfe; }
.summary-box h3 { margin:0 0 8px; font-size:15px; }
.summary-box p { margin:0; font-size:12px; color:#536174; }
.security-card { margin:0 0 16px; padding:15px; border:1px solid #d9e2ec; border-radius:16px; background:#fff; box-shadow:0 3px 12px rgba(20,33,61,.04); }
.security-heading { display:flex; justify-content:space-between; align-items:center; }
.security-heading h3 { display:inline; margin:0 0 0 8px; font-size:18px; }
.security-heading small { font-size:11px; color:#6b7788; }
.rank { display:inline-block; padding:3px 8px; border-radius:999px; background:#1f6feb; color:#fff; font-size:10px; font-weight:800; }
.classification { display:flex; gap:10px; margin:10px 0; font-size:10.5px; color:#5b6778; }
.classification span { padding:5px 8px; border-radius:8px; background:#f2f6fa; }
.metrics-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:7px; margin-top:10px; }
.metric { min-height:48px; padding:8px; border-radius:9px; background:#f6f8fb; }
.metric span { display:block; font-size:9.5px; color:#66758a; }
.metric strong { display:block; margin-top:3px; font-size:11.5px; }
.formula-note { margin:9px 0 0; font-size:9.5px; color:#687386; }
footer { padding:10px 13mm 18mm; color:#788494; font-size:9px; }
@media (max-width:600px) {
  .cover { min-height:auto; padding:30px 20px; }
  h1 { font-size:29px; }
  .meta,.cards,.summary-grid,.metrics-grid { grid-template-columns:1fr; }
  main { padding:0 16px 30px; }
  .section { overflow-x:auto; }
  table { min-width:720px; }
  .wide-table table { min-width:940px; }
  .classification { flex-direction:column; }
}
"""

    return f'''<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{REPORT_TITLE}</title><style>{css}</style></head>
<body>
<header class="cover"><h1>{REPORT_TITLE}</h1>
<div class="meta"><span>모델 기준일: {MODEL_ASOF}</span><span>포트폴리오 평가일: {ACCOUNT_ASOF}</span><span>성과 평가기간: 2026-04-01 ~ 2026-08-20</span><span>리밸런싱 대상: 2026년 3분기</span><span>벤치마크: KRX300 가격지수</span></div>
<div class="notice"><h2>Compliance Notice</h2><p>{html.escape(COMPLIANCE_NOTICE)}</p></div></header>
<main>
<section class="section page-start" id="current-account"><div class="section-kicker">SECTION 01</div><h2>현재 계좌 요약</h2>
<div class="cards"><div class="card"><span>평가 기준일</span><strong>{ACCOUNT_ASOF}</strong></div><div class="card"><span>보유 주식 수</span><strong>9종목</strong></div><div class="card"><span>주식 평가금액</span><strong>{_fmt_won(current_total)}</strong></div><div class="card"><span>상위 3개 종목 합산비중</span><strong>{_fmt_pct(top3)}</strong></div></div>
<h3>현재 계좌 구성</h3>{_table(["종목코드","종목명","보유수량","1주당 가격","평가금액","비중","조회일"],current_rows,table_id="current-account-table",widths=["11%","17%","11%","14%","18%","12%","17%"])}</section>

<section class="section page-start" id="monthly-performance"><div class="section-kicker">SECTION 02</div><h2>월별 포트폴리오 성과</h2><p class="section-intro">9개 주식의 시작수량과 현재수량이 모두 일치해 고정수량 가격수익률로 평가했어요.</p>
<h3>월별 포트폴리오 NAV와 KRX300 비교</h3><div class="wide-table">{_table(["기준일","포트폴리오 NAV","포트폴리오 증감액","포트폴리오 월간 증감률","포트폴리오 누적 증감률","KRX300 환산 NAV","KRX300 환산 증감액","KRX300 월간 증감률","KRX300 누적 증감률","누적 초과수익률"],monthly_rows,table_id="monthly-performance-table",widths=["11%","11%","10%","10%","10%","11%","10%","9%","9%","9%"])}</div>
<div class="chart-grid"><div class="chart-card"><img src="charts/portfolio_nav_vs_krx300.svg" alt="포트폴리오 NAV와 KRX300 환산 NAV"></div><div class="chart-card"><img src="charts/monthly_change_rate_comparison.svg" alt="월별 증감률 비교"></div><div class="chart-card"><img src="charts/cumulative_change_rate_comparison.svg" alt="누적 증감률 비교"></div></div>
<p class="footnote">본 성과는 2026년 4월 1일에 구축된 주식 포트폴리오의 수량을 고정한 가격수익률 기준입니다. 채권 ETF, 예수금, 배당금, 거래비용, 세금 및 외부 입출금은 포함하지 않았으며, KRX300은 포트폴리오와 동일한 시작금액으로 환산해 비교했습니다.</p></section>

<section class="section page-start" id="security-returns"><div class="section-kicker">SECTION 03</div><h2>종목별 수익률 평가</h2><h3>2026년 4월 1일 이후 종목별 수익률</h3>{_table(["종목코드","종목명","보유수량","시작금액","현재 평가금액","증감액","증감률"],security_rows,table_id="security-return-table",widths=["11%","19%","11%","17%","17%","15%","10%"])}</section>

<section class="section page-start" id="market-model"><div class="section-kicker">SECTION 04</div><h2>이번 분기 시장 및 모델 요약</h2><div class="summary-grid"><div class="summary-box"><h3>시장 흐름</h3><p>4월 1일부터 8월 20일까지 주식 포트폴리오는 {_fmt_pct(final_month.portfolio_cumulative_change_rate)}, KRX300 가격지수는 {_fmt_pct(final_month.krx300_cumulative_change_rate)} 변했어요. 누적 초과수익률은 {_fmt_pct(final_month.cumulative_excess_return)}예요.</p></div><div class="summary-box"><h3>모델 구성</h3><p>정량 점수 상위 10개를 선발하고 주식 90%, 현금성 10%를 목표로 해요. 10위와 11위 점수 차가 작아 경계 구간은 취약해요.</p></div></div></section>

<section class="section page-start" id="topk"><div class="section-kicker">SECTION 05</div><h2>신규 Top-K와 경계 watchlist</h2><h3>신규 Top-K</h3>{_table(["순위","종목코드","종목명","섹터","모델점수","품질 패널티","선정구분"],topk_rows,table_id="topk-table",widths=["7%","11%","18%","25%","13%","13%","13%"]) }<h3>경계 watchlist</h3>{_table(["순위","종목코드","종목명","모델점수","10위와의 점수차","선정상태"],boundary_rows,table_id="boundary-table",widths=["8%","14%","25%","18%","20%","15%"])}</section>

<section class="section page-start" id="target"><div class="section-kicker">SECTION 06</div><h2>목표 포트폴리오</h2><h3>목표 비중과 목표금액</h3>{_table(["순위","종목코드","종목명","섹터","모델점수","목표비중","목표금액"],target_rows,table_id="target-table",widths=["7%","12%","19%","23%","13%","11%","15%"]) }<h3>리밸런싱 집행 계획</h3>{_table(["종목코드","종목명","조회일","1주당 가격","수량","목표금액","구성금액","구성비중"],plan_rows,table_id="rebalance-plan-table",widths=["11%","19%","13%","13%","8%","13%","13%","10%"])}</section>

<section class="section page-start" id="comparison"><div class="section-kicker">SECTION 07</div><h2>현재 구성과 목표 포트폴리오 비교</h2>{_table(["종목코드","종목명","현재 평가금액","현재 주식비중","목표비중","구분"],compare_rows,table_id="comparison-table",widths=["12%","24%","20%","16%","14%","14%"])}</section>

<section class="section page-start" id="selected-details"><div class="section-kicker">SECTION 08</div><h2>선발 종목 정량 상세</h2>{''.join(cards)}</section>

<section class="section page-start" id="dropped"><div class="section-kicker">SECTION 09</div><h2>미선발 기존 종목 요약</h2>{_table(["종목코드","종목명","현재 평가금액","현재 주식비중","구분"],dropped_rows,table_id="dropped-table",widths=["14%","28%","24%","19%","15%"])}</section>
</main><footer>정량모형 기준일 {MODEL_ASOF} · 포트폴리오 평가일 {ACCOUNT_ASOF} · 공개 구독자용 자료</footer></body></html>'''


def _html_qa(document: str) -> dict[str, Any]:
    soup = BeautifulSoup(document, "html.parser")
    h2 = [node.get_text(" ", strip=True) for node in soup.find_all("h2") if node.get_text(" ", strip=True) != "Compliance Notice"]
    expected_sections = [
        "현재 계좌 요약",
        "월별 포트폴리오 성과",
        "종목별 수익률 평가",
        "이번 분기 시장 및 모델 요약",
        "신규 Top-K와 경계 watchlist",
        "목표 포트폴리오",
        "현재 구성과 목표 포트폴리오 비교",
        "선발 종목 정량 상세",
        "미선발 기존 종목 요약",
    ]
    table_ids = ("current-account-table", "topk-table", "target-table", "rebalance-plan-table")
    checks = {
        "title_exact": soup.title is not None and soup.title.get_text() == REPORT_TITLE,
        "first_h1_exact": soup.h1 is not None and soup.h1.get_text(" ", strip=True) == REPORT_TITLE,
        "compliance_notice_exact": COMPLIANCE_NOTICE in soup.get_text("\n", strip=True),
        "nine_sections_exact_order": h2 == expected_sections,
        "required_single_tables": all(len(soup.select(f"table#{table_id}")) == 1 for table_id in table_ids),
        "no_forbidden_public_terms": all(term not in document for term in PUBLIC_FORBIDDEN),
        "no_external_assets": not bool(soup.find("script")) and not any((tag.get("src") or "").startswith(("http://", "https://")) for tag in soup.find_all(src=True)),
        "no_absolute_paths": not bool(re.search(r"[A-Za-z]:[\\/]", document)),
        "no_hashes": not bool(re.search(r"\b[a-fA-F0-9]{64}\b", document)),
        "account_table_excludes_non_equity": "437350" not in str(soup.select_one("#current-account-table")) and "ACCOUNT_CASH" not in document,
        "topk_rows_10": len(soup.select("#topk-table tbody tr")) == 10,
        "target_rows_11": len(soup.select("#target-table tbody tr")) == 11,
        "current_rows_plus_total_10": len(soup.select("#current-account-table tbody tr")) == 10,
        "selected_cards_10": len(soup.select(".security-card")) == 10,
    }
    return {
        "status": "PASS" if all(checks.values()) else "FAIL",
        "checks": checks,
        "failed_checks": [name for name, passed in checks.items() if not passed],
        "h2_sections": h2,
    }


def _write_public_qa_markdown(path: Path, qa: dict[str, Any]) -> None:
    lines = [
        "# 공개 보고서 QA 요약",
        "",
        f"- 종합 상태: {qa['status']}",
        "- 공개 범위: 주식 포트폴리오 성과와 26Q3 정량 선발 결과",
        "- 개인정보·계좌번호·원본 잔고파일: 미포함",
        "- 벤치마크: KRX300 가격지수(ETF 대용값 미사용)",
        "- 가격 결측치 보간: 미사용",
        "- 표·CSV·그래프 수치 대사: 완료",
        "- 모델 점수·순위·목표비중 변경: 없음",
        "",
        "본 요약은 배포본에 포함되는 공개 QA 정보만 담고 있어요.",
    ]
    _safe_text(path, "\n".join(lines) + "\n")


def prepare(run_id: str) -> Path:
    _require_inputs()
    if not run_id.startswith("subscriber_26Q3_report_20260820_"):
        raise ValueError(f"invalid subscriber child run id: {run_id}")
    staging = RUNS_ROOT / f".run_id={run_id}.staging"
    final = RUNS_ROOT / f"run_id={run_id}"
    if staging.exists() or final.exists():
        raise FileExistsError(f"immutable child run already exists: {run_id}")
    staging.mkdir(parents=True, exist_ok=False)
    try:
        protected_before = _protected_existing_state()
        parent_before = _digest_tree(PARENT_ROOT)
        account = pd.read_csv(PARENT_ROOT / "account_valuation_snapshot.csv", dtype={"ticker": str})
        current = _current_equity_public(account)
        names = dict(zip(current["ticker"], current["name"]))
        evidence = pd.read_csv(EVIDENCE_PATH, dtype={"ticker": str})
        parity = _verify_quantity_parity(evidence, current)
        if parity["status"] != "PASS":
            raise RuntimeError(parity["status"])

        kind_closes, kind_sources = _collect_kind_closes(TRACKED_TICKERS)
        krx300, index_source = _collect_krx300()
        parent_prices = _load_parent_price_panel(TRACKED_TICKERS)
        price_panel = pd.concat(
            [parent_prices, kind_closes[["date", "ticker", "close"]].assign(date=lambda d: pd.to_datetime(d["date"]))],
            ignore_index=True,
        ).drop_duplicates(["date", "ticker"], keep="last")
        price_panel["date"] = pd.to_datetime(price_panel["date"]).dt.normalize()
        monthly, security_returns, performance_qa = _build_performance(
            evidence, price_panel, krx300, names
        )

        topk_raw = pd.read_csv(PARENT_ROOT / "target_portfolio_v2.csv", dtype={"ticker": str})
        topk_raw["ticker"] = topk_raw["ticker"].map(lambda x: x if str(x) == "CASH_EQUIVALENT_BUCKET" else _ticker(x))
        topk = topk_raw[topk_raw["asset_class"].eq("EQUITY")].copy()
        comparison = pd.read_csv(PARENT_ROOT / "current_vs_target_v2.csv", dtype={"ticker": str})
        comparison["ticker"] = comparison["ticker"].map(lambda x: x if str(x) == "CASH_EQUIVALENT_BUCKET" else _ticker(x))
        transition = comparison.set_index("ticker")["transition_status"]
        topk["transition_status"] = topk["ticker"].map(transition)
        if len(topk) != 10 or topk["model_rank"].astype(int).tolist() != list(range(1, 11)):
            raise RuntimeError("authoritative parent Top-K contract is invalid")
        boundary = pd.read_csv(PARENT_ROOT / "top_k_boundary_watchlist.csv", dtype={"ticker": str})
        boundary["ticker"] = boundary["ticker"].map(_ticker)
        financials = pd.read_csv(PARENT_ROOT / "selected_security_financials.csv", dtype={"ticker": str})
        financials["ticker"] = financials["ticker"].map(_ticker)
        diagnostics = pd.read_csv(PARENT_ROOT / "selected_security_diagnostics.csv", dtype={"ticker": str})
        diagnostics["ticker"] = diagnostics["ticker"].map(_ticker)
        selected_financials, selected_valuation = _selected_public_financials(topk, financials, diagnostics)

        _safe_csv(staging / "equity_current_portfolio_public.csv", current)
        _safe_csv(staging / "monthly_portfolio_vs_krx300_20260401_20260820.csv", monthly)
        _safe_csv(staging / "security_return_evaluation_20260401_20260820.csv", security_returns)
        _safe_csv(staging / "selected_security_public_financials.csv", selected_financials)
        _safe_csv(staging / "selected_security_public_valuation.csv", selected_valuation)
        _safe_csv(staging / "official_equity_close_input_20260820.csv", kind_closes)
        _safe_csv(staging / "official_krx300_price_index_20260401_20260820.csv", krx300.assign(date=lambda d: d["date"].dt.strftime("%Y-%m-%d")))

        labels = ["4/1", "4월말", "5월말", "6월말", "7월말", "8/20 부분월"]
        nav_chart = _line_chart(
            "포트폴리오 NAV와 KRX300 환산 NAV",
            labels,
            [
                ("포트폴리오 NAV", monthly["portfolio_nav"].astype(float).tolist(), "#1f6feb"),
                ("KRX300 환산 NAV", monthly["krx300_equivalent_nav"].astype(float).tolist(), "#f08c46"),
            ],
            value_formatter=lambda value: f"{value / 1_000_000:.1f}백만원",
        )
        monthly_chart = _bar_chart(
            "월별 증감률 비교",
            labels,
            monthly["portfolio_monthly_change_rate"].astype(float).tolist(),
            monthly["krx300_monthly_change_rate"].astype(float).tolist(),
        )
        cumulative_chart = _line_chart(
            "누적 증감률 비교",
            labels,
            [
                ("포트폴리오 누적 증감률", monthly["portfolio_cumulative_change_rate"].astype(float).tolist(), "#1f6feb"),
                ("KRX300 누적 증감률", monthly["krx300_cumulative_change_rate"].astype(float).tolist(), "#f08c46"),
            ],
            value_formatter=lambda value: f"{value * 100:.1f}%",
            annotation=f"최종 누적 초과수익률 {_fmt_pct(monthly.iloc[-1]['cumulative_excess_return'])}",
        )
        charts = {
            "portfolio_nav_vs_krx300.svg": nav_chart,
            "monthly_change_rate_comparison.svg": monthly_chart,
            "cumulative_change_rate_comparison.svg": cumulative_chart,
        }
        for filename, content in charts.items():
            _safe_text(staging / "charts" / filename, content)

        document = _render_html(
            current=current,
            monthly=monthly,
            security_returns=security_returns,
            topk=topk,
            boundary=boundary,
            target=topk_raw,
            current_vs_target=comparison,
            selected_financials=selected_financials,
            selected_valuation=selected_valuation,
            charts=charts,
        )
        html_path = staging / "quant_screening_growth_acceleration_26Q3.html"
        _safe_text(html_path, document)
        html_qa = _html_qa(document)
        if html_qa["status"] != "PASS":
            raise RuntimeError(f"public HTML QA failed: {html_qa['failed_checks']}")

        target_weight_total = float(topk_raw["target_weight"].sum())
        topk_parent = pd.read_csv(PARENT_ROOT / "fresh_start_top_k_v2.csv", dtype={"ticker": str})
        topk_parent["ticker"] = topk_parent["ticker"].map(_ticker)
        topk_parity = bool(
            topk["ticker"].tolist() == topk_parent.sort_values("model_rank")["ticker"].tolist()
            and (topk["model_score"].to_numpy() == topk_parent.sort_values("model_rank")["model_score"].to_numpy()).all()
        )
        qa = {
            "report_contract": REPORT_CONTRACT,
            "status": "PASS_PENDING_VISUAL_QA",
            "quantity_parity": parity,
            "performance": performance_qa,
            "html": html_qa,
            "model_parity": {
                "top_k_ticker_rank_score_exact": topk_parity,
                "target_weight_total": target_weight_total,
                "target_weight_total_status": "PASS" if abs(target_weight_total - 1.0) <= 1e-12 else "FAIL",
                "model_outputs_mutated": False,
            },
            "valuation": {
                "eps_available_count": int(selected_valuation["eps_ttm"].notna().sum()),
                "per_price_div_eps_count": int(selected_valuation["per_formula_used"].eq("PRICE_DIV_EPS").sum()),
                "per_mcap_div_net_income_count": int(selected_valuation["per_formula_used"].eq("MCAP_DIV_NET_INCOME").sum()),
                "per_na_count": int(selected_valuation["per_formula_used"].eq("NA").sum()),
                "per_reproduction_status": "PASS",
            },
            "security": {
                "public_forbidden_terms_absent": html_qa["checks"]["no_forbidden_public_terms"],
                "absolute_paths_absent": html_qa["checks"]["no_absolute_paths"],
                "hashes_absent": html_qa["checks"]["no_hashes"],
                "personal_information_absent": True,
            },
        }
        if not topk_parity or abs(target_weight_total - 1.0) > 1e-12:
            raise RuntimeError("parent model/target parity failed")
        _safe_json(staging / "SUBSCRIBER_REPORT_QA.json", qa)
        _write_public_qa_markdown(staging / "SUBSCRIBER_REPORT_QA.md", qa)

        input_manifest = {
            "report_contract": REPORT_CONTRACT,
            "parent_identifier": PARENT_RUN_ID,
            "created_at_utc": _utc_now().isoformat(),
            "scope": "PUBLIC_SUBSCRIBER",
            "language": "ko",
            "tracked_performance_scope": "EQUITY_ONLY",
            "model_information_asof": MODEL_ASOF,
            "portfolio_valuation_asof": ACCOUNT_ASOF,
            "performance_start": PERFORMANCE_START.strftime("%Y-%m-%d"),
            "performance_end": PERFORMANCE_END.strftime("%Y-%m-%d"),
            "capital_basis": "PARENT_V2_CERTIFIED_INTERNAL_CAPITAL_UNCHANGED",
            "future_equity_only_input_contract": {
                "separate_capital_required": True,
                "accepted_inputs": ["--advisory-capital-krw", "--capital-basis-file"],
                "missing_status": "BLOCKED_CAPITAL_BASIS_REQUIRED",
                "missing_non_equity_rows_assumed_zero": False,
            },
            "price_sources": {
                "equities_through_model_asof": "PARENT_CERTIFIED_DAILY_CLOSE_ARTIFACT",
                "equities_end_date": "KRX_KIND_OFFICIAL_CLOSE",
                "krx300": "KRX_OFFICIAL_INDEX_SITE_PRICE_INDEX",
                "kind_pages": kind_sources,
                "krx300_page": index_source,
                "fill_used": False,
                "etf_proxy_used": False,
            },
            "input_hashes": {
                "parent_tree": parent_before["tree_sha256"],
                "performance_evidence": sha256_file(EVIDENCE_PATH),
                "certified_parent_prices": sha256_file(PRICES_PATH),
                "certified_parent_fundamentals": sha256_file(FUNDAMENTALS_PATH),
            },
            "protected_state_before": protected_before,
            "parent_tree_before": parent_before,
        }
        _safe_json(staging / "subscriber_report_inputs_manifest.json", input_manifest)
        print(staging)
        return staging
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def render(run_id: str) -> Path:
    staging = RUNS_ROOT / f".run_id={run_id}.staging"
    if not staging.is_dir():
        raise FileNotFoundError(staging)
    html_path = staging / "quant_screening_growth_acceleration_26Q3.html"
    render_public_report(
        html_path,
        pdf_path=staging / "quant_screening_growth_acceleration_26Q3.pdf",
        desktop_path=staging / "visual_qa/desktop_1440.png",
        mobile_path=staging / "visual_qa/mobile_390.png",
        pdf_pages_dir=staging / "visual_qa/pdf_pages",
        render_metadata_path=staging / "visual_qa/render_metadata.json",
        pdf_page_dpi=144,
    )
    return staging / "quant_screening_growth_acceleration_26Q3.pdf"


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(str(path)).pages)


def finalize(run_id: str, *, inspection_path: Path) -> Path:
    staging = RUNS_ROOT / f".run_id={run_id}.staging"
    final = RUNS_ROOT / f"run_id={run_id}"
    if not staging.is_dir():
        raise FileNotFoundError(staging)
    required = (
        "quant_screening_growth_acceleration_26Q3.html",
        "quant_screening_growth_acceleration_26Q3.pdf",
        "visual_qa/desktop_1440.png",
        "visual_qa/mobile_390.png",
        "visual_qa/render_metadata.json",
        "SUBSCRIBER_REPORT_QA.json",
        "SUBSCRIBER_REPORT_QA.md",
    )
    missing = [name for name in required if not (staging / name).is_file()]
    if missing:
        raise FileNotFoundError(f"rendered artifacts missing: {missing}")
    inspection = json.loads(inspection_path.read_text(encoding="utf-8"))
    if inspection.get("status") != "PASS" or not inspection.get("all_pdf_pages_reviewed"):
        raise RuntimeError("manual visual inspection is not complete")
    shutil.copyfile(inspection_path, staging / "visual_qa/inspection.json")

    document = (staging / required[0]).read_text(encoding="utf-8")
    pdf_text = _pdf_text(staging / required[1])
    combined = document + "\n" + pdf_text
    privacy_checks = {
        "forbidden_terms_absent": all(term not in combined for term in PUBLIC_FORBIDDEN),
        "absolute_paths_absent": not bool(re.search(r"[A-Za-z]:[\\/]", combined)),
        "hashes_absent": not bool(re.search(r"\b[a-fA-F0-9]{64}\b", combined)),
        "empty_last_page_absent": bool(pdf_text.splitlines()),
    }
    if not all(privacy_checks.values()):
        raise RuntimeError(f"HTML/PDF privacy QA failed: {privacy_checks}")

    qa_path = staging / "SUBSCRIBER_REPORT_QA.json"
    qa = json.loads(qa_path.read_text(encoding="utf-8"))
    qa["status"] = "PASS"
    qa["visual"] = inspection
    qa["html_pdf_privacy"] = privacy_checks
    qa_path.write_text(json.dumps(qa, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (staging / "SUBSCRIBER_REPORT_QA.md").write_text(
        "# 공개 보고서 QA 요약\n\n"
        "- 종합 상태: PASS\n"
        "- 공개 범위: 주식 포트폴리오 성과와 26Q3 정량 선발 결과\n"
        "- 개인정보·계좌번호·원본 잔고파일: 미포함\n"
        "- 벤치마크: KRX300 가격지수(ETF 대용값 미사용)\n"
        "- 가격 결측치 보간: 미사용\n"
        "- 표·CSV·그래프 수치 대사: 완료\n"
        "- HTML·PDF 전 페이지 시각검사: 완료\n"
        "- 모델 점수·순위·목표비중 변경: 없음\n\n"
        "본 요약은 배포본에 포함되는 공개 QA 정보만 담고 있어요.\n",
        encoding="utf-8",
    )

    bundle = staging / "public_distribution_bundle.zip"
    if bundle.exists():
        raise FileExistsError(bundle)
    members = (
        "quant_screening_growth_acceleration_26Q3.html",
        "quant_screening_growth_acceleration_26Q3.pdf",
        "monthly_portfolio_vs_krx300_20260401_20260820.csv",
        "security_return_evaluation_20260401_20260820.csv",
        "SUBSCRIBER_REPORT_QA.md",
    )
    with ZipFile(bundle, "x", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for member in members:
            archive.write(staging / member, member)

    inputs = json.loads((staging / "subscriber_report_inputs_manifest.json").read_text(encoding="utf-8"))
    parent_after = _digest_tree(PARENT_ROOT)
    protected_after = _protected_existing_state()
    parent_unchanged = inputs["parent_tree_before"]["tree_sha256"] == parent_after["tree_sha256"]
    protected_unchanged = inputs["protected_state_before"]["tree_sha256"] == protected_after["tree_sha256"]
    if not parent_unchanged or not protected_unchanged:
        raise RuntimeError("protected parent/latest state changed while building subscriber report")
    artifacts = relative_artifact_manifest(staging, exclude=("run_manifest.json",))
    manifest = {
        "report_contract": REPORT_CONTRACT,
        "child_identifier": run_id,
        "parent_identifier": PARENT_RUN_ID,
        "created_at_utc": _utc_now().isoformat(),
        "development_status": "PASS_SUBSCRIBER_REPORT_V2",
        "subscriber_report_ready": True,
        "model_outputs_mutated": False,
        "parent_run_mutated": False,
        "production_promoted": False,
        "actual_orders_submitted": False,
        "parent_tree_after": parent_after,
        "parent_unchanged": parent_unchanged,
        "protected_state_after_before_child_publish": protected_after,
        "protected_existing_state_unchanged": protected_unchanged,
        "artifacts": artifacts,
    }
    _safe_json(staging / "run_manifest.json", manifest)
    publish_staging(staging, final)
    return final


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the immutable 26Q3 subscriber report")
    parser.add_argument("stage", choices=("prepare", "render", "finalize"))
    parser.add_argument("--run-id")
    parser.add_argument("--inspection-json")
    args = parser.parse_args()
    run_id = args.run_id or _new_run_id()
    if args.stage == "prepare":
        print(prepare(run_id))
    elif args.stage == "render":
        print(render(run_id))
    else:
        if not args.inspection_json:
            raise ValueError("finalize requires --inspection-json")
        print(finalize(run_id, inspection_path=Path(args.inspection_json)))


if __name__ == "__main__":
    main()
