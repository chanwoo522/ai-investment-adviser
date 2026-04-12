# 리밸런싱 보고서

본 보고서는 한국 주식 팩터 기반 분기 리밸런싱 전략의 산출물입니다.
개별 종목의 절대적 우열 판단이 아니라, 동일 시점 유니버스 내 상대 점수 비교 결과를 반영합니다.
본 보고서는 투자판단 보조를 위한 정량 리밸런싱 자료이며, 최종 주문 집행 전 유동성·이벤트·체결 가능성을 추가 점검합니다.

- 보고서 제목: **리밸런싱 보고서**
- 기준일(asof): **2026-04-12**
- 목표 리밸런싱일(target): **2026-03-31**
- 전략명: **D_quality_filter_debt_profitaccel_liq**
- 전략 설명: **이익 가속도 중심(로그 완화) + 연속 성장 보너스 + 부채 통제 + 유동성 필터(거래대금+시총)**
- 점수 재구성 원칙: **exact score csv 우선, 없으면 feature 기반 근사 재구성**

## 1. 유의사항

- 본 전략은 이익 가속도와 재무 건전성 중심의 상대평가 전략입니다.
- 대형 우량주라도 해당 시점의 점수 경쟁에서 제외될 수 있습니다.
- 과거 부진했던 기업이라도 현재 흑자 전환 및 이익 가속이 확인되면 편입될 수 있습니다.
- BUY/SELL는 절대적 우열이 아니라 이번 분기 기준 상대 점수 재정렬 결과입니다.

## 2. 요약

- BUY 종목 수: **7**
- SELL 종목 수: **0**
- HOLD/REVIEW 종목 수: **3**
- 기존 보유 상위 점수 유지 종목 수: **2**

## 3. SCORE 계산 방식

### 3.1 점수 식

```text
Score_raw =  1.00 × z(OpIncome_acc2_log1p)
           + 0.25 × z(Revenue_acc2)
           - 0.35 × z(Debt_to_Equity_log)
           + 0.15 × op_growth_streak2
           + 0.05 × rev_growth_streak2
```

- 스코어링 방식: Cross-sectional zscore 기준 조합 / clip_z=4.0 / holding_bonus=0.5 / robust_z=true / tiered_clip=(|z|≤1.0: 1.0배, |z|≤2.0: 0.8배, |z|≤3.0: 0.6배, |z|≤4.0: 0.4배)

### 3.2 계산 절차

- 1) 리밸런싱 시점 유니버스를 구성한다.
- 2) 전략에서 사용하는 팩터를 계산한다.
- 3) 연속형 팩터는 동일 시점 유니버스 내 cross-sectional z-score로 표준화한다.
- 4) extreme clipping 설정이 있으면 표준화 점수에 상하한을 적용한다.
- 5) 가중합으로 Score_raw를 계산한다.
- 6) holding bonus 설정이 있고 현재 보유 종목이면 Score_adj에 보너스를 더한다.
- 7) 전략 필터를 적용한 뒤 최종 점수 순으로 상위 종목을 편입한다.

### 3.3 핵심 팩터 설명

- **OpIncome_acc2_log1p**: OpIncome_acc2를 signed log1p로 완화한 팩터. 극단값 영향은 줄이되 이익 가속의 방향성과 크기는 유지한다.
- **Revenue_acc2**: 최근 구간에서 매출 증가 속도가 얼마나 가속되었는지를 나타내는 팩터. 매출 모멘텀의 가속 여부를 본다.
- **Debt_to_Equity_log**: 부채/자본 비율을 로그 변환한 값. 높을수록 재무 레버리지가 큰 상태로 보고 감점 요인으로 사용한다.
- **op_growth_streak2**: 최근 두 구간 연속으로 단분기 영업이익이 개선되고 현재 단분기 영업이익이 양수인 경우 1, 아니면 0인 보너스 팩터.
- **rev_growth_streak2**: 최근 두 구간 연속으로 단분기 매출이 증가한 경우 1, 아니면 0인 보너스 팩터.

### 3.4 포트폴리오 종목별 SCORE 계산 표시

