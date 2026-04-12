# src/schema.py
# NOTE: 기존 값/이름은 절대 변경 금지 (호환성 유지)

MONTH_COL = "month_end"   # 절대 변경 금지
TICKER_COL = "ticker"
NAME_COL = "name"

# returns_monthly schema
RETURNS_MONTHLY_COLS = [
    TICKER_COL,
    MONTH_COL,
    "ret_1m",          # (t-1 month_end -> t month_end) 단일 정의
]

# inputs_long schema (디버깅용 핵심 로그 테이블)
INPUTS_LONG_BASE_COLS = [
    TICKER_COL,
    NAME_COL,
    "rebalance_month_end",
    "asof",
    "selected",
    "score_total",
    "ret_1m",
]

# -----------------------------
# 아래는 "추가" (기존 코드 호환성 유지)
# -----------------------------

# features_live 최소 필수 컬럼 (백테스트/스코어링 공통 기반)
FEATURES_LIVE_REQUIRED_COLS = [
    TICKER_COL,
    "year",
    "quarter",
]

# returns_monthly 필수 컬럼(고정)
RETURNS_MONTHLY_REQUIRED_COLS = RETURNS_MONTHLY_COLS[:]

# inputs_long에서 반드시 있어야 하는 최소 컬럼 (디버깅/리포트)
INPUTS_LONG_REQUIRED_COLS = INPUTS_LONG_BASE_COLS[:]

# inputs_long에 남기는 근거 컬럼 suffix (가독성/검증용)
EVIDENCE_SUFFIXES = ["__raw", "__z", "__rank", "__mult", "__contrib"]