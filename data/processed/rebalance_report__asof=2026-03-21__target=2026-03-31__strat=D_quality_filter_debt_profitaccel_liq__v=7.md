# 리밸런싱 보고서

본 보고서는 한국 주식 팩터 기반 분기 리밸런싱 전략의 산출물입니다.
개별 종목의 절대적 우열 판단이 아니라, 동일 시점 유니버스 내 상대 점수 비교 결과를 반영합니다.
본 보고서는 투자판단 보조를 위한 정량 리밸런싱 자료이며, 최종 주문 집행 전 유동성·이벤트·체결 가능성을 추가 점검합니다.

- 기준일(asof): **2026-03-21**
- 목표 리밸런싱일(target): **2026-03-31**
- 전략명: **D_quality_filter_debt_profitaccel_liq**
- 전략 설명: **이익 가속도 중심 + 부채 통제 + 약한 현금흐름 품질 + 유동성 필터(거래대금+시총)**

## 1. 유의사항

- 본 전략은 이익 가속도와 재무 건전성 중심의 상대평가 전략입니다.
- 대형 우량주라도 해당 시점의 점수 경쟁에서 제외될 수 있습니다.
- 반대로 적자 기업이라도 이익 개선 가속이 강하면 편입될 수 있습니다.
- BUY/SELL는 절대적 우열이 아니라 이번 분기 기준 상대 점수 재정렬 결과입니다.

## 2. 요약

- BUY 종목 수: **10**
- SELL 종목 수: **3**
- HOLD 종목 수: **0**

## 3. SCORE 계산 방식

### 3.1 점수 식

```text
Score_raw =  1.00 × z(OpIncome_acc2)
           + 0.25 × z(Revenue_acc2)
           - 0.35 × z(Debt_to_Equity_log)
           + 0.10 × z(Quality_CFO_to_Assets)
           - 0.10 × CFO_isnull
```

- 스코어링 방식: Cross-sectional zscore 기준 조합

### 3.2 계산 절차

- 1) 리밸런싱 시점 유니버스를 구성한다.
- 2) 전략에서 사용하는 팩터를 계산한다.
- 3) 연속형 팩터는 동일 시점 유니버스 내 cross-sectional z-score로 표준화한다.
- 4) 가중합으로 Score_raw를 계산한다.
- 5) 전략 필터를 적용한 뒤 최종 점수 순으로 상위 종목을 편입한다.

### 3.3 핵심 팩터 설명

- **OpIncome_acc2**: 최근 구간에서 영업이익 증가 속도가 얼마나 가속되었는지를 나타내는 팩터. 값이 높을수록 영업이익 개선 속도가 빨라진 것으로 해석한다.
- **Revenue_acc2**: 최근 구간에서 매출 증가 속도가 얼마나 가속되었는지를 나타내는 팩터. 매출 모멘텀의 가속 여부를 본다.
- **Debt_to_Equity_log**: 부채/자본 비율을 로그 변환한 값. 높을수록 재무 레버리지가 큰 상태로 보고 감점 요인으로 사용한다.
- **Quality_CFO_to_Assets**: 영업현금흐름을 자산으로 나눈 품질 팩터. 높을수록 이익의 현금화 품질이 좋다고 해석한다.
- **CFO_isnull**: 영업현금흐름 데이터 결측 여부를 나타내는 패널티 더미(0/1). 결측이면 감점한다.

### 3.4 포트폴리오 종목별 SCORE 계산 표시

- 각 종목에 대해 contrib_op / contrib_rev / contrib_debt / contrib_cfo / contrib_missing를 별도로 출력합니다.
- score_rebuilt는 가능한 경우 score_latest_rebalance의 실제 factor contribution을 합산한 값입니다.
- exact scoring source를 찾지 못한 경우에만 feature 기반 근사 재구성을 사용합니다.

### 3.5 필터 / 제약조건

