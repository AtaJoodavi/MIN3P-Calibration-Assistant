from __future__ import annotations
"""V13.5 species diagnostics and optional scientific constraints."""
from datetime import datetime
from pathlib import Path
from typing import Any
import pandas as pd
DIAGNOSTIC_FILE = "v13_5_objective_diagnostics.xlsx"
def _f(v, default=None):
    try: return default if v is None or pd.isna(v) else float(v)
    except Exception: return default
def _read(p: Path):
    try: return pd.read_excel(p) if p.exists() else pd.DataFrame()
    except Exception: return pd.DataFrame()
def _match(df, run_folder):
    if df.empty or "run_folder" not in df.columns: return None
    m = df[df["run_folder"].astype(str).str.contains(Path(str(run_folder)).name, regex=False, na=False)]
    return None if m.empty else m.iloc[0]
def _rmse_columns(df): return [c for c in df.columns if str(c).upper().startswith("RMSE_")]

def evaluate_scientific_constraints(candidate_row, config):
    out={"scientific_ok":True,"scientific_penalty":0.0,"constraint_failures":"","constraint_count":0}
    if candidate_row is None: return out
    try: rules=config.sheet("v13_scientific_constraints")
    except Exception: rules=pd.DataFrame()
    if rules.empty or "metric" not in rules.columns: return out
    failures=[]; penalty=0.0
    for _,r in rules.iterrows():
        metric=str(r.get("metric","")).strip()
        if not metric or metric not in candidate_row.index: continue
        value, lo, hi = _f(candidate_row.get(metric)), _f(r.get("min_value")), _f(r.get("max_value"))
        if value is None: continue
        violation=max((lo-value) if lo is not None else 0.0,(value-hi) if hi is not None else 0.0,0.0)
        if violation <= 0: continue
        hard=str(r.get("hard","yes")).strip().lower() in {"1","true","yes","y"}
        penalty += violation*(_f(r.get("penalty_weight"),0.0) or 0.0)
        failures.append(f"{metric}={value:g}")
        if hard: out["scientific_ok"]=False
    out.update({"scientific_penalty":penalty,"constraint_failures":"; ".join(failures),"constraint_count":len(failures)})
    return out

def write_candidate_diagnostics(*, results_dir, ranking_file, baseline_run_folder, candidate_run_folder,
                                baseline_total_score, candidate_total_score, parameter, group, direction,
                                step_fraction, accepted, decision, rejection_reason, config, candidate_id=""):
    results_dir=Path(results_dir); ranking=_read(Path(ranking_file))
    baseline,candidate=_match(ranking,baseline_run_folder),_match(ranking,candidate_run_folder)
    constraints=evaluate_scientific_constraints(candidate,config); rows=[]; improved=worsened=0
    if baseline is not None and candidate is not None:
        for metric in sorted(set(_rmse_columns(ranking))):
            before,after=_f(baseline.get(metric)),_f(candidate.get(metric))
            if before is None or after is None: continue
            delta=after-before; effect="improved" if delta<0 else ("worsened" if delta>0 else "unchanged")
            improved += effect=="improved"; worsened += effect=="worsened"
            rows.append({"timestamp":datetime.now().strftime("%Y-%m-%d %H:%M:%S"),"candidate_id":candidate_id,
                         "baseline_run_folder":baseline_run_folder,"candidate_run_folder":candidate_run_folder,
                         "parameter":parameter,"group":group,"direction":direction,"step_fraction":step_fraction,
                         "accepted":bool(accepted),"decision":decision,"rejection_reason":rejection_reason,
                         "baseline_TOTAL_SCORE":baseline_total_score,"candidate_TOTAL_SCORE":candidate_total_score,
                         "metric":metric,"baseline_rmse":before,"candidate_rmse":after,"delta_rmse":delta,"effect":effect})
    tradeoff="globally_consistent_improvement" if improved and not worsened else ("species_tradeoff" if improved and worsened else ("global_degradation" if worsened else "no_metric_detail_available"))
    if rows:
        p=results_dir/DIAGNOSTIC_FILE; _read(p)
        existing=_read(p); pd.concat([existing,pd.DataFrame(rows)],ignore_index=True).to_excel(p,index=False)
    return {"diagnostic_file":str(results_dir/DIAGNOSTIC_FILE),"species_improved_count":int(improved),
            "species_worsened_count":int(worsened),"tradeoff_class":tradeoff,**constraints}
