from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from modules.adaptive_coordinate_optimizer_V14 import AdaptiveCoordinateOptimizerV14
from modules.calibration_strategy_manager import CalibrationStrategyManager
from modules.parameter_state_manager import ParameterStateManager
from modules.step_size_controller import StepSizeController


class DummyPaths:
    def __init__(self, root: Path):
        self.results_dir = root / "04_results"
        self.agent_core_dir = root / "07_agent_core_V14_4_4"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        (self.agent_core_dir / "config").mkdir(parents=True, exist_ok=True)
        (self.agent_core_dir / "config" / "calibration_rules.yaml").write_text(
            "initial_step_fraction: 0.05\n"
            "minimum_step_fraction: 0.005\n"
            "maximum_step_fraction: 0.20\n"
            "log_range_step_scaling_enabled: true\n"
            "automatic_initial_scales_enabled: true\n"
            "automatic_initial_max_factor: 10\n"
            "automatic_reference_log_decades: 6\n",
            encoding="utf-8",
        )


class DummyConfig:
    def __init__(self, frame: pd.DataFrame, optimizer_settings=None):
        self.frame = frame.copy()
        self.settings = dict(optimizer_settings or {})

    def parameters(self):
        return self.frame.copy()

    def optimizer_v13(self):
        return dict(self.settings)

    def sheet(self, name: str):
        if name == "v13_parameter_groups":
            rows = []
            for i, parameter in enumerate(self.frame["parameter"].astype(str), start=1):
                group = "hydraulic" if parameter in {"Kz", "bottom_head"} else "mineral_kinetics"
                rows.append({"parameter": parameter, "group": group, "enabled": "yes", "priority": i})
            return pd.DataFrame(rows)
        raise ValueError(name)


def build_controller(root: Path, frame: pd.DataFrame, optimizer_settings=None):
    paths = DummyPaths(root)
    config = DummyConfig(frame, optimizer_settings)
    return paths, config, StepSizeController(paths, config)


def build_optimizer(root: Path, frame: pd.DataFrame, optimizer_settings=None):
    paths, config, controller = build_controller(root, frame, optimizer_settings)
    optimizer = AdaptiveCoordinateOptimizerV14(paths, config)
    optimizer.attach_services(
        state_manager=ParameterStateManager(paths, config),
        step_controller=controller,
        strategy_manager=CalibrationStrategyManager(paths, config),
    )
    optimizer.initialize(10.0, "baseline")
    return optimizer, controller


