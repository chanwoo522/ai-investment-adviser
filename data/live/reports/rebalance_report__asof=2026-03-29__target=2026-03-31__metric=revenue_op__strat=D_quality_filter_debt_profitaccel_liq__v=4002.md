# 리밸런싱 보고서

본 보고서는 한국 주식 팩터 기반 분기 리밸런싱 전략의 산출물입니다.
개별 종목의 절대적 우열 판단이 아니라, 동일 시점 유니버스 내 상대 점수 비교 결과를 반영합니다.
본 보고서는 투자판단 보조를 위한 정량 리밸런싱 자료이며, 최종 주문 집행 전 유동성·이벤트·체결 가능성을 추가 점검합니다.

- 기준일(asof): **2026-03-29**
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

- BUY 종목 수: **3**
- SELL 종목 수: **2**
- HOLD/REVIEW 종목 수: **7**
- 기존 보유 상위 점수 유지 종목 수: **3**

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
| 098460 | 고영 | BUY | 전략 신규 편입 (rank=4) | 032902 | 특수 목적용 기계 제조업 | 3.29 | 3.29 | 0.0 | 4.00 | 5.06 | 1.80 | 2.39 | 1 | 0.15 | 0.25 | 0.42 | 1 | 0.05 | 0.20 | 0.28 | 0.04 | 0 | 3.29 | 27,300 | 2.13조원 | 2,025 | 4 | 603억원 | 691억원 | 0.15 | 47억원 | 69억원 | 0.48 | 2,326억원 | 173억원 | 148억원 | 144.46 | 6.36 | 9.16 | 260 | 7백만원 | 104 | 3백만원 | 78 | 2백만원 | 78 | 2백만원 | 5.00 |
| 100840 | SNT에너지 | BUY | 전략 신규 편입 (rank=7) | 032901 | 일반 목적용 기계 제조업 | 2.98 | 2.98 | 0.0 | 7.00 | 3.93 | 1.60 | 2.18 | 0 | 0.00 | 1.15 | 0.70 | 1 | 0.05 | 0.48 | 0.06 | 0.17 | 0 | 2.98 | 48,200 | 9,803억원 | 2,025 | 4 | 1,483억원 | 2,019억원 | 0.36 | 243억원 | 468억원 | 0.92 | 6,061억원 | 1,113억원 | 844억원 | 11.62 | 2.69 | 1.62 | 147 | 7백만원 | 59 | 3백만원 | 44 | 2백만원 | 44 | 2백만원 | 5.00 |
| 353200 | 대덕전자 | BUY | 전략 신규 편입 (rank=8) | 032602 | 전자부품 제조업 | 2.95 | 2.95 | 0.0 | 8.00 | 3.88 | 1.59 | 2.17 | 1 | 0.15 | 0.21 | 0.36 | 1 | 0.05 | 0.27 | 0.23 | 0.06 | 0 | 2.95 | 85,400 | 2.84조원 | 2,025 | 4 | 2,862억원 | 3,179억원 | 0.11 | 244억원 | 289억원 | 0.18 | 1.07조원 | 491억원 | 476억원 | 59.69 | 3.17 | 2.67 | 83 | 7백만원 | 33 | 3백만원 | 25 | 2백만원 | 25 | 2백만원 | 5.00 |

## 5. SELL 종목 요약표

