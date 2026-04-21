# Live Rebalance Runbook

## 🎯 목적

본 문서는 실전 리밸런싱을 **안전하게, 재현 가능하게, 일관된 방식으로 실행**하기 위한 표준 절차를 정의한다.

핵심 원칙:

- 동일 입력 → 동일 결과
- 자동 실행 + 자동 검증
- 오류 발생 시 즉시 중단
- **이미 성공한 산출물은 최대한 재활용**
- **API 호출이 큰 단계(fundamentals)는 불필요 재실행 금지**

---

## 📁 디렉터리 구조 (핵심)

```text
data/
├─ portfolio/
│  ├─ current/     # 현재 운용 포트폴리오 (입력)
│  └─ history/     # 과거 계좌 스냅샷
├─ live/
│  ├─ actions/
│  ├─ candidates/
│  ├─ reports/
│  └─ scores/
├─ features/
│  ├─ features_live/
│  └─ features_phase1__*.parquet
├─ processed/
│  ├─ fundamentals_quarterly__*.parquet
│  ├─ factors_ttm_acc2__*.parquet
│  ├─ prices_daily__*.parquet
│  ├─ returns_monthly__*.parquet
│  ├─ krx_marketdata__*.parquet
│  └─ execution_plan__*.csv

---

## 🧾 입력 데이터

### 1. 현재 보유 포트폴리오
data/portfolio/current/YYYYMMDD_holdings_clean.csv

필수 컬럼:

- ticker (6자리)
- name
- shares

---

## ⚙️ 실행 방법

### 🔥 단일 실행 명령

```powershell
.\scripts\live\run_live_rebalance.ps1 `
  -ASOF 2026-03-29 `
  -TARGET 2026-03-31 `
  -HOLDINGS .\data\portfolio\current\20260330_holdings_clean.csv `
  -FEATV 3291 `
  -ACTIONV 3291 `
  -REPORTV 3291

🔄 실행 흐름

Score 계산
매수/매도 액션 생성
Execution plan 생성
리포트 생성
결과 검증 (validate_run_outputs.py)

✅ 검증 항목

자동으로 다음 항목을 체크한다:

features_live 존재
scores 파일 존재
actions 생성 여부
execution_plan 금액 합계 검증
report/md/html 생성 여부
holdings 구조 정상

🚨 실패 처리

다음 경우 즉시 실행 중단:

파일 누락
금액 불일치
holdings 오류
actions 비어 있음

📊 결과 파일

핵심 출력

data/live/actions/
data/live/reports/
data/live/scores/
data/processed/execution_plan__*.csv

🧠 운영 원칙

1. holdings는 반드시 명시 입력

자동 탐색 금지

2. 버전 명시 (feat_v, action_v, report_v)

latest 사용 금지

3. 실행 후 반드시 리포트 확인

자동화 ≠ 무검증

🔁 리밸런싱 후 작업

실제 체결 완료
계좌 상태 저장
data/portfolio/history/YYYYMMDD.xlsx

다음 분기용 current 업데이트
data/portfolio/current/YYYYMMDD_holdings_clean.csv

🏁 결론

이 시스템은 다음을 보장한다:

실전에서 오류 발생 방지
결과 재현 가능성 확보
일관된 리밸런싱 수행