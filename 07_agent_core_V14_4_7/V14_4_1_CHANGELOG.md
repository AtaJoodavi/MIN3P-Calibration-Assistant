# V14.4.1 Changelog

V14.4.1 is a narrow reliability/efficiency patch on top of V14.4.

## Modified production files

1. `modules/adaptive_coordinate_optimizer_V14.py`
   - version -> V14.4.1
   - removes the duplicate delegated write to `v14_optimizer_events.xlsx`
   - adds `count_as_valid_run` so cache reuse advances the search without pretending a new MIN3P run occurred

2. `modules/v14_transaction_manager.py`
   - version -> V14.4.1
   - adds semantic workbook hashing
   - adds evaluation-context hashing
   - adds persistent `v14_candidate_cache.jsonl`
   - registers the accepted baseline and valid candidate evaluations
   - reuses exact previously evaluated candidates
   - bootstraps cache lookup from `run_ranking.xlsx` only for run folders referenced by the current V14 decision log and only when every parameter value matches
   - audits cache hits without changing canonical/best rollback semantics

3. `min3p_ai_pipeline_V14.py`
   - version -> V14.4.1
   - propagates V14.4 step metadata into `calibration_decision_log.xlsx`
   - includes the same metadata in `event_json`
   - checks the exact-candidate cache before launching MIN3P
   - does not increment physical valid-run counters for cache hits

## New audit fields in calibration_decision_log.xlsx

- `move_space`
- `step_basis`
- `range_normalized`
- `configured_log_range_decades`
- `factor_applied`
- `cache_hit`
- `cache_source_candidate_id`
- `cache_source_transaction`
- `cache_source`

## New file

- `04_results/v14_candidate_cache.jsonl`

## Unchanged

- objective function
- acceptance/rejection thresholds
- parameter bounds
- directional pair precedence
- freeze/reactivation rules
- transaction rollback/recovery rules
- GPT remains advisory only
