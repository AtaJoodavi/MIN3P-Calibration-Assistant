from __future__ import annotations

from pathlib import Path

import pandas as pd

from .config import ProjectPaths
from .diagnostics import geochemical_diagnosis
from .io_utils import log, timestamp


def _tail_table(path: Path, n: int = 10) -> str:
    if not path.exists():
        return f"Not available: {path}"
    try:
        df = pd.read_excel(path)
        if df.empty:
            return "Empty file."
        return df.tail(n).to_markdown(index=False)
    except Exception as exc:
        return f"Could not read {path}: {exc}"


def write_calibration_report(paths: ProjectPaths) -> Path:
    log(paths, "V9 STEP REPORT - Write deterministic calibration report")
    latest = None
    if paths.history_file.exists():
        hist = pd.read_excel(paths.history_file)
        latest = hist.iloc[-1] if not hist.empty else None
    diagnosis = geochemical_diagnosis(latest)
    lines = [
        "# MIN3P AI Calibration Report — V9.0",
        "",
        f"Generated: {timestamp()}",
        "",
        "## Architecture",
        "",
        "V9.0 separates the workflow into two roles:",
        "",
        "- **Deterministic calibration engine:** reads outputs, diagnoses bias, checks runtime stability, proposes bounded parameter changes, and optionally updates `agent_config.xlsx`.",
        "- **GPT scientific supervisor:** reviews the deterministic evidence and writes scientific interpretation only. It does not modify parameters.",
        "",
        "## Latest history",
        "",
        _tail_table(paths.history_file, 10),
        "",
        "## Run diagnostics",
        "",
        _tail_table(paths.run_diagnostics_file, 10),
        "",
        "## Deterministic geochemical diagnosis",
        "",
        diagnosis.to_markdown(index=False),
        "",
        "## Parameter suggestions",
        "",
        _tail_table(paths.suggestions_file, 10),
        "",
        "## Sensitivity coefficients",
        "",
        _tail_table(paths.sensitivity_coefficients_file, 20),
        "",
        "## Decision rules",
        "",
        "- pH and SO4 are calibrated before trace metals.",
        "- Trace-metal changes are postponed while pH or SO4 bias is large.",
        "- Failed or unstable runs are diagnostic evidence but are not treated as valid calibration improvements.",
        "- Runtime and failed-timestep safeguards reduce step size or stop automation.",
        "- All parameter changes are clipped by min/max and change-factor limits in `agent_config.xlsx`.",
    ]
    paths.calibration_report_file.parent.mkdir(parents=True, exist_ok=True)
    paths.calibration_report_file.write_text("\n".join(lines), encoding="utf-8")
    return paths.calibration_report_file
