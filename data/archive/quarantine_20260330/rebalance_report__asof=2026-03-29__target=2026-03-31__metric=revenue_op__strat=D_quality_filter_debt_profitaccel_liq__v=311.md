# 리밸런싱 보고서

본 보고서는 한국 주식 팩터 기반 분기 리밸런싱 전략의 산출물입니다.
개별 종목의 절대적 우열 판단이 아니라, 동일 시점 유니버스 내 상대 점수 비교 결과를 반영합니다.
본 보고서는 투자판단 보조를 위한 정량 리밸런싱 자료이며, 최종 주문 집행 전 유동성·이벤트·체결 가능성을 추가 점검합니다.

- 기준일(asof): **2026-03-29**
- 목표 리밸런싱일(target): **2026-03-31**
- 전략명: **D_quality_filter_debt_profitaccel_liq**
- 전략 설명: **이익 가속도 중심(로그 완화) + 연속 성장 보너스 + 부채 통제 + 유동성 필터(거래대금+시총)**

## 1. 유의사항

- 본 전략은 이익 가속도와 재무 건전성 중심의 상대평가 전략입니다.
- 대형 우량주라도 해당 시점의 점수 경쟁에서 제외될 수 있습니다.
- 반대로 적자 기업이라도 이익 개선 가속이 강하면 편입될 수 있습니다.
- BUY/SELL는 절대적 우열이 아니라 이번 분기 기준 상대 점수 재정렬 결과입니다.
- 실제 액션(BUY/HOLD/SELL)은 순수 점수 순위 외에 운용 규칙(예: 최소 보유 유지 규칙, 평가 불가 종목 보호)에 의해 조정될 수 있습니다.

## 2. 요약

- BUY 종목 수: **10**
- SELL 종목 수: **0**
- REVIEW 종목 수: **3**

## 3. SCORE 계산 방식

### 3.1 점수 식

```text
Score_raw =  1.00 × z(OpIncome_acc2_log1p)
           + 0.25 × z(Revenue_acc2)
           - 0.35 × z(Debt_to_Equity_log)
           + 0.15 × op_growth_streak2
           + 0.05 × rev_growth_streak2
```

- 스코어링 방식: Cross-sectional zscore 기준 조합 / clip_z=2.5 / holding_bonus=0.2 / robust_z=true

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

## 4. BUY 종목 요약표

