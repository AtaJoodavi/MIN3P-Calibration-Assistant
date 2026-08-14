from __future__ import annotations

"""V13.4 configurable parameter groups and within-stage priorities."""

from typing import Any
import pandas as pd

STAGE_ORDER = ["hydraulic", "mineral_kinetics", "sorption", "boundary_chemistry", "other"]


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _enabled(value: Any) -> bool:
    return _text(value).lower() not in {"0", "false", "no", "n", "inactive"}


def default_group(parameter: str) -> str:
    p = _text(parameter).lower()
    if p in {
        "bottom_head", "kz", "vg_alpha", "vg_l", "vg_n",
        "porosity", "residual_sat", "dispersivity", "aq_diff",
    }:
        return "hydraulic"
    if p.startswith(("keff_", "usr_", "imr_", "phi_")):
        return "mineral_kinetics"
    if p.startswith(("feoh_", "sorption_", "site_", "surface_")):
        return "sorption"
    if p.startswith(("bc_", "top_flux", "rain_", "inflow_", "outflow_")):
        return "boundary_chemistry"
    return "other"


def group_overrides(config) -> dict[str, dict[str, Any]]:
    """Optional sheet `v13_parameter_groups`.

    Supported columns:
      parameter | group | enabled | priority

    Missing sheet/columns are safe: deterministic defaults are used.
    """
    try:
        df = config.sheet("v13_parameter_groups")
    except Exception:
        df = pd.DataFrame()

    result: dict[str, dict[str, Any]] = {}
    if df.empty or "parameter" not in df.columns:
        return result

    for _, row in df.iterrows():
        parameter = _text(row.get("parameter"))
        if not parameter:
            continue
        group = _text(row.get("group")).lower() or default_group(parameter)
        priority = pd.to_numeric(row.get("priority", 1000), errors="coerce")
        result[parameter] = {
            "group": group,
            "enabled": _enabled(row.get("enabled", "yes")),
            "priority": int(priority) if pd.notna(priority) else 1000,
        }
    return result


def assign_groups(parameters: pd.DataFrame, config) -> pd.DataFrame:
    out = parameters.copy()
    overrides = group_overrides(config)
    records = []
    for _, row in out.iterrows():
        parameter = _text(row.get("parameter"))
        override = overrides.get(parameter, {})
        records.append({
            "v13_group": override.get("group", default_group(parameter)),
            "v13_group_enabled": bool(override.get("enabled", True)),
            "v13_priority": int(override.get("priority", 1000)),
        })
    extra = pd.DataFrame(records, index=out.index)
    out = pd.concat([out, extra], axis=1)
    out["v13_group_order"] = out["v13_group"].map(
        {name: index for index, name in enumerate(STAGE_ORDER)}
    ).fillna(len(STAGE_ORDER)).astype(int)
    return out


def ordered_groups(parameters: pd.DataFrame) -> list[str]:
    if parameters.empty:
        return []
    groups = (
        parameters[parameters["v13_group_enabled"]]
        [["v13_group", "v13_group_order"]]
        .drop_duplicates()
        .sort_values(["v13_group_order", "v13_group"])
    )
    return groups["v13_group"].astype(str).tolist()
