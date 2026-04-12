# ai_inv_adv 마스터 런북 (기준일: 2026-04-11)

## 1. 현재 프로젝트 목표

이 문서는 `ai_inv_adv` 프로젝트를 새 대화창에서도 그대로 이어가기 위한 기준 문서다.

현재 목표는 다음과 같다.

1. **전략 백테스트 파이프라인**을 2026-04-11 기준으로 다시 점검한다.
2. **보고서 생성기 파이프라인**을 2026-04-11 기준으로 다시 점검한다.
3. 각 파이프라인이 실제로 참조하는 **코드 파일, 데이터 파일, 중간 산출물 경로**를 정리한다.
4. **불필요한 패치본 / 중복 파일 / 임시 산출물**을 식별하고 정리 원칙을 세운다.
5. GitHub 업로드 전에 문서/구조/실행 순서를 정리한다.
6. 이후 **AI 분류 모듈**을 추가한다.
   - 입력: 스코어 계산에 쓰는 변수들
   - 출력: 다음 분기 리밸런싱 전까지 종목이 `상승` / `하락` 할지 여부
   - 기존 TOP-K 스코어링 모델에 대해 **상승 예측 종목만 통과시키는 필터** 후보로 평가
7. AI 모듈을 붙인 버전과 기존 버전의 **백테스트 및 분기별 수익률 비교**를 수행한다.
8. 비교 결과를 바탕으로 현 모델에 AI 필터를 붙일지 결정한다.

---

## 2. 현재 파악된 핵심 파이프라인

### 2.1 전략 백테스트 파이프라인
대략 다음 순서로 이해하고 있다.

1. 데이터 수집
2. 유니버스 구성
3. 팩터/스코어 계산
4. 리밸런싱 결과 및 수익률 비교
5. 평가 및 요약 보고

### 2.2 보고서 생성기 파이프라인
대략 다음 순서로 이해하고 있다.

1. 데이터 수집
2. 스코어 계산
3. 액션 플랜 수립
4. 직전 리밸런싱 성과 평가
5. 보고서 생성

---

## 3. 현재 파일 라이브러리에서 확인된 핵심 코드 파일

아래는 현재 업로드/검색 가능한 범위에서 확인된 핵심 파일들이다.
실제 로컬 저장소(`C:\Users\chanw\ai_inv_adv`)의 전체 구조를 100% 직접 읽은 것은 아니므로,
반드시 로컬에서 `tree` / `Get-ChildItem`로 최종 대조해야 한다.

### 3.1 데이터 수집 / 기초 데이터
- `collect_fundamentals_quarterly.py`
- `collect_krx_marketdata.py`
- `collect_dart_shares_industry.py`
- `make_prices_daily.py`

### 3.2 유니버스 / 팩터
- `build_universe.py`
- `build_factors_ttm_acc2.py`
- `make_features_live.py` 계열 (이전 대화 기준 `make_features_live_fixed_v2.py` 존재)

### 3.3 스코어 / 리밸런싱 / 액션
- `score_latest_rebalance.py` (이전 대화에서 반복 사용됨)
- `generate_live_actions.py`
- `make_order_sheet.py`
- `execution_plan` 생성 스크립트 계열 (이전 대화 기준 존재, 현재 파일 라이브러리에서는 직접 확인 범위 제한)

### 3.4 보고서
- `generate_rebalance_report.py`
- 패치 이력 파일:
  - `generate_rebalance_report_patched.py`
  - `generate_rebalance_report_final_patched.py`
  - `generate_rebalance_report_final_patched_v2.py`

> 정리 포인트: `generate_rebalance_report.py`의 패치본이 여러 개 있으므로 GitHub 정리 전에 **최종 채택본 1개만 남기고 나머지는 archive 또는 삭제 후보**로 분류한다.

---

## 4. 현재 파악된 파일/로직 단서

### 4.1 `build_universe.py`
- `data/processed` 아래의 `krx_master__asof=*` 와 `krx_marketdata__asof=*` 파일을 찾는다.
- 요청한 `asof`와 정확히 일치하지 않아도 **가장 가까운 파일을 fallback**으로 사용할 수 있다.
- 필요한 파일이 없으면 `collect_krx_master.py`, `collect_krx_marketdata.py`를 자동 실행하도록 구성되어 있다.

### 4.2 `make_prices_daily.py`
- `data/processed` 아래 여러 파일 패턴에서 ticker를 모아 union을 만든다.
- 사용 패턴 예:
  - `krx_master__asof=*`
  - `krx_marketdata__asof=*`
  - `fundamentals_quarterly__asof=*`
  - `universe__asof=*`
  - `features_live__asof=*`
  - `shares_industry__asof=*`