| ticker | name | action | reason | industry4 | industry_name | score | score_adj | hold_bonus_applied | score_rank | score_adj_rank | op_acc2 | op_acc2_log1p | contrib_op_log | op_growth_streak2 | contrib_op_streak | rev_acc2 | contrib_rev | rev_growth_streak2 | contrib_rev_streak | debt_log | contrib_debt | cfo_to_assets | cfo_isnull | score_rebuilt | price | mcap | year | quarter | revenue_prev_q | revenue_cur_q | revenue_qoq | op_prev_q | op_cur_q | op_qoq | revenue | op_income | net_income | per | pbr | psr | cohort_status | score_availability_reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 060280 | 큐렉소 | BUY | 전략 신규 편입 (rank=1) | 47812.0 | 기타 상품 전문 소매업 | 3.55 | 3.553165298384555 | 0.0 | 1.00 | 1.0 | 4.72 | 1.7442099508906124 | 2.5 | 0.0 | 0.0 | 0.58 | 0.62 | 0.0 | 0.0 | 0.05 | 0.43 | -0.11 | 0 | 3.55 | 17,460 | 7,166억원 | 2,025 | 4 | 172억원 | 208억원 | 0.21 | 3억원 | 14억원 | 3.05 | 745억원 | 24억원 | 28억원 | 258.56 | 7.45 | 9.61 | - | - |
| 041920 | 메디아나 | BUY | 전략 신규 편입 (rank=2) | 27112.0 | - | 3.53 | 3.529750463129177 | 0.0 | 2.00 | 2.0 | 4.41 | 1.6874566232521575 | 2.5 | 0.0 | 0.0 | 0.41 | 0.62 | 0.0 | 0.0 | 0.08 | 0.40 | 0.04 | 0 | 3.53 | 19,900 | 4,798억원 | 2,025 | 4 | 150억원 | 192억원 | 0.28 | 16억원 | 19억원 | 0.19 | 649억원 | 60억원 | 53억원 | 90.97 | 3.54 | 7.39 | - | - |
| 322000 | HD현대에너지솔루션 | BUY | 전략 신규 편입 (rank=3) | 2612.0 | 반도체 제조업 | 3.41 | 3.4116390618919814 | 0.0 | 3.00 | 3.0 | 11.57 | 2.531244466771533 | 2.5 | 0.0 | 0.0 | 0.39 | 0.62 | 0.0 | 0.0 | 0.24 | 0.29 | 0.03 | 0 | 3.41 | 123,200 | 9,117억원 | 2,025 | 4 | 1,210억원 | 1,526억원 | 0.26 | 147억원 | 145억원 | -0.01 | 4,927억원 | 412억원 | 417억원 | 21.88 | 2.18 | 1.85 | - | - |
| 425420 | 티에프이 | BUY | 전략 신규 편입 (rank=4) | 262.0 | 전자부품 제조업 | 3.39 | 3.385118577991137 | 0.0 | 4.00 | 4.0 | 3.86 | 1.5818483115435626 | 2.5 | 0.0 | 0.0 | 0.60 | 0.62 | 0.0 | 0.0 | 0.27 | 0.26 | 0.09 | 0 | 3.39 | 69,000 | 4,991억원 | 2,025 | 4 | 272억원 | 374억원 | 0.38 | 48억원 | 73억원 | 0.52 | 1,117억원 | 191억원 | 181억원 | 27.56 | 4.57 | 4.47 | - | - |
| 218410 | RFHIC | BUY | 전략 신규 편입 (rank=5) | 264.0 | 통신 및 방송 장비 제조업 | 3.35 | 3.350549329532591 | 0.0 | 5.00 | 5.0 | 15.14 | 2.781523944541956 | 2.5 | 0.0 | 0.0 | 0.59 | 0.62 | 0.0 | 0.0 | 0.32 | 0.23 | 0.05 | 0 | 3.35 | 82,400 | 1.33조원 | 2,025 | 4 | 405억원 | 688억원 | 0.70 | 74억원 | 115억원 | 0.56 | 1,858억원 | 309억원 | 355억원 | 37.52 | 3.36 | 7.17 | - | - |
| 098460 | 고영 | BUY | 전략 신규 편입 (rank=6) | 29271.0 | 특수 목적용 기계 제조업 | 3.33 | 3.330400224238243 | 0.0 | 6.00 | 6.0 | 5.06 | 1.8011839833658807 | 2.5 | 0.0 | 0.0 | 0.25 | 0.52 | 0.0 | 0.0 | 0.20 | 0.31 | 0.04 | 0 | 3.33 | 27,300 | 2.13조원 | 2,025 | 4 | 603억원 | 691억원 | 0.15 | 47억원 | 69억원 | 0.48 | 2,326억원 | 173억원 | 148억원 | 144.46 | 6.36 | 9.16 | - | - |
| 053610 | 프로텍 | BUY | 전략 신규 편입 (rank=7) | 29271.0 | 특수 목적용 기계 제조업 | 3.24 | 3.240711189643327 | 0.0 | 7.00 | 7.0 | 2.67 | 1.3011310343640652 | 2.400918194038148 | 0.0 | 0.0 | 0.26 | 0.54 | 0.0 | 0.0 | 0.22 | 0.30 | 0.07 | 0 | 3.24 | 54,900 | 6,237억원 | 2,025 | 4 | 510억원 | 846억원 | 0.66 | 156억원 | 180억원 | 0.16 | 2,305억원 | 463억원 | 354억원 | 17.63 | 1.76 | 2.71 | - | - |
| 100840 | SNT에너지 | BUY | 전략 신규 편입 (rank=8) | 29176.0 | 일반 목적용 기계 제조업 | 3.23 | 3.2306483243809 | 0.0 | 8.00 | 8.0 | 3.93 | 1.596323630857286 | 2.5 | 0.0 | 0.0 | 1.15 | 0.62 | 0.0 | 0.0 | 0.48 | 0.11 | 0.17 | 0 | 3.23 | 48,200 | 9,803억원 | 2,025 | 4 | 1,483억원 | 2,019억원 | 0.36 | 243억원 | 468억원 | 0.92 | 6,061억원 | 1,113억원 | 844억원 | 11.62 | 2.69 | 1.62 | - | - |
| 353200 | 대덕전자 | BUY | 전략 신규 편입 (rank=9) | 2622.0 | 전자부품 제조업 | 3.20 | 3.1969275521447127 | 0.0 | 9.00 | 9.0 | 3.88 | 1.5857399191320751 | 2.5 | 0.0 | 0.0 | 0.21 | 0.44 | 0.0 | 0.0 | 0.27 | 0.26 | 0.06 | 0 | 3.20 | 85,400 | 2.84조원 | 2,025 | 4 | 2,862억원 | 3,179억원 | 0.11 | 244억원 | 289억원 | 0.18 | 1.07조원 | 491억원 | 476억원 | 59.69 | 3.17 | 2.67 | - | - |
| 356860 | 티엘비 | BUY | 전략 신규 편입 (rank=10) | 2622.0 | 전자부품 제조업 | 3.09 | 3.0890648144939963 | 0.0 | 10.00 | 10.0 | 6.63 | 2.031937275399213 | 2.5 | 0.0 | 0.0 | 0.39 | 0.62 | 0.0 | 0.0 | 0.67 | -0.04 | 0.03 | 0 | 3.09 | 73,100 | 5,457억원 | 2,025 | 4 | 689억원 | 726억원 | 0.05 | 87억원 | 86억원 | -0.01 | 2,585억원 | 260억원 | 190억원 | 28.79 | 4.33 | 2.11 | - | - |