- 각 종목에 대해 contrib_op_log / contrib_op_streak / contrib_rev / contrib_rev_streak / contrib_debt / contrib_cfo / contrib_missing를 별도로 출력합니다.
- score_rebuilt는 가능한 경우 score_latest_rebalance의 실제 factor contribution을 합산한 값입니다.
- exact scoring source를 찾지 못한 경우에만 feature 기반 근사 재구성을 사용합니다.

### 3.5 필터 / 제약조건

- Debt_to_Equity_log <= 2.398
- traded_value >= 1000000000
- mcap >= 100000000000
- OpIncome_ttm >= 0
- op_cur_q >= 0
- op_qoq > 0

## 4. BUY 종목 요약표

| ticker | name | action | reason | industry_code | industry_name | score | score_adj | hold_bonus_applied | score_rank | op_acc2 | op_acc2_log1p | contrib_op_log | op_growth_streak2 | contrib_op_streak | rev_acc2 | contrib_rev | rev_growth_streak2 | contrib_rev_streak | debt_log | contrib_debt | cfo_to_assets | cfo_isnull | score_rebuilt | price | mcap | year | quarter | revenue_prev_q | revenue_cur_q | revenue_qoq | op_prev_q | op_cur_q | op_qoq | revenue | op_income | net_income | per | pbr | psr | planned_qty | planned_value | day1_qty | day1_value | day2_qty | day2_value | day3_qty | day3_value | est_slippage_bps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 218410 | RFHIC | BUY | 전략 신규 편입 (rank=1) | 032604 | 통신 및 방송 장비 제조업 | 3.69 | 3.69 | 0.0 | 1.00 | 15.14 | 2.78 | 2.80 | 0 | 0.00 | 0.59 | 0.70 | 0 | 0.00 | 0.32 | 0.19 | 0.05 | 0 | 3.69 | 75,000 | 1.33조원 | 2,025 | 4 | 405억원 | 688억원 | 0.70 | 74억원 | 115억원 | 0.56 | 1,858억원 | 309억원 | 355억원 | 37.52 | 3.36 | 7.17 | 147 | 11백만원 | 59 | 4백만원 | 44 | 3백만원 | 44 | 3백만원 | 5.00 |
| 060280 | 큐렉소 | BUY | 전략 신규 편입 (rank=2) | 074708 | 기타 상품 전문 소매업 | 3.55 | 3.55 | 0.0 | 2.00 | 4.72 | 1.74 | 2.25 | 1 | 0.15 | 0.58 | 0.70 | 1 | 0.05 | 0.05 | 0.40 | -0.11 | 0 | 3.55 | 14,610 | 7,166억원 | 2,025 | 4 | 172억원 | 208억원 | 0.21 | 3억원 | 14억원 | 3.05 | 745억원 | 24억원 | 28억원 | 258.56 | 7.45 | 9.61 | 760 | 11백만원 | 304 | 4백만원 | 228 | 3백만원 | 228 | 3백만원 | 5.01 |
| 041920 | 메디아나 | BUY | 전략 신규 편입 (rank=3) | 032701 | 의료용 기기 제조업 | 3.32 | 3.32 | 0.0 | 3.00 | 4.41 | 1.69 | 2.19 | 1 | 0.15 | 0.41 | 0.60 | 0 | 0.00 | 0.08 | 0.38 | 0.04 | 0 | 3.32 | 20,200 | 4,798억원 | 2,025 | 4 | 150억원 | 192억원 | 0.28 | 16억원 | 19억원 | 0.19 | 649억원 | 60억원 | 53억원 | 90.97 | 3.54 | 7.39 | 550 | 11백만원 | 220 | 4백만원 | 165 | 3백만원 | 165 | 3백만원 | 5.02 |
| 425420 | 티에프이 | BUY | 전략 신규 편입 (rank=4) | 032602 | 전자부품 제조업 | 3.21 | 3.21 | 0.0 | 4.00 | 3.86 | 1.58 | 2.09 | 1 | 0.15 | 0.60 | 0.70 | 1 | 0.05 | 0.27 | 0.22 | 0.09 | 0 | 3.21 | 58,100 | 4,991억원 | 2,025 | 4 | 272억원 | 374억원 | 0.38 | 48억원 | 73억원 | 0.52 | 1,117억원 | 191억원 | 181억원 | 27.56 | 4.57 | 4.47 | 190 | 11백만원 | 76 | 4백만원 | 57 | 3백만원 | 57 | 3백만원 | 5.01 |
| 098460 | 고영 | BUY | 전략 신규 편입 (rank=5) | 032902 | 특수 목적용 기계 제조업 | 3.21 | 3.21 | 0.0 | 5.00 | 5.06 | 1.80 | 2.31 | 1 | 0.15 | 0.25 | 0.42 | 1 | 0.05 | 0.20 | 0.28 | 0.04 | 0 | 3.21 | 24,450 | 2.13조원 | 2,025 | 4 | 603억원 | 691억원 | 0.15 | 47억원 | 69억원 | 0.48 | 2,326억원 | 173억원 | 148억원 | 144.46 | 6.36 | 9.16 | 453 | 11백만원 | 181 | 4백만원 | 136 | 3백만원 | 136 | 3백만원 | 5.00 |
| 171090 | 선익시스템 | BUY | 전략 신규 편입 (rank=6) | 032902 | 특수 목적용 기계 제조업 | 3.18 | 3.18 | 0.0 | 6.00 | 16.05 | 2.84 | 2.80 | 0 | 0.00 | 2.76 | 0.70 | 0 | 0.00 | 0.93 | -0.32 | 0.27 | 0 | 3.18 | 83,800 | 9,494억원 | 2,025 | 4 | 888억원 | 2,210억원 | 1.49 | 198억원 | 536억원 | 1.71 | 5,158억원 | 1,115억원 | 965억원 | 9.84 | 6.60 | 1.84 | 131 | 11백만원 | 53 | 4백만원 | 39 | 3백만원 | 39 | 3백만원 | 5.01 |
| 005690 | 파미셀 | BUY | 전략 신규 편입 (rank=7) | 032101 | 기초 의약물질 제조업 | 3.02 | 3.02 | 0.0 | 7.00 | 3.78 | 1.56 | 2.07 | 0 | 0.00 | 0.61 | 0.70 | 0 | 0.00 | 0.24 | 0.25 | - | 1 | 3.02 | 14,590 | 9,813억원 | 2,025 | 4 | 256억원 | 346억원 | 0.35 | 81억원 | 96억원 | 0.18 | 1,141억원 | 343억원 | 403억원 | 24.36 | 7.92 | 8.60 | 760 | 11백만원 | 304 | 4백만원 | 228 | 3백만원 | 228 | 3백만원 | 5.01 |

