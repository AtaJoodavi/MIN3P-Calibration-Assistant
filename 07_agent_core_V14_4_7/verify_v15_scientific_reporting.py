from __future__ import annotations

"""Offline verifier for V15.0 scientific reporting.

Uses temporary synthetic campaign evidence only. No HCT2 files are read or
modified.
"""

from pathlib import Path
import hashlib
import json
import sys
import tempfile

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from modules.scientific_reporting_V15 import generate_v15_scientific_report


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "Demo"
        core = root / "07_agent_core_V14"
        config = core / "config"
        input_dir = root / "01_input"
        results = root / "04_results"
        for folder in (config, input_dir, results):
            folder.mkdir(parents=True, exist_ok=True)

        (input_dir / "agent_config.xlsx").write_bytes(b"protected-config")
        (results / "best_parameters_V14.xlsx").write_bytes(b"protected-best")
        (results / "v14_optimizer_state.xlsx").write_bytes(b"protected-state")
        (results / "v14_optimizer_parameter_state.xlsx").write_bytes(b"protected-memory")
        (results / "v14_parameter_runtime_state.json").write_text("{}", encoding="utf-8")
        (results / "v14_step_size_state.json").write_text("{}", encoding="utf-8")
        (results / "v14_campaign_state.json").write_text(json.dumps({"current_best_score": 1.234}), encoding="utf-8")

        (config / "conceptual_model_V15.json").write_text((ROOT / "config" / "conceptual_model_V15.json").read_text(encoding="utf-8"), encoding="utf-8")
        pd.DataFrame([
            {
                "timestamp": "2026-01-01T09:00:00+00:00",
                "candidate_id": "a",
                "parameter": "keff_pyrite",
                "group": "mineral_kinetics",
                "direction": "increase",
                "old_value": 1.0,
                "new_value": 1.05,
                "step_fraction": 0.05,
                "baseline_objective": 1.234,
                "candidate_objective": 1.300,
                "decision": "reject",
                "rejection_reason": "TOTAL_SCORE_not_meaningfully_improved",
                "restoration_verified": True,
            },
            {
                "timestamp": "2026-01-01T10:00:00+00:00",
                "candidate_id": "b",
                "parameter": "phi_calcite",
                "group": "mineral_kinetics",
                "direction": "decrease",
                "old_value": 0.02,
                "new_value": 0.019,
                "step_fraction": 0.05,
                "baseline_objective": 1.234,
                "candidate_objective": 1.220,
                "decision": "accept",
                "rejection_reason": "",
                "restoration_verified": True,
            },
        ]).to_excel(results / "calibration_decision_log.xlsx", index=False)

        protected = [input_dir / "agent_config.xlsx", results / "best_parameters_V14.xlsx", results / "v14_optimizer_state.xlsx"]
        before = {str(path): sha(path) for path in protected}
        result = generate_v15_scientific_report(core)
        after = {str(path): sha(path) for path in protected}
        assert result["protected_campaign_hashes_unchanged"], result
        assert before == after
        assert result["trial_count"] == 2
        for key in ["report_markdown", "report_html", "manifest", "conceptual_svg", "conceptual_png", "timeline_png", "process_map_svg", "response_atlas_png", "parameter_trial_effects", "pH_SO4_response_metrics"]:
            assert Path(result[key]).exists(), key
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        assert manifest["protected_campaign_hashes_unchanged"] is True
        assert manifest["pH_SO4_metrics"]["available"] is False

    print("PASS: V15.0 reporter generated conceptual, timeline, process-map and response-atlas artifacts.")
    print("PASS: absent candidate pH/SO4 metrics were recorded as not_available, not invented.")
    print("PASS: protected V14 campaign files were unchanged.")


if __name__ == "__main__":
    main()
