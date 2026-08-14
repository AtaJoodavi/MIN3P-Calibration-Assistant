from __future__ import annotations

"""Adaptive, parameter-aware V14 step-size controller.

V14.1 safety update
-------------------
Positive physical parameters use symmetric multiplicative moves and an
internal positive floor even when the Excel configuration omits a minimum.
This prevents invalid negative candidates from reaching MIN3P.

V14.4 search-space update
-------------------------
When a logarithmic parameter has explicit positive min/max bounds, the adaptive
step fraction is applied to the configured log10 range. Missing or unusable
bounds retain the V14.3.7 reciprocal local move.

V14.4.4 automatic range-adaptive initial exploration
---------------------------------------------------
Never-tested parameters no longer require manual initial-search columns.
The first search scale is inferred automatically from the configured min/max:

* positive logarithmic ranges receive a smooth coarse-to-fine starting factor,
  capped at x10 / divide by 10 for very wide ranges;
* bounded-linear ranges start from the global range fraction (default 5%);
* relative parameters with valid bounds are converted so the first move equals
  the same fraction of the configured span;
* zero, negative, missing, equal, or otherwise unusable bounds fall back safely
  to the global initial step (default 0.05).

Existing parameters that already have outcome history are never reset.

V14.4.5 bound/no-op guard
-------------------------
Candidates that collapse onto the current value after hard-bound clamping, or
whose remaining numeric change is below a conservative floating-point tolerance,
are marked as blocked before MIN3P.  The tolerance scales with the current value
and configured range; it is deliberately far smaller than any calibration step.

V14.4.7 Windows state-write hardening
------------------------------------
Step-size state persistence now uses the shared retrying atomic writer. Brief
Windows file-handle locks no longer terminate a long calibration campaign after
a single failed ``os.replace`` attempt.
"""

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any

from modules.v14_utils import (
    as_bool,
    as_float,
    json_safe,
    normalise_parameter,
    now,
    read_json,
    read_simple_yaml,
    write_json_atomic,
)


@dataclass
class StepPolicy:
    initial_step_fraction: float = 0.05
    minimum_step_fraction: float = 0.005
    maximum_step_fraction: float = 0.20
    growth_factor: float = 1.5
    shrink_factor: float = 0.5
    growth_after_accepted_streak: int = 2
    # V14.4: when explicit positive min/max bounds are available, a logarithmic
    # step is interpreted as a fraction of the configured log10 range rather
    # than as a local +/- percentage of the current value.  Missing/invalid
    # bounds automatically fall back to the legacy reciprocal multiplicative
    # move, preserving backwards compatibility and positive-parameter safety.
    log_range_step_scaling_enabled: bool = True
    # V14.4.4 automatic first-search scaling.  The default cap means no
    # never-tested positive parameter jumps by more than one order of magnitude
    # solely because its configured range is very wide.
    automatic_initial_scales_enabled: bool = True
    automatic_initial_max_factor: float = 10.0
    automatic_reference_log_decades: float = 6.0
    # This is a semantic guard, not a calibrated lower bound.  Explicit Excel
    # bounds still take precedence when they are stricter.
    semantic_positive_floor: float = 1.0e-12
    # V14.4.5: numerical no-op guard. These tolerances are not calibration
    # thresholds; they only prevent effectively identical configurations from
    # being sent to MIN3P after a proposed move is clamped to a hard bound.
    candidate_noop_absolute_tolerance: float = 1.0e-30
    candidate_noop_relative_tolerance: float = 1.0e-10
    candidate_noop_range_tolerance: float = 1.0e-12


