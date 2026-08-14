from __future__ import annotations

"""V14 adaptive directional coordinate optimiser.

This is an incremental evolution of V13.5, not a replacement of its safety
contract.  The protected single-parameter invariant is retained:

* a direction family is completed before a different parameter is selected;
* rejected increase tests decrease of the same parameter next;
* rejected decrease tests increase of the same parameter next;
* accepted moves continue in their accepted direction;
* rejected/invalid candidates are reported so the pipeline can restore the
  exact V14 best snapshot.

V14 adds a runtime state overlay, adaptive parameter-specific steps, bounded
interaction-pair search, diagnostics-aware reactivation, and advisory GPT
ranking.  Deterministic rules always take precedence over GPT.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import time
import uuid
import warnings

import pandas as pd

from modules.v13_parameter_groups import assign_groups, ordered_groups
from modules.v14_utils import (
    append_excel_atomic,
    as_bool,
    as_float,
    json_safe,
    normalise_parameter,
    now,
    read_excel_safe,
    read_json,
    write_excel_atomic,
)


VALID_USER_ACTIVE = {"active", "yes", "true", "1", "y"}


def _queue(value: Any) -> list[str]:
    if value is None or pd.isna(value):
        return []
    return [item for item in str(value).split("|") if item in {"increase", "decrease"}]


def _opposite(direction: str) -> str:
    return "decrease" if direction == "increase" else "increase"


@dataclass
class OptimizerSettingsV14:
    initial_step_fraction: float = 0.05
    minimum_step_fraction: float = 0.005
    maximum_step_fraction: float = 0.20
    step_growth_factor: float = 1.5
    step_shrink_factor: float = 0.5
    min_absolute_improvement: float = 0.001
    min_relative_improvement: float = 0.0001
    neutral_absolute_band: float = 0.0005
    max_passes: int = 2
    reopen_hydraulic_after_any_accept: int = 1
    reopen_same_group_after_accept: int = 1
    reactivation_valid_run_interval: int = 4
    exhausted_cooldown_valid_runs: int = 4
    gpt_min_confidence: float = 0.60
    # V14.3.3: local futility is deliberately enabled only for the two
    # independently validated coordinate-search groups below.  This scope is
    # code-controlled, not Excel-controlled, so a malformed configuration
    # cannot silently extend automatic completion to unrelated process groups.
    local_futility_enabled: bool = True
    local_futility_min_non_neutral_failed_pairs: int = 2
    local_futility_groups: tuple[str, ...] = ("sorption", "boundary_chemistry")
    # V14.3.4 release guard: a second pass is allowed only when the immediately
    # preceding pass produced at least one accepted meaningful improvement.
    # This is code-controlled, not Excel-controlled. ``max_passes`` remains a
    # hard upper cap rather than a command to repeat every parameter.
    require_accepted_improvement_for_additional_pass: bool = True


class AdaptiveCoordinateOptimizerV14:
    VERSION = "V14.4.7"

    def __init__(self, paths, config, settings: OptimizerSettingsV14 | None = None):
        self.paths = paths
        self.config = config
        self.settings = settings or self._settings()
        self.memory_file = Path(paths.results_dir) / "v14_optimizer_parameter_state.xlsx"
        self.state_file = Path(paths.results_dir) / "v14_optimizer_state.xlsx"
        self.event_file = Path(paths.results_dir) / "v14_optimizer_events.xlsx"
        self.decision_file = Path(paths.results_dir) / "v14_candidate_decisions.xlsx"
        self.services: dict[str, Any] = {}
        self._gpt_advisory: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Basic persistence and configuration
    # ------------------------------------------------------------------

    def _settings(self) -> OptimizerSettingsV14:
        settings = OptimizerSettingsV14()
        values: dict[str, Any] = {}
        try:
            values = self.config.optimizer_v13() or {}
        except Exception:
            pass
        # Keep compatibility with the current numeric optimizer sheet.  The
        # group scope remains code-controlled in V14.3.3, so this gate cannot
        # accidentally spread to unrelated process groups through a malformed
        # Excel value.
        for field in settings.__dataclass_fields__:
            if field in {
                "local_futility_enabled",
                "local_futility_groups",
                "require_accepted_improvement_for_additional_pass",
            }:
                continue
            raw = as_float(values.get(field), getattr(settings, field))
            if raw is None:
                continue
            if field.startswith(("max_", "reopen_", "reactivation_", "exhausted_", "local_futility_min_")):
                setattr(settings, field, int(raw))
            else:
                setattr(settings, field, float(raw))
        settings.minimum_step_fraction = max(settings.minimum_step_fraction, 1e-12)
        settings.maximum_step_fraction = max(settings.maximum_step_fraction, settings.minimum_step_fraction)
        settings.step_growth_factor = max(settings.step_growth_factor, 1.0)
        settings.step_shrink_factor = min(max(settings.step_shrink_factor, 1e-12), 1.0)
        settings.max_passes = max(int(settings.max_passes), 1)
        settings.local_futility_min_non_neutral_failed_pairs = max(
            int(settings.local_futility_min_non_neutral_failed_pairs), 1
        )
        # Preserve an explicit, deterministic scope even when a caller passes
        # a custom settings instance.  Excel never controls this list.
        raw_groups = getattr(settings, "local_futility_groups", ())
        if isinstance(raw_groups, str):
            raw_groups = (raw_groups,)
        try:
            groups = tuple(
                str(group).strip().casefold()
                for group in raw_groups
                if str(group).strip()
            )
        except TypeError:
            groups = ()
        settings.local_futility_groups = groups or ("sorption",)
        # Never permit a workbook value to silently disable release pass-control.
        settings.require_accepted_improvement_for_additional_pass = bool(
            getattr(settings, "require_accepted_improvement_for_additional_pass", True)
        )
        return settings

    @staticmethod
    def _read(path: Path) -> pd.DataFrame:
        return read_excel_safe(path)

    @staticmethod
    def _write(path: Path, frame: pd.DataFrame) -> None:
        last_error: PermissionError | None = None
        for attempt in range(8):
            try:
                write_excel_atomic(path, frame)
                return
            except PermissionError as exc:
                last_error = exc
                if attempt < 7:
                    time.sleep(0.10 * (attempt + 1))
        if last_error is not None:
            raise last_error

    def _read_state(self) -> dict[str, Any]:
        frame = self._read(self.state_file)
        if frame.empty:
            return {}
        state = frame.iloc[0].to_dict()
        # Excel persists dictionaries as JSON text.  Restore only the known
        # structured fields so selection logic never treats a string as a map.
        for key in ("last_diagnostics", "pending_pair_context"):
            value = state.get(key)
            if isinstance(value, str):
                try:
                    decoded = json.loads(value)
                    state[key] = decoded if isinstance(decoded, dict) else {}
                except Exception:
                    state[key] = {}
            elif not isinstance(value, dict):
                state[key] = {}
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        # V14.4.2 audit fix: state loaded from older releases can already
        # contain stale ``updated_at`` / ``optimizer_version`` keys.  Write
        # authoritative metadata *after* the persisted state so old values
        # cannot overwrite the current release stamp.
        self._write(
            self.state_file,
            pd.DataFrame([{**json_safe(state), "updated_at": now(), "optimizer_version": self.VERSION}]),
        )

    def _read_memory(self) -> pd.DataFrame:
        return self._read(self.memory_file)

    def _write_memory(self, memory: pd.DataFrame) -> None:
        self._write(self.memory_file, memory)

    def _event(self, payload: dict[str, Any]) -> None:
        row = {"timestamp": now(), "optimizer_version": self.VERSION, **json_safe(payload)}
        last_error: PermissionError | None = None
        for attempt in range(8):
            try:
                append_excel_atomic(self.event_file, [row])
                last_error = None
                break
            except PermissionError as exc:
                last_error = exc
                if attempt < 7:
                    time.sleep(0.10 * (attempt + 1))
        if last_error is not None:
            raise last_error
        # V14.4.1: _event() is the single authoritative writer for
        # v14_optimizer_events.xlsx.  Do not delegate the same row to the
        # optional history manager because it targets the same workbook.

    def attach_services(self, **services: Any) -> None:
        """Dependency injection from the V14 pipeline; all services are optional."""
        self.services.update({name: value for name, value in services.items() if value is not None})

    @property
    def state_manager(self):
        return self.services.get("state_manager")

    @property
    def step_controller(self):
        return self.services.get("step_controller")

    @property
    def pair_optimizer(self):
        return self.services.get("pair_optimizer")

    @property
    def strategy_manager(self):
        return self.services.get("strategy_manager")

    # ------------------------------------------------------------------
    # Parameter, memory, and campaign state preparation
    # ------------------------------------------------------------------

    def _params(self, *, include_nonactive: bool = False) -> pd.DataFrame:
        params = self.config.parameters().copy()
        params = params.dropna(subset=["parameter", "value"]).copy()
        if "status" not in params.columns:
            params["status"] = "active"
        params["status"] = params["status"].astype(str).str.strip().str.casefold()
        params = assign_groups(params, self.config)
        params = params[params["v13_group_enabled"]].copy()
        if not include_nonactive:
            params = params[params["status"].isin(VALID_USER_ACTIVE)].copy()
        return params

    def _ensure_memory(self) -> pd.DataFrame:
        memory = self._read_memory()
        all_params = self._params(include_nonactive=True)
        if self.state_manager is not None:
            try:
                state = self._read_state()
                self.state_manager.sync_with_parameters(all_params, iteration=int(as_float(state.get("iteration"), 0) or 0))
            except Exception as exc:
                self._event({"action": "state_manager_sync_warning", "reason": f"{type(exc).__name__}: {exc}"})

        if memory.empty:
            memory = pd.DataFrame()
        required_columns = {
            "parameter": "",
            "group": "other",
            "group_order": 999,
            "priority": 1000,
            "user_status": "active",
            "status": "unexplored",
            "phase": "symmetric",
            "pass_number": 1,
            # Counts completed failed direction families since the most recent
            # accepted/reopened baseline.  It is used only to schedule local
            # refinement rounds; it never weakens directional safety.
            "refinement_round": 0,
            # V14.4.2: number of reduced-step symmetric refinement pairs that
            # are allowed to bypass the ordinary stage-wide largest-step
            # ordering.  A bracket created after an accepted improvement earns
            # one protected refinement pair.
            "priority_refinement_rounds_remaining": 0,
            # V14.3.3 local-futility evidence is independent of user status and
            # is retained only until an accepted/reopened baseline resets it.
            "futility_non_neutral_failed_pair_count": 0,
            "futility_pair_objective_rejection_count": 0,
            "futility_pair_blocked": False,
            "futility_history_hydrated": False,
            "local_futility_reason": "",
            "step_fraction": self.settings.initial_step_fraction,
            "move_space": "",
            # V14.4.4 initial-search audit metadata. These describe only the
            # starting scale; current adaptive step_fraction remains authoritative.
            "initial_search_mode": "automatic_fallback",
            "initial_search_source": "automatic_global_fallback",
            "initial_factor_requested": None,
            "initial_range_fraction_requested": None,
            "initial_step_fraction_resolved": self.settings.initial_step_fraction,
            "initial_factor_effective": None,
            "direction_queue": "increase|decrease",
            "continuation_direction": "",
            "tested_increase": False,
            "tested_decrease": False,
            "pending": False,
            "pending_candidate_id": "",
            "pending_direction": "",
            "pending_step_fraction": None,
            "pending_old_value": None,
            "pending_new_value": None,
            "pending_factor_applied": None,
            "pending_parent_best_run_folder": "",
            "last_completion_at": "",
            "reopen_count": 0,
            "reopened_after_parameter": "",
            "invalid_count": 0,
            "accepted_improvement_count": 0,
            "sensitivity_score": None,
            "last_update": now(),
        }
        priority_refinement_column_missing = "priority_refinement_rounds_remaining" not in memory.columns
        for column, default in required_columns.items():
            if column not in memory.columns:
                memory[column] = default
        # Existing V14 campaigns created before round-based refinement did not
        # have this column.  Missing values are safely treated as round zero;
        # current step size remains the primary migration-safe ordering key.
        memory["refinement_round"] = pd.to_numeric(
            memory["refinement_round"], errors="coerce"
        ).fillna(0).astype(int)
        memory["priority_refinement_rounds_remaining"] = pd.to_numeric(
            memory["priority_refinement_rounds_remaining"], errors="coerce"
        ).fillna(0).astype(int).clip(lower=0)
        # Migration from V14.4.1: a parameter already sitting in a reduced-step
        # symmetric refinement after at least one accepted improvement is the
        # exact state V14.4.2 is intended to protect.  Grant one immediate
        # refinement pair once when the new column is first introduced.
        migrated_priority_parameters: list[str] = []
        if priority_refinement_column_missing and not memory.empty:
            migration_mask = (
                memory["status"].astype(str).eq("refine_symmetric")
                & memory["refinement_round"].gt(0)
                & pd.to_numeric(memory.get("accepted_improvement_count"), errors="coerce").fillna(0).gt(0)
            )
            memory.loc[migration_mask, "priority_refinement_rounds_remaining"] = 1
            migrated_priority_parameters = memory.loc[migration_mask, "parameter"].astype(str).tolist()
        for column in [
            "futility_non_neutral_failed_pair_count",
            "futility_pair_objective_rejection_count",
        ]:
            memory[column] = pd.to_numeric(memory[column], errors="coerce").fillna(0).astype(int)
        for column in ["futility_pair_blocked", "futility_history_hydrated"]:
            memory[column] = memory[column].map(as_bool)
        # Empty Excel-origin columns with only None values are inferred as
        # float64 by pandas.  These fields later receive identifiers and labels,
        # so force object dtype before candidate metadata are assigned.
        for column in [
            "parameter", "group", "user_status", "status", "phase", "move_space",
            "initial_search_mode", "initial_search_source",
            "direction_queue", "continuation_direction", "pending_candidate_id",
            "pending_direction", "pending_parent_best_run_folder", "last_completion_at",
            "reopened_after_parameter", "local_futility_reason", "last_update",
        ]:
            memory[column] = memory[column].astype(object)

        known = {normalise_parameter(value) for value in memory["parameter"].astype(str).tolist()}
        additions: list[dict[str, Any]] = []
        for _, p in all_params.iterrows():
            parameter = str(p["parameter"]).strip()
            key = normalise_parameter(parameter)
            if not parameter or key in known:
                continue
            step = self.settings.initial_step_fraction
            move_space = "relative"
            initial_meta = {}
            if self.step_controller is not None:
                record = self.step_controller.sync_initial_scale_if_pristine(
                    parameter,
                    value=as_float(p.get("value")),
                    lower=as_float(p.get("min")),
                    upper=as_float(p.get("max")),
                    group=str(p.get("v13_group", "")),
                )
                step = float(record.get("current_step_fraction", step))
                move_space = str(record.get("move_space", move_space))
                initial_meta = record
            additions.append({
                "parameter": parameter,
                "group": str(p["v13_group"]),
                "group_order": int(p["v13_group_order"]),
                "priority": int(p["v13_priority"]),
                "user_status": str(p.get("status", "active")),
                "status": "unexplored",
                "phase": "symmetric",
                "pass_number": 1,
                "refinement_round": 0,
                "priority_refinement_rounds_remaining": 0,
                "futility_non_neutral_failed_pair_count": 0,
                "futility_pair_objective_rejection_count": 0,
                "futility_pair_blocked": False,
                "futility_history_hydrated": False,
                "local_futility_reason": "",
                "step_fraction": step,
                "move_space": move_space,
                "initial_search_mode": initial_meta.get("initial_search_mode", "automatic_fallback"),
                "initial_search_source": initial_meta.get("initial_search_source", "automatic_global_fallback"),
                "initial_factor_requested": initial_meta.get("initial_factor_requested"),
                "initial_range_fraction_requested": initial_meta.get("initial_range_fraction_requested"),
                "initial_step_fraction_resolved": initial_meta.get("initial_step_fraction_resolved", step),
                "initial_factor_effective": initial_meta.get("initial_factor_effective"),
                "direction_queue": "increase|decrease",
                "continuation_direction": "",
                "tested_increase": False,
                "tested_decrease": False,
                "pending": False,
                "pending_candidate_id": "",
                "pending_direction": "",
                "pending_step_fraction": None,
                "pending_old_value": None,
                "pending_new_value": None,
                "pending_factor_applied": None,
                "pending_parent_best_run_folder": "",
                "last_completion_at": "",
                "reopen_count": 0,
                "reopened_after_parameter": "",
                "invalid_count": 0,
                "accepted_improvement_count": 0,
                "sensitivity_score": None,
                "last_update": now(),
            })
        if additions:
            with warnings.catch_warnings():
                warnings.filterwarnings(
                    "ignore",
                    message="The behavior of DataFrame concatenation with empty or all-NA entries is deprecated.*",
                    category=FutureWarning,
                )
                memory = pd.concat([memory, pd.DataFrame(additions)], ignore_index=True)
        # Synchronise live group/priority/user status without changing search state.
        lookup = all_params.copy()
        lookup["_key"] = lookup["parameter"].map(normalise_parameter)
        lookup = lookup.set_index("_key")
        for idx, row in memory.iterrows():
            key = normalise_parameter(row.get("parameter"))
            if key not in lookup.index:
                memory.at[idx, "status"] = "inactive"
                continue
            p = lookup.loc[key]
            memory.at[idx, "group"] = str(p["v13_group"])
            memory.at[idx, "group_order"] = int(p["v13_group_order"])
            memory.at[idx, "priority"] = int(p["v13_priority"])
            memory.at[idx, "user_status"] = str(p.get("status", "active"))
            if self.step_controller is not None:
                never_tested = (
                    str(memory.at[idx, "status"]) == "unexplored"
                    and str(memory.at[idx, "phase"]) == "symmetric"
                    and not as_bool(memory.at[idx, "tested_increase"], False)
                    and not as_bool(memory.at[idx, "tested_decrease"], False)
                    and not as_bool(memory.at[idx, "pending"], False)
                    and int(as_float(memory.at[idx, "refinement_round"], 0) or 0) == 0
                    and int(as_float(memory.at[idx, "accepted_improvement_count"], 0) or 0) == 0
                    and int(as_float(memory.at[idx, "reopen_count"], 0) or 0) == 0
                )
                if never_tested:
                    record = self.step_controller.sync_initial_scale_if_pristine(
                        str(row["parameter"]),
                        value=as_float(p.get("value")),
                        lower=as_float(p.get("min")),
                        upper=as_float(p.get("max")),
                        group=str(p.get("v13_group", "")),
                    )
                else:
                    record = self.step_controller.ensure_parameter(
                        str(row["parameter"]),
                        as_float(p.get("value")),
                        lower=as_float(p.get("min")),
                        upper=as_float(p.get("max")),
                        group=str(p.get("v13_group", "")),
                    )
                memory.at[idx, "step_fraction"] = float(record.get("current_step_fraction", self.settings.initial_step_fraction))
                memory.at[idx, "move_space"] = str(record.get("move_space", "relative"))
                memory.at[idx, "initial_search_mode"] = str(record.get("initial_search_mode", "automatic_fallback"))
                memory.at[idx, "initial_search_source"] = str(record.get("initial_search_source", "automatic_global_fallback"))
                memory.at[idx, "initial_factor_requested"] = record.get("initial_factor_requested")
                memory.at[idx, "initial_range_fraction_requested"] = record.get("initial_range_fraction_requested")
                memory.at[idx, "initial_step_fraction_resolved"] = record.get("initial_step_fraction_resolved", record.get("initial_step_fraction"))
                memory.at[idx, "initial_factor_effective"] = record.get("initial_factor_effective")
        self._write_memory(memory)
        if migrated_priority_parameters:
            self._event({
                "action": "v14_4_2_priority_refinement_migrated",
                "parameters": "|".join(migrated_priority_parameters),
                "priority_refinement_rounds_granted": 1,
            })
        return memory

    def initialize(self, total_score: float, run_folder: str, baseline_source: str = "run_ranking.xlsx") -> dict[str, Any]:
        existing = self._read_state()
        if as_float(existing.get("current_best_score")) is not None:
            return existing
        params = self._params()
        groups = ordered_groups(params)
        state = {
            "campaign_id": uuid.uuid4().hex[:12],
            "current_best_score": float(total_score),
            "current_best_run_folder": str(run_folder),
            "baseline_source": baseline_source,
            "current_stage": groups[0] if groups else "other",
            "stage_index": 0,
            "pass_number": 1,
            "iteration": 0,
            "valid_run_count": 0,
            "accepted_improvements_count": 0,
            "pass_accepted_improvements_count": 0,
            "last_completed_pass_number": 0,
            "last_completed_pass_accepted_improvements_count": 0,
            "pass_control_migration_note": "",
            "rejected_candidates_count": 0,
            "invalid_candidates_count": 0,
            "reopened_parameters_count": 0,
            "convergence_status": "running",
            "convergence_reason": "",
            "last_action": "initialized",
            "last_parameter": "",
            "last_direction": "",
            "last_step_fraction": None,
            "last_diagnostics": {},
            "last_event_accepted": False,
            "pending_candidate_id": "",
            "pending_candidate_type": "",
            "pending_pair_id": "",
            "pending_pair_context": {},
        }
        self._ensure_memory()
        self._write_state(state)
        self._event({"action": "initialized", "TOTAL_SCORE": total_score, "run_folder": run_folder, "stage": state["current_stage"], "baseline_source": baseline_source})
        return state

    def current_best_score(self) -> float | None:
        return as_float(self._read_state().get("current_best_score"))

    def record_invocation_summary(self, summary: dict[str, Any]) -> dict[str, Any]:
        """Persist per-invocation CLI/stop accounting without changing search logic."""
        state = self._read_state()
        state.update(json_safe(summary or {}))
        self._write_state(state)
        return state

    def pass_control_status(self) -> dict[str, Any]:
        """Return a read-only summary of V14.4 second-pass eligibility."""
        state = self._read_state()
        tracked = as_float(state.get("pass_accepted_improvements_count"))
        current_pass = max(int(as_float(state.get("pass_number"), 1) or 1), 1)
        count = None if tracked is None else max(int(tracked), 0)
        next_pass = current_pass + 1
        hard_cap_reached = next_pass > self.settings.max_passes
        improvement_gate_satisfied = (
            count is not None and count > 0
        ) or not self.settings.require_accepted_improvement_for_additional_pass
        return {
            "optimizer_version": self.VERSION,
            "max_passes": int(self.settings.max_passes),
            "require_accepted_improvement_for_additional_pass": bool(
                self.settings.require_accepted_improvement_for_additional_pass
            ),
            "current_pass": current_pass,
            "pass_accepted_improvements_count": count,
            "pass_control_migration_required": count is None,
            "next_pass": next_pass,
            "hard_cap_reached": hard_cap_reached,
            "improvement_gate_satisfied": improvement_gate_satisfied,
            "next_pass_allowed_if_current_pass_ends_now": bool(
                not hard_cap_reached and improvement_gate_satisfied
            ),
        }

    def synchronize_pass_control(self) -> dict[str, Any]:
        """Persist migration-safe pass accounting without running MIN3P.

        This touches only the optimizer state/audit files.  It never changes
        agent_config.xlsx, best_parameters_V14.xlsx, parameter runtime state,
        or the current best score.
        """
        state = self._read_state()
        migrated = self._ensure_pass_control_state(state)
        if migrated:
            self._write_state(state)
        return {"state_migrated": migrated, **self.pass_control_status()}

    # ------------------------------------------------------------------
    # Selection order and V13.5 directional safety invariants
    # ------------------------------------------------------------------

    def _direction_family_pending(self, row: pd.Series) -> bool:
        status = str(row.get("status", ""))
        phase = str(row.get("phase", ""))
        queued = _queue(row.get("direction_queue"))
        return (
            status in {"test_remaining_direction", "test_opposite_after_directional_failure", "continue_increase", "continue_decrease"}
            or phase in {"directional_continue", "directional_opposite"}
            or (phase == "symmetric" and len(queued) == 1 and as_bool(row.get("tested_increase")) != as_bool(row.get("tested_decrease")))
        )

    def _is_selectable(self, row: pd.Series) -> bool:
        if str(row.get("status", "")) in {"pass_complete", "locally_complete", "inactive"}:
            return False
        if as_bool(row.get("pending"), False):
            return False
        parameter = str(row.get("parameter", ""))
        if self.state_manager is not None:
            # Incomplete pair direction must not be blocked by an automatic
            # temporary state; user locks remain absolute.
            return self.state_manager.is_eligible(parameter, ignore_runtime_freeze=self._direction_family_pending(row))
        return str(row.get("user_status", "active")).casefold() in VALID_USER_ACTIVE

    def _ensure_pass_control_state(self, state: dict[str, Any]) -> bool:
        """Migrate legacy campaign state to explicit per-pass acceptance accounting.

        V14.3.3 states contain only the all-time acceptance count.  For a legacy
        first pass that total is exact, so it can be retained safely.  For a
        legacy later pass, historical pass attribution is unavailable; V14.3.4
        deliberately seeds zero and requires a new accepted change before any
        *further* pass can begin.  This conservative migration cannot create an
        accidental repeat pass or modify canonical configuration files.
        """
        existing = as_float(state.get("pass_accepted_improvements_count"))
        if existing is not None:
            state["pass_accepted_improvements_count"] = max(int(existing), 0)
            return False

        current_pass = max(int(as_float(state.get("pass_number"), 1) or 1), 1)
        total_accepted = max(int(as_float(state.get("accepted_improvements_count"), 0) or 0), 0)
        seeded = total_accepted if current_pass == 1 else 0
        note = (
            "migrated_from_total_accepted_count_for_pass_1"
            if current_pass == 1
            else "legacy_later_pass_seeded_zero_requires_post_patch_accept"
        )
        state.update({
            "pass_accepted_improvements_count": seeded,
            "last_completed_pass_number": int(as_float(state.get("last_completed_pass_number"), 0) or 0),
            "last_completed_pass_accepted_improvements_count": int(
                as_float(state.get("last_completed_pass_accepted_improvements_count"), 0) or 0
            ),
            "pass_control_migration_note": note,
        })
        self._event({
            "action": "pass_control_state_migrated",
            "current_pass": current_pass,
            "seeded_pass_accepted_improvements_count": seeded,
            "reason": note,
        })
        return True

    def _start_next_pass(self, memory: pd.DataFrame, state: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any], bool]:
        self._ensure_pass_control_state(state)
        current_pass = max(int(as_float(state.get("pass_number"), 1) or 1), 1)
        completed_pass_accepts = max(
            int(as_float(state.get("pass_accepted_improvements_count"), 0) or 0),
            0,
        )
        next_pass = current_pass + 1

        # Hard cap takes precedence even if the just-finished pass improved.
        # Crucially, neither branch below resets parameter states.
        if next_pass > self.settings.max_passes:
            reason = "max_passes_hard_cap_reached"
            state.update({
                "convergence_status": "converged",
                "convergence_reason": reason,
                "last_action": "scientific_convergence",
                "last_completed_pass_number": current_pass,
                "last_completed_pass_accepted_improvements_count": completed_pass_accepts,
            })
            self._event({
                "action": "scientific_convergence",
                "reason": reason,
                "completed_pass": current_pass,
                "completed_pass_accepted_improvements_count": completed_pass_accepts,
                "max_passes": self.settings.max_passes,
            })
            return memory, state, False

        # V14.3.4 release guard: an unproductive pass may not trigger broad
        # retesting merely because max_passes permits it.
        if (
            self.settings.require_accepted_improvement_for_additional_pass
            and completed_pass_accepts <= 0
        ):
            reason = (
                "first_pass_complete_without_accepted_improvement"
                if current_pass == 1
                else "pass_complete_without_accepted_improvement"
            )
            state.update({
                "convergence_status": "converged",
                "convergence_reason": reason,
                "last_action": "scientific_convergence",
                "last_completed_pass_number": current_pass,
                "last_completed_pass_accepted_improvements_count": completed_pass_accepts,
            })
            self._event({
                "action": "scientific_convergence",
                "reason": reason,
                "completed_pass": current_pass,
                "completed_pass_accepted_improvements_count": completed_pass_accepts,
                "next_pass": next_pass,
                "max_passes": self.settings.max_passes,
                "parameter_state_reset": False,
            })
            return memory, state, False

        groups = ordered_groups(self._params())
        reset = memory["status"].astype(str).isin({"pass_complete", "bounded", "inactive"})
        # Preserve actual user locks; only reset parameters that are currently
        # eligible in user configuration.
        for idx in memory.index[reset]:
            parameter = str(memory.at[idx, "parameter"])
            user_eligible = self.state_manager is None or not self.state_manager.is_user_locked(parameter)
            if not user_eligible:
                continue
            memory.at[idx, "status"] = "unexplored"
            memory.at[idx, "phase"] = "symmetric"
            memory.at[idx, "refinement_round"] = 0
            memory.at[idx, "priority_refinement_rounds_remaining"] = 0
            self._reset_local_futility_evidence(memory, idx)
            memory.at[idx, "direction_queue"] = "increase|decrease"
            memory.at[idx, "tested_increase"] = False
            memory.at[idx, "tested_decrease"] = False
        state.update({
            "pass_number": next_pass,
            "stage_index": 0,
            "current_stage": groups[0] if groups else "other",
            "last_action": "start_next_pass",
            "last_completed_pass_number": current_pass,
            "last_completed_pass_accepted_improvements_count": completed_pass_accepts,
            "pass_accepted_improvements_count": 0,
        })
        self._event({
            "action": "start_next_pass",
            "pass_number": next_pass,
            "preceding_pass": current_pass,
            "preceding_pass_accepted_improvements_count": completed_pass_accepts,
        })
        return memory, state, True

    def _advance_stage(self, memory: pd.DataFrame, state: dict[str, Any]) -> tuple[pd.DataFrame, dict[str, Any], bool]:
        groups = ordered_groups(self._params())
        stage = str(state.get("current_stage", "other"))
        index = groups.index(stage) if stage in groups else -1
        if index + 1 < len(groups):
            state.update({"current_stage": groups[index + 1], "stage_index": index + 1, "last_action": "advance_stage"})
            self._event({"action": "advance_stage", "stage": state["current_stage"]})
            return memory, state, True
        return self._start_next_pass(memory, state)

    def _candidate_index(self, memory: pd.DataFrame, state: dict[str, Any]) -> tuple[int | None, pd.DataFrame, dict[str, Any]]:
        available = memory[memory.apply(self._is_selectable, axis=1)].copy()
        if available.empty:
            memory, state, keep = self._advance_stage(memory, state)
            return (None, memory, state) if not keep else self._candidate_index(memory, state)

        # Priority 1 (hard invariant): no different parameter can interrupt an
        # incomplete individual direction family, regardless of group.
        pending_family = available[available.apply(self._direction_family_pending, axis=1)].copy()
        if not pending_family.empty:
            pending_family["_sens"] = pd.to_numeric(
                pending_family.get("sensitivity_score"), errors="coerce"
            ).fillna(-1.0)
            selected = pending_family.sort_values(
                ["last_update", "priority", "_sens", "parameter"],
                ascending=[True, True, False, True],
            ).index[0]
            return int(selected), memory, state

        # Priority 2 (V14.4.2): after an accepted improvement has been bracketed
        # by failed continuation/opposite tests, immediately execute one full
        # reduced-step symmetric refinement pair before moving to an unrelated
        # parameter.  This is deliberately narrower than making *all* reduced
        # steps globally dominant; ordinary refinements still obey the proven
        # stage-wide largest-step ordering once the one-pair grant is consumed.
        priority_refinement = available[
            available["status"].astype(str).eq("refine_symmetric")
            & pd.to_numeric(
                available.get(
                    "priority_refinement_rounds_remaining",
                    pd.Series(0, index=available.index, dtype=float),
                ),
                errors="coerce",
            ).fillna(0).gt(0)
        ].copy()
        if not priority_refinement.empty:
            priority_refinement["_sens"] = pd.to_numeric(
                priority_refinement.get("sensitivity_score"), errors="coerce"
            ).fillna(-1.0)
            selected = priority_refinement.sort_values(
                ["priority", "_sens", "last_update", "parameter"],
                ascending=[True, False, True, True],
            ).index[0]
            return int(selected), memory, state

        stage = str(state.get("current_stage", "other"))
        candidates = available[available["group"].astype(str).eq(stage)].copy()
        if candidates.empty:
            memory, state, keep = self._advance_stage(memory, state)
            return (None, memory, state) if not keep else self._candidate_index(memory, state)

        # V14.3.1 regression repair: preserve the verified stage-wide
        # global-step ordering.  Local-futility changes only eligibility;
        # it must never let a smaller refinement bypass an untouched larger
        # step in the same stage.
        # Priority 3: one stage-wide step-size round across all ready
        # single-parameter candidates.
        #
        # "unexplored", "reopened", and "refine_symmetric" parameters share
        # one queue.  The largest current step is always selected first, so an
        # untouched parameter at +/-5% cannot be bypassed by another parameter
        # already refining at +/-2.5% or +/-1.25%.  This fixes the case where
        # vg_alpha could otherwise monopolise the hydraulic stage before vg_l
        # and vg_n received their first +/-5% tests.
        #
        # Direction families are already handled above.  GPT may only break a
        # tie within the same legal step-size round; it can never promote a
        # smaller-step candidate over a larger-step candidate.
        ready_statuses = {"unexplored", "reopened", "refine_symmetric"}
        ready = candidates[candidates["status"].astype(str).isin(ready_statuses)].copy()
        if not ready.empty:
            ready["_step"] = pd.to_numeric(
                ready.get("step_fraction"), errors="coerce"
            ).fillna(self.settings.minimum_step_fraction)
            ready["_round"] = pd.to_numeric(
                ready.get("refinement_round"), errors="coerce"
            ).fillna(0).astype(int)
            ready["_sens"] = pd.to_numeric(
                ready.get("sensitivity_score"), errors="coerce"
            ).fillna(-1.0)
            ready["_gpt_rank"] = 1

            advice = self._gpt_advisory or {}
            suggested = (
                normalise_parameter(advice.get("suggested_parameter", ""))
                if isinstance(advice, dict)
                else ""
            )
            confidence = (
                as_float(advice.get("confidence"), 0.0)
                if isinstance(advice, dict)
                else 0.0
            )
            if (
                suggested
                and confidence is not None
                and confidence >= self.settings.gpt_min_confidence
            ):
                ready.loc[
                    ready["parameter"].map(normalise_parameter).eq(suggested),
                    "_gpt_rank",
                ] = 0

            selected = ready.sort_values(
                ["_step", "_gpt_rank", "priority", "_round", "_sens", "last_update", "parameter"],
                ascending=[False, True, True, True, False, True, True],
            ).index[0]
            return int(selected), memory, state

        # Fallback for any future ready state not explicitly listed above.
        # GPT remains advisory only inside this already legal fallback tier.
        candidates["_sens"] = pd.to_numeric(
            candidates.get("sensitivity_score"), errors="coerce"
        ).fillna(-1.0)
        candidates["_gpt_rank"] = 1
        advice = self._gpt_advisory or {}
        suggested = (
            normalise_parameter(advice.get("suggested_parameter", ""))
            if isinstance(advice, dict)
            else ""
        )
        confidence = (
            as_float(advice.get("confidence"), 0.0)
            if isinstance(advice, dict)
            else 0.0
        )
        if (
            suggested
            and confidence is not None
            and confidence >= self.settings.gpt_min_confidence
        ):
            candidates.loc[
                candidates["parameter"].map(normalise_parameter).eq(suggested),
                "_gpt_rank",
            ] = 0
        selected = candidates.sort_values(
            ["_gpt_rank", "priority", "_sens", "last_update", "parameter"],
            ascending=[True, True, False, True, True],
        ).index[0]
        return int(selected), memory, state


    def _local_futility_applies(self, row: pd.Series) -> bool:
        """Apply auto-completion only to explicitly validated independent groups."""
        group = str(row.get("group", "")).strip().casefold()
        return bool(self.settings.local_futility_enabled) and (
            group in set(self.settings.local_futility_groups)
        )

    def _local_futility_scope_label(self) -> str:
        """Compact deterministic scope label for JSON/XLSX audit records."""
        return "|".join(self.settings.local_futility_groups)

    @staticmethod
    def _as_direction_set(frame: pd.DataFrame) -> set[str]:
        if "direction" not in frame.columns:
            return set()
        return {
            str(value).strip().casefold()
            for value in frame["direction"].tolist()
            if str(value).strip().casefold() in {"increase", "decrease"}
        }

    def _reset_local_futility_evidence(self, memory: pd.DataFrame, idx: int) -> None:
        """Clear local evidence after an accepted/reopened baseline change."""
        memory.at[idx, "futility_non_neutral_failed_pair_count"] = 0
        memory.at[idx, "futility_pair_objective_rejection_count"] = 0
        memory.at[idx, "futility_pair_blocked"] = False
        memory.at[idx, "futility_history_hydrated"] = True
        memory.at[idx, "local_futility_reason"] = ""

    def _historical_local_futility_evidence(self, parameter: str) -> dict[str, Any]:
        """Recover local-futility evidence from finalized V14 decisions.

        V14.3.2 counts *any* two distinct complete, valid, non-neutral,
        objective-rejected symmetric pairs after the most recent accepted
        baseline.  An invalid pair remains numerical-stability evidence but
        does not permanently block completion when two later valid pairs exist.
        Near-neutral and scientific-constraint pairs remain conservative hard
        blocks because they do not establish local objective futility.
        """
        result: dict[str, Any] = {
            "parameter": parameter,
            "completed_pair_steps": [],
            "non_neutral_failed_pair_count": 0,
            "blocked": False,
            "invalid_pair_steps": [],
            "near_neutral_or_constraint_pair_steps": [],
            "reason": "no_history",
        }
        history = self._read(self.decision_file)
        if history.empty or "parameter" not in history.columns:
            return result
        rows = history[
            history["parameter"].astype(str).map(normalise_parameter)
            .eq(normalise_parameter(parameter))
        ].copy()
        if rows.empty:
            return result
        if "candidate_type" in rows.columns:
            rows = rows[rows["candidate_type"].astype(str).eq("single_parameter")]
        if rows.empty:
            return result

        # A newly accepted candidate establishes a fresh local baseline.
        if "accepted" in rows.columns:
            accepted_positions = [
                pos for pos, value in enumerate(rows["accepted"].tolist()) if as_bool(value)
            ]
            if accepted_positions:
                rows = rows.iloc[accepted_positions[-1] + 1 :].copy()
        if rows.empty or "step_fraction" not in rows.columns:
            return result

        rows["_step"] = pd.to_numeric(rows["step_fraction"], errors="coerce")
        rows = rows[rows["_step"].notna()].copy()
        if rows.empty:
            return result

        valid_steps: list[float] = []
        invalid_steps: list[float] = []
        hard_block_steps: list[float] = []
        steps = sorted({float(value) for value in rows["_step"].tolist()}, reverse=True)
        for step in steps:
            pair = rows[(rows["_step"] - step).abs() <= 1e-12].copy()
            if self._as_direction_set(pair) != {"increase", "decrease"}:
                continue

            decisions = pair.get("decision", pd.Series("", index=pair.index)).astype(str).str.casefold()
            decision_ok = decisions.eq("reject").all()
            reasons = pair.get("rejection_reason", pd.Series("", index=pair.index)).astype(str)
            reason_folded = reasons.str.casefold()
            if "valid_run" in pair.columns:
                valid_ok = pair["valid_run"].map(as_bool).all()
            else:
                valid_ok = not reason_folded.str.contains("invalid_or_failed_min3p_run", na=False).any()

            invalid_pair = (
                (not valid_ok)
                or reason_folded.str.contains("invalid_or_failed_min3p_run", na=False).any()
            )
            near_neutral_or_constraint = (
                reason_folded.str.contains("near_neutral", na=False).any()
                or reason_folded.str.contains("scientific_constraint", na=False).any()
            )
            objective_pair = bool(
                decision_ok
                and valid_ok
                and reasons.eq("TOTAL_SCORE_not_meaningfully_improved").all()
                and not near_neutral_or_constraint
            )

            if objective_pair:
                valid_steps.append(step)
            elif invalid_pair:
                invalid_steps.append(step)
            else:
                # Constraint, neutral, or an unrecognised non-objective outcome
                # must remain a conservative block for automatic completion.
                hard_block_steps.append(step)

        required = int(self.settings.local_futility_min_non_neutral_failed_pairs)
        selected_steps = valid_steps[:required]
        result["completed_pair_steps"] = selected_steps
        result["invalid_pair_steps"] = invalid_steps
        result["near_neutral_or_constraint_pair_steps"] = hard_block_steps
        result["non_neutral_failed_pair_count"] = min(len(valid_steps), required)
        result["blocked"] = bool(hard_block_steps)

        if hard_block_steps:
            result["reason"] = "historical_near_neutral_or_constraint_pair"
        elif len(valid_steps) >= required:
            if invalid_steps:
                result["reason"] = "historical_valid_pairs_after_invalid_trial"
            else:
                result["reason"] = "historical_completed_non_neutral_rejected_pairs"
        elif invalid_steps:
            result["reason"] = "historical_insufficient_valid_pairs_after_invalid_trial"
        else:
            result["reason"] = "historical_pair_not_eligible"
        return result

    def _hydrate_local_futility_evidence(
        self,
        memory: pd.DataFrame,
        *,
        force_history_refresh: bool = False,
    ) -> None:
        """Synchronize futility evidence from finalized decision history.

        ``force_history_refresh`` is used by the V14.3.2 manual/startup gate so
        campaigns already hydrated by V14.3 are reinterpreted under the repaired
        valid-pair aggregation rule.
        """
        for idx, row in memory.iterrows():
            if not self._local_futility_applies(row):
                continue
            if as_bool(row.get("futility_history_hydrated")) and not force_history_refresh:
                continue
            evidence = self._historical_local_futility_evidence(str(row.get("parameter", "")))
            memory.at[idx, "futility_non_neutral_failed_pair_count"] = int(
                evidence.get("non_neutral_failed_pair_count", 0) or 0
            )
            memory.at[idx, "futility_history_hydrated"] = True
            memory.at[idx, "futility_pair_blocked"] = bool(evidence.get("blocked"))
            if evidence.get("reason") not in {"", "no_history"}:
                details = []
                if evidence.get("completed_pair_steps"):
                    details.append(
                        "valid_steps=" + "|".join(
                            f"{float(step):.6g}" for step in evidence["completed_pair_steps"]
                        )
                    )
                if evidence.get("invalid_pair_steps"):
                    details.append(
                        "invalid_steps=" + "|".join(
                            f"{float(step):.6g}" for step in evidence["invalid_pair_steps"]
                        )
                    )
                if evidence.get("near_neutral_or_constraint_pair_steps"):
                    details.append(
                        "blocked_steps=" + "|".join(
                            f"{float(step):.6g}" for step in evidence["near_neutral_or_constraint_pair_steps"]
                        )
                    )
                suffix = "; " + "; ".join(details) if details else ""
                memory.at[idx, "local_futility_reason"] = str(evidence.get("reason")) + suffix

    def _mark_locally_complete(
        self,
        memory: pd.DataFrame,
        idx: int,
        state: dict[str, Any],
        *,
        source: str,
    ) -> bool:
        row = memory.loc[idx]
        if not self._local_futility_applies(row):
            return False
        if as_bool(row.get("pending")) or self._direction_family_pending(row):
            return False
        required = int(self.settings.local_futility_min_non_neutral_failed_pairs)
        count = int(as_float(row.get("futility_non_neutral_failed_pair_count"), 0) or 0)
        if count < required or as_bool(row.get("futility_pair_blocked")):
            return False
        parameter = str(row.get("parameter", ""))
        evidence_note = str(row.get("local_futility_reason", "")).strip()
        reason = (
            f"local_futility: {count} completed non-neutral rejected symmetric pairs "
            f"in group={row.get('group', '')}; source={source}"
        )
        if evidence_note:
            reason = f"{reason}; evidence={evidence_note}"
        memory.at[idx, "status"] = "locally_complete"
        memory.at[idx, "phase"] = "local_futility_complete"
        memory.at[idx, "direction_queue"] = ""
        memory.at[idx, "continuation_direction"] = ""
        memory.at[idx, "tested_increase"] = True
        memory.at[idx, "tested_decrease"] = True
        memory.at[idx, "priority_refinement_rounds_remaining"] = 0
        memory.at[idx, "last_completion_at"] = now()
        memory.at[idx, "last_update"] = now()
        memory.at[idx, "local_futility_reason"] = reason
        if self.state_manager is not None:
            self.state_manager.mark_temporarily_exhausted(
                parameter,
                iteration=int(as_float(state.get("iteration"), 0) or 0),
                valid_run_count=int(as_float(state.get("valid_run_count"), 0) or 0),
                reason="local_futility_gate",
                cooldown_valid_runs=self.settings.exhausted_cooldown_valid_runs,
            )
        self._event({
            "action": "local_futility_complete",
            "parameter": parameter,
            "group": str(row.get("group", "")),
            "non_neutral_failed_pair_count": count,
            "reason": reason,
        })
        return True

    def _apply_local_futility_gate(
        self,
        memory: pd.DataFrame,
        state: dict[str, Any],
        *,
        source: str,
        force_history_refresh: bool = False,
    ) -> list[str]:
        self._hydrate_local_futility_evidence(
            memory,
            force_history_refresh=force_history_refresh,
        )
        completed: list[str] = []
        for idx in memory.index:
            if self._mark_locally_complete(memory, int(idx), state, source=source):
                completed.append(str(memory.at[idx, "parameter"]))
        return completed

    def _advance_completed_stage_if_needed(
        self,
        memory: pd.DataFrame,
        state: dict[str, Any],
    ) -> tuple[pd.DataFrame, dict[str, Any], list[dict[str, str]]]:
        """Advance only across empty stages; never begin a new pass here."""
        transitions: list[dict[str, str]] = []
        groups = ordered_groups(self._params())
        maximum = max(len(groups), 1)
        for _ in range(maximum):
            stage = str(state.get("current_stage", "other"))
            available = memory[
                memory["group"].astype(str).eq(stage)
                & memory.apply(self._is_selectable, axis=1)
            ]
            if not available.empty:
                break
            index = groups.index(stage) if stage in groups else -1
            if index + 1 >= len(groups):
                state.update({
                    "convergence_status": "stage_transition_required",
                    "convergence_reason": (
                        "local futility completed the current stage; "
                        "no later active process group is configured"
                    ),
                    "last_action": "local_futility_stage_transition_required",
                })
                self._event({
                    "action": "local_futility_stage_transition_required",
                    "stage": stage,
                    "reason": state["convergence_reason"],
                })
                break
            next_stage = groups[index + 1]
            state.update({
                "current_stage": next_stage,
                "stage_index": index + 1,
                "last_action": "advance_stage_after_local_futility",
            })
            transitions.append({"from": stage, "to": next_stage})
            self._event({"action": "advance_stage_after_local_futility", "from_stage": stage, "to_stage": next_stage})
        return memory, state, transitions

    def apply_local_futility_gate(self, *, advance_stage: bool = True) -> dict[str, Any]:
        """Apply V14.3.3 local-futility completion without running MIN3P.

        This method modifies only optimizer/runtime state and audit records.  It
        never modifies the canonical configuration or best-parameter workbook.
        """
        state = self._read_state()
        memory = self._ensure_memory()
        completed = self._apply_local_futility_gate(
            memory, state, source="manual_or_startup", force_history_refresh=True
        )
        transitions: list[dict[str, str]] = []
        if advance_stage and completed:
            memory, state, transitions = self._advance_completed_stage_if_needed(memory, state)
        self._write_memory(memory)
        self._write_state(state)
        return {
            "optimizer_version": self.VERSION,
            "local_futility_enabled": bool(self.settings.local_futility_enabled),
            "group_scope": self._local_futility_scope_label(),
            "group_scope_groups": list(self.settings.local_futility_groups),
            "minimum_non_neutral_failed_pairs": int(self.settings.local_futility_min_non_neutral_failed_pairs),
            "locally_completed_parameters": completed,
            "stage_transitions": transitions,
            "current_stage": str(state.get("current_stage", "")),
            "canonical_configuration_modified": False,
        }

    def local_futility_status(self) -> dict[str, Any]:
        """Return a read-only V14.4 preflight summary with true eligibility."""
        memory = self._read_memory()
        if memory.empty:
            return {"available": False, "locally_completed_parameters": [], "pending_candidates": []}
        groups = set(self.settings.local_futility_groups)
        scoped = memory[
            memory.get("group", pd.Series("", index=memory.index))
            .astype(str).str.casefold().isin(groups)
        ].copy()
        completed_mask = scoped.get("status", pd.Series("", index=scoped.index)).astype(str).eq("locally_complete")
        completed = scoped[completed_mask]
        pending = scoped[scoped.get("pending", pd.Series(False, index=scoped.index)).map(as_bool)]
        not_complete = scoped[~completed_mask].copy()
        selectable_mask = not_complete.apply(self._is_selectable, axis=1) if not not_complete.empty else pd.Series(dtype=bool)
        selectable = not_complete.loc[selectable_mask] if not not_complete.empty else not_complete
        unselectable = not_complete.loc[~selectable_mask] if not not_complete.empty else not_complete
        user_status = not_complete.get("user_status", pd.Series("", index=not_complete.index)).astype(str).str.strip().str.casefold()
        user_inactive = not_complete.loc[user_status.eq("inactive")]
        user_frozen = not_complete.loc[user_status.eq("frozen")]
        state = self._read_state()
        return {
            "available": True,
            "group_scope": self._local_futility_scope_label(),
            "group_scope_groups": list(self.settings.local_futility_groups),
            "locally_completed_parameters": completed.get("parameter", pd.Series(dtype=str)).astype(str).tolist(),
            "pending_candidates": pending.get("parameter", pd.Series(dtype=str)).astype(str).tolist(),
            # Backward-compatible field name with corrected semantics: only
            # parameters the runtime may actually select are listed here.
            "eligible_parameters": selectable.get("parameter", pd.Series(dtype=str)).astype(str).tolist(),
            "runtime_selectable_parameters": selectable.get("parameter", pd.Series(dtype=str)).astype(str).tolist(),
            "not_locally_complete_parameters": not_complete.get("parameter", pd.Series(dtype=str)).astype(str).tolist(),
            "user_inactive_parameters": user_inactive.get("parameter", pd.Series(dtype=str)).astype(str).tolist(),
            "user_frozen_parameters": user_frozen.get("parameter", pd.Series(dtype=str)).astype(str).tolist(),
            "runtime_unselectable_parameters": unselectable.get("parameter", pd.Series(dtype=str)).astype(str).tolist(),
            "convergence_status": str(state.get("convergence_status", "")),
            "convergence_reason": str(state.get("convergence_reason", "")),
            "current_stage": str(state.get("current_stage", "")),
        }

    def _finish_or_refine(self, memory: pd.DataFrame, idx: int, state: dict[str, Any], *, reason: str) -> str:
        parameter = str(memory.at[idx, "parameter"])
        row_before = memory.loc[idx].copy()
        priority_rounds_before = int(
            as_float(row_before.get("priority_refinement_rounds_remaining"), 0) or 0
        )
        # Count only a fully completed, symmetric, valid objective-rejection
        # pair.  Invalid, constrained, or near-neutral outcomes are scientific
        # evidence but must never trigger automatic local completion.
        objective_pair = (
            reason == "symmetric_pair_failed"
            and int(as_float(row_before.get("futility_pair_objective_rejection_count"), 0) or 0) == 2
            and not as_bool(row_before.get("futility_pair_blocked"))
        )
        if objective_pair and self._local_futility_applies(row_before):
            memory.at[idx, "futility_non_neutral_failed_pair_count"] = int(
                as_float(row_before.get("futility_non_neutral_failed_pair_count"), 0) or 0
            ) + 1
        # Current-pair flags must never leak into the next step-size round.
        memory.at[idx, "futility_pair_objective_rejection_count"] = 0
        memory.at[idx, "futility_pair_blocked"] = False
        memory.at[idx, "futility_history_hydrated"] = True

        # V14.3.4 is intentionally checked before requesting another smaller
        # local step.  Thus a proven-futile +/-5% and +/-2.5% sequence ends
        # without consuming 1.25%, 0.625%, or 0.5% model runs.
        if self._mark_locally_complete(memory, idx, state, source="completed_symmetric_pair"):
            return "local_futility_complete"

        if self.step_controller is not None:
            record = self.step_controller.finalize_failed_direction_family(parameter)
            step = float(record.get("current_step_fraction", self.settings.minimum_step_fraction))
            minimum = float(record.get("minimum_step_fraction", self.settings.minimum_step_fraction))
        else:
            old_step = as_float(memory.at[idx, "step_fraction"], self.settings.initial_step_fraction) or self.settings.initial_step_fraction
            step = max(old_step * self.settings.step_shrink_factor, self.settings.minimum_step_fraction)
            minimum = self.settings.minimum_step_fraction
        memory.at[idx, "step_fraction"] = step
        if step <= minimum + 1e-15:
            memory.at[idx, "priority_refinement_rounds_remaining"] = 0
            memory.at[idx, "status"] = "pass_complete"
            memory.at[idx, "phase"] = "complete"
            memory.at[idx, "direction_queue"] = ""
            memory.at[idx, "last_completion_at"] = now()
            if self.state_manager is not None:
                self.state_manager.mark_temporarily_exhausted(
                    parameter,
                    iteration=int(as_float(state.get("iteration"), 0) or 0),
                    valid_run_count=int(as_float(state.get("valid_run_count"), 0) or 0),
                    reason=reason,
                    cooldown_valid_runs=self.settings.exhausted_cooldown_valid_runs,
                )
            return "finish_parameter_for_pass"
        memory.at[idx, "phase"] = "symmetric"
        memory.at[idx, "refinement_round"] = int(
            as_float(memory.at[idx, "refinement_round"], 0) or 0
        ) + 1
        memory.at[idx, "direction_queue"] = "increase|decrease"
        memory.at[idx, "tested_increase"] = False
        memory.at[idx, "tested_decrease"] = False
        # V14.4.2 local-refinement priority:
        # - Completing the bracket after an accepted improvement earns one
        #   immediate reduced-step symmetric pair.
        # - Completing that protected symmetric pair consumes the grant.
        # - A later accepted improvement creates a new bracket and earns a new
        #   grant through the directional_pair_failed path.
        if reason == "directional_pair_failed":
            memory.at[idx, "priority_refinement_rounds_remaining"] = max(priority_rounds_before, 1)
        elif reason == "symmetric_pair_failed" and priority_rounds_before > 0:
            memory.at[idx, "priority_refinement_rounds_remaining"] = max(priority_rounds_before - 1, 0)
        else:
            memory.at[idx, "priority_refinement_rounds_remaining"] = priority_rounds_before
        memory.at[idx, "status"] = "refine_symmetric"
        return "shrink_step_after_completed_direction_family"

    def _reopen_after_accept(self, memory: pd.DataFrame, state: dict[str, Any], accepted_parameter: str, accepted_group: str) -> list[str]:
        candidates = memory["status"].astype(str).isin({"pass_complete", "locally_complete"}) & memory["parameter"].astype(str).ne(accepted_parameter)
        allow = pd.Series(False, index=memory.index)
        if bool(self.settings.reopen_same_group_after_accept):
            allow |= memory["group"].astype(str).eq(accepted_group)
        if bool(self.settings.reopen_hydraulic_after_any_accept):
            allow |= memory["group"].astype(str).eq("hydraulic")
        targets = memory.index[candidates & allow]
        names: list[str] = []
        for idx in targets:
            parameter = str(memory.at[idx, "parameter"])
            if self.state_manager is not None and self.state_manager.is_user_locked(parameter):
                continue
            memory.at[idx, "status"] = "reopened"
            memory.at[idx, "phase"] = "symmetric"
            memory.at[idx, "refinement_round"] = 0
            self._reset_local_futility_evidence(memory, idx)
            memory.at[idx, "direction_queue"] = "increase|decrease"
            memory.at[idx, "tested_increase"] = False
            memory.at[idx, "tested_decrease"] = False
            memory.at[idx, "reopen_count"] = int(as_float(memory.at[idx, "reopen_count"], 0) or 0) + 1
            memory.at[idx, "reopened_after_parameter"] = accepted_parameter
            memory.at[idx, "last_update"] = now()
            names.append(parameter)
            if self.state_manager is not None:
                self.state_manager.transition(parameter, "reactivation_candidate", reason="reopened_after_coupled_accepted_change", iteration=int(as_float(state.get("iteration"), 0) or 0), related_parameters=[accepted_parameter])
        if names:
            state["reopened_parameters_count"] = int(as_float(state.get("reopened_parameters_count"), 0) or 0) + len(names)
            self._event({"action": "reopen_after_accepted_improvement", "accepted_parameter": accepted_parameter, "accepted_group": accepted_group, "reopened_parameters": "|".join(names)})
        return names

    # ------------------------------------------------------------------
    # Candidate generation
    # ------------------------------------------------------------------

    def _select_interaction_candidate(self, memory: pd.DataFrame, state: dict[str, Any]) -> pd.DataFrame:
        if self.pair_optimizer is None or self.state_manager is None or self.step_controller is None:
            return pd.DataFrame()
        # Interaction pairs are explicitly lower priority than any unfinished
        # individual direction family.  `_candidate_index` is not called until
        # this guard is checked in `next_suggestion`.
        params = self._params()
        try:
            candidate = self.pair_optimizer.next_candidate(
                parameters=params,
                state_manager=self.state_manager,
                step_controller=self.step_controller,
                evidence={**dict(state.get("last_diagnostics", {}) or {}), "last_event_accepted": bool(state.get("last_event_accepted", False))},
                valid_run_count=int(as_float(state.get("valid_run_count"), 0) or 0),
                iteration=int(as_float(state.get("iteration"), 0) or 0),
            )
        except Exception as exc:
            self._event({"action": "interaction_selection_warning", "reason": f"{type(exc).__name__}: {exc}"})
            return pd.DataFrame()
        if not candidate:
            return pd.DataFrame()

        candidate_id = uuid.uuid4().hex
        rows: list[dict[str, Any]] = []
        for move in candidate["moves"]:
            rows.append({
                "candidate_id": candidate_id,
                "candidate_type": "interaction_pair",
                "pair_id": candidate["pair_id"],
                "parameter_a": candidate["parameter_a"],
                "parameter_b": candidate["parameter_b"],
                "pair_direction": candidate["pair_direction"],
                "interaction_enabled": True,
                "parameter": move["parameter"],
                "old_value": move["old_value"],
                "new_value": move["new_value"],
                "numeric_change": move["numeric_change"],
                "factor_requested": move["factor_requested"],
                "factor_applied": move["factor_applied"],
                "priority": candidate["priority"],
                "reason": f"V14 interaction pair {candidate['pair_id']}: {candidate['phase']}; {candidate['pair_direction']}; {candidate['scientific_rationale']}",
                "evidence": "configured pair + diagnostic/history evidence gate satisfied",
                "optimizer_version": self.VERSION,
                "optimizer_mode": "adaptive_directional_interaction_pair_search",
                "group": candidate["group"],
                "direction": move["direction"],
                "direction_label": move["direction"],
                "step_fraction_requested": move["step_fraction"],
                "step_fraction_applied": move["step_fraction"],
                "move_space": move["move_space"],
                "step_basis": move.get("step_basis", ""),
                "range_normalized": bool(move.get("range_normalized", False)),
                "configured_log_range_decades": move.get("configured_log_range_decades"),
                "initial_search_mode": move.get("initial_search_mode", "automatic_fallback"),
                "initial_search_source": move.get("initial_search_source", "automatic_global_fallback"),
                "initial_factor_requested": move.get("initial_factor_requested"),
                "initial_range_fraction_requested": move.get("initial_range_fraction_requested"),
                "initial_step_fraction_resolved": move.get("initial_step_fraction_resolved"),
                "initial_step_fraction_clamped": move.get("initial_step_fraction_clamped", False),
                "initial_factor_effective": move.get("initial_factor_effective"),
                "pass_number": state.get("pass_number", 1),
                "parent_best_run_folder": state.get("current_best_run_folder", ""),
                "search_phase": candidate["phase"],
            })
        state.update({
            "pending_candidate_id": candidate_id,
            "pending_candidate_type": "interaction_pair",
            "pending_pair_id": candidate["pair_id"],
            "pending_pair_context": json_safe(candidate),
            "last_action": "interaction_pair_selected",
            "last_parameter": f"{candidate['parameter_a']} + {candidate['parameter_b']}",
            "last_direction": candidate["pair_direction"],
        })
        self._write_state(state)
        self._event({"action": "interaction_pair_selected", "candidate_id": candidate_id, **json_safe(candidate), "baseline_objective": state.get("current_best_score")})
        return pd.DataFrame(rows)

    def next_suggestion(self) -> pd.DataFrame:
        state = self._read_state()
        migrated_pass_control = self._ensure_pass_control_state(state)
        if as_float(state.get("current_best_score")) is None or str(state.get("convergence_status", "")).casefold() in {"converged", "stage_transition_required"}:
            if migrated_pass_control:
                self._write_state(state)
            return pd.DataFrame()
        memory = self._ensure_memory()
        # V14.3.4 migration/runtime guard.  It runs before selection and never
        # interrupts an unfinished direction family because such rows are not
        # eligible for local completion.
        completed_by_gate = self._apply_local_futility_gate(
            memory, state, source="automatic_before_selection", force_history_refresh=True
        )
        if completed_by_gate:
            memory, state, _ = self._advance_completed_stage_if_needed(memory, state)
            self._write_memory(memory)
            self._write_state(state)
        if memory.get("pending", pd.Series(dtype=bool)).map(as_bool).any() or str(state.get("pending_candidate_type", "")) == "interaction_pair":
            return pd.DataFrame()

        # Periodic runtime reactivation occurs before selecting a new candidate.
        if self.state_manager is not None:
            try:
                self.state_manager.periodic_reactivation(
                    valid_run_count=int(as_float(state.get("valid_run_count"), 0) or 0),
                    iteration=int(as_float(state.get("iteration"), 0) or 0),
                    new_best=bool(state.get("last_event_accepted", False)),
                    accepted_parameter=str(state.get("last_parameter", "")),
                    residual_changed=bool((state.get("last_diagnostics", {}) or {}).get("v14_residual_diagnostics", {}).get("flags")),
                )
            except Exception:
                pass

        # Interaction-pair search is lower priority than an unfinished
        # direction family *and* any reduced-step individual refinement.
        # Otherwise a pair could skip the local evidence generated after a
        # completed symmetric +/- test.
        unfinished = memory[
            memory.apply(self._direction_family_pending, axis=1)
            & memory.apply(self._is_selectable, axis=1)
        ]
        refinements_pending = memory[
            memory["status"].astype(str).eq("refine_symmetric")
            & memory.apply(self._is_selectable, axis=1)
        ]
        if unfinished.empty and refinements_pending.empty:
            pair = self._select_interaction_candidate(memory, state)
            if not pair.empty:
                return pair

        idx, memory, state = self._candidate_index(memory, state)
        if idx is None:
            self._write_memory(memory)
            self._write_state(state)
            return pd.DataFrame()

        params = self._params().copy()
        params["_key"] = params["parameter"].map(normalise_parameter)
        params = params.set_index("_key")
        parameter = str(memory.at[idx, "parameter"])
        key = normalise_parameter(parameter)
        if key not in params.index:
            memory.at[idx, "status"] = "inactive"
            self._write_memory(memory)
            return self.next_suggestion()
        p = params.loc[key]

        queue = _queue(memory.at[idx, "direction_queue"])
        phase = str(memory.at[idx, "phase"])
        if not queue:
            if phase == "directional_continue":
                direction = str(memory.at[idx, "continuation_direction"])
                memory.at[idx, "phase"] = "directional_opposite"
                memory.at[idx, "direction_queue"] = _opposite(direction)
                memory.at[idx, "status"] = "test_opposite_after_directional_failure"
                queue = _queue(memory.at[idx, "direction_queue"])
            elif phase == "directional_opposite":
                self._finish_or_refine(memory, idx, state, reason="directional_pair_failed")
                self._write_memory(memory)
                return self.next_suggestion()
            else:
                self._finish_or_refine(memory, idx, state, reason="symmetric_pair_failed")
                self._write_memory(memory)
                return self.next_suggestion()

        direction = queue[0]
        base = as_float(p.get("value"))
        if base is None:
            memory.at[idx, "status"] = "inactive"
            self._write_memory(memory)
            return self.next_suggestion()
        if self.step_controller is not None:
            move = self.step_controller.propose(
                parameter=parameter,
                base_value=base,
                direction=direction,
                lower=as_float(p.get("min")),
                upper=as_float(p.get("max")),
            )
            step = float(move["step_fraction"])
        else:
            step = as_float(memory.at[idx, "step_fraction"], self.settings.initial_step_fraction) or self.settings.initial_step_fraction
            factor = 1.0 + step if direction == "increase" else 1.0 - step
            candidate = base * factor
            lower, upper = as_float(p.get("min")), as_float(p.get("max"))
            if lower is not None:
                candidate = max(candidate, lower)
            if upper is not None:
                candidate = min(candidate, upper)
            move = {
                "new_value": candidate,
                "numeric_change": candidate - base,
                "factor_requested": factor,
                "factor_applied": candidate / base if base else 1.0,
                "move_space": "relative",
                "step_basis": "legacy_optimizer_fallback",
                "configured_log_range_decades": None,
                "range_normalized": False,
                "at_bound": candidate == base,
            }

        queue.remove(direction)
        memory.at[idx, "direction_queue"] = "|".join(queue)
        blocked_noop = bool(move.get("blocked_noop", move["new_value"] == base))
        if blocked_noop:
            iteration_now = int(as_float(state.get("iteration"), 0) or 0)
            if self.state_manager is not None:
                self.state_manager.mark_direction_bounded(parameter, direction, iteration=iteration_now)
            self._event({
                "action": "candidate_blocked_by_bound_noop",
                "parameter": parameter,
                "group": memory.at[idx, "group"],
                "direction": direction,
                "old_value": base,
                "requested_new_value": move.get("new_value"),
                "numeric_change": move.get("numeric_change"),
                "noop_delta": move.get("noop_delta"),
                "noop_tolerance": move.get("noop_tolerance"),
                "bound_reason": move.get("bound_reason", "candidate_effectively_equals_baseline"),
                "step_fraction": step,
                "move_space": move.get("move_space", "relative"),
                "step_basis": move.get("step_basis", ""),
                "factor_requested": move.get("factor_requested"),
                "factor_applied": move.get("factor_applied"),
                "configured_lower": move.get("configured_lower"),
                "configured_upper": move.get("configured_upper"),
                "min3p_run_started": False,
                "physical_run_count_increment": 0,
            })
            state.update({
                "last_action": "candidate_blocked_by_bound_noop",
                "last_parameter": parameter,
                "last_direction": direction,
                "last_step_fraction": step,
            })
            if not queue:
                self._finish_or_refine(memory, idx, state, reason="parameter_bound_noop")
            else:
                memory.at[idx, "status"] = "test_remaining_direction"
                memory.at[idx, "last_update"] = now()
            self._write_memory(memory)
            self._write_state(state)
            return self.next_suggestion()

        candidate_id = uuid.uuid4().hex
        memory.at[idx, "pending"] = True
        memory.at[idx, "pending_candidate_id"] = candidate_id
        memory.at[idx, "pending_direction"] = direction
        memory.at[idx, "pending_step_fraction"] = step
        memory.at[idx, "pending_old_value"] = base
        memory.at[idx, "pending_new_value"] = move["new_value"]
        memory.at[idx, "pending_factor_applied"] = move["factor_applied"]
        memory.at[idx, "pending_parent_best_run_folder"] = str(state.get("current_best_run_folder", ""))
        memory.at[idx, "status"] = f"test_{direction}"
        memory.at[idx, "last_update"] = now()
        memory.at[idx, "step_fraction"] = step
        memory.at[idx, "move_space"] = move.get("move_space", memory.at[idx, "move_space"])
        self._write_memory(memory)

        state.update({
            "pending_candidate_id": candidate_id,
            "pending_candidate_type": "single_parameter",
            "pending_pair_id": "",
            "pending_pair_context": {},
            "last_action": "candidate_selected",
            "last_parameter": parameter,
            "last_direction": direction,
            "last_step_fraction": step,
        })
        self._write_state(state)
        self._event({
            "action": "candidate_selected",
            "candidate_id": candidate_id,
            "candidate_type": "single_parameter",
            "parameter": parameter,
            "group": memory.at[idx, "group"],
            "direction": direction,
            "direction_label": direction,
            "step_fraction": step,
            "move_space": move.get("move_space", "relative"),
            "step_basis": move.get("step_basis", ""),
            "range_normalized": bool(move.get("range_normalized", False)),
            "configured_log_range_decades": move.get("configured_log_range_decades"),
            "initial_search_mode": move.get("initial_search_mode", "automatic_fallback"),
            "initial_search_source": move.get("initial_search_source", "automatic_global_fallback"),
            "initial_factor_requested": move.get("initial_factor_requested"),
            "initial_range_fraction_requested": move.get("initial_range_fraction_requested"),
            "initial_step_fraction_resolved": move.get("initial_step_fraction_resolved"),
            "initial_step_fraction_clamped": move.get("initial_step_fraction_clamped", False),
            "initial_factor_effective": move.get("initial_factor_effective"),
            "old_value": base,
            "new_value": move["new_value"],
            "numeric_change": move["numeric_change"],
            "factor_applied": move["factor_applied"],
            "baseline_objective": state.get("current_best_score"),
            "parent_best_run_folder": state.get("current_best_run_folder", ""),
            "pass_number": state.get("pass_number", 1),
            "phase": memory.at[idx, "phase"],
            "priority_local_refinement": bool(
                int(as_float(memory.at[idx, "priority_refinement_rounds_remaining"], 0) or 0) > 0
            ),
            "priority_refinement_rounds_remaining": int(
                as_float(memory.at[idx, "priority_refinement_rounds_remaining"], 0) or 0
            ),
        })
        return pd.DataFrame([{
            "candidate_id": candidate_id,
            "candidate_type": "single_parameter",
            "parameter": parameter,
            "old_value": base,
            "new_value": move["new_value"],
            "numeric_change": move["numeric_change"],
            "factor_requested": move["factor_requested"],
            "factor_applied": move["factor_applied"],
            "priority": int(memory.at[idx, "priority"]),
            "reason": f"V14 {memory.at[idx, 'phase']}: {memory.at[idx, 'group']}; {direction}; step={step:.6g}; move_space={move.get('move_space', 'relative')}",
            "evidence": f"current_best_TOTAL_SCORE={state.get('current_best_score')}",
            "optimizer_version": self.VERSION,
            "optimizer_mode": "adaptive_directional_coordinate_search",
            "group": memory.at[idx, "group"],
            "direction": direction,
            "direction_label": direction,
            "step_fraction_requested": step,
            "step_fraction_applied": step,
            "move_space": move.get("move_space", "relative"),
            "step_basis": move.get("step_basis", ""),
            "range_normalized": bool(move.get("range_normalized", False)),
            "configured_log_range_decades": move.get("configured_log_range_decades"),
            "initial_search_mode": move.get("initial_search_mode", "automatic_fallback"),
            "initial_search_source": move.get("initial_search_source", "automatic_global_fallback"),
            "initial_factor_requested": move.get("initial_factor_requested"),
            "initial_range_fraction_requested": move.get("initial_range_fraction_requested"),
            "initial_step_fraction_resolved": move.get("initial_step_fraction_resolved"),
            "initial_step_fraction_clamped": move.get("initial_step_fraction_clamped", False),
            "initial_factor_effective": move.get("initial_factor_effective"),
            "pass_number": state.get("pass_number", 1),
            "parent_best_run_folder": state.get("current_best_run_folder", ""),
            "search_phase": memory.at[idx, "phase"],
            "priority_local_refinement": bool(
                int(as_float(memory.at[idx, "priority_refinement_rounds_remaining"], 0) or 0) > 0
            ),
            "priority_refinement_rounds_remaining": int(
                as_float(memory.at[idx, "priority_refinement_rounds_remaining"], 0) or 0
            ),
            "interaction_enabled": False,
        }])

    # ------------------------------------------------------------------
    # Candidate observation and decision finalisation
    # ------------------------------------------------------------------

    def _meaningful(self, baseline: float | None, candidate: float | None, penalty: float, valid: bool, scientific_ok: bool) -> tuple[bool, float | None, float, float]:
        effective = None if candidate is None else candidate + penalty
        improvement = None if effective is None or baseline is None else baseline - effective
        absolute = improvement or 0.0
        relative = absolute / max(abs(baseline or 1.0), 1e-30)
        meaningful = bool(valid) and bool(scientific_ok) and candidate is not None and absolute >= self.settings.min_absolute_improvement and relative >= self.settings.min_relative_improvement
        return meaningful, effective, absolute, relative

    def _clear_pending_single(self, memory: pd.DataFrame, idx: int) -> None:
        for column in [
            "pending", "pending_candidate_id", "pending_direction", "pending_step_fraction",
            "pending_old_value", "pending_new_value", "pending_factor_applied", "pending_parent_best_run_folder",
        ]:
            memory.at[idx, column] = False if column == "pending" else (None if column in {"pending_step_fraction", "pending_old_value", "pending_new_value", "pending_factor_applied"} else "")

    def _observe_single(
        self,
        memory: pd.DataFrame,
        state: dict[str, Any],
        *,
        candidate_objective: float | None,
        run_folder: str,
        scientific_ok: bool,
        scientific_penalty: float,
        valid: bool,
        diagnostics: dict[str, Any],
        min3p_run_status: str,
    ) -> dict[str, Any]:
        pending = memory[memory.get("pending", pd.Series(dtype=bool)).map(as_bool)]
        if pending.empty:
            return {"action": "baseline_observed", "accepted": False, "optimizer_version": self.VERSION}
        idx = int(pending.index[-1])
        row = memory.loc[idx].copy()
        baseline = as_float(state.get("current_best_score"))
        candidate = as_float(candidate_objective)
        penalty = float(scientific_penalty or 0.0)
        meaningful, effective, absolute, relative = self._meaningful(baseline, candidate, penalty, valid, scientific_ok)
        direction = str(row.get("pending_direction", ""))
        step = as_float(row.get("pending_step_fraction"), self.settings.initial_step_fraction) or self.settings.initial_step_fraction
        phase = str(row.get("phase", "symmetric"))
        parameter, group = str(row.get("parameter", "")), str(row.get("group", ""))
        candidate_id = str(row.get("pending_candidate_id", ""))

        self._clear_pending_single(memory, idx)
        memory.at[idx, f"tested_{direction}"] = True
        memory.at[idx, "last_update"] = now()
        event: dict[str, Any] = {
            "candidate_id": candidate_id,
            "candidate_type": "single_parameter",
            "run_folder": str(run_folder),
            "parent_best_run_folder": str(row.get("pending_parent_best_run_folder", "")),
            "parameter": parameter,
            "group": group,
            "old_value": as_float(row.get("pending_old_value")),
            "new_value": as_float(row.get("pending_new_value")),
            "numeric_change": (as_float(row.get("pending_new_value"), 0.0) or 0.0) - (as_float(row.get("pending_old_value"), 0.0) or 0.0),
            "factor_applied": as_float(row.get("pending_factor_applied")),
            "direction": direction,
            "direction_label": direction,
            "step_fraction": step,
            "search_phase": phase,
            "baseline_TOTAL_SCORE": baseline,
            "candidate_TOTAL_SCORE": candidate,
            "effective_TOTAL_SCORE": effective,
            "objective_improved": bool(candidate is not None and baseline is not None and candidate < baseline),
            "improvement_absolute": absolute,
            "improvement_relative": relative,
            "scientific_ok": bool(scientific_ok),
            "scientific_penalty": penalty,
            "MIN3P_run_status": min3p_run_status,
            "valid_run": bool(valid),
            "pass_number": int(as_float(state.get("pass_number"), 1) or 1),
            "pass_accepted_improvements_count": int(
                as_float(state.get("pass_accepted_improvements_count"), 0) or 0
            ),
            **json_safe(diagnostics or {}),
        }

        if meaningful:
            if self.step_controller is not None:
                step_record = self.step_controller.observe_outcome(parameter, direction=direction, accepted=True, defer_shrink=False, reason="accepted_meaningful_improvement")
                memory.at[idx, "step_fraction"] = float(step_record.get("current_step_fraction", step))
            memory.at[idx, "accepted_improvement_count"] = int(as_float(row.get("accepted_improvement_count"), 0) or 0) + 1
            memory.at[idx, "sensitivity_score"] = abs((candidate or baseline or 0.0) - (baseline or 0.0)) / max(step, 1e-30)
            memory.at[idx, "phase"] = "directional_continue"
            memory.at[idx, "refinement_round"] = 0
            self._reset_local_futility_evidence(memory, idx)
            memory.at[idx, "continuation_direction"] = direction
            memory.at[idx, "direction_queue"] = direction
            memory.at[idx, "tested_increase"] = False
            memory.at[idx, "tested_decrease"] = False
            memory.at[idx, "status"] = f"continue_{direction}"
            state["current_best_score"] = candidate
            state["current_best_run_folder"] = str(run_folder)
            state["accepted_improvements_count"] = int(as_float(state.get("accepted_improvements_count"), 0) or 0) + 1
            self._ensure_pass_control_state(state)
            state["pass_accepted_improvements_count"] = int(
                as_float(state.get("pass_accepted_improvements_count"), 0) or 0
            ) + 1
            reopened = self._reopen_after_accept(memory, state, parameter, group)
            if self.state_manager is not None:
                self.state_manager.mark_outcome(parameter, accepted=True, direction=direction, valid_run_count=int(as_float(state.get("valid_run_count"), 0) or 0), iteration=int(as_float(state.get("iteration"), 0) or 0), reason="accepted_meaningful_improvement")
            event.update({
                "action": "accepted_meaningful_improvement_continue_direction",
                "accepted": True,
                "decision": "accept",
                "rejection_reason": "",
                "next_step_fraction": memory.at[idx, "step_fraction"],
                "reopened_parameters": "|".join(reopened),
            })
        else:
            if not valid:
                reason, action = "invalid_or_failed_MIN3P_run", "mark_infeasible_continue"
                state["invalid_candidates_count"] = int(as_float(state.get("invalid_candidates_count"), 0) or 0) + 1
                memory.at[idx, "invalid_count"] = int(as_float(row.get("invalid_count"), 0) or 0) + 1
            elif not scientific_ok:
                reason, action = "scientific_constraint_failed", "reject_constraint_continue"
            elif candidate is not None and baseline is not None and abs(candidate - baseline) <= self.settings.neutral_absolute_band:
                reason, action = "near_neutral_change", "reject_neutral_continue"
            else:
                reason, action = "TOTAL_SCORE_not_meaningfully_improved", "reject_continue"
            # V14.3: record evidence for the current symmetric direction pair.
            # Only clean objective rejections count; neutral/invalid/constraint
            # outcomes deliberately block automatic futility completion.
            if phase == "symmetric":
                if reason == "TOTAL_SCORE_not_meaningfully_improved":
                    memory.at[idx, "futility_pair_objective_rejection_count"] = int(
                        as_float(memory.at[idx, "futility_pair_objective_rejection_count"], 0) or 0
                    ) + 1
                else:
                    memory.at[idx, "futility_pair_blocked"] = True
            state["rejected_candidates_count"] = int(as_float(state.get("rejected_candidates_count"), 0) or 0) + 1
            remaining = _queue(memory.at[idx, "direction_queue"])
            defer_shrink = bool(remaining) or phase in {"directional_continue", "directional_opposite"}
            if self.step_controller is not None:
                step_record = self.step_controller.observe_outcome(parameter, direction=direction, accepted=False, defer_shrink=defer_shrink, reason=reason)
                memory.at[idx, "step_fraction"] = float(step_record.get("current_step_fraction", step))
            if self.state_manager is not None:
                self.state_manager.mark_outcome(parameter, accepted=False, direction=direction, valid_run_count=int(as_float(state.get("valid_run_count"), 0) or 0), iteration=int(as_float(state.get("iteration"), 0) or 0), reason=reason)
            if remaining:
                # This is the protected V13.5 rule: the opposite queued
                # direction is selected before any other parameter.
                memory.at[idx, "status"] = "test_remaining_direction"
            elif phase == "directional_continue":
                memory.at[idx, "phase"] = "directional_opposite"
                memory.at[idx, "direction_queue"] = _opposite(direction)
                memory.at[idx, "status"] = "test_opposite_after_directional_failure"
                action = "test_opposite_after_directional_failure"
            elif phase == "directional_opposite":
                action = self._finish_or_refine(memory, idx, state, reason="directional_pair_failed")
            else:
                action = self._finish_or_refine(memory, idx, state, reason="symmetric_pair_failed")
            event.update({"action": action, "accepted": False, "decision": "reject", "rejection_reason": reason})
        event["next_step_fraction"] = as_float(memory.at[idx, "step_fraction"])
        event["priority_refinement_rounds_remaining"] = int(
            as_float(memory.at[idx, "priority_refinement_rounds_remaining"], 0) or 0
        )
        event["priority_local_refinement"] = bool(event["priority_refinement_rounds_remaining"] > 0)
        return event

    def _observe_pair(
        self,
        state: dict[str, Any],
        *,
        candidate_objective: float | None,
        run_folder: str,
        scientific_ok: bool,
        scientific_penalty: float,
        valid: bool,
        diagnostics: dict[str, Any],
        min3p_run_status: str,
    ) -> dict[str, Any]:
        context = dict(state.get("pending_pair_context", {}) or {})
        pair_id = str(state.get("pending_pair_id", context.get("pair_id", "")))
        moves = list(context.get("moves", []) or [])
        baseline = as_float(state.get("current_best_score"))
        candidate = as_float(candidate_objective)
        penalty = float(scientific_penalty or 0.0)
        meaningful, effective, absolute, relative = self._meaningful(baseline, candidate, penalty, valid, scientific_ok)
        reason = "accepted_meaningful_pair_improvement" if meaningful else (
            "invalid_or_failed_MIN3P_run" if not valid else ("scientific_constraint_failed" if not scientific_ok else "TOTAL_SCORE_not_meaningfully_improved")
        )
        pair_result = {"pair_action": "pair_service_unavailable", "pair_complete": True}
        if self.pair_optimizer is not None and pair_id:
            pair_result = self.pair_optimizer.observe_result(
                pair_id=pair_id,
                accepted=meaningful,
                valid=valid,
                reason=reason,
                iteration=int(as_float(state.get("iteration"), 0) or 0),
            )
        pair_complete = bool(pair_result.get("pair_complete", False))
        for move in moves:
            parameter = str(move.get("parameter", ""))
            direction = str(move.get("direction", ""))
            if self.step_controller is not None and parameter:
                step_record = self.step_controller.observe_outcome(parameter, direction=direction, accepted=meaningful, defer_shrink=(not meaningful), reason=reason)
                if not meaningful and pair_complete:
                    self.step_controller.finalize_failed_direction_family(parameter)
            if self.state_manager is not None and parameter:
                self.state_manager.mark_outcome(parameter, accepted=meaningful, direction=direction, valid_run_count=int(as_float(state.get("valid_run_count"), 0) or 0), iteration=int(as_float(state.get("iteration"), 0) or 0), reason=reason)
        parameters = [str(move.get("parameter", "")) for move in moves if str(move.get("parameter", ""))]
        if self.state_manager is not None and pair_complete:
            self.state_manager.release_interaction(parameters, iteration=int(as_float(state.get("iteration"), 0) or 0), reason=str(pair_result.get("pair_action", "pair_complete")))
        if meaningful:
            state["current_best_score"] = candidate
            state["current_best_run_folder"] = str(run_folder)
            state["accepted_improvements_count"] = int(as_float(state.get("accepted_improvements_count"), 0) or 0) + 1
            self._ensure_pass_control_state(state)
            state["pass_accepted_improvements_count"] = int(
                as_float(state.get("pass_accepted_improvements_count"), 0) or 0
            ) + 1
        else:
            state["rejected_candidates_count"] = int(as_float(state.get("rejected_candidates_count"), 0) or 0) + 1
            if not valid:
                state["invalid_candidates_count"] = int(as_float(state.get("invalid_candidates_count"), 0) or 0) + 1
        candidate_id = str(state.get("pending_candidate_id", ""))
        state.update({"pending_candidate_id": "", "pending_candidate_type": "", "pending_pair_id": "", "pending_pair_context": {}})
        label = f"{context.get('parameter_a', '')} + {context.get('parameter_b', '')}".strip(" +")
        return {
            "candidate_id": candidate_id,
            "candidate_type": "interaction_pair",
            "pair_id": pair_id,
            "pair_direction": context.get("pair_direction", ""),
            "parameter": label,
            "group": context.get("group", "interaction"),
            "direction": context.get("pair_direction", ""),
            "direction_label": context.get("pair_direction", ""),
            "baseline_TOTAL_SCORE": baseline,
            "candidate_TOTAL_SCORE": candidate,
            "effective_TOTAL_SCORE": effective,
            "objective_improved": bool(candidate is not None and baseline is not None and candidate < baseline),
            "improvement_absolute": absolute,
            "improvement_relative": relative,
            "scientific_ok": bool(scientific_ok),
            "scientific_penalty": penalty,
            "MIN3P_run_status": min3p_run_status,
            "valid_run": bool(valid),
            "pass_number": int(as_float(state.get("pass_number"), 1) or 1),
            "pass_accepted_improvements_count": int(
                as_float(state.get("pass_accepted_improvements_count"), 0) or 0
            ),
            "action": pair_result.get("pair_action", "interaction_pair_evaluated"),
            "accepted": meaningful,
            "decision": "accept" if meaningful else "reject",
            "rejection_reason": "" if meaningful else reason,
            "interaction_moves": json_safe(moves),
            **json_safe(pair_result),
            **json_safe(diagnostics or {}),
        }

    def observe_run(
        self,
        candidate_objective,
        run_folder: str = "",
        scientific_ok: bool = True,
        scientific_penalty: float = 0.0,
        valid: bool = True,
        diagnostics: dict[str, Any] | None = None,
        min3p_run_status: str = "",
        count_as_valid_run: bool = True,
    ) -> dict[str, Any]:
        state = self._read_state()
        diagnostics = diagnostics or {}
        candidate_type = str(state.get("pending_candidate_type", ""))
        memory = self._ensure_memory()
        if candidate_type == "interaction_pair":
            event = self._observe_pair(
                state,
                candidate_objective=candidate_objective,
                run_folder=run_folder,
                scientific_ok=scientific_ok,
                scientific_penalty=scientific_penalty,
                valid=valid,
                diagnostics=diagnostics,
                min3p_run_status=min3p_run_status,
            )
        else:
            event = self._observe_single(
                memory,
                state,
                candidate_objective=candidate_objective,
                run_folder=run_folder,
                scientific_ok=scientific_ok,
                scientific_penalty=scientific_penalty,
                valid=valid,
                diagnostics=diagnostics,
                min3p_run_status=min3p_run_status,
            )
        state["iteration"] = int(as_float(state.get("iteration"), 0) or 0) + 1
        if valid and count_as_valid_run:
            state["valid_run_count"] = int(as_float(state.get("valid_run_count"), 0) or 0) + 1
        state.update({
            "last_action": event.get("action", "candidate_observed"),
            "last_parameter": event.get("parameter", ""),
            "last_direction": event.get("direction", ""),
            "last_step_fraction": event.get("step_fraction"),
            "last_diagnostics": json_safe(diagnostics),
            "last_event_accepted": bool(event.get("accepted", False)),
            "pending_candidate_id": "",
            "pending_candidate_type": "" if candidate_type != "interaction_pair" else state.get("pending_candidate_type", ""),
        })
        if candidate_type != "interaction_pair":
            state["pending_candidate_type"] = ""
        self._write_memory(memory)
        self._write_state(state)
        self._event(event)
        return {"optimizer_version": self.VERSION, **event}

    # ------------------------------------------------------------------
    # Pipeline safety validation and GPT advisory ingestion
    # ------------------------------------------------------------------

    def validate_suggestion_precedence(self, *, suggestion: pd.DataFrame) -> dict[str, Any]:
        if suggestion is None or suggestion.empty:
            return {"ok": False, "reason": "empty_suggestion"}
        state = self._read_state()
        candidate_ids = {str(v) for v in suggestion.get("candidate_id", pd.Series(dtype=str)).tolist() if str(v)}
        if len(candidate_ids) != 1:
            return {"ok": False, "reason": "candidate_must_have_exactly_one_candidate_id"}
        candidate_id = next(iter(candidate_ids))
        if candidate_id != str(state.get("pending_candidate_id", "")):
            return {"ok": False, "reason": "candidate_id_does_not_match_pending_optimizer_state"}
        candidate_type = str(suggestion.iloc[0].get("candidate_type", "single_parameter"))
        memory = self._ensure_memory()
        individual_pending = memory[memory.apply(self._direction_family_pending, axis=1) & memory.apply(self._is_selectable, axis=1)]
        if candidate_type == "interaction_pair" and not individual_pending.empty:
            return {"ok": False, "reason": "interaction_pair_cannot_interrupt_individual_direction_family"}
        if candidate_type == "single_parameter":
            if len(suggestion) != 1:
                return {"ok": False, "reason": "single_parameter_candidate_must_have_one_row"}
            pending = memory[memory.get("pending", pd.Series(dtype=bool)).map(as_bool)]
            if pending.empty:
                return {"ok": False, "reason": "no_pending_single_parameter_candidate"}
            parameter = str(suggestion.iloc[0].get("parameter", ""))
            expected = str(pending.iloc[-1].get("parameter", ""))
            if normalise_parameter(parameter) != normalise_parameter(expected):
                return {"ok": False, "reason": "candidate_parameter_does_not_match_pending_direction_family"}
        if self.strategy_manager is not None:
            try:
                result = self.strategy_manager.validate_candidate(suggestion=suggestion, state_manager=self.state_manager, pair_optimizer=self.pair_optimizer)
                if isinstance(result, dict) and not result.get("ok", False):
                    return {"ok": False, "reason": str(result.get("reason", "strategy_manager_rejected_candidate"))}
            except Exception as exc:
                return {"ok": False, "reason": f"strategy_manager_validation_error:{type(exc).__name__}:{exc}"}
        return {"ok": True, "reason": "directional_precedence_and_hierarchy_validated"}

    def set_gpt_advisory(self, advice: dict[str, Any]) -> None:
        """Store advisory only.  Deterministic selection validates it later."""
        if not isinstance(advice, dict):
            return
        required = {"suggested_parameter", "recommended_group", "confidence", "recommended_action"}
        if not required.issubset(advice):
            return
        self._gpt_advisory = json_safe(advice)
        self._event({"action": "gpt_advisory_received", "confidence": advice.get("confidence"), "suggested_parameter": advice.get("suggested_parameter"), "recommended_group": advice.get("recommended_group")})

    def finalize_decision(self, event: dict[str, Any], restoration_verified: bool) -> dict[str, Any]:
        record = {"timestamp": now(), "optimizer_version": self.VERSION, **json_safe(event), "restoration_verified": bool(restoration_verified)}
        last_error: PermissionError | None = None
        for attempt in range(8):
            try:
                append_excel_atomic(self.decision_file, [record])
                last_error = None
                break
            except PermissionError as exc:
                last_error = exc
                if attempt < 7:
                    time.sleep(0.10 * (attempt + 1))
        if last_error is not None:
            raise last_error
        self._event({"action": "candidate_decision_finalized", "candidate_id": event.get("candidate_id", ""), "candidate_type": event.get("candidate_type", "single_parameter"), "accepted": event.get("accepted", False), "restoration_verified": bool(restoration_verified)})
        return record
