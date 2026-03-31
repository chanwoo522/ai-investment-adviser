# 리밸런싱 보고서

본 보고서는 한국 주식 팩터 기반 분기 리밸런싱 전략의 산출물입니다.
개별 종목의 절대적 우열 판단이 아니라, 동일 시점 유니버스 내 상대 점수 비교 결과를 반영합니다.
본 보고서는 투자판단 보조를 위한 정량 리밸런싱 자료이며, 최종 주문 집행 전 유동성·이벤트·체결 가능성을 추가 점검합니다.

- 기준일(asof): **2026-03-31**
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
- SELL 종목 수: **2**
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
| 005690 | 파미셀 | BUY | 전략 신규 편입 (rank=1) | 4.31 | 1.00 | 211.0 | - | - | 16,150 | - | 884억원 | 262억원 | 315억원 | 3.46 | 0.62 | 0.24 | 0.11 | 0 | - | - | - | 3.00 | 0.75 | 0.26 | 0.30 | -0.00 | 4.31 | 617 | 10백만원 | 247 | 4백만원 | 185 | 3백만원 | 185 | 3백만원 | 5.00 |
| 060280 | 큐렉소 | BUY | 전략 신규 편입 (rank=2) | 3.97 | 2.00 | 47812.0 | - | - | 15,230 | - | 573억원 | 20억원 | 19억원 | 2.41 | 0.61 | 0.05 | -0.04 | 0 | - | - | - | 3.00 | 0.75 | 0.40 | -0.18 | -0.00 | 3.97 | 654 | 10백만원 | 262 | 4백만원 | 196 | 3백만원 | 196 | 3백만원 | 5.01 |
| 218410 | RFHIC | BUY | 전략 신규 편입 (rank=3) | 3.94 | 3.00 | 264.0 | - | - | 80,900 | - | 1,453억원 | 235억원 | 284억원 | 8.30 | 0.62 | 0.32 | 0.01 | 0 | - | - | - | 3.00 | 0.75 | 0.20 | -0.01 | -0.00 | 3.94 | 123 | 10백만원 | 49 | 4백만원 | 37 | 3백만원 | 37 | 3백만원 | 5.00 |
| 450080 | 에코프로머티 | BUY | 전략 신규 편입 (rank=4) | 3.91 | 4.00 | 28202.0 | - | - | 62,500 | - | 3,294억원 | -402억원 | -1,375억원 | 3.21 | 1.08 | 0.41 | 0.03 | 0 | - | - | - | 3.00 | 0.75 | 0.13 | 0.03 | -0.00 | 3.91 | 160 | 10백만원 | 64 | 4백만원 | 48 | 3백만원 | 48 | 3백만원 | 5.00 |
| 053610 | 프로텍 | BUY | 전략 신규 편입 (rank=5) | 3.84 | 5.00 | 29271.0 | - | - | 57,900 | - | 1,795억원 | 308억원 | 235억원 | 2.63 | 0.35 | 0.22 | 0.03 | 0 | - | - | - | 3.00 | 0.53 | 0.27 | 0.04 | -0.00 | 3.84 | 171 | 10백만원 | 69 | 4백만원 | 51 | 3백만원 | 51 | 3백만원 | 5.02 |
| 100840 | SNT에너지 | BUY | 전략 신규 편입 (rank=6) | 3.84 | 6.00 | 29176.0 | - | - | 53,300 | - | 4,578억원 | 870억원 | 595억원 | 4.08 | 1.09 | 0.48 | 0.02 | 0 | - | - | - | 3.00 | 0.75 | 0.08 | 0.01 | -0.00 | 3.84 | 187 | 10백만원 | 75 | 4백만원 | 56 | 3백만원 | 56 | 3백만원 | 5.01 |
| 041920 | 메디아나 | BUY | 전략 신규 편입 (rank=7) | 3.83 | 7.00 | 27112.0 | - | - | 19,850 | - | 499억원 | 44억원 | 30억원 | 1.91 | 0.41 | 0.08 | 0.02 | 0 | - | - | - | 2.83 | 0.62 | 0.38 | -0.00 | -0.00 | 3.83 | 503 | 10백만원 | 201 | 4백만원 | 151 | 3백만원 | 151 | 3백만원 | 5.01 |
| 171090 | 선익시스템 | BUY | 전략 신규 편입 (rank=8) | 3.79 | 8.00 | 292.0 | - | - | 103,200 | - | 4,270억원 | 917억원 | 900억원 | 11.96 | 1.88 | 0.93 | 0.11 | 0 | - | - | - | 3.00 | 0.75 | -0.26 | 0.30 | -0.00 | 3.79 | 96 | 10백만원 | 38 | 4백만원 | 29 | 3백만원 | 29 | 3백만원 | 5.00 |
| 361610 | SK아이이테크놀로지 | BUY | 전략 신규 편입 (rank=9) | 3.73 | 9.00 | 282.0 | - | - | 22,200 | - | 1,828억원 | -1,991억원 | -1,714억원 | 9.94 | 0.74 | 0.52 | -0.00 | 0 | - | - | - | 3.00 | 0.75 | 0.05 | -0.07 | -0.00 | 3.73 | 450 | 10백만원 | 180 | 4백만원 | 135 | 3백만원 | 135 | 3백만원 | 5.02 |
| 003160 | 디아이 | BUY | 전략 신규 편입 (rank=10) | 3.72 | 10.00 | 27212.0 | - | - | 38,750 | - | 3,232억원 | 265억원 | 59억원 | 4.09 | 1.01 | 0.70 | 0.03 | 0 | - | - | - | 3.00 | 0.75 | -0.08 | 0.05 | -0.00 | 3.72 | 257 | 10백만원 | 103 | 4백만원 | 77 | 3백만원 | 77 | 3백만원 | 5.01 |

