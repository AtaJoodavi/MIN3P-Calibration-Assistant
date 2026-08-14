from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from modules.adaptive_coordinate_optimizer_V14 import AdaptiveCoordinateOptimizerV14
from modules.calibration_strategy_manager import CalibrationStrategyManager
from modules.parameter_state_manager import ParameterStateManager
from modules.step_size_controller import StepSizeController


class DummyConfig:
    def __init__(self, optimizer_settings=None, parameters=None):
        self._settings = dict(optimizer_settings or {})
        self.frame = parameters.copy() if parameters is not None else pd.DataFrame()

    def optimizer_v13(self):
        return dict(self._settings)

    def parameters(self):
        return self.frame.copy()

    def sheet(self, name: str):
        if name == "v13_parameter_groups":
            return pd.DataFrame([
                {
                    "parameter": "keff_pyrite",
                    "group": "mineral_kinetics",
                    "enabled": "yes",
                    "priority": 1,
                }
            ])
        raise ValueError(name)


class DummyPaths:
    def __init__(self, root: Path):
        self.results_dir = root / "04_results"
        self.agent_core_dir = root / "07_agent_core_V14_4"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        (self.agent_core_dir / "config").mkdir(parents=True, exist_ok=True)


def build_controller(root: Path, optimizer_settings=None) -> StepSizeController:
    paths = DummyPaths(root)
    # Project defaults are intentionally true; agent_config settings must still
    # be able to override this value.
    (paths.agent_core_dir / "config" / "calibration_rules.yaml").write_text(
        "initial_step_fraction: 0.05\n"
        "minimum_step_fraction: 0.005\n"
        "maximum_step_fraction: 0.20\n"
        "log_range_step_scaling_enabled: true\n",
        encoding="utf-8",
    )
    return StepSizeController(paths, DummyConfig(optimizer_settings=optimizer_settings))


class TestV144RangeNormalizedLogSteps(unittest.TestCase):
    def test_six_decade_range_uses_automatic_coarse_log_range(self):
        with tempfile.TemporaryDirectory() as temp:
            controller = build_controller(Path(temp))
            base = 1.0e-3
            up = controller.propose(
                parameter="keff_pyrite", base_value=base, direction="increase",
                lower=1.0e-6, upper=1.0,
            )
            down = controller.propose(
                parameter="keff_pyrite", base_value=base, direction="decrease",
                lower=1.0e-6, upper=1.0,
            )
            expected = 10.0
            self.assertTrue(up["range_normalized"])
            self.assertEqual(up["step_basis"], "configured_log_range")
            self.assertAlmostEqual(up["configured_log_range_decades"], 6.0, places=12)
            self.assertAlmostEqual(up["factor_requested"], expected, places=12)
            self.assertAlmostEqual(down["factor_requested"], 1.0 / expected, places=12)
            self.assertAlmostEqual(
                up["factor_requested"] * down["factor_requested"], 1.0, places=12
            )

    def test_missing_positive_bounds_falls_back_to_legacy_log_fraction(self):
        with tempfile.TemporaryDirectory() as temp:
            controller = build_controller(Path(temp))
            up = controller.propose(
                parameter="keff_pyrite", base_value=1.0e-3, direction="increase",
                lower=None, upper=None,
            )
            down = controller.propose(
                parameter="keff_pyrite", base_value=1.0e-3, direction="decrease",
                lower=None, upper=None,
            )
            self.assertFalse(up["range_normalized"])
            self.assertEqual(up["step_basis"], "legacy_local_log_fraction")
            self.assertAlmostEqual(up["factor_requested"], 1.05, places=12)
            self.assertAlmostEqual(down["factor_requested"], 1.0 / 1.05, places=12)

    def test_zero_lower_bound_falls_back_instead_of_using_semantic_floor_as_range(self):
        with tempfile.TemporaryDirectory() as temp:
            controller = build_controller(Path(temp))
            move = controller.propose(
                parameter="keff_pyrite", base_value=1.0e-3, direction="increase",
                lower=0.0, upper=1.0,
            )
            self.assertFalse(move["range_normalized"])
            self.assertAlmostEqual(move["factor_requested"], 1.05, places=12)

    def test_agent_config_can_disable_range_scaling_despite_yaml_default(self):
        with tempfile.TemporaryDirectory() as temp:
            controller = build_controller(
                Path(temp), optimizer_settings={"log_range_step_scaling_enabled": False}
            )
            move = controller.propose(
                parameter="keff_pyrite", base_value=1.0e-3, direction="increase",
                lower=1.0e-6, upper=1.0,
            )
            self.assertFalse(move["range_normalized"])
            self.assertEqual(move["step_basis"], "legacy_local_log_fraction")
            self.assertAlmostEqual(move["factor_requested"], 1.05, places=12)

    def test_range_scaled_candidate_is_clamped_to_configured_bound(self):
        with tempfile.TemporaryDirectory() as temp:
            controller = build_controller(Path(temp))
            move = controller.propose(
                parameter="keff_pyrite", base_value=0.9, direction="increase",
                lower=1.0e-6, upper=1.0,
            )
            self.assertEqual(move["new_value"], 1.0)
            self.assertTrue(move["at_bound"])
            self.assertEqual(move["bound_reason"], "configured_upper_bound_reached")

    def test_bounded_linear_parameters_keep_fraction_of_linear_span(self):
        with tempfile.TemporaryDirectory() as temp:
            controller = build_controller(Path(temp))
            move = controller.propose(
                parameter="bottom_head", base_value=-0.3, direction="increase",
                lower=-2.0, upper=2.0,
            )
            self.assertEqual(move["move_space"], "bounded_linear")
            self.assertTrue(move["range_normalized"])
            self.assertEqual(move["step_basis"], "configured_linear_range")
            self.assertAlmostEqual(move["new_value"], -0.1, places=12)


class TestV144OptimizerAuditFields(unittest.TestCase):
    def test_single_candidate_records_range_scaling_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paths = DummyPaths(root)
            (paths.agent_core_dir / "config" / "calibration_rules.yaml").write_text(
                "initial_step_fraction: 0.05\n"
                "minimum_step_fraction: 0.005\n"
                "maximum_step_fraction: 0.20\n"
                "log_range_step_scaling_enabled: true\n",
                encoding="utf-8",
            )
            config = DummyConfig(parameters=pd.DataFrame([
                {
                    "parameter": "keff_pyrite",
                    "value": 1.0e-3,
                    "min": 1.0e-6,
                    "max": 1.0,
                    "status": "active",
                }
            ]))
            optimizer = AdaptiveCoordinateOptimizerV14(paths, config)
            optimizer.attach_services(
                state_manager=ParameterStateManager(paths, config),
                step_controller=StepSizeController(paths, config),
                strategy_manager=CalibrationStrategyManager(paths, config),
            )
            optimizer.initialize(10.0, "baseline")
            suggestion = optimizer.next_suggestion()
            self.assertEqual(len(suggestion), 1)
            row = suggestion.iloc[0]
            self.assertEqual(row["optimizer_version"], "V14.4.7")
            self.assertTrue(bool(row["range_normalized"]))
            self.assertEqual(row["step_basis"], "configured_log_range")
            self.assertAlmostEqual(float(row["configured_log_range_decades"]), 6.0, places=12)
            self.assertAlmostEqual(float(row["new_value"]), 1.0e-2, places=12)


if __name__ == "__main__":
    unittest.main()
