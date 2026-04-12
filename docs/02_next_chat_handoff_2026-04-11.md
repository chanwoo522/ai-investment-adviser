# ai_inv_adv 다음 대화용 핸드오프 프롬프트 (기준일: 2026-04-11)

아래 내용을 그대로 다음 대화창 첫 메시지로 사용하면 된다.

---

나는 `ai_inv_adv` 프로젝트를 계속 진행 중이다.

## 현재 프로젝트 상태
`ai_inv_adv`는 어느 정도 완성되었고, 현재 모델은 크게 두 기능으로 구성되어 있다.

1. **전략 백테스트**
2. **보고서 생성기**

### 전략 백테스트 파이프라인
대략 다음 순서다.
- 데이터 수집
- 스코어 계산
- 수익률 비교
- 평가 및 보고

### 보고서 생성기 파이프라인
대략 다음 순서다.
- 데이터 수집
- 스코어 계산
- 액션 플랜 수립
- 직전 리밸런싱 성과 평가
- 보고서 생성

## 지금부터 해야 할 목표
기준일은 **2026-04-11**이다.

1. 전략 백테스트와 보고서 생성기까지 **모두 end-to-end로 실행**한다.
2. 파이프라인 가동 중 참조하는 **코드 파일 / 데이터 파일 / 중간 산출물**을 추적한다.
3. **불필요한 파일은 삭제 후보로 분류**한다.
4. GitHub에 올릴 수 있도록 **docs / README / 실행 순서**를 정리한다.
5. 그 다음 **AI 분류 모듈**을 추가한다.

## AI 분류 모듈 목표
데이터 수집 이후, 스코어 계산에 쓰는 변수들을 입력으로 사용하는 AI 모듈을 만들고 싶다.

이 AI 모듈은 유니버스 내 종목이 **다음 분기 리밸런싱 전까지 주가가 상승할지 하락할지**를 학습하고 예측하는 분류기다.

현재 종목 선정 방식은 **스코어링 + TOP-K** 방식이므로,
AI 모듈은 **상승 예측 종목만 통과시키는 필터** 후보로 검토하고 싶다.

즉, 후보 흐름은 대략 다음과 같다.
1. 기존 팩터 스코어 계산
2. AI 분류기 예측
3. 상승 예측 종목만 남김
4. 남은 종목에서 score 기준 TOP-K 선정

이 방식으로 백테스트도 해보고,
**분기별 수익률**과 전체 성과를 기존 모델과 비교한 뒤
현재 모델에 붙일지 말지 결정하고 싶다.

## 현재까지 파악된 핵심 파일
파일 라이브러리/이전 대화 기준으로 핵심 후보는 다음과 같다.

### 데이터 수집 / 기초 데이터
- `collect_fundamentals_quarterly.py`
- `collect_krx_marketdata.py`
- `collect_dart_shares_industry.py`
- `make_prices_daily.py`

### 유니버스 / 팩터
- `build_universe.py`
- `build_factors_ttm_acc2.py`
- `make_features_live.py` 계열

### 스코어 / 리밸런싱 / 액션
- `score_latest_rebalance.py`
- `generate_live_actions.py`
- `make_order_sheet.py`

### 보고서
- `generate_rebalance_report.py`
- 패치본들:
  - `generate_rebalance_report_patched.py`
  - `generate_rebalance_report_final_patched.py`
  - `generate_rebalance_report_final_patched_v2.py`

## 중요하게 기억할 점
- `generate_rebalance_report.py` 계열 패치본이 여러 개 있으니 **최종 채택본 1개를 확정**해야 한다.
- `build_universe.py`와 `make_prices_daily.py`는 여러 `data/processed` 산출물에 의존한다.
- `generate_rebalance_report.py`는 `actions_csv`, `features_live`, `execution_plan`, `supp_file`, `scores_csv` 등을 받아서 md/csv/html 보고서를 만든다.
- 삭제는 나중에 하고, 먼저 **참조 관계를 문서화**해야 한다.

## 이번 대화에서 우선 해줬으면 하는 일
1. `C:\Users\chanw\ai_inv_adv` 기준으로 전체 디렉터리 구조와 핵심 파일 구조를 정리해줘.
2. 전략 백테스트 파이프라인과 보고서 생성기 파이프라인을 **실행 순서 기준으로 재구성**해줘.
3. 각 단계별로
   - 입력 파일
   - 출력 파일
   - 호출 스크립트
   - 삭제 금지 파일
   를 표처럼 정리해줘.
4. GitHub 업로드 전에 docs에 넣을 문서를 갱신할 수 있게
   - README 개요
   - 실행 가이드
   - AI 모듈 추가 계획
   까지 정리해줘.
5. 마지막에는
   - 즉시 실행할 명령어
   - 삭제 후보 확인 명령어
   - 구조 덤프 명령어
   까지 PowerShell 복붙 형태로 줘.

## 참고
문서 초안은 이미 만들어 둔 상태다.
- `01_master_runbook_2026-04-11.md`
- `02_next_chat_handoff_2026-04-11.md`

이 문서 내용과 이번 대화 맥락을 이어서, 누락 없이 다음 작업을 진행해줘.

---

