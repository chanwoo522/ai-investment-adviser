# Parameterized quarterly advisor pipeline

`scripts/advisor/run_quarterly_advisor_pipeline.py` runs an immutable
`ADVISOR_FULL_RESET` advisory report. It does not submit orders, manage fills,
require orderable cash, or create execution-phase artifacts.

## Inputs and invariants

The broker workbook, account date, model information cutoff, reference-price
cutoff, performance interval, quarter label, report title, previous portfolio
evidence, capital certification, and output root are explicit CLI inputs. The
strategy definition remains the production definition, while the fresh-start
entry point enforces `holding_bonus=0`, `keep_current_top_n=0`, and no current
account membership in score, rank, or Top-K selection.

The runner uses one immutable staging directory:

```text
<output-root>/.run_id=<run-id>.staging
```

It publishes by atomic rename to:

```text
<output-root>/run_id=<run-id>
```

Neither path may already exist. A failed run removes only the exact staging
directory it created. The raw broker workbook is never copied into staging or
either ZIP bundle.

## Model input modes

- `existing-certified-run` reads the production score artifact named in the
  certified run manifest, builds the existing score-parity bundle, and
  materializes the certified fresh-start projection for exact replay.
- `collect` invokes `scripts/data_pipeline/prepare_asof.py`, then calls the
  same fresh-start scoring callable. Missing collector outputs fail closed.

Financial report metrics must come from an upstream certified generic
financial/full-security run covering the selected-plus-dropped union. A source
run with missing tickers or incomplete EPS, net-income, PER, PBR/PSR, or CFO
diagnostics fails closed. `--use-network-if-missing` allows the existing market
and model collectors; it does not permit an external EPS/PER substitute.

## Validate-only

`--validate-only` checks paths, date order, strategy configuration, capital
basis, source-run mode, callable availability, API-key state, output collision,
and protected source identities. It creates no directory or artifact.

```powershell
.\.venv\Scripts\python.exe scripts\advisor\run_quarterly_advisor_pipeline.py `
  --broker-account-file 'C:\private\latest-account.xlsx' `
  --account-asof 'YYYY-MM-DD' `
  --model-information-asof 'YYYY-MM-DD' `
  --reference-price-asof 'YYYY-MM-DD' `
  --performance-start 'YYYY-MM-DD' `
  --performance-end 'YYYY-MM-DD' `
  --quarter-label 'YYQn' `
  --report-title '분기 리밸런싱 제안' `
  --previous-portfolio-evidence 'C:\private\previous-portfolio.csv' `
  --output-root 'C:\private\advisor-runs' `
  --run-id 'advisor_YYQn_YYYYMMDD' `
  --strategy-config 'configs\quarterly_advisor_pipeline.json' `
  --target-cash-weight 0.10 `
  --top-k 10 `
  --model-input-mode existing-certified-run `
  --source-model-run-dir 'C:\immutable\certified-model-run' `
  --capital-basis-file 'C:\private\advisory-capital.json' `
  --validate-only
```

Remove `--validate-only` only after the validation result is `PASS_VALIDATE_ONLY`.

For a new quarter in collection mode, use the same explicit inputs and replace
only the quarter-specific values:

```powershell
.\.venv\Scripts\python.exe scripts\advisor\run_quarterly_advisor_pipeline.py `
  --broker-account-file 'C:\private\latest-account.xlsx' `
  --account-asof 'YYYY-MM-DD' `
  --model-information-asof 'YYYY-MM-DD' `
  --reference-price-asof 'YYYY-MM-DD' `
  --performance-start 'YYYY-MM-DD' `
  --performance-end 'YYYY-MM-DD' `
  --quarter-label 'YYQn' `
  --report-title '퀀트 스크리닝 성장 가속 YYQn 리밸런싱 제안' `
  --previous-portfolio-evidence 'C:\private\previous-portfolio.csv' `
  --output-root 'C:\private\advisor-runs' `
  --run-id 'advisor_YYQn_YYYYMMDD' `
  --strategy-config 'configs\quarterly_advisor_pipeline.json' `
  --target-cash-weight 0.10 `
  --top-k 10 `
  --model-input-mode collect `
  --capital-basis-file 'C:\private\advisory-capital.json'
```

Exactly one of `--capital-basis-file` and `--advisory-capital-krw` is required.
The amount must reconcile to the imported full-account liquidation capital;
missing cash or cash-equivalent assets are never synthesized.

## Main artifacts

The run contains canonical account import aliases, current portfolio,
hypothetical full liquidation, advisory capital summary, fresh-start scores and
Top-K, target portfolio, current-vs-target, prior-model and provisional actual
account performance, selected/dropped financial details, HTML/PDF, subscriber
QA, public distribution bundle, private audit bundle, and run manifest.

`reference_target_qty` is a reference quantity, never an order quantity. The
runner does not emit `planned_qty`, `order_qty`, `trade_qty`, or
`executable_qty`.
