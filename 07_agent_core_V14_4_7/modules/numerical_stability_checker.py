from __future__ import annotations

"""Conservative numerical-stability checks using existing run artefacts."""

from pathlib import Path
import re
from typing import Any

import pandas as pd

from modules.v14_utils import as_float


class NumericalStabilityChecker:
    VERSION = "V14"

    def __init__(self, paths, config):
        self.paths = paths
        self.config = config

    @staticmethod
    def _extract_fraction(text: str) -> float | None:
        patterns = [
            r"failed\s*(?:time)?\s*step(?:s)?\s*(?:fraction|ratio)?\s*[:=]\s*([0-9.]+)\s*%",
            r"failed\s*(?:time)?\s*step(?:s)?\s*(?:fraction|ratio)?\s*[:=]\s*([0-9.eE+-]+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.IGNORECASE)
            if not match:
                continue
            value = as_float(match.group(1))
            if value is None:
                continue
            return value / 100.0 if "%" in match.group(0) or value > 1 else value
        return None

    def _read_run_text(self, run_folder: str) -> str:
        root = Path(run_folder)
        if not root.exists() or not root.is_dir():
            return ""
        fragments: list[str] = []
        for path in list(root.rglob("*.log"))[:20] + list(root.rglob("*.out"))[:20] + list(root.rglob("*.txt"))[:20]:
            try:
                fragments.append(path.read_text(encoding="utf-8", errors="ignore")[:100_000])
            except Exception:
                continue
        return "\n".join(fragments)

    def evaluate(self, *, paths, config, run_folder: str, score: float | None, run_valid: bool) -> dict[str, Any]:
        text = self._read_run_text(run_folder)
        failed_fraction = self._extract_fraction(text)
        warnings: list[str] = []
        hard_reject = not bool(run_valid)
        if not run_valid:
            warnings.append("MIN3P run is not valid under the inherited run-status rule")
        if failed_fraction is not None:
            if failed_fraction > 0.40:
                hard_reject = True
                warnings.append(f"failed timestep fraction {failed_fraction:.2%} exceeds hard limit")
            elif failed_fraction > 0.25:
                warnings.append(f"failed timestep fraction {failed_fraction:.2%} exceeds high-risk warning limit")
            elif failed_fraction > 0.15:
                warnings.append(f"failed timestep fraction {failed_fraction:.2%} exceeds moderate-risk warning limit")
        lower = text.casefold()
        if any(token in lower for token in ("floating point exception", "nan", "segmentation fault", "fatal error")):
            hard_reject = True
            warnings.append("fatal numerical text marker found in run artefacts")
        return {
            "status": "available" if text else "insufficient_data",
            "run_valid": bool(run_valid),
            "score": score,
            "failed_timestep_fraction": failed_fraction,
            "hard_reject": hard_reject,
            "warnings": warnings,
            "risk_level": "HIGH" if hard_reject or (failed_fraction is not None and failed_fraction > 0.25) else ("MODERATE" if failed_fraction is not None and failed_fraction > 0.15 else "LOW"),
        }

    check = evaluate
    assess = evaluate
