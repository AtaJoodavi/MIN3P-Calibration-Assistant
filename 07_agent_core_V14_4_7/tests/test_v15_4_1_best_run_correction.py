from __future__ import annotations

from pathlib import Path
import shutil
import tempfile
import unittest

import pandas as pd

from modules.scientific_reporting_V15 import V15ProjectPaths, V15ScientificReporter


class V1541BestRunCorrectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="v15_4_1_correction_"))
        self.project = self.tmp / "HCT3"
        self.core = self.project / "07_agent_core_V14_3_7"
        self.results = self.project / "04_results"
        self.core.mkdir(parents=True)
        self.results.mkdir(parents=True)
        (self.project / "01_input").mkdir()
        (self.project / "05_reports").mkdir()

        baseline = {
            "timestamp": "2026-01-01 00:00:00",
            "run_folder": str(self.project / "03_runs" / "run_baseline"),
            "run_status": "success",
            "TOTAL_SCORE": 10.0,
            "TOTAL_SCORE_OBJECTIVE_MODE": "multi_species_frozen_reference_weighted_rmse",
            "TOTAL_SCORE_INCLUDED_METRICS": "RMSE_pH, RMSE_so4-2",
            "TOTAL_SCORE_CONFIGURED_METRICS": "RMSE_pH, RMSE_so4-2",
            "RMSE_pH": 1.0,
            "Mean_obs_pH": 5.5,
            "Mean_model_pH": 4.8,
            "Bias_pH": -0.7,
            "NORM_RMSE_pH": 1.0,
            "WEIGHTED_RMSE_pH": 2.0,
            "RMSE_so4-2": 0.002,
            "Mean_obs_so4-2": 0.004,
            "Mean_model_so4-2": 0.002,
            "Bias_so4-2": -0.002,
            "NORM_RMSE_so4-2": 1.0,
            "WEIGHTED_RMSE_so4-2": 2.0,
        }
        best = dict(baseline)
        best.update({
            "timestamp": "2026-01-02 00:00:00",
            "run_folder": str(self.project / "03_runs" / "run_best"),
            "TOTAL_SCORE": 8.0,
            "RMSE_pH": 0.8,
            "Mean_model_pH": 5.0,
            "Bias_pH": -0.5,
            "NORM_RMSE_pH": 0.8,
            "WEIGHTED_RMSE_pH": 1.6,
            "RMSE_so4-2": 0.0015,
            "Mean_model_so4-2": 0.0025,
            "Bias_so4-2": -0.0015,
            "NORM_RMSE_so4-2": 0.75,
            "WEIGHTED_RMSE_so4-2": 1.5,
        })
        pd.DataFrame([baseline, best]).to_excel(self.results / "run_ranking.xlsx", index=False)

        failed_id = "failed_then_repaired"
        decisions = [
            {
                "timestamp": "2026-01-01T01:00:00+00:00",
                "phase": "candidate_evaluated",
                "candidate_id": failed_id,
                "candidate_type": "single_parameter",
                "parameter": "keff_pyrite",
                "group": "mineral_kinetics",
                "direction": "decrease",
                "baseline_objective": 10.0,
                "candidate_objective": 10.2,
                "accepted": 0,
                "decision": "optimizer_observe_run_failed",
                "rejection_reason": "optimizer observe_run failed: PermissionError",
                "current_best_objective": 10.0,
            },
            {
                "timestamp": "2026-01-02T01:00:00+00:00",
                "phase": "candidate_evaluated",
                "candidate_id": "accepted_best",
                "candidate_type": "single_parameter",
                "parameter": "keff_calcite",
                "group": "mineral_kinetics",
                "direction": "increase",
                "baseline_objective": 10.0,
                "candidate_objective": 8.0,
                "accepted": 1,
                "decision": "accept",
                "restoration_verified": 1,
                "current_best_objective": 8.0,
                "current_best_run_folder": str(self.project / "03_runs" / "run_best"),
            },
        ]
        pd.DataFrame(decisions).to_excel(self.results / "calibration_decision_log.xlsx", index=False)
        pd.DataFrame([{"parameter": "keff_pyrite", "pending": False, "pending_candidate_id": ""}]).to_excel(
            self.results / "v14_optimizer_parameter_state.xlsx", index=False
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_best_run_and_recovered_transaction_are_reported_correctly(self) -> None:
        reporter = V15ScientificReporter(V15ProjectPaths.from_agent_core(self.core), campaign_review_mode="off")
        trials, _ = reporter._decision_table()
        self.assertEqual(trials["decision_class"].value_counts().to_dict(), {"rejected": 1, "accepted": 1})

        package = {
            "analysis": {
                "campaign_summary": {
                    "accepted_candidates": 1,
                    "rejected_candidates": 0,
                    "unresolved_candidates": 1,
                },
                "objective_summary": {"initial_score": 10.0, "best_score": 8.0},
                "species_summary": [],
            },
            "review": {
                "executive_summary": "old",
                "optimizer_assessment": {"evidence": ["1 unresolved candidate"], "possible_software_issues": []},
                "scientific_findings": [],
                "recommended_actions": [],
            },
            "review_metadata": {"used_mode": "deterministic"},
            "artifacts": {},
        }
        corrected = reporter._apply_v15_4_1_corrections(package)
        analysis = corrected["analysis"]
        self.assertEqual(analysis["objective_summary"]["objective_mode"], "multi_species_frozen_reference_weighted_rmse")
        self.assertEqual(analysis["campaign_summary"]["unresolved_candidates"], 0)
        self.assertEqual(analysis["campaign_summary"]["rejected_candidates"], 1)
        species = {row["species"]: row for row in analysis["species_summary"]}
        self.assertAlmostEqual(species["pH"]["rmse"], 0.8)
        self.assertAlmostEqual(species["so4-2"]["rmse"], 0.0015)
        self.assertEqual(species["pH"]["evidence_role"], "protected_best_accepted_run")


if __name__ == "__main__":
    unittest.main()
