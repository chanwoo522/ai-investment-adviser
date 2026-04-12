# AI Investment Adviser

한국 주식 대상의 팩터 기반 **실전 리밸런싱 파이프라인**입니다.  
핵심 철학은 **“매출 성장보다 이익의 가속이 더 중요하다”** 입니다.

이 프로젝트는 단순 백테스트를 넘어서,

- 데이터 준비
- 팩터 계산
- 실전용 점수 산출
- 리밸런싱 액션 생성
- 실행 계획 생성
- 리밸런싱 보고서 생성

까지 이어지는 **실전 운용형 파이프라인**을 목표로 합니다.

---

## 1. 프로젝트 목적

이 프로젝트의 목적은 다음과 같습니다.

1. 한국 주식 유니버스를 대상으로 분기 리밸런싱 전략을 운용한다.
2. 이익 가속도 중심의 팩터 모델로 종목을 선별한다.
3. 현재 보유 종목과 목표 포트폴리오를 비교해 BUY / HOLD / REVIEW / 감액매도 실행계획을 생성한다.
4. 결과를 md / html / csv 보고서로 남겨 재현 가능하게 관리한다.
5. 향후 AI 분류 모듈을 추가해 기존 TOP-K 점수 전략 위에 상승 예측 필터를 얹는다.

---

## 2. 핵심 전략 개요

현재 검증된 대표 전략:

- 전략명: `D_quality_filter_debt_profitaccel_liq`
- metric: `revenue_op`

전략 설명:

- 이익 가속도 중심 (로그 완화)
- 매출 가속 보조
- 연속 성장 보너스
- 부채 통제
- 유동성 필터 (거래대금 + 시가총액)

대표 점수식:

Score_raw = 1.00 × z(OpIncome_acc2_log1p)  
           + 0.25 × z(Revenue_acc2)  
           - 0.35 × z(Debt_to_Equity_log)  
           + 0.15 × op_growth_streak2  
           + 0.05 × rev_growth_streak2  

운용 제약:

- Debt_to_Equity_log <= 2.398  
- traded_value >= 1,000,000,000  
- mcap >= 100,000,000,000  
- OpIncome_ttm >= 0  
- op_cur_q >= 0  
- op_qoq > 0  

---

## 3. 전체 실행 흐름

prepare_asof  
→ build_factors_ttm_acc2  
→ make_features_live  
→ score_latest_rebalance  
→ generate_live_actions  
→ make_execution_plan  
→ generate_rebalance_report  

---

## 4. 디렉터리 구조

ai_investment_adviser/  
├─ scripts/  
│  ├─ data_pipeline/  
│  │  ├─ prepare_asof.py  
│  │  ├─ collect_fundamentals_quarterly.py  
│  │  ├─ build_factors_ttm_acc2.py  
│  │  └─ make_features_live.py  
│  └─ live/  
│     ├─ score_latest_rebalance.py  
│     ├─ generate_live_actions.py  
│     ├─ make_execution_plan.py  
│     └─ generate_rebalance_report.py  
├─ configs/  
├─ data/  
├─ run_full_live_rebalance.ps1  
└─ README.md  

---

## 5. 입력 데이터

data/processed/current_holdings_manual.csv  

필수 컬럼:

- ticker  
- name  
- shares  

---

## 6. 실행 모드

dryrun  
→ 실행 없이 명령만 출력  

refresh  
→ 전체 재생성 (API 사용량 큼)  

smart  
→ 기존 결과 재활용 + 필요한 단계만 실행  

fast  
→ 최소 단계 실행  

---

## 7. smart vs missing_only

smart = 전체 파이프라인 실행 전략  
missing_only = fundamentals 증분 수집 방식  

smart ≠ missing_only  

---

## 8. 실행 예시

