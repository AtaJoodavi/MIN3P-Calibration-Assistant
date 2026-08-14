from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

import pandas as pd

from modules.parameter_pair_optimizer import ParameterPairOptimizer


class _Config:
    pass


class _StateManager:
    def __init__(self, statuses=None):
        self.statuses = dict(statuses or {})
        self.released = []

    def is_eligible(self, parameter, *, for_interaction=False):
        status = self.statuses.get(parameter, "active")
        return status in {"active", "reactivation_candidate", "interaction_active"}

    def activate_interaction(self, parameters, *, iteration, pair_id):
        for parameter in parameters:
            if self.statuses.get(parameter, "active") == "active":
                self.statuses[parameter] = "interaction_active"

    def release_interaction(self, parameters, *, iteration, reason):
        self.released.append((tuple(parameters), reason))
        for parameter in parameters:
            if self.statuses.get(parameter) == "interaction_active":
                self.statuses[parameter] = "active"

    def mark_direction_bounded(self, parameter, direction, *, iteration):
        self.statuses[parameter] = "bounded"


class _StepController:
    def propose(self, *, parameter, base_value, direction, lower, upper):
        factor = 1.05 if direction == "increase" else 1 / 1.05
        new = base_value * factor
        return {
            "parameter": parameter,
            "new_value": new,
            "numeric_change": new - base_value,
            "factor_requested": factor,
            "factor_applied": factor,
            "step_fraction": 0.05,
            "move_space": "relative",
        }


def _parameters():
    return pd.DataFrame([
        {"parameter": "Kz", "value": 1.0, "min": 0.1, "max": 10.0},
        {"parameter": "bottom_head", "value": 2.0, "min": 0.1, "max": 10.0},
    ])


def _definition(*, required_flags="", enabled=True):
    return pd.DataFrame([{
        "pair_id": "Kz__bottom_head",
        "parameter_a": "Kz",
        "parameter_b": "bottom_head",
        "enabled": enabled,
        "priority": 50,
        "activation_evidence": "residual_pattern",
        "required_diagnostic_flags": required_flags,
        "allowed_move_patterns": "co_direction",
        "max_trials_per_activation": 2,
        "max_activations": 2,
        "cooldown_valid_runs": 0,
        "scientific_rationale": "test fixture",
    }])


class V1435InteractionSafetyTests(unittest.TestCase):
    def build(self, temp, *, required_flags="", statuses=None):
        root = Path(temp)
        (root / "config").mkdir(parents=True)
        (root / "results").mkdir(parents=True)
        _definition(required_flags=required_flags).to_excel(
            root / "config" / "parameter_interactions.xlsx", index=False
        )
        pair = ParameterPairOptimizer(
            SimpleNamespace(agent_core_dir=root, results_dir=root / "results"), _Config()
        )
        return pair, _StateManager(statuses), _StepController()

    def test_no_evidence_is_dry_run_blocked_without_state_file_write(self):
        with tempfile.TemporaryDirectory() as temp:
            pair, states, _ = self.build(temp)
            report = pair.dry_run(
                parameters=_parameters(), state_manager=states, evidence={},
                valid_run_count=3, iteration=1,
            )
            self.assertIsNone(report["selected_pair"])
            self.assertEqual(report["reason"], "no_legal_interaction_pair")
            self.assertFalse(pair.state_file.exists())
            self.assertIn("activation_evidence_unsatisfied", [x["reason"] for x in report["evaluations"]])

    def test_required_diagnostic_flag_is_enforced(self):
        with tempfile.TemporaryDirectory() as temp:
            pair, states, _ = self.build(temp, required_flags="redox_transition_signal")
            report = pair.dry_run(
                parameters=_parameters(), state_manager=states,
                evidence={"v14_residual_diagnostics": {"status": "available", "flags": []}},
                valid_run_count=3, iteration=1,
            )
            self.assertIsNone(report["selected_pair"])
            self.assertIn("required_diagnostic_flags_missing", [x["reason"] for x in report["evaluations"]])

    def test_inactive_or_frozen_parameter_blocks_pair(self):
        for status in ("inactive", "frozen"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                pair, states, _ = self.build(temp, statuses={"bottom_head": status})
                report = pair.dry_run(
                    parameters=_parameters(), state_manager=states,
                    evidence={"v14_residual_diagnostics": {"status": "available", "flags": ["redox"]}},
                    valid_run_count=3, iteration=1,
                )
                self.assertIsNone(report["selected_pair"])
                self.assertIn("parameter_not_interaction_eligible:bottom_head", [x["reason"] for x in report["evaluations"]])

    def test_legal_evidence_selects_bounded_pair(self):
        with tempfile.TemporaryDirectory() as temp:
            pair, states, steps = self.build(temp)
            candidate = pair.next_candidate(
                parameters=_parameters(), state_manager=states, step_controller=steps,
                evidence={"v14_residual_diagnostics": {"status": "available", "flags": ["redox"]}},
                valid_run_count=3, iteration=1,
            )
            self.assertIsNotNone(candidate)
            self.assertEqual(candidate["candidate_type"], "interaction_pair")
            self.assertEqual(candidate["pair_id"], "Kz__bottom_head")
            self.assertEqual(candidate["pair_direction"], "increase|increase")
            self.assertEqual(len(candidate["moves"]), 2)
            self.assertEqual(states.statuses["Kz"], "interaction_active")

    def test_freeze_between_pair_directions_suspends_without_new_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            pair, states, steps = self.build(temp)
            evidence = {"v14_residual_diagnostics": {"status": "available", "flags": ["redox"]}}
            first = pair.next_candidate(
                parameters=_parameters(), state_manager=states, step_controller=steps,
                evidence=evidence, valid_run_count=3, iteration=1,
            )
            self.assertIsNotNone(first)
            result = pair.observe_result(
                pair_id="Kz__bottom_head", accepted=False, valid=True,
                reason="TOTAL_SCORE_not_meaningfully_improved", iteration=1,
            )
            self.assertEqual(result["pair_action"], "test_remaining_pair_direction")
            states.statuses["bottom_head"] = "frozen"

            before = pair.dry_run(
                parameters=_parameters(), state_manager=states, evidence=evidence,
                valid_run_count=4, iteration=2,
            )
            self.assertIsNone(before["selected_pair"])
            self.assertIn("parameter_not_interaction_eligible:bottom_head", [x["reason"] for x in before["evaluations"]])

            candidate = pair.next_candidate(
                parameters=_parameters(), state_manager=states, step_controller=steps,
                evidence=evidence, valid_run_count=4, iteration=2,
            )
            self.assertIsNone(candidate)
            snapshot = pair.snapshot()
            record = snapshot["pairs"]["Kz__bottom_head"]
            self.assertEqual(record["phase"], "completed")
            self.assertEqual(record["direction_queue"], [])
            self.assertTrue(record["last_completion_reason"].startswith("continuation_blocked:"))
            self.assertEqual(states.statuses["Kz"], "active")
            self.assertTrue(any(x["action"] == "interaction_pair_continuation_suspended" for x in snapshot["events"]))


if __name__ == "__main__":
    unittest.main()
