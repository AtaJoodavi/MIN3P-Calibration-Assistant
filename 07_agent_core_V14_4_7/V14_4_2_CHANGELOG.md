# V14.4.2 Changelog

V14.4.2 builds on V14.4.1 without changing the objective function, acceptance thresholds, transaction rollback, or MIN3P scientific constraints.

## Calibration changes

1. Immediate local refinement after a promising directional bracket
   - After an accepted improvement is followed by a failed continuation and failed opposite-direction test, the parameter receives one protected reduced-step symmetric refinement pair.
   - That protected pair is selected ahead of unrelated stage peers with larger unexplored steps.
   - An unfinished direction family remains the highest-priority invariant and cannot be interrupted.
   - After the protected pair is completed, ordinary stage-wide largest-step ordering resumes unless a new accepted improvement creates a new bracket.

2. V14.4.1 migration
   - Existing `refine_symmetric` parameters with a prior accepted improvement and a reduced refinement round are granted one protected refinement pair when the new state column is first introduced.
   - This allows an existing HCT3 Kz state at step 0.025 to continue under the new rule without resetting the campaign.

## Audit fixes

3. `v14_optimizer_state.xlsx`
   - `updated_at` and `optimizer_version` are now written after loaded state fields, so stale metadata from an older release cannot overwrite the current values.

4. Cache console source
   - Cache-hit output now falls back to `cache_source` or `run_folder` when `source_candidate_id` is absent.

5. Decision-log metadata
   - Adds `priority_local_refinement`, `priority_refinement_rounds_remaining`, and `next_step_fraction` to the main calibration audit record.

## Validation

- V14 tests: 54 passed + 2 subtests, 0 failures.
- Full mixed V14/V15 suite: 65 passed + 2 subtests, 4 pre-existing V15 reporting failures unrelated to the V14.4.2 calibration engine.
