from __future__ import annotations

"""Compact but complete V14 campaign report generator."""

import json
from pathlib import Path
from typing import Any

import pandas as pd

from modules.v14_utils import read_excel_safe, read_json


def _count(frame: pd.DataFrame, column: str, value: Any = None) -> int:
    if frame.empty or column not in frame.columns:
        return 0
    if value is None:
        return int(frame[column].notna().sum())
    return int(frame[column].astype(str).str.casefold().eq(str(value).casefold()).sum())


def write_v14_calibration_report(
    *,
    paths,
    campaign_state_file,
    state_history_file,
    interaction_history_file,
    decision_log_file,
    gpt_log_file,
    output_file,
) -> Path:
    campaign = read_json(Path(campaign_state_file), {})
    states = read_excel_safe(Path(state_history_file))
    interactions = read_excel_safe(Path(interaction_history_file))
    decisions = read_excel_safe(Path(decision_log_file))

    gpt_calls = 0
    gpt_valid = 0
    gpt_path = Path(gpt_log_file)
    if gpt_path.exists():
        for line in gpt_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            gpt_calls += int(bool(row.get("called")))
            gpt_valid += int(row.get("status") == "valid_advisory_received")

    accepted = _count(decisions, "accepted", True)
    rejected = _count(decisions, "accepted", False)
    pair_tests = _count(interactions, "pair_id")
    best = campaign.get("current_best_score", "not established") if isinstance(campaign, dict) else "not established"
    best_run = campaign.get("current_best_run_folder", "not established") if isinstance(campaign, dict) else "not established"

    lines = [
        "# V14 Calibration Report",
        "",
        f"**Project:** `{paths.project_dir.name}`",
        "",
        "## Current best",
        "",
        f"- TOTAL_SCORE: `{best}`",
        f"- Run folder: `{best_run}`",
        "",
        "## Candidate decisions",
        "",
        f"- Accepted candidates: `{accepted}`",
        f"- Rejected/invalid candidates: `{rejected}`",
        f"- Parameter-state audit rows: `{len(states)}`",
        "",
        "## Interaction-pair search",
        "",
        f"- Interaction audit rows: `{pair_tests}`",
        "- Pair candidates are only eligible when configured and evidence-gated; no exhaustive combination search is performed.",
        "",
        "## GPT Scientific Supervisor",
        "",
        f"- Advisory calls requested: `{gpt_calls}`",
        f"- Valid structured advisories: `{gpt_valid}`",
        "- GPT is advisory only and cannot override user statuses, bounds, stability checks, rollback, or hard-stop logic.",
        "",
        "## Safety invariants",
        "",
        "- Individual rejected increase/decrease moves force the opposite-direction test before another parameter is selected.",
        "- Accepted individual moves continue in the same direction until deterministic evidence ends that direction family.",
        "- Rejected and invalid candidates are restored to the V14 best snapshot by the pipeline.",
        "- User-frozen and inactive parameters are never auto-reactivated.",
    ]
    target = Path(output_file)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target