- Debt_to_Equity_log <= 2.398
- traded_value >= 1000000000
- mcap >= 100000000000

## 4. BUY 종목 요약표

| ticker | name | action | reason | score | score_rank | industry4 | cohort_status | score_availability_reason | price | mcap | revenue | op_income | net_income | op_acc2 | rev_acc2 | debt_log | cfo_to_assets | cfo_isnull | per | pbr | psr | contrib_op | contrib_rev | contrib_debt | contrib_cfo | contrib_missing | score_rebuilt | planned_qty | planned_value | day1_qty | day1_value | day2_qty | day2_value | day3_qty | day3_value | est_slippage_bps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 042520 | 한스바이오메드 | BUY | 전략 신규 편입 (rank=1) | 8.44 | 1.00 | - | - | - | 53,000 | 5,789억원 | 677억원 | -259억원 | -286억원 | 914.64 | 0.11 | 1.32 | 0.01 | 0 | - | 17.08 | 8.55 | 8.73 | 0.02 | -0.28 | -0.03 | -0.00 | 8.44 | 187 | 10백만원 | 75 | 4백만원 | 56 | 3백만원 | 56 | 3백만원 | 5.01 |
| 322000 | HD현대에너지솔루션 | BUY | 전략 신규 편입 (rank=2) | 3.40 | 2.00 | - | - | - | 131,000 | 9,117억원 | 3,716억원 | 265억원 | 297억원 | 331.35 | 0.36 | 0.24 | -0.01 | 0 | 30.69 | 2.18 | 2.45 | 3.07 | 0.10 | 0.33 | -0.10 | -0.00 | 3.40 | 74 | 10백만원 | 30 | 4백만원 | 22 | 3백만원 | 22 | 3백만원 | 5.00 |
| 095610 | 테스 | BUY | 전략 신규 편입 (rank=3) | 0.86 | 3.00 | - | - | - | 65,800 | 1.31조원 | 2,835억원 | 494억원 | 448억원 | 67.84 | -0.09 | 0.22 | 0.03 | 0 | 29.16 | 3.34 | 4.61 | 0.51 | -0.04 | 0.34 | 0.06 | -0.00 | 0.86 | 150 | 10백만원 | 60 | 4백만원 | 45 | 3백만원 | 45 | 3백만원 | 5.00 |
| 000370 | 한화손해보험 | BUY | 전략 신규 편입 (rank=4) | 0.83 | 4.00 | - | - | - | 7,150 | 8,825억원 | 8,296억원 | 3,657억원 | 2,566억원 | -0.08 | 5.46 | 2.02 | -0.01 | 0 | 3.44 | 0.33 | 1.06 | -0.15 | 1.75 | -0.69 | -0.08 | -0.00 | 0.83 | 1,397 | 10백만원 | 559 | 4백만원 | 419 | 3백만원 | 419 | 3백만원 | 5.00 |
| 298380 | 에이비엘바이오 | BUY | 전략 신규 편입 (rank=5) | 0.65 | 5.00 | - | - | - | 190,500 | 8.90조원 | 779억원 | -180억원 | -166억원 | 17.74 | 2.61 | 0.58 | -0.08 | 0 | - | 56.82 | 114.28 | 0.02 | 0.83 | 0.14 | -0.33 | -0.00 | 0.65 | 50 | 10백만원 | 20 | 4백만원 | 15 | 3백만원 | 15 | 3백만원 | 5.00 |
| 100840 | SNT에너지 | BUY | 전략 신규 편입 (rank=6) | 0.42 | 6.00 | - | - | - | 53,300 | 9,803억원 | 4,578억원 | 870억원 | 595억원 | 4.08 | 1.09 | 0.48 | 0.02 | 0 | 16.47 | 2.69 | 2.14 | -0.11 | 0.33 | 0.20 | 0.01 | -0.00 | 0.42 | 187 | 10백만원 | 75 | 4백만원 | 56 | 3백만원 | 56 | 3백만원 | 5.01 |
| 041920 | 메디아나 | BUY | 전략 신규 편입 (rank=7) | 0.40 | 7.00 | - | - | - | 19,850 | 4,798억원 | 499억원 | 44억원 | 30억원 | 1.91 | 0.41 | 0.08 | 0.02 | 0 | 159.89 | 3.54 | 9.61 | -0.13 | 0.12 | 0.42 | -0.00 | -0.00 | 0.40 | 503 | 10백만원 | 201 | 4백만원 | 151 | 3백만원 | 151 | 3백만원 | 5.01 |
| 003670 | 포스코퓨처엠 | BUY | 전략 신규 편입 (rank=8) | 0.38 | 8.00 | - | - | - | 199,200 | 17.35조원 | 2.06조원 | -338억원 | -99억원 | 51.84 | -0.06 | 0.71 | 0.02 | 0 | - | 3.85 | 8.41 | 0.35 | -0.03 | 0.07 | -0.00 | -0.00 | 0.38 | 50 | 10백만원 | 20 | 4백만원 | 15 | 3백만원 | 15 | 3백만원 | 5.00 |
| 302440 | SK바이오사이언스 | BUY | 전략 신규 편입 (rank=9) | 0.36 | 9.00 | - | - | - | 42,850 | 3.86조원 | 5,006억원 | -1,041억원 | -773억원 | -0.30 | 0.94 | 0.37 | 0.01 | 0 | - | 1.89 | 7.71 | -0.16 | 0.29 | 0.26 | -0.03 | -0.00 | 0.36 | 233 | 10백만원 | 93 | 4백만원 | 70 | 3백만원 | 70 | 3백만원 | 5.02 |
| 098460 | 고영 | BUY | 전략 신규 편입 (rank=10) | 0.34 | 10.00 | - | - | - | 29,050 | 2.13조원 | 1,724억원 | 126억원 | 63억원 | 4.67 | 0.21 | 0.20 | 0.04 | 0 | 338.25 | 6.36 | 12.37 | -0.11 | 0.05 | 0.35 | 0.04 | -0.00 | 0.34 | 343 | 10백만원 | 137 | 4백만원 | 103 | 3백만원 | 103 | 3백만원 | 5.00 |