class TestV1444AutomaticInitialScales(unittest.TestCase):
    def test_six_decade_log_range_automatically_starts_at_factor_10(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = pd.DataFrame([{
                "parameter": "keff_pyrite", "value": 4.62e-3,
                "min": 1e-6, "max": 1.0, "status": "active",
            }])
            _, _, controller = build_controller(Path(temp), frame)
            record = controller.ensure_parameter(
                "keff_pyrite", 4.62e-3, lower=1e-6, upper=1.0, group="mineral_kinetics"
            )
            self.assertEqual(record["initial_search_mode"], "automatic_log_range")
            self.assertEqual(record["initial_search_source"], "automatic_positive_log_range")
            self.assertAlmostEqual(record["initial_step_fraction_resolved"], 1.0 / 6.0, places=12)
            self.assertAlmostEqual(record["initial_factor_effective"], 10.0, places=12)

            up = controller.propose(
                parameter="keff_pyrite", base_value=4.62e-3,
                direction="increase", lower=1e-6, upper=1.0,
            )
            down = controller.propose(
                parameter="keff_pyrite", base_value=4.62e-3,
                direction="decrease", lower=1e-6, upper=1.0,
            )
            self.assertAlmostEqual(up["factor_requested"], 10.0, places=12)
            self.assertAlmostEqual(down["factor_requested"], 0.1, places=12)

    def test_very_wide_log_range_is_capped_at_factor_10(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = pd.DataFrame([{
                "parameter": "keff_sphalerite", "value": 1e-6,
                "min": 1e-10, "max": 1.0, "status": "active",
            }])
            _, _, controller = build_controller(Path(temp), frame)
            record = controller.ensure_parameter(
                "keff_sphalerite", 1e-6, lower=1e-10, upper=1.0, group="mineral_kinetics"
            )
            self.assertAlmostEqual(record["initial_step_fraction_resolved"], 0.1, places=12)
            self.assertAlmostEqual(record["initial_factor_effective"], 10.0, places=12)

    def test_narrow_kz_range_gets_small_automatic_factor(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = pd.DataFrame([{
                "parameter": "Kz", "value": 1.14e-4,
                "min": 9.12e-5, "max": 1.368e-4, "status": "active",
            }])
            _, _, controller = build_controller(Path(temp), frame)
            record = controller.ensure_parameter(
                "Kz", 1.14e-4, lower=9.12e-5, upper=1.368e-4, group="hydraulic"
            )
            self.assertEqual(record["initial_search_mode"], "automatic_log_range")
            self.assertAlmostEqual(record["initial_factor_effective"], 1.0218978815657618, places=12)
            self.assertLess(record["initial_step_fraction_resolved"], 0.06)

    def test_zero_lower_bound_for_log_parameter_uses_safe_default(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = pd.DataFrame([{
                "parameter": "keff_test", "value": 0.1,
                "min": 0.0, "max": 1.0, "status": "active",
            }])
            _, _, controller = build_controller(Path(temp), frame)
            record = controller.ensure_parameter(
                "keff_test", 0.1, lower=0.0, upper=1.0, group="mineral_kinetics"
            )
            self.assertEqual(record["initial_search_mode"], "automatic_fallback")
            self.assertAlmostEqual(record["current_step_fraction"], 0.05, places=12)
            self.assertIn("global_fallback", record["initial_search_note"])
            move = controller.propose(
                parameter="keff_test", base_value=0.1,
                direction="increase", lower=0.0, upper=1.0,
            )
            self.assertEqual(move["step_basis"], "legacy_local_log_fraction")
            self.assertAlmostEqual(move["factor_requested"], 1.05, places=12)

    def test_bottom_head_automatically_uses_five_percent_of_linear_span(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = pd.DataFrame([{
                "parameter": "bottom_head", "value": -0.25,
                "min": -0.3, "max": 0.0, "status": "active",
            }])
            _, _, controller = build_controller(Path(temp), frame)
            record = controller.ensure_parameter(
                "bottom_head", -0.25, lower=-0.3, upper=0.0, group="hydraulic"
            )
            self.assertEqual(record["initial_search_mode"], "automatic_linear_range")
            self.assertAlmostEqual(record["current_step_fraction"], 0.05, places=12)
            move = controller.propose(
                parameter="bottom_head", base_value=-0.25,
                direction="increase", lower=-0.3, upper=0.0,
            )
            self.assertAlmostEqual(move["new_value"], -0.235, places=12)
            self.assertEqual(move["step_basis"], "configured_linear_range")

    def test_relative_parameter_uses_five_percent_of_configured_span(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = pd.DataFrame([{
                "parameter": "bc_top_pH", "value": 5.6,
                "min": 4.48, "max": 6.72, "status": "active",
            }])
            _, _, controller = build_controller(Path(temp), frame)
            record = controller.ensure_parameter(
                "bc_top_pH", 5.6, lower=4.48, upper=6.72, group="boundary_chemistry"
            )
            self.assertEqual(record["initial_search_mode"], "automatic_relative_range")
            self.assertAlmostEqual(record["current_step_fraction"], 0.02, places=12)
            move = controller.propose(
                parameter="bc_top_pH", base_value=5.6,
                direction="increase", lower=4.48, upper=6.72,
            )
            self.assertAlmostEqual(move["new_value"], 5.712, places=12)

    def test_missing_bounds_use_safe_default(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = pd.DataFrame([{
                "parameter": "keff_test", "value": 1e-3,
                "min": None, "max": None, "status": "active",
            }])
            _, _, controller = build_controller(Path(temp), frame)
            record = controller.ensure_parameter("keff_test", 1e-3, lower=None, upper=None)
            self.assertEqual(record["initial_search_mode"], "automatic_fallback")
            self.assertAlmostEqual(record["current_step_fraction"], 0.05, places=12)

    def test_manual_v14_4_3_columns_are_ignored(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = pd.DataFrame([{
                "parameter": "keff_pyrite", "value": 1e-3,
                "min": 1e-6, "max": 1.0, "status": "active",
                "initial_search_mode": "global", "initial_factor": 2.0,
                "initial_range_fraction": 0.01,
            }])
            _, _, controller = build_controller(Path(temp), frame)
            record = controller.ensure_parameter(
                "keff_pyrite", 1e-3, lower=1e-6, upper=1.0, group="mineral_kinetics"
            )
            self.assertEqual(record["initial_search_source"], "automatic_positive_log_range")
            self.assertAlmostEqual(record["initial_factor_effective"], 10.0, places=12)

    def test_pristine_v14_4_3_record_migrates_but_explored_record_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = pd.DataFrame([{
                "parameter": "keff_pyrite", "value": 1e-3,
                "min": 1e-6, "max": 1.0, "status": "active",
            }])
            _, _, controller = build_controller(Path(temp), frame)
            state = {
                "parameters": {
                    "keff_pyrite": {
                        "parameter": "keff_pyrite", "move_space": "logarithmic",
                        "requires_strictly_positive": True,
                        "initial_step_fraction": 0.05, "minimum_step_fraction": 0.005,
                        "maximum_step_fraction": 0.20, "current_step_fraction": 0.05,
                        "accepted_streak": 0, "rejected_streak": 0, "oscillation_count": 0,
                        "last_direction": "", "last_outcome": "", "pending_shrink_after_pair": False,
                    }
                }, "metadata": {"optimizer_version": "V14.4.3"},
            }
            controller._save(state)
            migrated = controller.sync_initial_scale_if_pristine(
                "keff_pyrite", value=1e-3, lower=1e-6, upper=1.0, group="mineral_kinetics"
            )
            self.assertAlmostEqual(migrated["current_step_fraction"], 1.0 / 6.0, places=12)
            self.assertTrue(migrated["initial_scale_applied_v14_4_4"])

            state = controller._load()
            rec = state["parameters"]["keff_pyrite"]
            rec["current_step_fraction"] = 0.0125
            rec["last_direction"] = "increase"
            rec["last_outcome"] = "accepted"
            state["parameters"]["keff_pyrite"] = rec
            controller._save(state)
            preserved = controller.sync_initial_scale_if_pristine(
                "keff_pyrite", value=1e-3, lower=1e-6, upper=1.0, group="mineral_kinetics"
            )
            self.assertAlmostEqual(preserved["current_step_fraction"], 0.0125, places=12)

    def test_optimizer_suggestion_exposes_automatic_audit_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            frame = pd.DataFrame([{
                "parameter": "keff_pyrite", "value": 1e-3,
                "min": 1e-6, "max": 1.0, "status": "active",
            }])
            optimizer, _ = build_optimizer(Path(temp), frame)
            suggestion = optimizer.next_suggestion()
            self.assertEqual(len(suggestion), 1)
            row = suggestion.iloc[0]
            self.assertEqual(row["optimizer_version"], "V14.4.7")
            self.assertEqual(row["initial_search_mode"], "automatic_log_range")
            self.assertEqual(row["initial_search_source"], "automatic_positive_log_range")
            self.assertAlmostEqual(float(row["step_fraction_applied"]), 1.0 / 6.0, places=12)
            self.assertAlmostEqual(float(row["factor_requested"]), 10.0, places=12)


if __name__ == "__main__":
    unittest.main()
