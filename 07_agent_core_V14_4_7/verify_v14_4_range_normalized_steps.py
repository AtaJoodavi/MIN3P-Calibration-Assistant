from __future__ import annotations

"""Deterministic smoke test for V14.4 range-normalized stepping."""

import math
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from modules.step_size_controller import StepSizeController


class Config:
    def optimizer_v13(self):
        return {"log_range_step_scaling_enabled": True, "automatic_initial_scales_enabled": False}


with TemporaryDirectory() as temp:
    root = Path(temp)
    (root / "config").mkdir()
    (root / "results").mkdir()
    paths = SimpleNamespace(results_dir=root / "results", agent_core_dir=root)
    controller = StepSizeController(paths, Config())

    base = 1.0e-3
    lower = 1.0e-6
    upper = 1.0
    up = controller.propose(
        parameter="keff_pyrite", base_value=base, direction="increase",
        lower=lower, upper=upper,
    )
    down = controller.propose(
        parameter="keff_pyrite", base_value=base, direction="decrease",
        lower=lower, upper=upper,
    )

    expected_factor = 10.0 ** (0.05 * 6.0)
    assert up["step_basis"] == "configured_log_range", up
    assert down["step_basis"] == "configured_log_range", down
    assert math.isclose(up["factor_requested"], expected_factor, rel_tol=1e-12), up
    assert math.isclose(down["factor_requested"], 1.0 / expected_factor, rel_tol=1e-12), down
    assert math.isclose(up["factor_requested"] * down["factor_requested"], 1.0, rel_tol=1e-12)

    legacy = controller.propose(
        parameter="keff_pyrite", base_value=base, direction="increase",
        lower=None, upper=None,
    )
    assert legacy["step_basis"] == "legacy_local_log_fraction", legacy
    assert math.isclose(legacy["factor_requested"], 1.05, rel_tol=1e-12), legacy

    print("PASS: V14.4 range-normalized logarithmic stepping")
    print(f"  configured range: [{lower:g}, {upper:g}] = 6 decades")
    print(f"  5% increase factor: {up['factor_requested']:.9f}")
    print(f"  5% decrease factor: {down['factor_requested']:.9f}")
    print(f"  no-bounds fallback factor: {legacy['factor_requested']:.9f}")
