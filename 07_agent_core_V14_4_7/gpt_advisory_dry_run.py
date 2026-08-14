from __future__ import annotations

"""Non-mutating V14.3.6 GPT advisory validation.

This script makes no API call, launches no MIN3P run, and does not invoke
optimizer candidate selection. It uses a schema-valid mocked advisory in an
isolated temporary audit log, then verifies that deterministic eligibility and
precedence rules dominate illegal GPT recommendations.
"""

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import json
import tempfile

import pandas as pd

from min3p_ai_pipeline_V14 import V14Workflow, V14_VERSION
from modules.adaptive_coordinate_optimizer_V14 import AdaptiveCoordinateOptimizerV14
from modules.gpt_prompt_builder import build_system_prompt, build_user_input
from modules.gpt_response_validator import validate_gpt_response


VALID_ADVICE = {
    "scientific_assessment": "Mocked advisory used only to validate V14 governance.",
    "recommended_group": "other",
    "recommended_action": "rank_candidate",
    "suggested_parameter": "top_flux",
    "suggested_pair": "",
    "suggested_direction": "increase",
    "confidence": 0.99,
    "reasoning_summary": "This deliberately suggests a frozen parameter.",
    "freeze_suggestions": ["s_albite"],
    "reactivation_suggestions": ["top_flux"],
    "stop_continue_recommendation": "continue_with_deterministic_rules",
    "warnings": ["mocked_no_api_call"],
}


def _sha(path: Path) -> str | None:
    if not path.exists():
        return None
    return sha256(path.read_bytes()).hexdigest()