| ticker | name | action | reason | industry_code | industry_name | score | score_adj | hold_bonus_applied | score_rank | op_acc2 | op_acc2_log1p | contrib_op_log | op_growth_streak2 | contrib_op_streak | rev_acc2 | contrib_rev | rev_growth_streak2 | contrib_rev_streak | debt_log | contrib_debt | cfo_to_assets | cfo_isnull | score_rebuilt | price | mcap | year | quarter | revenue_prev_q | revenue_cur_q | revenue_qoq | op_prev_q | op_cur_q | op_qoq | revenue | op_income | net_income | per | pbr | psr | cohort_status | score_availability_reason | planned_qty | planned_value | day1_qty | day1_value | day2_qty | day2_value | day3_qty | day3_value | est_slippage_bps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 005930 | 삼성전자 | SELL | 전략 탈락 (universe_rank=144) | 032604 | 통신 및 방송 장비 제조업 | -1.93 | -1.43 | 0.5 | 144.00 | -3.65 | -1.54 | -2.26 | 1 | 0.15 | -0.05 | -0.11 | 1 | 0.05 | 0.26 | 0.24 | 0.15 | 0 | -1.93 | 179,700 | 1,081.72조원 | 2,025 | 4 | 86.06조원 | 93.84조원 | 0.09 | 12.17조원 | 20.07조원 | 0.65 | 333.61조원 | 43.60조원 | 45.21조원 | 23.93 | 2.48 | 3.24 | in_target_cohort | scored_in_target_cohort | 100 | 18백만원 | 70 | 13백만원 | 30 | 5백만원 | 0 | 0원 | 5.00 |
| 214150 | 클래시스 | SELL | 전략 탈락 (universe_rank=68) | 032701 | 의료용 기기 제조업 | 0.23 | 0.73 | 0.5 | 68.00 | 0.03 | 0.03 | -0.08 | 0 | 0.00 | 0.04 | 0.06 | 0 | 0.00 | 0.24 | 0.25 | 0.23 | 0 | 0.23 | 54,000 | 4.15조원 | 2,025 | 4 | 830억원 | 934억원 | 0.13 | 376억원 | 512억원 | 0.36 | 3,368억원 | 1,706억원 | 1,320억원 | 31.47 | 7.53 | 12.33 | in_target_cohort | scored_in_target_cohort | 600 | 32백만원 | 420 | 23백만원 | 180 | 10백만원 | 0 | 0원 | 5.02 |

## 6. REVIEW 종목 요약표