- 즉, **가격 데이터 생성은 여러 중간산출물에 느슨하게 의존**한다.

### 4.3 `generate_live_actions.py`
- 현재 보유 종목 CSV와 최신 점수/유니버스/Top-K 결과를 합쳐 액션을 생성한다.
- `min_keep_current`, `sell_unscored_current` 같은 안전장치가 있다.
- 현재 운용 포트폴리오와 새 타깃 포트폴리오를 연결하는 실전 운용 핵심 파일이다.

### 4.4 `generate_rebalance_report.py`
- 입력:
  - `actions_csv`
  - `features_live`
  - `execution_plan`
  - `supp_file` 여러 개
  - `strategy`
  - `asof`
  - `target_date`
  - `output_md`
  - `output_csv`
  - `output_html`
  - `scores_csv` (옵션)
- 동작:
  1. 액션과 features를 ticker 기준 병합
  2. 정확한 scores csv 자동 감지 시 merge
  3. execution plan merge
  4. supplemental 파일 merge
  5. industry reference 보강
  6. valuation 파생값 계산
  7. score breakdown 계산
  8. markdown / csv / html 보고서 출력

> 정리 포인트: 보고서 생성기는 `supp_file`에 무엇을 넣느냐에 따라 출력 품질이 달라진다.  
> GitHub 문서에는 **필수 supp_file / 선택 supp_file**를 명확히 구분해 둬야 한다.

---

## 5. 데이터/산출물 정리 관점

현재까지 드러난 산출물 위치는 주로 다음과 같다.

- `data/processed`
- `data/features`
- `data/live`
- `data/live/actions`
- `data/live/reports`
- `data/archive` 또는 `archive/old_versions` 계열
- `current_portfolio`

정리 원칙 초안:

### 5.1 남겨야 하는 것
1. **현행 파이프라인에서 직접 참조되는 코드**
2. **재생산이 오래 걸리는 원천/중간 데이터**
3. **실전 운용 재현에 필요한 최신 산출물**
4. **백테스트 핵심 결과 요약본**
5. **GitHub에 필요한 README / docs / 예시 실행 명령어**

### 5.2 치워야 하는 것
1. 중복 패치 파일
2. 테스트 중간 버전이지만 최종 채택되지 않은 스크립트
3. 동일 목적의 중복 보고서 산출물
4. 더 이상 참조되지 않는 임시 CSV / HTML / MD
5. archive에 이미 들어간 오래된 중복본

### 5.3 바로 삭제하지 말 것
다음은 무조건 **삭제 전에 참조 관계 확인**이 필요하다.

- `generate_rebalance_report*.py` 패치본들
- `collect_krx_marketdata_fixed_v*.py`
- `make_features_live_fixed_v*.py`
- 과거 버전 `fundamentals_quarterly`, `features_live`, `universe`, `scores`, `live_actions`
- 보고서 생성기에 supplemental로 넣는 참조 CSV / parquet

---

## 6. 2026-04-11 기준 권장 작업 순서

### 단계 A. 로컬 구조 확정
1. `C:\Users\chanw\ai_inv_adv` 전체 트리 출력
2. `scripts`, `data`, `docs`, `current_portfolio` 중심으로 구조 캡처
3. 최종 실행 대상 파일과 패치 후보 파일 구분

권장 PowerShell 예시:
```powershell
cd C:\Users\chanw\ai_inv_adv
tree /F /A > .\docs\project_tree_2026-04-11.txt

Get-ChildItem .\scripts -Recurse -File |
  Select-Object FullName |
  Out-File .\docs\scripts_inventory_2026-04-11.txt -Encoding utf8

Get-ChildItem .\data -Recurse -File |
  Select-Object FullName, Length, LastWriteTime |
  Sort-Object FullName |
  Out-File .\docs\data_inventory_2026-04-11.txt -Encoding utf8
```

### 단계 B. 전략 백테스트 파이프라인 재실행
1. 데이터 수집 스크립트 확인
2. 유니버스/팩터 생성
3. 스코어 계산
4. 백테스트 실행
5. 수익률 비교 및 요약 산출물 생성

### 단계 C. 보고서 생성기 파이프라인 재실행
1. 데이터 최신화
2. features_live 생성
3. latest score 산출
4. live actions 생성
5. execution plan 생성
6. 직전 리밸런싱 성과 평가
7. 리밸런싱 보고서 생성