class _MockSupervisor:
    def __init__(self, advice: dict[str, Any], *, enabled: bool = True):
        self.advice = dict(advice)
        self.calls = 0
        self.settings = {
            "enabled": enabled,
            "valid_run_interval": 4,
            "call_on_new_best": True,
            "call_on_repeated_failures": True,
            "call_on_group_transition": True,
            "call_on_numerical_warning": True,
            "call_on_possible_stop": True,
        }

    def review_evidence(self, *, evidence: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        assert evidence["allowed_decision_scope"]["advisory_only"] is True
        return validate_gpt_response(self.advice)


class _FakeOptimizer:
    def __init__(self) -> None:
        self.received: dict[str, Any] | None = None

    def set_gpt_advisory(self, advice: dict[str, Any]) -> None:
        self.received = dict(advice)


class _Eligibility:
    def __init__(self, blocked: set[str] | None = None):
        self.blocked = {str(value).casefold() for value in (blocked or set())}

    def is_eligible(self, parameter: str, **_: Any) -> bool:
        return str(parameter).casefold() not in self.blocked


def _row(
    parameter: str,
    *,
    status: str = "unexplored",
    phase: str = "symmetric",
    step: float = 0.05,
    queue: str = "increase|decrease",
    tested_increase: bool = False,
    tested_decrease: bool = False,
) -> dict[str, Any]:
    return {
        "parameter": parameter,
        "group": "other",
        "user_status": "active",
        "status": status,
        "phase": phase,
        "step_fraction": step,
        "direction_queue": queue,
        "pending": False,
        "tested_increase": tested_increase,
        "tested_decrease": tested_decrease,
        "priority": 100,
        "sensitivity_score": None,
        "refinement_round": 0,
        "last_update": "2026-07-07T10:00:00+03:00",
    }


def _selector(*, advice: dict[str, Any], memory: pd.DataFrame, blocked: set[str] | None = None) -> str:
    optimizer = AdaptiveCoordinateOptimizerV14.__new__(AdaptiveCoordinateOptimizerV14)
    optimizer.services = {"state_manager": _Eligibility(blocked)}
    optimizer.settings = SimpleNamespace(minimum_step_fraction=0.005, gpt_min_confidence=0.60)
    optimizer._gpt_advisory = advice
    idx, _, _ = optimizer._candidate_index(memory, {"current_stage": "other"})
    if idx is None:
        raise AssertionError("Expected a legal deterministic candidate.")
    return str(memory.at[idx, "parameter"])


def pipeline_advisory_checks() -> dict[str, Any]:
    """Exercise the actual pipeline advisory method in a temporary audit log."""
    evidence = {
        "allowed_decision_scope": {
            "advisory_only": True,
            "cannot_edit_dat": True,
            "cannot_override_user_constraints": True,
            "cannot_override_bounds": True,
            "cannot_override_rollback": True,
            "cannot_override_numerical_rules": True,
            "cannot_override_hard_stop": True,
        }
    }

    with tempfile.TemporaryDirectory(prefix="v14_gpt_dry_run_") as temp_dir:
        log_file = Path(temp_dir) / "gpt_supervisor_log.jsonl"
        workflow = V14Workflow.__new__(V14Workflow)
        workflow.v14_components = {"gpt_supervisor": ("mock", "MockSupervisor")}
        workflow.v14_gpt_log_file = log_file
        workflow.optimizer_v14 = _FakeOptimizer()
        workflow.using_v13_5_optimizer_fallback = False
        workflow._latest_gpt_advice = None
        supervisor = _MockSupervisor(VALID_ADVICE, enabled=True)
        workflow._instantiate_component = lambda _: supervisor
        workflow._gpt_evidence = lambda **_: evidence

        def append_jsonl(path: Path, record: dict[str, Any]) -> None:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            with Path(path).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")

        workflow._append_jsonl = append_jsonl
        V14Workflow._request_gpt_advisory(
            workflow,
            event={"accepted": True, "action": "accepted_meaningful_improvement_continue_direction"},
            diagnostics={"scientific_ok": True},
            suggestion=pd.Series({"parameter": "s_albite", "group": "other"}),
            completed_valid_runs=1,
            disable_gpt=False,
        )
        lines = [json.loads(line) for line in log_file.read_text(encoding="utf-8").splitlines() if line.strip()]
        valid_record = lines[-1]
        assert valid_record["status"] == "valid_advisory_received"
        assert workflow.optimizer_v14.received == VALID_ADVICE
        assert supervisor.calls == 1

        disabled_log = Path(temp_dir) / "gpt_supervisor_disabled.jsonl"
        disabled_workflow = V14Workflow.__new__(V14Workflow)
        disabled_workflow.v14_components = {"gpt_supervisor": ("mock", "MockSupervisor")}
        disabled_workflow.v14_gpt_log_file = disabled_log
        disabled_workflow.optimizer_v14 = _FakeOptimizer()
        disabled_workflow.using_v13_5_optimizer_fallback = False
        disabled_workflow._latest_gpt_advice = None
        disabled_supervisor = _MockSupervisor(VALID_ADVICE, enabled=False)
        disabled_workflow._instantiate_component = lambda _: disabled_supervisor
        disabled_workflow._gpt_evidence = lambda **_: evidence
        disabled_workflow._append_jsonl = append_jsonl
        V14Workflow._request_gpt_advisory(
            disabled_workflow,
            event={"accepted": True, "action": "accepted_meaningful_improvement_continue_direction"},
            diagnostics={"scientific_ok": True},
            suggestion=pd.Series({"parameter": "s_albite", "group": "other"}),
            completed_valid_runs=1,
            disable_gpt=False,
        )
        disabled_record = json.loads(disabled_log.read_text(encoding="utf-8").splitlines()[-1])
        assert disabled_record["called"] is False
        assert disabled_record["trigger"] == "disabled_in_gpt_config"
        assert disabled_workflow.optimizer_v14.received is None
        assert disabled_supervisor.calls == 0

        return {
            "mock_valid_advisory_logged": True,
            "mock_valid_advisory_set_on_optimizer": True,
            "disabled_yaml_configuration_logged_without_api_call": True,
            "temporary_log_only": True,
        }


def selector_precedence_checks() -> dict[str, Any]:
    """Show that GPT cannot bypass eligibility, step order, or direction pairs."""
    illegal = dict(VALID_ADVICE)
    illegal["suggested_parameter"] = "top_flux"
    illegal["confidence"] = 0.99
    frozen_selected = _selector(
        advice=illegal,
        memory=pd.DataFrame([_row("s_albite"), _row("top_flux")]),
        blocked={"top_flux"},
    )
    assert frozen_selected == "s_albite"

    smaller_step = dict(VALID_ADVICE)
    smaller_step["suggested_parameter"] = "Kz"
    smaller_step["confidence"] = 0.99
    step_selected = _selector(
        advice=smaller_step,
        memory=pd.DataFrame([
            _row("Kz", status="refine_symmetric", step=0.025),
            _row("s_albite", status="unexplored", step=0.05),
        ]),
    )
    assert step_selected == "s_albite"

    pending_selected = _selector(
        advice=dict(VALID_ADVICE, suggested_parameter="s_albite", confidence=0.99),
        memory=pd.DataFrame([
            _row(
                "Kz",
                status="test_remaining_direction",
                phase="symmetric",
                step=0.025,
                queue="decrease",
                tested_increase=True,
                tested_decrease=False,
            ),
            _row("s_albite", status="unexplored", step=0.05),
        ]),
    )
    assert pending_selected == "Kz"

    return {
        "frozen_suggestion_overridden": True,
        "smaller_step_suggestion_overridden": True,
        "unfinished_direction_pair_overridden": True,
        "deterministic_selection_source": "optimizer_precedence",
    }


def run_dry_run() -> dict[str, Any]:
    root = Path(__file__).resolve().parent
    project_dir = root.parent
    results_dir = project_dir / "04_results"
    config_file = project_dir / "01_input" / "agent_config.xlsx"
    best_file = results_dir / "best_parameters_V14.xlsx"
    state_file = results_dir / "v14_optimizer_state.xlsx"
    live_gpt_log = results_dir / "gpt_supervisor_log.jsonl"

    before = {
        "canonical_config": _sha(config_file),
        "best_config": _sha(best_file),
        "optimizer_state": _sha(state_file),
        "live_gpt_log": _sha(live_gpt_log),
    }

    prompt = build_system_prompt()
    user_input = build_user_input({"allowed_decision_scope": {"advisory_only": True}})
    assert "advisory only" in prompt.casefold()
    assert "do not assert" in user_input.casefold()
    assert validate_gpt_response(VALID_ADVICE) == VALID_ADVICE

    pipeline_result = pipeline_advisory_checks()
    selector_result = selector_precedence_checks()

    after = {
        "canonical_config": _sha(config_file),
        "best_config": _sha(best_file),
        "optimizer_state": _sha(state_file),
        "live_gpt_log": _sha(live_gpt_log),
    }
    unchanged = before == after
    if not unchanged:
        raise RuntimeError("GPT advisory dry-run changed a protected campaign file.")

    report = {
        "optimizer_version": V14_VERSION,
        "dry_run": True,
        "api_called": False,
        "min3p_run_launched": False,
        "pipeline_advisory_checks": pipeline_result,
        "deterministic_precedence_checks": selector_result,
        "prompt_restrictions_verified": True,
        "schema_validation_verified": True,
        "protected_campaign_hashes_unchanged": True,
        "live_gpt_supervisor_log_unchanged": True,
    }
    results_dir.mkdir(parents=True, exist_ok=True)
    output = results_dir / "V14_gpt_advisory_dry_run.json"
    temporary = output.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(output)
    report["audit_file"] = str(output)
    return report


def main() -> None:
    print(json.dumps(run_dry_run(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