.\run_full_live_rebalance.ps1 `
  -Mode smart `
  -ASOF "2026-04-12" `
  -TARGET "2026-03-31" `
  -METRIC "revenue_op" `
  -STRAT "D_quality_filter_debt_profitaccel_liq" `
  -RunScope sandbox `
  -FACTOR_V 3291 `
  -FEAT_V 3291 `
  -ACTION_V 1 `
  -EXEC_V 1 `
  -REPORT_V 1 `
  -TOTAL_VALUE 100000000 `
  -HOLDINGS_CSV ".\data\processed\current_holdings_manual.csv"

---

## 9. 후반부만 실행 (API 절약)

execution plan:

$CONFIG = @(
  ".\configs\execution.yaml",
  ".\configs\execution_config.yaml",
  ".\configs\strategy_config.yaml",
  ".\configs\config.yaml"
) | Where-Object { Test-Path $_ } | Select-Object -First 1

$ASOF="2026-04-12"
$TARGET="2026-03-31"
$METRIC="revenue_op"
$STRAT="D_quality_filter_debt_profitaccel_liq"
$TOTAL="100000000"
$EXEC_V="1"

$ACTIONS_CSV = Get-ChildItem .\data\live\actions\ -File |
  Where-Object { $_.Name -like "live_actions__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__target=${TARGET}__v=*.csv" } |
  Sort-Object LastWriteTime -Descending |
  Select-Object -First 1 -ExpandProperty FullName

python .\scripts\live\make_execution_plan.py `
  --actions_csv "$ACTIONS_CSV" `
  --total_capital $TOTAL `
  --config "$CONFIG" `
  --out_v $EXEC_V `
  --asof $ASOF `
  --metric $METRIC `
  --ret_v 1 `
  --price_date $TARGET

report:

$FEATV="3291"
$REPORT_V="1"

$FEATURES_LIVE=".\data\features\features_live\features_live__asof=${ASOF}__metric=${METRIC}__v=${FEATV}.parquet"
$PHASE1_FILE=".\data\features\features_phase1__asof=${ASOF}__src=phase1__v=1.parquet"

$SCORES_CSV = Get-ChildItem .\data\live\scores\ -File |
  Where-Object { $_.Name -like "latest_scores__asof=${ASOF}__metric=${METRIC}__strat=${STRAT}__featv=${FEATV}__target=${TARGET}__full_universe.csv" } |
  Select-Object -First 1 -ExpandProperty FullName

$EXEC_PLAN = Get-ChildItem .\data\processed\ -File |
  Where-Object { $_.Name -like "execution_plan__total=${TOTAL}__v=${EXEC_V}.csv" } |
  Select-Object -First 1 -ExpandProperty FullName

python .\scripts\live\generate_rebalance_report.py `
  --actions_csv "$ACTIONS_CSV" `
  --features_live "$FEATURES_LIVE" `
  --execution_plan "$EXEC_PLAN" `
  --supp_file "$PHASE1_FILE" `
  --strategy $STRAT `
  --asof $ASOF `
  --target_date $TARGET `
  --output_md ".\data\live\reports\rebalance_report__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.md" `
  --output_csv ".\data\live\reports\rebalance_report_detail__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.csv" `
  --output_html ".\data\live\reports\rebalance_report__asof=${ASOF}__target=${TARGET}__metric=${METRIC}__strat=${STRAT}__v=${REPORT_V}.html" `
  --scores_csv "$SCORES_CSV"

---

## 10. 주의사항

PowerShell 변수는 반드시 ${VAR} 형태 사용  

target_date는 실제 존재하는 리밸런싱 날짜 사용  

DART API 한도 초과 시 → 후반부만 실행  

---

## 11. 현재 상태

- 데이터 파이프라인 안정화 완료  
- 스코어링 완료  
- 액션 생성 완료  
- execution plan 생성 완료  
- report 생성 완료  

→ 실전 운용 가능한 상태  

---

## 12. 다음 단계

1. 런북 정리  
2. GitHub 구조 정리  
3. 파일 정리  
4. AI 분류 모듈 개발  

AI 모듈은 기존 전략을 대체하지 않고  
→ 상승 가능 종목만 통과시키는 필터로 사용 예정  
