# V14.4.3 Changelog

V14.4.3 builds on V14.4.2 and adds parameter-specific coarse-to-fine initial exploration without changing the objective function, acceptance thresholds, transaction rollback, directional-pair safety, candidate cache, or V15 reporting logic.

## Added

- Optional per-parameter `initial_search_mode` in `agent_config.xlsx / parameters`.
- `initial_factor` for logarithmic parameters, e.g. factor 10 gives x10 / divide-by-10 initial probes when compatible with bounds and step caps.
- `initial_range_fraction` for narrow/physically constrained parameters.
- Optional per-parameter `minimum_step_fraction` and `maximum_step_fraction`.
- Optional group defaults in `optimizer_v13`: `initial_factor_group_<group>` and `initial_range_fraction_group_<group>`.
- Migration-safe application: V14.4.2 parameters that have never been tested can adopt the new initial scale; parameters with outcome history keep their current adaptive step.
- Initial-search audit fields in optimizer parameter state, candidate suggestions/events, and the main calibration decision log.
- `preview_v14_4_3_initial_scales.py` for a read-only pre-run preview.
- `verify_v14_4_3_initial_scales.py` deterministic smoke test.

## Initial-search audit fields

- `initial_search_mode`
- `initial_search_source`
- `initial_factor_requested`
- `initial_range_fraction_requested`
- `initial_step_fraction_resolved`
- `initial_step_fraction_clamped`
- `initial_factor_effective`

## Validation

- V14 tests: 61 passed + 2 subtests, 0 failures.
- Full mixed suite: 72 passed + 2 subtests, 4 pre-existing V15 reporting failures unrelated to V14.4.3.
