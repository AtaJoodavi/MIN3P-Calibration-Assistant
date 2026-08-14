from __future__ import annotations

from pathlib import Path
import pandas as pd


def _read(path: Path) -> pd.DataFrame:
    try:
        return pd.read_excel(path) if path.exists() else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def write_v13_optimizer_campaign_report(paths):
    """Create V13.4 staged optimizer audit report."""
    results = paths.results_dir
    state = _read(results / "v13_4_optimizer_state.xlsx")
    events = _read(results / "v13_4_optimizer_events.xlsx")
    params = _read(results / "v13_4_optimizer_parameter_state.xlsx")
    diagnostics = _read(results / "v13_4_objective_diagnostics.xlsx")

    report = paths.reports_dir / "V13_4_optimizer_campaign_report.md"
    report.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# V13.4 Staged Optimizer Campaign Report", ""]

    if not state.empty:
        s = state.iloc[0]
        lines += [
            "## Current state", "",
            f"- Current best TOTAL_SCORE: `{s.get('current_best_score', '')}`",
            f"- Current best run: `{s.get('current_best_run_folder', '')}`",
            f"- Current stage: `{s.get('current_stage', '')}`",
            f"- Pass number: `{s.get('pass_number', '')}`",
            f"- Accepted improvements: `{s.get('accepted_improvements_count', '')}`",
            f"- Rejected candidates: `{s.get('rejected_candidates_count', '')}`",
            f"- Convergence status: `{s.get('convergence_status', '')}`",
            "",
        ]

    lines += ["## Recent candidate decisions", ""]
    if events.empty:
        lines += ["No V13.4 events available.", ""]
    else:
        cols = [c for c in [
            "timestamp", "action", "group", "parameter", "direction",
            "step_fraction", "baseline_objective", "candidate_objective",
            "accepted", "decision", "rejection_reason", "scientific_ok", "tradeoff_class"
        ] if c in events.columns]
        lines += [events[cols].tail(40).to_markdown(index=False), ""]

    lines += ["## Parameter-stage status", ""]
    if not params.empty:
        cols = [c for c in ["group", "parameter", "status", "pass_number", "step_fraction", "tried_increase", "tried_decrease"] if c in params.columns]
        lines += [params[cols].sort_values(["group", "parameter"]).to_markdown(index=False), ""]
    else:
        lines += ["No parameter state file available.", ""]

    lines += ["## Species-level trade-offs", ""]
    if not diagnostics.empty and "tradeoff_class" in diagnostics.columns:
        summary = diagnostics.drop_duplicates("candidate_run_folder", keep="last")
        cols = [c for c in ["candidate_run_folder", "parameter", "group", "accepted", "tradeoff_class", "species_improved_count", "species_worsened_count", "constraint_failures"] if c in summary.columns]
        lines += [summary[cols].tail(25).to_markdown(index=False), ""]
    else:
        lines += ["No RMSE component diagnostics available yet.", ""]

    report.write_text("\n".join(lines), encoding="utf-8")
    return report