## 5. SELL 종목 요약표

_없음_

## 6. REVIEW 종목 요약표

| ticker | name | action | reason | industry4 | industry_name | score | score_adj | hold_bonus_applied | score_rank | score_adj_rank | op_acc2 | op_acc2_log1p | contrib_op_log | op_growth_streak2 | contrib_op_streak | rev_acc2 | contrib_rev | rev_growth_streak2 | contrib_rev_streak | debt_log | contrib_debt | cfo_to_assets | cfo_isnull | score_rebuilt | price | mcap | year | quarter | revenue_prev_q | revenue_cur_q | revenue_qoq | op_prev_q | op_cur_q | op_qoq | revenue | op_income | net_income | per | pbr | psr | cohort_status | score_availability_reason |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 005930 | 삼성전자 | HOLD | 전략 탈락 (universe_rank=300) / 최소 보유 유지 규칙 적용 | 264.0 | 통신 및 방송 장비 제조업 | -2.37 | -2.170252240194896 | 0.2 | 300.00 | 297.0 | -3.65 | -1.537105125880618 | -2.5 | 0.0 | 0.0 | -0.05 | -0.14 | 0.0 | 0.0 | 0.26 | 0.27 | 0.15 | 0 | -2.37 | 179,700 | 1,081.72조원 | 2,025 | 4 | 86.06조원 | 93.84조원 | 0.09 | 12.17조원 | 20.07조원 | 0.65 | 333.61조원 | 43.60조원 | 45.21조원 | 23.93 | 2.48 | 3.24 | in_target_cohort | scored_in_target_cohort |
| 058470 | 리노공업 | HOLD_REVIEW | 평가 보류 유지 (no_feature_history) | 2629.0 | 전자부품 제조업 | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | - | 104,900 | 1.49조원 | - | - | - | - | - | - | - | - | - | - | - | - | - | - | not_in_target_cohort | no_feature_history |
| 214150 | 클래시스 | HOLD | 전략 탈락 (universe_rank=109) / 최소 보유 유지 규칙 적용 | 271.0 | 의료용 기기 제조업 | 0.37 | 0.5666125091309282 | 0.2 | 109.00 | 97.0 | 0.03 | 0.0267466912781963 | 0.0280689571784459 | 0.0 | 0.0 | 0.04 | 0.06 | 0.0 | 0.0 | 0.24 | 0.28 | 0.23 | 0 | 0.37 | 54,000 | 4.15조원 | 2,025 | 4 | 830억원 | 934억원 | 0.13 | 376억원 | 512억원 | 0.36 | 3,368억원 | 1,706억원 | 1,320억원 | 31.47 | 7.53 | 12.33 | in_target_cohort | scored_in_target_cohort |

