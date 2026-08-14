from __future__ import annotations

"""V13.4 staged coordinate search with symmetric local bracketing.

For each parameter and step size:
  +step and -step are both tested around the same current-best configuration.
Only after both fail does the step shrink. Invalid runs are treated as
infeasible candidates and the campaign continues.
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
    max_consecutive_steps_per_parameter: int = 6
    min_absolute_improvement: float = 0.001
    min_relative_improvement: float = 0.0001
    neutral_absolute_band: float = 0.0005
    max_passes: int = 2


class AdaptiveCoordinateOptimizer:
    VERSION = "V13.4"

    def __init__(self, paths, config, settings: OptimizerSettings | None = None):
        self.paths, self.config = paths, config
        self.settings = settings or self._settings()
        self.memory_file = paths.results_dir / "v13_4_optimizer_parameter_state.xlsx"
        self.state_file = paths.results_dir / "v13_4_optimizer_state.xlsx"
        self.event_file = paths.results_dir / "v13_4_optimizer_events.xlsx"

    def _settings(self):
        settings = OptimizerSettings()
        values = {}
        try:
            values = self.config.optimizer_v13()
        except Exception:
            try:
                table = self.config.sheet("optimizer_v13")
                values = dict(zip(table["setting"].astype(str), table["value"]))
            except Exception:
                pass
        for field in settings.__dataclass_fields__:
            raw = _f(values.get(field, getattr(settings, field)), getattr(settings, field))
            setattr(settings, field, int(raw) if field.startswith("max_") else float(raw))
        settings.minimum_step_fraction = max(settings.minimum_step_fraction, 1e-12)
        settings.maximum_step_fraction = max(settings.maximum_step_fraction, settings.minimum_step_fraction)
        return settings

    @staticmethod
    def _read(path: Path) -> pd.DataFrame:
        try:
            return pd.read_excel(path) if path.exists() else pd.DataFrame()
        except Exception:
            return pd.DataFrame()

    def _write(self, path: Path, frame: pd.DataFrame) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        frame.to_excel(path, index=False)

    def _read_state(self) -> dict[str, Any]:
        frame = self._read(self.state_file)
        return {} if frame.empty else frame.iloc[0].to_dict()

    def _write_state(self, state: dict[str, Any]) -> None:
        state = {"updated_at": _now(), "optimizer_version": self.VERSION, **state}
        self._write(self.state_file, pd.DataFrame([state]))

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
            params = params[params["status"].astype(str).str.strip().str.lower().isin({"active", "yes", "true", "1"})]
        params = params.dropna(subset=["parameter", "value"])
        params = assign_groups(params, self.config)
        return params[params["v13_group_enabled"]].copy()

    def _ensure_memory(self) -> pd.DataFrame:
        memory = self._read_memory()
        params = self._params()
        known = set(memory.get("parameter", pd.Series(dtype=str)).astype(str))
        additions = []
        for _, row in params.iterrows():
            parameter = str(row["parameter"]).strip()
            if parameter in known:
                continue
            additions.append({
                "parameter": parameter,
                "group": row["v13_group"],
                "group_order": int(row["v13_group_order"]),
                "priority": int(row["v13_priority"]),
                "status": "unexplored",
                "pass_number": 1,
                "step_fraction": self.settings.initial_step_fraction,
                "direction_queue": "increase|decrease",
                "tested_increase": False,
                "tested_decrease": False,
                "pending": False,
                "pending_direction": "",
                "pending_step_fraction": None,
                "last_update": _now(),
                "invalid_count": 0,
                "accepted_improvement_count": 0,
                "sensitivity_score": None,
            })
        if additions:
            memory = pd.concat([memory, pd.DataFrame(additions)], ignore_index=True)
            self._write_memory(memory)
        return memory

    def initialize(self, total_score: float, run_folder: str) -> dict[str, Any]:
        existing = self._read_state()
        if _f(existing.get("current_best_score")) is not None:
            return existing
        groups = ordered_groups(self._params())
        state = {
            "campaign_id": uuid.uuid4().hex[:12],
            "current_best_score": float(total_score),
            "current_best_run_folder": str(run_folder),
            "current_stage": groups[0] if groups else "other",
            "stage_index": 0,
            "pass_number": 1,
            "accepted_improvements_count": 0,
            "rejected_candidates_count": 0,
            "invalid_candidates_count": 0,
            "convergence_status": "running",
            "convergence_reason": "",
            "last_action": "initialized",
            "last_parameter": "",
            "last_direction": "",
            "last_step_fraction": None,
        }
        self._ensure_memory()
        self._write_state(state)
        self._event({"action": "initialized", "TOTAL_SCORE": total_score, "run_folder": run_folder, "stage": state["current_stage"]})
        return state

    def current_best_score(self):
        return _f(self._read_state().get("current_best_score"))

    def _advance(self, memory: pd.DataFrame, state: dict[str, Any]):
        groups = ordered_groups(self._params())
        current = str(state.get("current_stage", "other"))
        index = groups.index(current) if current in groups else -1
        if index + 1 < len(groups):
            state["current_stage"], state["stage_index"], state["last_action"] = groups[index + 1], index + 1, "advance_stage"
            self._event({"action": "advance_stage", "stage": state["current_stage"]})
            return memory, state, True

        next_pass = int(_f(state.get("pass_number"), 1)) + 1
        if next_pass > self.settings.max_passes:
            state["convergence_status"] = "converged"
            state["convergence_reason"] = "all groups completed all configured passes"
            state["last_action"] = "scientific_convergence"
            self._event({"action": "scientific_convergence", "reason": state["convergence_reason"]})
            return memory, state, False

        state["pass_number"], state["stage_index"], state["current_stage"], state["last_action"] = next_pass, 0, (groups[0] if groups else "other"), "start_next_pass"
        finished = memory["status"].astype(str).isin({"pass_complete", "bounded"})
        memory.loc[finished, "status"] = "unexplored"
        memory.loc[:, "tested_increase"] = False
        memory.loc[:, "tested_decrease"] = False
        memory.loc[:, "direction_queue"] = "increase|decrease"
        memory.loc[:, "step_fraction"] = (
            pd.to_numeric(memory["step_fraction"], errors="coerce")
            .fillna(self.settings.initial_step_fraction)
            .mul(self.settings.step_shrink_factor)
            .clip(lower=self.settings.minimum_step_fraction)
        )
        self._event({"action": "start_next_pass", "pass_number": next_pass})
        return memory, state, True

    def _candidate_index(self, memory: pd.DataFrame, state: dict[str, Any]):
        stage = str(state.get("current_stage", "other"))
        active = memory[
            (memory["group"].astype(str) == stage)
            & ~memory["status"].astype(str).isin({"pass_complete", "bounded", "inactive"})
            & ~memory["pending"].map(_b)
        ].copy()
        if active.empty:
            memory, state, keep = self._advance(memory, state)
            return (None, memory, state) if not keep else self._candidate_index(memory, state)

        status = active["status"].astype(str)
        active["_continuation"] = status.str.startswith(("test_", "bracket_", "refine_")).map({True: 0, False: 1})
        active["_sens"] = pd.to_numeric(active.get("sensitivity_score"), errors="coerce").fillna(-1.0)
        selected = active.sort_values(
            ["_continuation", "priority", "_sens", "last_update", "parameter"],
            ascending=[True, True, False, True, True],
        ).index[0]
        return int(selected), memory, state

    def _queue_items(self, value: Any) -> list[str]:
        """Return only valid queued directions; Excel blanks are read as NaN."""
        if value is None or pd.isna(value):
            return []
        return [item for item in str(value).split("|") if item in {"increase", "decrease"}]

    def _next_direction(self, row: pd.Series) -> str | None:
        queue = self._queue_items(row.get("direction_queue", ""))
        return queue[0] if queue else None

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

        direction = self._next_direction(memory.loc[idx])
        if direction is None:
            step = _f(memory.at[idx, "step_fraction"], self.settings.initial_step_fraction)
            if step <= self.settings.minimum_step_fraction:
                memory.at[idx, "status"] = "pass_complete"
                memory.at[idx, "last_update"] = _now()
                self._write_memory(memory)
                return self.next_suggestion()
            step = max(step * self.settings.step_shrink_factor, self.settings.minimum_step_fraction)
            memory.at[idx, "step_fraction"] = step
            memory.at[idx, "direction_queue"] = "increase|decrease"
            memory.at[idx, "tested_increase"] = False
            memory.at[idx, "tested_decrease"] = False
            memory.at[idx, "status"] = "refine_symmetric"
            direction = "increase"

        base = _f(params.at[parameter, "value"])
        step = _f(memory.at[idx, "step_fraction"], self.settings.initial_step_fraction)
        factor = 1.0 + step if direction == "increase" else 1.0 - step
        candidate = base * factor
        lower = _f(params.at[parameter, "min"]) if "min" in params.columns else None
        upper = _f(params.at[parameter, "max"]) if "max" in params.columns else None
        if lower is not None: candidate = max(candidate, lower)
        if upper is not None: candidate = min(candidate, upper)
        if candidate == base:
            queue = [x for x in str(memory.at[idx, "direction_queue"]).split("|") if x and x != direction]
            memory.at[idx, "direction_queue"] = "|".join(queue)
            memory.at[idx, "status"] = "bounded"
            self._write_memory(memory)
            return self.next_suggestion()

        queue = self._queue_items(memory.at[idx, "direction_queue"])
        if direction in queue:
            queue.remove(direction)
        memory.at[idx, "direction_queue"] = "|".join(queue)
        memory.at[idx, "pending"] = True
        memory.at[idx, "pending_direction"] = direction
        memory.at[idx, "pending_step_fraction"] = step
        memory.at[idx, "status"] = f"test_{direction}"
        memory.at[idx, "last_update"] = _now()
        self._write_memory(memory)

        state.update({"last_action": "candidate_selected", "last_parameter": parameter, "last_direction": direction, "last_step_fraction": step})
        self._write_state(state)
        self._event({
            "action": "candidate_selected", "parameter": parameter, "group": memory.at[idx, "group"],
            "direction": direction, "step_fraction": step, "baseline_objective": state.get("current_best_score"),
            "pass_number": state.get("pass_number", 1),
        })
        return pd.DataFrame([{
            "parameter": parameter, "old_value": base, "new_value": candidate,
            "factor_requested": factor, "factor_applied": candidate / base if base else 1.0,
            "priority": int(memory.at[idx, "priority"]),
            "reason": f"V13.4 symmetric bracketing: {memory.at[idx, 'group']}; {direction}; step={step:.6g}",
            "evidence": f"current_best_TOTAL_SCORE={state.get('current_best_score')}",
            "optimizer_version": self.VERSION, "optimizer_mode": "staged_symmetric_coordinate_search",
            "group": memory.at[idx, "group"], "direction": direction,
            "step_fraction_requested": step, "step_fraction_applied": step,
            "pass_number": state.get("pass_number", 1),
        }])

    def observe_run(self, candidate_objective, run_folder="", scientific_ok=True, scientific_penalty=0.0, valid=True, diagnostics=None):
        memory = self._ensure_memory()
        pending = memory[memory.get("pending", pd.Series(dtype=bool)).map(_b)]
        if pending.empty:
            return {"action": "baseline_observed", "accepted": False, "optimizer_version": self.VERSION}

        idx = int(pending.index[-1])
        row = memory.loc[idx].copy()
        state = self._read_state()
        baseline = _f(state.get("current_best_score"))
        candidate = _f(candidate_objective)
        effective = None if candidate is None else candidate + float(scientific_penalty or 0.0)
        delta = None if effective is None or baseline is None else baseline - effective
        absolute = delta or 0.0
        relative = absolute / max(abs(baseline or 1.0), 1e-30)
        meaningful = bool(valid) and bool(scientific_ok) and candidate is not None and absolute >= self.settings.min_absolute_improvement and relative >= self.settings.min_relative_improvement
        neutral = bool(valid) and candidate is not None and abs(candidate - baseline) <= self.settings.neutral_absolute_band

        direction = str(row.get("pending_direction", ""))
        step = _f(row.get("pending_step_fraction"), self.settings.initial_step_fraction)
        parameter = str(row.get("parameter", ""))
        event = {
            "parameter": parameter, "group": str(row.get("group", "")), "run_folder": str(run_folder),
            "baseline_objective": baseline, "candidate_objective": candidate,
            "effective_candidate_objective": effective, "direction": direction, "step_fraction": step,
            "scientific_ok": bool(scientific_ok), "scientific_penalty": scientific_penalty,
            "objective_improved": bool(candidate is not None and baseline is not None and candidate < baseline),
            "improvement_absolute": absolute, "improvement_relative": relative,
            "valid_run": bool(valid), "accepted": meaningful, **(diagnostics or {}),
        }
        memory.at[idx, "pending"] = False
        memory.at[idx, "pending_direction"] = ""
        memory.at[idx, "pending_step_fraction"] = None
        memory.at[idx, f"tested_{direction}"] = True
        memory.at[idx, "last_update"] = _now()

        if meaningful:
            memory.at[idx, "accepted_improvement_count"] = int(_f(row.get("accepted_improvement_count"), 0)) + 1
            memory.at[idx, "sensitivity_score"] = abs(candidate - baseline) / max(step, 1e-30)
            state["current_best_score"], state["current_best_run_folder"] = candidate, str(run_folder)
            state["accepted_improvements_count"] = int(_f(state.get("accepted_improvements_count"), 0)) + 1
            # Continue in same direction at an expanded step around the new best.
            memory.at[idx, "step_fraction"] = min(step * self.settings.step_growth_factor, self.settings.maximum_step_fraction)
            memory.at[idx, "direction_queue"] = direction
            memory.at[idx, "tested_increase"] = False
            memory.at[idx, "tested_decrease"] = False
            memory.at[idx, "status"] = f"bracket_{direction}"
            event.update({"action": "accepted_meaningful_improvement", "decision": "accept", "rejection_reason": ""})
        else:
            if not valid:
                state["invalid_candidates_count"] = int(_f(state.get("invalid_candidates_count"), 0)) + 1
                reason, action = "invalid_or_failed_MIN3P_run", "mark_infeasible_continue"
                memory.at[idx, "invalid_count"] = int(_f(row.get("invalid_count"), 0)) + 1
            elif not scientific_ok:
                reason, action = "scientific_constraint_failed", "reject_constraint_continue"
            elif neutral:
                reason, action = "near_neutral_change", "reject_neutral_continue"
            else:
                reason, action = "TOTAL_SCORE_not_meaningfully_improved", "reject_continue"

            state["rejected_candidates_count"] = int(_f(state.get("rejected_candidates_count"), 0)) + 1
            if not self._queue_items(memory.at[idx, "direction_queue"]):
                if step <= self.settings.minimum_step_fraction:
                    memory.at[idx, "status"] = "pass_complete"
                    action = "finish_parameter_for_pass"
                else:
                    memory.at[idx, "step_fraction"] = max(step * self.settings.step_shrink_factor, self.settings.minimum_step_fraction)
                    memory.at[idx, "direction_queue"] = "increase|decrease"
                    memory.at[idx, "tested_increase"] = False
                    memory.at[idx, "tested_decrease"] = False
                    memory.at[idx, "status"] = "refine_symmetric"
                    action = "shrink_step_after_symmetric_bracket"
            else:
                memory.at[idx, "status"] = "test_remaining_direction"
            event.update({"action": action, "decision": "reject", "rejection_reason": reason})

        state.update({"last_action": event["action"], "last_parameter": parameter, "last_direction": direction, "last_step_fraction": step})
        self._write_memory(memory)
        self._write_state(state)
        self._event(event)
        return {"optimizer_version": self.VERSION, **event}
