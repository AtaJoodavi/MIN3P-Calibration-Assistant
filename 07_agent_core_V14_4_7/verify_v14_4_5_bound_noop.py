from __future__ import annotations

import tempfile
from pathlib import Path

import pandas as pd

from modules.adaptive_coordinate_optimizer_V14 import AdaptiveCoordinateOptimizerV14
from modules.calibration_strategy_manager import CalibrationStrategyManager
from modules.parameter_state_manager import ParameterStateManager
from modules.step_size_controller import StepSizeController


class Paths:
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


class Config:
    def __init__(self):
        self.frame = pd.DataFrame([
            {"parameter": "s_biotite", "value": 100.0, "min": 0.01, "max": 100.0, "status": "active"},
        ])

    def parameters(self):
        return self.frame.copy()

    def optimizer_v13(self):
        return {}

    def sheet(self, name: str):
        if name == "v13_parameter_groups":
            return pd.DataFrame([
                {"parameter": "s_biotite", "group": "mineral_kinetics", "enabled": "yes", "priority": 1}
            ])
        raise ValueError(name)


with tempfile.TemporaryDirectory() as tmp:
    paths = Paths(Path(tmp))
    config = Config()
    controller = StepSizeController(paths, config)

    blocked = controller.propose(
        parameter="s_biotite", base_value=100.0, direction="increase",
        lower=0.01, upper=100.0,
    )
    assert blocked["blocked_noop"] is True
    assert blocked["new_value"] == 100.0

    meaningful = controller.propose(
        parameter="s_biotite", base_value=90.0, direction="increase",
        lower=0.01, upper=100.0,
    )
    assert meaningful["new_value"] == 100.0
    assert meaningful["blocked_noop"] is False

    optimizer = AdaptiveCoordinateOptimizerV14(paths, config)
    optimizer.attach_services(
        state_manager=ParameterStateManager(paths, config),
        step_controller=controller,
        strategy_manager=CalibrationStrategyManager(paths, config),
    )
    optimizer.initialize(10.0, "baseline")
    suggestion = optimizer.next_suggestion()
    assert not suggestion.empty
    row = suggestion.iloc[0]
    assert row["parameter"] == "s_biotite"
    assert row["direction"] == "decrease"

    events = pd.read_excel(paths.results_dir / "v14_optimizer_events.xlsx")
    event = events.loc[events["action"].eq("candidate_blocked_by_bound_noop")].iloc[-1]
    assert event["direction"] == "increase"
    assert int(event["physical_run_count_increment"]) == 0

print("PASS: V14.4.5 bound/no-op pre-MIN3P guard")
print("  exact upper-bound increase: blocked without MIN3P")
print("  meaningful move to a bound: preserved")
print("  optimizer immediately selected opposite legal direction")
