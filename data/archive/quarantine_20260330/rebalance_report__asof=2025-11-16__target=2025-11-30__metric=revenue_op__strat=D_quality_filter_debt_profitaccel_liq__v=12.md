# 리밸런싱 보고서

본 보고서는 한국 주식 팩터 기반 분기 리밸런싱 전략의 산출물입니다.
개별 종목의 절대적 우열 판단이 아니라, 동일 시점 유니버스 내 상대 점수 비교 결과를 반영합니다.
본 보고서는 투자판단 보조를 위한 정량 리밸런싱 자료이며, 최종 주문 집행 전 유동성·이벤트·체결 가능성을 추가 점검합니다.

- 기준일(asof): **2025-11-16**
- 목표 리밸런싱일(target): **2025-11-30**
- 전략명: **D_quality_filter_debt_profitaccel_liq**
- 전략 설명: **이익 가속도 중심 + 부채 통제 + 유동성 필터(거래대금+시총)**

## 1. 유의사항

- 본 전략은 이익 가속도와 재무 건전성 중심의 상대평가 전략입니다.
- 대형 우량주라도 해당 시점의 점수 경쟁에서 제외될 수 있습니다.
- 반대로 적자 기업이라도 이익 개선 가속이 강하면 편입될 수 있습니다.
- BUY/SELL는 절대적 우열이 아니라 이번 분기 기준 상대 점수 재정렬 결과입니다.

## 2. 요약

- BUY 종목 수: **10**
- SELL 종목 수: **3**
- REVIEW 종목 수: **0**

## 3. SCORE 계산 방식

### 3.1 점수 식

```text
Score_raw =  1.00 × z(OpIncome_acc2)
           + 0.25 × z(Revenue_acc2)
           - 0.35 × z(Debt_to_Equity_log)
```

- 스코어링 방식: Cross-sectional zscore 기준 조합 / clip_z=3.0 / holding_bonus=0.2 / robust_z=true

### 3.2 계산 절차

- 1) 리밸런싱 시점 유니버스를 구성한다.
- 2) 전략에서 사용하는 팩터를 계산한다.
- 3) 연속형 팩터는 동일 시점 유니버스 내 cross-sectional z-score로 표준화한다.
- 4) extreme clipping 설정이 있으면 표준화 점수에 상하한을 적용한다.
- 5) 가중합으로 Score_raw를 계산한다.
- 6) holding bonus 설정이 있고 현재 보유 종목이면 Score_adj에 보너스를 더한다.
- 7) 전략 필터를 적용한 뒤 최종 점수 순으로 상위 종목을 편입한다.

### 3.3 핵심 팩터 설명

- **OpIncome_acc2**: 최근 구간에서 영업이익 증가 속도가 얼마나 가속되었는지를 나타내는 팩터. 값이 높을수록 영업이익 개선 속도가 빨라진 것으로 해석한다.
- **Revenue_acc2**: 최근 구간에서 매출 증가 속도가 얼마나 가속되었는지를 나타내는 팩터. 매출 모멘텀의 가속 여부를 본다.
- **Debt_to_Equity_log**: 부채/자본 비율을 로그 변환한 값. 높을수록 재무 레버리지가 큰 상태로 보고 감점 요인으로 사용한다.

### 3.4 포트폴리오 종목별 SCORE 계산 표시

- 각 종목에 대해 contrib_op / contrib_rev / contrib_debt / contrib_cfo / contrib_missing를 별도로 출력합니다.
- score_rebuilt는 가능한 경우 score_latest_rebalance의 실제 factor contribution을 합산한 값입니다.
- exact scoring source를 찾지 못한 경우에만 feature 기반 근사 재구성을 사용합니다.

### 3.5 필터 / 제약조건

- Debt_to_Equity_log <= 2.398
- traded_value >= 1000000000
- mcap >= 100000000000

## 4. BUY 종목 요약표