## 5. SELL 종목 요약표

_없음_

## 6. REVIEW 종목 요약표

| ticker | name | action | reason | industry_code | industry_name | score | score_adj | hold_bonus_applied | score_rank | op_acc2 | op_acc2_log1p | contrib_op_log | op_growth_streak2 | contrib_op_streak | rev_acc2 | contrib_rev | rev_growth_streak2 | contrib_rev_streak | debt_log | contrib_debt | cfo_to_assets | cfo_isnull | score_rebuilt | price | mcap | year | quarter | revenue_prev_q | revenue_cur_q | revenue_qoq | op_prev_q | op_cur_q | op_qoq | revenue | op_income | net_income | per | pbr | psr | cohort_status | score_availability_reason | planned_qty | planned_value | day1_qty | day1_value | day2_qty | day2_value | day3_qty | day3_value | est_slippage_bps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 005930 | 삼성전자 | HOLD | 기존 보유 상위 점수 유지 (rank=153) | 032604 | 통신 및 방송 장비 제조업 | -1.90 | -1.40 | 0.5 | 153.00 | -3.65 | -1.54 | -2.23 | 1 | 0.15 | -0.05 | -0.11 | 1 | 0.05 | 0.26 | 0.23 | 0.15 | 0 | -1.90 | 167,200 | 1,081.72조원 | 2,025 | 4 | 86.06조원 | 93.84조원 | 0.09 | 12.17조원 | 20.07조원 | 0.65 | 333.61조원 | 43.60조원 | 45.21조원 | 23.93 | 2.48 | 3.24 | in_target_cohort | scored_in_target_cohort | 33 | 6백만원 | 23 | 4백만원 | 10 | 2백만원 | 0 | 0원 | 5.00 |
| 058470 | 리노공업 | HOLD_REVIEW | 평가 보류 유지 (no_feature_history) | 032602 | 전자부품 제조업 | - | - | - | - | 0.34 | - | - | 0 | - | 0.25 | - | 0 | - | 0.08 | - | - | 1 | - | 94,400 | 1.49조원 | 2,025 | 4 | 968억원 | 848억원 | -0.12 | 483억원 | 404억원 | -0.16 | 3,725억원 | 1,770억원 | 1,520억원 | 9.78 | 2.03 | 3.99 | not_in_target_cohort | no_feature_history | 50 | 5백만원 | 35 | 3백만원 | 15 | 1백만원 | 0 | 0원 | 5.00 |
| 214150 | 클래시스 | HOLD | 기존 보유 상위 점수 유지 (rank=73) | 032701 | 의료용 기기 제조업 | 0.20 | 0.70 | 0.5 | 73.00 | 0.03 | 0.03 | -0.11 | 0 | 0.00 | 0.04 | 0.06 | 0 | 0.00 | 0.24 | 0.25 | 0.23 | 0 | 0.20 | 50,500 | 4.15조원 | 2,025 | 4 | 830억원 | 934억원 | 0.13 | 376억원 | 512억원 | 0.36 | 3,368억원 | 1,706억원 | 1,320억원 | 31.47 | 7.53 | 12.33 | in_target_cohort | scored_in_target_cohort | 378 | 19백만원 | 265 | 13백만원 | 113 | 6백만원 | 0 | 0원 | 5.01 |

