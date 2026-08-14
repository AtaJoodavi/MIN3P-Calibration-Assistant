from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from modules.adaptive_coordinate_optimizer_V14 import AdaptiveCoordinateOptimizerV14
from modules.parameter_state_manager import ParameterStateManager
from modules.step_size_controller import StepSizeController
from modules.calibration_strategy_manager import CalibrationStrategyManager


class DummyPaths:
    def __init__(self, root: Path):
        self.results_dir = root / "04_results"
        self.agent_core_dir = root / "07_agent_core_V14"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.agent_core_dir.mkdir(parents=True, exist_ok=True)


class DummyConfig:
    def __init__(self, parameters: pd.DataFrame):
        self.frame = parameters.copy()

    def parameters(self):
        return self.frame.copy()

    def optimizer_v13(self):
        return {}

    def sheet(self, name: str):
        if name == "v13_parameter_groups":
            return pd.DataFrame([
                {"parameter": "Kz", "group": "hydraulic", "enabled": "yes", "priority": 1},
                {"parameter": "bottom_head", "group": "hydraulic", "enabled": "yes", "priority": 2},
            ])
        raise ValueError(name)

    def apply(self, suggestion: pd.DataFrame):
        for _, row in suggestion.iterrows():
            mask = self.frame["parameter"].astype(str).str.casefold().eq(str(row["parameter"]).casefold())
            self.frame.loc[mask, "value"] = float(row["new_value"])


def build_optimizer(root: Path, *, kz_status: str = "active"):
    paths = DummyPaths(root)
    config = DummyConfig(pd.DataFrame([
        {"parameter": "Kz", "value": 1.0, "min": 0.1, "max": 10.0, "status": kz_status},
        {"parameter": "bottom_head", "value": 0.0, "min": -2.0, "max": 2.0, "status": "active"},
    ]))
    optimizer = AdaptiveCoordinateOptimizerV14(paths, config)
    optimizer.attach_services(
        state_manager=ParameterStateManager(paths, config),
        step_controller=StepSizeController(paths, config),
        strategy_manager=CalibrationStrategyManager(paths, config),
    )
    optimizer.initialize(10.0, "baseline")
    return optimizer, config


class TestV14DirectionalSafety(unittest.TestCase):
    def test_rejected_kz_increase_immediately_tests_kz_decrease(self):
        with tempfile.TemporaryDirectory() as temp:
            optimizer, config = build_optimizer(Path(temp))
            first = optimizer.next_suggestion()
            self.assertEqual(first.iloc[0]["parameter"], "Kz")
            self.assertEqual(first.iloc[0]["direction"], "increase")
            self.assertTrue(optimizer.validate_suggestion_precedence(suggestion=first)["ok"])
            config.apply(first)
            optimizer.observe_run(11.0, "run_bad", valid=True, scientific_ok=True)
            second = optimizer.next_suggestion()
            self.assertEqual(second.iloc[0]["parameter"], "Kz")
            self.assertEqual(second.iloc[0]["direction"], "decrease")

    def test_rejected_kz_increase_cannot_jump_to_bottom_head(self):
        with tempfile.TemporaryDirectory() as temp:
            optimizer, config = build_optimizer(Path(temp))
            first = optimizer.next_suggestion()
            config.apply(first)
            optimizer.observe_run(11.0, "run_bad", valid=True, scientific_ok=True)
            second = optimizer.next_suggestion()
            self.assertNotEqual(second.iloc[0]["parameter"], "bottom_head")
            self.assertEqual(second.iloc[0]["parameter"], "Kz")

    def test_accepted_increase_continues_increase(self):
        with tempfile.TemporaryDirectory() as temp:
            optimizer, config = build_optimizer(Path(temp))
            first = optimizer.next_suggestion()
            config.apply(first)
            optimizer.observe_run(9.0, "run_good", valid=True, scientific_ok=True)
            second = optimizer.next_suggestion()
            self.assertEqual(second.iloc[0]["parameter"], "Kz")
            self.assertEqual(second.iloc[0]["direction"], "increase")

    def test_user_frozen_parameter_is_never_selected(self):
        with tempfile.TemporaryDirectory() as temp:
            optimizer, config = build_optimizer(Path(temp), kz_status="frozen")
            suggestion = optimizer.next_suggestion()
            self.assertEqual(suggestion.iloc[0]["parameter"], "bottom_head")

    def test_step_grows_after_repeated_acceptance_and_shrinks_after_completed_failure(self):
        with tempfile.TemporaryDirectory() as temp:
            optimizer, config = build_optimizer(Path(temp))
            first = optimizer.next_suggestion(); config.apply(first)
            optimizer.observe_run(9.0, "good1", valid=True, scientific_ok=True)
            second = optimizer.next_suggestion(); config.apply(second)
            optimizer.observe_run(8.0, "good2", valid=True, scientific_ok=True)
            memory = pd.read_excel(optimizer.memory_file)
            step_after_growth = float(memory.loc[memory["parameter"].eq("Kz"), "step_fraction"].iloc[0])
            self.assertGreaterEqual(step_after_growth, 0.05)
            third = optimizer.next_suggestion(); config.apply(third)
            optimizer.observe_run(9.0, "bad1", valid=True, scientific_ok=True)
            fourth = optimizer.next_suggestion(); config.apply(fourth)
            optimizer.observe_run(9.0, "bad2", valid=True, scientific_ok=True)
            memory = pd.read_excel(optimizer.memory_file)
            step_after_failure = float(memory.loc[memory["parameter"].eq("Kz"), "step_fraction"].iloc[0])
            self.assertLessEqual(step_after_failure, step_after_growth)