## 7. BUY 종목 상세

### 큐렉소 (060280)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=1)
- 보고서 SCORE: **3.5532** / rank 1
- 조정 SCORE: **3.5532**
- holding bonus: 0.0000
- 재구성 SCORE: **3.5532**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 47812
- 업종명: 기타 상품 전문 소매업
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
- op_growth_streak2: 0.0000
- Revenue_acc2: 0.5802
- Revenue_acc2_log1p: -
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.0495
- Quality_CFO_to_Assets: -0.1113
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -
- contrib_op_log: 2.5000
- contrib_op_streak: 0.0000
- contrib_rev: 0.6250
- contrib_rev_log: -
- contrib_rev_streak: 0.0000
- contrib_debt: 0.4282
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 258.56
- PBR: 7.45
- PSR: 9.61
- EV/EBIT: 305.49

실행 계획:
- 총 목표 주문수량: -주
- 총 목표 주문금액: -
- Day1: -주 / -
- Day2: -주 / -
- Day3: -주 / -
- 예상 슬리피지: - bps

### 메디아나 (041920)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=2)
- 보고서 SCORE: **3.5298** / rank 2
- 조정 SCORE: **3.5298**
- holding bonus: 0.0000
- 재구성 SCORE: **3.5298**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 27112
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
- op_growth_streak2: 0.0000
- Revenue_acc2: 0.4113
- Revenue_acc2_log1p: -
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.0808
- Quality_CFO_to_Assets: 0.0375
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -
- contrib_op_log: 2.5000
- contrib_op_streak: 0.0000
- contrib_rev: 0.6250
- contrib_rev_log: -
- contrib_rev_streak: 0.0000
- contrib_debt: 0.4048
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 90.97
- PBR: 3.54
- PSR: 7.39
- EV/EBIT: 82.56

실행 계획:
- 총 목표 주문수량: -주
- 총 목표 주문금액: -
- Day1: -주 / -
- Day2: -주 / -
- Day3: -주 / -
- 예상 슬리피지: - bps

### HD현대에너지솔루션 (322000)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=3)
- 보고서 SCORE: **3.4116** / rank 3
- 조정 SCORE: **3.4116**
- holding bonus: 0.0000
- 재구성 SCORE: **3.4116**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 2612
- 업종명: 반도체 제조업
- 현재가: 123,200
- 시가총액: 9,117억원

재무:
- 매출(TTM): 4,927억원
- 영업이익(TTM): 412억원
- 순이익(TTM): 417억원
- 영업현금흐름(CFO): 177억원

팩터:
- OpIncome_acc2: 11.5691
- OpIncome_acc2_log1p: 2.5312
- op_growth_streak2: 0.0000
- Revenue_acc2: 0.3930
- Revenue_acc2_log1p: -
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.2387
- Quality_CFO_to_Assets: 0.0334
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -
- contrib_op_log: 2.5000
- contrib_op_streak: 0.0000
- contrib_rev: 0.6250
- contrib_rev_log: -
- contrib_rev_streak: 0.0000
- contrib_debt: 0.2866
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 21.88
- PBR: 2.18
- PSR: 1.85
- EV/EBIT: 24.85

