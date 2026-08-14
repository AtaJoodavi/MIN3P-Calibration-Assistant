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


class V151ParameterSensitivityTests(unittest.TestCase):
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
        default_config = ROOT / "config" / "conceptual_model_V15.json"
        (core / "config" / "conceptual_model_V15.json").write_text(
            default_config.read_text(encoding="utf-8"), encoding="utf-8"
        )
        decisions = [
            {
                "timestamp": "2026-01-01", "parameter": "keff_pyrite",
                "group": "mineral_kinetics", "direction": "increase",
                "old_value": 1.0, "new_value": 1.05, "step_fraction": 0.05,
                "baseline_objective": 9.8, "candidate_objective": 10.0,
                "decision": "reject", "rejection_reason": "TOTAL_SCORE_not_meaningfully_improved",
                "restoration_verified": True,
            },
            {
                "timestamp": "2026-01-02", "parameter": "keff_pyrite",
                "group": "mineral_kinetics", "direction": "decrease",
                "old_value": 1.0, "new_value": 0.95238095, "step_fraction": 0.05,
                "baseline_objective": 9.8, "candidate_objective": 9.9,
                "decision": "reject", "rejection_reason": "TOTAL_SCORE_not_meaningfully_improved",
                "restoration_verified": True,
            },
            {
                "timestamp": "2026-01-03", "parameter": "feoh_s_density",
                "group": "sorption", "direction": "decrease",
                "old_value": 0.056, "new_value": 0.05333333, "step_fraction": 0.05,
                "baseline_objective": 9.8, "candidate_objective": np.inf,
                "decision": "reject", "rejection_reason": "invalid_or_failed_MIN3P_run",
                "restoration_verified": True,
            },
            {
                "timestamp": "2026-01-04", "parameter": "feoh_s_density",
                "group": "sorption", "direction": "increase",
                "old_value": 0.056, "new_value": 0.0588, "step_fraction": 0.05,
                "baseline_objective": 9.8, "candidate_objective": 9.93,
                "decision": "reject", "rejection_reason": "TOTAL_SCORE_not_meaningfully_improved",
                "restoration_verified": True,
            },
            {
                "timestamp": "2026-01-05", "parameter": "feoh_s_density",
                "group": "sorption", "direction": "increase",
                "old_value": 0.056, "new_value": 0.0588, "step_fraction": 0.05,
                "baseline_objective": 9.8, "candidate_objective": np.nan,
                "decision": "cancelled_before_candidate_evaluation",
                "rejection_reason": "user_interrupt:keyboard_interrupt",
                "restoration_verified": True,
            },
        ]
        pd.DataFrame(decisions).to_excel(
            project / "04_results" / "calibration_decision_log.xlsx", index=False
        )
        hashes = {str(path): digest(path) for path in protected_files}
        return core, hashes

    def test_summary_contains_all_parameters_and_excludes_invalid_infinite_scores(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            core, before = self._project(Path(temp))
            result = generate_v15_scientific_report(core)
            self.assertEqual(REPORTER_VERSION, "V15.2.1")
            self.assertTrue(result["protected_campaign_hashes_unchanged"])
            summary_path = Path(result["parameter_sensitivity_summary"])
            group_path = Path(result["calibration_group_summary"])
            self.assertTrue(summary_path.exists())
            self.assertTrue(group_path.exists())

            summary = pd.read_excel(summary_path)
            self.assertEqual(set(summary["parameter"]), {"keff_pyrite", "feoh_s_density"})
            feoh = summary.loc[summary["parameter"].eq("feoh_s_density")].iloc[0]
            self.assertEqual(int(feoh["invalid_trial_count"]), 1)
            self.assertEqual(int(feoh["unscored_trial_count"]), 1)
            self.assertTrue(np.isfinite(float(feoh["median_abs_objective_change_per_1pct"])))
            self.assertEqual(feoh["pH_candidate_response"], "not_available")
            self.assertEqual(feoh["SO4_candidate_response"], "not_available")

            groups = pd.read_excel(group_path)
            self.assertEqual(set(groups["calibration_group"]), {"mineral_kinetics", "sorption"})
            sorption = groups.loc[groups["calibration_group"].eq("sorption")].iloc[0]
            self.assertTrue(np.isfinite(float(sorption["best_candidate_TOTAL_SCORE"])))
            self.assertEqual(int(sorption["invalid_trial_count"]), 1)
            self.assertEqual(int(sorption["unscored_trial_count"]), 1)

            report = Path(result["report_markdown"]).read_text(encoding="utf-8")
            self.assertIn("Parameter sensitivity and calibration evidence", report)
            self.assertIn("Calibration-group overview", report)
            self.assertNotIn("### Group summary", report)
            after = {path: digest(Path(path)) for path in before}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
