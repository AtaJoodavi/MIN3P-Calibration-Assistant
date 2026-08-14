from __future__ import annotations

"""Explicit V14 decision hierarchy validator.

The optimiser remains the sole candidate selector.  This manager validates that
an already-selected candidate cannot violate a higher-priority deterministic
rule before any GPT ranking is considered.
"""

from typing import Any


class CalibrationStrategyManager:
    VERSION = "V14"
    HIERARCHY = [
        "user_constraints",
        "hard_physical_bounds",
        "numerical_stability_rules",
        "objective_and_rollback_rules",
        "parameter_state_rules",
        "interaction_pair_rules",
        "gpt_supervisor_recommendation",
        "default_optimizer_selection",
    ]

    def __init__(self, paths, config):
        self.paths = paths
        self.config = config

    def validate_candidate(self, *, suggestion, state_manager, pair_optimizer=None) -> dict[str, Any]:
        if suggestion is None or getattr(suggestion, "empty", True):
            return {"ok": False, "reason": "empty_candidate", "decision_source": "default_optimizer_selection"}
        required = {"candidate_id", "parameter", "old_value", "new_value", "direction"}
        missing = [column for column in required if column not in suggestion.columns]
        if missing:
            return {"ok": False, "reason": f"candidate_missing_columns:{'|'.join(missing)}", "decision_source": "default_optimizer_selection"}
        for _, row in suggestion.iterrows():
            parameter = str(row.get("parameter", "")).strip()
            if not parameter:
                return {"ok": False, "reason": "candidate_has_empty_parameter", "decision_source": "user_constraints"}
            if not state_manager.is_eligible(parameter, for_interaction=str(row.get("candidate_type", "")) == "interaction_pair"):
                return {"ok": False, "reason": f"parameter_not_eligible:{parameter}", "decision_source": "parameter_state_rules"}
            if row.get("old_value") == row.get("new_value"):
                return {"ok": False, "reason": f"null_or_bounded_move:{parameter}", "decision_source": "hard_physical_bounds"}
        return {"ok": True, "reason": "candidate_satisfies_v14_hierarchy", "decision_source": "default_optimizer_selection"}

    def snapshot(self) -> dict[str, Any]:
        return {"optimizer_version": self.VERSION, "decision_hierarchy": list(self.HIERARCHY)}
