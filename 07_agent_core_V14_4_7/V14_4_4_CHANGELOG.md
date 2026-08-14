# V14.4.4 Changelog

V14.4.4 replaces the manual V14.4.3 parameter-specific initial-search configuration with automatic range-adaptive initialization.

## Calibration changes

- No `initial_search_mode`, `initial_factor`, or `initial_range_fraction` columns are required.
- Positive logarithmic parameters infer their first scale from `log10(max/min)`.
- Automatic first multiplicative probes are capped at `x10 / divide-by-10` by default.
- Narrow logarithmic ranges receive proportionally smaller first moves.
- Bounded-linear parameters start at the global fraction of the configured span (default 5%).
- Relative parameters with valid bounds are converted so the first numeric move equals the same configured-span fraction.
- Zero, negative, missing, equal, or invalid bounds use the safe global fallback rather than raising an error.
- Existing explored/refined parameters preserve their adaptive step during migration.

## Safety unchanged

- transaction/rollback behavior unchanged;
- acceptance thresholds and TOTAL_SCORE unchanged;
- direction-pair completion unchanged;
- candidate cache unchanged;
- local-refinement priority unchanged;
- GPT remains advisory only.

## Verification

Run:

```powershell
python .\verify_v14_4_4_initial_scales.py
python .\preview_v14_4_4_initial_scales.py --active-only
```