## 7. BUY 종목 상세

### RFHIC (218410)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=1)
- 보고서 SCORE: **3.6867** / rank 1
- 조정 SCORE: **3.6867**
- holding bonus: 0.0000
- 재구성 SCORE: **3.6867**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 032604
- 업종명: 통신 및 방송 장비 제조업
- 현재가: 75,000
- 시가총액: 1.33조원

재무:
- 매출(TTM): 1,858억원
- 영업이익(TTM): 309억원
- 순이익(TTM): 355억원
- 영업현금흐름(CFO): 295억원

팩터:
- OpIncome_acc2: 15.1436
- OpIncome_acc2_log1p: 2.7815
- op_growth_streak2: 0.0000
- Revenue_acc2: 0.5865
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.3204
- Quality_CFO_to_Assets: 0.0540
- CFO_isnull: 0

스코어 기여도:
- contrib_op_log: 2.8000
- contrib_op_streak: 0.0000
- contrib_rev: 0.7000
- contrib_rev_streak: 0.0000
- contrib_debt: 0.1867

멀티플:
- PER: 37.52
- PBR: 3.36
- PSR: 7.17
- EV/EBIT: 47.97

실행 계획:
- 총 목표 주문수량: 147주
- 총 목표 주문금액: 11백만원
- Day1: 59주 / 4백만원
- Day2: 44주 / 3백만원
- Day3: 44주 / 3백만원
- 예상 슬리피지: 5.002 bps

### 큐렉소 (060280)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=2)
- 보고서 SCORE: **3.5484** / rank 2
- 조정 SCORE: **3.5484**
- holding bonus: 0.0000
- 재구성 SCORE: **3.5484**
- SCORE 차이(보고서-재구성): -0.0000
- industry_code: 074708
- 업종명: 기타 상품 전문 소매업
- 현재가: 14,610
- 시가총액: 7,166억원

재무:
- 매출(TTM): 745억원
- 영업이익(TTM): 24억원
- 순이익(TTM): 28억원
- 영업현금흐름(CFO): -113억원

팩터:
- OpIncome_acc2: 4.7214
- OpIncome_acc2_log1p: 1.7442
- op_growth_streak2: 1.0000
- Revenue_acc2: 0.5802
- rev_growth_streak2: 1.0000
- Debt_to_Equity_log: 0.0495
- Quality_CFO_to_Assets: -0.1113
- CFO_isnull: 0