### 단계 D. 참조 관계 문서화
각 단계마다 아래를 문서화:
- 입력 파일
- 출력 파일
- 의존 코드
- 선택 옵션
- 실패 시 fallback

### 단계 E. 정리/삭제 후보 분류
- KEEP
- ARCHIVE
- DELETE_CANDIDATE
세 범주로 나눠 정리

### 단계 F. GitHub 업로드 정리
1. README 갱신
2. docs 갱신
3. 실행 예시 명령어 갱신
4. 불필요 대용량/민감파일 제외
5. `.gitignore` 점검

### 단계 G. AI 모듈 설계/검증
1. 학습 데이터셋 정의
2. 타깃 라벨 정의
3. 학습/검증/워크포워드 방식 결정
4. 확률 또는 클래스 출력
5. 기존 스코어링 모델 앞단 필터로 접목
6. 백테스트 비교
7. 분기별 성과 비교
8. 최종 채택 여부 결정

---

## 7. AI 분류 모듈 설계 초안

### 7.1 목적
유니버스 종목이 **다음 분기 리밸런싱 전까지 상승할지 하락할지**를 분류한다.

### 7.2 입력 후보
기존 스코어 계산에 사용되는 변수들을 우선 사용한다.
예상 후보:
- `OpIncome_acc2`
- `OpIncome_acc2_log1p`
- `op_growth_streak2`
- `Revenue_acc2`
- `rev_growth_streak2`
- `Debt_to_Equity_log`
- `Quality_CFO_to_Assets`
- `CFO_isnull`
- 필요 시 valuation / liquidity / sector 정보 보강

### 7.3 라벨 정의 초안
리밸런싱 기준 시점 `t`에서 종목 `i`에 대해,
다음 리밸런싱 직전 또는 다음 리밸런싱 시점 `t+1`까지의 누적 수익률이 0보다 크면 `1`, 아니면 `0`.

예시:
- `label_up = 1 if forward_return_q > 0 else 0`

### 7.4 적용 방식 초안
기존 방식:
1. 팩터 스코어 계산
2. 점수순 정렬
3. Top-K 선정

후보 방식:
1. 팩터 스코어 계산
2. AI 분류기 확률 산출
3. `상승` 또는 `P(up) >= threshold`만 통과
4. 남은 종목에서 스코어 기준 Top-K 선정

### 7.5 검증 포인트
- 단순 정확도보다 **포트폴리오 수익률 개선**이 핵심
- 상승 종목 필터링으로 인해 분산이 과도하게 줄어들지 않는지 확인
- 분기별 성과 안정성
- 거래회전율 변화
- 특정 섹터 편중 악화 여부
- 과최적화 방지를 위한 walk-forward 검증 필수

---

## 8. AI 모듈 검증 시 꼭 봐야 할 비교표

최소 아래 비교는 문서화한다.

1. **기존 모델 vs AI 필터 적용 모델**
2. 지표:
   - CAGR
   - 누적수익률
   - 샤프
   - MDD
   - 승률
   - 분기별 평균 수익률
   - 분기별 표준편차
   - turnover
3. 서브 분석:
   - 분기별 초과수익
   - 상승장 / 하락장 구간 성과
   - 필터 통과율
   - Top-K 후보 부족 발생 빈도
   - 산업/시총 편향 변화

---

## 9. GitHub 정리 전 체크리스트

- [ ] 최종 실행 스크립트 이름 확정
- [ ] 패치본/실험본 제거 또는 archive 이동
- [ ] docs에 실행 순서 기재
- [ ] 샘플 명령어 최신화
- [ ] 민감정보 제거
- [ ] API 키 / 개인 경로 / 계좌 관련 정보 제거
- [ ] 대용량 parquet / html / report 파일 업로드 범위 결정
- [ ] `.gitignore` 정리
- [ ] 최소 실행 예시 1개 보장
- [ ] AI 모듈 브랜치 전략 결정 (`main` 분리 여부)

---

## 10. 지금 바로 해야 할 첫 작업

가장 먼저 할 일:

1. 로컬 저장소 구조를 텍스트로 덤프한다.
2. 현행 파이프라인에서 실제 사용하는 최종 스크립트를 확정한다.
3. 백테스트 1회, 보고서 생성기 1회를 **2026-04-11 기준으로 end-to-end 재실행**한다.
4. 각 단계의 입출력 파일을 문서에 반영한다.
5. 그 다음에만 삭제 후보를 건드린다.

