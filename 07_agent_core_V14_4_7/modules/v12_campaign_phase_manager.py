"""
V12 campaign phase manager.

This module adds scientific strategy logic without changing the V11 engine mechanics.

It is intentionally passive unless called by the main pipeline or strategy updater.

Main concepts:
- campaign phase:
    flow
    redox_oxygen
    sulfide_kinetics
    sorption
    mineralogy_buffering
    boundary_chemistry

- group-level cooldown:
    after repeated valid-but-not-best or bad runs, pause that scientific group

- safer step/bound handling:
    reduce step size after bad/non-improving runs
    avoid pushing parameters directly against bounds
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any
import json
import math

try:
    from modules.v12_status_labels import normalize_status_label
except Exception:
    from v12_status_labels import normalize_status_label


@dataclass(frozen=True)
class PhaseDefinition:
    name: str
    description: str
    parameter_keywords: tuple[str, ...]
    priority: int


PHASES: tuple[PhaseDefinition, ...] = (
    PhaseDefinition(
        name="flow",
        description="Hydraulic boundary condition, seepage, water balance, hydraulic conductivity and porosity first-order effects.",
        parameter_keywords=(
            "head",
            "bottom_head",
            "top_head",
            "flux",
            "flow",
            "kz",
            "kx",
            "ky",
            "hydraulic",
            "porosity",
            "theta",
            "water",
            "drain",
            "dispersivity",
            "dispersion",
            "vg_",
            "vg_alpha",
            "vg_n",
            "vg_l",
            "residual_sat",
            "saturation",
        ),
        priority=10,
    ),
    PhaseDefinition(
        name="redox_oxygen",
        description="Oxygen supply, oxygen scaling, gas/aquatic diffusion and redox-front control.",
        parameter_keywords=(
            "oxygen",
            "o2",
            "ox",
            "scaling_ox",
            "gas",
            "diff",
            "aq_diff",
            "redox",
            "redox_potential",
            "oxidation_reduction",
        ),
        priority=20,
    ),
    PhaseDefinition(
        name="sulfide_kinetics",
        description="Sulfide mineral oxidation and primary acid/metals release kinetics.",
        parameter_keywords=(
            "pyrite",
            "pyrrhot",
            "sphalerite",
            "chalcopyr",
            "galena",
            "sulfide",
            "sulphide",
            "keff_pyrite",
            "keff_pyrrhot",
            "keff_sphalerite",
            "keff_chalcopyr",
            "keff_galena",
        ),
        priority=30,
    ),
    PhaseDefinition(
        name="sorption",
        description="Retardation, adsorption, exchange and surface-complexation controls.",
        parameter_keywords=(
            "sorption",
            "sorb",
            "kd",
            "k_d",
            "exchange",
            "cec",
            "surface",
            "retard",
            "ferrihydrite_sorption",
            "feoh",
            "feoh_s",
            "feoh_w",
            "surface_area",
            "surface_density",
            "surface_mass",
        ),
        priority=40,
    ),
    PhaseDefinition(
        name="mineralogy_buffering",
        description="Neutralisation, buffering, mineral abundance, reactive surface area and secondary mineral effects.",
        parameter_keywords=(
            "calcite",
            "ferrihydrite",
            "biotite",
            "albite",
            "chlorite",
            "phi_",
            "imr_",
            "usr_",
            "surface_area",
            "buffer",
            "neutral",
            "alkalinity",
        ),
        priority=50,
    ),
    PhaseDefinition(
        name="boundary_chemistry",
        description="Influent/source/boundary water chemistry and concentration controls.",
        parameter_keywords=(
            "boundary",
            "inflow",
            "inlet",
            "source",
            "initial",
            "conc",
            "concentration",
            "ph",
            "so4",
            "sulfate",
            "sulphate",
            "init_fe",
            "bc_fe",
            "zn",
            "cu",
            "pb",
            "alk",
        ),
        priority=60,
    ),
)


PHASE_ORDER = tuple(p.name for p in sorted(PHASES, key=lambda p: p.priority))


BAD_OR_NONIMPROVING_STATUSES = {
    "valid_not_new_best",
    "rejected",
    "invalid",
    "failed",
    "runtime_failed",
    "rollback",
    "rolled_back",
    "blocked",
    "exhausted",
}


IMPROVING_STATUSES = {
    "accepted_new_best",
    "new_best",
    "accepted_best",
}


def phase_for_parameter(parameter: str | None) -> str:
    """
    Assign a parameter to a V12 scientific phase using keyword matching.
    """
    if not parameter:
        return "unknown"

    p = str(parameter).strip().lower()

    for phase in PHASES:
        for key in phase.parameter_keywords:
            if key.lower() in p:
                return phase.name

    return "unknown"


def _safe_float(value: Any) -> float | None:
    if _is_missing(value):
        return None
    try:
        x = float(value)
        if math.isnan(x) or math.isinf(x):
            return None
        return x
    except Exception:
        return None


def _is_missing(value: Any) -> bool:
    """
    Treat Python None, pandas/Excel NA, NaN, and text-like nan/null values as missing.
    This prevents recent-history summaries from reporting parameter='nan'.
    """
    if value is None:
        return True

    try:
        if value != value:
            return True
    except Exception:
        pass

    text = str(value).strip().lower()
    if text in {"", "nan", "none", "null", "<na>", "nat"}:
        return True

    return False


def _get_first(record: dict[str, Any], keys: tuple[str, ...], default: Any = None) -> Any:
    for key in keys:
        if key in record and not _is_missing(record[key]):
            return record[key]
    return default


def extract_objective(record: dict[str, Any]) -> float | None:
    """
    Robust objective-score extraction from V10/V11/V12 history rows.

    Important:
    - Do NOT scan generic score/RMSE/MAE/error columns.
    - In this project, run_health_score can be around 1.04 and must not be
      interpreted as the calibration objective.
    """
    exact_keys = (
        "qc_objective_score",
        "objective_total",
        "best_objective",
        "total_objective",
        "total_objective_score",
        "objective",
        "objective_score",
        "latest_objective",
        "latest_objective_score",
        "TOTAL_SCORE",
        "total_score",
        "run_objective",
        "run_objective_score",
        "run_total_score",
        "weighted_total_score",
        "calibration_score",
        "calibration_objective",
    )

    value = _get_first(record, exact_keys)
    return _safe_float(value)


def extract_parameter(record: dict[str, Any]) -> str | None:
    """
    Robust changed-parameter extraction from history rows.

    Avoids returning nan and searches common V10/V11/V12 parameter-name columns.
    """
    value = _get_first(
        record,
        (
            "parameter",
            "parameter_name",
            "changed_parameter",
            "changed_param",
            "candidate_parameter",
            "candidate_param",
            "selected_parameter",
            "selected_param",
            "suggested_parameter",
            "suggestion_parameter",
            "latest_parameter",
            "last_parameter",
            "updated_parameter",
            "modified_parameter",
            "parameter_changed",
        ),
    )

    if not _is_missing(value):
        return str(value).strip()

    # Fallback: scan for a singular parameter-like column.
    excluded = (
        "parameters",
        "count",
        "file",
        "folder",
        "reason",
        "status",
        "action",
        "blocked",
        "exhausted",
        "limited",
        "active",
        "inactive",
        "frozen",
        "timestamp",
        "time",
    )

    for key, value in record.items():
        key_l = str(key).lower()
        if "parameter" not in key_l and "param" not in key_l:
            continue
        if any(e in key_l for e in excluded):
            continue
        if _is_missing(value):
            continue

        text = str(value).strip()
        if len(text) <= 80:
            return text

    return None


def extract_status(record: dict[str, Any]) -> str:
    """
    Extract calibration-decision status.

    Acceptance/decision columns are preferred over generic run_status because
    run_status='success' only means MIN3P completed, not that calibration improved.
    """
    value = _get_first(
        record,
        (
            "qc_acceptance_status",
            "acceptance_status",
            "decision_action",
            "qc_decision_action",
            "decision_status",
            "qc_decision_status",
            "candidate_status",
            "suggestion_status",
            "calibration_status",
            "status",
            "run_status",
        ),
        default="unknown",
    )
    value = normalize_status_label(value)
    return str(value)


def history_records(history: Any) -> list[dict[str, Any]]:
    """
    Accept a pandas DataFrame, list of dicts, or None.
    """
    if history is None:
        return []

    if hasattr(history, "to_dict"):
        return list(history.to_dict(orient="records"))

    if isinstance(history, list):
        return [dict(r) for r in history]

    return []


@dataclass
class PhaseRecommendation:
    active_phase: str
    allowed_phases: list[str]
    cooldown_phases: dict[str, int]
    multiplier_scale: float
    reason: str
    recent_non_improving_by_phase: dict[str, int]
    recent_attempts_by_phase: dict[str, int]


class V12CampaignPhaseManager:
    """
    Passive V12 scientific campaign manager.

    It does not run MIN3P, modify DAT files, score results, or update Excel outputs.
    It only recommends which scientific group should be active and whether step sizes
    should be reduced.
    """

    def __init__(
        self,
        state_path: str | Path | None = None,
        non_improvement_limit: int = 3,
        cooldown_runs: int = 2,
        recent_window: int = 6,
    ):
        self.state_path = Path(state_path) if state_path else Path("v12_campaign_state.json")
        self.non_improvement_limit = int(non_improvement_limit)
        self.cooldown_runs = int(cooldown_runs)
        self.recent_window = int(recent_window)
        self.state = self.load_state()

    def load_state(self) -> dict[str, Any]:
        if self.state_path.exists():
            try:
                return json.loads(self.state_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        return {
            "version": "V12",
            "active_phase": "flow",
            "cooldowns": {},
            "notes": [],
        }

    def save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def decrement_cooldowns(self) -> None:
        cooldowns = dict(self.state.get("cooldowns", {}))
        updated = {}

        for phase, remaining in cooldowns.items():
            try:
                remaining_int = int(remaining)
            except Exception:
                remaining_int = 0

            if remaining_int > 1:
                updated[phase] = remaining_int - 1

        self.state["cooldowns"] = updated

    def update_cooldowns_from_recent_history(self, history: Any) -> None:
        records = history_records(history)
        recent = records[-self.recent_window :]

        non_improving_by_phase: dict[str, int] = {}

        for record in recent:
            status = extract_status(record)
            parameter = extract_parameter(record)
            phase = phase_for_parameter(parameter)

            if status in BAD_OR_NONIMPROVING_STATUSES:
                non_improving_by_phase[phase] = non_improving_by_phase.get(phase, 0) + 1

        cooldowns = dict(self.state.get("cooldowns", {}))

        for phase, count in non_improving_by_phase.items():
            if phase == "unknown":
                continue
            if count >= self.non_improvement_limit:
                cooldowns[phase] = self.cooldown_runs

        self.state["cooldowns"] = cooldowns

    def recommend(self, history: Any = None) -> PhaseRecommendation:
        """
        Return a campaign recommendation based on recent history.
        """
        self.decrement_cooldowns()
        self.update_cooldowns_from_recent_history(history)

        records = history_records(history)
        recent = records[-self.recent_window :]

        attempts_by_phase: dict[str, int] = {}
        non_improving_by_phase: dict[str, int] = {}
        improving_by_phase: dict[str, int] = {}

        for record in recent:
            parameter = extract_parameter(record)
            phase = phase_for_parameter(parameter)
            status = extract_status(record)

            attempts_by_phase[phase] = attempts_by_phase.get(phase, 0) + 1

            if status in BAD_OR_NONIMPROVING_STATUSES:
                non_improving_by_phase[phase] = non_improving_by_phase.get(phase, 0) + 1

            if status in IMPROVING_STATUSES:
                improving_by_phase[phase] = improving_by_phase.get(phase, 0) + 1

        cooldowns = {
            phase: int(value)
            for phase, value in dict(self.state.get("cooldowns", {})).items()
            if int(value) > 0
        }

        allowed = [p for p in PHASE_ORDER if p not in cooldowns]

        if not allowed:
            active = "manual_review"
            multiplier_scale = 0.25
            reason = "all scientific phases are in cooldown; manual review is recommended"
        else:
            current = self.state.get("active_phase", "flow")
            if current in allowed:
                active = current
            else:
                active = allowed[0]

            # Move to next available phase if the active phase repeatedly fails to improve.
            active_non_improving = non_improving_by_phase.get(active, 0)
            if active_non_improving >= self.non_improvement_limit:
                next_allowed = [p for p in allowed if p != active]
                if next_allowed:
                    active = next_allowed[0]

            # Reduce multiplier if recent campaign is mostly non-improving.
            recent_non_improving = sum(non_improving_by_phase.values())
            recent_attempts = max(1, sum(attempts_by_phase.values()))
            ratio = recent_non_improving / recent_attempts

            if ratio >= 0.75:
                multiplier_scale = 0.5
                reason = "recent campaign is mostly valid-but-not-best or bad; reduce step multiplier"
            elif ratio >= 0.5:
                multiplier_scale = 0.75
                reason = "recent campaign has moderate non-improvement; mildly reduce step multiplier"
            else:
                multiplier_scale = 1.0
                reason = "recent campaign does not require global multiplier reduction"

        self.state["active_phase"] = active
        self.save_state()

        return PhaseRecommendation(
            active_phase=active,
            allowed_phases=allowed,
            cooldown_phases=cooldowns,
            multiplier_scale=multiplier_scale,
            reason=reason,
            recent_non_improving_by_phase=non_improving_by_phase,
            recent_attempts_by_phase=attempts_by_phase,
        )


def adjust_candidate_step(
    old_value: float,
    proposed_value: float,
    min_value: float | None = None,
    max_value: float | None = None,
    multiplier_scale: float = 1.0,
    avoid_bound_fraction: float = 0.02,
) -> dict[str, Any]:
    """
    Safer V12 candidate adjustment.

    This function does not decide which parameter to change.
    It only makes an already proposed value less aggressive and safer near bounds.
    """
    old = _safe_float(old_value)
    proposed = _safe_float(proposed_value)

    if old is None or proposed is None:
        return {
            "new_value": proposed_value,
            "changed": False,
            "reason": "old or proposed value is not numeric",
        }

    scale = _safe_float(multiplier_scale)
    if scale is None:
        scale = 1.0

    # Reduce the step around the old value.
    adjusted = old + scale * (proposed - old)
    reasons = []

    if scale < 1.0:
        reasons.append(f"step reduced by multiplier_scale={scale:g}")

    lower = _safe_float(min_value)
    upper = _safe_float(max_value)

    if lower is not None and upper is not None and upper > lower:
        span = upper - lower
        margin = avoid_bound_fraction * span
        safe_lower = lower + margin
        safe_upper = upper - margin

        if adjusted < safe_lower:
            adjusted = safe_lower
            reasons.append("candidate moved inside lower bound safety margin")

        if adjusted > safe_upper:
            adjusted = safe_upper
            reasons.append("candidate moved inside upper bound safety margin")

    elif lower is not None:
        if adjusted < lower:
            adjusted = lower
            reasons.append("candidate clipped to lower bound")

    elif upper is not None:
        if adjusted > upper:
            adjusted = upper
            reasons.append("candidate clipped to upper bound")

    return {
        "new_value": adjusted,
        "changed": adjusted != proposed,
        "reason": "; ".join(reasons) if reasons else "candidate unchanged",
    }


def recommendation_as_dict(recommendation: PhaseRecommendation) -> dict[str, Any]:
    return asdict(recommendation)