스코어 기여도:
- contrib_op_log: 2.2503
- contrib_op_streak: 0.1500
- contrib_rev: 0.7000
- contrib_rev_streak: 0.0500
- contrib_debt: 0.3981

멀티플:
- PER: 258.56
- PBR: 7.45
- PSR: 9.61
- EV/EBIT: 305.49

실행 계획:
- 총 목표 주문수량: 760주
- 총 목표 주문금액: 11백만원
- Day1: 304주 / 4백만원
- Day2: 228주 / 3백만원
- Day3: 228주 / 3백만원
- 예상 슬리피지: 5.005 bps

### 메디아나 (041920)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=3)
- 보고서 SCORE: **3.3248** / rank 3
- 조정 SCORE: **3.3248**
- holding bonus: 0.0000
- 재구성 SCORE: **3.3248**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 032701
- 업종명: 의료용 기기 제조업
- 현재가: 20,200
- 시가총액: 4,798억원

재무:
- 매출(TTM): 649억원
- 영업이익(TTM): 60억원
- 순이익(TTM): 53억원
- 영업현금흐름(CFO): 55억원

팩터:
- OpIncome_acc2: 4.4057
- OpIncome_acc2_log1p: 1.6875
- op_growth_streak2: 1.0000
- Revenue_acc2: 0.4113
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.0808
- Quality_CFO_to_Assets: 0.0375
- CFO_isnull: 0

스코어 기여도:
- contrib_op_log: 2.1936
- contrib_op_streak: 0.1500
- contrib_rev: 0.6037
- contrib_rev_streak: 0.0000
- contrib_debt: 0.3774

멀티플:
- PER: 90.97
- PBR: 3.54
- PSR: 7.39
- EV/EBIT: 82.56

실행 계획:
- 총 목표 주문수량: 550주
- 총 목표 주문금액: 11백만원
- Day1: 220주 / 4백만원
- Day2: 165주 / 3백만원
- Day3: 165주 / 3백만원
- 예상 슬리피지: 5.017 bps

### 티에프이 (425420)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=4)
- 보고서 SCORE: **3.2129** / rank 4
- 조정 SCORE: **3.2129**
- holding bonus: 0.0000
- 재구성 SCORE: **3.2129**
- SCORE 차이(보고서-재구성): -0.0000
- industry_code: 032602
- 업종명: 전자부품 제조업
- 현재가: 58,100
- 시가총액: 4,991억원

재무:
- 매출(TTM): 1,117억원
- 영업이익(TTM): 191억원
- 순이익(TTM): 181억원
- 영업현금흐름(CFO): 128억원

팩터:
- OpIncome_acc2: 3.8639
- OpIncome_acc2_log1p: 1.5818
- op_growth_streak2: 1.0000
- Revenue_acc2: 0.6011
- rev_growth_streak2: 1.0000
- Debt_to_Equity_log: 0.2742
- Quality_CFO_to_Assets: 0.0892
- CFO_isnull: 0

스코어 기여도:
- contrib_op_log: 2.0881
- contrib_op_streak: 0.1500
- contrib_rev: 0.7000
- contrib_rev_streak: 0.0500
- contrib_debt: 0.2248

멀티플:
- PER: 27.56
- PBR: 4.57
- PSR: 4.47
- EV/EBIT: 28.00

실행 계획:
- 총 목표 주문수량: 190주
- 총 목표 주문금액: 11백만원
- Day1: 76주 / 4백만원
- Day2: 57주 / 3백만원
- Day3: 57주 / 3백만원
- 예상 슬리피지: 5.006 bps

### 고영 (098460)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=5)
- 보고서 SCORE: **3.2095** / rank 5
- 조정 SCORE: **3.2095**
- holding bonus: 0.0000
- 재구성 SCORE: **3.2095**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 032902
- 업종명: 특수 목적용 기계 제조업
- 현재가: 24,450
- 시가총액: 2.13조원

재무:
- 매출(TTM): 2,326억원
- 영업이익(TTM): 173억원
- 순이익(TTM): 148억원
- 영업현금흐름(CFO): 181억원