| ticker | name | action | reason | score | score_adj | hold_bonus_applied | score_rank | score_adj_rank | industry4 | cohort_status | score_availability_reason | price | mcap | year | quarter | revenue | op_income | net_income | revenue_prev_q | revenue_cur_q | revenue_qoq | op_prev_q | op_cur_q | op_qoq | op_acc2 | rev_acc2 | debt_log | cfo_to_assets | cfo_isnull | per | pbr | psr | contrib_op | contrib_rev | contrib_debt | score_rebuilt | planned_qty | planned_value | day1_qty | day1_value | day2_qty | day2_value | day3_qty | day3_value | est_slippage_bps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 005690 | 파미셀 | BUY | 전략 신규 편입 (rank=1) | 4.07 | 4.074332222001554 | 0.0 | 1.00 | 1.0 | 211.0 | - | - | 17,470 | 9,813억원 | 2,025 | 3 | 1,009억원 | 278억원 | 288억원 | 268억원 | 256억원 | -0.04 | 82억원 | 81억원 | -0.00 | 17.51 | 0.75 | 0.16 | 0.18 | 0 | 34.11 | 8.89 | 9.72 | 3.00 | 0.75 | 0.32 | 4.07 | 570 | 10백만원 | 228 | 4백만원 | 171 | 3백만원 | 171 | 3백만원 | 5.00 |
| 095610 | 테스 | BUY | 전략 신규 편입 (rank=2) | 4.04 | 4.041310994137562 | 0.0 | 2.00 | 2.0 | 29271.0 | - | - | 41,700 | 1.31조원 | 2,025 | 3 | 3,207억원 | 671억원 | 672억원 | 821억원 | 676억원 | -0.18 | 204억원 | 84억원 | -0.59 | 6.72 | 0.87 | 0.21 | 0.17 | 0 | 19.44 | 3.52 | 4.07 | 3.00 | 0.75 | 0.29 | 4.04 | 237 | 10백만원 | 95 | 4백만원 | 71 | 3백만원 | 71 | 3백만원 | 5.01 |
| 060280 | 큐렉소 | BUY | 전략 신규 편입 (rank=3) | 3.98 | 3.978768792842944 | 0.0 | 3.00 | 3.0 | 47812.0 | - | - | 10,670 | 7,166억원 | 2,025 | 3 | 686억원 | -5억원 | -44억원 | 168억원 | 172억원 | 0.03 | -2억원 | 3억원 | 2.66 | 2.09 | 0.40 | 0.09 | -0.07 | 0 | - | 7.60 | 10.45 | 2.99 | 0.61 | 0.38 | 3.98 | 936 | 10백만원 | 374 | 4백만원 | 281 | 3백만원 | 281 | 3백만원 | 5.01 |
| 302440 | SK바이오사이언스 | BUY | 전략 신규 편입 (rank=4) | 3.94 | 3.939420576528376 | 0.0 | 4.00 | 4.0 | 211.0 | - | - | 54,600 | 3.86조원 | 2,025 | 3 | 6,240억원 | -1,227억원 | -24억원 | 1,619억원 | 1,508억원 | -0.07 | -374억원 | -194억원 | 0.48 | 19.92 | 2.62 | 0.34 | 0.02 | 0 | - | 1.86 | 6.18 | 3.00 | 0.75 | 0.19 | 3.94 | 181 | 10백만원 | 73 | 4백만원 | 54 | 3백만원 | 54 | 3백만원 | 5.01 |
| 450080 | 에코프로머티 | BUY | 전략 신규 편입 (rank=5) | 3.92 | 3.9207299690376978 | 0.0 | 5.00 | 5.0 | 28202.0 | - | - | 62,900 | 4.59조원 | 2,025 | 3 | 3,654억원 | -783억원 | 1,151억원 | 781억원 | 632억원 | -0.19 | -288억원 | -251억원 | 0.13 | 2.58 | 0.63 | 0.37 | 0.03 | 0 | 39.86 | 3.66 | 12.56 | 3.00 | 0.75 | 0.17 | 3.92 | 157 | 10백만원 | 63 | 4백만원 | 47 | 3백만원 | 47 | 3백만원 | 5.00 |
| 039030 | 이오테크닉스 | BUY | 전략 신규 편입 (rank=6) | 3.82 | 3.823555736317304 | 0.0 | 6.00 | 6.0 | 29271.0 | - | - | 277,000 | 4.50조원 | 2,025 | 3 | 3,703억원 | 750억원 | 570억원 | 943억원 | 1,005억원 | 0.07 | 258억원 | 260억원 | 0.01 | 2.04 | 0.36 | 0.12 | 0.13 | 0 | 78.85 | 6.73 | 12.14 | 2.91 | 0.55 | 0.36 | 3.82 | 34 | 9백만원 | 14 | 4백만원 | 10 | 3백만원 | 10 | 3백만원 | 5.00 |
| 100840 | SNT에너지 | BUY | 전략 신규 편입 (rank=7) | 3.79 | 3.79042436788437 | 0.0 | 7.00 | 7.0 | 29176.0 | - | - | 40,300 | 9,803억원 | 2,025 | 3 | 5,000억원 | 736억원 | 645억원 | 1,407억원 | 1,483억원 | 0.05 | 274억원 | 243억원 | -0.11 | 2.28 | 0.80 | 0.54 | 0.18 | 0 | 15.20 | 2.96 | 1.96 | 3.00 | 0.75 | 0.04 | 3.79 | 247 | 10백만원 | 99 | 4백만원 | 74 | 3백만원 | 74 | 3백만원 | 5.01 |
| 206650 | 유바이오로직스 | BUY | 전략 신규 편입 (rank=8) | 3.75 | 3.7512846344031687 | 0.0 | 8.00 | 8.0 | 212.0 | - | - | 13,990 | 4,918억원 | 2,025 | 3 | 1,537억원 | 637억원 | 491억원 | 362억원 | 411억원 | 0.13 | 112억원 | 189억원 | 0.68 | 7.96 | 0.35 | 0.29 | 0.32 | 0 | 10.02 | 2.83 | 3.20 | 3.00 | 0.52 | 0.23 | 3.75 | 713 | 10백만원 | 285 | 4백만원 | 214 | 3백만원 | 214 | 3백만원 | 5.03 |
| 218410 | RFHIC | BUY | 전략 신규 편입 (rank=9) | 3.72 | 3.7179640598406585 | 0.0 | 9.00 | 9.0 | 264.0 | - | - | 30,400 | 1.33조원 | 2,025 | 3 | 1,557억원 | 219억원 | 144억원 | 446억원 | 405억원 | -0.09 | 83억원 | 74억원 | -0.11 | 4.00 | 0.33 | 0.30 | 0.09 | 0 | 92.80 | 3.57 | 8.56 | 3.00 | 0.50 | 0.22 | 3.72 | 327 | 10백만원 | 131 | 4백만원 | 98 | 3백만원 | 98 | 3백만원 | 5.01 |
| 036810 | 에프에스티 | BUY | 전략 신규 편입 (rank=10) | 3.60 | 3.603465125564517 | 0.0 | 10.00 | 10.0 | 2629.0 | - | - | 32,676 | 9,312억원 | 2,025 | 3 | 2,881억원 | 69억원 | -19억원 | 760억원 | 655억원 | -0.14 | 17억원 | -19억원 | -2.08 | 22.84 | 0.54 | 0.79 | 0.05 | 0 | - | 3.94 | 3.23 | 3.00 | 0.75 | -0.15 | 3.60 | 304 | 10백만원 | 122 | 4백만원 | 91 | 3백만원 | 91 | 3백만원 | 5.01 |