## 5. SELL 종목 요약표

| ticker | name | action | reason | score | score_rank | industry4 | cohort_status | score_availability_reason | price | mcap | revenue | op_income | net_income | op_acc2 | rev_acc2 | debt_log | cfo_to_assets | cfo_isnull | per | pbr | psr | contrib_op | contrib_rev | contrib_debt | contrib_cfo | contrib_missing | score_rebuilt | planned_qty | planned_value | day1_qty | day1_value | day2_qty | day2_value | day3_qty | day3_value | est_slippage_bps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 005930 | 삼성전자 | SELL | 전략 탈락 (universe_rank=18) | 0.21 | 18.00 | - | in_target_cohort | scored_in_target_cohort | 199,400 | 1,081.72조원 | 247.54조원 | 31.43조원 | 32.98조원 | -4.36 | -0.04 | 0.26 | 0.05 | 0 | 32.80 | 2.48 | 4.37 | -0.19 | -0.03 | 0.32 | 0.11 | -0.00 | 0.21 | 100 | 20백만원 | 70 | 14백만원 | 30 | 6백만원 | 0 | 0원 | 5.00 |
| 058470 | 리노공업 | SELL | 전략 탈락 (no_feature_history) | - | - | - | not_in_target_cohort | no_feature_history | 111,700 | 1.49조원 | 1,956억원 | 909억원 | 949억원 | - | - | 0.10 | 0.09 | 0 | 15.66 | 2.16 | 7.60 | - | - | - | - | - | - | 124 | 14백만원 | 87 | 10백만원 | 37 | 4백만원 | 0 | 0원 | 5.00 |
| 214150 | 클래시스 | SELL | 전략 탈락 (no_feature_history) | - | - | - | not_in_target_cohort | no_feature_history | 53,400 | 4.15조원 | 1,826억원 | 903억원 | 978억원 | 0.27 | 0.22 | 0.23 | 0.07 | 0 | 42.47 | 8.01 | 22.75 | - | - | - | - | - | - | 600 | 32백만원 | 420 | 22백만원 | 180 | 10백만원 | 0 | 0원 | 5.01 |