실행 계획:
- 총 목표 주문수량: -주
- 총 목표 주문금액: -
- Day1: -주 / -
- Day2: -주 / -
- Day3: -주 / -
- 예상 슬리피지: - bps

### 티에프이 (425420)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=4)
- 보고서 SCORE: **3.3851** / rank 4
- 조정 SCORE: **3.3851**
- holding bonus: 0.0000
- 재구성 SCORE: **3.3851**
- SCORE 차이(보고서-재구성): -0.0000
- industry4: 262
- 업종명: 전자부품 제조업
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
- op_growth_streak2: 0.0000
- Revenue_acc2: 0.6011
- Revenue_acc2_log1p: -
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.2742
- Quality_CFO_to_Assets: 0.0892
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -
- contrib_op_log: 2.5000
- contrib_op_streak: 0.0000
- contrib_rev: 0.6250
- contrib_rev_log: -
- contrib_rev_streak: 0.0000
- contrib_debt: 0.2601
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 27.56
- PBR: 4.57
- PSR: 4.47
- EV/EBIT: 28.00

실행 계획:
- 총 목표 주문수량: -주
- 총 목표 주문금액: -
- Day1: -주 / -
- Day2: -주 / -
- Day3: -주 / -
- 예상 슬리피지: - bps

### RFHIC (218410)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=5)
- 보고서 SCORE: **3.3505** / rank 5
- 조정 SCORE: **3.3505**
- holding bonus: 0.0000
- 재구성 SCORE: **3.3505**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 264
- 업종명: 통신 및 방송 장비 제조업
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
- Revenue_acc2_log1p: -
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.3204
- Quality_CFO_to_Assets: 0.0540
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -
- contrib_op_log: 2.5000
- contrib_op_streak: 0.0000
- contrib_rev: 0.6250
- contrib_rev_log: -
- contrib_rev_streak: 0.0000
- contrib_debt: 0.2255
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 37.52
- PBR: 3.36
- PSR: 7.17
- EV/EBIT: 47.97

실행 계획:
- 총 목표 주문수량: -주
- 총 목표 주문금액: -
- Day1: -주 / -
- Day2: -주 / -
- Day3: -주 / -
- 예상 슬리피지: - bps

### 고영 (098460)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=6)
- 보고서 SCORE: **3.3304** / rank 6
- 조정 SCORE: **3.3304**
- holding bonus: 0.0000
- 재구성 SCORE: **3.3304**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 29271
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
- op_growth_streak2: 0.0000
- Revenue_acc2: 0.2510
- Revenue_acc2_log1p: -
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.2023
- Quality_CFO_to_Assets: 0.0441
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -
- contrib_op_log: 2.5000
- contrib_op_streak: 0.0000
- contrib_rev: 0.5165
- contrib_rev_log: -
- contrib_rev_streak: 0.0000
- contrib_debt: 0.3139
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 144.46
- PBR: 6.36
- PSR: 9.16
- EV/EBIT: 127.26

실행 계획:
- 총 목표 주문수량: -주
- 총 목표 주문금액: -
- Day1: -주 / -
- Day2: -주 / -
- Day3: -주 / -
- 예상 슬리피지: - bps

### 프로텍 (053610)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=7)
- 보고서 SCORE: **3.2407** / rank 7
- 조정 SCORE: **3.2407**
- holding bonus: 0.0000
- 재구성 SCORE: **3.2407**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 29271
- 업종명: 특수 목적용 기계 제조업
- 현재가: 54,900
- 시가총액: 6,237억원

재무:
- 매출(TTM): 2,305억원
- 영업이익(TTM): 463억원
- 순이익(TTM): 354억원
- 영업현금흐름(CFO): 296억원