| ticker | name | action | reason | industry_code | industry_name | score | score_adj | hold_bonus_applied | score_rank | op_acc2 | op_acc2_log1p | contrib_op_log | op_growth_streak2 | contrib_op_streak | rev_acc2 | contrib_rev | rev_growth_streak2 | contrib_rev_streak | debt_log | contrib_debt | cfo_to_assets | cfo_isnull | score_rebuilt | price | mcap | year | quarter | revenue_prev_q | revenue_cur_q | revenue_qoq | op_prev_q | op_cur_q | op_qoq | revenue | op_income | net_income | per | pbr | psr | cohort_status | score_availability_reason | planned_qty | planned_value | day1_qty | day1_value | day2_qty | day2_value | day3_qty | day3_value | est_slippage_bps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 041920 | 메디아나 | HOLD | 기존 보유 상위 점수 유지 (rank=3) | 032701 | 의료용 기기 제조업 | 3.40 | 3.90 | 0.5 | 3.00 | 4.41 | 1.69 | 2.27 | 1 | 0.15 | 0.41 | 0.60 | 0 | 0.00 | 0.08 | 0.38 | 0.04 | 0 | 3.40 | 19,900 | 4,798억원 | 2,025 | 4 | 150억원 | 192억원 | 0.28 | 16억원 | 19억원 | 0.19 | 649억원 | 60억원 | 53억원 | 90.97 | 3.54 | 7.39 | in_target_cohort | scored_in_target_cohort | 207 | 4백만원 | 83 | 2백만원 | 62 | 1백만원 | 62 | 1백만원 | 5.01 |
| 042000 | 카페24 | HOLD | 전략 유지 (rank=9) | 106301 | 자료처리, 호스팅, 포털 및 기타 인터넷 정보매개 서비스업 | 2.79 | 3.29 | 0.5 | 9.00 | 11.55 | 2.53 | 2.80 | 0 | 0.00 | -0.05 | -0.10 | 0 | 0.00 | 0.45 | 0.09 | 0.20 | 0 | 2.79 | 27,800 | 8,234억원 | 2,025 | 4 | 768억원 | 873억원 | 0.14 | 100억원 | 130억원 | 0.29 | 3,148억원 | 402억원 | 391억원 | 21.08 | 3.10 | 2.62 | in_target_cohort | scored_in_target_cohort | 150 | 4백만원 | 60 | 2백만원 | 45 | 1백만원 | 45 | 1백만원 | 5.01 |
| 060280 | 큐렉소 | HOLD | 기존 보유 상위 점수 유지 (rank=2) | 074708 | 기타 상품 전문 소매업 | 3.63 | 4.13 | 0.5 | 2.00 | 4.72 | 1.74 | 2.33 | 1 | 0.15 | 0.58 | 0.70 | 1 | 0.05 | 0.05 | 0.40 | -0.11 | 0 | 3.63 | 17,460 | 7,166억원 | 2,025 | 4 | 172억원 | 208억원 | 0.21 | 3억원 | 14억원 | 3.05 | 745억원 | 24억원 | 28억원 | 258.56 | 7.45 | 9.61 | in_target_cohort | scored_in_target_cohort | 234 | 4백만원 | 94 | 2백만원 | 70 | 1백만원 | 70 | 1백만원 | 5.00 |
| 131290 | 티에스이 | HOLD | 전략 유지 (rank=10) | 032702 | 측정, 시험, 항해, 제어 및 기타 정밀기기 제조업; 광학기기 제외 | 2.73 | 3.23 | 0.5 | 10.00 | 18.00 | 2.94 | 2.80 | 0 | 0.00 | -0.16 | -0.30 | 0 | 0.00 | 0.27 | 0.23 | 0.08 | 0 | 2.73 | 110,300 | 9,623억원 | 2,025 | 4 | 1,044억원 | 1,239억원 | 0.19 | 115억원 | 198억원 | 0.72 | 4,289억원 | 492억원 | 400억원 | 24.03 | 2.07 | 2.24 | in_target_cohort | scored_in_target_cohort | 37 | 4백만원 | 15 | 2백만원 | 11 | 1백만원 | 11 | 1백만원 | 5.00 |
| 171090 | 선익시스템 | HOLD | 전략 유지 (rank=6) | 032902 | 특수 목적용 기계 제조업 | 3.19 | 3.69 | 0.5 | 6.00 | 16.05 | 2.84 | 2.80 | 0 | 0.00 | 2.76 | 0.70 | 0 | 0.00 | 0.93 | -0.31 | 0.27 | 0 | 3.19 | 92,800 | 9,494억원 | 2,025 | 4 | 888억원 | 2,210억원 | 1.49 | 198억원 | 536억원 | 1.71 | 5,158억원 | 1,115억원 | 965억원 | 9.84 | 6.60 | 1.84 | in_target_cohort | scored_in_target_cohort | 43 | 4백만원 | 17 | 2백만원 | 13 | 1백만원 | 13 | 1백만원 | 5.00 |
| 218410 | RFHIC | HOLD | 기존 보유 상위 점수 유지 (rank=1) | 032604 | 통신 및 방송 장비 제조업 | 3.69 | 4.19 | 0.5 | 1.00 | 15.14 | 2.78 | 2.80 | 0 | 0.00 | 0.59 | 0.70 | 0 | 0.00 | 0.32 | 0.19 | 0.05 | 0 | 3.69 | 82,400 | 1.33조원 | 2,025 | 4 | 405억원 | 688억원 | 0.70 | 74억원 | 115억원 | 0.56 | 1,858억원 | 309억원 | 355억원 | 37.52 | 3.36 | 7.17 | in_target_cohort | scored_in_target_cohort | 47 | 4백만원 | 19 | 2백만원 | 14 | 1백만원 | 14 | 1백만원 | 5.00 |
| 425420 | 티에프이 | HOLD | 전략 유지 (rank=5) | 032602 | 전자부품 제조업 | 3.29 | 3.79 | 0.5 | 5.00 | 3.86 | 1.58 | 2.16 | 1 | 0.15 | 0.60 | 0.70 | 1 | 0.05 | 0.27 | 0.23 | 0.09 | 0 | 3.29 | 69,000 | 4,991억원 | 2,025 | 4 | 272억원 | 374억원 | 0.38 | 48억원 | 73억원 | 0.52 | 1,117억원 | 191억원 | 181억원 | 27.56 | 4.57 | 4.47 | in_target_cohort | scored_in_target_cohort | 57 | 4백만원 | 23 | 2백만원 | 17 | 1백만원 | 17 | 1백만원 | 5.00 |

## 7. BUY 종목 상세

### 고영 (098460)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=4)
- 보고서 SCORE: **3.2876** / rank 4
- 조정 SCORE: **3.2876**
- holding bonus: 0.0000
- 재구성 SCORE: **3.2876**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 032902
- 업종명: 특수 목적용 기계 제조업
- 현재가: 27,300
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
- contrib_op_log: 2.3878
- contrib_op_streak: 0.1500
- contrib_rev: 0.4151
- contrib_rev_streak: 0.0500
- contrib_debt: 0.2847

멀티플:
- PER: 144.46
- PBR: 6.36
- PSR: 9.16
- EV/EBIT: 127.26