## 5. SELL 종목 요약표

| ticker | name | action | reason | score | score_adj | hold_bonus_applied | score_rank | score_adj_rank | industry4 | cohort_status | score_availability_reason | price | mcap | year | quarter | revenue | op_income | net_income | revenue_prev_q | revenue_cur_q | revenue_qoq | op_prev_q | op_cur_q | op_qoq | op_acc2 | rev_acc2 | debt_log | cfo_to_assets | cfo_isnull | per | pbr | psr | contrib_op | contrib_rev | contrib_debt | score_rebuilt | planned_qty | planned_value | day1_qty | day1_value | day2_qty | day2_value | day3_qty | day3_value | est_slippage_bps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 005930 | 삼성전자 | SELL | 전략 탈락 (universe_rank=423) | -2.85 | -2.646052781185924 | 0.2 | 423.00 | 419.0 | 264.0 | in_target_cohort | scored_in_target_cohort | 97,200 | 1,081.72조원 | 2,025 | 3 | 315.56조원 | 30.02조원 | 33.32조원 | 74.57조원 | 86.06조원 | 0.15 | 4.68조원 | 12.17조원 | 1.60 | -2.58 | -0.04 | 0.24 | 0.15 | 0 | 32.47 | 2.62 | 3.43 | -3.00 | -0.12 | 0.27 | -2.85 | - | - | - | - | - | - | - | - | - |
| 058470 | 리노공업 | SELL | 전략 탈락 (universe_rank=76) | 1.59 | 1.7887687752954384 | 0.2 | 76.00 | 71.0 | 2629.0 | in_target_cohort | scored_in_target_cohort | 56,500 | 1.49조원 | 2,025 | 3 | 3,712억원 | 1,737억원 | 1,507억원 | 1,125억원 | 968억원 | -0.14 | 534억원 | 483억원 | -0.10 | 0.30 | 0.45 | 0.10 | 0.23 | 0 | 9.86 | 2.16 | 4.00 | 0.52 | 0.70 | 0.37 | 1.59 | - | - | - | - | - | - | - | - | - |
| 214150 | 클래시스 | SELL | 전략 탈락 (universe_rank=106) | 0.96 | 1.1576203752957266 | 0.2 | 106.00 | 98.0 | 271.0 | in_target_cohort | scored_in_target_cohort | 56,100 | 4.15조원 | 2,025 | 3 | 3,178억원 | 1,552억원 | 1,177억원 | 833억원 | 830억원 | -0.00 | 430억원 | 376억원 | -0.13 | 0.18 | 0.23 | 0.23 | 0.17 | 0 | 35.29 | 8.01 | 13.07 | 0.35 | 0.34 | 0.27 | 0.96 | - | - | - | - | - | - | - | - | - |

