from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pandas as pd

from modules.adaptive_coordinate_optimizer_V14 import AdaptiveCoordinateOptimizerV14
from modules.calibration_strategy_manager import CalibrationStrategyManager
from modules.parameter_state_manager import ParameterStateManager
from modules.parameter_pair_optimizer import ParameterPairOptimizer
from modules.step_size_controller import StepSizeController


class DummyPaths:
    def __init__(self, root: Path):
        self.results_dir = root / "04_results"
        self.agent_core_dir = root / "07_agent_core_V14_4_5"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        (self.agent_core_dir / "config").mkdir(parents=True, exist_ok=True)
        (self.agent_core_dir / "config" / "calibration_rules.yaml").write_text(
            "initial_step_fraction: 0.05\n"
            "minimum_step_fraction: 0.005\n"
            "maximum_step_fraction: 0.20\n"
            "log_range_step_scaling_enabled: true\n"
            "automatic_initial_scales_enabled: true\n"
            "automatic_initial_max_factor: 10\n"
            "automatic_reference_log_decades: 6\n"
            "candidate_noop_absolute_tolerance: 1e-30\n"
            "candidate_noop_relative_tolerance: 1e-10\n"
            "candidate_noop_range_tolerance: 1e-12\n",
            encoding="utf-8",
        )


class DummyConfig:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame.copy()

    def parameters(self):
        return self.frame.copy()

    def optimizer_v13(self):
        return {}

    def sheet(self, name: str):
        if name == "v13_parameter_groups":
            return pd.DataFrame([
                {"parameter": p, "group": "mineral_kinetics", "enabled": "yes", "priority": i + 1}
                for i, p in enumerate(self.frame["parameter"].astype(str))
            ])
        raise ValueError(name)


def build(root: Path, frame: pd.DataFrame):
    paths = DummyPaths(root)
    config = DummyConfig(frame)
    controller = StepSizeController(paths, config)
    optimizer = AdaptiveCoordinateOptimizerV14(paths, config)
    optimizer.attach_services(
        state_manager=ParameterStateManager(paths, config),
        step_controller=controller,
        strategy_manager=CalibrationStrategyManager(paths, config),
    )
    optimizer.initialize(10.0, "baseline")
    return paths, config, controller, optimizer


class TestV1445BoundNoopGuard(unittest.TestCase):
    def test_exact_upper_bound_increase_is_blocked_noop(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = pd.DataFrame([{
                "parameter": "s_biotite", "value": 100.0,
                "min": 0.01, "max": 100.0, "status": "active",
            }])
            paths = DummyPaths(Path(temp))
            config = DummyConfig(frame)
            controller = StepSizeController(paths, config)
            move = controller.propose(
                parameter="s_biotite", base_value=100.0, direction="increase",
                lower=0.01, upper=100.0,
            )
            self.assertTrue(move["blocked_noop"])
            self.assertEqual(move["new_value"], 100.0)
            self.assertEqual(move["factor_applied"], 1.0)
            self.assertGreaterEqual(move["noop_tolerance"], 0.0)

    def test_near_bound_sub_tolerance_residual_is_blocked(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = pd.DataFrame([{
                "parameter": "Kz", "value": 0.9999999999995,
                "min": 0.5, "max": 1.0, "status": "active",
            }])
            paths = DummyPaths(Path(temp))
            config = DummyConfig(frame)
            controller = StepSizeController(paths, config)
            move = controller.propose(
                parameter="Kz", base_value=0.9999999999995, direction="increase",
                lower=0.5, upper=1.0,
            )
            self.assertEqual(move["new_value"], 1.0)
            self.assertTrue(move["blocked_noop"])
            self.assertLessEqual(move["noop_delta"], move["noop_tolerance"])

    def test_meaningful_bound_clamped_move_is_not_noop(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = pd.DataFrame([{
                "parameter": "keff_pyrite", "value": 0.9,
                "min": 1e-6, "max": 1.0, "status": "active",
            }])
            paths = DummyPaths(Path(temp))
            config = DummyConfig(frame)
            controller = StepSizeController(paths, config)
            move = controller.propose(
                parameter="keff_pyrite", base_value=0.9, direction="increase",
                lower=1e-6, upper=1.0,
            )
            self.assertEqual(move["new_value"], 1.0)
            self.assertTrue(move["at_bound"])
            self.assertFalse(move["blocked_noop"])
            self.assertGreater(move["noop_delta"], move["noop_tolerance"])

    def test_optimizer_skips_blocked_increase_and_returns_decrease_without_run(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = pd.DataFrame([{
                "parameter": "s_biotite", "value": 100.0,
                "min": 0.01, "max": 100.0, "status": "active",
            }])
            paths, _, _, optimizer = build(Path(temp), frame)
            suggestion = optimizer.next_suggestion()
            self.assertFalse(suggestion.empty)
            row = suggestion.iloc[0]
            self.assertEqual(row["parameter"], "s_biotite")
            self.assertEqual(row["direction"], "decrease")
            self.assertLess(float(row["new_value"]), 100.0)

            events = pd.read_excel(paths.results_dir / "v14_optimizer_events.xlsx")
            blocked = events[events["action"].astype(str).eq("candidate_blocked_by_bound_noop")]
            self.assertEqual(len(blocked), 1)
            event = blocked.iloc[0]
            self.assertEqual(event["parameter"], "s_biotite")
            self.assertEqual(event["direction"], "increase")
            self.assertEqual(bool(event["min3p_run_started"]), False)
            self.assertEqual(int(event["physical_run_count_increment"]), 0)

    def test_interaction_pair_is_suppressed_when_one_move_is_noop(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = pd.DataFrame([
                {"parameter": "s_biotite", "value": 100.0, "min": 0.01, "max": 100.0, "status": "active"},
                {"parameter": "keff_pyrite", "value": 1e-3, "min": 1e-6, "max": 1.0, "status": "active"},
            ])
            paths = DummyPaths(Path(temp))
            config = DummyConfig(frame)
            controller = StepSizeController(paths, config)
            state_manager = ParameterStateManager(paths, config)
            pair_optimizer = ParameterPairOptimizer(paths, config)
            definition = pd.Series({
                "pair_id": "test_pair",
                "parameter_a": "s_biotite",
                "parameter_b": "keff_pyrite",
                "group": "interaction",
                "priority": 1,
                "scientific_rationale": "test",
            })
            pair_state = {"phase": "symmetric"}
            candidate = pair_optimizer._build_candidate(
                definition, pair_state, frame, controller, state_manager,
                ("increase", "increase"), valid_run_count=0, iteration=1,
            )
            self.assertIsNone(candidate)
            self.assertEqual(pair_state["last_blocked_parameter"], "s_biotite")
            self.assertEqual(pair_state["last_blocked_direction"], "increase")
            self.assertIn("bound", pair_state["last_blocked_reason"])


if __name__ == "__main__":
    unittest.main()