실행 계획:
- 총 목표 주문수량: 260주
- 총 목표 주문금액: 7백만원
- Day1: 104주 / 3백만원
- Day2: 78주 / 2백만원
- Day3: 78주 / 2백만원
- 예상 슬리피지: 5.001 bps

### SNT에너지 (100840)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=7)
- 보고서 SCORE: **2.9832** / rank 7
- 조정 SCORE: **2.9832**
- holding bonus: 0.0000
- 재구성 SCORE: **2.9832**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 032901
- 업종명: 일반 목적용 기계 제조업
- 현재가: 48,200
- 시가총액: 9,803억원

재무:
- 매출(TTM): 6,061억원
- 영업이익(TTM): 1,113억원
- 순이익(TTM): 844억원
- 영업현금흐름(CFO): 1,030억원

팩터:
- OpIncome_acc2: 3.9349
- OpIncome_acc2_log1p: 1.5963
- op_growth_streak2: 0.0000
- Revenue_acc2: 1.1458
- rev_growth_streak2: 1.0000
- Debt_to_Equity_log: 0.4807
- Quality_CFO_to_Assets: 0.1746
- CFO_isnull: 0

스코어 기여도:
- contrib_op_log: 2.1761
- contrib_op_streak: 0.0000
- contrib_rev: 0.7000
- contrib_rev_streak: 0.0500
- contrib_debt: 0.0571

멀티플:
- PER: 11.62
- PBR: 2.69
- PSR: 1.62
- EV/EBIT: 10.83

실행 계획:
- 총 목표 주문수량: 147주
- 총 목표 주문금액: 7백만원
- Day1: 59주 / 3백만원
- Day2: 44주 / 2백만원
- Day3: 44주 / 2백만원
- 예상 슬리피지: 5.003 bps

### 대덕전자 (353200)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=8)
- 보고서 SCORE: **2.9519** / rank 8
- 조정 SCORE: **2.9519**
- holding bonus: 0.0000
- 재구성 SCORE: **2.9519**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 032602
- 업종명: 전자부품 제조업
- 현재가: 85,400
- 시가총액: 2.84조원

재무:
- 매출(TTM): 1.07조원
- 영업이익(TTM): 491억원
- 순이익(TTM): 476억원
- 영업현금흐름(CFO): 716억원

팩터:
- OpIncome_acc2: 3.8829
- OpIncome_acc2_log1p: 1.5857
- op_growth_streak2: 1.0000
- Revenue_acc2: 0.2133
- rev_growth_streak2: 1.0000
- Debt_to_Equity_log: 0.2722
- Quality_CFO_to_Assets: 0.0607
- CFO_isnull: 0

스코어 기여도:
- contrib_op_log: 2.1651
- contrib_op_streak: 0.1500
- contrib_rev: 0.3592
- contrib_rev_streak: 0.0500
- contrib_debt: 0.2276

멀티플:
- PER: 59.69
- PBR: 3.17
- PSR: 2.67
- EV/EBIT: 63.64

실행 계획:
- 총 목표 주문수량: 83주
- 총 목표 주문금액: 7백만원
- Day1: 33주 / 3백만원
- Day2: 25주 / 2백만원
- Day3: 25주 / 2백만원
- 예상 슬리피지: 5.001 bps

## 8. SELL 종목 상세

### 삼성전자 (005930)

- 액션: **SELL**
- 사유: 전략 탈락 (universe_rank=144)
- 보고서 SCORE: **-1.9342** / rank 144
- 조정 SCORE: **-1.4342**
- holding bonus: 0.5000
- 재구성 SCORE: **-1.9342**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 032604
- 업종명: 통신 및 방송 장비 제조업
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 179,700
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
- contrib_op_log: -2.2622
- contrib_op_streak: 0.1500
- contrib_rev: -0.1079
- contrib_rev_streak: 0.0500
- contrib_debt: 0.2360

멀티플:
- PER: 23.93
- PBR: 2.48
- PSR: 3.24
- EV/EBIT: 27.81

실행 계획:
- 총 목표 주문수량: 100주
- 총 목표 주문금액: 18백만원
- Day1: 70주 / 13백만원
- Day2: 30주 / 5백만원
- Day3: 0주 / 0원
- 예상 슬리피지: 5.000 bps

### 클래시스 (214150)