class StepSizeController:
    VERSION = "V14.4.7"

    def __init__(self, paths, config):
        self.paths = paths
        self.config = config
        self.state_file = Path(paths.results_dir) / "v14_step_size_state.json"
        self.rules_file = Path(paths.agent_core_dir) / "config" / "calibration_rules.yaml"
        self._optimizer_settings = self._read_optimizer_settings()
        self.policy = self._load_policy()

    def _read_optimizer_settings(self) -> dict[str, Any]:
        try:
            return dict(self.config.optimizer_v13() or {})
        except Exception:
            return {}

    def _load_policy(self) -> StepPolicy:
        # Defaults in calibration_rules.yaml are project-wide; explicit values
        # in agent_config.xlsx / optimizer_v13 override them.  This order also
        # matches the documented configuration precedence.
        data: dict[str, Any] = {}
        data.update(read_simple_yaml(self.rules_file))
        data.update(self._optimizer_settings)
        policy = StepPolicy()
        aliases = {
            "initial_step_fraction": "initial_step_fraction",
            "minimum_step_fraction": "minimum_step_fraction",
            "maximum_step_fraction": "maximum_step_fraction",
            "step_growth_factor": "growth_factor",
            "step_shrink_factor": "shrink_factor",
            "growth_after_accepted_streak": "growth_after_accepted_streak",
            "automatic_initial_max_factor": "automatic_initial_max_factor",
            "automatic_reference_log_decades": "automatic_reference_log_decades",
            "semantic_positive_floor": "semantic_positive_floor",
            "candidate_noop_absolute_tolerance": "candidate_noop_absolute_tolerance",
            "candidate_noop_relative_tolerance": "candidate_noop_relative_tolerance",
            "candidate_noop_range_tolerance": "candidate_noop_range_tolerance",
        }
        for source, field in aliases.items():
            value = as_float(data.get(source), getattr(policy, field))
            if value is not None:
                setattr(policy, field, int(value) if field == "growth_after_accepted_streak" else float(value))
        if "log_range_step_scaling_enabled" in data:
            policy.log_range_step_scaling_enabled = as_bool(
                data.get("log_range_step_scaling_enabled"),
                policy.log_range_step_scaling_enabled,
            )
        if "automatic_initial_scales_enabled" in data:
            policy.automatic_initial_scales_enabled = as_bool(
                data.get("automatic_initial_scales_enabled"),
                policy.automatic_initial_scales_enabled,
            )
        policy.minimum_step_fraction = max(policy.minimum_step_fraction, 1e-12)
        policy.maximum_step_fraction = max(policy.maximum_step_fraction, policy.minimum_step_fraction)
        policy.growth_factor = max(policy.growth_factor, 1.0)
        policy.shrink_factor = min(max(policy.shrink_factor, 1e-12), 1.0)
        policy.growth_after_accepted_streak = max(int(policy.growth_after_accepted_streak), 1)
        policy.automatic_initial_max_factor = max(float(policy.automatic_initial_max_factor), 1.000001)
        policy.automatic_reference_log_decades = max(float(policy.automatic_reference_log_decades), 1e-9)
        policy.semantic_positive_floor = max(float(policy.semantic_positive_floor), 1e-30)
        policy.candidate_noop_absolute_tolerance = max(float(policy.candidate_noop_absolute_tolerance), 0.0)
        policy.candidate_noop_relative_tolerance = max(float(policy.candidate_noop_relative_tolerance), 0.0)
        policy.candidate_noop_range_tolerance = max(float(policy.candidate_noop_range_tolerance), 0.0)
        return policy

    def _load(self) -> dict[str, Any]:
        state = read_json(self.state_file, {"parameters": {}, "metadata": {}})
        if not isinstance(state, dict):
            state = {"parameters": {}, "metadata": {}}
        state.setdefault("parameters", {})
        state.setdefault("metadata", {})
        return state

    def _save(self, state: dict[str, Any]) -> None:
        state["metadata"].update({
            "updated_at": now(),
            "optimizer_version": self.VERSION,
            "initial_step_fraction": self.policy.initial_step_fraction,
            "minimum_step_fraction": self.policy.minimum_step_fraction,
            "maximum_step_fraction": self.policy.maximum_step_fraction,
            "log_range_step_scaling_enabled": self.policy.log_range_step_scaling_enabled,
            "automatic_initial_scales_enabled": self.policy.automatic_initial_scales_enabled,
            "automatic_initial_max_factor": self.policy.automatic_initial_max_factor,
            "automatic_reference_log_decades": self.policy.automatic_reference_log_decades,
            "candidate_noop_absolute_tolerance": self.policy.candidate_noop_absolute_tolerance,
            "candidate_noop_relative_tolerance": self.policy.candidate_noop_relative_tolerance,
            "candidate_noop_range_tolerance": self.policy.candidate_noop_range_tolerance,
        })
        write_json_atomic(self.state_file, state)

    @staticmethod
    def requires_strictly_positive(parameter: str) -> bool:
        """Return True only for parameters whose physical domain is > 0.

        This deliberately excludes pH, heads, and van Genuchten ``vg_l``.
        Parameters which can physically be zero (for example residual
        saturation) should be governed by their explicit configuration bounds.
        """
        name = normalise_parameter(parameter)
        if name.startswith(("phi_", "keff_", "usr_", "imr_", "feoh_", "scaling_", "s_")):
            return True
        return name in {"kz", "aq_diff", "dispersivity", "vg_alpha", "vg_n"}

    @staticmethod
    def infer_move_space(parameter: str, value: float | None) -> str:
        name = normalise_parameter(parameter)
        # Positive quantities use a reciprocal multiplicative pair:
        # +s -> x*(1+s), -s -> x/(1+s).  This is symmetric in log-space and
        # prevents a nominal 5% decrement becoming an absolute subtraction.
        if StepSizeController.requires_strictly_positive(name):
            return "logarithmic"
        if name == "kz" or name.startswith(("keff_", "usr_", "imr_", "aq_diff", "dispers")):
            return "logarithmic"
        if name.startswith("phi_") or any(token in name for token in ("porosity", "bottom_head", "head", "vg_alpha", "vg_n", "vg_l", "residual_sat")):
            return "bounded_linear"
        if value is not None and value > 0:
            return "relative"
        return "bounded_linear"

    @staticmethod
    def _group_key(group: str | None) -> str:
        text = str(group or "").strip().casefold()
        return "_".join(part for part in "".join(ch if ch.isalnum() else " " for ch in text).split() if part)

    def _parameter_config_row(self, parameter: str) -> dict[str, Any]:
        """Return the parameter row; only value/min/max metadata are used here."""
        try:
            frame = self.config.parameters()
        except Exception:
            return {}
        if frame is None or frame.empty or "parameter" not in frame.columns:
            return {}
        key = normalise_parameter(parameter)
        matches = frame[frame["parameter"].map(normalise_parameter).eq(key)]
        if matches.empty:
            return {}
        row = matches.iloc[0]
        return {str(column): row.get(column) for column in frame.columns}

    def _resolve_initial_search(
        self,
        *,
        parameter: str,
        value: float | None,
        lower: float | None,
        upper: float | None,
        group: str | None,
        move_space: str,
    ) -> dict[str, Any]:
        """Infer the first search scale automatically from configured bounds.

        V14.4.4 intentionally ignores the V14.4.3 manual columns
        ``initial_search_mode``, ``initial_factor`` and
        ``initial_range_fraction``.  The user's ``value/min/max`` are sufficient.

        Positive log-range rule
        -----------------------
        Let R = log10(max/min).  The starting transformed-range fraction grows
        smoothly from the global default (normally 0.05) toward the fraction
        that would produce x10 over a six-decade range.  The requested factor
        is always capped at ``automatic_initial_max_factor`` (default x10).

        This gives, approximately, x10 for six-or-more decades, x2 for a
        three-decade range, and only ~x1.02 for the narrow Kz range used in HCT3.

        For bounded-linear parameters the global initial fraction is already a
        fraction of the configured span.  For relative parameters with valid
        bounds, the local relative fraction is converted so the numeric first
        move equals the global fraction of the configured span.  If bounds are
        unusable (including zero/negative bounds for a logarithmic transform),
        the controller falls back to the global initial step without failing.
        """
        row = self._parameter_config_row(parameter)

        minimum = max(float(self.policy.minimum_step_fraction), 1e-12)
        maximum = max(float(self.policy.maximum_step_fraction), minimum)

        configured_lower = as_float(lower)
        configured_upper = as_float(upper)
        if configured_lower is None:
            configured_lower = as_float(row.get("min"))
        if configured_upper is None:
            configured_upper = as_float(row.get("max"))
        current_value = as_float(value)
        if current_value is None:
            current_value = as_float(row.get("value"))

        def finite(x: float | None) -> bool:
            return x is not None and math.isfinite(float(x))

        valid_bounds = (
            finite(configured_lower)
            and finite(configured_upper)
            and float(configured_upper) > float(configured_lower)
        )
        valid_positive_log_bounds = (
            valid_bounds
            and float(configured_lower) > 0.0
            and float(configured_upper) > 0.0
        )

        default_step = float(self.policy.initial_step_fraction)
        raw_step = default_step
        source = "automatic_global_fallback"
        resolved_mode = "automatic_fallback"
        note = ""
        log_decades = None
        automatic_factor_target = None

        if not self.policy.automatic_initial_scales_enabled:
            source = "automatic_scaling_disabled_global_fallback"
            note = "automatic_initial_scales_enabled=false"
        elif (
            self.policy.log_range_step_scaling_enabled
            and move_space == "logarithmic"
            and current_value is not None
            and current_value > 0
            and valid_positive_log_bounds
        ):
            log_decades = math.log10(float(configured_upper) / float(configured_lower))
            if log_decades > 0 and math.isfinite(log_decades):
                max_factor = float(self.policy.automatic_initial_max_factor)
                reference_decades = float(self.policy.automatic_reference_log_decades)
                target_reference_fraction = math.log10(max_factor) / reference_decades
                breadth = min(max(log_decades / reference_decades, 0.0), 1.0)
                smooth_fraction = default_step + (target_reference_fraction - default_step) * breadth
                factor_cap_fraction = math.log10(max_factor) / log_decades
                raw_step = min(smooth_fraction, factor_cap_fraction)
                automatic_factor_target = 10.0 ** (raw_step * log_decades)
                source = "automatic_positive_log_range"
                resolved_mode = "automatic_log_range"
            else:
                note = "nonpositive_or_nonfinite_log_range; global_fallback_used"
        elif move_space == "bounded_linear" and valid_bounds:
            raw_step = default_step
            source = "automatic_bounded_linear_range"
            resolved_mode = "automatic_linear_range"
        elif move_space == "relative" and valid_bounds and current_value is not None and current_value != 0:
            # propose(relative) uses base_value * (1 +/- step).  Convert the
            # configured range fraction into an equivalent local relative step.
            span = float(configured_upper) - float(configured_lower)
            raw_step = default_step * span / abs(float(current_value))
            source = "automatic_relative_from_configured_range"
            resolved_mode = "automatic_relative_range"
        else:
            if move_space == "logarithmic" and not self.policy.log_range_step_scaling_enabled:
                source = "log_range_scaling_disabled_global_fallback"
                note = "log_range_step_scaling_enabled=false; global_fallback_used"
            elif move_space == "logarithmic":
                note = "log_range_unusable_zero_negative_missing_or_equal_bound; global_fallback_used"
            elif not valid_bounds:
                note = "configured_bounds_missing_equal_or_invalid; global_fallback_used"
            else:
                note = "automatic_range_transform_unavailable; global_fallback_used"

        if not math.isfinite(float(raw_step)) or raw_step <= 0:
            raw_step = default_step
            source = "automatic_nonfinite_step_global_fallback"
            resolved_mode = "automatic_fallback"
            note = "resolved_initial_step_nonfinite_or_nonpositive; global_fallback_used"

        resolved_step = min(max(float(raw_step), minimum), maximum)
        clamped = not math.isclose(resolved_step, float(raw_step), rel_tol=1e-12, abs_tol=1e-15)

        effective_factor = None
        if move_space == "logarithmic" and valid_positive_log_bounds:
            if log_decades is None:
                log_decades = math.log10(float(configured_upper) / float(configured_lower))
            if log_decades > 0:
                effective_factor = 10.0 ** (resolved_step * log_decades)
        elif move_space == "relative" and current_value not in (None, 0):
            effective_factor = 1.0 + resolved_step

        return {
            "initial_search_mode": resolved_mode,
            "initial_search_source": source,
            "initial_factor_requested": None,
            "initial_range_fraction_requested": None,
            "initial_step_fraction_raw": float(raw_step),
            "initial_step_fraction_resolved": resolved_step,
            "initial_step_fraction_clamped": clamped,
            "initial_factor_effective": effective_factor,
            "initial_configured_log_range_decades": log_decades,
            "automatic_initial_factor_target": automatic_factor_target,
            "automatic_initial_max_factor": float(self.policy.automatic_initial_max_factor),
            "automatic_default_step_fraction": default_step,
            "initial_search_note": note,
            "minimum_step_fraction": minimum,
            "maximum_step_fraction": maximum,
            "configured_group": str(group or ""),
        }

    @staticmethod
    def _record_is_pristine(record: dict[str, Any]) -> bool:
        return (
            not str(record.get("last_direction", "") or "").strip()
            and not str(record.get("last_outcome", "") or "").strip()
            and int(record.get("accepted_streak", 0) or 0) == 0
            and int(record.get("rejected_streak", 0) or 0) == 0
            and int(record.get("oscillation_count", 0) or 0) == 0
            and not bool(record.get("pending_shrink_after_pair", False))
        )

    def sync_initial_scale_if_pristine(
        self,
        parameter: str,
        *,
        value: float | None = None,
        lower: float | None = None,
        upper: float | None = None,
        group: str | None = None,
    ) -> dict[str, Any]:
        """Apply V14.4.4 initial scale only to a never-tested parameter."""
        state = self._load()
        key = normalise_parameter(parameter)
        record = state["parameters"].get(key)
        if record is None:
            return self.ensure_parameter(parameter, value, lower=lower, upper=upper, group=group)
        self._migrate_record(record, parameter, value, lower=lower, upper=upper, group=group)
        if self._record_is_pristine(record):
            spec = self._resolve_initial_search(
                parameter=parameter,
                value=value,
                lower=lower,
                upper=upper,
                group=group,
                move_space=str(record.get("move_space") or self.infer_move_space(parameter, value)),
            )
            record.update(spec)
            record["initial_step_fraction"] = spec["initial_step_fraction_resolved"]
            record["current_step_fraction"] = spec["initial_step_fraction_resolved"]
            record["initial_scale_applied_v14_4_4"] = True
            record["last_update"] = now()
            state["parameters"][key] = record
            self._save(state)
        return dict(record)

    def _migrate_record(
        self,
        record: dict[str, Any],
        parameter: str,
        value: float | None,
        *,
        lower: float | None = None,
        upper: float | None = None,
        group: str | None = None,
    ) -> bool:
        """Upgrade existing state records without resetting adaptive history."""
        changed = False
        must_be_positive = self.requires_strictly_positive(parameter)
        desired_space = self.infer_move_space(parameter, value)
        if must_be_positive and str(record.get("move_space", "")) != "logarithmic":
            record["move_space"] = "logarithmic"
            record["move_space_migration_reason"] = "V14.1_positive_parameter_safety"
            changed = True
        if bool(record.get("requires_strictly_positive", False)) != must_be_positive:
            record["requires_strictly_positive"] = must_be_positive
            changed = True
        if not str(record.get("move_space", "")):
            record["move_space"] = desired_space
            changed = True
        effective_group = group if str(group or "").strip() else record.get("configured_group", "")
        spec = self._resolve_initial_search(
            parameter=parameter, value=value, lower=lower, upper=upper, group=effective_group,
            move_space=str(record.get("move_space") or desired_space),
        )
        for field, value_ in spec.items():
            if field not in record or record.get(field) != value_:
                record[field] = value_
                changed = True
        # Per-parameter adaptive limits are safe to migrate; the current step is
        # deliberately untouched here. `sync_initial_scale_if_pristine` is the
        # only method allowed to reset a never-tested parameter to its new start.
        record["minimum_step_fraction"] = spec["minimum_step_fraction"]
        record["maximum_step_fraction"] = spec["maximum_step_fraction"]
        if changed:
            record["last_update"] = now()
        return changed

    def ensure_parameter(
        self,
        parameter: str,
        value: float | None = None,
        *,
        lower: float | None = None,
        upper: float | None = None,
        group: str | None = None,
    ) -> dict[str, Any]:
        state = self._load()
        key = normalise_parameter(parameter)
        record = state["parameters"].get(key)
        changed = False
        if record is None:
            move_space = self.infer_move_space(parameter, value)
            spec = self._resolve_initial_search(
                parameter=parameter, value=value, lower=lower, upper=upper, group=group, move_space=move_space
            )
            record = {
                "parameter": parameter,
                "move_space": move_space,
                "requires_strictly_positive": self.requires_strictly_positive(parameter),
                "initial_step_fraction": spec["initial_step_fraction_resolved"],
                "minimum_step_fraction": spec["minimum_step_fraction"],
                "maximum_step_fraction": spec["maximum_step_fraction"],
                "current_step_fraction": spec["initial_step_fraction_resolved"],
                **spec,
                "initial_scale_applied_v14_4_4": True,
                "accepted_streak": 0,
                "rejected_streak": 0,
                "oscillation_count": 0,
                "last_direction": "",
                "pending_shrink_after_pair": False,
                "last_update": now(),
            }
            state["parameters"][key] = record
            changed = True
        else:
            changed = self._migrate_record(
                record, parameter, value, lower=lower, upper=upper, group=group
            )
            state["parameters"][key] = record
        if changed:
            self._save(state)
        return dict(record)

    def current_step(self, parameter: str, value: float | None = None) -> float:
        record = self.ensure_parameter(parameter, value)
        return float(record["current_step_fraction"])

    def no_op_assessment(
        self,
        *,
        base_value: float,
        candidate_value: float,
        lower: float | None = None,
        upper: float | None = None,
    ) -> dict[str, Any]:
        """Return a conservative assessment of whether a move is effectively a no-op.

        This is intentionally a numerical guard, not an optimisation stopping
        threshold.  It catches exact hard-bound clamps and sub-floating-scale
        residual moves without suppressing legitimate small refinement steps.
        """
        base = float(base_value)
        candidate = float(candidate_value)
        delta = abs(candidate - base)
        magnitude = max(abs(base), abs(candidate))

        lo = as_float(lower)
        hi = as_float(upper)
        span = None
        if lo is not None and hi is not None and math.isfinite(lo) and math.isfinite(hi) and hi > lo:
            span = hi - lo

        abs_tol = float(self.policy.candidate_noop_absolute_tolerance)
        rel_tol = float(self.policy.candidate_noop_relative_tolerance) * magnitude
        range_tol = (
            float(self.policy.candidate_noop_range_tolerance) * span
            if span is not None
            else 0.0
        )
        tolerance = max(abs_tol, rel_tol, range_tol)
        blocked = delta <= tolerance
        return {
            "blocked_noop": bool(blocked),
            "noop_delta": delta,
            "noop_tolerance": tolerance,
            "noop_absolute_tolerance": abs_tol,
            "noop_relative_component": rel_tol,
            "noop_range_component": range_tol,
        }

    def propose(
        self,
        *,
        parameter: str,
        base_value: float,
        direction: str,
        lower: float | None,
        upper: float | None,
    ) -> dict[str, Any]:
        record = self.ensure_parameter(parameter, base_value, lower=lower, upper=upper)
        step = float(record["current_step_fraction"])
        move_space = str(record["move_space"])
        sign = 1.0 if direction == "increase" else -1.0
        strict_positive = bool(record.get("requires_strictly_positive", self.requires_strictly_positive(parameter)))

        configured_lower = as_float(lower)
        configured_upper = as_float(upper)
        effective_lower = configured_lower
        if strict_positive:
            floor = self.policy.semantic_positive_floor
            effective_lower = max(configured_lower if configured_lower is not None else floor, floor)

        if configured_upper is not None and effective_lower is not None and configured_upper < effective_lower:
            return {
                "parameter": parameter,
                "move_space": move_space,
                "step_fraction": step,
                "old_value": base_value,
                "new_value": base_value,
                "direction": direction,
                "factor_requested": 1.0,
                "factor_applied": 1.0,
                "numeric_change": 0.0,
                "at_bound": True,
                "invalid": True,
                "bound_reason": "configured_upper_below_effective_lower_bound",
                "configured_lower": configured_lower,
                "configured_upper": configured_upper,
                "effective_lower": effective_lower,
                "strictly_positive": strict_positive,
                "bound_direction": direction,
                "step_basis": "invalid_bounds",
                "configured_log_range_decades": None,
                "transformed_position": None,
                "range_normalized": False,
                "initial_search_mode": record.get("initial_search_mode", "automatic_fallback"),
                "initial_search_source": record.get("initial_search_source", "automatic_global_fallback"),
                "initial_factor_requested": record.get("initial_factor_requested"),
                "initial_range_fraction_requested": record.get("initial_range_fraction_requested"),
                "initial_step_fraction_resolved": record.get("initial_step_fraction_resolved", record.get("initial_step_fraction")),
                "initial_step_fraction_clamped": bool(record.get("initial_step_fraction_clamped", False)),
                "initial_factor_effective": record.get("initial_factor_effective"),
                "blocked_noop": True,
                "noop_delta": 0.0,
                "noop_tolerance": 0.0,
            }

        if strict_positive and base_value <= 0:
            return {
                "parameter": parameter,
                "move_space": move_space,
                "step_fraction": step,
                "old_value": base_value,
                "new_value": base_value,
                "direction": direction,
                "factor_requested": 1.0,
                "factor_applied": 1.0,
                "numeric_change": 0.0,
                "at_bound": True,
                "invalid": True,
                "bound_reason": "nonpositive_baseline_for_strictly_positive_parameter",
                "configured_lower": configured_lower,
                "configured_upper": configured_upper,
                "effective_lower": effective_lower,
                "strictly_positive": strict_positive,
                "bound_direction": direction,
                "step_basis": "invalid_baseline",
                "configured_log_range_decades": None,
                "transformed_position": None,
                "range_normalized": False,
                "initial_search_mode": record.get("initial_search_mode", "automatic_fallback"),
                "initial_search_source": record.get("initial_search_source", "automatic_global_fallback"),
                "initial_factor_requested": record.get("initial_factor_requested"),
                "initial_range_fraction_requested": record.get("initial_range_fraction_requested"),
                "initial_step_fraction_resolved": record.get("initial_step_fraction_resolved", record.get("initial_step_fraction")),
                "initial_step_fraction_clamped": bool(record.get("initial_step_fraction_clamped", False)),
                "initial_factor_effective": record.get("initial_factor_effective"),
                "blocked_noop": True,
                "noop_delta": 0.0,
                "noop_tolerance": 0.0,
            }

        step_basis = "local_fraction"
        configured_log_range_decades = None
        transformed_position = None

        if move_space == "logarithmic" and base_value > 0:
            # V14.4 range-normalised logarithmic move.  When the user supplied
            # valid positive min/max bounds, `step` means a fraction of the
            # feasible log10 search interval.  This gives comparable search
            # effort to parameters whose ranges span very different orders of
            # magnitude.  Example: [1e-6, 1] with step=0.05 => factor=10^0.3
            # ~= 1.995 instead of the legacy 1.05.
            use_configured_log_range = (
                self.policy.log_range_step_scaling_enabled
                and configured_lower is not None
                and configured_lower > 0
                and configured_upper is not None
                and configured_upper > configured_lower
            )
            if use_configured_log_range:
                log_lower = math.log10(configured_lower)
                log_upper = math.log10(configured_upper)
                configured_log_range_decades = log_upper - log_lower
                delta_log10 = sign * step * configured_log_range_decades
                requested_factor = 10.0 ** delta_log10
                requested_candidate = base_value * requested_factor
                step_basis = "configured_log_range"
                transformed_position = (math.log10(base_value) - log_lower) / configured_log_range_decades
            else:
                requested_factor = (1.0 + step) if sign > 0 else 1.0 / (1.0 + step)
                requested_candidate = base_value * requested_factor
                step_basis = "legacy_local_log_fraction"
        elif move_space == "relative":
            requested_candidate = base_value * (1.0 + sign * step)
            requested_factor = requested_candidate / base_value if base_value else 1.0
            step_basis = "local_relative_fraction"
        else:
            span = None
            if configured_lower is not None and configured_upper is not None and configured_upper > configured_lower:
                span = configured_upper - configured_lower
            scale = span if span is not None else max(abs(base_value), 1.0)
            requested_candidate = base_value + sign * step * scale
            requested_factor = requested_candidate / base_value if base_value else 1.0
            if span is not None:
                step_basis = "configured_linear_range"
                transformed_position = (base_value - configured_lower) / span
            else:
                step_basis = "unbounded_linear_local_scale"

        hit_lower = effective_lower is not None and requested_candidate < effective_lower
        hit_upper = configured_upper is not None and requested_candidate > configured_upper
        candidate = requested_candidate
        if effective_lower is not None:
            candidate = max(candidate, effective_lower)
        if configured_upper is not None:
            candidate = min(candidate, configured_upper)

        noop = self.no_op_assessment(
            base_value=base_value,
            candidate_value=candidate,
            lower=configured_lower,
            upper=configured_upper,
        )

        if hit_lower:
            bound_reason = "effective_lower_bound_reached"
        elif hit_upper:
            bound_reason = "configured_upper_bound_reached"
        elif noop["blocked_noop"]:
            bound_reason = "candidate_effectively_equals_baseline"
        else:
            bound_reason = ""

        return {
            "parameter": parameter,
            "move_space": move_space,
            "step_fraction": step,
            "old_value": base_value,
            "new_value": candidate,
            "direction": direction,
            "factor_requested": requested_factor,
            "factor_applied": candidate / base_value if base_value else 1.0,
            "numeric_change": candidate - base_value,
            "at_bound": bool(noop["blocked_noop"] or hit_lower or hit_upper),
            "invalid": False,
            "bound_reason": bound_reason,
            "configured_lower": configured_lower,
            "configured_upper": configured_upper,
            "effective_lower": effective_lower,
            "strictly_positive": strict_positive,
            "bound_direction": direction if (candidate == base_value or hit_lower or hit_upper) else "",
            "step_basis": step_basis,
            "configured_log_range_decades": configured_log_range_decades,
            "transformed_position": transformed_position,
            "range_normalized": step_basis in {"configured_log_range", "configured_linear_range"},
            "initial_search_mode": record.get("initial_search_mode", "automatic_fallback"),
            "initial_search_source": record.get("initial_search_source", "automatic_global_fallback"),
            "initial_factor_requested": record.get("initial_factor_requested"),
            "initial_range_fraction_requested": record.get("initial_range_fraction_requested"),
            "initial_step_fraction_resolved": record.get("initial_step_fraction_resolved", record.get("initial_step_fraction")),
            "initial_step_fraction_clamped": bool(record.get("initial_step_fraction_clamped", False)),
            "initial_factor_effective": record.get("initial_factor_effective"),
            **noop,
        }

    def observe_outcome(
        self,
        parameter: str,
        *,
        direction: str,
        accepted: bool,
        defer_shrink: bool,
        reason: str,
    ) -> dict[str, Any]:
        state = self._load()
        key = normalise_parameter(parameter)
        record = state["parameters"].get(key, self.ensure_parameter(parameter))
        step = float(record["current_step_fraction"])
        prior_direction = str(record.get("last_direction", ""))
        if accepted:
            streak = int(record.get("accepted_streak", 0) or 0) + 1
            record["accepted_streak"] = streak
            record["rejected_streak"] = 0
            record["pending_shrink_after_pair"] = False
            if streak >= self.policy.growth_after_accepted_streak:
                step = min(step * self.policy.growth_factor, float(record["maximum_step_fraction"]))
                record["accepted_streak"] = 0
        else:
            record["accepted_streak"] = 0
            record["rejected_streak"] = int(record.get("rejected_streak", 0) or 0) + 1
            if prior_direction and prior_direction != direction:
                record["oscillation_count"] = int(record.get("oscillation_count", 0) or 0) + 1
            if defer_shrink:
                record["pending_shrink_after_pair"] = True
            else:
                step = max(step * self.policy.shrink_factor, float(record["minimum_step_fraction"]))
                record["pending_shrink_after_pair"] = False
        record["current_step_fraction"] = step
        record["last_direction"] = direction
        record["last_outcome"] = "accepted" if accepted else "rejected"
        record["last_reason"] = reason
        record["last_update"] = now()
        state["parameters"][key] = record
        self._save(state)
        return dict(record)

    def finalize_failed_direction_family(self, parameter: str) -> dict[str, Any]:
        """Apply a deferred shrink only after the symmetric/opposite pair completed."""
        state = self._load()
        key = normalise_parameter(parameter)
        record = state["parameters"].get(key, self.ensure_parameter(parameter))
        step = float(record["current_step_fraction"])
        if bool(record.get("pending_shrink_after_pair", False)):
            step = max(step * self.policy.shrink_factor, float(record["minimum_step_fraction"]))
        record["current_step_fraction"] = step
        record["pending_shrink_after_pair"] = False
        record["accepted_streak"] = 0
        record["last_reason"] = "completed_rejected_direction_family"
        record["last_update"] = now()
        state["parameters"][key] = record
        self._save(state)
        return dict(record)

    def snapshot(self) -> dict[str, Any]:
        return json_safe(self._load())