## 6. BUY 종목 상세

### 한스바이오메드 (042520)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=1)
- 보고서 SCORE: **8.4440** / rank 1
- 재구성 SCORE: **8.4440**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: -
- 현재가: 53,000
- 시가총액: 5,789억원

재무:
- 매출(TTM): 677억원
- 영업이익(TTM): -259억원
- 순이익(TTM): -286억원
- 영업현금흐름(CFO): 12억원

팩터:
- OpIncome_acc2: 914.6441
- Revenue_acc2: 0.1103
- Debt_to_Equity_log: 1.3165
- Quality_CFO_to_Assets: 0.0095
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 8.7330
- contrib_rev: 0.0197
- contrib_debt: -0.2825
- contrib_cfo: -0.0261
- contrib_missing: -0.0000

멀티플:
- PBR: 17.08
- PSR: 8.55

실행 계획:
- 총 목표 주문수량: 187주
- 총 목표 주문금액: 10백만원
- Day1: 75주 / 4백만원
- Day2: 56주 / 3백만원
- Day3: 56주 / 3백만원
- 예상 슬리피지: 5.008 bps

### HD현대에너지솔루션 (322000)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=2)
- 보고서 SCORE: **3.4048** / rank 2
- 재구성 SCORE: **3.4048**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: -
- 현재가: 131,000
- 시가총액: 9,117억원

재무:
- 매출(TTM): 3,716억원
- 영업이익(TTM): 265억원
- 순이익(TTM): 297억원
- 영업현금흐름(CFO): -62억원

팩터:
- OpIncome_acc2: 331.3522
- Revenue_acc2: 0.3610
- Debt_to_Equity_log: 0.2387
- Quality_CFO_to_Assets: -0.0117
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0665
- contrib_rev: 0.1008
- contrib_debt: 0.3335
- contrib_cfo: -0.0960
- contrib_missing: -0.0000

멀티플:
- PER: 30.69
- PBR: 2.18
- PSR: 2.45
- EV/EBIT: 38.58

실행 계획:
- 총 목표 주문수량: 74주
- 총 목표 주문금액: 10백만원
- Day1: 30주 / 4백만원
- Day2: 22주 / 3백만원
- Day3: 22주 / 3백만원
- 예상 슬리피지: 5.000 bps

### 테스 (095610)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=3)
- 보고서 SCORE: **0.8631** / rank 3
- 재구성 SCORE: **0.8631**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: -
- 현재가: 65,800
- 시가총액: 1.31조원

재무:
- 매출(TTM): 2,835억원
- 영업이익(TTM): 494억원
- 순이익(TTM): 448억원
- 영업현금흐름(CFO): 169억원

팩터:
- OpIncome_acc2: 67.8429
- Revenue_acc2: -0.0895
- Debt_to_Equity_log: 0.2186
- Quality_CFO_to_Assets: 0.0347
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 0.5066
- contrib_rev: -0.0450
- contrib_debt: 0.3450
- contrib_cfo: 0.0564
- contrib_missing: -0.0000

멀티플:
- PER: 29.16
- PBR: 3.34
- PSR: 4.61
- EV/EBIT: 28.38

실행 계획:
- 총 목표 주문수량: 150주
- 총 목표 주문금액: 10백만원
- Day1: 60주 / 4백만원
- Day2: 45주 / 3백만원
- Day3: 45주 / 3백만원
- 예상 슬리피지: 5.004 bps

