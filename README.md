# MIN3P-Calibration-Assistant

MIN3P-Calibration-Assistant is a deterministic calibration and sensitivity-analysis framework for MIN3P reactive transport models. It automates generation of candidate parameter sets, MIN3P execution, comparison with observations, acceptance/rejection decisions, rollback, parameter-state tracking, sensitivity analysis, and scientific reporting.

The code is designed so that every tested parameter change is recorded and the calibration history can be reconstructed.

MIN3P-Calibration-Assistant was developed by Ata Joodavi at the Geological Survey of Finland (GTK).
ata.joodavi@gtk.fi

> ** A licensed MIN3P executable and the required thermodynamic/database files must be supplied separately by the user.

---

## 1. Project structure

A calibration project should have the following structure:

```text
MyProject/
│
├── 00_project_info/                 # Optional project notes
├── 01_input/
│   ├── MyProject_template.dat       # MIN3P template containing {{parameter}} placeholders
│   ├── agent_config.xlsx            # Calibration configuration
│   └── observed data for min3p.xlsx # Observations used in the objective function
│
├── 03_runs/                         # Created/used for individual MIN3P runs
├── 04_results/                      # Calibration state, history, ranking and audit files
├── 05_reports/                      # Calibration and scientific reports
├── 06_knowledge/                    # Optional supporting information
├── database/                        # MIN3P database files
│
└── 07_agent_core_V14_4_7/
    ├── min3p_ai_pipeline_V14.py
    ├── plotsV46.py
    ├── generate_v15_scientific_report.py
    ├── generate_v15_explainability.py
    ├── modules/
    └── config/
```

The pipeline automatically creates missing output directories such as `03_runs`, `04_results`, `05_reports`, and `06_knowledge`.

---

## 2. Software requirements

Recommended environment:

- Windows
- Python 3.11 or a compatible recent Python 3 version
- MIN3P executable
- MIN3P database files

Install the main Python packages:

```powershell
python -m pip install pandas numpy openpyxl matplotlib pyyaml
```

Optional packages:

```powershell
python -m pip install openai pytest
```

`openai` is required only when GPT-based scientific supervision or GPT campaign review is enabled. The calibration engine can run without GPT.

### MIN3P executable

Place the MIN3P executable in one of these locations:

```text
MyProject/
MyProject/01_input/
MyProject/database/
MyProject/07_agent_core_V14_4_7/
```

The runner searches for filenames such as:

```text
MIN3P-HPC-V*.exe
MIN3P*.exe
min3p*.exe
```

---

# 3. Creating a new calibration project

The two main configuration files are (in MIN3P-Calibration-Assistant\01_input):

1. `*_template.dat`
2. `agent_config.xlsx`

An observation workbook is also required when model results are compared with measurements.

## Step 1 — Start from a working MIN3P model

First create and test a normal MIN3P `.dat` file manually. The model should run successfully before automated calibration is started.

For example:

```text
MyProject.dat
```

Make a copy and rename it:

```text
MyProject_template.dat
```

Do not use the calibration assistant to repair a MIN3P model that does not already run successfully.

---

## Step 2 — Add placeholders to `*_template.dat`

Replace numerical values that may be calibrated with parameter placeholders using the format:

```text
{{parameter_name}}
```

Example:

Original MIN3P value:

```text
0.004534532383
```

Template value:

```text
{{keff_pyrite}}
```

Another example:

```text
{{porosity}}
{{Kz}}
{{vg_alpha}}
{{vg_n}}
{{keff_calcite}}
```

The parameter name inside `{{ }}` must exactly match the name in the `parameters` sheet of `agent_config.xlsx`.

**Important:** every active placeholder in the template must have a matching row in `agent_config.xlsx`. If an unreplaced placeholder remains in the generated DAT, the pipeline stops before MIN3P is run.

Parameters may exist in `agent_config.xlsx` without appearing in the template; these are ignored by the DAT builder until they are used in the template.

---

# 4. Configure `agent_config.xlsx` (MIN3P-Calibration-Assistant\01_input)

The supplied HCT3 example contains the main sheets needed by the calibration workflow.

## 4.1 `parameters` sheet

Typical columns are:

| Column | Purpose |
|---|---|
| `parameter` | Must match the placeholder in the template DAT |
| `status` | `active`, `inactive`, or user-controlled frozen state |
| `value` | Current/baseline parameter value |
| `min` | Lower allowed bound |
| `max` | Upper allowed bound |
| `sensitivity_mode` | How local perturbations are defined |
| `sensitivity_multiplier` | Perturbation setting used by sensitivity tools |
| `group` | Scientific/process group for the parameter |

Example:

| parameter | status | value | min | max | sensitivity_mode | sensitivity_multiplier | group |
|---|---|---:|---:|---:|---|---:|---|
| `keff_pyrite` | active | 0.00453 | 1e-6 | 1 | multiplier | 0.25 | sulfide |
| `keff_calcite` | active | 1.43e-4 | 1e-10 | 0.01 | multiplier | 0.25 | buffering |
| `porosity` | active | 0.40 | 0.30 | 0.50 | multiplier | 0.25 | flow |

