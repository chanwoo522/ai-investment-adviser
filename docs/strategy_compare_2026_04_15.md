# Strategy Comparison 2026-04-15

## Purpose

This document records the `2026-04-15` strategy comparison run used to decide the current main live strategy direction.

The comparison includes:

1. `quant_only`
2. `factor_composite`
3. `ai_wf_topk`
4. `combo_overlay_replace`
5. `factor_composite_ai_filter`
6. `factor_composite_ai_overlay`
7. `factor_composite_ai_limited_replace`

## Data Status

This run is **not final production truth**.

- `provisional = true`
- `fallback source = fallback_seed_from_2026-03-31`

Reason:

- `KRX master` for `2026-04-15` was not collected from the primary path.
- The run used a fallback seed snapshot.
- DART / KRX primary collection should be restored and the same comparison rerun.

## Evaluation Views

Two views were used.

### 1. Full-period comparison

This uses each strategy's own available evaluation range.

Important:

- Full-period metrics are **not all based on the exact same month range**.
- `ai_wf_topk` and `combo_overlay_replace` have shorter effective windows than `factor_composite`.
- `strategy_comparison.csv` therefore includes:
  - `eval_months`
  - `eval_periods`
  - `trained_only_eval`
  - `trained_months`
  - `trained_periods`
  - `fallback_baseline_included`
  - `eval_start_month`
  - `eval_end_month`

### 2. Common-period comparison

To make the comparison fairer, all strategies were also sliced to the common window:

- `common_eval_start_month = 2016-05-31`
- `common_eval_end_month = 2025-10-31`

The following recomputed metrics were added:

- `net_nav_common`
- `cagr_common`
- `sharpe_common`
- `mdd_common`

## Final Result

### Full-period winner

- `factor_composite`

### Common-period winner

- `factor_composite`

## Why factor_composite remained the winner

`factor_composite` remained the strongest choice in both views.

Key interpretation:

- It kept the highest overall wealth creation in both the original comparison and the common-period comparison.
- It remained competitive on Sharpe while using the existing practical live selection logic.
- It avoided giving AI full authority over portfolio replacement.
- It stayed more stable than the experimental AI-assisted variants once the comparison was normalized to the same evaluation window.

## Why the AI variants were not adopted

### `ai_wf_topk`

- Strong experimental strategy.
- However, it is a fully AI-led selector.
- It uses a different effective evaluation window from the baseline full-period comparison.
- It also includes a pre-trained fallback phase in the full-period result.
- Because of this, it is not adopted as the main live strategy.

### `factor_composite_ai_filter`

- AI changed too many names relative to the baseline.
- Overlap with `factor_composite` dropped materially.
- It did not improve enough in NAV or Sharpe to justify the added complexity.

### `factor_composite_ai_overlay`

- This was the most conservative AI scoring variant.
- It preserved turnover reasonably well.
- But it still did not beat `factor_composite` on the main score dimensions.

### `factor_composite_ai_limited_replace`

- This was the strongest of the controlled AI auxiliary variants.
- It kept average replacement count lower than the other AI auxiliary variants.
- Even so, it still did not beat `factor_composite` in the final comparison.

## Adoption Decision

Current decision:

- Main live strategy: `factor_composite`
- AI strategies: `experimental`
- `ai_wf_topk`: satellite or research-only candidate
- `factor_composite_ai_filter`: not adopted
- `factor_composite_ai_overlay`: not adopted
- `factor_composite_ai_limited_replace`: not adopted for now
- `combo_overlay_replace`: retained as experimental blend, not main

## Required Follow-up

Before final production confidence is claimed, rerun after primary collection recovery:

1. restore primary `KRX master` collection for `2026-04-15` or newer live run dates
2. restore final DART / KRX source integrity
3. rerun `prepare_asof`
4. rebuild feature stack
5. rerun `run_strategy_compare`
6. confirm that both:
   - full-period winner
   - common-period winner
   remain `factor_composite`

## Output Files

Primary outputs for this run:

- `artifacts/strategy_compare/asof=2026-04-15/strategy_comparison.csv`
- `artifacts/strategy_compare/asof=2026-04-15/best_strategy.json`

These outputs are generated artifacts and are **not** Git tracking targets.