## 5. SELL 종목 요약표

| ticker | name | action | reason | score | score_rank | industry4 | cohort_status | score_availability_reason | price | mcap | revenue | op_income | net_income | op_acc2 | rev_acc2 | debt_log | cfo_to_assets | cfo_isnull | per | pbr | psr | contrib_op | contrib_rev | contrib_debt | contrib_cfo | contrib_missing | score_rebuilt | planned_qty | planned_value | day1_qty | day1_value | day2_qty | day2_value | day3_qty | day3_value | est_slippage_bps |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 005930 | 삼성전자 | SELL | 전략 탈락 (universe_rank=360) | -2.70 | 360.00 | 264.0 | in_target_cohort | scored_in_target_cohort | 199,400 | - | 247.54조원 | 31.43조원 | 32.98조원 | -4.36 | -0.04 | 0.26 | 0.05 | 0 | - | - | - | -3.00 | -0.06 | 0.24 | 0.12 | -0.00 | -2.70 | 100 | 20백만원 | 70 | 14백만원 | 30 | 6백만원 | 0 | 0원 | 5.00 |
| 058470 | 리노공업 | SELL | 전략 탈락 (universe_rank=87) | 1.12 | 87.00 | 2629.0 | in_target_cohort | scored_in_target_cohort | 111,700 | - | 2,757억원 | 1,287억원 | 1,100억원 | 0.22 | 0.17 | 0.08 | 0.05 | 0 | - | - | - | 0.38 | 0.25 | 0.38 | 0.10 | -0.00 | 1.12 | 49 | 5백만원 | 34 | 4백만원 | 15 | 2백만원 | 0 | 0원 | 5.00 |

## 6. BUY 종목 상세

### 파미셀 (005690)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=1)
- 보고서 SCORE: **4.3118** / rank 1
- 재구성 SCORE: **4.3118**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 211
- 현재가: 16,150
- 시가총액: -

