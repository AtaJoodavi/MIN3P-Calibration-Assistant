from __future__ import annotations

from pathlib import Path
import json
import tempfile
import pandas as pd

from modules.calibration_explainability_V15 import EXPLAINABILITY_VERSION, build_v15_candidate_explainability


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "P"
        core = root / "07_agent_core_V14"
        results = root / "04_results"
        reports = root / "05_reports" / "V15_scientific_report"
        inputs = root / "01_input"
        for p in [core / "modules", core / "config", results, reports, inputs]:
            p.mkdir(parents=True, exist_ok=True)
        pd.DataFrame([
            {"timestamp":"t1","parameter":"Kz","group":"hydraulic","direction":"decrease","step_fraction":0.05,"baseline_objective":10.0,"candidate_objective":9.8,"decision":"accept","accepted":1},
            {"timestamp":"t2","parameter":"s_chlorite","group":"other","direction":"increase","step_fraction":0.05,"baseline_objective":9.854199,"candidate_objective":10.100374,"decision":"reject","accepted":0,"rejection_reason":"TOTAL_SCORE_not_meaningfully_improved"},
        ]).to_excel(results / "calibration_decision_log.xlsx", index=False)
        pd.DataFrame([
            {"parameter":"Kz","group":"hydraulic","status":"active","phase":"symmetric","user_status":"active"},
            {"parameter":"s_chlorite","group":"other","status":"active","phase":"symmetric","user_status":"active"},
        ]).to_excel(results / "v14_optimizer_parameter_state.xlsx", index=False)
        (core / "config" / "gpt_supervisor_config.yaml").write_text("enabled: false\n", encoding="utf-8")
        (reports / "V15_calibration_story_report.html").write_text("<html><head><style></style></head><body><h2>Warnings</h2></body></html>", encoding="utf-8")
        (reports / "V15_calibration_story_report.md").write_text("# Report\n\n## Warnings\n", encoding="utf-8")
        result = build_v15_candidate_explainability(core)
        assert EXPLAINABILITY_VERSION == "V15.3.1"
        assert result["explainability_version"] == "V15.3.1"
        assert result["candidate_count"] == 2
        assert Path(result["outputs"]["candidate_explanation_xlsx"]).exists()
        assert (reports / "explainability" / "candidate_explanations.json").exists()

        table = pd.read_excel(result["outputs"]["candidate_explanation_xlsx"])
        latest = table.iloc[-1]
        assert latest["parameter"] == "s_chlorite"
        assert latest["display_group"] == "silicate-weathering / other"
        assert latest["outcome_text"] == "Rejected; best parameter set was retained."
        assert "worsened TOTAL_SCORE from 9.854199 to 10.100374" in latest["why_selected"]
        assert latest["scientific_interpretation"] == "Increasing chlorite weathering/scaling did not improve the composite calibration objective."

        html = (reports / "V15_calibration_story_report.html").read_text(encoding="utf-8")
        markdown = (reports / "V15_calibration_story_report.md").read_text(encoding="utf-8")
        assert "Latest candidate explanation" in html
        assert "latest-candidate-card" in html
        assert "<ul>" in html and "<li>s_chlorite was active and eligible.</li>" in html
        assert "## Latest candidate explanation" in markdown
        assert "- s_chlorite was active and eligible." in markdown
        assert result["protected_campaign_hashes_unchanged"] is True
    print("PASS: V15.3.1 explainability layer generated outputs and updated reports in a temporary project.")
    print("PASS: latest-candidate card, Markdown bullets, and HTML bullets were verified.")


if __name__ == "__main__":
    main()
