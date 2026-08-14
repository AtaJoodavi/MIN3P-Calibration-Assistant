# V14 configuration files

- `calibration_rules.yaml` contains conservative V14 step and reactivation defaults.
- `gpt_supervisor_config.yaml` must remain `enabled: false` during deterministic smoke tests.
- `parameter_interactions.xlsx` contains the only interaction pairs V14 may consider. Ambiguous HCT2 parameter mappings are disabled until confirmed.
- `parameter_groups.xlsx` is a review template; V13 group definitions inside `agent_config.xlsx` remain the active group source unless a later V14 enhancement explicitly reads this template.


Conceptual image:
"enabled": false
The code will fall back to the deterministic conceptual draft.


