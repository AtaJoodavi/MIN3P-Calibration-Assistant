from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import shutil
import tempfile
import unittest

import pandas as pd

from modules.adaptive_coordinate_optimizer_V14 import (
    AdaptiveCoordinateOptimizerV14,
    OptimizerSettingsV14,
)


class _Config:
    def optimizer_v13(self):
        return {}


class V1434PassControlTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v14_3_4_pass_"))
        self.paths = SimpleNamespace(results_dir=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _optimizer(self, *, max_passes: int = 2):
        settings = OptimizerSettingsV14(max_passes=max_passes)
        optimizer = AdaptiveCoordinateOptimizerV14(self.paths, _Config(), settings=settings)
        optimizer._event = lambda payload: None
        optimizer._params = lambda **kwargs: pd.DataFrame({
            "v13_group": ["hydraulic"],
            "v13_group_enabled": [True],
            "v13_group_order": [0],
        })
        return optimizer

    @staticmethod
    def _memory(status: str = "pass_complete") -> pd.DataFrame:
        return pd.DataFrame([{
            "parameter": "Kz",
            "group": "hydraulic",
            "user_status": "active",
            "status": status,
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

    @staticmethod
    def _state(
        *,
        pass_number: int = 1,
        total_accepted: int = 0,
        pass_accepted: int | None = 0,
    ) -> dict:
        state = {
            "pass_number": pass_number,
            "accepted_improvements_count": total_accepted,
            "current_stage": "hydraulic",
            "stage_index": 0,
            "iteration": 5,
            "valid_run_count": 5,
            "convergence_status": "running",
            "convergence_reason": "",
        }
        if pass_accepted is not None:
            state["pass_accepted_improvements_count"] = pass_accepted
        return state

    def test_no_accepted_improvement_blocks_second_pass_without_reset(self):
        optimizer = self._optimizer(max_passes=2)
        memory = self._memory()
        original = memory.copy(deep=True)
        memory, state, keep = optimizer._start_next_pass(
            memory, self._state(pass_accepted=0)
        )
        self.assertFalse(keep)
        self.assertEqual(state["convergence_status"], "converged")
        self.assertEqual(
            state["convergence_reason"],
            "first_pass_complete_without_accepted_improvement",
        )
        self.assertEqual(memory.loc[0, "status"], original.loc[0, "status"])
        self.assertEqual(
            memory.loc[0, "direction_queue"], original.loc[0, "direction_queue"]
        )

    def test_accepted_improvement_allows_second_pass_and_resets_counter(self):
        optimizer = self._optimizer(max_passes=2)
        memory, state, keep = optimizer._start_next_pass(
            self._memory(), self._state(total_accepted=1, pass_accepted=1)
        )
        self.assertTrue(keep)
        self.assertEqual(state["pass_number"], 2)
        self.assertEqual(state["pass_accepted_improvements_count"], 0)
        self.assertEqual(state["last_completed_pass_number"], 1)
        self.assertEqual(state["last_completed_pass_accepted_improvements_count"], 1)
        self.assertEqual(memory.loc[0, "status"], "unexplored")
        self.assertEqual(memory.loc[0, "direction_queue"], "increase|decrease")

    def test_hard_max_pass_cap_blocks_even_after_acceptance(self):
        optimizer = self._optimizer(max_passes=1)
        memory, state, keep = optimizer._start_next_pass(
            self._memory(), self._state(total_accepted=1, pass_accepted=1)
        )
        self.assertFalse(keep)
        self.assertEqual(state["convergence_status"], "converged")
        self.assertEqual(state["convergence_reason"], "max_passes_hard_cap_reached")
        self.assertEqual(memory.loc[0, "status"], "pass_complete")

    def test_legacy_first_pass_state_migrates_conservatively(self):
        optimizer = self._optimizer(max_passes=2)
        memory, state, keep = optimizer._start_next_pass(
            self._memory(), self._state(total_accepted=0, pass_accepted=None)
        )
        self.assertFalse(keep)
        self.assertEqual(state["pass_accepted_improvements_count"], 0)
        self.assertEqual(
            state["pass_control_migration_note"],
            "migrated_from_total_accepted_count_for_pass_1",
        )
        self.assertEqual(
            state["convergence_reason"],
            "first_pass_complete_without_accepted_improvement",
        )
        self.assertEqual(memory.loc[0, "status"], "pass_complete")

    def test_local_futility_status_reports_actual_runtime_selectability(self):
        optimizer = self._optimizer(max_passes=2)
        memory = pd.DataFrame([
            {
                "parameter": "bc_top_pH",
                "group": "boundary_chemistry",
                "user_status": "active",
                "status": "locally_complete",
                "phase": "local_futility_complete",
                "pending": False,
            },
            {
                "parameter": "bc_top_po2",
                "group": "boundary_chemistry",
                "user_status": "inactive",
                "status": "unexplored",
                "phase": "symmetric",
                "pending": False,
            },
            {
                "parameter": "top_flux",
                "group": "boundary_chemistry",
                "user_status": "frozen",
                "status": "unexplored",
                "phase": "symmetric",
                "pending": False,
            },
        ])
        optimizer._write_memory(memory)
        optimizer._write_state(self._state())
        report = optimizer.local_futility_status()
        self.assertEqual(report["eligible_parameters"], [])
        self.assertEqual(report["runtime_selectable_parameters"], [])
        self.assertEqual(report["user_inactive_parameters"], ["bc_top_po2"])
        self.assertEqual(report["user_frozen_parameters"], ["top_flux"])
        self.assertEqual(
            report["runtime_unselectable_parameters"],
            ["bc_top_po2", "top_flux"],
        )

    def test_synchronize_legacy_state_persists_only_pass_metadata(self):
        optimizer = AdaptiveCoordinateOptimizerV14(self.paths, _Config())
        optimizer._event = lambda payload: None
        optimizer._write_state({
            "current_best_score": 9.854199,
            "pass_number": 1,
            "accepted_improvements_count": 0,
        })
        status = optimizer.synchronize_pass_control()
        self.assertTrue(status["state_migrated"])
        self.assertEqual(status["pass_accepted_improvements_count"], 0)
        self.assertFalse(status["next_pass_allowed_if_current_pass_ends_now"])


if __name__ == "__main__":
    unittest.main()