`active` parameters are eligible for calibration. Parameters that should remain fixed should be marked `inactive` or otherwise excluded according to the project configuration.

The values in this sheet define the starting model for a new campaign. Keep an archived copy of the initial `agent_config.xlsx` before calibration if the initial parameter set must be preserved separately.

---

## 4.2 `species` sheet

This sheet defines the observation groups used in the objective function.

Typical columns are:

| Column | Purpose |
|---|---|
| `species` | MIN3P species or `pH` |
| `active` | Include/exclude the species from calibration |
| `weight` | Relative contribution to the objective |
| `phase` | Usually `aqueous` for the current workflow |

Example:

| species | active | weight | phase |
|---|---|---:|---|
| pH | yes | 2 | aqueous |
| so4-2 | yes | 2 | aqueous |
| zn+2 | yes | 1 | aqueous |
| cu+2 | yes | 1 | aqueous |

Only active species are included in model/observation evaluation.

---

## 4.3 `model_files` sheet

This sheet connects the code to the project files.

Example:

| key | value |
|---|---|
| `template_file` | `MyProject_template.dat` |
| `input_file` | `MyProject.dat` |
| `observed_file` | `observed data for min3p.xlsx` |
| `exe_file` | `MIN3P-HPC-V2.6.4.903.exe` |
| `timeseries_file` | `model_vs_observed_timeseries.xlsx` |

The most important entries are:

- `template_file`: DAT file containing placeholders
- `input_file`: generated MIN3P DAT file
- `observed_file`: observation workbook

The names must match the files used by the project. The supplied HCT3 `agent_config.xlsx` currently defines `input_file = HCT.dat`; if you rename the generated model to `HCT3.dat`, update this cell as well.

---

# 5. Observation data

The observed-data workbook should normally be stored in:

```text
01_input/observed data for min3p.xlsx
```

A `day` column is required.

Example:

| day | pH | so4-2 | zn+2 | cu+2 |
|---:|---:|---:|---:|---:|
| 0 | 7.56 | 0.00655 | 0.000898 | 3.02e-6 |
| 7 | 7.30 | 0.00955 | 0.000036 | 1.01e-6 |
| 14 | 4.70 | 0.00625 | 0.000030 | 6.69e-7 |

For the current evaluator:

- pH is unitless.
- Dissolved species should be supplied in units consistent with the MIN3P concentration outputs used by the evaluator (normally mol/L water).
- Missing values can be left blank/NaN.
- Only species marked active in `agent_config.xlsx` contribute to the objective.

The code interpolates model results to the observation times before calculating RMSE, MAE, and bias.

---

# 6. Check the project before calibration

Open PowerShell and move to the agent-core folder:

```powershell
cd C:\Dev\MIN3P-Calibration-Assistant\07_agent_core_V14_4_7
```

Run the preflight check:

```powershell
python .\min3p_ai_pipeline_V14.py --mode preflight
```

Review the output and confirm that:

- `agent_config.xlsx` is found;
- required V14 modules are available;
- no unresolved transaction exists;
- the project is ready for V14 auto mode.

You can also preview automatic initial search scales without running MIN3P:

```powershell
python .\preview_v14_4_4_initial_scales.py --active-only
```

---

# 7. Run the calibration

For a small test campaign:

```powershell
python .\min3p_ai_pipeline_V14.py --mode auto --max-physical-runs 5 --max-changes 1 --disable-gpt
```

For a larger campaign:

```powershell
python .\min3p_ai_pipeline_V14.py --mode auto --max-physical-runs 20 --max-changes 1 --disable-gpt
```

The campaign can be continued by running the same command again. Optimizer state and audit files in `04_results` preserve the previous progress.

### Candidate limit versus physical-run limit

`--max-physical-runs` is recommended when you want to control the number of new MIN3P simulations.

```powershell
--max-physical-runs 20
```

`--max-candidates` counts optimizer candidate cycles, including cache hits:

```powershell
python .\min3p_ai_pipeline_V14.py --mode auto --max-candidates 20 --max-changes 1
```

`--max-runs` is retained as a backward-compatible alias for `--max-candidates`.

---

# 8. What happens during one calibration candidate

For each candidate, the framework approximately follows this sequence:

```text
Read accepted agent_config.xlsx
        ↓
Select one eligible parameter
        ↓
Generate increase/decrease candidate
        ↓
Create isolated candidate configuration
        ↓
Build DAT from *_template.dat
        ↓
Run MIN3P
        ↓
Post-process GBT/GBM outputs
        ↓
Compare simulation with observations
        ↓
Calculate composite objective
        ↓
Apply deterministic acceptance rules
        ↓
Accept candidate OR restore previous accepted state
        ↓
Write audit and optimizer-state files
```

