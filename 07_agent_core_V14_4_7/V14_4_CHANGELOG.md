# V14.4 Changelog

## V14.4.0 — range-normalized search-space stepping

Implemented:

1. `StepSizeController.VERSION = V14.4`.
2. Logarithmic parameters use configured log-range scaling when explicit
   positive `min` and `max` bounds are available.
3. Missing/invalid log bounds use the legacy reciprocal multiplicative move.
4. Bounded-linear stepping remains a fraction of the configured linear span.
5. `agent_config.xlsx / optimizer_v13` now correctly overrides YAML defaults in
   the step controller, matching the documented precedence.
6. Candidate/event audit metadata records step basis and configured log range.
7. Pipeline and adaptive optimizer version strings updated to V14.4.
8. Added deterministic V14.4 unit tests.
9. Removed stale `.pre_*` backup source/test files and `__pycache__` artifacts
   from the clean V14.4 release package.

## Compatibility

The V14.3.7 transaction manager and recovery design are retained. Existing
`v14_step_size_state.json` records remain readable because the public
`move_space` label `logarithmic` is unchanged.

For a scientifically clean comparison, a new V14.4 calibration campaign should
start with a preserved copy of the V14.3.7 results and fresh V14 optimizer/run
state rather than mixing both step policies in one publication campaign.

## V14.4.7 sensitivity-first extension

Added after the original V14.4.7 CLI/status release:

1. Coverage-first local sensitivity screening for campaigns with at least five active parameters.
2. One diagnostic perturbation is evaluated for each active parameter against the same accepted baseline before ordinary directional calibration begins.
3. Screening candidates are restored rather than committed, preserving comparability of the initial sensitivity estimates.
4. Local screening sensitivity is calculated from the absolute objective response normalized by the fractional perturbation.
5. After coverage is complete, ordinary calibration starts with the parameter having the largest finite screening sensitivity.
6. Existing transaction isolation, rollback, restoration verification, cache handling, bounds, checkpoints, and directional-search rules are retained.
7. Standard command-line use does not require any AI/GPT option; the optional internal capability is inactive by default.