팩터:
- OpIncome_acc2: 5.0568
- OpIncome_acc2_log1p: 1.8012
- op_growth_streak2: 1.0000
- Revenue_acc2: 0.2510
- rev_growth_streak2: 1.0000
- Debt_to_Equity_log: 0.2023
- Quality_CFO_to_Assets: 0.0441
- CFO_isnull: 0

스코어 기여도:
- contrib_op_log: 2.3072
- contrib_op_streak: 0.1500
- contrib_rev: 0.4182
- contrib_rev_streak: 0.0500
- contrib_debt: 0.2841

멀티플:
- PER: 144.46
- PBR: 6.36
- PSR: 9.16
- EV/EBIT: 127.26

실행 계획:
- 총 목표 주문수량: 453주
- 총 목표 주문금액: 11백만원
- Day1: 181주 / 4백만원
- Day2: 136주 / 3백만원
- Day3: 136주 / 3백만원
- 예상 슬리피지: 5.002 bps

### 선익시스템 (171090)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=6)
- 보고서 SCORE: **3.1811** / rank 6
- 조정 SCORE: **3.1811**
- holding bonus: 0.0000
- 재구성 SCORE: **3.1811**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 032902
- 업종명: 특수 목적용 기계 제조업
- 현재가: 83,800
- 시가총액: 9,494억원

재무:
- 매출(TTM): 5,158억원
- 영업이익(TTM): 1,115억원
- 순이익(TTM): 965억원
- 영업현금흐름(CFO): 973억원

팩터:
- OpIncome_acc2: 16.0541
- OpIncome_acc2_log1p: 2.8364
- op_growth_streak2: 0.0000
- Revenue_acc2: 2.7580
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.9331
- Quality_CFO_to_Assets: 0.2661
- CFO_isnull: 0

스코어 기여도:
- contrib_op_log: 2.8000
- contrib_op_streak: 0.0000
- contrib_rev: 0.7000
- contrib_rev_streak: 0.0000
- contrib_debt: -0.3189

멀티플:
- PER: 9.84
- PBR: 6.60
- PSR: 1.84
- EV/EBIT: 10.50

실행 계획:
- 총 목표 주문수량: 131주
- 총 목표 주문금액: 11백만원
- Day1: 53주 / 4백만원
- Day2: 39주 / 3백만원
- Day3: 39주 / 3백만원
- 예상 슬리피지: 5.005 bps

### 파미셀 (005690)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=7)
- 보고서 SCORE: **3.0244** / rank 7
- 조정 SCORE: **3.0244**
- holding bonus: 0.0000
- 재구성 SCORE: **3.0244**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 032101
- 업종명: 기초 의약물질 제조업
- 현재가: 14,590
- 시가총액: 9,813억원

재무:
- 매출(TTM): 1,141억원
- 영업이익(TTM): 343억원
- 순이익(TTM): 403억원

팩터:
- OpIncome_acc2: 3.7760
- OpIncome_acc2_log1p: 1.5636
- op_growth_streak2: 0.0000
- Revenue_acc2: 0.6054
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.2381
- CFO_isnull: 1

스코어 기여도:
- contrib_op_log: 2.0698
- contrib_op_streak: 0.0000
- contrib_rev: 0.7000
- contrib_rev_streak: 0.0000
- contrib_debt: 0.2546

멀티플:
- PER: 24.36
- PBR: 7.92
- PSR: 8.60
- EV/EBIT: 29.58

실행 계획:
- 총 목표 주문수량: 760주
- 총 목표 주문금액: 11백만원
- Day1: 304주 / 4백만원
- Day2: 228주 / 3백만원
- Day3: 228주 / 3백만원
- 예상 슬리피지: 5.008 bps

## 8. SELL 종목 상세

_없음_
## 9. REVIEW 종목 상세

### 삼성전자 (005930)

- 액션: **HOLD**
- 사유: 기존 보유 상위 점수 유지 (rank=153)
- 보고서 SCORE: **-1.9011** / rank 153
- 조정 SCORE: **-1.4011**
- holding bonus: 0.5000
- 재구성 SCORE: **-1.9011**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 032604
- 업종명: 통신 및 방송 장비 제조업
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 167,200
- 시가총액: 1,081.72조원

