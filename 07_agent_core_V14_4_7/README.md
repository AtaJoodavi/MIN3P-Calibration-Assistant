# V14.4.7 calibration engine

# MIN3P AI Assistant — V14.4 Calibration + V15.4 Reporting

V14.4 starts the evidence-adaptive calibration upgrade while preserving the
V14.3.7 transaction/recovery safeguards and the existing V15.4 reporting layer.


## V14.4.7 clear candidate vs. physical-run limits

V14.4.7 preserves the V14.4.6 calibration algorithm and clarifies invocation
limits. `--max-runs` remains compatible but is now explicitly an alias for
`--max-candidates`, so cache hits count toward that limit. Use
`--max-physical-runs` when the desired quota is the number of new MIN3P
simulations. Each invocation prints and persists an explicit summary and stop
reason in `v14_optimizer_state.xlsx`.

Examples:

```powershell
python .\min3p_ai_pipeline_V14.py --mode auto --max-candidates 20 --max-changes 1
python .\min3p_ai_pipeline_V14.py --mode auto --max-physical-runs 20 --max-changes 1
```

## V14.4.6 Windows state-write hardening

V14.4.6 preserves the V14.4.5 calibration algorithm and hardens persistence for
long unattended campaigns. V14 JSON/Excel state files now use unique temporary
files plus retry/backoff around atomic replacement, so brief Windows file locks
(WinError 5/32/33) do not stop a campaign on the first failed replace attempt.
The pipeline also uses the same robust writer for V14 best/config snapshots and
suppresses the pandas all-NA concat FutureWarning in flexible audit tables.


## V14.4 calibration change implemented in this release

### Range-normalized logarithmic stepping

For logarithmic parameters with explicit positive `min` and `max` values in
`agent_config.xlsx`, `step_fraction` now represents a fraction of the configured
log10 search range.

Example:

```text
parameter = keff_pyrite
min       = 1e-6
max       = 1
step      = 0.05
```

The feasible range is 6 log10 decades, so a 5% move is 0.30 decades:

```text
increase factor = 10^(+0.05 * 6) = 1.995262...
decrease factor = 10^(-0.05 * 6) = 0.501187...
```

This replaces the old local `×1.05 / ×0.952381` move when valid positive bounds
are available. The increase/decrease pair remains reciprocal in log space.

### Safe fallback

If a logarithmic parameter has missing, zero, negative, or otherwise unusable
bounds, V14.4 automatically falls back to the V14.3.7 reciprocal local move:

```text
increase = value * (1 + step)
decrease = value / (1 + step)
```

No semantic floor is used to invent a calibration range.

### Audit fields

Candidate suggestions and optimizer events now include:

- `step_basis`
- `range_normalized`
- `configured_log_range_decades`

These fields make it explicit whether a candidate used the V14.4 transformed
search range or the legacy fallback.

### Configuration

The default is enabled in `config/calibration_rules.yaml`:

```yaml
log_range_step_scaling_enabled: true
```

An explicit value in `agent_config.xlsx / optimizer_v13` overrides the YAML
default. Set it to `false` to restore the legacy logarithmic step behavior.

## V14.4.4 automatic range-adaptive initial search

V14.4.4 removes the manual V14.4.3 initial-search columns. The existing
`parameter`, `status`, `value`, `min`, `max`, sensitivity fields, and `group` are
sufficient. Never-tested parameters infer their initial exploration scale from
the configured range.

For positive logarithmic parameters with valid positive bounds, V14.4.4 uses
the number of log10 decades to choose a smooth coarse starting factor, capped
at `x10 / divide-by-10`. A six-decade `keff_pyrite` range therefore starts at
about `x10`, while a narrow Kz range starts close to `x1.02`.

For bounded-linear parameters, the default first move is 5% of the configured
span. Relative parameters with valid bounds are converted so the first numeric
move is also 5% of their configured span.

If the logarithmic transform is impossible because a bound is zero/negative,
or bounds are missing/equal/invalid, the controller falls back safely to the
global initial step (`0.05` by default). No parameter-specific search columns
are required.

Preview resolved scales without running MIN3P or modifying optimizer state:

```powershell
python .\preview_v14_4_4_initial_scales.py --active-only
```

Optionally write the preview workbook:

```powershell
python .\preview_v14_4_4_initial_scales.py --active-only --xlsx
```

Run the deterministic smoke test:

```powershell
python .\verify_v14_4_4_initial_scales.py
```

See `V14_4_4_AUTOMATIC_SEARCH_GUIDE.md` for the exact rule and fallback logic.

## Run calibration

```powershell
python .\min3p_ai_pipeline_V14.py --mode auto --max-runs 100 --max-changes 1
```

## Verify V14.4 stepping

```powershell
python .\verify_v14_4_range_normalized_steps.py
```

Or run all V14 tests:

```powershell
python -m pytest -q tests\test_v14*.py
```

## V15.4 reporting

The V15.4 reporting workflow remains available and is not allowed to mutate the
V14 optimizer state or calibration inputs.

```powershell
python .\generate_v15_scientific_report.py `
  --dat-file ..\01_input\HCT.dat `
  --conceptual-image-mode deterministic `
  --conceptual-detail paper `
  --campaign-review-mode auto
```

See `V14_4_CHANGELOG.md` and `V14_4_DEVELOPMENT_PLAN.md` for the calibration
upgrade sequence.


## V14.4.5 bound/no-op candidate guard

Before MIN3P, a candidate is now checked after min/max clamping. If the
resulting change is numerically indistinguishable from the current value, the
direction is recorded as `candidate_blocked_by_bound_noop`, no transaction/run
is launched for that candidate, and the optimizer immediately continues with
the remaining legal direction. Interaction-pair moves use the same guard.