if __name__ == "__main__":
    unittest.main()

class TestV14InteractionPairs(unittest.TestCase):
    def test_enabled_pair_completes_its_opposite_pattern_before_single_selection(self):
        from modules.parameter_pair_optimizer import ParameterPairOptimizer
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            optimizer, config = build_optimizer(root)
            pair_dir = root / "07_agent_core_V14" / "config"
            pair_dir.mkdir(parents=True, exist_ok=True)
            pd.DataFrame([{
                "pair_id": "Kz__bottom_head",
                "parameter_a": "Kz",
                "parameter_b": "bottom_head",
                "group": "hydraulic",
                "enabled": True,
                "priority": 1,
                "activation_evidence": "always",
                "allowed_move_patterns": "co_direction",
                "max_trials_per_activation": 2,
                "max_activations": 1,
                "cooldown_valid_runs": 99,
            }]).to_excel(pair_dir / "parameter_interactions.xlsx", index=False)
            optimizer.attach_services(pair_optimizer=ParameterPairOptimizer(optimizer.paths, config))
            first = optimizer.next_suggestion()
            self.assertEqual(len(first), 2)
            self.assertTrue((first["candidate_type"] == "interaction_pair").all())
            self.assertEqual(first.iloc[0]["pair_direction"], "increase|increase")
            self.assertTrue(optimizer.validate_suggestion_precedence(suggestion=first)["ok"])
            config.apply(first)
            optimizer.observe_run(11.0, "pair_bad", valid=True, scientific_ok=True)
            second = optimizer.next_suggestion()
            self.assertEqual(len(second), 2)
            self.assertEqual(second.iloc[0]["pair_id"], "Kz__bottom_head")
            self.assertEqual(second.iloc[0]["pair_direction"], "decrease|decrease")

class TestV14RuntimeStates(unittest.TestCase):
    def test_exhausted_parameter_reactivates_after_valid_run_cooldown_but_user_frozen_does_not(self):
        with tempfile.TemporaryDirectory() as temp:
            optimizer, config = build_optimizer(Path(temp), kz_status="active")
            manager = optimizer.state_manager
            manager.mark_temporarily_exhausted(
                "Kz", iteration=1, valid_run_count=0, reason="test", cooldown_valid_runs=1
            )
            self.assertFalse(manager.is_eligible("Kz"))
            reactivated = manager.periodic_reactivation(valid_run_count=1, iteration=2)
            self.assertIn("Kz", reactivated)
            self.assertTrue(manager.is_eligible("Kz"))
            manager.transition("bottom_head", "frozen_by_user", reason="test user lock", iteration=1)
            manager.periodic_reactivation(valid_run_count=99, iteration=99, new_best=True)
            self.assertFalse(manager.is_eligible("bottom_head"))


class TestV14GlobalStepRounds(unittest.TestCase):
    def test_larger_unexplored_step_precedes_smaller_refinement(self):
        """A 5% untouched peer must precede a 2.5% refinement in one stage."""
        with tempfile.TemporaryDirectory() as temp:
            optimizer, _ = build_optimizer(Path(temp))
            memory = optimizer._ensure_memory()
            kz_idx = memory.index[memory["parameter"].astype(str).eq("Kz")][0]
            bottom_idx = memory.index[memory["parameter"].astype(str).eq("bottom_head")][0]

            memory.at[kz_idx, "status"] = "refine_symmetric"
            memory.at[kz_idx, "phase"] = "symmetric"
            memory.at[kz_idx, "step_fraction"] = 0.025
            memory.at[kz_idx, "refinement_round"] = 1
            memory.at[kz_idx, "direction_queue"] = "increase|decrease"
            memory.at[kz_idx, "tested_increase"] = False
            memory.at[kz_idx, "tested_decrease"] = False

            memory.at[bottom_idx, "status"] = "unexplored"
            memory.at[bottom_idx, "phase"] = "symmetric"
            memory.at[bottom_idx, "step_fraction"] = 0.05
            memory.at[bottom_idx, "refinement_round"] = 0
            memory.at[bottom_idx, "direction_queue"] = "increase|decrease"
            memory.at[bottom_idx, "tested_increase"] = False
            memory.at[bottom_idx, "tested_decrease"] = False

            state = optimizer._read_state()
            state["current_stage"] = "hydraulic"
            selected, _, _ = optimizer._candidate_index(memory, state)
            self.assertEqual(memory.at[selected, "parameter"], "bottom_head")
