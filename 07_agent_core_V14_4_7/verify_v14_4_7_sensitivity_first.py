from __future__ import annotations

"""Deterministic smoke test for the V14.4.7 coverage-first sensitivity patch."""

import pandas as pd

from modules.adaptive_coordinate_optimizer_V14 import (
    AdaptiveCoordinateOptimizerV14,
    OptimizerSettingsV14,
)


def _bare_optimizer() -> AdaptiveCoordinateOptimizerV14:
    opt = AdaptiveCoordinateOptimizerV14.__new__(AdaptiveCoordinateOptimizerV14)
    opt.settings = OptimizerSettingsV14()
    opt.services = {}
    opt._gpt_advisory = None
    opt._event = lambda payload: None
    return opt


def main() -> None:
    opt = _bare_optimizer()

    rows = []
    for i, name in enumerate(["a", "b", "c", "d", "e"]):
        rows.append(
            {
                "parameter": name,
                "group": "hydraulic",
                "group_order": 1,
                "priority": i,
                "user_status": "active",
                "status": "unexplored",
                "phase": "symmetric",
                "direction_queue": "increase|decrease",
                "tested_increase": False,
                "tested_decrease": False,
                "pending": False,
                "step_fraction": 0.05,
                "sensitivity_score": None,
                "coverage_tested": i < 2,
                "coverage_sensitivity_score": [2.0, 5.0, None, None, None][i],
                "coverage_test_direction": "",
                "coverage_test_objective": None,
                "pending_screening_only": False,
                "last_update": f"2026-01-0{i+1}",
            }
        )

    memory = pd.DataFrame(rows)
    state = {"current_stage": "hydraulic"}

    idx, _, _ = opt._candidate_index(memory.copy(), state.copy())
    assert memory.at[idx, "parameter"] == "c", "Unscreened parameter must be selected first."

    memory["coverage_tested"] = True
    memory["coverage_sensitivity_score"] = [2.0, 5.0, 1.0, 3.0, 4.0]
    idx, _, updated = opt._candidate_index(memory.copy(), state.copy())
    assert memory.at[idx, "parameter"] == "b", "Highest-sensitivity parameter must start calibration."
    assert updated.get("coverage_first_complete") is True

    # Verify screening does not accept an improving candidate.
    screen = memory.iloc[[0]].copy()
    screen.loc[:, "pending"] = True
    screen.loc[:, "pending_candidate_id"] = "screen-a"
    screen.loc[:, "pending_direction"] = "increase"
    screen.loc[:, "pending_step_fraction"] = 0.05
    screen.loc[:, "pending_old_value"] = 1.0
    screen.loc[:, "pending_new_value"] = 1.05
    screen.loc[:, "pending_factor_applied"] = 1.05
    screen.loc[:, "pending_parent_best_run_folder"] = "baseline"
    screen.loc[:, "pending_screening_only"] = True
    screen.loc[:, "direction_queue"] = "decrease"
    screen.loc[:, "coverage_tested"] = False

    state2 = {
        "current_best_score": 10.0,
        "current_best_run_folder": "baseline",
        "pass_number": 1,
        "pass_accepted_improvements_count": 0,
    }
    event = opt._observe_single(
        screen,
        state2,
        candidate_objective=9.5,
        run_folder="screen_run",
        scientific_ok=True,
        scientific_penalty=0.0,
        valid=True,
        diagnostics={},
        min3p_run_status="success",
    )
    assert event["decision"] == "screen_only"
    assert state2["current_best_score"] == 10.0
    assert bool(screen.iloc[0]["coverage_tested"])

    print("PASS: coverage-first screening and sensitivity-guided start are working.")


if __name__ == "__main__":
    main()
