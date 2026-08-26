from __future__ import annotations

import pandas as pd

from scripts.subscriber_report.build_subscriber_26q3_report import (
    REPORT_TITLE,
    _common_dates,
    _growth_label,
    _html_qa,
)


def test_growth_labels_follow_sign_contract() -> None:
    assert _growth_label(100, 125, operating=False) == "25.0%"
    assert _growth_label(-10, 5, operating=True) == "흑자전환"
    assert _growth_label(10, -5, operating=True) == "적자전환"
    assert _growth_label(-10, -5, operating=True) == "적자축소"
    assert _growth_label(-5, -10, operating=True) == "적자확대"
    assert _growth_label(0, 5, operating=True) == "산출 불가"


def test_common_dates_use_last_complete_month_date() -> None:
    tickers = [f"{i:06d}" for i in range(9)]
    dates = pd.to_datetime(
        ["2026-04-01", "2026-04-30", "2026-05-29", "2026-06-30", "2026-07-31", "2026-08-20"]
    )
    prices = pd.DataFrame(
        [(date, ticker, 100) for date in dates for ticker in tickers],
        columns=["date", "ticker", "close"],
    )
    benchmark = pd.DataFrame({"date": dates, "krx300_price_index": range(6)})
    import scripts.subscriber_report.build_subscriber_26q3_report as module

    original = module.TRACKED_TICKERS
    module.TRACKED_TICKERS = tuple(tickers)
    try:
        assert _common_dates(prices, benchmark) == list(dates)
    finally:
        module.TRACKED_TICKERS = original


def test_html_qa_rejects_internal_term() -> None:
    document = f"<html><head><title>{REPORT_TITLE}</title></head><body><h1>{REPORT_TITLE}</h1><p>run_id</p></body></html>"
    assert _html_qa(document)["status"] == "FAIL"
