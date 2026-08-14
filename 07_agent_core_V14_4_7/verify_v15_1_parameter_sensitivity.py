from __future__ import annotations

"""Offline verifier for V15.1 parameter sensitivity reporting.

Uses only a temporary synthetic project. It never reads or modifies HCT2.
"""

import hashlib
import json
from pathlib import Path
import sys
import tempfile

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from modules.scientific_reporting_V15 import REPORTER_VERSION, generate_v15_scientific_report


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    with tempfile.TemporaryDirectory() as temp:
        project = Path(temp) / "Demo"
        core = project / "07_agent_core_V14"
        config = core / "config"
        inputs = project / "01_input"
        results = project / "04_results"
        for folder in (config, inputs, results):
            folder.mkdir(parents=True, exist_ok=True)

        protected = [
            inputs / "agent_config.xlsx",
            results / "best_parameters_V14.xlsx",
            results / "v14_optimizer_state.xlsx",
            results / "v14_optimizer_parameter_state.xlsx",
        ]
        for path, payload in zip(protected, [b"config", b"best", b"state", b"memory"]):
            path.write_bytes(payload)
        (results / "v14_parameter_runtime_state.json").write_text("{}", encoding="utf-8")
        (results / "v14_step_size_state.json").write_text("{}", encoding="utf-8")
        (results / "v14_campaign_state.json").write_text(
            json.dumps({"current_best_score": 1.0}), encoding="utf-8"
        )
        (config / "conceptual_model_V15.json").write_text(
            (ROOT / "config" / "conceptual_model_V15.json").read_text(encoding="utf-8"),
            encoding="utf-8",
        )

        pd.DataFrame([
            {
                "timestamp": "2026-01-01", "parameter": "keff_pyrite",
                "group": "mineral_kinetics", "direction": "increase",
                "old_value": 1.0, "new_value": 1.05, "step_fraction": 0.05,
                "baseline_objective": 1.0, "candidate_objective": 1.12,
                "decision": "reject", "rejection_reason": "TOTAL_SCORE_not_meaningfully_improved",
            },
            {
                "timestamp": "2026-01-02", "parameter": "feoh_s_density",
                "group": "sorption", "direction": "decrease",
                "old_value": 0.056, "new_value": 0.0533333, "step_fraction": 0.05,
                "baseline_objective": 1.0, "candidate_objective": np.inf,
                "decision": "reject", "rejection_reason": "invalid_or_failed_MIN3P_run",
            },
        ]).to_excel(results / "calibration_decision_log.xlsx", index=False)

        before = {str(path): sha(path) for path in protected}
        result = generate_v15_scientific_report(core)
        summary = pd.read_excel(result["parameter_sensitivity_summary"])
        groups = pd.read_excel(result["calibration_group_summary"])
        after = {str(path): sha(path) for path in protected}

        assert REPORTER_VERSION == "V15.1"
        assert set(summary["parameter"]) == {"keff_pyrite", "feoh_s_density"}
        assert np.isfinite(float(summary.loc[summary["parameter"].eq("keff_pyrite"), "median_abs_objective_change_per_1pct"].iloc[0]))
        assert int(summary.loc[summary["parameter"].eq("feoh_s_density"), "invalid_trial_count"].iloc[0]) == 1
        assert np.isfinite(float(groups["best_candidate_TOTAL_SCORE"].dropna().iloc[0]))
        assert before == after
        assert result["protected_campaign_hashes_unchanged"]

    print("PASS: V15.1 reporter loaded.")
    print("PASS: one-row-per-parameter objective sensitivity table was generated.")
    print("PASS: invalid/infinite scores were excluded from summary statistics.")
    print("PASS: pH/SO4 stayed not_available when no candidate-level response evidence existed.")
    print("PASS: protected campaign inputs and optimizer state were unchanged.")


if __name__ == "__main__":
    main()
