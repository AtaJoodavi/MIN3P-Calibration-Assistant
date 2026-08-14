from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

import pandas as pd

from modules.campaign_review_V15_4 import (
    CampaignAnalysisV15_4,
    CampaignReviewPaths,
    GPTCampaignReviewerV15_4,
    generate_campaign_review_package,
)


class V154CampaignReviewTests(unittest.TestCase):
    def _project(self, root: Path) -> Path:
        core = root / "07_agent_core_V14"
        for folder in [core / "config", root / "01_input", root / "03_runs", root / "04_results", root / "05_reports"]:
            folder.mkdir(parents=True, exist_ok=True)
        (core / "config" / "gpt_campaign_review_V15_4.yaml").write_text(
            "enabled: false\nmodel: gpt-5.5\nreasoning_effort: medium\nmax_output_tokens: 5000\n",
            encoding="utf-8",
        )
        history = []
        base = {
            "run_status": "success",
            "failed_time_steps": 0,
            "failed_step_fraction": 0,
            "warnings": "charge_balance_warning",
            "initial_charge_balance_error_percent": 26.0,
            "TOTAL_SCORE": 10.0,
            "TOTAL_SCORE_OBJECTIVE_MODE": "multi_species_frozen_reference_weighted_rmse",
            "TOTAL_SCORE_INCLUDED_METRICS": "RMSE_pH, RMSE_so4-2, RMSE_al+3",
            "RMSE_pH": 1.0,
            "MAE_pH": 0.6,
            "Bias_pH": -0.6,
            "Mean_obs_pH": 5.5,
            "Mean_model_pH": 4.9,
            "RMSE_so4-2": 0.002,
            "MAE_so4-2": 0.0015,
            "Bias_so4-2": -0.0015,
            "Mean_obs_so4-2": 0.0045,
            "Mean_model_so4-2": 0.003,
            "RMSE_al+3": 3e-5,
            "MAE_al+3": 2e-5,
            "Bias_al+3": 1.8e-5,
            "Mean_obs_al+3": 2e-6,
            "Mean_model_al+3": 2e-5,
            "NORM_RMSE_pH": 1,
            "WEIGHTED_RMSE_pH": 2,
            "NORM_RMSE_so4-2": 1,
            "WEIGHTED_RMSE_so4-2": 2,
            "NORM_RMSE_al+3": 1,
            "WEIGHTED_RMSE_al+3": 1,
        }
        for index in range(13):
            row = dict(base)
            row["run_folder"] = str(root / "03_runs" / f"run_{index:03d}")
            if index:
                row["TOTAL_SCORE"] = 10.0 + index * 0.01
            history.append(row)
        pd.DataFrame(history).to_excel(root / "04_results" / "optimization_history.xlsx", index=False)

        decisions = []
        for index in range(12):
            decisions.append({
                "phase": "candidate_evaluated",
                "candidate_type": "single_parameter",
                "parameter": "Kz" if index < 6 else "keff_pyrite",
                "group": "hydraulic" if index < 6 else "mineral_kinetics",
                "direction": "increase" if index % 2 == 0 else "decrease",
                "step_fraction": 0.05 if index < 8 else 0.025,
                "baseline_objective": 10.0,
                "candidate_objective": 10.01 + index * 0.01,
                "accepted": 0,
                "decision": "reject",
                "rejection_reason": "TOTAL_SCORE_not_meaningfully_improved",
                "restoration_verified": 1,
                "current_best_objective": 10.0,
                "tradeoff_class": "species_tradeoff",
            })
        pd.DataFrame(decisions).to_excel(root / "04_results" / "calibration_decision_log.xlsx", index=False)
        return core

    def test_deterministic_analysis_and_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "P"
            core = self._project(root)
            package = generate_campaign_review_package(core, mode="auto")
            analysis = package["analysis"]
            review = package["review"]
            self.assertEqual(analysis["campaign_summary"]["total_runs"], 13)
            self.assertEqual(analysis["campaign_summary"]["baseline_runs"], 1)
            self.assertEqual(analysis["campaign_summary"]["candidate_count"], 12)
            self.assertEqual(analysis["campaign_summary"]["accepted_candidates"], 0)
            self.assertEqual(analysis["campaign_summary"]["species_tradeoff_count"], 12)
            self.assertTrue(analysis["search_summary"]["local_stagnation_detected"])
            self.assertFalse(analysis["search_summary"]["same_configuration_additional_runs_recommended"])
            self.assertEqual(package["review_metadata"]["used_mode"], "deterministic")
            self.assertEqual(review["campaign_assessment"]["status"], "stagnant")
            self.assertIn("single-parameter", review["paper_ready_conclusion"])
            for path in package["artifacts"].values():
                self.assertTrue(Path(path).exists(), path)

    def test_analysis_json_is_reusable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "P"
            core = self._project(root)
            paths = CampaignReviewPaths.from_agent_core(core)
            analyzer = CampaignAnalysisV15_4(paths)
            analysis = analyzer.build()
            artifacts = analyzer.write(analysis)
            reloaded = json.loads(Path(artifacts["campaign_analysis_json"]).read_text(encoding="utf-8"))
            reviewer = GPTCampaignReviewerV15_4(paths)
            review = reviewer.deterministic_review(reloaded)
            self.assertEqual(review["optimizer_assessment"]["implementation_status"], "behaved_consistently_with_recorded_rules")
            self.assertTrue(review["stagnation_analysis"]["local_minimum_likely"])


if __name__ == "__main__":
    unittest.main()
