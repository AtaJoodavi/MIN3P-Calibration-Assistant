from __future__ import annotations

"""Standalone V14.3.5 interaction safety verifier.

Run from the V14 project root:
    python .\verify_v14_3_5_interaction_safety.py

The verifier uses only temporary directories. It never opens the HCT2
configuration, does not run MIN3P, and does not modify 01_input or 04_results.
"""

from pathlib import Path
from types import SimpleNamespace
import shutil
import sys
import tempfile

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.parameter_pair_optimizer import ParameterPairOptimizer


class _Config:
    pass


class _StateManager:
    def __init__(self):
        self.statuses = {}

    def is_eligible(self, parameter, *, for_interaction=False):
        return self.statuses.get(parameter, "active") in {"active", "reactivation_candidate", "interaction_active"}

    def activate_interaction(self, parameters, *, iteration, pair_id):
        for parameter in parameters:
            if self.statuses.get(parameter, "active") == "active":
                self.statuses[parameter] = "interaction_active"

    def release_interaction(self, parameters, *, iteration, reason):
        for parameter in parameters:
            if self.statuses.get(parameter) == "interaction_active":
                self.statuses[parameter] = "active"

    def mark_direction_bounded(self, parameter, direction, *, iteration):
        self.statuses[parameter] = "bounded"


class _Steps:
    def propose(self, *, parameter, base_value, direction, lower, upper):
        factor = 1.05 if direction == "increase" else 1 / 1.05
        return {
            "parameter": parameter,
            "new_value": base_value * factor,
            "numeric_change": base_value * factor - base_value,
            "factor_requested": factor,
            "factor_applied": factor,
            "step_fraction": 0.05,
            "move_space": "relative",
        }


def main() -> None:
    temp = Path(tempfile.mkdtemp(prefix="verify_v14_3_5_"))
    try:
        (temp / "config").mkdir()
        (temp / "results").mkdir()
        pd.DataFrame([{
            "pair_id": "Kz__bottom_head",
            "parameter_a": "Kz",
            "parameter_b": "bottom_head",
            "enabled": True,
            "priority": 50,
            "activation_evidence": "residual_pattern",
            "required_diagnostic_flags": "",
            "allowed_move_patterns": "co_direction",
            "max_trials_per_activation": 2,
            "max_activations": 2,
            "cooldown_valid_runs": 0,
        }]).to_excel(temp / "config" / "parameter_interactions.xlsx", index=False)
        parameters = pd.DataFrame([
            {"parameter": "Kz", "value": 1.0, "min": 0.1, "max": 10.0},
            {"parameter": "bottom_head", "value": 2.0, "min": 0.1, "max": 10.0},
        ])
        pair = ParameterPairOptimizer(SimpleNamespace(agent_core_dir=temp, results_dir=temp / "results"), _Config())
        states, steps = _StateManager(), _Steps()

        no_evidence = pair.dry_run(parameters=parameters, state_manager=states, evidence={}, valid_run_count=2, iteration=1)
        assert no_evidence["selected_pair"] is None
        assert not pair.state_file.exists(), "dry_run wrote interaction runtime state"

        evidence = {"v14_residual_diagnostics": {"status": "available", "flags": ["redox"]}}
        candidate = pair.next_candidate(parameters=parameters, state_manager=states, step_controller=steps, evidence=evidence, valid_run_count=2, iteration=1)
        assert candidate and candidate["pair_id"] == "Kz__bottom_head"
        pair.observe_result(pair_id="Kz__bottom_head", accepted=False, valid=True, reason="TOTAL_SCORE_not_meaningfully_improved", iteration=1)

        states.statuses["bottom_head"] = "frozen"
        blocked = pair.next_candidate(parameters=parameters, state_manager=states, step_controller=steps, evidence=evidence, valid_run_count=3, iteration=2)
        assert blocked is None
        snapshot = pair.snapshot()
        record = snapshot["pairs"]["Kz__bottom_head"]
        assert record["phase"] == "completed" and not record["direction_queue"]
        assert any(event["action"] == "interaction_pair_continuation_suspended" for event in snapshot["events"])
        assert pair.VERSION == "V14.3.5"
    finally:
        shutil.rmtree(temp, ignore_errors=True)

    print("PASS: V14.3.5 interaction optimizer loaded.")
    print("PASS: dry-run blocked an unsupported pair without writing runtime state.")
    print("PASS: legal residual evidence selected one bounded configured pair.")
    print("PASS: freezing a parameter between pair directions suspended continuation safely.")
    print("PASS: verifier used only temporary directories; campaign files were not modified.")


if __name__ == "__main__":
    main()
