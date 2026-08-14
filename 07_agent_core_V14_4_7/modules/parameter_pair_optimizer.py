from __future__ import annotations

"""Bounded, evidence-gated interaction-pair search for V14.

This module never enumerates all combinations.  It only considers rows in
``config/parameter_interactions.xlsx`` whose parameter names match the active
configuration exactly and whose evidence gate is satisfied.
"""

from pathlib import Path
from typing import Any

import pandas as pd

from modules.v14_utils import as_bool, as_float, json_safe, normalise_parameter, now, read_excel_safe, read_json, write_json_atomic


class ParameterPairOptimizer:
    VERSION = "V14.3.5"

    def __init__(self, paths, config):
        self.paths = paths
        self.config = config
        self.definition_file = Path(paths.agent_core_dir) / "config" / "parameter_interactions.xlsx"
        self.state_file = Path(paths.results_dir) / "v14_interaction_runtime_state.json"

    def _load_state(self) -> dict[str, Any]:
        state = read_json(self.state_file, {"pairs": {}, "metadata": {}})
        if not isinstance(state, dict):
            state = {"pairs": {}, "metadata": {}}
        state.setdefault("pairs", {})
        state.setdefault("metadata", {})
        state.setdefault("events", [])
        if not isinstance(state.get("events"), list):
            state["events"] = []
        return state

    def _append_audit(
        self,
        state: dict[str, Any],
        *,
        action: str,
        pair_id: str,
        reason: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        """Append a bounded interaction audit event to the runtime-state file.

        The pair optimizer has no direct dependency on the V14 Excel history
        manager.  This JSON audit trail therefore records pair-local state
        transitions, including safety suspensions, before any pipeline-level
        candidate can be created.
        """
        event = {
            "timestamp": now(),
            "optimizer_version": self.VERSION,
            "action": str(action),
            "pair_id": str(pair_id),
            "reason": str(reason),
            **json_safe(details or {}),
        }
        events = state.setdefault("events", [])
        events.append(event)
        # Keep a durable but bounded audit tail; historical candidate decisions
        # remain in the V14 pipeline audit files.
        if len(events) > 500:
            del events[:-500]

    def _save_state(self, state: dict[str, Any]) -> None:
        state["metadata"].update({"updated_at": now(), "optimizer_version": self.VERSION})
        write_json_atomic(self.state_file, state)

    def definitions(self) -> pd.DataFrame:
        frame = read_excel_safe(self.definition_file)
        if frame.empty:
            return pd.DataFrame(columns=[
                "pair_id", "parameter_a", "parameter_b", "enabled", "priority",
                "scientific_rationale", "activation_evidence", "allowed_move_patterns",
                "max_trials_per_activation", "max_activations", "cooldown_valid_runs",
                "required_diagnostic_flags", "status",
            ])
        required_defaults = {
            "pair_id": "",
            "parameter_a": "",
            "parameter_b": "",
            "enabled": False,
            "priority": 1000,
            "scientific_rationale": "",
            "activation_evidence": "residual_pattern_or_new_best",
            "allowed_move_patterns": "co_direction",
            "max_trials_per_activation": 2,
            "max_activations": 2,
            "cooldown_valid_runs": 4,
            "required_diagnostic_flags": "",
            "status": "configured",
        }
        out = frame.copy()
        for col, value in required_defaults.items():
            if col not in out.columns:
                out[col] = value
        out["pair_id"] = out["pair_id"].astype(str).str.strip()
        out = out[out["pair_id"].ne("")].copy()
        return out

    @staticmethod
    def _patterns(value: Any) -> list[tuple[str, str]]:
        text = str(value or "co_direction").strip().casefold()
        patterns: list[tuple[str, str]] = []
        if "co_direction" in text or "same" in text or "increase_increase" in text:
            patterns.extend([("increase", "increase"), ("decrease", "decrease")])
        if "opposing" in text or "opposite" in text or "increase_decrease" in text:
            patterns.extend([("increase", "decrease"), ("decrease", "increase")])
        return patterns or [("increase", "increase"), ("decrease", "decrease")]

    @staticmethod
    def _pattern_label(pattern: tuple[str, str]) -> str:
        return f"{pattern[0]}|{pattern[1]}"

    @staticmethod
    def _opposite_pattern(pattern: tuple[str, str]) -> tuple[str, str]:
        swap = {"increase": "decrease", "decrease": "increase"}
        return swap.get(pattern[0], "increase"), swap.get(pattern[1], "increase")

    def _evidence_reason(self, row: pd.Series, evidence: dict[str, Any], valid_run_count: int) -> str:
        """Return an empty string only when the configured evidence gate passes."""
        requested = str(row.get("activation_evidence", "residual_pattern_or_new_best")).strip().casefold()
        if requested == "always":
            return ""
        if requested == "manual":
            return "manual_activation_required"

        flags: set[str] = set()
        residual = evidence.get("v14_residual_diagnostics", evidence.get("residual_diagnostics", {})) if isinstance(evidence, dict) else {}
        if isinstance(residual, dict):
            flags = {str(v).casefold() for v in residual.get("flags", []) or []}
        accepted = bool(evidence.get("last_event_accepted", False)) if isinstance(evidence, dict) else False
        has_residual = bool(flags) or (isinstance(residual, dict) and residual.get("status") == "available")
        raw_required_flags = row.get("required_diagnostic_flags", "")
        # Blank Excel cells are read as NaN; they mean no required flags, not
        # a literal diagnostic flag named "nan".
        if raw_required_flags is None or pd.isna(raw_required_flags):
            raw_required_flags = ""
        required_flags = [x.strip().casefold() for x in str(raw_required_flags).split("|") if x.strip()]
        if required_flags and not set(required_flags).issubset(flags):
            return "required_diagnostic_flags_missing"
        if "new_best" in requested and accepted:
            return ""
        if "residual" in requested and has_residual:
            return ""
        if "after_valid_runs" in requested and valid_run_count > 0:
            return ""
        return "activation_evidence_unsatisfied"

    def _evidence_supports(self, row: pd.Series, evidence: dict[str, Any], valid_run_count: int) -> bool:
        return self._evidence_reason(row, evidence, valid_run_count) == ""

    def _state_for(self, state: dict[str, Any], pair_id: str) -> dict[str, Any]:
        record = state["pairs"].get(pair_id)
        if record is None:
            record = {
                "pair_id": pair_id,
                "phase": "idle",
                "direction_queue": [],
                "pending": False,
                "pending_pattern": "",
                "trial_count": 0,
                "activation_count": 0,
                "last_activation_valid_run": -999999,
                "last_update": now(),
            }
            state["pairs"][pair_id] = record
        return record

    def _pair_eligibility_reason(
        self,
        definition: pd.Series,
        parameters: pd.DataFrame,
        state_manager,
        valid_run_count: int,
        evidence: dict[str, Any],
    ) -> str:
        """Explain why a pair is ineligible; empty means eligible.

        This method is deliberately used both for initial activation and every
        queued continuation.  A pair cannot continue merely because it was
        eligible at activation time.
        """
        if not as_bool(definition.get("enabled"), False):
            return "pair_disabled"
        a, b = str(definition.get("parameter_a", "")).strip(), str(definition.get("parameter_b", "")).strip()
        if not a or not b or normalise_parameter(a) == normalise_parameter(b):
            return "invalid_pair_definition"
        names = {normalise_parameter(v): str(v) for v in parameters.get("parameter", pd.Series(dtype=str)).astype(str).tolist()}
        if normalise_parameter(a) not in names:
            return f"parameter_not_in_active_configuration:{a}"
        if normalise_parameter(b) not in names:
            return f"parameter_not_in_active_configuration:{b}"
        if not state_manager.is_eligible(a, for_interaction=True):
            return f"parameter_not_interaction_eligible:{a}"
        if not state_manager.is_eligible(b, for_interaction=True):
            return f"parameter_not_interaction_eligible:{b}"
        return self._evidence_reason(definition, evidence, valid_run_count)

    def _pair_is_eligible(
        self,
        definition: pd.Series,
        parameters: pd.DataFrame,
        state_manager,
        valid_run_count: int,
        evidence: dict[str, Any],
    ) -> bool:
        return self._pair_eligibility_reason(
            definition, parameters, state_manager, valid_run_count, evidence
        ) == ""

    def _suspend_continuation(
        self,
        state: dict[str, Any],
        definition: pd.Series,
        record: dict[str, Any],
        state_manager,
        *,
        iteration: int,
        reason: str,
    ) -> None:
        """Safely terminate a queued pair when its legal basis has changed.

        No candidate is built.  Any still-active partner is released, the
        queued directions are discarded, and a persistent pair-local audit
        event records why the continuation was blocked.
        """
        pair_id = str(definition.get("pair_id", ""))
        parameters = [
            str(definition.get("parameter_a", "")).strip(),
            str(definition.get("parameter_b", "")).strip(),
        ]
        parameters = [p for p in parameters if p]
        record.update({
            "phase": "completed",
            "direction_queue": [],
            "pending": False,
            "pending_pattern": "",
            "pending_moves": [],
            "last_completion_reason": f"continuation_blocked:{reason}",
            "last_update": now(),
        })
        try:
            state_manager.release_interaction(
                parameters,
                iteration=iteration,
                reason=f"interaction_continuation_blocked:{pair_id}:{reason}",
            )
        finally:
            self._append_audit(
                state,
                action="interaction_pair_continuation_suspended",
                pair_id=pair_id,
                reason=reason,
                details={"parameters": "|".join(parameters)},
            )

    def dry_run(
        self,
        *,
        parameters: pd.DataFrame,
        state_manager,
        evidence: dict[str, Any],
        valid_run_count: int,
        iteration: int,
    ) -> dict[str, Any]:
        """Inspect interaction eligibility without mutating JSON or runtime state.

        This method does not call ``activate_interaction``, ``release_interaction``,
        ``_build_candidate``, or ``_save_state``.  It is safe for release
        validation and reports the first legal continuation/activation that the
        mutable selector would consider.
        """
        del iteration  # Included for API symmetry and audit callers.
        definitions = self.definitions()
        state = self._load_state()
        report: list[dict[str, Any]] = []
        if definitions.empty:
            return {
                "dry_run": True,
                "optimizer_version": self.VERSION,
                "selected_pair": None,
                "reason": "no_interaction_definitions",
                "evaluations": [],
            }

        # A queued pair direction has the same legal precedence as in the
        # mutable selector, but dry-run only reports it; it never resumes it.
        for _, definition in definitions.sort_values(["priority", "pair_id"]).iterrows():
            pair_id = str(definition["pair_id"])
            record = dict(state.get("pairs", {}).get(pair_id, {}))
            queue = [tuple(str(x).split("|", 1)) for x in record.get("direction_queue", []) if "|" in str(x)]
            if record.get("pending"):
                report.append({"pair_id": pair_id, "disposition": "pending", "reason": "pair_candidate_already_pending"})
                continue
            if record.get("phase") in {"symmetric", "directional_continue", "directional_opposite"} and queue:
                reason = self._pair_eligibility_reason(definition, parameters, state_manager, valid_run_count, evidence)
                item = {
                    "pair_id": pair_id,
                    "disposition": "would_continue" if not reason else "blocked_continuation",
                    "reason": reason,
                    "pair_direction": self._pattern_label(queue[0]),
                    "parameter_a": str(definition.get("parameter_a", "")),
                    "parameter_b": str(definition.get("parameter_b", "")),
                }
                report.append(item)
                if not reason:
                    return {
                        "dry_run": True,
                        "optimizer_version": self.VERSION,
                        "selected_pair": item,
                        "reason": "legal_queued_pair_continuation",
                        "evaluations": report,
                    }

        # No legal continuation: inspect fresh activations under the same
        # evidence/budget/cooldown contract used by next_candidate().
        candidates: list[tuple[int, str, dict[str, Any]]] = []
        for _, definition in definitions.iterrows():
            pair_id = str(definition["pair_id"])
            record = dict(state.get("pairs", {}).get(pair_id, {}))
            if record.get("pending"):
                report.append({"pair_id": pair_id, "disposition": "blocked", "reason": "pair_candidate_already_pending"})
                continue
            if record.get("phase", "idle") not in {"idle", "completed"}:
                report.append({"pair_id": pair_id, "disposition": "blocked", "reason": f"pair_phase_not_restartable:{record.get('phase')}"})
                continue
            reason = self._pair_eligibility_reason(definition, parameters, state_manager, valid_run_count, evidence)
            if reason:
                report.append({"pair_id": pair_id, "disposition": "blocked", "reason": reason})
                continue
            max_activations = int(as_float(definition.get("max_activations"), 2) or 2)
            cooldown = int(as_float(definition.get("cooldown_valid_runs"), 4) or 4)
            if int(record.get("activation_count", 0) or 0) >= max_activations:
                report.append({"pair_id": pair_id, "disposition": "blocked", "reason": "activation_budget_exhausted"})
                continue
            if valid_run_count - int(record.get("last_activation_valid_run", -999999) or -999999) < cooldown:
                report.append({"pair_id": pair_id, "disposition": "blocked", "reason": "cooldown_active"})
                continue
            patterns = self._patterns(definition.get("allowed_move_patterns"))
            item = {
                "pair_id": pair_id,
                "disposition": "would_activate",
                "reason": "legal_evidence_gated_pair",
                "pair_direction": self._pattern_label(patterns[0]),
                "parameter_a": str(definition.get("parameter_a", "")),
                "parameter_b": str(definition.get("parameter_b", "")),
                "priority": int(as_float(definition.get("priority"), 1000) or 1000),
            }
            report.append(item)
            candidates.append((item["priority"], pair_id, item))

        if candidates:
            selected = sorted(candidates, key=lambda item: (item[0], item[1]))[0][2]
            return {
                "dry_run": True,
                "optimizer_version": self.VERSION,
                "selected_pair": selected,
                "reason": "legal_fresh_pair_activation",
                "evaluations": report,
            }
        return {
            "dry_run": True,
            "optimizer_version": self.VERSION,
            "selected_pair": None,
            "reason": "no_legal_interaction_pair",
            "evaluations": report,
        }

    def _build_candidate(
        self,
        definition: pd.Series,
        pair_state: dict[str, Any],
        parameters: pd.DataFrame,
        step_controller,
        state_manager,
        pattern: tuple[str, str],
        valid_run_count: int,
        iteration: int,
    ) -> dict[str, Any] | None:
        a, b = str(definition["parameter_a"]).strip(), str(definition["parameter_b"]).strip()
        lookup = parameters.copy()
        lookup["_key"] = lookup["parameter"].map(normalise_parameter)
        lookup = lookup.set_index("_key")
        moves: list[dict[str, Any]] = []
        for parameter, direction in ((a, pattern[0]), (b, pattern[1])):
            p = lookup.loc[normalise_parameter(parameter)]
            base = as_float(p.get("value"))
            if base is None:
                return None
            move = step_controller.propose(
                parameter=parameter,
                base_value=base,
                direction=direction,
                lower=as_float(p.get("min")),
                upper=as_float(p.get("max")),
            )
            if bool(move.get("blocked_noop", move["new_value"] == base)):
                state_manager.mark_direction_bounded(parameter, direction, iteration=iteration)
                pair_state["last_blocked_parameter"] = parameter
                pair_state["last_blocked_direction"] = direction
                pair_state["last_blocked_reason"] = move.get("bound_reason", "candidate_effectively_equals_baseline")
                pair_state["last_blocked_noop_delta"] = move.get("noop_delta")
                pair_state["last_blocked_noop_tolerance"] = move.get("noop_tolerance")
                pair_state["last_update"] = now()
                return None
            moves.append(move)

        pair_id = str(definition["pair_id"])
        pair_state["pending"] = True
        pair_state["pending_pattern"] = self._pattern_label(pattern)
        pair_state["pending_moves"] = json_safe(moves)
        pair_state["pending_at_valid_run"] = valid_run_count
        pair_state["last_update"] = now()
        return {
            "candidate_type": "interaction_pair",
            "pair_id": pair_id,
            "parameter_a": a,
            "parameter_b": b,
            "pair_direction": self._pattern_label(pattern),
            "moves": moves,
            "phase": pair_state.get("phase", "symmetric"),
            "group": str(definition.get("group", "interaction")),
            "priority": int(as_float(definition.get("priority"), 1000) or 1000),
            "scientific_rationale": str(definition.get("scientific_rationale", "")),
        }

    def next_candidate(
        self,
        *,
        parameters: pd.DataFrame,
        state_manager,
        step_controller,
        evidence: dict[str, Any],
        valid_run_count: int,
        iteration: int,
    ) -> dict[str, Any] | None:
        definitions = self.definitions()
        if definitions.empty:
            return None
        state = self._load_state()

        # First finish an existing pair direction family.  This is analogous to
        # the single-parameter direction-pair guard and takes precedence over
        # starting a fresh pair.
        for _, definition in definitions.sort_values(["priority", "pair_id"]).iterrows():
            pair_id = str(definition["pair_id"])
            record = self._state_for(state, pair_id)
            if record.get("pending"):
                continue
            queue = [tuple(x.split("|", 1)) for x in record.get("direction_queue", []) if "|" in str(x)]
            if record.get("phase") in {"symmetric", "directional_continue", "directional_opposite"} and queue:
                continuation_reason = self._pair_eligibility_reason(
                    definition, parameters, state_manager, valid_run_count, evidence
                )
                if continuation_reason:
                    self._suspend_continuation(
                        state, definition, record, state_manager,
                        iteration=iteration, reason=continuation_reason,
                    )
                    continue
                candidate = self._build_candidate(
                    definition, record, parameters, step_controller, state_manager,
                    queue[0], valid_run_count, iteration,
                )
                if candidate:
                    queue.pop(0)
                    record["direction_queue"] = [self._pattern_label(x) for x in queue]
                    state_manager.activate_interaction([candidate["parameter_a"], candidate["parameter_b"]], iteration=iteration, pair_id=pair_id)
                    self._save_state(state)
                    return candidate

        # No existing pair direction family: selectively activate one supported
        # pair only when evidence and cooldown budgets allow it.
        candidates: list[tuple[int, pd.Series, dict[str, Any]]] = []
        for _, definition in definitions.iterrows():
            pair_id = str(definition["pair_id"])
            record = self._state_for(state, pair_id)
            if record.get("pending") or record.get("phase") not in {"idle", "completed"}:
                continue
            if not self._pair_is_eligible(definition, parameters, state_manager, valid_run_count, evidence):
                continue
            max_activations = int(as_float(definition.get("max_activations"), 2) or 2)
            cooldown = int(as_float(definition.get("cooldown_valid_runs"), 4) or 4)
            if int(record.get("activation_count", 0) or 0) >= max_activations:
                continue
            if valid_run_count - int(record.get("last_activation_valid_run", -999999) or -999999) < cooldown:
                continue
            candidates.append((int(as_float(definition.get("priority"), 1000) or 1000), definition, record))

        if not candidates:
            self._save_state(state)
            return None
        _, definition, record = sorted(candidates, key=lambda item: (item[0], str(item[1]["pair_id"])))[0]
        patterns = self._patterns(definition.get("allowed_move_patterns"))
        record.update(
            {
                "phase": "symmetric",
                "direction_queue": [self._pattern_label(x) for x in patterns],
                "pending": False,
                "pending_pattern": "",
                "pending_moves": [],
                "trial_count": 0,
                "activation_count": int(record.get("activation_count", 0) or 0) + 1,
                "last_activation_valid_run": valid_run_count,
                "last_update": now(),
            }
        )
        self._save_state(state)
        return self.next_candidate(
            parameters=parameters,
            state_manager=state_manager,
            step_controller=step_controller,
            evidence=evidence,
            valid_run_count=valid_run_count,
            iteration=iteration,
        )

    def observe_result(
        self,
        *,
        pair_id: str,
        accepted: bool,
        valid: bool,
        reason: str,
        iteration: int,
    ) -> dict[str, Any]:
        state = self._load_state()
        record = self._state_for(state, pair_id)
        pattern_label = str(record.get("pending_pattern", ""))
        pattern = tuple(pattern_label.split("|", 1)) if "|" in pattern_label else ("increase", "increase")
        queue = [str(v) for v in record.get("direction_queue", [])]
        phase = str(record.get("phase", "symmetric"))
        record["pending"] = False
        record["pending_pattern"] = ""
        record["trial_count"] = int(record.get("trial_count", 0) or 0) + 1
        action = ""
        pair_complete = False

        if accepted:
            record["phase"] = "directional_continue"
            record["direction_queue"] = [self._pattern_label(pattern)]
            action = "accepted_pair_continue_direction"
        elif queue:
            record["direction_queue"] = queue
            action = "test_remaining_pair_direction"
        elif phase == "directional_continue":
            record["phase"] = "directional_opposite"
            record["direction_queue"] = [self._pattern_label(self._opposite_pattern(pattern))]
            action = "test_pair_opposite_after_directional_failure"
        else:
            record["phase"] = "completed"
            record["direction_queue"] = []
            record["last_completion_reason"] = reason
            pair_complete = True
            action = "complete_pair_direction_family"

        definitions = self.definitions()
        match = definitions[definitions["pair_id"].astype(str).eq(pair_id)]
        max_trials = 2
        if not match.empty:
            max_trials = int(as_float(match.iloc[0].get("max_trials_per_activation"), 2) or 2)
        # Trial limits are absolute.  An accepted final trial is retained as a
        # valid best candidate, but the pair is not allowed to keep expanding
        # indefinitely within the same activation.
        if int(record.get("trial_count", 0) or 0) >= max_trials:
            record["phase"] = "completed"
            record["direction_queue"] = []
            pair_complete = True
            action = "pair_trial_budget_reached_after_accept" if accepted else "pair_trial_budget_reached"

        record["last_update"] = now()
        self._append_audit(
            state,
            action="interaction_pair_result_observed",
            pair_id=pair_id,
            reason=reason,
            details={
                "accepted": bool(accepted),
                "valid": bool(valid),
                "pair_action": action,
                "pair_complete": bool(pair_complete),
            },
        )
        self._save_state(state)
        return {
            "pair_id": pair_id,
            "pair_direction": pattern_label,
            "pair_phase": phase,
            "pair_action": action,
            "pair_complete": pair_complete,
            "pair_trial_count": record.get("trial_count"),
            "pair_reason": reason,
            "pair_valid": bool(valid),
        }

    def snapshot(self) -> dict[str, Any]:
        return json_safe(self._load_state())
