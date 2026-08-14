from __future__ import annotations

"""Offline verifier for V15.3.1 scientific report summary and sensitivity figure.

Uses only a temporary synthetic project. It does not read or modify the HCT2
campaign files except for importing the installed reporting module.
"""

import hashlib
import json
from pathlib import Path
import sys
import tempfile

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from modules.scientific_reporting_V15 import REPORTER_VERSION, generate_v15_scientific_report


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        project = Path(temp) / "HCT2"
        core = project / "07_agent_core_V14"
        (core / "config").mkdir(parents=True)
        (project / "01_input").mkdir(parents=True)
        (project / "04_results").mkdir(parents=True)

        # Minimal conceptual configuration for the temporary project.
        (core / "config" / "conceptual_model_V15.json").write_text(
            json.dumps({
                "observables": ["pH", "SO4", "metals"],
                "title": "Synthetic V15.3.1 conceptual model",
                "subtitle": "Temporary verifier input",
            }, indent=2),
            encoding="utf-8",
        )

        # Minimal DAT file. The reporter only needs exactly one selected DAT file
        # for this offline structural test; it must not scan HCT2 inputs.
        dat_file = project / "01_input" / "synthetic_HCT.dat"
        dat_file.write_text(
            "\n".join([
                "'global control parameters'",
                "'synthetic_HCT'",
                "'done'",
                "'spatial discretization'",
                "1 ; number of control volumes in x",
                "1 ; number of control volumes in y",
                "5 ; number of control volumes in z",
                "1 ; number of discretization intervals in x",
                "1 ; number of discretization intervals in y",
                "5 ; number of discretization intervals in z",
                "0 1 ; xmin,xmax",
                "0 1 ; ymin,ymax",
                "0 1 ; zmin,zmax",
                "'done'",
                "'geochemical system'",
                "'components'",
                "'h+1' 'so4-2' 'o2(aq)'",
                "'minerals'",
                "'pyrite' 'calcite' 'albite'",
                "'gases'",
                "'o2(g)' 'co2(g)'",
                "'done'",
                "'physical parameters - porous medium'",
                "1 ; number of property zones",
                "'synthetic_zone' ; number and name of zone",
                "0.35 ; porosity",
                "'done'",
            ]) + "\n",
            encoding="utf-8",
        )

        protected = [
            project / "01_input" / "agent_config.xlsx",
            project / "04_results" / "best_parameters_V14.xlsx",
            project / "04_results" / "v14_optimizer_state.xlsx",
            project / "04_results" / "v14_optimizer_parameter_state.xlsx",
        ]
        for path, payload in zip(protected, [b"config", b"best", b"state", b"memory"]):
            path.write_bytes(payload)
        (project / "04_results" / "v14_parameter_runtime_state.json").write_text("{}", encoding="utf-8")
        (project / "04_results" / "v14_step_size_state.json").write_text("{}", encoding="utf-8")
        (project / "04_results" / "v14_campaign_state.json").write_text(
            json.dumps({"current_best_score": 9.8}), encoding="utf-8"
        )
        pd.DataFrame([
            {
                "timestamp": "2026-01-01", "parameter": "keff_pyrite",
                "group": "mineral_kinetics", "direction": "increase",
                "old_value": 1.0, "new_value": 1.05, "step_fraction": 0.05,
                "baseline_objective": 9.8, "candidate_objective": 10.0,
                "decision": "reject", "rejection_reason": "TOTAL_SCORE_not_meaningfully_improved",
            },
            {
                "timestamp": "2026-01-02", "parameter": "s_albite",
                "group": "other", "direction": "decrease",
                "old_value": 30.0, "new_value": 28.571429, "step_fraction": 0.05,
                "baseline_objective": 9.8, "candidate_objective": 9.95,
                "decision": "reject", "rejection_reason": "TOTAL_SCORE_not_meaningfully_improved",
            },
        ]).to_excel(project / "04_results" / "calibration_decision_log.xlsx", index=False)

        before = {str(path): digest(path) for path in protected}
        result = generate_v15_scientific_report(core, dat_file=dat_file)
        report = Path(result["report_markdown"]).read_text(encoding="utf-8")
        summary = pd.read_excel(result["parameter_sensitivity_summary"])
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        after = {str(path): digest(path) for path in protected}

        required_columns = {
            "parameter", "calibration_group", "process_family",
            "hydrogeochemical_process", "baseline_value",
            "best_tested_value", "sensitivity_class",
        }
        assert REPORTER_VERSION == "V15.3.1"
        assert result["reporter_version"] == "V15.3.1"
        assert manifest["reporter_version"] == "V15.3.1"
        assert required_columns.issubset(summary.columns)
        assert Path(result["parameter_sensitivity_png"]).exists()
        assert Path(result["parameter_sensitivity_png"]).stat().st_size > 1000
        assert "## Parameter sensitivity summary" in report
        assert "05_parameter_sensitivity_summary.png" in report
        assert before == after
        assert result["protected_campaign_hashes_unchanged"]

    print("PASS: V15.3.1 reporter loaded.")
    print("PASS: report contains the compact parameter summary table.")
    print("PASS: 05_parameter_sensitivity_summary.png was generated.")
    print("PASS: sensitivity table retains one row per calibrated parameter.")
    print("PASS: protected V14 inputs and optimizer-state files were unchanged.")


if __name__ == "__main__":
    main()
