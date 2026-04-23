# AI Investment Adviser

## 📌 Overview
AI Investment Adviser는 한국 주식 시장을 대상으로 한  
**퀀트 기반 실전 투자 시스템**입니다.

핵심 철학:
> 매출 성장보다 이익의 가속이 더 중요하다

---

## 🧠 Strategy Concept

### Core Factors
- **OpIncome_acc2 (핵심)**  
  → 영업이익 2차 가속도

- **Revenue_acc2 (보조)**  
  → 매출 가속

- **Debt Control (필터)**  
  → 과도한 레버리지 제거

- **Growth Streak Bonus**
  → 연속 성장 기업 가산점

---

## ⚙️ Pipeline

Data Collection → Feature Engineering → Scoring → Ranking (Top-K) → Rebalancing → Execution Plan → Report Generation

---

## 📂 Project Structure

ai_inv_adv/
├── configs/
├── scripts/
│   ├── data_pipeline/
│   ├── backtest/
│   ├── live/
│   ├── factor_weight_ml/
│   └── ai_filter/
├── docs/
├── reference/
└── run_full_live_rebalance.ps1

---

## 🚀 Live Rebalance Execution

```powershell
.\run_full_live_rebalance.ps1 `
  -Mode smart `
  -ASOF 2026-04-20 `
  -TARGET 2026-03-31 `
  -METRIC revenue_op `
  -STRAT D_quality_filter_debt_profitaccel_liq `
  -K 10
```

---

## 📊 Output

- Top-K portfolio
- Execution plan
- Performance tracking
- Rebalance report

---

## 🧪 Current Status

- Data pipeline stabilized
- Live rebalance pipeline operational
- Reporting system implemented
- Git repository cleaned and production-ready

---

## 🔮 Next Steps

- Factor Weight ML
- AI Filter Module
- Execution Enhancement

---

## 📈 Philosophy

Price converges to earnings.  
Multiples are temporary.