- 액션: **SELL**
- 사유: 전략 탈락 (universe_rank=68)
- 보고서 SCORE: **0.2339** / rank 68
- 조정 SCORE: **0.7339**
- holding bonus: 0.5000
- 재구성 SCORE: **0.2339**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 032701
- 업종명: 의료용 기기 제조업
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 54,000
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
- contrib_op_log: -0.0767
- contrib_op_streak: 0.0000
- contrib_rev: 0.0606
- contrib_rev_streak: 0.0000
- contrib_debt: 0.2500

멀티플:
- PER: 31.47
- PBR: 7.53
- PSR: 12.33
- EV/EBIT: 25.24

실행 계획:
- 총 목표 주문수량: 600주
- 총 목표 주문금액: 32백만원
- Day1: 420주 / 23백만원
- Day2: 180주 / 10백만원
- Day3: 0주 / 0원
- 예상 슬리피지: 5.019 bps

## 9. REVIEW 종목 상세

### 메디아나 (041920)

- 액션: **HOLD**
- 사유: 기존 보유 상위 점수 유지 (rank=3)
- 보고서 SCORE: **3.3990** / rank 3
- 조정 SCORE: **3.8990**
- holding bonus: 0.5000
- 재구성 SCORE: **3.3990**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 032701
- 업종명: 의료용 기기 제조업
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 19,900
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
- contrib_op_log: 2.2703
- contrib_op_streak: 0.1500
- contrib_rev: 0.6015
- contrib_rev_streak: 0.0000
- contrib_debt: 0.3772

멀티플:
- PER: 90.97
- PBR: 3.54
- PSR: 7.39
- EV/EBIT: 82.56

실행 계획:
- 총 목표 주문수량: 207주
- 총 목표 주문금액: 4백만원
- Day1: 83주 / 2백만원
- Day2: 62주 / 1백만원
- Day3: 62주 / 1백만원
- 예상 슬리피지: 5.006 bps

### 카페24 (042000)

- 액션: **HOLD**
- 사유: 전략 유지 (rank=9)
- 보고서 SCORE: **2.7885** / rank 9
- 조정 SCORE: **3.2885**
- holding bonus: 0.5000
- 재구성 SCORE: **2.7885**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 106301
- 업종명: 자료처리, 호스팅, 포털 및 기타 인터넷 정보매개 서비스업
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 27,800
- 시가총액: 8,234억원

재무:
- 매출(TTM): 3,148억원
- 영업이익(TTM): 402억원
- 순이익(TTM): 391억원
- 영업현금흐름(CFO): 826억원

팩터:
- OpIncome_acc2: 11.5467
- OpIncome_acc2_log1p: 2.5295
- op_growth_streak2: 0.0000
- Revenue_acc2: -0.0470
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.4465
- Quality_CFO_to_Assets: 0.1986
- CFO_isnull: 0

스코어 기여도:
- contrib_op_log: 2.8000
- contrib_op_streak: 0.0000
- contrib_rev: -0.0965
- contrib_rev_streak: 0.0000
- contrib_debt: 0.0850

멀티플:
- PER: 21.08
- PBR: 3.10
- PSR: 2.62
- EV/EBIT: 24.19

실행 계획:
- 총 목표 주문수량: 150주
- 총 목표 주문금액: 4백만원
- Day1: 60주 / 2백만원
- Day2: 45주 / 1백만원
- Day3: 45주 / 1백만원
- 예상 슬리피지: 5.012 bps

### 큐렉소 (060280)

- 액션: **HOLD**
- 사유: 기존 보유 상위 점수 유지 (rank=2)
- 보고서 SCORE: **3.6266** / rank 2
- 조정 SCORE: **4.1266**
- holding bonus: 0.5000
- 재구성 SCORE: **3.6266**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 074708
- 업종명: 기타 상품 전문 소매업
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 17,460
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
- contrib_op_log: 2.3289
- contrib_op_streak: 0.1500
- contrib_rev: 0.7000
- contrib_rev_streak: 0.0500
- contrib_debt: 0.3977

멀티플:
- PER: 258.56
- PBR: 7.45
- PSR: 9.61
- EV/EBIT: 305.49

실행 계획:
- 총 목표 주문수량: 234주
- 총 목표 주문금액: 4백만원
- Day1: 94주 / 2백만원
- Day2: 70주 / 1백만원
- Day3: 70주 / 1백만원
- 예상 슬리피지: 5.002 bps