재무:
- 매출(TTM): 884억원
- 영업이익(TTM): 262억원
- 순이익(TTM): 315억원
- 영업현금흐름(CFO): 173억원

팩터:
- OpIncome_acc2: 3.4557
- Revenue_acc2: 0.6186
- Debt_to_Equity_log: 0.2381
- Quality_CFO_to_Assets: 0.1099
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0000
- contrib_rev: 0.7500
- contrib_debt: 0.2618
- contrib_cfo: 0.3000
- contrib_missing: -0.0000

멀티플:
- 가용 멀티플 데이터 없음

실행 계획:
- 총 목표 주문수량: 617주
- 총 목표 주문금액: 10백만원
- Day1: 247주 / 4백만원
- Day2: 185주 / 3백만원
- Day3: 185주 / 3백만원
- 예상 슬리피지: 5.004 bps

### 큐렉소 (060280)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=2)
- 보고서 SCORE: **3.9748** / rank 2
- 재구성 SCORE: **3.9748**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 47,812
- 현재가: 15,230
- 시가총액: -

재무:
- 매출(TTM): 573억원
- 영업이익(TTM): 20억원
- 순이익(TTM): 19억원
- 영업현금흐름(CFO): -36억원

팩터:
- OpIncome_acc2: 2.4144
- Revenue_acc2: 0.6057
- Debt_to_Equity_log: 0.0495
- Quality_CFO_to_Assets: -0.0354
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0000
- contrib_rev: 0.7500
- contrib_debt: 0.4034
- contrib_cfo: -0.1786
- contrib_missing: -0.0000

멀티플:
- 가용 멀티플 데이터 없음

실행 계획:
- 총 목표 주문수량: 654주
- 총 목표 주문금액: 10백만원
- Day1: 262주 / 4백만원
- Day2: 196주 / 3백만원
- Day3: 196주 / 3백만원
- 예상 슬리피지: 5.008 bps

### RFHIC (218410)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=3)
- 보고서 SCORE: **3.9376** / rank 3
- 재구성 SCORE: **3.9376**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 264
- 현재가: 80,900
- 시가총액: -

재무:
- 매출(TTM): 1,453억원
- 영업이익(TTM): 235억원
- 순이익(TTM): 284억원
- 영업현금흐름(CFO): 71억원

팩터:
- OpIncome_acc2: 8.3040
- Revenue_acc2: 0.6214
- Debt_to_Equity_log: 0.3204
- Quality_CFO_to_Assets: 0.0130
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0000
- contrib_rev: 0.7500
- contrib_debt: 0.1999
- contrib_cfo: -0.0123
- contrib_missing: -0.0000

멀티플:
- 가용 멀티플 데이터 없음

실행 계획:
- 총 목표 주문수량: 123주
- 총 목표 주문금액: 10백만원
- Day1: 49주 / 4백만원
- Day2: 37주 / 3백만원
- Day3: 37주 / 3백만원
- 예상 슬리피지: 5.002 bps

### 에코프로머티 (450080)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=4)
- 보고서 SCORE: **3.9111** / rank 4
- 재구성 SCORE: **3.9111**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 28,202
- 현재가: 62,500
- 시가총액: -

재무:
- 매출(TTM): 3,294억원
- 영업이익(TTM): -402억원
- 순이익(TTM): -1,375억원
- 영업현금흐름(CFO): 542억원

팩터:
- OpIncome_acc2: 3.2094
- Revenue_acc2: 1.0793
- Debt_to_Equity_log: 0.4125
- Quality_CFO_to_Assets: 0.0313
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0000
- contrib_rev: 0.7500
- contrib_debt: 0.1307
- contrib_cfo: 0.0304
- contrib_missing: -0.0000

멀티플:
- 가용 멀티플 데이터 없음