## 6. REVIEW 종목 요약표

_없음_

## 7. BUY 종목 상세

### 파미셀 (005690)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=1)
- 보고서 SCORE: **4.0743** / rank 1
- 조정 SCORE: **4.0743**
- holding bonus: 0.0000
- 재구성 SCORE: **4.0743**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 211
- 현재가: 17,470
- 시가총액: 9,813억원

재무:
- 매출(TTM): 1,009억원
- 영업이익(TTM): 278억원
- 순이익(TTM): 288억원
- 영업현금흐름(CFO): 239억원

팩터:
- OpIncome_acc2: 17.5104
- Revenue_acc2: 0.7533
- Debt_to_Equity_log: 0.1634
- Quality_CFO_to_Assets: 0.1837
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0000
- contrib_rev: 0.7500
- contrib_debt: 0.3243
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 34.11
- PBR: 8.89
- PSR: 9.72
- EV/EBIT: 35.97

실행 계획:
- 총 목표 주문수량: 570주
- 총 목표 주문금액: 10백만원
- Day1: 228주 / 4백만원
- Day2: 171주 / 3백만원
- Day3: 171주 / 3백만원
- 예상 슬리피지: 5.001 bps

### 테스 (095610)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=2)
- 보고서 SCORE: **4.0413** / rank 2
- 조정 SCORE: **4.0413**
- holding bonus: 0.0000
- 재구성 SCORE: **4.0413**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 29271
- 현재가: 41,700
- 시가총액: 1.31조원

재무:
- 매출(TTM): 3,207억원
- 영업이익(TTM): 671억원
- 순이익(TTM): 672억원
- 영업현금흐름(CFO): 768억원

팩터:
- OpIncome_acc2: 6.7158
- Revenue_acc2: 0.8727
- Debt_to_Equity_log: 0.2073
- Quality_CFO_to_Assets: 0.1685
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0000
- contrib_rev: 0.7500
- contrib_debt: 0.2913
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 19.44
- PBR: 3.52
- PSR: 4.07
- EV/EBIT: 20.75

실행 계획:
- 총 목표 주문수량: 237주
- 총 목표 주문금액: 10백만원
- Day1: 95주 / 4백만원
- Day2: 71주 / 3백만원
- Day3: 71주 / 3백만원
- 예상 슬리피지: 5.006 bps

