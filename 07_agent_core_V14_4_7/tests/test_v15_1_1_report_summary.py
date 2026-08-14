from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.scientific_reporting_V15 import REPORTER_VERSION, generate_v15_scientific_report


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class V1511ReportSummaryTests(unittest.TestCase):
    def _project(self, root: Path) -> tuple[Path, dict[str, str]]:
        project = root / "HCT2"
        core = project / "07_agent_core_V14"
        (core / "config").mkdir(parents=True)
        (project / "01_input").mkdir(parents=True)
        fixture = ROOT / "tests" / "HCT_one_layer_fixture.dat"
        if not fixture.is_file():
            raise FileNotFoundError(f"Missing test DAT fixture: {fixture}")
        shutil.copy2(fixture, project / "01_input" / "HCT.dat")
        (project / "04_results").mkdir(parents=True)
        protected_files = [
            project / "01_input" / "agent_config.xlsx",
            project / "04_results" / "best_parameters_V14.xlsx",
            project / "04_results" / "v14_optimizer_state.xlsx",
            project / "04_results" / "v14_optimizer_parameter_state.xlsx",
        ]
        for path, payload in zip(protected_files, [b"config", b"best", b"state", b"memory"]):
            path.write_bytes(payload)
        (project / "04_results" / "v14_parameter_runtime_state.json").write_text("{}", encoding="utf-8")
        (project / "04_results" / "v14_step_size_state.json").write_text("{}", encoding="utf-8")
        (project / "04_results" / "v14_campaign_state.json").write_text(
            json.dumps({"current_best_score": 9.8}), encoding="utf-8"
        )
        config_source = ROOT / "config" / "conceptual_model_V15.json"
        (core / "config" / "conceptual_model_V15.json").write_text(
            config_source.read_text(encoding="utf-8"), encoding="utf-8"
        )
        rows = [
            {
                "timestamp": "2026-01-01", "parameter": "keff_pyrite",
                "group": "mineral_kinetics", "direction": "increase",
                "old_value": 1.0, "new_value": 1.05, "step_fraction": 0.05,
                "baseline_objective": 9.8, "candidate_objective": 10.0,
                "decision": "reject", "rejection_reason": "TOTAL_SCORE_not_meaningfully_improved",
            },
            {
                "timestamp": "2026-01-02", "parameter": "keff_pyrite",
                "group": "mineral_kinetics", "direction": "decrease",
                "old_value": 1.0, "new_value": 0.95238095, "step_fraction": 0.05,
                "baseline_objective": 9.8, "candidate_objective": 9.9,
                "decision": "reject", "rejection_reason": "TOTAL_SCORE_not_meaningfully_improved",
            },
            {
                "timestamp": "2026-01-03", "parameter": "s_albite",
                "group": "other", "direction": "increase",
                "old_value": 30.0, "new_value": 31.5, "step_fraction": 0.05,
                "baseline_objective": 9.8, "candidate_objective": 9.96,
                "decision": "reject", "rejection_reason": "TOTAL_SCORE_not_meaningfully_improved",
            },
        ]
        pd.DataFrame(rows).to_excel(project / "04_results" / "calibration_decision_log.xlsx", index=False)
        return core, {str(path): digest(path) for path in protected_files}

    def test_report_includes_requested_summary_fields_and_figure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            core, before = self._project(Path(temp))
            result = generate_v15_scientific_report(core)
            self.assertEqual(REPORTER_VERSION, "V15.2.1")
            sensitivity_figure = Path(result["parameter_sensitivity_png"])
            self.assertTrue(sensitivity_figure.exists())
            self.assertGreater(sensitivity_figure.stat().st_size, 1000)

            report = Path(result["report_markdown"]).read_text(encoding="utf-8")
            self.assertIn("## Parameter sensitivity summary", report)
            self.assertIn("| Parameter | Calibration group | Process family | Hydrogeochemical process | Baseline value | Best tested value | Sensitivity class |", report)
            self.assertIn("05_parameter_sensitivity_summary.png", report)

            summary = pd.read_excel(result["parameter_sensitivity_summary"])
            required = {
                "parameter", "calibration_group", "process_family",
                "hydrogeochemical_process", "baseline_value",
                "best_tested_value", "sensitivity_class",
            }
            self.assertTrue(required.issubset(summary.columns))
            after = {path: digest(Path(path)) for path in before}
            self.assertEqual(before, after)
            self.assertTrue(result["protected_campaign_hashes_unchanged"])


if __name__ == "__main__":
    unittest.main()
