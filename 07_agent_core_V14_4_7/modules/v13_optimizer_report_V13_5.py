from __future__ import annotations
from pathlib import Path
import pandas as pd
def _read(path: Path):
    try: return pd.read_excel(path) if path.exists() else pd.DataFrame()
    except Exception: return pd.DataFrame()
def write_v13_5_optimizer_campaign_report(paths):
    r=paths.results_dir
    state=_read(r/"v13_5_optimizer_state.xlsx"); decisions=_read(r/"v13_5_candidate_decisions.xlsx")
    params=_read(r/"v13_5_optimizer_parameter_state.xlsx"); report=paths.reports_dir/"V13_5_optimizer_campaign_report.md"
    lines=["# V13.5 Staged Directional Coordinate Optimizer Report",""]
    if not state.empty:
        s=state.iloc[0]; lines += ["## Current state","",f"- Current best TOTAL_SCORE: `{s.get('current_best_score','')}`",
        f"- Current best run: `{s.get('current_best_run_folder','')}`",f"- Current stage: `{s.get('current_stage','')}`",
        f"- Pass: `{s.get('pass_number','')}`",f"- Accepted improvements: `{s.get('accepted_improvements_count','')}`",
        f"- Reopened parameters: `{s.get('reopened_parameters_count','')}`",f"- Convergence: `{s.get('convergence_status','')}`",""]
    lines += ["## Final candidate decisions",""]
    if decisions.empty: lines += ["No V13.5 candidate decisions available.",""]
    else:
        cols=[c for c in ["timestamp","candidate_id","group","parameter","search_phase","direction_label","old_value","new_value","numeric_change","step_fraction","baseline_TOTAL_SCORE","candidate_TOTAL_SCORE","effective_TOTAL_SCORE","accepted","decision","rejection_reason","restoration_verified"] if c in decisions.columns]
        lines += [decisions[cols].tail(60).to_markdown(index=False),""]
    lines += ["## Parameter states",""]
    if params.empty: lines += ["No V13.5 parameter state available.",""]
    else:
        cols=[c for c in ["group","parameter","status","phase","step_fraction","reopen_count","reopened_after_parameter","accepted_improvement_count"] if c in params.columns]
        lines += [params[cols].sort_values(["group","parameter"]).to_markdown(index=False),""]
    report.write_text("\n".join(lines),encoding="utf-8"); return report