팩터:
- OpIncome_acc2: 2.6734
- OpIncome_acc2_log1p: 1.3011
- op_growth_streak2: 0.0000
- Revenue_acc2: 0.2627
- Revenue_acc2_log1p: -
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.2236
- Quality_CFO_to_Assets: 0.0669
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -
- contrib_op_log: 2.4009
- contrib_op_streak: 0.0000
- contrib_rev: 0.5418
- contrib_rev_log: -
- contrib_rev_streak: 0.0000
- contrib_debt: 0.2980
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 17.63
- PBR: 1.76
- PSR: 2.71
- EV/EBIT: 15.38

실행 계획:
- 총 목표 주문수량: -주
- 총 목표 주문금액: -
- Day1: -주 / -
- Day2: -주 / -
- Day3: -주 / -
- 예상 슬리피지: - bps

### SNT에너지 (100840)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=8)
- 보고서 SCORE: **3.2306** / rank 8
- 조정 SCORE: **3.2306**
- holding bonus: 0.0000
- 재구성 SCORE: **3.2306**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 29176
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
- Revenue_acc2_log1p: -
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.4807
- Quality_CFO_to_Assets: 0.1746
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -
- contrib_op_log: 2.5000
- contrib_op_streak: 0.0000
- contrib_rev: 0.6250
- contrib_rev_log: -
- contrib_rev_streak: 0.0000
- contrib_debt: 0.1056
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 11.62
- PBR: 2.69
- PSR: 1.62
- EV/EBIT: 10.83

실행 계획:
- 총 목표 주문수량: -주
- 총 목표 주문금액: -
- Day1: -주 / -
- Day2: -주 / -
- Day3: -주 / -
- 예상 슬리피지: - bps

### 대덕전자 (353200)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=9)
- 보고서 SCORE: **3.1969** / rank 9
- 조정 SCORE: **3.1969**
- holding bonus: 0.0000
- 재구성 SCORE: **3.1969**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 2622
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
- op_growth_streak2: 0.0000
- Revenue_acc2: 0.2133
- Revenue_acc2_log1p: -
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.2722
- Quality_CFO_to_Assets: 0.0607
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -
- contrib_op_log: 2.5000
- contrib_op_streak: 0.0000
- contrib_rev: 0.4353
- contrib_rev_log: -
- contrib_rev_streak: 0.0000
- contrib_debt: 0.2616
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 59.69
- PBR: 3.17
- PSR: 2.67
- EV/EBIT: 63.64

실행 계획:
- 총 목표 주문수량: -주
- 총 목표 주문금액: -
- Day1: -주 / -
- Day2: -주 / -
- Day3: -주 / -
- 예상 슬리피지: - bps

### 티엘비 (356860)

- 액션: **BUY**
- 사유: 전략 신규 편입 (rank=10)
- 보고서 SCORE: **3.0891** / rank 10
- 조정 SCORE: **3.0891**
- holding bonus: 0.0000
- 재구성 SCORE: **3.0891**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 2622
- 업종명: 전자부품 제조업
- 현재가: 73,100
- 시가총액: 5,457억원

재무:
- 매출(TTM): 2,585억원
- 영업이익(TTM): 260억원
- 순이익(TTM): 190억원
- 영업현금흐름(CFO): 82억원

팩터:
- OpIncome_acc2: 6.6289
- OpIncome_acc2_log1p: 2.0319
- op_growth_streak2: 0.0000
- Revenue_acc2: 0.3853
- Revenue_acc2_log1p: -
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.6699
- Quality_CFO_to_Assets: 0.0332
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -
- contrib_op_log: 2.5000
- contrib_op_streak: 0.0000
- contrib_rev: 0.6250
- contrib_rev_log: -
- contrib_rev_streak: 0.0000
- contrib_debt: -0.0359
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 28.79
- PBR: 4.33
- PSR: 2.11
- EV/EBIT: 25.65

실행 계획:
- 총 목표 주문수량: -주
- 총 목표 주문금액: -
- Day1: -주 / -
- Day2: -주 / -
- Day3: -주 / -
- 예상 슬리피지: - bps

## 8. SELL 종목 상세

_없음_
## 9. REVIEW 종목 상세

### 삼성전자 (005930)