Candidate preparation does not directly overwrite the accepted configuration. Rejected or invalid candidates are restored through the transaction/rollback system.

---

# 9. Main output files

Important results are written to `04_results`.

Typical files include:

```text
run_ranking.xlsx
optimization_history.xlsx
optimization_history_V14.xlsx
calibration_decision_log.xlsx
best_parameters_V14.xlsx
v14_optimizer_state.xlsx
v14_optimizer_parameter_state.xlsx
v14_candidate_decisions.xlsx
v14_parameter_runtime_state.json
v14_step_size_state.json
```

Individual simulations are stored under:

```text
03_runs/run_YYYYMMDD_HHMMSS/
```

The ranking workbook lists successful runs and their objective values.

---

# 10. Stop and resume safely

Request a safe stop:

```powershell
python .\min3p_ai_pipeline_V14.py --mode request-safe-stop
```

Clear the safe-stop flag before continuing:

```powershell
python .\min3p_ai_pipeline_V14.py --mode clear-safe-stop
```

Recover interrupted transactions when required:

```powershell
python .\min3p_ai_pipeline_V14.py --mode recover-interrupted
```

---

# 11. Generate calibration reports

Generate the V14 campaign report:

```powershell
python .\min3p_ai_pipeline_V14.py --mode report
```

Generate the full post-processing package for the current V14 best run without rerunning the MIN3P solver:

```powershell
python .\min3p_ai_pipeline_V14.py --mode postprocess-best
```

---

# 12. Generate the V15 scientific report

After the calibration campaign, generate the scientific report using the generated or selected project DAT file.

Example:

```powershell
python .\generate_v15_scientific_report.py `
  --dat-file ..\01_input\MyProject.dat `
  --conceptual-image-mode deterministic `
  --conceptual-detail paper `
  --campaign-review-mode deterministic
```

For HCT3, use the exact non-template DAT filename that exists in `01_input` (the current uploaded `agent_config.xlsx` uses `HCT.dat` unless you change it):

```powershell
python .\generate_v15_scientific_report.py `
  --dat-file ..\01_input\HCT.dat `
  --conceptual-image-mode deterministic `
  --conceptual-detail paper `
  --campaign-review-mode auto
```

The reporter produces, among other files:

```text
05_reports/V15_scientific_report/
├── V15_calibration_story_report.html
├── V15_calibration_story_report.md
├── campaign_analysis_V15_4.xlsx
├── campaign_review_V15_4.md
├── figures/
│   ├── 01_conceptual_hydrogeochemical_model.png
│   ├── 02_calibration_decision_timeline.png
│   └── 05_parameter_sensitivity_summary.png
└── tables/
    ├── parameter_trial_effects.xlsx
    ├── parameter_sensitivity_and_calibration_evidence.xlsx
    └── calibration_group_overview.xlsx
```

The scientific-report command is read-only with respect to the protected V14 calibration state.

---

# 13. Generate candidate explainability outputs

Run:

```powershell
python .\generate_v15_explainability.py
```

This creates candidate-level explanations showing why each parameter and direction was tested and what decision followed.

Typical outputs include:

```text
05_reports/V15_scientific_report/explainability/
├── candidate_explanations.json
├── candidate_explanation.xlsx
└── explainability_summary.xlsx
```
---

# 14. Creating another project from the HCT3 example

A simple way to start another model is:

1. Copy the project directory to a new project folder.
2. Delete/archive previous `03_runs`, `04_results`, and `05_reports` campaign outputs.
3. Put the new working MIN3P model in `01_input`.
4. Create `NewProject_template.dat` from that model.
5. Replace calibratable values with `{{parameter_name}}` placeholders.
6. Copy and edit `agent_config.xlsx`.
7. Make sure every template placeholder has a matching `parameters` row.
8. Set `template_file`, `input_file`, and `observed_file` in the `model_files` sheet.
9. Add the new observations workbook with a valid `day` column.
10. Confirm the MIN3P executable and database are available.
11. Run `--mode preflight`.
12. Start with a small `--max-physical-runs 5` calibration test.
13. Inspect `run_ranking.xlsx`, `calibration_decision_log.xlsx`, and the generated plots before starting a long campaign.

---

# 15. Reproducibility notes

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
source code version/tag
```

Do not publish API keys, `.env` files, confidential datasets, temporary files, Python cache files, or third-party software that cannot legally be redistributed.

---

# 16. Current development status

This repository is research software developed for automated calibration and scientific audit of MIN3P reactive transport models. Calibration results should be interpreted together with the underlying conceptual model, parameter bounds, observation weights, and model limitations. Automated calibration does not eliminate conceptual-model uncertainty or provide formal parameter uncertainty quantification by itself.

---

## Citation

A formal software citation and DOI can be added here when the publication release is archived.

```text
Joodavi, A. et al. MIN3P-Calibration-Assistant, version X.X.
```