### 한화손해보험 (000370)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=4)
- 보고서 SCORE: **0.8294** / rank 4
- 재구성 SCORE: **0.8294**
- SCORE 차이(보고서-재구성): -0.0000
- industry4: -
- 현재가: 7,150
- 시가총액: 8,825억원

재무:
- 매출(TTM): 8,296억원
- 영업이익(TTM): 3,657억원
- 순이익(TTM): 2,566억원
- 영업현금흐름(CFO): -1,425억원

팩터:
- OpIncome_acc2: -0.0795
- Revenue_acc2: 5.4605
- Debt_to_Equity_log: 2.0240
- Quality_CFO_to_Assets: -0.0070
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -0.1533
- contrib_rev: 1.7499
- contrib_debt: -0.6870
- contrib_cfo: -0.0803
- contrib_missing: -0.0000

멀티플:
- PER: 3.44
- PBR: 0.33
- PSR: 1.06
- EV/EBIT: 51.04

실행 계획:
- 총 목표 주문수량: 1,397주
- 총 목표 주문금액: 10백만원
- Day1: 559주 / 4백만원
- Day2: 419주 / 3백만원
- Day3: 419주 / 3백만원
- 예상 슬리피지: 5.004 bps

### 에이비엘바이오 (298380)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=5)
- 보고서 SCORE: **0.6512** / rank 5
- 재구성 SCORE: **0.6512**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: -
- 현재가: 190,500
- 시가총액: 8.90조원

재무:
- 매출(TTM): 779억원
- 영업이익(TTM): -180억원
- 순이익(TTM): -166억원
- 영업현금흐름(CFO): -236억원

팩터:
- OpIncome_acc2: 17.7418
- Revenue_acc2: 2.6123
- Debt_to_Equity_log: 0.5838
- Quality_CFO_to_Assets: -0.0841
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 0.0199
- contrib_rev: 0.8288
- contrib_debt: 0.1362
- contrib_cfo: -0.3338
- contrib_missing: -0.0000

멀티플:
- PBR: 56.82
- PSR: 114.28

실행 계획:
- 총 목표 주문수량: 50주
- 총 목표 주문금액: 10백만원
- Day1: 20주 / 4백만원
- Day2: 15주 / 3백만원
- Day3: 15주 / 3백만원
- 예상 슬리피지: 5.001 bps

### SNT에너지 (100840)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=6)
- 보고서 SCORE: **0.4229** / rank 6
- 재구성 SCORE: **0.4229**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: -
- 현재가: 53,300
- 시가총액: 9,803억원

재무:
- 매출(TTM): 4,578억원
- 영업이익(TTM): 870억원
- 순이익(TTM): 595억원
- 영업현금흐름(CFO): 113억원

팩터:
- OpIncome_acc2: 4.0841
- Revenue_acc2: 1.0853
- Debt_to_Equity_log: 0.4807
- Quality_CFO_to_Assets: 0.0192
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -0.1128
- contrib_rev: 0.3350
- contrib_debt: 0.1952
- contrib_cfo: 0.0056
- contrib_missing: -0.0000

멀티플:
- PER: 16.47
- PBR: 2.69
- PSR: 2.14
- EV/EBIT: 13.86

실행 계획:
- 총 목표 주문수량: 187주
- 총 목표 주문금액: 10백만원
- Day1: 75주 / 4백만원
- Day2: 56주 / 3백만원
- Day3: 56주 / 3백만원
- 예상 슬리피지: 5.006 bps

### 메디아나 (041920)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=7)
- 보고서 SCORE: **0.4039** / rank 7
- 재구성 SCORE: **0.4039**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: -
- 현재가: 19,850
- 시가총액: 4,798억원

재무:
- 매출(TTM): 499억원
- 영업이익(TTM): 44억원
- 순이익(TTM): 30억원
- 영업현금흐름(CFO): 24억원

