from __future__ import annotations

"""Standalone verifier for V14.3.6 GPT advisory governance."""

from pathlib import Path

from gpt_advisory_dry_run import run_dry_run
from modules.gpt_scientific_supervisor import GPTScientificSupervisorV14


def main() -> None:
    assert GPTScientificSupervisorV14.VERSION == "V14.3.6"
    result = run_dry_run()
    assert result["api_called"] is False
    assert result["min3p_run_launched"] is False
    assert result["protected_campaign_hashes_unchanged"] is True
    assert result["deterministic_precedence_checks"]["frozen_suggestion_overridden"] is True
    assert result["deterministic_precedence_checks"]["smaller_step_suggestion_overridden"] is True
    assert result["deterministic_precedence_checks"]["unfinished_direction_pair_overridden"] is True
    print("PASS: V14.3.6 GPT supervisor loaded.")
    print("PASS: mocked advisory was schema-validated and logged only in a temporary audit log.")
    print("PASS: disabled YAML configuration produced a not-called audit record without an API call.")
    print("PASS: deterministic rules overrode frozen, smaller-step, and direction-pair-interrupting GPT suggestions.")
    print("PASS: canonical config, best snapshot, optimizer state, and live GPT log were unchanged.")
    print("Audit:", result["audit_file"])


if __name__ == "__main__":
    main()
