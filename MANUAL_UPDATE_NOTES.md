# Files to replace in the repository

Copy these files to the same relative paths in `AtaJoodavi/MIN3P-Calibration-Assistant`:

```text
README.md
07_agent_core_V14_4_7/README.md
07_agent_core_V14_4_7/INSTALL_V14.md
07_agent_core_V14_4_7/V14_4_CHANGELOG.md
07_agent_core_V14_4_7/V14_4_7_SENSITIVITY_FIRST_PATCH.md
07_agent_core_V14_4_7/min3p_ai_pipeline_V14.py
07_agent_core_V14_4_7/modules/adaptive_coordinate_optimizer_V14.py
07_agent_core_V14_4_7/verify_v14_4_7_sensitivity_first.py
```

`config/calibration_rules.yaml` does not need modification for this patch because the new defaults are implemented in the optimizer code.

The standard calibration command is:

```powershell
python .\min3p_ai_pipeline_V14.py --mode auto --max-physical-runs 20 --max-changes 1
```

After replacement, run:

```powershell
python .\verify_v14_4_7_sensitivity_first.py
python .\min3p_ai_pipeline_V14.py --help
```

The verification should report `PASS`, and the normal help/commands do not expose the optional GPT control.
