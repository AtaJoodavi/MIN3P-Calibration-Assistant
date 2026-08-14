from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
import pandas as pd

from modules.calibration_explainability_V15 import build_v15_candidate_explainability


class V153ExplainabilityTests(unittest.TestCase):
    def test_candidate_explainability_outputs_and_report_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "P"
            core = root / "07_agent_core_V14"
            results = root / "04_results"
            reports = root / "05_reports" / "V15_scientific_report"
            inputs = root / "01_input"
            for p in [core / "modules", core / "config", results, reports, inputs]:
                p.mkdir(parents=True, exist_ok=True)
            pd.DataFrame([
                {"timestamp":"t1","parameter":"s_chlorite","group":"other","direction":"increase","step_fraction":0.05,"baseline_objective":9.854199,"candidate_objective":10.100374,"decision":"reject","accepted":0,"rejection_reason":"TOTAL_SCORE_not_meaningfully_improved"},
            ]).to_excel(results / "calibration_decision_log.xlsx", index=False)
            pd.DataFrame([
                {"parameter":"s_chlorite","group":"other","status":"active","phase":"symmetric","user_status":"active"},
            ]).to_excel(results / "v14_optimizer_parameter_state.xlsx", index=False)
            (core / "config" / "gpt_supervisor_config.yaml").write_text("enabled: false\n", encoding="utf-8")
            (reports / "V15_calibration_story_report.html").write_text("<html><body><h2>Warnings</h2></body></html>", encoding="utf-8")
            (reports / "V15_calibration_story_report.md").write_text("# Report\n\n## Warnings\n", encoding="utf-8")
            result = build_v15_candidate_explainability(core)
            self.assertEqual(result["candidate_count"], 1)
            self.assertTrue((reports / "explainability" / "candidate_explanations.json").exists())
            self.assertTrue((reports / "explainability" / "candidate_explanation.xlsx").exists())
            self.assertIn("Why candidates were tested", (reports / "V15_calibration_story_report.html").read_text(encoding="utf-8"))
            self.assertTrue(result["protected_campaign_hashes_unchanged"])


if __name__ == "__main__":
    unittest.main()
