from __future__ import annotations

"""Deterministic plausibility flags; it does not create hidden rejection rules."""

from typing import Any


class ScientificPlausibilityChecker:
    VERSION = "V14"

    def __init__(self, paths, config):
        self.paths = paths
        self.config = config

    def evaluate(
        self,
        *,
        paths,
        config,
        run_folder: str,
        score: float | None,
        residual_diagnostics: dict[str, Any],
        numerical_diagnostics: dict[str, Any],
    ) -> dict[str, Any]:
        flags: list[str] = []
        numerical = numerical_diagnostics if isinstance(numerical_diagnostics, dict) else {}
        residual = residual_diagnostics if isinstance(residual_diagnostics, dict) else {}
        if numerical.get("risk_level") in {"MODERATE", "HIGH"}:
            flags.append("numerical_stability_warning")
        if residual.get("status") == "insufficient_data":
            flags.append("residual_evidence_insufficient")
        # Hard physical rejection belongs to user bounds and explicit scientific
        # constraint sheets.  This module therefore does not introduce a hidden
        # hard reject merely because an interpretation is uncertain.
        return {
            "status": "available",
            "hard_reject": False,
            "flags": flags,
            "reason": "No additional V14 hard physical constraint was configured outside existing deterministic bounds and scientific-constraint rules.",
            "run_folder": str(run_folder),
            "score": score,
        }

    check = evaluate
    assess = evaluate
