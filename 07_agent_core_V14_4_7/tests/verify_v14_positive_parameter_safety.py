from __future__ import annotations

"""Quick deterministic verification for V14.1 positive-parameter moves."""

from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from modules.step_size_controller import StepSizeController


class Config:
    def optimizer_v13(self):
        return {}


with TemporaryDirectory() as temp:
    root = Path(temp)
    (root / "config").mkdir()
    paths = SimpleNamespace(results_dir=root / "results", agent_core_dir=root)
    paths.results_dir.mkdir()
    controller = StepSizeController(paths, Config())

    cases = [
        ("phi_magnesite", 0.00531),
        ("keff_pyrite", 1.0e-6),
        ("feoh_s_mass", 0.2),
        ("feoh_w_density", 2.3),
    ]
    for parameter, base in cases:
        up = controller.propose(parameter=parameter, base_value=base, direction="increase", lower=None, upper=None)
        down = controller.propose(parameter=parameter, base_value=base, direction="decrease", lower=None, upper=None)
        assert up["move_space"] == "logarithmic", up
        assert down["move_space"] == "logarithmic", down
        assert up["new_value"] > base > down["new_value"] > 0, (parameter, up, down)
        print(f"{parameter}: {down['new_value']:.12g} < {base:.12g} < {up['new_value']:.12g}")

print("V14.1 positive-parameter safety verification OK")
