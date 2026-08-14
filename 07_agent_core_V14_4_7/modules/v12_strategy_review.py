"""
V12 strategy-review helper.

Purpose:
- Create a scientific review package after V11/V12 stops due to no progress.
- Keep MIN3P mechanics unchanged.
- Summarize what was tried, which groups are exhausted/cooling down,
  and what should be reviewed manually or by GPT before the next campaign.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from datetime import datetime
import json

try:
    import pandas as pd
except Exception:
    pd = None

try:
    from modules.v12_campaign_phase_manager import (
        V12CampaignPhaseManager,
        phase_for_parameter,
        extract_objective,
        extract_parameter,
        extract_status,
        history_records,
        recommendation_as_dict,
    )
except Exception:
    from v12_campaign_phase_manager import (
        V12CampaignPhaseManager,
        phase_for_parameter,
        extract_objective,
        extract_parameter,
        extract_status,
        history_records,
        recommendation_as_dict,
    )


def _read_history(history: Any) -> list[dict[str, Any]]:
    if history is None:
        return []

    if isinstance(history, (str, Path)):
        path = Path(history)
        if not path.exists():
            return []

        if path.suffix.lower() in {".xlsx", ".xls"}:
            if pd is None:
                return []
            df = pd.read_excel(path)
            return list(df.to_dict(orient="records"))

        if path.suffix.lower() == ".csv":
            if pd is None:
                return []
            df = pd.read_csv(path)
            return list(df.to_dict(orient="records"))

        return []

    return history_records(history)


def _safe_float(value: Any) -> float | None:
    try:
        x = float(value)
        if x != x:
            return None
        return x
    except Exception:
        return None


def summarize_history(records: list[dict[str, Any]], recent_window: int = 10) -> dict[str, Any]:
    recent = records[-recent_window:]

    best_objective = None
    best_idx = None

    # Prefer the latest accepted_best objective.
    # In this project, best_objective can represent the previous best before
    # the current row is accepted, while qc_objective_score/objective_total
    # represents the actual accepted run score.
    for i in range(len(records) - 1, -1, -1):
        record = records[i]
        status = extract_status(record)
        if status in {"accepted_best", "new_best", "accepted_new_best"}:
            value = _safe_float(record.get("qc_objective_score"))
            if value is None:
                value = _safe_float(record.get("objective_total"))
            if value is not None:
                best_objective = value
                best_idx = i
                break

    # Fallback to latest non-empty best_objective only if no accepted_best row exists.
    if best_objective is None:
        for i in range(len(records) - 1, -1, -1):
            record = records[i]
            value = _safe_float(record.get("best_objective"))
            if value is not None:
                best_objective = value
                best_idx = i
                break

    parameter_counts: dict[str, int] = {}
    phase_counts: dict[str, int] = {}
    status_counts: dict[str, int] = {}
    recent_parameters: list[str] = []
    recent_phases: list[str] = []

    # Last fallback only: if no cumulative best or accepted_best row exists,
    # use the minimum recognized objective.
    if best_objective is None:
        for i, record in enumerate(records):
            objective = extract_objective(record)
            if objective is not None and (best_objective is None or objective < best_objective):
                best_objective = objective
                best_idx = i

    for record in recent:
        parameter = extract_parameter(record) or "unknown"
        phase = phase_for_parameter(parameter)
        status = extract_status(record)

        parameter_counts[parameter] = parameter_counts.get(parameter, 0) + 1
        phase_counts[phase] = phase_counts.get(phase, 0) + 1
        status_counts[status] = status_counts.get(status, 0) + 1
        recent_parameters.append(parameter)
        recent_phases.append(phase)

    repeated_parameters = {
        k: v for k, v in sorted(parameter_counts.items(), key=lambda item: item[1], reverse=True)
        if v >= 2 and k != "unknown"
    }

    repeated_phases = {
        k: v for k, v in sorted(phase_counts.items(), key=lambda item: item[1], reverse=True)
        if v >= 2 and k != "unknown"
    }

    return {
        "n_records": len(records),
        "recent_window": recent_window,
        "best_objective": best_objective,
        "best_row_index_zero_based": best_idx,
        "recent_status_counts": status_counts,
        "recent_parameter_counts": parameter_counts,
        "recent_phase_counts": phase_counts,
        "repeated_recent_parameters": repeated_parameters,
        "repeated_recent_phases": repeated_phases,
        "recent_parameters": recent_parameters,
        "recent_phases": recent_phases,
    }


def build_review_markdown(
    records: list[dict[str, Any]],
    stop_trigger: str | None = None,
    stop_reason: str | None = None,
    project: str | None = None,
    recent_window: int = 10,
) -> str:
    summary = summarize_history(records, recent_window=recent_window)

    manager = V12CampaignPhaseManager(
        state_path=Path("v12_campaign_state.json"),
        recent_window=recent_window,
    )
    recommendation = manager.recommend(records)

    lines: list[str] = []

    lines.append("# V12 Scientific Strategy Review")
    lines.append("")
    lines.append(f"**Created:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if project:
        lines.append(f"**Project:** `{project}`")
    lines.append("")
    lines.append("## Stop context")
    lines.append("")
    lines.append(f"- Stop trigger: `{stop_trigger or 'not provided'}`")
    lines.append(f"- Stop reason: {stop_reason or 'not provided'}")
    lines.append(f"- History rows reviewed: {summary['n_records']}")
    lines.append(f"- Recent window: {summary['recent_window']} run(s)")
    lines.append(f"- Best objective found in history: `{summary['best_objective']}`")
    lines.append("")

    lines.append("## Recent campaign pattern")
    lines.append("")
    lines.append("### Status counts")
    for key, value in summary["recent_status_counts"].items():
        lines.append(f"- `{key}`: {value}")
    if not summary["recent_status_counts"]:
        lines.append("- No recent status records found.")
    lines.append("")

    lines.append("### Recent scientific phase counts")
    for key, value in summary["recent_phase_counts"].items():
        lines.append(f"- `{key}`: {value}")
    if not summary["recent_phase_counts"]:
        lines.append("- No recent phase records found.")
    lines.append("")

    lines.append("### Repeated recent parameters")
    if summary["repeated_recent_parameters"]:
        for key, value in summary["repeated_recent_parameters"].items():
            lines.append(f"- `{key}`: {value} attempt(s)")
    else:
        lines.append("- No repeated parameter detected in the recent window.")
    lines.append("")

    lines.append("## V12 phase-manager recommendation")
    lines.append("")
    lines.append(f"- Active phase for next campaign: `{recommendation.active_phase}`")
    lines.append(f"- Allowed phases: `{recommendation.allowed_phases}`")
    lines.append(f"- Cooldown phases: `{recommendation.cooldown_phases}`")
    lines.append(f"- Recommended multiplier scale: `{recommendation.multiplier_scale}`")
    lines.append(f"- Reason: {recommendation.reason}")
    lines.append("")

    lines.append("## Scientific interpretation")
    lines.append("")
    lines.append(
        "- V11 mechanics should remain unchanged because the stop was caused by scientific non-improvement, not by software failure."
    )
    lines.append(
        "- Keep the current best run as the baseline and start V12 from the rolled-back best DAT state."
    )
    lines.append(
        "- Avoid repeatedly perturbing the same parameter or scientific group after valid-but-not-best runs."
    )
    lines.append(
        "- Use smaller multipliers after repeated non-improvement, especially near parameter bounds."
    )
    lines.append("- Move calibration by phase: flow -> redox/oxygen -> sulfide kinetics -> sorption -> mineralogy/buffering -> boundary chemistry.")
    lines.append("")

    lines.append("## GPT/manual review questions before next automatic campaign")
    lines.append("")
    lines.append("1. Is the current best improvement controlled mainly by flow or oxygen availability?")
    lines.append("2. Are accepted parameters physically plausible?")
    lines.append("3. Which process group has been over-tested without improvement?")
    lines.append("4. Should the next campaign lock the accepted parameters `bottom_head = -0.275` and `scaling_ox2 = 1.10e-08`?")
    lines.append("5. Should sulfide kinetics or mineral buffering be cooled down before testing boundary chemistry?")
    lines.append("")

    return "\n".join(lines)


def write_v12_strategy_review(
    history: Any,
    output_dir: str | Path,
    stop_trigger: str | None = None,
    stop_reason: str | None = None,
    project: str | None = None,
    recent_window: int = 10,
) -> dict[str, Path]:
    """
    Write V12 strategy-review Markdown and JSON files.
    """
    records = _read_history(history)

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    markdown = build_review_markdown(
        records=records,
        stop_trigger=stop_trigger,
        stop_reason=stop_reason,
        project=project,
        recent_window=recent_window,
    )

    summary = summarize_history(records, recent_window=recent_window)

    md_path = output_path / f"V12_strategy_review_{timestamp}.md"
    json_path = output_path / f"V12_strategy_review_{timestamp}.json"

    md_path.write_text(markdown, encoding="utf-8")

    json_payload = {
        "created": datetime.now().isoformat(timespec="seconds"),
        "project": project,
        "stop_trigger": stop_trigger,
        "stop_reason": stop_reason,
        "summary": summary,
    }

    json_path.write_text(
        json.dumps(json_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return {
        "markdown": md_path,
        "json": json_path,
    }