### 큐렉소 (060280)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=3)
- 보고서 SCORE: **3.9788** / rank 3
- 조정 SCORE: **3.9788**
- holding bonus: 0.0000
- 재구성 SCORE: **3.9788**
- SCORE 차이(보고서-재구성): -0.0000
- industry4: 47812
- 현재가: 10,670
- 시가총액: 7,166억원

재무:
- 매출(TTM): 686억원
- 영업이익(TTM): -5억원
- 순이익(TTM): -44억원
- 영업현금흐름(CFO): -71억원

팩터:
- OpIncome_acc2: 2.0928
- Revenue_acc2: 0.4025
- Debt_to_Equity_log: 0.0946
- Quality_CFO_to_Assets: -0.0686
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 2.9884
- contrib_rev: 0.6143
- contrib_debt: 0.3761
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PBR: 7.60
- PSR: 10.45

실행 계획:
- 총 목표 주문수량: 936주
- 총 목표 주문금액: 10백만원
- Day1: 374주 / 4백만원
- Day2: 281주 / 3백만원
- Day3: 281주 / 3백만원
- 예상 슬리피지: 5.010 bps

### SK바이오사이언스 (302440)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=4)
- 보고서 SCORE: **3.9394** / rank 4
- 조정 SCORE: **3.9394**
- holding bonus: 0.0000
- 재구성 SCORE: **3.9394**
- SCORE 차이(보고서-재구성): -0.0000
- industry4: 211
- 현재가: 54,600
- 시가총액: 3.86조원

재무:
- 매출(TTM): 6,240억원
- 영업이익(TTM): -1,227억원
- 순이익(TTM): -24억원
- 영업현금흐름(CFO): 659억원

팩터:
- OpIncome_acc2: 19.9237
- Revenue_acc2: 2.6198
- Debt_to_Equity_log: 0.3427
- Quality_CFO_to_Assets: 0.0225
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0000
- contrib_rev: 0.7500
- contrib_debt: 0.1894
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PBR: 1.86
- PSR: 6.18

실행 계획:
- 총 목표 주문수량: 181주
- 총 목표 주문금액: 10백만원
- Day1: 73주 / 4백만원
- Day2: 54주 / 3백만원
- Day3: 54주 / 3백만원
- 예상 슬리피지: 5.006 bps

### 에코프로머티 (450080)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=5)
- 보고서 SCORE: **3.9207** / rank 5
- 조정 SCORE: **3.9207**
- holding bonus: 0.0000
- 재구성 SCORE: **3.9207**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 28202
- 현재가: 62,900
- 시가총액: 4.59조원

재무:
- 매출(TTM): 3,654억원
- 영업이익(TTM): -783억원
- 순이익(TTM): 1,151억원
- 영업현금흐름(CFO): 570억원

팩터:
- OpIncome_acc2: 2.5776
- Revenue_acc2: 0.6332
- Debt_to_Equity_log: 0.3675
- Quality_CFO_to_Assets: 0.0315
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0000
- contrib_rev: 0.7500
- contrib_debt: 0.1707
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 39.86
- PBR: 3.66
- PSR: 12.56

실행 계획:
- 총 목표 주문수량: 157주
- 총 목표 주문금액: 10백만원
- Day1: 63주 / 4백만원
- Day2: 47주 / 3백만원
- Day3: 47주 / 3백만원
- 예상 슬리피지: 5.002 bps

### 이오테크닉스 (039030)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=6)
- 보고서 SCORE: **3.8236** / rank 6
- 조정 SCORE: **3.8236**
- holding bonus: 0.0000
- 재구성 SCORE: **3.8236**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 29271
- 현재가: 277,000
- 시가총액: 4.50조원

재무:
- 매출(TTM): 3,703억원
- 영업이익(TTM): 750억원
- 순이익(TTM): 570억원
- 영업현금흐름(CFO): 1,012억원

팩터:
- OpIncome_acc2: 2.0385
- Revenue_acc2: 0.3638
- Debt_to_Equity_log: 0.1168
- Quality_CFO_to_Assets: 0.1348
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 2.9134
- contrib_rev: 0.5508
- contrib_debt: 0.3594
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 78.85
- PBR: 6.73
- PSR: 12.14
- EV/EBIT: 61.09