재무:
- 매출(TTM): 333.61조원
- 영업이익(TTM): 43.60조원
- 순이익(TTM): 45.21조원
- 영업현금흐름(CFO): 85.32조원

팩터:
- OpIncome_acc2: -3.6511
- OpIncome_acc2_log1p: -1.5371
- op_growth_streak2: 1.0000
- Revenue_acc2: -0.0532
- rev_growth_streak2: 1.0000
- Debt_to_Equity_log: 0.2619
- Quality_CFO_to_Assets: 0.1505
- CFO_isnull: 0

스코어 기여도:
- contrib_op_log: -2.2285
- contrib_op_streak: 0.1500
- contrib_rev: -0.1076
- contrib_rev_streak: 0.0500
- contrib_debt: 0.2349

멀티플:
- PER: 23.93
- PBR: 2.48
- PSR: 3.24
- EV/EBIT: 27.81

실행 계획:
- 총 목표 주문수량: 33주
- 총 목표 주문금액: 6백만원
- Day1: 23주 / 4백만원
- Day2: 10주 / 2백만원
- Day3: 0주 / 0원
- 예상 슬리피지: 5.000 bps

### 리노공업 (058470)

- 액션: **HOLD_REVIEW**
- 사유: 평가 보류 유지 (no_feature_history)
- 보고서 SCORE: **-** / rank -
- 조정 SCORE: **-**
- holding bonus: -
- 재구성 SCORE: **-**
- SCORE 차이(보고서-재구성): -
- industry_code: 032602
- 업종명: 전자부품 제조업
- cohort_status: not_in_target_cohort
- score_availability_reason: no_feature_history
- 현재가: 94,400
- 시가총액: 1.49조원

재무:
- 매출(TTM): 3,725억원
- 영업이익(TTM): 1,770억원
- 순이익(TTM): 1,520억원

팩터:
- OpIncome_acc2: 0.3392
- op_growth_streak2: 0.0000
- Revenue_acc2: 0.2507
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.0798
- CFO_isnull: 1

스코어 기여도:

멀티플:
- PER: 9.78
- PBR: 2.03
- PSR: 3.99
- EV/EBIT: 8.74

실행 계획:
- 총 목표 주문수량: 50주
- 총 목표 주문금액: 5백만원
- Day1: 35주 / 3백만원
- Day2: 15주 / 1백만원
- Day3: 0주 / 0원
- 예상 슬리피지: 5.000 bps

### 클래시스 (214150)

- 액션: **HOLD**
- 사유: 기존 보유 상위 점수 유지 (rank=73)
- 보고서 SCORE: **0.2014** / rank 73
- 조정 SCORE: **0.7014**
- holding bonus: 0.5000
- 재구성 SCORE: **0.2014**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 032701
- 업종명: 의료용 기기 제조업
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 50,500
- 시가총액: 4.15조원

재무:
- 매출(TTM): 3,368억원
- 영업이익(TTM): 1,706억원
- 순이익(TTM): 1,320억원
- 영업현금흐름(CFO): 1,639억원

팩터:
- OpIncome_acc2: 0.0271
- OpIncome_acc2_log1p: 0.0267
- op_growth_streak2: 0.0000
- Revenue_acc2: 0.0377
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.2447
- Quality_CFO_to_Assets: 0.2326
- CFO_isnull: 0

스코어 기여도:
- contrib_op_log: -0.1097
- contrib_op_streak: 0.0000
- contrib_rev: 0.0620
- contrib_rev_streak: 0.0000
- contrib_debt: 0.2491

멀티플:
- PER: 31.47
- PBR: 7.53
- PSR: 12.33
- EV/EBIT: 25.24

실행 계획:
- 총 목표 주문수량: 378주
- 총 목표 주문금액: 19백만원
- Day1: 265주 / 13백만원
- Day2: 113주 / 6백만원
- Day3: 0주 / 0원
- 예상 슬리피지: 5.013 bps
