from __future__ import annotations

"""Standalone V14.3.4 release-guard verifier.

Run from the V14 project root:
    python .\verify_v14_3_4_hotfix.py

The verifier uses only temporary directories.  It never runs MIN3P and does
not modify agent_config.xlsx, best_parameters_V14.xlsx, or 04_results.
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

from modules.adaptive_coordinate_optimizer_V14 import (
    AdaptiveCoordinateOptimizerV14,
    OptimizerSettingsV14,
)


class _Config:
    def optimizer_v13(self):
        return {}


def memory() -> pd.DataFrame:
    return pd.DataFrame([{
        "parameter": "Kz",
        "group": "hydraulic",
        "user_status": "active",
        "status": "pass_complete",
        "phase": "complete",
        "refinement_round": 0,
        "direction_queue": "",
        "tested_increase": True,
        "tested_decrease": True,
        "pending": False,
        "futility_non_neutral_failed_pair_count": 0,
        "futility_pair_objective_rejection_count": 0,
        "futility_pair_blocked": False,
        "futility_history_hydrated": True,
        "local_futility_reason": "",
    }])


def state(*, accepted: int) -> dict:
    return {
        "pass_number": 1,
        "accepted_improvements_count": accepted,
        "pass_accepted_improvements_count": accepted,
        "current_stage": "hydraulic",
        "stage_index": 0,
        "iteration": 1,
        "valid_run_count": 1,
        "convergence_status": "running",
        "convergence_reason": "",
    }


def build(root: Path, *, max_passes: int = 2) -> AdaptiveCoordinateOptimizerV14:
    optimizer = AdaptiveCoordinateOptimizerV14(
        SimpleNamespace(results_dir=root),
        _Config(),
        settings=OptimizerSettingsV14(max_passes=max_passes),
    )
    optimizer._event = lambda payload: None
    optimizer._params = lambda **kwargs: pd.DataFrame({
            "v13_group": ["hydraulic"],
            "v13_group_enabled": [True],
            "v13_group_order": [0],
        })
    return optimizer


def main() -> None:
    temp = Path(tempfile.mkdtemp(prefix="verify_v14_3_4_"))
    try:
        no_accept = build(temp / "no_accept")
        before = memory()
        after, no_accept_state, keep = no_accept._start_next_pass(before, state(accepted=0))
        assert not keep
        assert no_accept_state["convergence_reason"] == "first_pass_complete_without_accepted_improvement"
        assert after.loc[0, "status"] == "pass_complete"

        accepted = build(temp / "accepted")
        after, accepted_state, keep = accepted._start_next_pass(memory(), state(accepted=1))
        assert keep
        assert accepted_state["pass_number"] == 2
        assert accepted_state["pass_accepted_improvements_count"] == 0
        assert after.loc[0, "status"] == "unexplored"

        capped = build(temp / "capped", max_passes=1)
        after, capped_state, keep = capped._start_next_pass(memory(), state(accepted=1))
        assert not keep
        assert capped_state["convergence_reason"] == "max_passes_hard_cap_reached"
        assert after.loc[0, "status"] == "pass_complete"

        assert no_accept.VERSION == "V14.3.4", no_accept.VERSION
    finally:
        shutil.rmtree(temp, ignore_errors=True)

    print("PASS: V14.3.4 loaded.")
    print("PASS: no-acceptance first pass converges without resetting parameters.")
    print("PASS: accepted first pass can start pass 2 and resets per-pass counter.")
    print("PASS: max_passes remains a hard cap even after acceptance.")
    print("PASS: verifier used only temporary directories; campaign files were not modified.")


if __name__ == "__main__":
    main()
