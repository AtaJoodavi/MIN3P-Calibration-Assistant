from __future__ import annotations

"""Preview V14.4.4 automatic initial search scales without changing calibration state."""

import argparse
from pathlib import Path

import pandas as pd

from modules.config import ProjectPaths
from modules.config_reader import ConfigReader
from modules.step_size_controller import StepSizeController
from modules.v13_parameter_groups import assign_groups
from modules.v14_utils import as_float


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--xlsx", action="store_true", help="also write 04_results/v14_4_4_initial_scale_preview.xlsx")
    parser.add_argument("--active-only", action="store_true", help="show only parameters with status=active")
    args = parser.parse_args()

    core = Path(__file__).resolve().parent
    paths = ProjectPaths.from_agent_core(core)
    config = ConfigReader(paths)
    params = assign_groups(config.parameters().copy(), config)
    controller = StepSizeController(paths, config)

    rows = []
    for _, p in params.iterrows():
        parameter = str(p.get("parameter", "")).strip()
        if not parameter:
            continue
        status = str(p.get("status", "active")).strip().casefold()
        if args.active_only and status != "active":
            continue
        value = as_float(p.get("value"))
        lower = as_float(p.get("min"))
        upper = as_float(p.get("max"))
        group = str(p.get("v13_group", ""))
        move_space = controller.infer_move_space(parameter, value)
        spec = controller._resolve_initial_search(
            parameter=parameter,
            value=value,
            lower=lower,
            upper=upper,
            group=group,
            move_space=move_space,
        )
        rows.append({
            "parameter": parameter,
            "status": status,
            "group": group,
            "value": value,
            "min": lower,
            "max": upper,
            "move_space": move_space,
            **spec,
        })

    out = pd.DataFrame(rows)
    columns = [
        "parameter", "status", "group", "value", "min", "max", "move_space",
        "initial_search_mode", "initial_search_source",
        "initial_configured_log_range_decades",
        "initial_step_fraction_raw", "initial_step_fraction_resolved",
        "initial_step_fraction_clamped", "initial_factor_effective",
        "automatic_initial_factor_target", "automatic_initial_max_factor",
        "automatic_default_step_fraction", "minimum_step_fraction",
        "maximum_step_fraction", "initial_search_note",
    ]
    columns = [c for c in columns if c in out.columns]
    print(out[columns].to_string(index=False))
    if args.xlsx:
        target = paths.results_dir / "v14_4_4_initial_scale_preview.xlsx"
        target.parent.mkdir(parents=True, exist_ok=True)
        out[columns].to_excel(target, index=False)
        print(f"\nWrote: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
