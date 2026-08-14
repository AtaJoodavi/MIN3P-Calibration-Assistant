from __future__ import annotations

"""Deterministic smoke test for V14.4.4 automatic range-adaptive initial scales."""

import math
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pandas as pd

from modules.step_size_controller import StepSizeController


class Config:
    def __init__(self):
        self._params = pd.DataFrame([
            {"parameter": "keff_pyrite", "value": 4.62e-3, "min": 1e-6, "max": 1.0, "status": "active"},
            {"parameter": "Kz", "value": 1.14e-4, "min": 9.12e-5, "max": 1.368e-4, "status": "active"},
            {"parameter": "bottom_head", "value": -0.25, "min": -0.3, "max": 0.0, "status": "active"},
            {"parameter": "keff_zero_bound", "value": 0.1, "min": 0.0, "max": 1.0, "status": "active"},
        ])

    def optimizer_v13(self):
        return {
            "log_range_step_scaling_enabled": True,
            "automatic_initial_scales_enabled": True,
        }

    def parameters(self):
        return self._params.copy()


with TemporaryDirectory() as temp:
    root = Path(temp)
    (root / "config").mkdir()
    (root / "results").mkdir()
    paths = SimpleNamespace(results_dir=root / "results", agent_core_dir=root)
    controller = StepSizeController(paths, Config())

    pyrite = controller.ensure_parameter("keff_pyrite", 4.62e-3, lower=1e-6, upper=1.0)
    pyrite_up = controller.propose(parameter="keff_pyrite", base_value=4.62e-3, direction="increase", lower=1e-6, upper=1.0)
    pyrite_down = controller.propose(parameter="keff_pyrite", base_value=4.62e-3, direction="decrease", lower=1e-6, upper=1.0)
    assert pyrite["initial_search_source"] == "automatic_positive_log_range", pyrite
    assert math.isclose(pyrite["initial_factor_effective"], 10.0, rel_tol=1e-12), pyrite
    assert math.isclose(pyrite_up["factor_requested"], 10.0, rel_tol=1e-12), pyrite_up
    assert math.isclose(pyrite_down["factor_requested"], 0.1, rel_tol=1e-12), pyrite_down

    kz = controller.ensure_parameter("Kz", 1.14e-4, lower=9.12e-5, upper=1.368e-4)
    assert 1.0 < kz["initial_factor_effective"] < 1.05, kz

    head = controller.ensure_parameter("bottom_head", -0.25, lower=-0.3, upper=0.0)
    head_up = controller.propose(parameter="bottom_head", base_value=-0.25, direction="increase", lower=-0.3, upper=0.0)
    assert head["initial_search_source"] == "automatic_bounded_linear_range", head
    assert math.isclose(head_up["new_value"], -0.235, rel_tol=1e-12), head_up

    zero = controller.ensure_parameter("keff_zero_bound", 0.1, lower=0.0, upper=1.0)
    zero_up = controller.propose(parameter="keff_zero_bound", base_value=0.1, direction="increase", lower=0.0, upper=1.0)
    assert zero["initial_search_mode"] == "automatic_fallback", zero
    assert math.isclose(zero_up["factor_requested"], 1.05, rel_tol=1e-12), zero_up

    print("PASS: V14.4.4 automatic range-adaptive initial search")
    print("  keff_pyrite [1e-6, 1]: automatic x10 / divide-by-10 first probe")
    print(f"  Kz narrow range: automatic factor {kz['initial_factor_effective']:.9f}")
    print("  bottom_head [-0.3, 0]: automatic 5% bounded-linear range move")
    print("  zero lower bound: safe global fallback factor 1.05")
