from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
import sys
import tempfile
import unittest

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from modules.scientific_reporting_V15 import generate_v15_scientific_report


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class V15ScientificReportingTests(unittest.TestCase):
    def _project(self, root: Path, include_metrics: bool = False) -> tuple[Path, dict[str, str]]:
        project = root / "HCT2"
        core = project / "07_agent_core_V14"
        (core / "config").mkdir(parents=True)
        (project / "01_input").mkdir(parents=True)
        fixture = ROOT / "tests" / "HCT_one_layer_fixture.dat"
        if not fixture.is_file():
            raise FileNotFoundError(f"Missing test DAT fixture: {fixture}")
        shutil.copy2(fixture, project / "01_input" / "HCT.dat")
        (project / "04_results").mkdir(parents=True)
        (project / "01_input" / "agent_config.xlsx").write_bytes(b"config")
        (project / "04_results" / "best_parameters_V14.xlsx").write_bytes(b"best")
        (project / "04_results" / "v14_optimizer_state.xlsx").write_bytes(b"state")
        (project / "04_results" / "v14_optimizer_parameter_state.xlsx").write_bytes(b"memory")
        (project / "04_results" / "v14_parameter_runtime_state.json").write_text("{}", encoding="utf-8")
        (project / "04_results" / "v14_step_size_state.json").write_text("{}", encoding="utf-8")
        (project / "04_results" / "v14_campaign_state.json").write_text(json.dumps({"current_best_score": 9.8}), encoding="utf-8")
        default_config = ROOT / "config" / "conceptual_model_V15.json"
        (core / "config" / "conceptual_model_V15.json").write_text(default_config.read_text(encoding="utf-8"), encoding="utf-8")
        rows = [
            {"timestamp": "2026-01-01", "parameter": "keff_pyrite", "group": "mineral_kinetics", "direction": "increase", "old_value": 1.0, "new_value": 1.05, "step_fraction": 0.05, "baseline_objective": 9.8, "candidate_objective": 10.1, "decision": "reject", "rejection_reason": "TOTAL_SCORE_not_meaningfully_improved", "restoration_verified": True},
            {"timestamp": "2026-01-02", "parameter": "bc_top_pH", "group": "boundary_chemistry", "direction": "decrease", "old_value": 5.6, "new_value": 5.32, "step_fraction": 0.05, "baseline_objective": 9.8, "candidate_objective": 9.9, "decision": "reject", "rejection_reason": "TOTAL_SCORE_not_meaningfully_improved", "restoration_verified": True},
        ]
        if include_metrics:
            rows[0]["delta_pH"] = -0.12
            rows[0]["delta_SO4"] = 5.2
            rows[1]["delta_pH"] = 0.04
            rows[1]["delta_SO4"] = -1.3
        pd.DataFrame(rows).to_excel(project / "04_results" / "calibration_decision_log.xlsx", index=False)
        protected = {
            str(project / "01_input" / "agent_config.xlsx"): digest(project / "01_input" / "agent_config.xlsx"),
            str(project / "04_results" / "best_parameters_V14.xlsx"): digest(project / "04_results" / "best_parameters_V14.xlsx"),
            str(project / "04_results" / "v14_optimizer_state.xlsx"): digest(project / "04_results" / "v14_optimizer_state.xlsx"),
        }
        return core, protected

    def test_reporting_is_read_only_and_generates_all_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            core, before = self._project(Path(temp))
            result = generate_v15_scientific_report(core)
            self.assertTrue(result["protected_campaign_hashes_unchanged"])
            self.assertEqual(result["trial_count"], 2)
            self.assertFalse(result["pH_SO4_metrics_available"])
            for path in [result["report_markdown"], result["report_html"], result["conceptual_svg"], result["timeline_png"], result["process_map_svg"], result["response_atlas_png"]]:
                self.assertTrue(Path(path).exists(), path)
            after = {path: digest(Path(path)) for path in before}
            self.assertEqual(before, after)

    def test_explicit_delta_columns_enable_response_atlas_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            core, _ = self._project(Path(temp), include_metrics=True)
            result = generate_v15_scientific_report(core)
            self.assertTrue(result["pH_SO4_metrics_available"])
            metrics = pd.read_excel(result["pH_SO4_response_metrics"])
            self.assertTrue(metrics["pH_metric_status"].eq("available").all())
            self.assertTrue(metrics["SO4_metric_status"].eq("available").all())

    def test_run_diagnostic_metrics_are_joined_only_by_explicit_run_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            core, _ = self._project(Path(temp))
            project = core.parent
            run_folder = str(project / "03_runs" / "run_001")
            decision_path = project / "04_results" / "calibration_decision_log.xlsx"
            decisions = pd.read_excel(decision_path)
            decisions["run_folder"] = [run_folder, str(project / "03_runs" / "run_002")]
            decisions.to_excel(decision_path, index=False)
            pd.DataFrame([
                {"run_folder": run_folder, "Bias_pH": -0.32, "Relative_Bias_SO4": 0.41},
            ]).to_excel(project / "04_results" / "run_diagnostics_V11.xlsx", index=False)
            result = generate_v15_scientific_report(core)
            self.assertTrue(result["pH_SO4_metrics_available"])
            metrics = pd.read_excel(result["pH_SO4_response_metrics"])
            self.assertEqual(metrics.loc[0, "metric_kind"], "diagnostic_metric")
            self.assertEqual(metrics.loc[0, "pH_metric_name"], "Bias_pH")
            self.assertTrue(pd.isna(metrics.loc[1, "pH_metric"]))


if __name__ == "__main__":
    unittest.main()