팩터:
- OpIncome_acc2: 1.9089
- Revenue_acc2: 0.4150
- Debt_to_Equity_log: 0.0808
- Quality_CFO_to_Assets: 0.0162
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -0.1339
- contrib_rev: 0.1182
- contrib_debt: 0.4237
- contrib_cfo: -0.0041
- contrib_missing: -0.0000

멀티플:
- PER: 159.89
- PBR: 3.54
- PSR: 9.61
- EV/EBIT: 112.40

실행 계획:
- 총 목표 주문수량: 503주
- 총 목표 주문금액: 10백만원
- Day1: 201주 / 4백만원
- Day2: 151주 / 3백만원
- Day3: 151주 / 3백만원
- 예상 슬리피지: 5.012 bps

### 포스코퓨처엠 (003670)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=8)
- 보고서 SCORE: **0.3815** / rank 8
- 재구성 SCORE: **0.3815**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: -
- 현재가: 199,200
- 시가총액: 17.35조원

재무:
- 매출(TTM): 2.06조원
- 영업이익(TTM): -338억원
- 순이익(TTM): -99억원
- 영업현금흐름(CFO): 1,551억원

팩터:
- OpIncome_acc2: 51.8446
- Revenue_acc2: -0.0562
- Debt_to_Equity_log: 0.7063
- Quality_CFO_to_Assets: 0.0170
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 0.3512
- contrib_rev: -0.0342
- contrib_debt: 0.0662
- contrib_cfo: -0.0017
- contrib_missing: -0.0000

멀티플:
- PBR: 3.85
- PSR: 8.41

실행 계획:
- 총 목표 주문수량: 50주
- 총 목표 주문금액: 10백만원
- Day1: 20주 / 4백만원
- Day2: 15주 / 3백만원
- Day3: 15주 / 3백만원
- 예상 슬리피지: 5.001 bps

### SK바이오사이언스 (302440)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=9)
- 보고서 SCORE: **0.3573** / rank 9
- 재구성 SCORE: **0.3573**
- SCORE 차이(보고서-재구성): -0.0000
- industry4: -
- 현재가: 42,850
- 시가총액: 3.86조원

재무:
- 매출(TTM): 5,006억원
- 영업이익(TTM): -1,041억원
- 순이익(TTM): -773억원
- 영업현금흐름(CFO): 237억원

팩터:
- OpIncome_acc2: -0.3013
- Revenue_acc2: 0.9358
- Debt_to_Equity_log: 0.3720
- Quality_CFO_to_Assets: 0.0080
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -0.1554
- contrib_rev: 0.2866
- contrib_debt: 0.2573
- contrib_cfo: -0.0312
- contrib_missing: -0.0000

멀티플:
- PBR: 1.89
- PSR: 7.71

실행 계획:
- 총 목표 주문수량: 233주
- 총 목표 주문금액: 10백만원
- Day1: 93주 / 4백만원
- Day2: 70주 / 3백만원
- Day3: 70주 / 3백만원
- 예상 슬리피지: 5.015 bps

### 고영 (098460)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=10)
- 보고서 SCORE: **0.3378** / rank 10
- 재구성 SCORE: **0.3378**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: -
- 현재가: 29,050
- 시가총액: 2.13조원

재무:
- 매출(TTM): 1,724억원
- 영업이익(TTM): 126억원
- 순이익(TTM): 63억원
- 영업현금흐름(CFO): 150억원

팩터:
- OpIncome_acc2: 4.6718
- Revenue_acc2: 0.2129
- Debt_to_Equity_log: 0.2023
- Quality_CFO_to_Assets: 0.0367
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -0.1071
- contrib_rev: 0.0529
- contrib_debt: 0.3543
- contrib_cfo: 0.0378
- contrib_missing: -0.0000

멀티플:
- PER: 338.25
- PBR: 6.36
- PSR: 12.37
- EV/EBIT: 174.51

