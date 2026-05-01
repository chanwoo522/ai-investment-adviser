# AI Investment Adviser

AI Investment Adviser (`ai_inv_adv`) is a Korean equity systematic investing project built around reproducible data collection, factor feature engineering, quarterly rebalancing, and live-action reporting.

The repository is organized so that a collaborator can rebuild the same pipeline from raw data collection through feature generation, strategy comparison, and live rebalance reporting with the same commands.

## Project Purpose

- Build a reproducible Korean equity investing workflow from data collection to rebalance report generation.
- Compare quant-only, composite-factor, and AI-assisted portfolio selection variants on the same quarterly backtest framework.
- Operate `factor_composite` as the current main live strategy candidate while preserving the legacy production strategy logic.

## Core Strategy Philosophy

- Earnings acceleration matters more than simple top-line growth.
- A usable live strategy must combine signal strength with quality control, liquidity filters, and turnover awareness.
- AI-based ranking can be useful, but it is treated as a candidate overlay and must be validated against overfitting risk.

## Main Strategies

### 1. `quant_only`

- Pure quant Top-K selection from the base score.
- Uses `score_base` or `score_total_base`.
- Intended as the clean baseline for comparison.

### 2. `factor_composite`

- Current main live strategy candidate.
- Combines:
  - quant base score
  - quality soft penalty
  - holding bonus
  - practical live filters
- In current configuration, this is the operational live alias of the legacy production strategy family centered on `D_quality_filter_debt_profitaccel_liq`.

### 3. `ai_wf_topk`

- Walk-forward AI directly reselects Top-K holdings.
- Strong experimental strategy in the 10-year walk-forward comparison.
- Must be treated carefully because higher performance can come with higher overfitting risk and turnover.

### 4. `combo_overlay_replace`

- Combines the composite strategy with the walk-forward AI strategy.
- Intended as a defensive or blended allocation candidate rather than the default main strategy.

## Repository Layout

- `scripts/`
  - `data_pipeline/`: raw data collection and feature preparation
  - `backtest/`: quarterly and robustness backtests
  - `live/`: scoring, actions, execution plan, reporting
  - `factor_weight_ml/`: AI factor-weight and walk-forward components
- `common/`
  - shared factor, industry, and portfolio utilities
- `configs/`
  - strategy and execution configuration
- `docs/`
  - runbooks and reproduction guides
- `run_full_live_rebalance.ps1`
  - existing end-to-end live rebalance wrapper

## Environment

- Python `3.12` recommended
- Windows PowerShell recommended for the provided wrappers
- Install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## External Dependencies

The project depends on external Korean market and filing sources.

- `KRX / pykrx`
  - used for market master, market data, price history, and benchmark series
- `DART / OpenDartReader`
  - used for financial statements, shares outstanding, and industry metadata

You must provide:

```powershell
$env:DART_API_KEY="YOUR_DART_KEY"
```

## Data Regeneration Procedure

Full regeneration for an `asof` date is orchestrated by `prepare_asof.py`.

Example:

```powershell
.\.venv\Scripts\python.exe .\scripts\data_pipeline\prepare_asof.py `
  --asof 2026-04-15 `
  --metric revenue_op `
  --universe_v 1 `
  --mcap_top 800 `
  --trd_bot 0.1 `
  --px_v 1 `
  --ret_v 1 `
  --lookback_years 15 `
  --start 20110101 `
  --fund_v 1 `
  --factor_in_v 1 `
  --factor_out_v 3291 `
  --fs_div CFS `
  --with_shares_industry `
  --shares_out_v 9201 `
  --fund_mode union `
  --allow_fallback
```

## Feature Stack Procedure

After fundamentals are ready, build the downstream feature stack:

```powershell
.\.venv\Scripts\python.exe .\scripts\data_pipeline\build_features_phase1.py --asof 2026-04-15
.\.venv\Scripts\python.exe .\scripts\data_pipeline\build_factors_ttm_acc2.py --asof 2026-04-15 --metric revenue_op --in_v 1 --out_v 3291
.\.venv\Scripts\python.exe .\scripts\data_pipeline\make_features_live.py --asof 2026-04-15 --metric revenue_op --in_v 3291 --out_v 3291 --save_meta
```

## 4-Strategy Comparison Command

If the feature stack is already ready, reuse it and skip internal preparation:

```powershell
.\.venv\Scripts\python.exe .\scripts\backtest\run_strategy_compare.py `
  --skip_prepare `
  --asof 2026-04-15 `
  --metric revenue_op `
  --strategy D_quality_filter_debt_profitaccel_liq `
  --k 10 `
  --feat_v 3291 `
  --ret_v 1 `
  --factor_v 3291 `
  --raw_px_v 1 `
  --model ridge `
  --feature_profile compact_dailyagg `
  --min_train_rows 20 `
  --valid_rows 4 `
  --temperature 1.0 `
  --cap_profit_accel_delta -1.0 `
  --ai_overlay_strength 1.0 `
  --mcap_top 800 `
  --trd_bot 0.1 `
  --start 20110101 `
  --lookback_years 15 `
  --out_dir artifacts\strategy_compare\asof=2026-04-15
```

## Live Factor Composite Pipeline

The dedicated wrapper for the current main live strategy candidate is:

- `scripts/live/run_factor_composite_pipeline.ps1`

Guide:

- [docs/run_factor_composite_pipeline.md](docs/run_factor_composite_pipeline.md)

Reproduction record for the current provisional build:

- [docs/reproduce_2026_04_15.md](docs/reproduce_2026_04_15.md)

## Provisional / Fallback Warning

If `krx_master` cannot be collected live and the pipeline uses a fallback seed, the result must be treated as provisional.

Current known provisional case:

- `provisional=true`
- `fallback_seed_from_2026-03-31`

In that case:

- reports and metadata should carry the fallback source
- results should be treated as temporary
- the same command should be rerun once primary KRX collection is restored

## GitHub Packaging Rule

This repository should track code and documentation, not local generated outputs.

Include:

- `scripts/`
- `common/`
- `configs/`
- `docs/`
- `requirements.txt`
- `README.md`
- runnable `.ps1` wrappers

Exclude:

- `data/`
- `artifacts/`
- `logs/`
- `.venv/`
- `__pycache__/`
- temporary `csv/parquet/json/log` outputs
- local backups and copied archives

## Current Main Recommendation

- Keep `factor_composite` as the main live strategy candidate.
- Keep `ai_wf_topk` as a satellite or bounded overlay candidate.
- Keep `combo_overlay_replace` as an experimental blended strategy, not the default main strategy.