### 티에스이 (131290)

- 액션: **HOLD**
- 사유: 전략 유지 (rank=10)
- 보고서 SCORE: **2.7282** / rank 10
- 조정 SCORE: **3.2282**
- holding bonus: 0.5000
- 재구성 SCORE: **2.7282**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 032702
- 업종명: 측정, 시험, 항해, 제어 및 기타 정밀기기 제조업; 광학기기 제외
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 110,300
- 시가총액: 9,623억원

재무:
- 매출(TTM): 4,289억원
- 영업이익(TTM): 492억원
- 순이익(TTM): 400억원
- 영업현금흐름(CFO): 486억원

팩터:
- OpIncome_acc2: 17.9958
- OpIncome_acc2_log1p: 2.9442
- op_growth_streak2: 0.0000
- Revenue_acc2: -0.1647
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.2691
- Quality_CFO_to_Assets: 0.0799
- CFO_isnull: 0

스코어 기여도:
- contrib_op_log: 2.8000
- contrib_op_streak: 0.0000
- contrib_rev: -0.3019
- contrib_rev_streak: 0.0000
- contrib_debt: 0.2301

멀티플:
- PER: 24.03
- PBR: 2.07
- PSR: 2.24
- EV/EBIT: 22.49

실행 계획:
- 총 목표 주문수량: 37주
- 총 목표 주문금액: 4백만원
- Day1: 15주 / 2백만원
- Day2: 11주 / 1백만원
- Day3: 11주 / 1백만원
- 예상 슬리피지: 5.003 bps

### 선익시스템 (171090)

- 액션: **HOLD**
- 사유: 전략 유지 (rank=6)
- 보고서 SCORE: **3.1872** / rank 6
- 조정 SCORE: **3.6872**
- holding bonus: 0.5000
- 재구성 SCORE: **3.1872**
- SCORE 차이(보고서-재구성): -0.0000
- industry_code: 032902
- 업종명: 특수 목적용 기계 제조업
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 92,800
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
- contrib_debt: -0.3128

멀티플:
- PER: 9.84
- PBR: 6.60
- PSR: 1.84
- EV/EBIT: 10.50

실행 계획:
- 총 목표 주문수량: 43주
- 총 목표 주문금액: 4백만원
- Day1: 17주 / 2백만원
- Day2: 13주 / 1백만원
- Day3: 13주 / 1백만원
- 예상 슬리피지: 5.002 bps

### RFHIC (218410)

- 액션: **HOLD**
- 사유: 기존 보유 상위 점수 유지 (rank=1)
- 보고서 SCORE: **3.6882** / rank 1
- 조정 SCORE: **4.1882**
- holding bonus: 0.5000
- 재구성 SCORE: **3.6882**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 032604
- 업종명: 통신 및 방송 장비 제조업
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 82,400
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
- contrib_debt: 0.1882

멀티플:
- PER: 37.52
- PBR: 3.36
- PSR: 7.17
- EV/EBIT: 47.97

실행 계획:
- 총 목표 주문수량: 47주
- 총 목표 주문금액: 4백만원
- Day1: 19주 / 2백만원
- Day2: 14주 / 1백만원
- Day3: 14주 / 1백만원
- 예상 슬리피지: 5.001 bps

### 티에프이 (425420)

- 액션: **HOLD**
- 사유: 전략 유지 (rank=5)
- 보고서 SCORE: **3.2871** / rank 5
- 조정 SCORE: **3.7871**
- holding bonus: 0.5000
- 재구성 SCORE: **3.2871**
- SCORE 차이(보고서-재구성): 0.0000
- industry_code: 032602
- 업종명: 전자부품 제조업
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 69,000
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
- contrib_op_log: 2.1611
- contrib_op_streak: 0.1500
- contrib_rev: 0.7000
- contrib_rev_streak: 0.0500
- contrib_debt: 0.2260

멀티플:
- PER: 27.56
- PBR: 4.57
- PSR: 4.47
- EV/EBIT: 28.00

실행 계획:
- 총 목표 주문수량: 57주
- 총 목표 주문금액: 4백만원
- Day1: 23주 / 2백만원
- Day2: 17주 / 1백만원
- Day3: 17주 / 1백만원
- 예상 슬리피지: 5.002 bps
