from __future__ import annotations

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
        self.agent_core_dir = root / "07_agent_core_V14"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.agent_core_dir.mkdir(parents=True, exist_ok=True)


class DummyConfig:
    def __init__(self):
        self.frame = pd.DataFrame([
            {"parameter": "Kz", "value": 1.0, "min": 0.1, "max": 10.0, "status": "active"},
            {"parameter": "bottom_head", "value": 0.0, "min": -2.0, "max": 2.0, "status": "active"},
        ])

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


def build_optimizer(root: Path):
    paths = DummyPaths(root)
    config = DummyConfig()
    optimizer = AdaptiveCoordinateOptimizerV14(paths, config)
    optimizer.attach_services(
        state_manager=ParameterStateManager(paths, config),
        step_controller=StepSizeController(paths, config),
        strategy_manager=CalibrationStrategyManager(paths, config),
    )
    optimizer.initialize(10.0, "baseline")
    return optimizer


class TestV1442PriorityRefinement(unittest.TestCase):
    def test_priority_refinement_precedes_larger_unexplored_peer(self):
        with tempfile.TemporaryDirectory() as temp:
            optimizer = build_optimizer(Path(temp))
            memory = optimizer._ensure_memory()
            kz = memory.index[memory["parameter"].eq("Kz")][0]
            bottom = memory.index[memory["parameter"].eq("bottom_head")][0]

            memory.at[kz, "status"] = "refine_symmetric"
            memory.at[kz, "phase"] = "symmetric"
            memory.at[kz, "step_fraction"] = 0.025
            memory.at[kz, "refinement_round"] = 1
            memory.at[kz, "priority_refinement_rounds_remaining"] = 1
            memory.at[kz, "direction_queue"] = "increase|decrease"

            memory.at[bottom, "status"] = "unexplored"
            memory.at[bottom, "step_fraction"] = 0.05
            memory.at[bottom, "direction_queue"] = "increase|decrease"

            state = optimizer._read_state()
            state["current_stage"] = "hydraulic"
            selected, _, _ = optimizer._candidate_index(memory, state)
            self.assertEqual(memory.at[selected, "parameter"], "Kz")

    def test_ordinary_refinement_still_yields_to_larger_unexplored_peer(self):
        with tempfile.TemporaryDirectory() as temp:
            optimizer = build_optimizer(Path(temp))
            memory = optimizer._ensure_memory()
            kz = memory.index[memory["parameter"].eq("Kz")][0]
            bottom = memory.index[memory["parameter"].eq("bottom_head")][0]

            memory.at[kz, "status"] = "refine_symmetric"
            memory.at[kz, "phase"] = "symmetric"
            memory.at[kz, "step_fraction"] = 0.025
            memory.at[kz, "refinement_round"] = 1
            memory.at[kz, "priority_refinement_rounds_remaining"] = 0
            memory.at[kz, "direction_queue"] = "increase|decrease"

            memory.at[bottom, "status"] = "unexplored"
            memory.at[bottom, "step_fraction"] = 0.05
            memory.at[bottom, "direction_queue"] = "increase|decrease"

            state = optimizer._read_state()
            state["current_stage"] = "hydraulic"
            selected, _, _ = optimizer._candidate_index(memory, state)
            self.assertEqual(memory.at[selected, "parameter"], "bottom_head")

    def test_unfinished_direction_family_still_beats_priority_refinement(self):
        # This is the migration-safe rule for the user's current HCT3 state:
        # bottom_head has an untested opposite direction, so it must finish
        # before the migrated Kz 2.5% protected refinement begins.
        with tempfile.TemporaryDirectory() as temp:
            optimizer = build_optimizer(Path(temp))
            memory = optimizer._ensure_memory()
            kz = memory.index[memory["parameter"].eq("Kz")][0]
            bottom = memory.index[memory["parameter"].eq("bottom_head")][0]

            memory.at[kz, "status"] = "refine_symmetric"
            memory.at[kz, "phase"] = "symmetric"
            memory.at[kz, "step_fraction"] = 0.025
            memory.at[kz, "refinement_round"] = 1
            memory.at[kz, "priority_refinement_rounds_remaining"] = 1
            memory.at[kz, "direction_queue"] = "increase|decrease"

            memory.at[bottom, "status"] = "test_remaining_direction"
            memory.at[bottom, "phase"] = "symmetric"
            memory.at[bottom, "step_fraction"] = 0.05
            memory.at[bottom, "direction_queue"] = "decrease"

            state = optimizer._read_state()
            state["current_stage"] = "hydraulic"
            selected, _, _ = optimizer._candidate_index(memory, state)
            self.assertEqual(memory.at[selected, "parameter"], "bottom_head")

    def test_v14_4_1_refinement_state_migrates_to_one_priority_round(self):
        with tempfile.TemporaryDirectory() as temp:
            optimizer = build_optimizer(Path(temp))
            memory = optimizer._ensure_memory()
            kz = memory.index[memory["parameter"].eq("Kz")][0]
            memory.at[kz, "status"] = "refine_symmetric"
            memory.at[kz, "phase"] = "symmetric"
            memory.at[kz, "refinement_round"] = 1
            memory.at[kz, "accepted_improvement_count"] = 1
            memory.at[kz, "step_fraction"] = 0.025
            # Emulate the V14.4.1 workbook schema by dropping the new column.
            memory = memory.drop(columns=["priority_refinement_rounds_remaining"])
            optimizer._write_memory(memory)

            migrated = optimizer._ensure_memory()
            row = migrated.loc[migrated["parameter"].eq("Kz")].iloc[0]
            self.assertEqual(int(row["priority_refinement_rounds_remaining"]), 1)

    def test_directional_bracket_grants_one_priority_round_and_symmetric_pair_consumes_it(self):
        with tempfile.TemporaryDirectory() as temp:
            optimizer = build_optimizer(Path(temp))
            memory = optimizer._ensure_memory()
            kz = memory.index[memory["parameter"].eq("Kz")][0]
            state = optimizer._read_state()

            # A directional bracket follows an accepted improvement.
            memory.at[kz, "phase"] = "directional_opposite"
            memory.at[kz, "step_fraction"] = 0.05
            action = optimizer._finish_or_refine(memory, int(kz), state, reason="directional_pair_failed")
            self.assertEqual(action, "shrink_step_after_completed_direction_family")
            self.assertEqual(int(memory.at[kz, "priority_refinement_rounds_remaining"]), 1)

            # Completing that protected reduced-step pair consumes the grant.
            memory.at[kz, "phase"] = "symmetric"
            action = optimizer._finish_or_refine(memory, int(kz), state, reason="symmetric_pair_failed")
            self.assertEqual(action, "shrink_step_after_completed_direction_family")
            self.assertEqual(int(memory.at[kz, "priority_refinement_rounds_remaining"]), 0)

    def test_state_metadata_is_current_release_not_stale_loaded_values(self):
        with tempfile.TemporaryDirectory() as temp:
            optimizer = build_optimizer(Path(temp))
            state = optimizer._read_state()
            state["updated_at"] = "2000-01-01T00:00:00"
            state["optimizer_version"] = "V14.4"
            optimizer._write_state(state)
            stored = pd.read_excel(optimizer.state_file).iloc[0]
            self.assertEqual(stored["optimizer_version"], "V14.4.7")
            self.assertNotEqual(stored["updated_at"], "2000-01-01T00:00:00")


if __name__ == "__main__":
    unittest.main()