- 액션: **HOLD**
- 사유: 전략 탈락 (universe_rank=300) / 최소 보유 유지 규칙 적용
- 보고서 SCORE: **-2.3703** / rank 300
- 조정 SCORE: **-2.1703**
- holding bonus: 0.2000
- 재구성 SCORE: **-2.3703**
- SCORE 차이(보고서-재구성): -0.0000
- industry4: 264
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
- op_growth_streak2: 0.0000
- Revenue_acc2: -0.0532
- Revenue_acc2_log1p: -
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.2619
- Quality_CFO_to_Assets: 0.1505
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -
- contrib_op_log: -2.5000
- contrib_op_streak: 0.0000
- contrib_rev: -0.1396
- contrib_rev_log: -
- contrib_rev_streak: 0.0000
- contrib_debt: 0.2693
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 23.93
- PBR: 2.48
- PSR: 3.24
- EV/EBIT: 27.81

실행 계획:
- 총 목표 주문수량: -주
- 총 목표 주문금액: -
- Day1: -주 / -
- Day2: -주 / -
- Day3: -주 / -
- 예상 슬리피지: - bps

### 리노공업 (058470)

- 액션: **HOLD_REVIEW**
- 사유: 평가 보류 유지 (no_feature_history)
- 보고서 SCORE: **-** / rank -
- 조정 SCORE: **-**
- holding bonus: -
- 재구성 SCORE: **-**
- SCORE 차이(보고서-재구성): -
- industry4: 2629
- 업종명: 전자부품 제조업
- cohort_status: not_in_target_cohort
- score_availability_reason: no_feature_history
- 현재가: 104,900
- 시가총액: 1.49조원

재무:
- 매출(TTM): -
- 영업이익(TTM): -
- 순이익(TTM): -
- 영업현금흐름(CFO): -

팩터:
- OpIncome_acc2: -
- OpIncome_acc2_log1p: -
- op_growth_streak2: -
- Revenue_acc2: -
- Revenue_acc2_log1p: -
- rev_growth_streak2: -
- Debt_to_Equity_log: -
- Quality_CFO_to_Assets: -
- CFO_isnull: -

스코어 기여도:
- contrib_op: -
- contrib_op_log: -
- contrib_op_streak: -
- contrib_rev: -
- contrib_rev_log: -
- contrib_rev_streak: -
- contrib_debt: -
- contrib_cfo: -
- contrib_missing: -

멀티플:
- 가용 멀티플 데이터 없음

실행 계획:
- 총 목표 주문수량: -주
- 총 목표 주문금액: -
- Day1: -주 / -
- Day2: -주 / -
- Day3: -주 / -
- 예상 슬리피지: - bps

### 클래시스 (214150)

- 액션: **HOLD**
- 사유: 전략 탈락 (universe_rank=109) / 최소 보유 유지 규칙 적용
- 보고서 SCORE: **0.3666** / rank 109
- 조정 SCORE: **0.5666**
- holding bonus: 0.2000
- 재구성 SCORE: **0.3666**
- SCORE 차이(보고서-재구성): 0.0000
- industry4: 271
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
- Revenue_acc2_log1p: -
- rev_growth_streak2: 0.0000
- Debt_to_Equity_log: 0.2447
- Quality_CFO_to_Assets: 0.2326
- CFO_isnull: 0

스코어 기여도:
- contrib_op: -
- contrib_op_log: 0.0281
- contrib_op_streak: 0.0000
- contrib_rev: 0.0564
- contrib_rev_log: -
- contrib_rev_streak: 0.0000
- contrib_debt: 0.2821
- contrib_cfo: -
- contrib_missing: -

멀티플:
- PER: 31.47
- PBR: 7.53
- PSR: 12.33
- EV/EBIT: 25.24

실행 계획:
- 총 목표 주문수량: -주
- 총 목표 주문금액: -
- Day1: -주 / -
- Day2: -주 / -
- Day3: -주 / -
- 예상 슬리피지: - bps
