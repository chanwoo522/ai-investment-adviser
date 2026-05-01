# Data Integrity TODO

## Purpose

This document fixes the current data-integrity backlog that must be resolved before the `2026-04-15` result can be treated as final.

## Current Status

- current result status: `provisional = true`
- current fallback source: `fallback_seed_from_2026-03-31`

## Required Recovery Items

### 1. KRX primary collection recovery

Must restore:

- live `krx_master`
- live `krx_marketdata`
- stable `prices_daily` / `returns_monthly` input lineage

### 2. DART endpoint / API collection normalization

Must confirm:

- DART endpoint access is stable
- DART collection does not rely on silent fallback behavior
- fundamentals and shares / industry collection are reproducible

### 3. Remove fallback dependency

Must remove:

- reliance on `fallback_seed_from_2026-03-31`

Goal:

- rerun with primary collection path only

## Mandatory Rerun After Primary Recovery

After KRX / DART primary recovery, rerun in this order:

1. `prepare_asof`
2. feature stack
3. `run_strategy_compare`
4. confirm:
   - full-period winner
   - common-period winner

## Expected Validation Outcome

The rerun should confirm whether:

- `factor_composite` remains the full-period winner
- `factor_composite` remains the common-period winner

If the winner changes, update:

- decision document
- release note
- README recommendation text

## Until Integrity Recovery Is Complete

Until the above recovery is complete:

- mark results as `provisional`
- keep fallback source visible
- avoid presenting the result as final production truth
