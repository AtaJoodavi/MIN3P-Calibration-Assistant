# Install / upgrade to V14.4

This package is a complete agent-core snapshot based on V14.3.7 with the first
V14.4 calibration improvement applied.

## Recommended clean upgrade

1. Preserve the current V14.3.7 folder and calibration results as an archive.
2. Extract this package as a new folder, for example:

   `07_agent_core_V14_4`

3. Keep the project folders (`00_project_info`, `01_input`, `03_runs`,
   `04_results`, `05_reports`, `06_knowledge`, `database`) outside the agent-core
   folder exactly as before.
4. Verify the new stepping logic before starting MIN3P:

```powershell
python .\verify_v14_4_range_normalized_steps.py
```

5. Run the V14 regression tests when `pytest` is available:

```powershell
python -m pytest -q tests\test_v14*.py
```

## New V14.4 setting

`config/calibration_rules.yaml` contains:

```yaml
log_range_step_scaling_enabled: true
```

To override it for one project, add the same setting to the
`agent_config.xlsx / optimizer_v13` sheet. Explicit Excel values take
precedence over YAML defaults.

## Existing campaigns

V14.4 remains able to read the existing V14 step-state format. However, because
the candidate step interpretation changed, do not mix V14.3.7 and V14.4 runs in
the same final publication campaign when you want a clean algorithmic record.
Archive the old campaign and start a fresh V14.4 calibration run from the chosen
initial parameter set.

## Preserved safety behavior

V14.4 keeps the V14.3.7 transaction/recovery machinery, direction-pair
precedence, bounds checks, freeze/reactivation framework, interaction-pair
safety, deterministic acceptance rules, and GPT advisory governance.
