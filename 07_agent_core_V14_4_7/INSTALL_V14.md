# Install / upgrade V14.4.7 sensitivity-first extension

This update preserves the existing V14.4.7 transaction, rollback, checkpoint, cache, bound, and directional-search safeguards while adding coverage-first local sensitivity screening.

## Recommended clean upgrade

1. Archive the current agent-core folder and campaign results before replacing files.
2. Replace only the files supplied in this update package.
3. Keep the project folders (`01_input`, `03_runs`, `04_results`, `05_reports`, and `database`) in their existing locations.
4. For a scientifically clean comparison, start a new calibration campaign from the intended initial parameter set rather than mixing pre-screening and sensitivity-first search trajectories in one final publication campaign.

## Sensitivity-first initialization

When at least five parameters are active, a new automatic campaign first evaluates one diagnostic perturbation for every active parameter against the same accepted baseline.

Screening candidates are not committed as accepted states. Their local response is summarized as:

```text
S = |J_candidate - J_baseline| / fractional_step
```

After coverage is complete, ordinary calibration begins with the active parameter having the largest finite screening sensitivity.

Default internal settings are:

```text
coverage_first_min_active_parameters = 5
sensitivity_guided_after_coverage = 1
```

No new command-line option is required.

## Verify before running MIN3P

Run:

```powershell
python .\verify_v14_4_7_sensitivity_first.py
```

Expected output:

```text
PASS: coverage-first screening and sensitivity-guided start are working.
```

Then run the existing stepping verification if desired:

```powershell
python .\verify_v14_4_range_normalized_steps.py
```

## Normal calibration command

```powershell
python .\min3p_ai_pipeline_V14.py --mode auto --max-physical-runs 20 --max-changes 1
```

## Existing campaigns

The optimizer state contains coverage-screening fields, including screening status and learned sensitivity. To evaluate the new initialization logic cleanly, use a fresh campaign state. Existing accepted parameter values can still be used as the chosen starting model.

## Configuration note

`config/calibration_rules.yaml` does not need to be changed for this patch. The sensitivity-first defaults are defined in the V14 optimizer and can be overridden through the existing optimizer configuration interface where supported.
