# =========================
# 0. 위치 확인
# =========================
cd C:\Users\chanw\ai_inv_adv\dist\ai_inv_adv_github_min
& .\.venv\Scripts\Activate.ps1

# =========================
# 1. .gitignore 생성
# =========================
@'
.venv/
__pycache__/
*.pyc

data/raw/
data/archive/
logs/
artifacts/

*.parquet
*.log

!data/sample/
!artifacts/*.md
!artifacts/*.html
!artifacts/*.csv
'@ | Set-Content .\.gitignore -Encoding UTF8

# =========================
# 2. runbook 파일 생성
# =========================
@'
# Live Rebalance Runbook

## 목적

본 문서는 실전 리밸런싱을 안전하고 재현 가능하게 실행하기 위한 표준 절차를 정의한다.

핵심 원칙:

- 동일 입력 → 동일 결과
- 자동 실행 + 자동 검증
- 오류 발생 시 즉시 중단
- 이미 성공한 산출물은 최대한 재활용
- API 호출이 큰 단계(fundamentals)는 불필요 재실행 금지

---

## 입력 데이터

data/processed/current_holdings_manual.csv

필수 컬럼:
- ticker
- name
- shares

---

## 모드 설명

dryrun  → 실행 없이 명령만 출력  
refresh → 전체 재생성  
smart   → 기존 결과 재활용  
fast    → 최소 단계 실행  

---

## smart vs missing_only

smart = 전체 파이프라인 실행 전략  
missing_only = fundamentals 증분 수집  

---

## 실행 예시

.\run_full_live_rebalance.ps1 `
  -Mode smart `
  -ASOF "2026-04-12" `
  -TARGET "2026-03-31" `
  -METRIC "revenue_op" `
  -STRAT "D_quality_filter_debt_profitaccel_liq" `
  -FACTOR_V 3291 `
  -FEAT_V 3291 `
  -ACTION_V 1 `
  -EXEC_V 1 `
  -REPORT_V 1 `
  -TOTAL_VALUE 100000000 `
  -HOLDINGS_CSV ".\data\processed\current_holdings_manual.csv"

---

## 주의사항

- PowerShell 변수는 ${VAR} 형태 사용
- target_date는 실제 존재하는 리밸런싱 날짜 사용
- DART 한도 초과 시 후반부만 실행
'@ | Set-Content .\runbook_run_full_live_rebalance.ps1.md -Encoding UTF8

# =========================
# 3. 파일 생성 확인
# =========================
Get-ChildItem .\.gitignore, .\runbook_run_full_live_rebalance.ps1.md

# =========================
# 4. 원격 최신 반영
# =========================
git fetch origin
git pull --rebase origin main

# =========================
# 5. 커밋 & push
# =========================
git add .gitignore runbook_run_full_live_rebalance.ps1.md
git commit -m "Add runbook and repository hygiene"
git push