실행 계획:
- 총 목표 주문수량: 160주
- 총 목표 주문금액: 10백만원
- Day1: 64주 / 4백만원
- Day2: 48주 / 3백만원
- Day3: 48주 / 3백만원
- 예상 슬리피지: 5.001 bps

### 프로텍 (053610)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=5)
- 보고서 SCORE: **3.8404** / rank 5
- 재구성 SCORE: **3.8404**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 29,271
- 현재가: 57,900
- 시가총액: -

재무:
- 매출(TTM): 1,795억원
- 영업이익(TTM): 308억원
- 순이익(TTM): 235억원
- 영업현금흐름(CFO): 123억원

팩터:
- OpIncome_acc2: 2.6260
- Revenue_acc2: 0.3543
- Debt_to_Equity_log: 0.2236
- Quality_CFO_to_Assets: 0.0278
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0000
- contrib_rev: 0.5292
- contrib_debt: 0.2726
- contrib_cfo: 0.0386
- contrib_missing: -0.0000

멀티플:
- 가용 멀티플 데이터 없음

실행 계획:
- 총 목표 주문수량: 171주
- 총 목표 주문금액: 10백만원
- Day1: 69주 / 4백만원
- Day2: 51주 / 3백만원
- Day3: 51주 / 3백만원
- 예상 슬리피지: 5.018 bps

### SNT에너지 (100840)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=6)
- 보고서 SCORE: **3.8384** / rank 6
- 재구성 SCORE: **3.8384**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 29,176
- 현재가: 53,300
- 시가총액: -

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
- contrib_op: 3.0000
- contrib_rev: 0.7500
- contrib_debt: 0.0795
- contrib_cfo: 0.0089
- contrib_missing: -0.0000

멀티플:
- 가용 멀티플 데이터 없음

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
- 보고서 SCORE: **3.8304** / rank 7
- 재구성 SCORE: **3.8304**
- SCORE 차이(보고서-재구성): -0.0000
- industry4: 27,112
- 현재가: 19,850
- 시가총액: -

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
- contrib_op: 2.8323
- contrib_rev: 0.6194
- contrib_debt: 0.3799
- contrib_cfo: -0.0012
- contrib_missing: -0.0000

멀티플:
- 가용 멀티플 데이터 없음

실행 계획:
- 총 목표 주문수량: 503주
- 총 목표 주문금액: 10백만원
- Day1: 201주 / 4백만원
- Day2: 151주 / 3백만원
- Day3: 151주 / 3백만원
- 예상 슬리피지: 5.012 bps

### 선익시스템 (171090)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=8)
- 보고서 SCORE: **3.7894** / rank 8
- 재구성 SCORE: **3.7894**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 292
- 현재가: 103,200
- 시가총액: -

재무:
- 매출(TTM): 4,270억원
- 영업이익(TTM): 917억원
- 순이익(TTM): 900억원
- 영업현금흐름(CFO): 397억원

팩터:
- OpIncome_acc2: 11.9622
- Revenue_acc2: 1.8841
- Debt_to_Equity_log: 0.9331
- Quality_CFO_to_Assets: 0.1086
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0000
- contrib_rev: 0.7500
- contrib_debt: -0.2606
- contrib_cfo: 0.3000
- contrib_missing: -0.0000

멀티플:
- 가용 멀티플 데이터 없음

실행 계획:
- 총 목표 주문수량: 96주
- 총 목표 주문금액: 10백만원
- Day1: 38주 / 4백만원
- Day2: 29주 / 3백만원
- Day3: 29주 / 3백만원
- 예상 슬리피지: 5.003 bps

### SK아이이테크놀로지 (361610)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=9)
- 보고서 SCORE: **3.7298** / rank 9
- 재구성 SCORE: **3.7298**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 282
- 현재가: 22,200
- 시가총액: -

재무:
- 매출(TTM): 1,828억원
- 영업이익(TTM): -1,991억원
- 순이익(TTM): -1,714억원
- 영업현금흐름(CFO): -132억원

