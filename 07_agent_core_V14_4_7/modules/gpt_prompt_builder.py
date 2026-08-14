from __future__ import annotations

"""Restricted evidence-only prompt construction for the V14 GPT supervisor."""

import json
from typing import Any

from modules.v14_utils import json_safe


def build_system_prompt() -> str:
    return (
        "You are the MIN3P V14 Scientific Supervisor. You are advisory only. "
        "Interpret the supplied structured calibration evidence and recommend a scientific focus. "
        "You must not request, create, edit, or modify MIN3P .dat files, Excel files, parameter bounds, "
        "or user parameter statuses. You cannot override numerical stability findings, rollback rules, hard stops, "
        "or deterministic candidate precedence. Return only the supplied JSON schema. "
        "When evidence is insufficient, state that explicitly and recommend conservative continuation or review."
    )


def build_user_input(evidence: dict[str, Any]) -> str:
    safe = json_safe(evidence)
    return (
        "Review this evidence package. Recommend only an advisory ranking or interpretation; "
        "do not assert that an illegal or unavailable candidate should be executed.\n\n"
        + json.dumps(safe, ensure_ascii=False, indent=2)
    )
