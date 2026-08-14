from __future__ import annotations

"""V14 runtime parameter-state overlay.

The manager never edits ``agent_config.xlsx``.  User intent remains in the
``status`` column; V14 state is an isolated overlay stored in 04_results.
"""

from pathlib import Path
from typing import Any

import pandas as pd

from modules.v14_utils import (
    append_excel_atomic,
    as_bool,
    as_float,
    json_safe,
    normalise_parameter,
    now,
    read_json,
    write_json_atomic,
)


ACTIVE = "active"
TEMPORARILY_FROZEN = "temporarily_frozen"
FROZEN_BY_USER = "frozen_by_user"
INTERACTION_ACTIVE = "interaction_active"
REACTIVATION_CANDIDATE = "reactivation_candidate"
BOUNDED = "bounded"
EXHAUSTED = "exhausted"
INACTIVE = "inactive"

USER_ACTIVE = {"active", "yes", "true", "1", "y"}
USER_INACTIVE = {"inactive", "no", "false", "0", "n", ""}
USER_FROZEN = {"frozen", "frozen_by_user", "user_frozen", "locked", "lock"}


class ParameterStateManager:
    VERSION = "V14"

    def __init__(self, paths, config):
        self.paths = paths
        self.config = config
        self.state_file = Path(paths.results_dir) / "v14_parameter_runtime_state.json"
        self.history_file = Path(paths.results_dir) / "parameter_state_manager_events.xlsx"

    def _load(self) -> dict[str, Any]:
        state = read_json(self.state_file, {"parameters": {}, "metadata": {}})
        if not isinstance(state, dict):
            state = {"parameters": {}, "metadata": {}}
        state.setdefault("parameters", {})
        state.setdefault("metadata", {})
        return state

    def _save(self, state: dict[str, Any]) -> None:
        state["metadata"].update({"updated_at": now(), "optimizer_version": self.VERSION})
        write_json_atomic(self.state_file, state)

    @staticmethod
    def user_state(status: Any) -> str:
        text = str(status if status is not None else "active").strip().casefold()
        if text in USER_FROZEN:
            return FROZEN_BY_USER
        if text in USER_INACTIVE:
            return INACTIVE
        return ACTIVE

    def sync_with_parameters(self, parameters: pd.DataFrame, *, iteration: int = 0) -> dict[str, Any]:
        """Synchronise user-controlled statuses without overwriting V14 runtime state."""
        state = self._load()
        entries: dict[str, dict[str, Any]] = state["parameters"]
        seen: set[str] = set()

        for _, row in parameters.iterrows():
            name = str(row.get("parameter", "")).strip()
            key = normalise_parameter(name)
            if not key:
                continue
            seen.add(key)
            user_status = self.user_state(row.get("status", "active"))
            entry = entries.get(key, {})
            old_runtime = str(entry.get("runtime_state", ""))

            if user_status in {FROZEN_BY_USER, INACTIVE}:
                runtime_state = user_status
                reason = "user_status_sync"
            elif old_runtime in {FROZEN_BY_USER, INACTIVE}:
                runtime_state = ACTIVE
                reason = "user_reactivated_parameter"
            elif old_runtime:
                runtime_state = old_runtime
                reason = str(entry.get("reason", "runtime_state_preserved"))
            else:
                runtime_state = ACTIVE
                reason = "initial_active_state"

            entries[key] = {
                "parameter": name,
                "user_status": user_status,
                "runtime_state": runtime_state,
                "reason": reason,
                "since_iteration": int(as_float(entry.get("since_iteration"), iteration) or iteration),
                "last_transition_at": entry.get("last_transition_at", now()),
                "expires_after_valid_runs": as_float(entry.get("expires_after_valid_runs")),
                "last_valid_run_count": int(as_float(entry.get("last_valid_run_count"), 0) or 0),
                "related_parameters": list(entry.get("related_parameters", []) or []),
                "bounded_directions": list(entry.get("bounded_directions", []) or []),
                "last_accepted_direction": str(entry.get("last_accepted_direction", "")),
                "last_rejected_direction": str(entry.get("last_rejected_direction", "")),
                "reactivation_conditions": list(entry.get("reactivation_conditions", []) or []),
            }

        # Keep removed entries for audit but never consider them eligible.
        for key, entry in entries.items():
            entry["present_in_current_config"] = key in seen

        self._save(state)
        return state

    def _entry(self, parameter: str) -> dict[str, Any]:
        state = self._load()
        key = normalise_parameter(parameter)
        entry = state["parameters"].get(key)
        if entry is None:
            entry = {
                "parameter": parameter,
                "user_status": ACTIVE,
                "runtime_state": ACTIVE,
                "reason": "implicit_active_state",
                "since_iteration": 0,
                "last_transition_at": now(),
                "expires_after_valid_runs": None,
                "last_valid_run_count": 0,
                "related_parameters": [],
                "bounded_directions": [],
                "last_accepted_direction": "",
                "last_rejected_direction": "",
                "reactivation_conditions": [],
                "present_in_current_config": True,
            }
            state["parameters"][key] = entry
            self._save(state)
        return entry

    def get(self, parameter: str) -> dict[str, Any]:
        return dict(self._entry(parameter))

    def runtime_state(self, parameter: str) -> str:
        return str(self._entry(parameter).get("runtime_state", ACTIVE))

    def is_user_locked(self, parameter: str) -> bool:
        return self.runtime_state(parameter) in {FROZEN_BY_USER, INACTIVE}

    def is_eligible(self, parameter: str, *, for_interaction: bool = False, ignore_runtime_freeze: bool = False) -> bool:
        entry = self._entry(parameter)
        runtime = str(entry.get("runtime_state", ACTIVE))
        if runtime in {FROZEN_BY_USER, INACTIVE}:
            return False
        if ignore_runtime_freeze:
            return True
        if for_interaction:
            return runtime in {ACTIVE, REACTIVATION_CANDIDATE, INTERACTION_ACTIVE}
        return runtime in {ACTIVE, REACTIVATION_CANDIDATE}

    def transition(
        self,
        parameter: str,
        runtime_state: str,
        *,
        reason: str,
        iteration: int | None = None,
        related_parameters: list[str] | None = None,
        expires_after_valid_runs: int | None = None,
        reactivation_conditions: list[str] | None = None,
    ) -> dict[str, Any]:
        allowed = {
            ACTIVE,
            TEMPORARILY_FROZEN,
            FROZEN_BY_USER,
            INTERACTION_ACTIVE,
            REACTIVATION_CANDIDATE,
            BOUNDED,
            EXHAUSTED,
            INACTIVE,
        }
        if runtime_state not in allowed:
            raise ValueError(f"Unsupported V14 runtime state: {runtime_state}")

        state = self._load()
        key = normalise_parameter(parameter)
        entry = state["parameters"].get(key, self._entry(parameter))
        previous = str(entry.get("runtime_state", ACTIVE))

        # User locks are absolute and cannot be replaced by automatic logic.
        if entry.get("user_status") in {FROZEN_BY_USER, INACTIVE} and runtime_state not in {FROZEN_BY_USER, INACTIVE}:
            return dict(entry)

        entry.update(
            {
                "parameter": parameter,
                "runtime_state": runtime_state,
                "reason": reason,
                "since_iteration": int(iteration if iteration is not None else entry.get("since_iteration", 0)),
                "last_transition_at": now(),
                "related_parameters": list(related_parameters or entry.get("related_parameters", []) or []),
                "expires_after_valid_runs": expires_after_valid_runs,
                "reactivation_conditions": list(reactivation_conditions or entry.get("reactivation_conditions", []) or []),
            }
        )
        state["parameters"][key] = entry
        self._save(state)
        append_excel_atomic(
            self.history_file,
            [{
                "timestamp": now(),
                "optimizer_version": self.VERSION,
                "parameter": parameter,
                "previous_runtime_state": previous,
                "new_runtime_state": runtime_state,
                "user_status": entry.get("user_status"),
                "reason": reason,
                "iteration": iteration,
                "related_parameters": "|".join(entry.get("related_parameters", [])),
                "expires_after_valid_runs": expires_after_valid_runs,
                "reactivation_conditions": "|".join(entry.get("reactivation_conditions", [])),
            }],
        )
        return dict(entry)

    def mark_direction_bounded(self, parameter: str, direction: str, *, iteration: int | None = None) -> dict[str, Any]:
        state = self._load()
        key = normalise_parameter(parameter)
        entry = state["parameters"].get(key, self._entry(parameter))
        directions = set(entry.get("bounded_directions", []) or [])
        directions.add(direction)
        entry["bounded_directions"] = sorted(directions)
        state["parameters"][key] = entry
        self._save(state)
        if {"increase", "decrease"}.issubset(directions):
            return self.transition(parameter, BOUNDED, reason="both_directions_at_hard_bound", iteration=iteration)
        return dict(entry)

    def clear_direction_bound(self, parameter: str, direction: str | None = None) -> None:
        state = self._load()
        key = normalise_parameter(parameter)
        entry = state["parameters"].get(key, self._entry(parameter))
        directions = set(entry.get("bounded_directions", []) or [])
        if direction:
            directions.discard(direction)
        else:
            directions.clear()
        entry["bounded_directions"] = sorted(directions)
        if not directions and entry.get("runtime_state") == BOUNDED and entry.get("user_status") == ACTIVE:
            entry["runtime_state"] = ACTIVE
            entry["reason"] = "bound_cleared"
            entry["last_transition_at"] = now()
        state["parameters"][key] = entry
        self._save(state)

    def mark_outcome(
        self,
        parameter: str,
        *,
        accepted: bool,
        direction: str,
        valid_run_count: int,
        iteration: int,
        reason: str,
    ) -> dict[str, Any]:
        state = self._load()
        key = normalise_parameter(parameter)
        entry = state["parameters"].get(key, self._entry(parameter))
        entry["last_valid_run_count"] = int(valid_run_count)
        if accepted:
            entry["last_accepted_direction"] = direction
            if entry.get("runtime_state") not in {FROZEN_BY_USER, INACTIVE, INTERACTION_ACTIVE}:
                entry["runtime_state"] = ACTIVE
                entry["reason"] = "accepted_directional_improvement"
        else:
            entry["last_rejected_direction"] = direction
            entry["reason"] = reason
        entry["last_transition_at"] = now()
        state["parameters"][key] = entry
        self._save(state)
        return dict(entry)

    def mark_temporarily_exhausted(
        self,
        parameter: str,
        *,
        iteration: int,
        valid_run_count: int,
        reason: str,
        cooldown_valid_runs: int = 4,
        related_parameters: list[str] | None = None,
    ) -> dict[str, Any]:
        self.transition(
            parameter,
            EXHAUSTED,
            reason=reason,
            iteration=iteration,
            related_parameters=related_parameters,
            expires_after_valid_runs=valid_run_count + max(int(cooldown_valid_runs), 1),
            reactivation_conditions=[
                "new_best_run",
                "coupled_parameter_accepted",
                "residual_pattern_changed",
                "valid_run_cooldown_elapsed",
            ],
        )
        return self.get(parameter)

    def activate_interaction(self, parameters: list[str], *, iteration: int, pair_id: str) -> None:
        for parameter in parameters:
            if self.is_user_locked(parameter):
                continue
            self.transition(
                parameter,
                INTERACTION_ACTIVE,
                reason=f"interaction_pair_active:{pair_id}",
                iteration=iteration,
                related_parameters=[p for p in parameters if p != parameter],
            )

    def release_interaction(self, parameters: list[str], *, iteration: int, reason: str) -> None:
        for parameter in parameters:
            entry = self.get(parameter)
            if entry.get("user_status") != ACTIVE:
                continue
            if entry.get("runtime_state") == INTERACTION_ACTIVE:
                self.transition(parameter, ACTIVE, reason=reason, iteration=iteration)

    def periodic_reactivation(
        self,
        *,
        valid_run_count: int,
        iteration: int,
        new_best: bool = False,
        accepted_parameter: str = "",
        residual_changed: bool = False,
        group_changed: bool = False,
    ) -> list[str]:
        """Reconsider temporary states; user locks are never reactivated."""
        state = self._load()
        reactivated: list[str] = []
        accepted_key = normalise_parameter(accepted_parameter)

        for key, entry in state["parameters"].items():
            if entry.get("user_status") != ACTIVE:
                continue
            runtime = str(entry.get("runtime_state", ACTIVE))
            if runtime not in {TEMPORARILY_FROZEN, EXHAUSTED, REACTIVATION_CANDIDATE, BOUNDED}:
                continue
            conditions = set(entry.get("reactivation_conditions", []) or [])
            expiry = as_float(entry.get("expires_after_valid_runs"))
            coupled = {normalise_parameter(v) for v in entry.get("related_parameters", []) or []}
            trigger = (
                (new_best and "new_best_run" in conditions)
                or (accepted_key and accepted_key in coupled and "coupled_parameter_accepted" in conditions)
                or (residual_changed and "residual_pattern_changed" in conditions)
                or (group_changed and "active_group_changed" in conditions)
                or (expiry is not None and valid_run_count >= int(expiry))
            )
            if not trigger:
                continue
            old = runtime
            entry.update(
                {
                    "runtime_state": ACTIVE,
                    "reason": "reactivated_by_configured_trigger",
                    "since_iteration": iteration,
                    "last_transition_at": now(),
                    "expires_after_valid_runs": None,
                }
            )
            reactivated.append(str(entry.get("parameter", key)))
            append_excel_atomic(
                self.history_file,
                [{
                    "timestamp": now(),
                    "optimizer_version": self.VERSION,
                    "parameter": entry.get("parameter", key),
                    "previous_runtime_state": old,
                    "new_runtime_state": ACTIVE,
                    "user_status": entry.get("user_status"),
                    "reason": "reactivated_by_configured_trigger",
                    "iteration": iteration,
                }],
            )
        self._save(state)
        return reactivated

    def snapshot(self) -> dict[str, Any]:
        return json_safe(self._load())