팩터:
- OpIncome_acc2: 9.9441
- Revenue_acc2: 0.7363
- Debt_to_Equity_log: 0.5237
- Quality_CFO_to_Assets: -0.0030
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0000
- contrib_rev: 0.7500
- contrib_debt: 0.0471
- contrib_cfo: -0.0673
- contrib_missing: -0.0000

멀티플:
- 가용 멀티플 데이터 없음

실행 계획:
- 총 목표 주문수량: 450주
- 총 목표 주문금액: 10백만원
- Day1: 180주 / 4백만원
- Day2: 135주 / 3백만원
- Day3: 135주 / 3백만원
- 예상 슬리피지: 5.020 bps

### 디아이 (003160)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=10)
- 보고서 SCORE: **3.7191** / rank 10
- 재구성 SCORE: **3.7191**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 27,212
- 현재가: 38,750
- 시가총액: -

재무:
- 매출(TTM): 3,232억원
- 영업이익(TTM): 265억원
- 순이익(TTM): 59억원
- 영업현금흐름(CFO): 120억원

팩터:
- OpIncome_acc2: 4.0939
- Revenue_acc2: 1.0061
- Debt_to_Equity_log: 0.6985
- Quality_CFO_to_Assets: 0.0321
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 3.0000
- contrib_rev: 0.7500
- contrib_debt: -0.0842
- contrib_cfo: 0.0534
- contrib_missing: -0.0000

멀티플:
- 가용 멀티플 데이터 없음

실행 계획:
- 총 목표 주문수량: 257주
- 총 목표 주문금액: 10백만원
- Day1: 103주 / 4백만원
- Day2: 77주 / 3백만원
- Day3: 77주 / 3백만원
- 예상 슬리피지: 5.005 bps

## 7. SELL 종목 상세

### 삼성전자 (005930)

- 액션: **SELL**
- 사유: 전략 탈락 (universe_rank=360)
- 보고서 SCORE: **-2.6988** / rank 360
- 재구성 SCORE: **-2.6988**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 264
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 199,400
- 시가총액: -

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
- contrib_op: -3.0000
- contrib_rev: -0.0602
- contrib_debt: 0.2439
- contrib_cfo: 0.1175
- contrib_missing: -0.0000

멀티플:
- 가용 멀티플 데이터 없음

실행 계획:
- 총 목표 주문수량: 100주
- 총 목표 주문금액: 20백만원
- Day1: 70주 / 14백만원
- Day2: 30주 / 6백만원
- Day3: 0주 / 0원
- 예상 슬리피지: 5.000 bps

### 리노공업 (058470)

- 액션: **SELL**
- 사유: 전략 탈락 (universe_rank=87)
- 보고서 SCORE: **1.1171** / rank 87
- 재구성 SCORE: **1.1171**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 2,629
- cohort_status: in_target_cohort
- score_availability_reason: scored_in_target_cohort
- 현재가: 111,700
- 시가총액: -

재무:
- 매출(TTM): 2,757억원
- 영업이익(TTM): 1,287억원
- 순이익(TTM): 1,100억원
- 영업현금흐름(CFO): 365억원

팩터:
- OpIncome_acc2: 0.2224
- Revenue_acc2: 0.1689
- Debt_to_Equity_log: 0.0798
- Quality_CFO_to_Assets: 0.0460
- CFO_isnull: 0

스코어 기여도:
- contrib_op: 0.3821
- contrib_rev: 0.2532
- contrib_debt: 0.3807
- contrib_cfo: 0.1011
- contrib_missing: -0.0000

멀티플:
- 가용 멀티플 데이터 없음

실행 계획:
- 총 목표 주문수량: 49주
- 총 목표 주문금액: 5백만원
- Day1: 34주 / 4백만원
- Day2: 15주 / 2백만원
- Day3: 0주 / 0원
- 예상 슬리피지: 5.000 bps
