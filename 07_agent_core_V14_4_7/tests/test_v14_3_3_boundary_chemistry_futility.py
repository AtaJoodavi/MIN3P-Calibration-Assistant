from __future__ import annotations

"""Regression tests for the V14.3.3 controlled local-futility scope.

V14.3.3 extends the already validated local-futility rule to independent
boundary-chemistry coordinate search.  It does not enable the rule for other
groups, and it does not alter the two-valid-pair / invalid-history safeguards
introduced in V14.3.2.
"""

from pathlib import Path
from types import SimpleNamespace
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


def _history_rows(parameter: str, steps: list[float], *, invalid_steps: set[float] | None = None) -> list[dict]:
    invalid_steps = invalid_steps or set()
    rows: list[dict] = []
    for step in steps:
        for direction in ("increase", "decrease"):
            invalid = step in invalid_steps
            rows.append(
                {
                    "candidate_type": "single_parameter",
                    "parameter": parameter,
                    "direction": direction,
                    "step_fraction": step,
                    "decision": "reject",
                    "rejection_reason": (
                        "invalid_or_failed_MIN3P_run"
                        if invalid
                        else "TOTAL_SCORE_not_meaningfully_improved"
                    ),
                    "valid_run": not invalid,
                    "accepted": False,
                }
            )
    return rows


def _memory_row(parameter: str, group: str) -> dict:
    return {
        "parameter": parameter,
        "group": group,
        "status": "refine_symmetric",
        "phase": "symmetric",
        "pending": False,
        "direction_queue": "increase|decrease",
        "tested_increase": False,
        "tested_decrease": False,
        "step_fraction": 0.00625,
        "futility_non_neutral_failed_pair_count": 0,
        "futility_pair_objective_rejection_count": 0,
        "futility_pair_blocked": False,
        "futility_history_hydrated": False,
        "local_futility_reason": "",
        "last_completion_at": "",
        "last_update": "",
    }


class TestV1433BoundaryChemistryLocalFutility(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        paths = SimpleNamespace(results_dir=Path(self.tmp.name))
        self.optimizer = AdaptiveCoordinateOptimizerV14(paths, _Config())

    def tearDown(self):
        self.tmp.cleanup()

    def test_scope_is_explicit_and_limited(self):
        self.assertEqual(
            self.optimizer.settings.local_futility_groups,
            ("sorption", "boundary_chemistry"),
        )
        self.assertTrue(self.optimizer._local_futility_applies(pd.Series({"group": "sorption"})))
        self.assertTrue(
            self.optimizer._local_futility_applies(
                pd.Series({"group": "boundary_chemistry"})
            )
        )
        self.assertFalse(self.optimizer._local_futility_applies(pd.Series({"group": "hydraulic"})))
        self.assertFalse(self.optimizer._local_futility_applies(pd.Series({"group": "mineral_kinetics"})))

    def test_boundary_chemistry_completes_after_two_valid_rejected_pairs(self):
        self.optimizer._write(
            self.optimizer.decision_file,
            pd.DataFrame(_history_rows("bc_top_pH", [0.05, 0.025, 0.0125])),
        )
        memory = pd.DataFrame([_memory_row("bc_top_pH", "boundary_chemistry")])
        state = {"iteration": 0, "valid_run_count": 6}

        completed = self.optimizer._apply_local_futility_gate(
            memory,
            state,
            source="v14_3_3_regression_test",
            force_history_refresh=True,
        )

        self.assertEqual(completed, ["bc_top_pH"])
        self.assertEqual(memory.at[0, "status"], "locally_complete")
        self.assertEqual(memory.at[0, "phase"], "local_futility_complete")
        self.assertEqual(memory.at[0, "direction_queue"], "")
        self.assertIn("valid_steps=0.05|0.025", memory.at[0, "local_futility_reason"])

    def test_boundary_chemistry_invalid_pair_does_not_block_two_later_valid_pairs(self):
        self.optimizer._write(
            self.optimizer.decision_file,
            pd.DataFrame(
                _history_rows(
                    "bc_top_pH",
                    [0.05, 0.025, 0.0125],
                    invalid_steps={0.05},
                )
            ),
        )
        memory = pd.DataFrame([_memory_row("bc_top_pH", "boundary_chemistry")])
        state = {"iteration": 0, "valid_run_count": 4}

        completed = self.optimizer._apply_local_futility_gate(
            memory,
            state,
            source="v14_3_3_regression_test",
            force_history_refresh=True,
        )

        self.assertEqual(completed, ["bc_top_pH"])
        self.assertEqual(memory.at[0, "status"], "locally_complete")
        self.assertIn(
            "historical_valid_pairs_after_invalid_trial",
            memory.at[0, "local_futility_reason"],
        )
        self.assertIn("invalid_steps=0.05", memory.at[0, "local_futility_reason"])

    def test_unapproved_groups_remain_outside_local_futility(self):
        self.optimizer._write(
            self.optimizer.decision_file,
            pd.DataFrame(_history_rows("Kz", [0.05, 0.025, 0.0125])),
        )
        memory = pd.DataFrame([_memory_row("Kz", "hydraulic")])
        state = {"iteration": 0, "valid_run_count": 6}

        completed = self.optimizer._apply_local_futility_gate(
            memory,
            state,
            source="v14_3_3_regression_test",
            force_history_refresh=True,
        )

        self.assertEqual(completed, [])
        self.assertEqual(memory.at[0, "status"], "refine_symmetric")
        self.assertEqual(memory.at[0, "futility_non_neutral_failed_pair_count"], 0)


if __name__ == "__main__":
    unittest.main()