실행 계획:
- 총 목표 주문수량: 34주
- 총 목표 주문금액: 9백만원
- Day1: 14주 / 4백만원
- Day2: 10주 / 3백만원
- Day3: 10주 / 3백만원
- 예상 슬리피지: 5.002 bps

### SNT에너지 (100840)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=7)
- 보고서 SCORE: **3.7904** / rank 7
- 조정 SCORE: **3.7904**
- holding bonus: 0.0000
- 재구성 SCORE: **3.7904**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 29176
- 현재가: 40,300
- 시가총액: 9,803억원

재무:
- 매출(TTM): 5,000억원
- 영업이익(TTM): 736억원
- 순이익(TTM): 645억원
- 영업현금흐름(CFO): 1,025억원

팩터:
- OpIncome_acc2: 2.2768
- Revenue_acc2: 0.7983
- Debt_to_Equity_log: 0.5407
- Quality_CFO_to_Assets: 0.1802
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0000
- contrib_rev: 0.7500
- contrib_debt: 0.0404
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 15.20
- PBR: 2.96
- PSR: 1.96
- EV/EBIT: 16.55

실행 계획:
- 총 목표 주문수량: 247주
- 총 목표 주문금액: 10백만원
- Day1: 99주 / 4백만원
- Day2: 74주 / 3백만원
- Day3: 74주 / 3백만원
- 예상 슬리피지: 5.008 bps

### 유바이오로직스 (206650)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=8)
- 보고서 SCORE: **3.7513** / rank 8
- 조정 SCORE: **3.7513**
- holding bonus: 0.0000
- 재구성 SCORE: **3.7513**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 212
- 현재가: 13,990
- 시가총액: 4,918억원

재무:
- 매출(TTM): 1,537억원
- 영업이익(TTM): 637억원
- 순이익(TTM): 491억원
- 영업현금흐름(CFO): 742억원

팩터:
- OpIncome_acc2: 7.9579
- Revenue_acc2: 0.3473
- Debt_to_Equity_log: 0.2920
- Quality_CFO_to_Assets: 0.3191
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0000
- contrib_rev: 0.5237
- contrib_debt: 0.2275
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 10.02
- PBR: 2.83
- PSR: 3.20
- EV/EBIT: 8.65

실행 계획:
- 총 목표 주문수량: 713주
- 총 목표 주문금액: 10백만원
- Day1: 285주 / 4백만원
- Day2: 214주 / 3백만원
- Day3: 214주 / 3백만원
- 예상 슬리피지: 5.030 bps

### RFHIC (218410)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=9)
- 보고서 SCORE: **3.7180** / rank 9
- 조정 SCORE: **3.7180**
- holding bonus: 0.0000
- 재구성 SCORE: **3.7180**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 264
- 현재가: 30,400
- 시가총액: 1.33조원

재무:
- 매출(TTM): 1,557억원
- 영업이익(TTM): 219억원
- 순이익(TTM): 144억원
- 영업현금흐름(CFO): 438억원

팩터:
- OpIncome_acc2: 3.9974
- Revenue_acc2: 0.3302
- Debt_to_Equity_log: 0.2988
- Quality_CFO_to_Assets: 0.0871
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0000
- contrib_rev: 0.4955
- contrib_debt: 0.2224
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 92.80
- PBR: 3.57
- PSR: 8.56
- EV/EBIT: 66.86

실행 계획:
- 총 목표 주문수량: 327주
- 총 목표 주문금액: 10백만원
- Day1: 131주 / 4백만원
- Day2: 98주 / 3백만원
- Day3: 98주 / 3백만원
- 예상 슬리피지: 5.007 bps

### 에프에스티 (036810)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=10)
- 보고서 SCORE: **3.6035** / rank 10
- 조정 SCORE: **3.6035**
- holding bonus: 0.0000
- 재구성 SCORE: **3.6035**
- SCORE 차이(보고서-재구성): -0.0000
- industry4: 2629
- 현재가: 32,676
- 시가총액: 9,312억원

재무:
- 매출(TTM): 2,881억원
- 영업이익(TTM): 69억원
- 순이익(TTM): -19억원
- 영업현금흐름(CFO): 256억원

