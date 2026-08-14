from __future__ import annotations
"""Standalone V13.5 entry point. Leaves min3p_ai_pipeline_V13.py untouched."""
import argparse, shutil
from pathlib import Path
import pandas as pd
from min3p_ai_pipeline_V13 import V13Workflow
from modules.adaptive_coordinate_optimizer_V13_5 import AdaptiveCoordinateOptimizerV135
from modules.v13_5_parameter_state import has_best, save_best, restore_best, metadata
from modules.v13_5_objective_diagnostics import evaluate_scientific_constraints, write_candidate_diagnostics
from modules.v13_optimizer_report_V13_5 import write_v13_5_optimizer_campaign_report
from modules.history import rank_runs
from modules.io_utils import log

VALID={"success","success_with_retries","partial_success"}

class V135Workflow(V13Workflow):
    def __init__(self, reset_campaign_start: bool=False):
        super().__init__(reset_campaign_start=reset_campaign_start)
        self.optimizer_v13_5=AdaptiveCoordinateOptimizerV135(self.paths,self.config)

    def _archive_v13_4_once(self):
        stamp=pd.Timestamp.now().strftime("%Y%m%d_%H%M%S")
        root=self.paths.results_dir/"archives"/f"V13_4_campaign_before_V13_5_{stamp}"
        marker=self.paths.results_dir/"v13_5_archive_manifest.txt"
        if marker.exists(): return
        root.mkdir(parents=True,exist_ok=True)
        names=["best_parameters_V13_4.xlsx","v13_4_optimizer_state.xlsx","v13_4_optimizer_parameter_state.xlsx",
               "v13_4_optimizer_events.xlsx","v13_4_objective_diagnostics.xlsx"]
        copied=[]
        for n in names:
            p=self.paths.results_dir/n
            if p.exists(): shutil.copy2(p,root/n); copied.append(str(p))
        rp=self.paths.reports_dir/"V13_4_optimizer_campaign_report.md"
        if rp.exists(): shutil.copy2(rp,root/rp.name); copied.append(str(rp))
        marker.write_text("V13.4 archive created:\n"+str(root)+"\n"+"\n".join(copied),encoding="utf-8")

    def _verify_value(self, parameter, expected, tol=1e-12):
        try:
            p=self.config.parameters()
            row=p[p["parameter"].astype(str).str.strip().eq(str(parameter).strip())].iloc[0]
            actual=float(row["value"]); expected=float(expected)
            return abs(actual-expected) <= tol*max(1.0,abs(expected))
        except Exception: return False

    def _ranking_best(self):
        rank_runs(self.paths,self.config)
        f=self.paths.ranking_file
        if not f.exists(): return None,None
        df=pd.read_excel(f)
        if df.empty or "TOTAL_SCORE" not in df.columns: return None,None
        valid=df.copy()
        if "run_status" in valid.columns:
            valid=valid[valid["run_status"].astype(str).str.lower().isin(VALID)]
        valid=valid.dropna(subset=["TOTAL_SCORE"])
        if valid.empty: return None,None
        row=valid.sort_values("TOTAL_SCORE").iloc[0]
        return self._safe_float(row["TOTAL_SCORE"]),str(row.get("run_folder",""))

    def _ensure_v13_5_baseline(self):
        state=self.optimizer_v13_5._read_state()
        if state and self.optimizer_v13_5.current_best_score() is not None: return True
        self._archive_v13_4_once()
        # Prefer an archived V13.4 best workbook as the configuration source.
        v134=self.paths.results_dir/"best_parameters_V13_4.xlsx"
        if v134.exists():
            shutil.copy2(v134,self.paths.config_file)
        ranking_score,ranking_run=self._ranking_best()
        source_meta={}
        if v134.exists():
            try:
                source_meta=pd.read_excel(v134,sheet_name="v13_4_metadata").iloc[0].to_dict()
            except Exception: source_meta={}
        source_score=self._safe_float(source_meta.get("TOTAL_SCORE"))
        source_run=str(source_meta.get("source_run_folder",""))
        if source_score is not None and ranking_score is not None and abs(source_score-ranking_score)>1e-9:
            log(self.paths,f"WARNING: V13.5 ranking best ({ranking_score}) and V13.4 best-memory score ({source_score}) differ. Using V13.4 configuration source and its recorded score.")
        score=source_score if source_score is not None else ranking_score
        run=source_run or ranking_run
        if score is None or not run:
            # No valid prior V13 memory: create a baseline run.
            baseline=self._v13_single_cycle()
            score=self._safe_float(baseline.get("TOTAL_SCORE")); run=str(baseline.get("run_folder",""))
            if score is None or str(baseline.get("run_status","")).lower() not in VALID:
                log(self.paths,"V13.5 stopped: baseline did not produce a valid TOTAL_SCORE."); return False
        save_best(self.paths.config_file,self.paths.results_dir,total_score=score,run_folder=run,
                  objective_reference_file=str(self.paths.results_dir/"objective_reference_V13.xlsx"),
                  optimizer_version="V13.5",stage="baseline")
        self.optimizer_v13_5.initialize(score,run,baseline_source="V13.4_best_memory_then_run_ranking")
        return True

    def _patch_history_v13_5(self, run_dir, score, event):
        f=self.paths.history_file
        if not f.exists(): return
        try:
            df=pd.read_excel(f); idx=df.index[-1]
            if "run_folder" in df.columns:
                m=df["run_folder"].astype(str).str.contains(Path(run_dir).name,regex=False,na=False)
                if m.any(): idx=df[m].index[-1]
            values={"v13_5_optimizer_version":"V13.5","v13_5_candidate_id":event.get("candidate_id",""),
                    "v13_5_decision":event.get("decision",""),"v13_5_accepted":event.get("accepted",False),
                    "v13_5_parameter":event.get("parameter",""),"v13_5_group":event.get("group",""),
                    "v13_5_direction":event.get("direction_label",event.get("direction","")),
                    "v13_5_step_fraction":event.get("step_fraction"),"v13_5_baseline_TOTAL_SCORE":event.get("baseline_TOTAL_SCORE"),
                    "v13_5_candidate_TOTAL_SCORE":event.get("candidate_TOTAL_SCORE",score),
                    "v13_5_effective_TOTAL_SCORE":event.get("effective_TOTAL_SCORE"),
                    "v13_5_restoration_verified":event.get("restoration_verified")}
            for k,v in values.items():
                if k not in df.columns: df[k]=pd.NA
                df.at[idx,k]=v
            df.to_excel(f,index=False)
        except Exception as exc: log(self.paths,f"WARNING: V13.5 history patch failed: {exc}")

    def auto_v13_5(self,max_runs:int=10,skip_gpt:bool=True):
        log(self.paths,"="*80); log(self.paths,"V13.5 ISOLATED STAGED CALIBRATION STARTED"); log(self.paths,"="*80)
        if not self._ensure_v13_5_baseline(): return
        for _ in range(max_runs):
            if self.paths.manual_stop_file.exists():
                log(self.paths,f"Manual stop file detected: {self.paths.manual_stop_file}"); break
            suggestion=self.optimizer_v13_5.next_suggestion()
            if suggestion.empty:
                log(self.paths,"V13.5 has no eligible candidate; campaign may be converged."); break
            row=suggestion.iloc[0]
            self.engine.apply_suggestions(suggestion)
            diagnostic=self._v13_single_cycle()
            run_dir=Path(diagnostic.get("run_folder",""))
            score=self._safe_float(diagnostic.get("TOTAL_SCORE"))
            valid=str(diagnostic.get("run_status","")).lower() in VALID and score is not None
            current=self.optimizer_v13_5._read_state()
            baseline_run=str(current.get("current_best_run_folder","")); baseline_score=self._safe_float(current.get("current_best_score"))
            preliminary={"scientific_ok":True,"scientific_penalty":0.0,"constraint_failures":"","tradeoff_class":"candidate_run_invalid" if not valid else ""}
            if valid:
                ranking=pd.read_excel(self.paths.ranking_file)
                matches=ranking[ranking["run_folder"].astype(str).str.contains(run_dir.name,regex=False,na=False)] if "run_folder" in ranking.columns else pd.DataFrame()
                preliminary=evaluate_scientific_constraints(matches.iloc[0] if not matches.empty else None,self.config)
            event=self.optimizer_v13_5.observe_run(score,str(run_dir),scientific_ok=bool(preliminary.get("scientific_ok",True)),
                scientific_penalty=float(preliminary.get("scientific_penalty",0.0) or 0.0),valid=valid,diagnostics=preliminary,
                min3p_run_status=str(diagnostic.get("run_status","")))
            if valid:
                diag=write_candidate_diagnostics(results_dir=self.paths.results_dir,ranking_file=self.paths.ranking_file,
                    baseline_run_folder=baseline_run,candidate_run_folder=str(run_dir),baseline_total_score=baseline_score,
                    candidate_total_score=score,parameter=str(row.get("parameter","")),group=str(row.get("group","")),
                    direction=str(row.get("direction_label",row.get("direction",""))),step_fraction=self._safe_float(row.get("step_fraction_applied")),
                    accepted=bool(event.get("accepted")),decision=str(event.get("decision","")),
                    rejection_reason=str(event.get("rejection_reason","")),config=self.config,candidate_id=str(row.get("candidate_id","")))
                event.update(diag)
            if event.get("accepted"):
                save_best(self.paths.config_file,self.paths.results_dir,total_score=score,run_folder=str(run_dir),
                          objective_reference_file=str(self.paths.results_dir/"objective_reference_V13.xlsx"),
                          optimizer_version="V13.5",stage=str(event.get("group","")),diagnostics=event)
                restored=self._verify_value(row["parameter"],row["new_value"])
            else:
                try:
                    restore_best(self.paths.config_file,self.paths.results_dir)
                    restored=self._verify_value(row["parameter"],row["old_value"])
                except Exception as exc:
                    restored=False; log(self.paths,f"WARNING: V13.5 best restore failed: {exc}")
            event=self.optimizer_v13_5.finalize_decision(event,restored)
            self._patch_history_v13_5(run_dir,score,event)
            log(self.paths,f"V13.5 {event.get('decision')} | {event.get('parameter')} | score={score} | restoration_verified={restored}")
        rank_runs(self.paths,self.config)
        try: write_v13_5_optimizer_campaign_report(self.paths)
        except Exception as exc: log(self.paths,f"WARNING: V13.5 report failed: {exc}")
        if not skip_gpt: self.supervisor.review()
        log(self.paths,"V13.5 isolated staged calibration finished")

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--mode",default="auto",choices=["auto"])
    p.add_argument("--max-runs",type=int,default=10)
    p.add_argument("--reset-campaign-start",action="store_true")
    p.add_argument("--use-gpt",action="store_true")
    a=p.parse_args()
    V135Workflow(reset_campaign_start=a.reset_campaign_start).auto_v13_5(max_runs=a.max_runs,skip_gpt=not a.use_gpt)
if __name__=="__main__": main()
