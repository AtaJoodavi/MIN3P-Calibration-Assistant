from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ProjectPaths:
    # Directories
    agent_core_dir: Path
    project_dir: Path
    project_info_dir: Path
    input_dir: Path
    runs_dir: Path
    results_dir: Path
    reports_dir: Path
    knowledge_dir: Path
    database_dir: Path

    # Input files
    config_file: Path
    post_script: Path

    # Core calibration outputs
    history_file: Path
    ranking_file: Path
    workflow_log_file: Path
    run_diagnostics_file: Path
    failed_runs_file: Path
    parameter_snapshots_file: Path
    suggestions_file: Path

    # Sensitivity analysis
    sensitivity_results_file: Path
    sensitivity_coefficients_file: Path
    parameter_influence_matrix_file: Path

    # Deterministic decision engine
    deterministic_decision_file: Path
    reaction_network_file: Path

    # Reports
    calibration_report_file: Path
    gpt_supervisor_report_file: Path

    # Calibration story outputs
    calibration_story_report_file: Path
    calibration_story_summary_file: Path
    parameter_change_history_file: Path
    calibration_pathway_file: Path
    parameter_importance_file: Path
    
    process_sensitivity_summary_file: Path
    process_sensitivity_report_file: Path

    # Control files
    manual_stop_file: Path
    
    
    sensitivity_interpretation_file: Path
    sensitivity_interpretation_report_file: Path

    # V13 adaptive optimizer outputs
    optimizer_memory_file: Path
    optimizer_events_file: Path
    optimizer_sensitivity_file: Path
    optimizer_report_file: Path

    @classmethod
    def from_agent_core(
        cls,
        agent_core_dir: Path | None = None,
    ) -> "ProjectPaths":
        agent_core_dir = (
            agent_core_dir
            or Path(__file__).resolve().parents[1]
        ).resolve()

        project_dir = Path(
            os.getenv("MIN3P_PROJECT_DIR", agent_core_dir.parent)
        ).resolve()

        project_info_dir = project_dir / "00_project_info"
        input_dir = project_dir / "01_input"
        runs_dir = project_dir / "03_runs"
        results_dir = project_dir / "04_results"
        reports_dir = project_dir / "05_reports"
        knowledge_dir = project_dir / "06_knowledge"
        database_dir = project_dir / "database"


        return cls(
            agent_core_dir=agent_core_dir,
            project_dir=project_dir,
            project_info_dir=project_info_dir,
            input_dir=input_dir,
            runs_dir=runs_dir,
            results_dir=results_dir,
            reports_dir=reports_dir,
            knowledge_dir=knowledge_dir,
            database_dir=database_dir,

            config_file=input_dir / "agent_config.xlsx",
            post_script=agent_core_dir / "plotsV46.py",

            history_file=results_dir / "optimization_history.xlsx",
            ranking_file=results_dir / "run_ranking.xlsx",
            workflow_log_file=results_dir / "workflow_log_V11.txt",
            run_diagnostics_file=results_dir / "run_diagnostics_V11.xlsx",
            failed_runs_file=results_dir / "failed_runs_V11.xlsx",
            parameter_snapshots_file=results_dir / "parameter_snapshots_V11.xlsx",
            suggestions_file=results_dir / "parameter_suggestions_V11.xlsx",

            sensitivity_results_file=results_dir / "sensitivity_results_V10.xlsx",
            sensitivity_coefficients_file=results_dir / "sensitivity_coefficients_V10.xlsx",
            parameter_influence_matrix_file=results_dir / "parameter_influence_matrix_V10.xlsx",

            deterministic_decision_file=results_dir / "deterministic_decision_state_V11.xlsx",
            reaction_network_file=results_dir / "reaction_network_V11.xlsx",

            calibration_report_file=reports_dir / "calibration_report_V11.md",
            gpt_supervisor_report_file=reports_dir / "gpt_scientific_supervisor_V11.md",

            calibration_story_report_file=reports_dir / "calibration_story_V11.md",
            calibration_story_summary_file=reports_dir / "calibration_story_summary_V11.txt",
            parameter_change_history_file=results_dir / "parameter_change_history_V11.xlsx",
            calibration_pathway_file=results_dir / "calibration_pathway_V11.xlsx",
            parameter_importance_file=results_dir / "parameter_importance_V10.xlsx",
            
            process_sensitivity_summary_file=results_dir / "process_sensitivity_summary_V10.xlsx",
            process_sensitivity_report_file=reports_dir / "process_sensitivity_report_V10.md",
            sensitivity_interpretation_file=results_dir / "sensitivity_interpretation_V10.xlsx",
            sensitivity_interpretation_report_file=reports_dir / "sensitivity_interpretation_report_V10.md",

            optimizer_memory_file=results_dir / "optimizer_parameter_memory_V13.xlsx",
            optimizer_events_file=results_dir / "optimizer_events_V13.xlsx",
            optimizer_sensitivity_file=results_dir / "local_sensitivity_history_V13.xlsx",
            optimizer_report_file=reports_dir / "V13_optimizer_campaign_report.md",

            manual_stop_file=project_dir / "STOP_CALIBRATION.txt",
        )

    def ensure_dirs(self) -> None:
        for folder in [
            self.project_info_dir,
            self.input_dir,
            self.runs_dir,
            self.results_dir,
            self.reports_dir,
            self.knowledge_dir,
            self.database_dir,
        ]:
            folder.mkdir(parents=True, exist_ok=True)