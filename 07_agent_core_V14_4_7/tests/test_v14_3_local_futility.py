from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import shutil
import tempfile
import unittest

import pandas as pd

from modules.adaptive_coordinate_optimizer_V14 import AdaptiveCoordinateOptimizerV14


class _Config:
    def optimizer_v13(self):
        return {}


class V143LocalFutilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v14_3_futility_"))
        self.paths = SimpleNamespace(results_dir=self.tmp)
        self.optimizer = AdaptiveCoordinateOptimizerV14(self.paths, _Config())
        self.state = {
            "current_stage": "sorption",
            "stage_index": 2,
            "iteration": 20,
            "valid_run_count": 20,
            "current_best_score": 9.854199,
        }
        self.memory = pd.DataFrame([{
            "parameter": "feoh_s_area",
            "group": "sorption",
            "status": "refine_symmetric",
            "phase": "symmetric",
            "direction_queue": "increase|decrease",
            "pending": False,
            "tested_increase": False,
            "tested_decrease": False,
            "refinement_round": 2,
            "futility_non_neutral_failed_pair_count": 0,
            "futility_pair_objective_rejection_count": 0,
            "futility_pair_blocked": False,
            "futility_history_hydrated": False,
            "local_futility_reason": "",
            "last_update": "",
            "last_completion_at": "",
            "continuation_direction": "",
        }])
        self.optimizer._ensure_memory = lambda: self.memory.copy()
        self.optimizer._write_memory = self._capture_memory
        self.optimizer._read_state = lambda: dict(self.state)
        self.optimizer._write_state = self._capture_state
        self.optimizer._params = lambda **kwargs: pd.DataFrame({"parameter": ["feoh_s_area"], "v13_group": ["sorption"]})
        self.optimizer._event = lambda payload: None

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _capture_memory(self, memory):
        self.memory = memory.copy()

    def _capture_state(self, state):
        self.state = dict(state)

    def _write_decisions(self, rows):
        pd.DataFrame(rows).to_excel(self.optimizer.decision_file, index=False)

    @staticmethod
    def _pair(step, reason="TOTAL_SCORE_not_meaningfully_improved"):
        return [
            {"parameter": "feoh_s_area", "candidate_type": "single_parameter", "step_fraction": step, "direction": "increase", "decision": "reject", "valid_run": True, "accepted": False, "rejection_reason": reason},
            {"parameter": "feoh_s_area", "candidate_type": "single_parameter", "step_fraction": step, "direction": "decrease", "decision": "reject", "valid_run": True, "accepted": False, "rejection_reason": reason},
        ]

    def test_two_completed_objective_pairs_mark_locally_complete(self):
        self._write_decisions(self._pair(0.05) + self._pair(0.025))
        summary = self.optimizer.apply_local_futility_gate(advance_stage=False)
        self.assertEqual(summary["locally_completed_parameters"], ["feoh_s_area"])
        self.assertEqual(self.memory.loc[0, "status"], "locally_complete")
        self.assertEqual(self.memory.loc[0, "phase"], "local_futility_complete")
        self.assertEqual(self.memory.loc[0, "direction_queue"], "")

    def test_near_neutral_pair_blocks_local_futility(self):
        rows = self._pair(0.05) + self._pair(0.025, reason="near_neutral_change")
        self._write_decisions(rows)
        summary = self.optimizer.apply_local_futility_gate(advance_stage=False)
        self.assertEqual(summary["locally_completed_parameters"], [])
        self.assertEqual(self.memory.loc[0, "status"], "refine_symmetric")

    def test_invalid_pair_does_not_block_two_later_valid_objective_pairs(self):
        rows = self._pair(0.025) + self._pair(0.0125)
        rows.extend([
            {"parameter": "feoh_s_area", "candidate_type": "single_parameter", "step_fraction": 0.05, "direction": "increase", "decision": "reject", "valid_run": True, "accepted": False, "rejection_reason": "TOTAL_SCORE_not_meaningfully_improved"},
            {"parameter": "feoh_s_area", "candidate_type": "single_parameter", "step_fraction": 0.05, "direction": "decrease", "decision": "reject", "valid_run": False, "accepted": False, "rejection_reason": "invalid_or_failed_MIN3P_run"},
        ])
        self._write_decisions(rows)
        # Simulate a campaign already hydrated by the old V14.3 rule: it had
        # counted the 2.5% pair but treated the historical invalid 5% pair as
        # a permanent block. V14.3.2 must force a corrected re-evaluation.
        self.memory.loc[0, "futility_history_hydrated"] = True
        self.memory.loc[0, "futility_non_neutral_failed_pair_count"] = 1
        self.memory.loc[0, "futility_pair_blocked"] = True
        self.memory.loc[0, "local_futility_reason"] = "historical_pair_not_eligible"
        summary = self.optimizer.apply_local_futility_gate(advance_stage=False)
        self.assertEqual(summary["locally_completed_parameters"], ["feoh_s_area"])
        self.assertEqual(self.memory.loc[0, "status"], "locally_complete")
        self.assertIn("historical_valid_pairs_after_invalid_trial", self.memory.loc[0, "local_futility_reason"])

    def test_accepted_event_resets_historical_futility_window(self):
        rows = self._pair(0.05) + self._pair(0.025)
        rows.append({"parameter": "feoh_s_area", "candidate_type": "single_parameter", "step_fraction": 0.05, "direction": "increase", "decision": "accept", "valid_run": True, "accepted": True, "rejection_reason": ""})
        self._write_decisions(rows)
        summary = self.optimizer.apply_local_futility_gate(advance_stage=False)
        self.assertEqual(summary["locally_completed_parameters"], [])
        self.assertEqual(self.memory.loc[0, "status"], "refine_symmetric")


if __name__ == "__main__":
    unittest.main()