팩터:
- OpIncome_acc2: 22.8354
- Revenue_acc2: 0.5367
- Debt_to_Equity_log: 0.7891
- Quality_CFO_to_Assets: 0.0492
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0000
- contrib_rev: 0.7500
- contrib_debt: -0.1465
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PBR: 3.94
- PSR: 3.23
- EV/EBIT: 175.47

실행 계획:
- 총 목표 주문수량: 304주
- 총 목표 주문금액: 10백만원
- Day1: 122주 / 4백만원
- Day2: 91주 / 3백만원
- Day3: 91주 / 3백만원
- 예상 슬리피지: 5.007 bps

## 8. SELL 종목 상세

### 삼성전자 (005930)

- 액션: **SELL**
- 사유: 전략 탈락 (universe_rank=423)
- 보고서 SCORE: **-2.8461** / rank 423
- 조정 SCORE: **-2.6461**
- holding bonus: 0.2000
- 재구성 SCORE: **-2.8461**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 264
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 97,200
- 시가총액: 1,081.72조원

재무:
- 매출(TTM): 315.56조원
- 영업이익(TTM): 30.02조원
- 순이익(TTM): 33.32조원
- 영업현금흐름(CFO): 78.54조원

팩터:
- OpIncome_acc2: -2.5773
- Revenue_acc2: -0.0419
- Debt_to_Equity_log: 0.2362
- Quality_CFO_to_Assets: 0.1500
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -3.0000
- contrib_rev: -0.1156
- contrib_debt: 0.2695
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 32.47
- PBR: 2.62
- PSR: 3.43
- EV/EBIT: 39.70

실행 계획:
- 총 목표 주문수량: -주
- 총 목표 주문금액: -
- Day1: -주 / -
- Day2: -주 / -
- Day3: -주 / -
- 예상 슬리피지: - bps

### 리노공업 (058470)

- 액션: **SELL**
- 사유: 전략 탈락 (universe_rank=76)
- 보고서 SCORE: **1.5888** / rank 76
- 조정 SCORE: **1.7888**
- holding bonus: 0.2000
- 재구성 SCORE: **1.5888**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 2629
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 56,500
- 시가총액: 1.49조원

재무:
- 매출(TTM): 3,712억원
- 영업이익(TTM): 1,737억원
- 순이익(TTM): 1,507억원
- 영업현금흐름(CFO): 1,735억원

팩터:
- OpIncome_acc2: 0.3040
- Revenue_acc2: 0.4549
- Debt_to_Equity_log: 0.1005
- Quality_CFO_to_Assets: 0.2275
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 0.5167
- contrib_rev: 0.7005
- contrib_debt: 0.3716
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 9.86
- PBR: 2.16
- PSR: 4.00
- EV/EBIT: 8.98

실행 계획:
- 총 목표 주문수량: -주
- 총 목표 주문금액: -
- Day1: -주 / -
- Day2: -주 / -
- Day3: -주 / -
- 예상 슬리피지: - bps

### 클래시스 (214150)

- 액션: **SELL**
- 사유: 전략 탈락 (universe_rank=106)
- 보고서 SCORE: **0.9576** / rank 106
- 조정 SCORE: **1.1576**
- holding bonus: 0.2000
- 재구성 SCORE: **0.9576**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 271
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 56,100
- 시가총액: 4.15조원

재무:
- 매출(TTM): 3,178억원
- 영업이익(TTM): 1,552억원
- 순이익(TTM): 1,177억원
- 영업현금흐름(CFO): 1,121억원

팩터:
- OpIncome_acc2: 0.1835
- Revenue_acc2: 0.2327
- Debt_to_Equity_log: 0.2331
- Quality_CFO_to_Assets: 0.1713
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 0.3502
- contrib_rev: 0.3355
- contrib_debt: 0.2719
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 35.29
- PBR: 8.01
- PSR: 13.07
- EV/EBIT: 27.64

실행 계획:
- 총 목표 주문수량: -주
- 총 목표 주문금액: -
- Day1: -주 / -
- Day2: -주 / -
- Day3: -주 / -
- 예상 슬리피지: - bps

## 9. REVIEW 종목 상세

_없음_