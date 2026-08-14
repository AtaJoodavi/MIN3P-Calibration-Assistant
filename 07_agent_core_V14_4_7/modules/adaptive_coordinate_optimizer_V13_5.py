from __future__ import annotations
"""V13.5 isolated staged coordinate optimizer.

V13.5 never changes the user-controlled ``status`` column in agent_config.xlsx.
Temporary optimizer status exists only in v13_5_optimizer_parameter_state.xlsx.

Search rules:
- symmetric bracketing tests increase/decrease at the same magnitude;
- accepted moves continue in the same multiplicative-factor direction;
- after that continuation fails, the opposite direction is tested at the same
  magnitude around the new best;
- only then is the step reduced symmetrically;
- completed parameters can be reopened after a material accepted change.
"""
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import uuid
import pandas as pd
from modules.v13_parameter_groups import assign_groups, ordered_groups

def _f(value: Any, default=None):
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default

def _b(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}

def _now() -> str:
    return pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")

@dataclass
class OptimizerSettings:
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

class AdaptiveCoordinateOptimizerV135:
    VERSION = "V13.5"

    def __init__(self, paths, config, settings: OptimizerSettings | None = None):
        self.paths, self.config = paths, config
        self.settings = settings or self._settings()
        self.memory_file = paths.results_dir / "v13_5_optimizer_parameter_state.xlsx"
        self.state_file = paths.results_dir / "v13_5_optimizer_state.xlsx"
        self.event_file = paths.results_dir / "v13_5_optimizer_events.xlsx"
        self.decision_file = paths.results_dir / "v13_5_candidate_decisions.xlsx"

    def _settings(self):
        settings = OptimizerSettings()
        values = self.config.optimizer_v13() if hasattr(self.config, "optimizer_v13") else {}
        for field in settings.__dataclass_fields__:
            default = getattr(settings, field)
            raw = _f(values.get(field, default), default)
            setattr(settings, field, int(raw) if field.startswith(("max_", "reopen_")) else float(raw))
        settings.minimum_step_fraction = max(settings.minimum_step_fraction, 1e-12)
        settings.maximum_step_fraction = max(settings.maximum_step_fraction, settings.minimum_step_fraction)
        settings.step_growth_factor = max(settings.step_growth_factor, 1.0)
        settings.step_shrink_factor = min(max(settings.step_shrink_factor, 1e-12), 1.0)
        return settings

    @staticmethod
    def _read(path: Path) -> pd.DataFrame:
        try:
            return pd.read_excel(path) if path.exists() else pd.DataFrame()
        except Exception:
            return pd.DataFrame()

    @staticmethod
    def _write(path: Path, frame: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_excel(path, index=False)

    def _read_state(self) -> dict[str, Any]:
        frame = self._read(self.state_file)
        return {} if frame.empty else frame.iloc[0].to_dict()

    def _write_state(self, state: dict[str, Any]) -> None:
        self._write(self.state_file, pd.DataFrame([{"updated_at": _now(), "optimizer_version": self.VERSION, **state}]))

    def _read_memory(self) -> pd.DataFrame:
        return self._read(self.memory_file)

    def _write_memory(self, memory: pd.DataFrame) -> None:
        self._write(self.memory_file, memory)

    def _event(self, payload: dict[str, Any]) -> None:
        existing = self._read(self.event_file)
        row = pd.DataFrame([{"timestamp": _now(), "optimizer_version": self.VERSION, **payload}])
        self._write(self.event_file, pd.concat([existing, row], ignore_index=True))

    def _params(self) -> pd.DataFrame:
        params = self.config.parameters().copy()
        if "status" in params.columns:
            active = params["status"].astype(str).str.strip().str.lower().isin({"active", "yes", "true", "1"})
            params = params.loc[active].copy()
        params = params.dropna(subset=["parameter", "value"])
        params = assign_groups(params, self.config)
        return params.loc[params["v13_group_enabled"]].copy()

    @staticmethod
    def _queue_items(value: Any) -> list[str]:
        if value is None or pd.isna(value):
            return []
        return [x for x in str(value).split("|") if x in {"increase", "decrease"}]

    @staticmethod
    def _opposite(direction: str) -> str:
        return "decrease" if direction == "increase" else "increase"

    def _ensure_memory(self) -> pd.DataFrame:
        memory = self._read_memory()
        params = self._params()
        if memory.empty:
            memory = pd.DataFrame()
        known = set(memory.get("parameter", pd.Series(dtype=str)).astype(str))
        rows = []
        for _, p in params.iterrows():
            name = str(p["parameter"]).strip()
            if name in known:
                continue
            rows.append({
                "parameter": name, "group": p["v13_group"], "group_order": int(p["v13_group_order"]),
                "priority": int(p["v13_priority"]), "status": "unexplored", "phase": "symmetric",
                "pass_number": 1, "step_fraction": self.settings.initial_step_fraction,
                "direction_queue": "increase|decrease", "continuation_direction": "",
                "tested_increase": False, "tested_decrease": False, "pending": False,
                "pending_candidate_id": "", "pending_direction": "", "pending_step_fraction": None,
                "pending_old_value": None, "pending_new_value": None, "pending_factor_applied": None,
                "pending_parent_best_run_folder": "", "last_completion_at": "", "reopen_count": 0,
                "reopened_after_parameter": "", "invalid_count": 0, "accepted_improvement_count": 0,
                "sensitivity_score": None, "last_update": _now(),
            })
        if rows:
            memory = pd.concat([memory, pd.DataFrame(rows)], ignore_index=True)
            self._write_memory(memory)
        return memory

    def initialize(self, total_score: float, run_folder: str, baseline_source: str = "run_ranking.xlsx") -> dict[str, Any]:
        existing = self._read_state()
        if _f(existing.get("current_best_score")) is not None:
            return existing
        groups = ordered_groups(self._params())
        state = {
            "campaign_id": uuid.uuid4().hex[:12],
            "current_best_score": float(total_score),
            "current_best_run_folder": str(run_folder),
            "baseline_source": baseline_source,
            "current_stage": groups[0] if groups else "other",
            "stage_index": 0, "pass_number": 1, "accepted_improvements_count": 0,
            "rejected_candidates_count": 0, "invalid_candidates_count": 0,
            "reopened_parameters_count": 0, "convergence_status": "running",
            "convergence_reason": "", "last_action": "initialized", "last_parameter": "",
            "last_direction": "", "last_step_fraction": None,
        }
        self._ensure_memory()
        self._write_state(state)
        self._event({"action": "initialized", "TOTAL_SCORE": total_score, "run_folder": run_folder,
                     "stage": state["current_stage"], "baseline_source": baseline_source})
        return state

    def current_best_score(self):
        return _f(self._read_state().get("current_best_score"))

    def _start_next_pass(self, memory: pd.DataFrame, state: dict[str, Any]):
        next_pass = int(_f(state.get("pass_number"), 1)) + 1
        if next_pass > self.settings.max_passes:
            state.update({"convergence_status": "converged", "convergence_reason": "all groups completed all configured passes",
                          "last_action": "scientific_convergence"})
            self._event({"action": "scientific_convergence", "reason": state["convergence_reason"]})
            return memory, state, False
        groups = ordered_groups(self._params())
        done = memory["status"].astype(str).isin({"pass_complete", "bounded"})
        memory.loc[done, "status"] = "unexplored"
        memory.loc[done, "phase"] = "symmetric"
        memory.loc[done, "direction_queue"] = "increase|decrease"
        memory.loc[done, "tested_increase"] = False
        memory.loc[done, "tested_decrease"] = False
        memory.loc[done, "step_fraction"] = pd.to_numeric(memory.loc[done, "step_fraction"], errors="coerce").fillna(self.settings.initial_step_fraction).clip(lower=self.settings.minimum_step_fraction)
        state.update({"pass_number": next_pass, "stage_index": 0, "current_stage": groups[0] if groups else "other",
                      "last_action": "start_next_pass"})
        self._event({"action": "start_next_pass", "pass_number": next_pass})
        return memory, state, True

    def _advance_stage(self, memory: pd.DataFrame, state: dict[str, Any]):
        groups = ordered_groups(self._params())
        stage = str(state.get("current_stage", "other"))
        idx = groups.index(stage) if stage in groups else -1
        if idx + 1 < len(groups):
            state.update({"current_stage": groups[idx + 1], "stage_index": idx + 1, "last_action": "advance_stage"})
            self._event({"action": "advance_stage", "stage": state["current_stage"]})
            return memory, state, True
        return self._start_next_pass(memory, state)

    def _candidate_index(self, memory: pd.DataFrame, state: dict[str, Any]):
        stage = str(state.get("current_stage", "other"))
        active = memory[(memory["group"].astype(str) == stage)
                        & ~memory["status"].astype(str).isin({"pass_complete", "bounded", "inactive"})
                        & ~memory["pending"].map(_b)].copy()
        if active.empty:
            memory, state, keep = self._advance_stage(memory, state)
            return (None, memory, state) if not keep else self._candidate_index(memory, state)
        # A direction pair must always be completed before selecting a new
        # parameter.  Without this precedence, a rejected Kz increase could
        # incorrectly be followed by bottom_head increase instead of Kz decrease.
        active["_must_complete_pair"] = (
            active["status"].astype(str).isin({
                "test_remaining_direction",
                "test_opposite_after_directional_failure",
            })
            | active["phase"].astype(str).isin({
                "directional_continue",
                "directional_opposite",
            })
        ).map({True: 0, False: 1})
        active["_sens"] = pd.to_numeric(active.get("sensitivity_score"), errors="coerce").fillna(-1.0)
        selected = active.sort_values(
            ["_must_complete_pair", "priority", "_sens", "last_update", "parameter"],
            ascending=[True, True, False, True, True],
        ).index[0]
        return int(selected), memory, state

    def _finish_or_refine(self, memory: pd.DataFrame, idx: int, state: dict[str, Any], *, reason: str):
        step = _f(memory.at[idx, "step_fraction"], self.settings.initial_step_fraction)
        if step <= self.settings.minimum_step_fraction + 1e-15:
            memory.at[idx, "status"] = "pass_complete"
            memory.at[idx, "phase"] = "complete"
            memory.at[idx, "direction_queue"] = ""
            memory.at[idx, "last_completion_at"] = _now()
            return "finish_parameter_for_pass"
        new_step = max(step * self.settings.step_shrink_factor, self.settings.minimum_step_fraction)
        memory.at[idx, "step_fraction"] = new_step
        memory.at[idx, "phase"] = "symmetric"
        memory.at[idx, "direction_queue"] = "increase|decrease"
        memory.at[idx, "tested_increase"] = False
        memory.at[idx, "tested_decrease"] = False
        memory.at[idx, "status"] = "refine_symmetric"
        return "shrink_step_after_symmetric_bracket"

    def _reopen_after_accept(self, memory: pd.DataFrame, state: dict[str, Any], accepted_parameter: str, accepted_group: str):
        candidates = memory["status"].astype(str).eq("pass_complete") & memory["parameter"].astype(str).ne(accepted_parameter)
        allow = pd.Series(False, index=memory.index)
        if _b(self.settings.reopen_same_group_after_accept):
            allow |= memory["group"].astype(str).eq(accepted_group)
        if _b(self.settings.reopen_hydraulic_after_any_accept):
            allow |= memory["group"].astype(str).eq("hydraulic")
        target = memory.index[candidates & allow]
        if len(target) == 0:
            return []
        for i in target:
            memory.at[i, "status"] = "reopened"
            memory.at[i, "phase"] = "symmetric"
            memory.at[i, "direction_queue"] = "increase|decrease"
            memory.at[i, "tested_increase"] = False
            memory.at[i, "tested_decrease"] = False
            memory.at[i, "reopen_count"] = int(_f(memory.at[i, "reopen_count"], 0)) + 1
            memory.at[i, "reopened_after_parameter"] = accepted_parameter
            memory.at[i, "last_update"] = _now()
        names = memory.loc[target, "parameter"].astype(str).tolist()
        state["reopened_parameters_count"] = int(_f(state.get("reopened_parameters_count"), 0)) + len(names)
        self._event({"action": "reopen_after_accepted_improvement", "accepted_parameter": accepted_parameter,
                     "accepted_group": accepted_group, "reopened_parameters": "|".join(names)})
        return names

    def next_suggestion(self) -> pd.DataFrame:
        state = self._read_state()
        if _f(state.get("current_best_score")) is None or str(state.get("convergence_status", "")).lower() == "converged":
            return pd.DataFrame()
        memory = self._ensure_memory()
        if memory.get("pending", pd.Series(dtype=bool)).map(_b).any():
            return pd.DataFrame()
        idx, memory, state = self._candidate_index(memory, state)
        if idx is None:
            self._write_memory(memory); self._write_state(state)
            return pd.DataFrame()

        params = self._params().set_index("parameter")
        parameter = str(memory.at[idx, "parameter"])
        if parameter not in params.index:
            memory.at[idx, "status"] = "inactive"
            self._write_memory(memory)
            return self.next_suggestion()

        queue = self._queue_items(memory.at[idx, "direction_queue"])
        if not queue:
            phase = str(memory.at[idx, "phase"])
            if phase == "directional_continue":
                direction = str(memory.at[idx, "continuation_direction"])
                memory.at[idx, "phase"] = "directional_opposite"
                memory.at[idx, "direction_queue"] = self._opposite(direction)
                memory.at[idx, "status"] = "test_opposite_after_directional_failure"
                queue = self._queue_items(memory.at[idx, "direction_queue"])
            elif phase == "directional_opposite":
                self._finish_or_refine(memory, idx, state, reason="directional_pair_failed")
                self._write_memory(memory)
                return self.next_suggestion()
            else:
                self._finish_or_refine(memory, idx, state, reason="symmetric_pair_failed")
                self._write_memory(memory)
                return self.next_suggestion()

        direction = queue[0]
        base = _f(params.at[parameter, "value"])
        step = _f(memory.at[idx, "step_fraction"], self.settings.initial_step_fraction)
        factor = 1.0 + step if direction == "increase" else 1.0 - step
        candidate = base * factor
        lower = _f(params.at[parameter, "min"]) if "min" in params.columns else None
        upper = _f(params.at[parameter, "max"]) if "max" in params.columns else None
        if lower is not None: candidate = max(candidate, lower)
        if upper is not None: candidate = min(candidate, upper)
        queue.remove(direction)
        memory.at[idx, "direction_queue"] = "|".join(queue)

        if candidate == base:
            if not queue:
                self._finish_or_refine(memory, idx, state, reason="parameter_bound")
            memory.at[idx, "status"] = "bounded"
            self._write_memory(memory)
            return self.next_suggestion()

        candidate_id = uuid.uuid4().hex
        memory.at[idx, "pending"] = True
        memory.at[idx, "pending_candidate_id"] = candidate_id
        memory.at[idx, "pending_direction"] = direction
        memory.at[idx, "pending_step_fraction"] = step
        memory.at[idx, "pending_old_value"] = base
        memory.at[idx, "pending_new_value"] = candidate
        memory.at[idx, "pending_factor_applied"] = candidate / base if base else 1.0
        memory.at[idx, "pending_parent_best_run_folder"] = str(state.get("current_best_run_folder", ""))
        memory.at[idx, "status"] = f"test_{direction}"
        memory.at[idx, "last_update"] = _now()
        self._write_memory(memory)

        state.update({"last_action": "candidate_selected", "last_parameter": parameter, "last_direction": direction, "last_step_fraction": step})
        self._write_state(state)
        self._event({"action": "candidate_selected", "candidate_id": candidate_id, "parameter": parameter,
                     "group": memory.at[idx, "group"], "direction": direction, "direction_label": direction,
                     "step_fraction": step, "old_value": base, "new_value": candidate, "numeric_change": candidate - base,
                     "factor_applied": candidate / base if base else 1.0, "baseline_objective": state.get("current_best_score"),
                     "parent_best_run_folder": state.get("current_best_run_folder", ""), "pass_number": state.get("pass_number", 1),
                     "phase": memory.at[idx, "phase"]})
        return pd.DataFrame([{
            "candidate_id": candidate_id, "parameter": parameter, "old_value": base, "new_value": candidate,
            "numeric_change": candidate - base, "factor_requested": factor, "factor_applied": candidate / base if base else 1.0,
            "priority": int(memory.at[idx, "priority"]), "reason": f"V13.5 {memory.at[idx, 'phase']}: {memory.at[idx, 'group']}; {direction}; step={step:.6g}",
            "evidence": f"current_best_TOTAL_SCORE={state.get('current_best_score')}",
            "optimizer_version": self.VERSION, "optimizer_mode": "staged_directional_symmetric_coordinate_search",
            "group": memory.at[idx, "group"], "direction": direction, "direction_label": direction,
            "step_fraction_requested": step, "step_fraction_applied": step, "pass_number": state.get("pass_number", 1),
            "parent_best_run_folder": state.get("current_best_run_folder", ""), "search_phase": memory.at[idx, "phase"],
        }])

    def observe_run(self, candidate_objective, run_folder="", scientific_ok=True, scientific_penalty=0.0,
                    valid=True, diagnostics=None, min3p_run_status=""):
        memory = self._ensure_memory()
        pending = memory[memory.get("pending", pd.Series(dtype=bool)).map(_b)]
        if pending.empty:
            return {"action": "baseline_observed", "accepted": False, "optimizer_version": self.VERSION}
        idx = int(pending.index[-1])
        row = memory.loc[idx].copy()
        state = self._read_state()
        baseline = _f(state.get("current_best_score"))
        candidate = _f(candidate_objective)
        penalty = float(scientific_penalty or 0.0)
        effective = None if candidate is None else candidate + penalty
        improvement = None if effective is None or baseline is None else baseline - effective
        absolute = improvement or 0.0
        relative = absolute / max(abs(baseline or 1.0), 1e-30)
        meaningful = bool(valid) and bool(scientific_ok) and candidate is not None and absolute >= self.settings.min_absolute_improvement and relative >= self.settings.min_relative_improvement
        direction = str(row.get("pending_direction", ""))
        step = _f(row.get("pending_step_fraction"), self.settings.initial_step_fraction)
        phase = str(row.get("phase", "symmetric"))
        parameter, group = str(row.get("parameter", "")), str(row.get("group", ""))

        for c in ["pending", "pending_candidate_id", "pending_direction", "pending_step_fraction", "pending_old_value", "pending_new_value", "pending_factor_applied", "pending_parent_best_run_folder"]:
            memory.at[idx, c] = False if c == "pending" else (None if c in {"pending_step_fraction", "pending_old_value", "pending_new_value", "pending_factor_applied"} else "")
        memory.at[idx, f"tested_{direction}"] = True
        memory.at[idx, "last_update"] = _now()

        event = {
            "candidate_id": str(row.get("pending_candidate_id", "")), "run_folder": str(run_folder),
            "parent_best_run_folder": str(row.get("pending_parent_best_run_folder", "")),
            "parameter": parameter, "group": group, "old_value": _f(row.get("pending_old_value")),
            "new_value": _f(row.get("pending_new_value")), "numeric_change": (_f(row.get("pending_new_value"), 0.0) - _f(row.get("pending_old_value"), 0.0)),
            "factor_applied": _f(row.get("pending_factor_applied")), "direction": direction, "direction_label": direction,
            "step_fraction": step, "search_phase": phase, "baseline_TOTAL_SCORE": baseline, "candidate_TOTAL_SCORE": candidate,
            "effective_TOTAL_SCORE": effective, "objective_improved": bool(candidate is not None and baseline is not None and candidate < baseline),
            "improvement_absolute": absolute, "improvement_relative": relative, "scientific_ok": bool(scientific_ok),
            "scientific_penalty": penalty, "MIN3P_run_status": min3p_run_status, "valid_run": bool(valid),
            **(diagnostics or {}),
        }

        if meaningful:
            memory.at[idx, "accepted_improvement_count"] = int(_f(row.get("accepted_improvement_count"), 0)) + 1
            memory.at[idx, "sensitivity_score"] = abs(candidate - baseline) / max(step, 1e-30)
            new_step = min(step * self.settings.step_growth_factor, self.settings.maximum_step_fraction)
            memory.at[idx, "step_fraction"] = new_step
            memory.at[idx, "phase"] = "directional_continue"
            memory.at[idx, "continuation_direction"] = direction
            memory.at[idx, "direction_queue"] = direction
            memory.at[idx, "tested_increase"] = False
            memory.at[idx, "tested_decrease"] = False
            memory.at[idx, "status"] = f"continue_{direction}"
            state["current_best_score"] = candidate
            state["current_best_run_folder"] = str(run_folder)
            state["accepted_improvements_count"] = int(_f(state.get("accepted_improvements_count"), 0)) + 1
            reopened = self._reopen_after_accept(memory, state, parameter, group)
            event.update({"action": "accepted_meaningful_improvement_continue_direction", "accepted": True, "decision": "accept",
                          "rejection_reason": "", "next_step_fraction": new_step, "reopened_parameters": "|".join(reopened)})
        else:
            if not valid:
                reason = "invalid_or_failed_MIN3P_run"; action = "mark_infeasible_continue"
                state["invalid_candidates_count"] = int(_f(state.get("invalid_candidates_count"), 0)) + 1
                memory.at[idx, "invalid_count"] = int(_f(row.get("invalid_count"), 0)) + 1
            elif not scientific_ok:
                reason = "scientific_constraint_failed"; action = "reject_constraint_continue"
            elif candidate is not None and baseline is not None and abs(candidate - baseline) <= self.settings.neutral_absolute_band:
                reason = "near_neutral_change"; action = "reject_neutral_continue"
            else:
                reason = "TOTAL_SCORE_not_meaningfully_improved"; action = "reject_continue"
            state["rejected_candidates_count"] = int(_f(state.get("rejected_candidates_count"), 0)) + 1
            remaining = self._queue_items(memory.at[idx, "direction_queue"])
            if remaining:
                memory.at[idx, "status"] = "test_remaining_direction"
            elif phase == "directional_continue":
                memory.at[idx, "phase"] = "directional_opposite"
                memory.at[idx, "direction_queue"] = self._opposite(direction)
                memory.at[idx, "status"] = "test_opposite_after_directional_failure"
                action = "test_opposite_after_directional_failure"
            elif phase == "directional_opposite":
                action = self._finish_or_refine(memory, idx, state, reason="directional_pair_failed")
            else:
                action = self._finish_or_refine(memory, idx, state, reason="symmetric_pair_failed")
            event.update({"action": action, "accepted": False, "decision": "reject", "rejection_reason": reason})

        state.update({"last_action": event["action"], "last_parameter": parameter, "last_direction": direction, "last_step_fraction": step})
        self._write_memory(memory)
        self._write_state(state)
        self._event(event)
        return {"optimizer_version": self.VERSION, **event}

    def finalize_decision(self, event: dict[str, Any], restoration_verified: bool) -> dict[str, Any]:
        """Append one immutable final-decision row after save/restore has completed."""
        record = {"timestamp": _now(), "optimizer_version": self.VERSION, **event,
                  "restoration_verified": bool(restoration_verified)}
        existing = self._read(self.decision_file)
        self._write(self.decision_file, pd.concat([existing, pd.DataFrame([record])], ignore_index=True))
        self._event({"action": "candidate_decision_finalized", "candidate_id": event.get("candidate_id", ""),
                     "accepted": event.get("accepted", False), "restoration_verified": bool(restoration_verified)})
        return record
