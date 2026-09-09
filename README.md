# MIN3P-Calibration-Assistant

MIN3P-Calibration-Assistant is a deterministic calibration and sensitivity-analysis framework for MIN3P reactive transport models. It automates candidate generation, MIN3P execution, comparison with observations, acceptance/rejection decisions, rollback, parameter-state tracking, local sensitivity screening, checkpointing, and scientific reporting.

The code is designed so that every tested parameter change is recorded and the calibration history can be reconstructed.

MIN3P-Calibration-Assistant was developed by Ata Joodavi at the Geological Survey of Finland (GTK).

> A licensed MIN3P executable and the required thermodynamic/database files must be supplied separately by the user.
> MIN3P is developed and maintained independently by the MIN3P development team. The included executable is redistributed for convenience under the applicable MIN3P license. MIN3P-Calibration-Assistant does not modify the MIN3P governing equations or numerical formulation.

## 1. Project structure

```text
MyProject/
├── 01_input/
│   ├── MyProject_template.dat
│   ├── agent_config.xlsx
│   └── observed data for min3p.xlsx
├── 03_runs/
├── 04_results/
├── 05_reports/
├── database/
└── 07_agent_core_V14_4_7/
    ├── min3p_ai_pipeline_V14.py
    ├── generate_v15_scientific_report.py
    ├── generate_v15_explainability.py
    ├── modules/
    └── config/
```

## 2. Software requirements

Recommended environment:

- Windows
- Python 3.11 or a compatible recent Python 3 version
- MIN3P executable
- MIN3P database files

Install the main Python packages:

```powershell
python -m pip install pandas numpy openpyxl matplotlib pyyaml pytest
```

## 3. Configure a calibration project

Start from a MIN3P model that already runs successfully. Create a template copy and replace calibratable values with placeholders such as:

```text
{{keff_pyrite}}
{{keff_calcite}}
{{porosity}}
{{Kz}}
{{vg_alpha}}
{{vg_n}}
```

Each placeholder must have a matching row in the `parameters` sheet of `agent_config.xlsx`.

Typical parameter columns are:

| Column | Purpose |
|---|---|
| `parameter` | Parameter/placeholder name |
| `status` | `active` or inactive/frozen state |
| `value` | Starting value |
| `min` | Lower bound |
| `max` | Upper bound |
| `sensitivity_mode` | Perturbation mode |
| `sensitivity_multiplier` | Perturbation setting |
| `group` | Process/calibration group |

The `species` sheet defines active observation groups and weights. The `model_files` sheet identifies the template, generated DAT, observation workbook, executable, and related model files.

## 4. Coverage-first sensitivity screening

For campaigns with at least five active parameters, the normal automatic workflow starts with a coverage-first local sensitivity screening phase.

Each active parameter receives one initial diagnostic perturbation before ordinary continuation/refinement is allowed to dominate the search. Screening candidates are evaluated against the same accepted baseline and are not committed as accepted states.

The local screening score is:

```text
S = |J_candidate - J_baseline| / fractional_step
```

where `J` is the composite objective score.

After all active parameters have been screened, ordinary deterministic calibration starts from the parameter with the largest finite screening sensitivity. The learned sensitivity remains available as a ranking signal during subsequent search.

Default internal settings are:

```text
coverage_first_min_active_parameters = 5
sensitivity_guided_after_coverage = 1
```

These defaults can be overridden through the existing optimizer configuration where supported. No separate sensitivity command is required.

## 5. Preflight

From the agent-core folder:

```powershell
python .\min3p_ai_pipeline_V14.py --mode preflight
```

Confirm that the template, observation file, MIN3P executable, required modules, and transaction state are valid.

You can preview automatic initial search scales without running MIN3P:

```powershell
python .\preview_v14_4_4_initial_scales.py --active-only
```

## 6. Run calibration

Small test campaign:

```powershell
python .\min3p_ai_pipeline_V14.py --mode auto --max-physical-runs 5 --max-changes 1
```

Larger campaign:

```powershell
python .\min3p_ai_pipeline_V14.py --mode auto --max-physical-runs 20 --max-changes 1
```

`--max-physical-runs` limits new MIN3P executions. `--max-candidates` limits candidate cycles and includes cache hits. `--max-runs` remains a backward-compatible alias for `--max-candidates`.

## 7. Candidate workflow

```text
Read accepted configuration
        ↓
If new campaign: perform coverage-first screening
        ↓
Rank screened parameters by local objective sensitivity
        ↓
Select one eligible parameter and direction
        ↓
Generate isolated candidate
        ↓
Check for valid cached evaluation
        ↓
Run MIN3P if required
        ↓
Compare model output with observations
        ↓
Calculate objective and diagnostics
        ↓
Apply deterministic decision rules
        ↓
Accept candidate or restore accepted state
        ↓
Write audit and workflow-state files
```

Rejected or invalid candidates cannot replace the accepted model state.

## 8. Main outputs

Typical files in `04_results` include:

```text
run_ranking.xlsx
optimization_history_V14.xlsx
calibration_decision_log.xlsx
best_parameters_V14.xlsx
v14_optimizer_state.xlsx
v14_optimizer_parameter_state.xlsx
v14_candidate_decisions.xlsx
v14_parameter_runtime_state.json
v14_step_size_state.json
```

Individual physical simulations are stored under `03_runs/run_YYYYMMDD_HHMMSS/`.

## 9. Stop, resume, and recover

```powershell
python .\min3p_ai_pipeline_V14.py --mode request-safe-stop
python .\min3p_ai_pipeline_V14.py --mode clear-safe-stop
python .\min3p_ai_pipeline_V14.py --mode recover-interrupted
```

## 10. Reports

Generate the V14 campaign report:

```powershell
python .\min3p_ai_pipeline_V14.py --mode report
```

Post-process the current best run without rerunning MIN3P:

```powershell
python .\min3p_ai_pipeline_V14.py --mode postprocess-best
```

Generate the deterministic V15 scientific report:

```powershell
python .\generate_v15_scientific_report.py `
  --dat-file ..\01_input\MyProject.dat `
  --conceptual-image-mode deterministic `
  --conceptual-detail paper `
  --campaign-review-mode deterministic
```

Generate candidate-level explainability outputs:

```powershell
python .\generate_v15_explainability.py
```

## 11. Verify the sensitivity-first extension

```powershell
python .\verify_v14_4_7_sensitivity_first.py
```

Expected result:

```text
PASS: coverage-first screening and sensitivity-guided start are working.
```

## 12. Reproducibility

For a publication/reproducibility package, preserve at least:

```text
*_template.dat
initial agent_config.xlsx
observed data workbook
final calibrated DAT
run_ranking.xlsx
optimization_history_V14.xlsx
calibration_decision_log.xlsx
best_parameters_V14.xlsx
parameter-sensitivity outputs
source-code version/tag
```