실행 계획:
- 총 목표 주문수량: 343주
- 총 목표 주문금액: 10백만원
- Day1: 137주 / 4백만원
- Day2: 103주 / 3백만원
- Day3: 103주 / 3백만원
- 예상 슬리피지: 5.001 bps

## 7. SELL 종목 상세

### 삼성전자 (005930)

- 액션: **SELL**
- 사유: 전략 탈락 (universe_rank=18)
- 보고서 SCORE: **0.2054** / rank 18
- 재구성 SCORE: **0.2054**
- SCORE 차이(보고서-재구성): -0.0000
- industry4: -
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 199,400
- 시가총액: 1,081.72조원

재무:
- 매출(TTM): 247.54조원
- 영업이익(TTM): 31.43조원
- 순이익(TTM): 32.98조원
- 영업현금흐름(CFO): 28.80조원

팩터:
- OpIncome_acc2: -4.3604
- Revenue_acc2: -0.0417
- Debt_to_Equity_log: 0.2619
- Quality_CFO_to_Assets: 0.0508
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -0.1948
- contrib_rev: -0.0295
- contrib_debt: 0.3203
- contrib_cfo: 0.1094
- contrib_missing: -0.0000

멀티플:
- PER: 32.80
- PBR: 2.48
- PSR: 4.37
- EV/EBIT: 38.57

실행 계획:
- 총 목표 주문수량: 100주
- 총 목표 주문금액: 20백만원
- Day1: 70주 / 14백만원
- Day2: 30주 / 6백만원
- Day3: 0주 / 0원
- 예상 슬리피지: 5.000 bps

### 리노공업 (058470)

- 액션: **SELL**
- 사유: 전략 탈락 (no_feature_history)
- 보고서 SCORE: **-** / rank -
- 재구성 SCORE: **-**
- SCORE 차이(보고서-재구성): -
- industry4: -
- cohort_status: not_in_target_cohort
- score_availability_reason: no_feature_history
- 현재가: 111,700
- 시가총액: 1.49조원

재무:
- 매출(TTM): 1,956억원
- 영업이익(TTM): 909억원
- 순이익(TTM): 949억원
- 영업현금흐름(CFO): 721억원

팩터:
- OpIncome_acc2: -
- Revenue_acc2: -
- Debt_to_Equity_log: 0.1005
- Quality_CFO_to_Assets: 0.0946
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -
- contrib_rev: -
- contrib_debt: -
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 15.66
- PBR: 2.16
- PSR: 7.60
- EV/EBIT: 17.15

실행 계획:
- 총 목표 주문수량: 124주
- 총 목표 주문금액: 14백만원
- Day1: 87주 / 10백만원
- Day2: 37주 / 4백만원
- Day3: 0주 / 0원
- 예상 슬리피지: 5.001 bps

### 클래시스 (214150)

- 액션: **SELL**
- 사유: 전략 탈락 (no_feature_history)
- 보고서 SCORE: **-** / rank -
- 재구성 SCORE: **-**
- SCORE 차이(보고서-재구성): -
- industry4: -
- cohort_status: not_in_target_cohort
- score_availability_reason: no_feature_history
- 현재가: 53,400
- 시가총액: 4.15조원

재무:
- 매출(TTM): 1,826억원
- 영업이익(TTM): 903억원
- 순이익(TTM): 978억원
- 영업현금흐름(CFO): 457억원

팩터:
- OpIncome_acc2: 0.2688
- Revenue_acc2: 0.2218
- Debt_to_Equity_log: 0.2331
- Quality_CFO_to_Assets: 0.0698
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -
- contrib_rev: -
- contrib_debt: -
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 42.47
- PBR: 8.01
- PSR: 22.75
- EV/EBIT: 47.48

실행 계획:
- 총 목표 주문수량: 600주
- 총 목표 주문금액: 32백만원
- Day1: 420주 / 22백만원
- Day2: 180주 / 10백만원
- Day3: 0주 / 0원
- 예상 슬리피지: 5.007 bps
