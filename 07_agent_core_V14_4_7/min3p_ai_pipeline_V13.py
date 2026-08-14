from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from modules.config import ProjectPaths
from modules.config_reader import ConfigReader
from modules.dat_builder import DatBuilder
from modules.decision_engine import DeterministicCalibrationEngine
from modules.evaluator import ResultEvaluator
from modules.gpt_supervisor import GPTScientificSupervisor
from modules.history import rank_runs, update_history
from modules.io_utils import log, append_df_excel, timestamp
from modules.log_parser import analyze_run
from modules.reports import write_calibration_report
from modules.runner import Min3pRunner
from modules.sensitivity import SensitivityAnalyzer
from modules.calibration_story import write_calibration_story_report
from modules.process_sensitivity_report import write_process_sensitivity_report
from modules.sensitivity_interpreter import write_sensitivity_interpretation_report
from modules.calibration_strategy import run_v10 as write_calibration_strategy
from modules.parameter_importance import write_parameter_importance_report
from modules.run_quality_classifier import classify_and_save
from modules.run_acceptance_memory import classify_acceptance, append_acceptance_memory
from modules.parameter_state_memory import (
    apply_parameter_memory_policy,
    restore_best_parameters,
    create_best_parameters_from_history,
)
from modules.bad_suggestion_memory import (
    update_bad_suggestion_memory,
    filter_suggestions_against_bad_memory,
    select_suggestions_with_fallback,
)
from modules.parameter_exhaustion_memory import (
    update_parameter_exhaustion_memory,
    filter_suggestions_against_parameter_exhaustion,
)
from modules.no_progress_stop_memory import (
    evaluate_no_progress_stop,
    ensure_campaign_start_marker,
)
from modules.manual_review_report import write_v10_9_manual_review_report
from modules.v12_strategy_review import write_v12_strategy_review
from modules.automatic_strategy_updater import update_v11_strategy
from modules.v11_rollback_guard import rollback_latest_nonbest_suggestion
from modules.adaptive_coordinate_optimizer import AdaptiveCoordinateOptimizer
from modules.v13_4_parameter_state import has_best as v13_4_has_best, save_best as v13_4_save_best, restore_best as v13_4_restore_best
from modules.v13_objective_diagnostics import write_candidate_diagnostics
from modules.v13_parameter_state import has_best as v13_has_best, save_best as v13_save_best, restore_best as v13_restore_best, metadata as v13_best_metadata


class V13Workflow:
    """
    MIN3P AI Pipeline V13

    Main structure:
    - deterministic MIN3P run/evaluation/history loop
    - sensitivity analysis
    - process sensitivity report
    - sensitivity interpretation
    - parameter importance
    - deterministic V10 calibration strategy
    - optional GPT scientific supervisor
    - calibration story report

    Important:
    - calibration_strategy.py creates the base V10.9 strategy; V12 overlays calibration_strategy_V11.xlsx and strategy_update_V11.md
    - parameter_importance.py creates parameter_importance_V10.xlsx and parameter_importance_V10.md
    - gpt_supervisor.py reviews the strategy and writes GPT report/recommendations
    - decision_engine.py still performs direct parameter suggestions/application for suggest-only and auto
    """

    def __init__(self, reset_campaign_start: bool = False):
        self.paths = ProjectPaths.from_agent_core(Path(__file__).resolve().parent)
        self.paths.ensure_dirs()

        # V11 campaign marker: no-progress counting starts here,
        # so old optimization_history rows do not prematurely stop a new campaign.
        self.v11_campaign = ensure_campaign_start_marker(
            self.paths.results_dir,
            reset=reset_campaign_start,
        )

        self.config = ConfigReader(self.paths)
        self.builder = DatBuilder(self.paths, self.config)
        self.runner = Min3pRunner(self.paths, self.config)
        self.evaluator = ResultEvaluator(self.paths, self.config)
        self.engine = DeterministicCalibrationEngine(self.paths, self.config)
        self.supervisor = GPTScientificSupervisor(self.paths, self.config)
        self.optimizer_v13 = AdaptiveCoordinateOptimizer(self.paths, self.config)

    @staticmethod
    def _safe_float(value):
        """Return a float or None for empty / invalid values."""
        try:
            if value is None or pd.isna(value):
                return None
            return float(value)
        except Exception:
            return None

    def _extract_objective_score(self, diagnostic: dict) -> float | None:
        """
        Extract the objective score from diagnostic when available.

        Different V10 modules may use different names, so this function checks
        several common score/error/RMSE keys. Lower values are assumed better.
        """
        if not isinstance(diagnostic, dict):
            return None

        preferred_keys = [
            "objective_score",
            "global_score",
            "total_score",
            "weighted_score",
            "calibration_score",
            "global_rmse",
            "mean_rmse",
            "rmse",
            "error_score",
        ]

        for key in preferred_keys:
            value = self._safe_float(diagnostic.get(key))
            if value is not None:
                return value

        return None

    def _get_previous_best_score(self, current_run_dir: Path | None = None) -> float | None:
        """
        Read the best previous objective from run_ranking.xlsx or
        optimization_history.xlsx. The current run is excluded when possible.

        In V10.9 the preferred objective is TOTAL_SCORE from run_ranking.xlsx.
        Lower values are better.
        """
        candidate_files = [
            getattr(self.paths, "ranking_file", None),
            getattr(self.paths, "history_file", None),
        ]

        score_columns = [
            "TOTAL_SCORE",
            "objective_score",
            "global_score",
            "total_score",
            "weighted_score",
            "calibration_score",
            "global_rmse",
            "mean_rmse",
            "rmse",
            "error_score",
        ]

        current_name = Path(current_run_dir).name if current_run_dir else None
        current_path = str(Path(current_run_dir)) if current_run_dir else None

        for file_path in candidate_files:
            if file_path is None or not Path(file_path).exists():
                continue

            try:
                df = pd.read_excel(file_path)
            except Exception:
                continue

            if df.empty:
                continue

            # Exclude the current run when the table contains run identifiers.
            if current_name or current_path:
                for run_col in [
                    "run_folder",
                    "run_dir",
                    "results_folder",
                    "run",
                    "run_id",
                    "run_name",
                    "folder",
                ]:
                    if run_col in df.columns:
                        mask = pd.Series(False, index=df.index)
                        if current_name:
                            mask = mask | df[run_col].astype(str).str.contains(
                                current_name, case=False, na=False, regex=False
                            )
                        if current_path:
                            mask = mask | df[run_col].astype(str).str.contains(
                                current_path, case=False, na=False, regex=False
                            )
                        df = df[~mask]
                        break

            if df.empty:
                continue

            for col in score_columns:
                if col in df.columns:
                    values = pd.to_numeric(df[col], errors="coerce").dropna()
                    if not values.empty:
                        return float(values.min())

        return None

    def _get_current_run_score(self, run_dir: Path) -> float | None:
        """
        Read the current run TOTAL_SCORE from run_ranking.xlsx after rank_runs().

        Robustness notes:
        - V10.9 ranking rows may store the full run_folder path, a relative path,
          a run name, or only results_folder.
        - If no exact match is found, fall back to the latest matching row in
          optimization_history.xlsx and reconstruct the same TOTAL_SCORE logic is
          not attempted here; instead only direct score columns are used.
        """
        ranking_file = getattr(self.paths, "ranking_file", None)
        run_name = Path(run_dir).name
        run_path = str(Path(run_dir))

        def _match_score(df: pd.DataFrame) -> float | None:
            if df is None or df.empty:
                return None

            score_cols = [
                "TOTAL_SCORE",
                "objective_total",
                "objective_score",
                "qc_objective_score",
                "global_score",
                "total_score",
                "weighted_score",
                "calibration_score",
                "global_rmse",
                "mean_rmse",
                "rmse",
                "error_score",
            ]
            existing_score_cols = [c for c in score_cols if c in df.columns]
            if not existing_score_cols:
                return None

            for run_col in [
                "run_folder",
                "run_dir",
                "results_folder",
                "run",
                "run_id",
                "run_name",
                "folder",
            ]:
                if run_col not in df.columns:
                    continue
                series = df[run_col].astype(str)
                mask = (
                    series.str.contains(run_path, case=False, na=False, regex=False)
                    | series.str.contains(run_name, case=False, na=False, regex=False)
                )
                matches = df[mask]
                if not matches.empty:
                    for col in existing_score_cols:
                        scores = pd.to_numeric(matches[col], errors="coerce").dropna()
                        if not scores.empty:
                            return float(scores.iloc[0])
            return None

        if ranking_file is not None and Path(ranking_file).exists():
            try:
                df_rank = pd.read_excel(ranking_file)
                score = _match_score(df_rank)
                if score is not None:
                    return score
            except Exception:
                pass

        # Fallback: direct score aliases in optimization_history, if present.
        history_file = getattr(self.paths, "history_file", None)
        if history_file is not None and Path(history_file).exists():
            try:
                df_hist = pd.read_excel(history_file)
                score = _match_score(df_hist)
                if score is not None:
                    return score
            except Exception:
                pass

        return None

    def _patch_latest_history_qc(self, run_dir: Path, quality: dict) -> None:
        """Update the latest matching history row with final V10.9 QC fields."""
        history_file = getattr(self.paths, "history_file", None)
        if history_file is None or not Path(history_file).exists():
            return

        try:
            df = pd.read_excel(history_file)
        except Exception as exc:
            log(self.paths, f"WARNING: could not patch QC fields in history: {exc}")
            return

        if df.empty:
            return

        run_name = Path(run_dir).name
        run_path = str(Path(run_dir))

        idx = df.index[-1]
        if "run_folder" in df.columns:
            mask = (
                df["run_folder"].astype(str).str.contains(run_path, case=False, na=False, regex=False)
                | df["run_folder"].astype(str).str.contains(run_name, case=False, na=False, regex=False)
            )
            if mask.any():
                idx = df[mask].index[-1]

        qc_map = {
            "qc_run_status": "run_status",
            "qc_normal_exit": "normal_exit",
            "qc_failed_step_fraction": "failed_step_fraction",
            "qc_runtime_warning": "runtime_warning",
            "qc_objective_score": "objective_score",
            "qc_previous_best_score": "previous_best_score",
            "qc_objective_improved": "objective_improved",
            "qc_scientific_rejection": "scientific_rejection",
            "qc_reason": "reason",
            "qc_log_file": "log_file",
        }

        for hist_col, quality_key in qc_map.items():
            if hist_col not in df.columns:
                df[hist_col] = pd.NA
            df.at[idx, hist_col] = quality.get(quality_key)

        # V11 compatibility aliases. Older V11 test code and quick checks use
        # objective_total / best_objective, while V10.9 stores the same
        # information as qc_objective_score / qc_previous_best_score. Write both.
        alias_map = {
            "objective_total": quality.get("objective_score"),
            "best_objective": quality.get("previous_best_score"),
            "objective_improved": quality.get("objective_improved"),
        }
        for hist_col, value in alias_map.items():
            if hist_col not in df.columns:
                df[hist_col] = pd.NA
            df.at[idx, hist_col] = value

        try:
            df.to_excel(history_file, index=False)
        except Exception as exc:
            log(self.paths, f"WARNING: could not write patched QC history: {exc}")


    def _patch_latest_history_acceptance(self, run_dir: Path, acceptance: dict) -> None:
        """Update the latest matching history row with V10.9 acceptance-memory fields."""
        history_file = getattr(self.paths, "history_file", None)
        if history_file is None or not Path(history_file).exists():
            return

        try:
            df = pd.read_excel(history_file)
        except Exception as exc:
            log(self.paths, f"WARNING: could not patch acceptance fields in history: {exc}")
            return

        if df.empty:
            return

        run_name = Path(run_dir).name
        run_path = str(Path(run_dir))

        idx = df.index[-1]
        if "run_folder" in df.columns:
            mask = (
                df["run_folder"].astype(str).str.contains(run_path, case=False, na=False, regex=False)
                | df["run_folder"].astype(str).str.contains(run_name, case=False, na=False, regex=False)
            )
            if mask.any():
                idx = df[mask].index[-1]

        acceptance_map = {
            "qc_acceptance_status": "acceptance_status",
            "qc_is_new_best": "is_new_best",
            "qc_should_update_best": "should_update_best",
            "qc_should_continue_auto": "should_continue_auto",
            "qc_score_change": "score_change",
            "qc_percent_improvement": "percent_improvement",
            "qc_acceptance_reason": "acceptance_reason",
        }

        for hist_col, acceptance_key in acceptance_map.items():
            if hist_col not in df.columns:
                df[hist_col] = pd.NA
            df.at[idx, hist_col] = acceptance.get(acceptance_key)

        # V11 compatibility aliases for quick history inspection.
        alias_map = {
            "decision_action": acceptance.get("acceptance_status"),
            "improvement": acceptance.get("score_change"),
        }
        for hist_col, value in alias_map.items():
            if hist_col not in df.columns:
                df[hist_col] = pd.NA
            df.at[idx, hist_col] = value

        try:
            df.to_excel(history_file, index=False)
        except Exception as exc:
            log(self.paths, f"WARNING: could not write patched acceptance history: {exc}")

    def _patch_latest_history_parameter_memory(self, run_dir: Path, memory: dict) -> None:
        """Update the latest matching history row with V10.9 parameter-memory fields."""
        history_file = getattr(self.paths, "history_file", None)
        if history_file is None or not Path(history_file).exists():
            return

        try:
            df = pd.read_excel(history_file)
        except Exception as exc:
            log(self.paths, f"WARNING: could not patch parameter-memory fields in history: {exc}")
            return

        if df.empty:
            return

        run_name = Path(run_dir).name
        run_path = str(Path(run_dir))

        idx = df.index[-1]
        if "run_folder" in df.columns:
            mask = (
                df["run_folder"].astype(str).str.contains(run_path, case=False, na=False, regex=False)
                | df["run_folder"].astype(str).str.contains(run_name, case=False, na=False, regex=False)
            )
            if mask.any():
                idx = df[mask].index[-1]

        memory_map = {
            "qc_parameter_memory_action": "parameter_memory_action",
            "qc_parameter_memory_reason": "parameter_memory_reason",
            "qc_best_parameters_file": "best_parameters_file",
            "qc_config_backup_file": "config_backup_file",
            "qc_best_source_run_folder": "best_source_run_folder",
            "qc_best_source_score": "best_source_score",
        }

        for hist_col, memory_key in memory_map.items():
            if hist_col not in df.columns:
                df[hist_col] = pd.NA
            df.at[idx, hist_col] = memory.get(memory_key)

        try:
            df.to_excel(history_file, index=False)
        except Exception as exc:
            log(self.paths, f"WARNING: could not write patched parameter-memory history: {exc}")

    def _patch_latest_history_bad_suggestions(self, run_dir: Path, bad_memory: dict) -> None:
        """Update the latest matching history row with V10.9 bad-suggestion fields."""
        history_file = getattr(self.paths, "history_file", None)
        if history_file is None or not Path(history_file).exists():
            return

        try:
            df = pd.read_excel(history_file)
        except Exception as exc:
            log(self.paths, f"WARNING: could not patch bad-suggestion fields in history: {exc}")
            return

        if df.empty:
            return

        run_name = Path(run_dir).name
        run_path = str(Path(run_dir))

        idx = df.index[-1]
        if "run_folder" in df.columns:
            mask = (
                df["run_folder"].astype(str).str.contains(run_path, case=False, na=False, regex=False)
                | df["run_folder"].astype(str).str.contains(run_name, case=False, na=False, regex=False)
            )
            if mask.any():
                idx = df[mask].index[-1]

        bad_map = {
            "qc_bad_suggestion_action": "bad_suggestion_action",
            "qc_bad_suggestion_reason": "bad_suggestion_reason",
            "qc_bad_suggestion_count": "bad_suggestion_count",
            "qc_bad_suggestion_file": "bad_suggestion_file",
            "qc_bad_suggestion_parameters": "bad_suggestion_parameters",
            "qc_bad_suggestion_filter_action": "bad_suggestion_filter_action",
            "qc_bad_suggestion_candidate_count": "bad_suggestion_candidate_count",
            "qc_bad_suggestion_blocked_count": "bad_suggestion_blocked_count",
            "qc_bad_suggestion_kept_count": "bad_suggestion_kept_count",
            "qc_bad_suggestion_blocked_parameters": "bad_suggestion_blocked_parameters",
            "qc_bad_suggestion_allowed_parameters": "bad_suggestion_allowed_parameters",
            "qc_bad_suggestion_filter_reason": "bad_suggestion_filter_reason",
            "qc_bad_suggestion_filter_log_file": "bad_suggestion_filter_log_file",
            "qc_bad_suggestion_fallback_action": "bad_suggestion_fallback_action",
            "qc_bad_suggestion_fallback_used": "bad_suggestion_fallback_used",
            "qc_bad_suggestion_fallback_reason": "bad_suggestion_fallback_reason",
            "qc_bad_suggestion_selected_count": "bad_suggestion_selected_count",
            "qc_bad_suggestion_selected_parameters": "bad_suggestion_selected_parameters",
            "qc_bad_suggestion_requested_changes": "bad_suggestion_requested_changes",
            "qc_bad_suggestion_first_candidate_blocked": "bad_suggestion_first_candidate_blocked",
            "qc_bad_suggestion_opposite_retry_action": "bad_suggestion_opposite_retry_action",
            "qc_bad_suggestion_opposite_retry_used": "bad_suggestion_opposite_retry_used",
            "qc_bad_suggestion_opposite_retry_count": "bad_suggestion_opposite_retry_count",
            "qc_bad_suggestion_opposite_retry_parameters": "bad_suggestion_opposite_retry_parameters",
            "qc_bad_suggestion_opposite_retry_directions": "bad_suggestion_opposite_retry_directions",
            "qc_bad_suggestion_opposite_retry_selected_count": "bad_suggestion_opposite_retry_selected_count",
            "qc_bad_suggestion_opposite_retry_selected_parameters": "bad_suggestion_opposite_retry_selected_parameters",
            "qc_bad_suggestion_opposite_retry_selected_directions": "bad_suggestion_opposite_retry_selected_directions",
            "qc_bad_suggestion_opposite_retry_reason": "bad_suggestion_opposite_retry_reason",
        }

        for hist_col, memory_key in bad_map.items():
            if hist_col not in df.columns:
                df[hist_col] = pd.NA
            if memory_key in bad_memory:
                df.at[idx, hist_col] = bad_memory.get(memory_key)

        try:
            df.to_excel(history_file, index=False)
        except Exception as exc:
            log(self.paths, f"WARNING: could not write patched bad-suggestion history: {exc}")


    def _patch_latest_history_parameter_exhaustion(self, run_dir: Path, exhaustion: dict) -> None:
        """Update the latest matching history row with V10.9 parameter-exhaustion fields."""
        history_file = getattr(self.paths, "history_file", None)
        if history_file is None or not Path(history_file).exists():
            return

        try:
            df = pd.read_excel(history_file)
        except Exception as exc:
            log(self.paths, f"WARNING: could not patch parameter-exhaustion fields in history: {exc}")
            return

        if df.empty:
            return

        run_name = Path(run_dir).name
        run_path = str(Path(run_dir))

        idx = df.index[-1]
        if "run_folder" in df.columns:
            mask = (
                df["run_folder"].astype(str).str.contains(run_path, case=False, na=False, regex=False)
                | df["run_folder"].astype(str).str.contains(run_name, case=False, na=False, regex=False)
            )
            if mask.any():
                idx = df[mask].index[-1]

        exhaustion_map = {
            "qc_parameter_exhaustion_action": "parameter_exhaustion_action",
            "qc_parameter_exhaustion_reason": "parameter_exhaustion_reason",
            "qc_parameter_exhaustion_count": "parameter_exhaustion_count",
            "qc_parameter_exhaustion_parameters": "parameter_exhaustion_parameters",
            "qc_parameter_exhaustion_file": "parameter_exhaustion_file",
            "qc_parameter_exhaustion_filter_action": "parameter_exhaustion_filter_action",
            "qc_parameter_exhaustion_candidate_count": "parameter_exhaustion_candidate_count",
            "qc_parameter_exhaustion_blocked_count": "parameter_exhaustion_blocked_count",
            "qc_parameter_exhaustion_kept_count": "parameter_exhaustion_kept_count",
            "qc_parameter_exhaustion_blocked_parameters": "parameter_exhaustion_blocked_parameters",
            "qc_parameter_exhaustion_allowed_parameters": "parameter_exhaustion_allowed_parameters",
            "qc_parameter_exhaustion_filter_reason": "parameter_exhaustion_filter_reason",
            "qc_parameter_exhaustion_filter_log_file": "parameter_exhaustion_filter_log_file",
        }

        for hist_col, memory_key in exhaustion_map.items():
            if hist_col not in df.columns:
                df[hist_col] = pd.NA
            if memory_key in exhaustion:
                df.at[idx, hist_col] = exhaustion.get(memory_key)

        try:
            df.to_excel(history_file, index=False)
        except Exception as exc:
            log(self.paths, f"WARNING: could not write patched parameter-exhaustion history: {exc}")



    def _patch_latest_history_no_progress(self, run_dir: Path, no_progress: dict) -> None:
        """Update the latest matching history row with V10.9 no-progress stop-control fields."""
        history_file = getattr(self.paths, "history_file", None)
        if history_file is None or not Path(history_file).exists():
            return

        try:
            df = pd.read_excel(history_file)
        except Exception as exc:
            log(self.paths, f"WARNING: could not patch no-progress fields in history: {exc}")
            return

        if df.empty:
            return

        run_name = Path(run_dir).name
        run_path = str(Path(run_dir))

        idx = df.index[-1]
        if "run_folder" in df.columns:
            mask = (
                df["run_folder"].astype(str).str.contains(run_path, case=False, na=False, regex=False)
                | df["run_folder"].astype(str).str.contains(run_name, case=False, na=False, regex=False)
            )
            if mask.any():
                idx = df[mask].index[-1]

        no_progress_map = {
            "qc_no_progress_stop_action": "no_progress_stop_action",
            "qc_no_progress_stop_trigger": "no_progress_stop_trigger",
            "qc_no_progress_should_stop_auto": "no_progress_should_stop_auto",
            "qc_no_progress_reason": "no_progress_reason",
            "qc_no_progress_consecutive_no_best_count": "consecutive_no_best_count",
            "qc_no_progress_max_consecutive_no_best": "max_consecutive_no_best",
            "qc_no_progress_latest_candidate_count": "latest_candidate_count",
            "qc_no_progress_latest_blocked_count": "latest_blocked_count",
            "qc_no_progress_latest_kept_count": "latest_kept_count",
            "qc_no_progress_latest_blocked_ratio": "latest_blocked_ratio",
            "qc_no_progress_latest_blocked_parameters": "latest_blocked_parameters",
            "qc_no_progress_blocked_ratio_limit": "blocked_ratio_limit",
            "qc_no_progress_active_parameter_count": "active_parameter_count",
            "qc_no_progress_exhausted_active_count": "exhausted_active_count",
            "qc_no_progress_direction_limited_active_count": "direction_limited_active_count",
            "qc_no_progress_exhausted_active_parameters": "exhausted_active_parameters",
            "qc_no_progress_direction_limited_active_parameters": "direction_limited_active_parameters",
            "qc_no_progress_active_exhausted_ratio": "active_exhausted_ratio",
            "qc_no_progress_campaign_start_action": "campaign_start_action",
            "qc_no_progress_campaign_start_file": "campaign_start_file",
            "qc_no_progress_campaign_start_timestamp": "campaign_start_timestamp",
            "qc_no_progress_campaign_filter_applied": "campaign_filter_applied",
            "qc_no_progress_campaign_rows_evaluated": "campaign_rows_evaluated",
            "qc_no_progress_stop_file": "no_progress_stop_file",
        }

        for hist_col, memory_key in no_progress_map.items():
            if hist_col not in df.columns:
                df[hist_col] = pd.NA
            if memory_key in no_progress:
                df.at[idx, hist_col] = no_progress.get(memory_key)

        try:
            df.to_excel(history_file, index=False)
        except Exception as exc:
            log(self.paths, f"WARNING: could not write patched no-progress history: {exc}")



    def _patch_latest_history_manual_review_report(self, run_dir: Path, report_info: dict) -> None:
        """Update the latest matching history row with V10.9 manual-review report fields."""
        history_file = getattr(self.paths, "history_file", None)
        if history_file is None or not Path(history_file).exists():
            return

        try:
            df = pd.read_excel(history_file)
        except Exception as exc:
            log(self.paths, f"WARNING: could not patch manual-review fields in history: {exc}")
            return

        if df.empty:
            return

        run_name = Path(run_dir).name if run_dir else ""
        run_path = str(Path(run_dir)) if run_dir else ""

        idx = df.index[-1]
        if "run_folder" in df.columns and (run_name or run_path):
            mask = (
                df["run_folder"].astype(str).str.contains(run_path, case=False, na=False, regex=False)
                | df["run_folder"].astype(str).str.contains(run_name, case=False, na=False, regex=False)
            )
            if mask.any():
                idx = df[mask].index[-1]

        manual_map = {
            "qc_manual_review_report_action": "manual_review_report_action",
            "qc_manual_review_report_context": "manual_review_report_context",
            "qc_manual_review_report_file": "manual_review_report_file",
            "qc_manual_review_report_log_file": "manual_review_report_log_file",
            "qc_manual_review_report_reason": "manual_review_report_reason",
            "qc_manual_review_report_stop_trigger": "manual_review_report_stop_trigger",
            "qc_manual_review_report_timestamp": "manual_review_report_timestamp",
            "qc_manual_review_exhausted_parameters": "manual_review_exhausted_parameters",
            "qc_manual_review_direction_limited_parameters": "manual_review_direction_limited_parameters",
            "qc_manual_review_latest_blocked_parameters": "manual_review_latest_blocked_parameters",
            "qc_v12_strategy_review_action": "v12_strategy_review_action",
            "qc_v12_strategy_review_file": "v12_strategy_review_file",
            "qc_v12_strategy_review_json_file": "v12_strategy_review_json_file",
            "qc_v12_strategy_review_reason": "v12_strategy_review_reason",
            "qc_v12_strategy_review_stop_trigger": "v12_strategy_review_stop_trigger",
        }

        for hist_col, report_key in manual_map.items():
            if hist_col not in df.columns:
                df[hist_col] = pd.NA
            if report_key in report_info:
                df.at[idx, hist_col] = report_info.get(report_key)

        try:
            df.to_excel(history_file, index=False)
        except Exception as exc:
            log(self.paths, f"WARNING: could not write patched manual-review history: {exc}")

    def _write_manual_review_report(
        self,
        *,
        context: str,
        run_dir: Path | None = None,
        stop_info: dict | None = None,
        filter_info: dict | None = None,
        diagnostic: dict | None = None,
    ) -> dict:
        """Create a deterministic V10.9 manual-review report and return metadata."""
        try:
            report_info = write_v10_9_manual_review_report(
                project_dir=self.paths.project_dir,
                results_dir=self.paths.results_dir,
                reports_dir=self.paths.reports_dir,
                history_file=self.paths.history_file,
                ranking_file=self.paths.ranking_file,
                config_file=self.paths.config_file,
                stop_info=stop_info,
                filter_info=filter_info,
                diagnostic=diagnostic,
                run_dir=run_dir,
                context=context,
            )
        except Exception as exc:
            report_info = {
                "manual_review_report_action": "manual_review_report_failed",
                "manual_review_report_context": context,
                "manual_review_report_reason": f"manual-review report failed: {exc}",
                "manual_review_report_stop_trigger": (stop_info or {}).get("no_progress_stop_trigger", "unknown"),
                "manual_review_report_timestamp": pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            log(self.paths, f"WARNING: V10.9 manual-review report failed: {exc}")

        # --------------------------------------------------------------
        # V12 scientific strategy review
        # --------------------------------------------------------------
        # Passive hook only: this writes a review package after a manual-review
        # trigger. It does not change MIN3P execution, scoring, rollback,
        # history mechanics, or candidate selection.
        try:
            stop_info_safe = stop_info or {}
            v12_review = write_v12_strategy_review(
                history=self.paths.history_file,
                output_dir=self.paths.reports_dir,
                stop_trigger=stop_info_safe.get("no_progress_stop_trigger", "unknown"),
                stop_reason=stop_info_safe.get("no_progress_reason", "unknown"),
                project=self.paths.project_dir.name,
                recent_window=10,
            )
            report_info["v12_strategy_review_action"] = "created"
            report_info["v12_strategy_review_file"] = str(v12_review.get("markdown"))
            report_info["v12_strategy_review_json_file"] = str(v12_review.get("json"))
            report_info["v12_strategy_review_reason"] = "created after manual-review trigger"
            report_info["v12_strategy_review_stop_trigger"] = stop_info_safe.get("no_progress_stop_trigger", "unknown")
            log(
                self.paths,
                "V12 scientific strategy review created: "
                f"{report_info.get('v12_strategy_review_file')}",
            )
        except Exception as exc:
            report_info["v12_strategy_review_action"] = "failed"
            report_info["v12_strategy_review_reason"] = f"V12 strategy review failed: {exc}"
            report_info["v12_strategy_review_stop_trigger"] = (stop_info or {}).get("no_progress_stop_trigger", "unknown")
            log(self.paths, f"WARNING: V12 scientific strategy review failed: {exc}")

        return report_info

    def manual_review_report(self, context: str = "manual") -> dict:
        """Create the V10.9 manual-review report on demand."""
        report_info = self._write_manual_review_report(context=context)
        log(
            self.paths,
            "V10.9 manual-review report: "
            f"{report_info.get('manual_review_report_action')} | "
            f"{report_info.get('manual_review_report_file')}",
        )
        return report_info

    @staticmethod
    def _suggestion_candidate_pool_size(max_changes: int) -> int:
        """
        Request extra candidate suggestions so V10.9 can fall back to the next
        unblocked parameter if the top-ranked suggestion repeats a known bad move.
        """
        try:
            n = max(int(max_changes), 1)
        except Exception:
            n = 1
        return max(n + 5, n * 5)

    def _apply_v12_multiplier_scale_to_suggestions(
        self,
        suggestions: pd.DataFrame,
        *,
        context: str,
    ) -> pd.DataFrame:
        """
        Apply the V12 phase-manager multiplier scale to generated suggestions.

        V12.1 writes v12_multiplier_scale to calibration_strategy_V11.xlsx.
        V12.2 uses that value to reduce the actual candidate step size before
        V10.9 bad-suggestion and exhaustion filters are applied.

        Example:
        - normal increase factor 1.10 with scale 0.5 -> 1.05
        - normal decrease factor 0.90 with scale 0.5 -> 0.95
        """
        if suggestions is None or suggestions.empty:
            return suggestions

        strategy_file = self.paths.results_dir / "calibration_strategy_V11.xlsx"
        if not strategy_file.exists():
            return suggestions

        try:
            strategy = pd.read_excel(strategy_file, sheet_name="calibration_strategy")
        except Exception as exc:
            log(self.paths, f"WARNING: V12.2 could not read strategy multiplier scale: {exc}")
            return suggestions

        if strategy.empty or "parameter" not in strategy.columns or "v12_multiplier_scale" not in strategy.columns:
            return suggestions

        scale_map: dict[str, float] = {}
        for _, row in strategy.iterrows():
            parameter = str(row.get("parameter", "")).strip()
            if not parameter or parameter.lower() in ["nan", "none"]:
                continue
            scale = self._safe_float(row.get("v12_multiplier_scale"))
            if scale is None:
                continue
            # Keep the scale positive and bounded. Values >1 would enlarge steps,
            # which is not needed for V12 conservative operation.
            scale_map[parameter] = max(0.01, min(float(scale), 1.0))

        if not scale_map:
            return suggestions

        out = suggestions.copy().reset_index(drop=True)

        for col, default in [
            ("v12_multiplier_scale", 1.0),
            ("v12_factor_before_scale", pd.NA),
            ("v12_factor_after_scale", pd.NA),
            ("v12_step_scaled", False),
        ]:
            if col not in out.columns:
                out[col] = default

        scaled_count = 0

        for idx, row in out.iterrows():
            parameter = str(row.get("parameter", "")).strip()
            if not parameter or parameter.lower() in ["nan", "none"]:
                continue

            scale = scale_map.get(parameter)
            if scale is None or abs(scale - 1.0) < 1e-12:
                continue

            old_value = self._safe_float(row.get("old_value"))
            new_value = self._safe_float(row.get("new_value"))
            if old_value is None or new_value is None:
                continue

            factor_requested = self._safe_float(row.get("factor_requested"))
            if factor_requested is None:
                if abs(old_value) > 0:
                    factor_requested = new_value / old_value
                else:
                    factor_requested = None

            if factor_requested is not None:
                if factor_requested >= 1.0:
                    factor_after_scale = 1.0 + (factor_requested - 1.0) * scale
                else:
                    factor_after_scale = 1.0 - (1.0 - factor_requested) * scale

                if abs(old_value) > 0:
                    scaled_new_value = old_value * factor_after_scale
                else:
                    # Zero baseline: reduce the absolute step directly.
                    scaled_new_value = old_value + (new_value - old_value) * scale
            else:
                factor_after_scale = pd.NA
                scaled_new_value = old_value + (new_value - old_value) * scale

            out.at[idx, "new_value"] = scaled_new_value
            out.at[idx, "factor_applied"] = factor_after_scale
            out.at[idx, "v12_multiplier_scale"] = scale
            out.at[idx, "v12_factor_before_scale"] = factor_requested
            out.at[idx, "v12_factor_after_scale"] = factor_after_scale
            out.at[idx, "v12_step_scaled"] = True

            old_reason = str(row.get("reason", "")).strip()
            v12_reason = (
                f"V12.2 multiplier scale applied in {context}: "
                f"scale={scale:g}, factor {factor_requested} -> {factor_after_scale}"
            )
            out.at[idx, "reason"] = f"{old_reason}; {v12_reason}" if old_reason else v12_reason
            scaled_count += 1

        if scaled_count:
            log(
                self.paths,
                f"V12.2 multiplier scaling applied to {scaled_count} suggestion(s) "
                f"in context={context}",
            )

        return out


    def _patch_latest_history_v11_strategy(self, run_dir: Path | None, strategy_update: dict) -> None:
        """Update the latest matching history row with V11 strategy-updater fields."""
        history_file = getattr(self.paths, "history_file", None)
        if history_file is None or not Path(history_file).exists():
            return

        try:
            df = pd.read_excel(history_file)
        except Exception as exc:
            log(self.paths, f"WARNING: could not patch V11 strategy fields in history: {exc}")
            return

        if df.empty:
            return

        run_name = Path(run_dir).name if run_dir else ""
        run_path = str(Path(run_dir)) if run_dir else ""

        idx = df.index[-1]
        if "run_folder" in df.columns and (run_name or run_path):
            mask = (
                df["run_folder"].astype(str).str.contains(run_path, case=False, na=False, regex=False)
                | df["run_folder"].astype(str).str.contains(run_name, case=False, na=False, regex=False)
            )
            if mask.any():
                idx = df[mask].index[-1]

        v11_map = {
            "qc_v11_strategy_update_action": "strategy_update_action",
            "qc_v11_strategy_update_reason": "strategy_update_reason",
            "qc_v11_strategy_file": "strategy_file",
            "qc_v11_strategy_log_file": "strategy_log_file",
            "qc_v11_strategy_report_file": "strategy_report_file",
            "qc_v11_strategy_available_count": "available_count",
            "qc_v11_strategy_blocked_count": "blocked_count",
            "qc_v11_strategy_next_cycle_parameters": "next_cycle_parameters",
            "qc_v11_strategy_blocked_parameters": "blocked_parameters",
        }

        for hist_col, key in v11_map.items():
            if hist_col not in df.columns:
                df[hist_col] = pd.NA
            if key in strategy_update:
                df.at[idx, hist_col] = strategy_update.get(key)

        try:
            df.to_excel(history_file, index=False)
        except Exception as exc:
            log(self.paths, f"WARNING: could not write patched V11 strategy history: {exc}")



    def _patch_latest_history_v11_rollback_guard(self, run_dir: Path, rollback_info: dict) -> None:
        """Update the latest matching history row with V11 rollback-guard fields."""
        history_file = getattr(self.paths, "history_file", None)
        if history_file is None or not Path(history_file).exists():
            return

        try:
            df = pd.read_excel(history_file)
        except Exception as exc:
            log(self.paths, f"WARNING: could not patch V11 rollback-guard fields in history: {exc}")
            return

        if df.empty:
            return

        run_name = Path(run_dir).name if run_dir else ""
        run_path = str(Path(run_dir)) if run_dir else ""

        idx = df.index[-1]
        if "run_folder" in df.columns and (run_name or run_path):
            mask = (
                df["run_folder"].astype(str).str.contains(run_path, case=False, na=False, regex=False)
                | df["run_folder"].astype(str).str.contains(run_name, case=False, na=False, regex=False)
            )
            if mask.any():
                idx = df[mask].index[-1]

        rollback_map = {
            "qc_v11_rollback_guard_action": "action",
            "qc_v11_rollback_guard_parameter": "parameter",
            "qc_v11_rollback_guard_old_value": "old_value",
            "qc_v11_rollback_guard_new_value": "new_value",
            "qc_v11_rollback_guard_current_before": "current_value_before",
            "qc_v11_rollback_guard_current_after": "current_value_after",
            "qc_v11_rollback_guard_acceptance": "acceptance",
            "qc_v11_rollback_guard_stop_action": "stop_action",
            "qc_v11_rollback_guard_objective_total": "objective_total",
            "qc_v11_rollback_guard_best_objective": "best_objective",
        }

        for hist_col, key in rollback_map.items():
            if hist_col not in df.columns:
                df[hist_col] = pd.NA
            if key in rollback_info:
                df.at[idx, hist_col] = rollback_info.get(key)

        try:
            df.to_excel(history_file, index=False)
        except Exception as exc:
            log(self.paths, f"WARNING: could not write patched V11 rollback-guard history: {exc}")

    def _write_run_quality_summary(self, quality: dict) -> None:
        """Append the latest quality classification to 04_results."""
        output_file = self.paths.results_dir / "run_quality_summary_V10_9.xlsx"
        row = pd.DataFrame([quality])

        if output_file.exists():
            try:
                old = pd.read_excel(output_file)
                row = pd.concat([old, row], ignore_index=True)
            except Exception:
                pass

        row.to_excel(output_file, index=False)

    def single_cycle(self, skip_gpt: bool = True, allow_restore: bool = False) -> dict:
        dat_file = self.builder.build()

        # Previous best must be read before the new run is added to history/ranking.
        previous_best_score = self._get_previous_best_score()

        run_dir, results_dir, return_code = self.runner.run(dat_file)

        diagnostic = analyze_run(
            run_dir,
            results_dir,
            return_code,
            self.config,
        )
        diagnostic["run_folder"] = str(run_dir)
        diagnostic["results_folder"] = str(results_dir) if results_dir else ""

        if results_dir is not None and diagnostic.get("run_status") in [
            "success",
            "success_with_retries",
            "partial_success",
        ]:
            try:
                self.evaluator.compare(results_dir)
            except Exception as exc:
                log(self.paths, f"WARNING: comparison failed: {exc}")

        # ------------------------------------------------------------------
        # V10.9 run-quality classification - initial pass
        # ------------------------------------------------------------------
        # At this point TOTAL_SCORE does not exist yet for the current run,
        # so this first pass checks runtime/log quality only.
        try:
            quality = classify_and_save(
                run_dir=run_dir,
                objective_score=None,
                previous_best_score=previous_best_score,
            )
        except Exception as exc:
            quality = {
                "run_status": "quality_check_failed",
                "scientific_rejection": False,
                "reason": f"run-quality classifier failed: {exc}",
            }
            log(self.paths, f"WARNING: run-quality classifier failed: {exc}")

        diagnostic["quality_run_status"] = quality.get("run_status")
        diagnostic["quality_runtime_warning"] = quality.get("runtime_warning")
        diagnostic["quality_scientific_rejection"] = quality.get("scientific_rejection")
        diagnostic["quality_reason"] = quality.get("reason")

        # Fields written to optimization_history.xlsx by modules/history.py.
        # These are patched with TOTAL_SCORE after rank_runs().
        diagnostic.update(
            {
                "qc_run_status": quality.get("run_status"),
                "qc_normal_exit": quality.get("normal_exit"),
                "qc_failed_step_fraction": quality.get("failed_step_fraction"),
                "qc_runtime_warning": quality.get("runtime_warning"),
                "qc_objective_score": quality.get("objective_score"),
                "qc_previous_best_score": quality.get("previous_best_score"),
                "qc_objective_improved": quality.get("objective_improved"),
                "qc_scientific_rejection": quality.get("scientific_rejection"),
                "qc_reason": quality.get("reason"),
                "qc_log_file": quality.get("log_file"),
            }
        )

        update_history(
            self.paths,
            self.config,
            run_dir,
            results_dir,
            diagnostic,
        )

        rank_runs(self.paths, self.config)

        # ------------------------------------------------------------------
        # V10.9 run-quality classification - final scored pass
        # ------------------------------------------------------------------
        # Now run_ranking.xlsx exists/has been refreshed, so use TOTAL_SCORE
        # as the objective score. Lower TOTAL_SCORE is better.
        objective_score = self._get_current_run_score(run_dir)
        previous_best_score = self._get_previous_best_score(current_run_dir=run_dir)

        try:
            quality = classify_and_save(
                run_dir=run_dir,
                objective_score=objective_score,
                previous_best_score=previous_best_score,
            )
        except Exception as exc:
            quality = {
                "run_status": "quality_check_failed",
                "scientific_rejection": False,
                "reason": f"run-quality classifier failed after ranking: {exc}",
                "objective_score": objective_score,
                "previous_best_score": previous_best_score,
            }
            log(self.paths, f"WARNING: scored run-quality classifier failed: {exc}")

        self._patch_latest_history_qc(run_dir, quality)
        self._write_run_quality_summary(quality)

        # ------------------------------------------------------------------
        # V10.9 accept/reject memory
        # ------------------------------------------------------------------
        acceptance = classify_acceptance(quality, run_dir=run_dir)
        append_acceptance_memory(self.paths.results_dir, acceptance)
        self._patch_latest_history_acceptance(run_dir, acceptance)

        # ------------------------------------------------------------------
        # V10.9 bad-suggestion memory
        # ------------------------------------------------------------------
        # If the run is valid but worse than the previous best, identify which
        # parameter values differed from best_parameters_V10_9.xlsx and record
        # the same parameter + direction as a bad move to avoid repeating it.
        bad_memory = update_bad_suggestion_memory(
            results_dir=self.paths.results_dir,
            history_file=self.paths.history_file,
            best_parameters_file=self.paths.results_dir / "best_parameters_V10_9.xlsx",
            acceptance=acceptance,
            run_dir=run_dir,
        )
        self._patch_latest_history_bad_suggestions(run_dir, bad_memory)

        diagnostic["qc_bad_suggestion_action"] = bad_memory.get("bad_suggestion_action")
        diagnostic["qc_bad_suggestion_reason"] = bad_memory.get("bad_suggestion_reason")
        diagnostic["qc_bad_suggestion_count"] = bad_memory.get("bad_suggestion_count")
        diagnostic["qc_bad_suggestion_file"] = bad_memory.get("bad_suggestion_file")
        diagnostic["qc_bad_suggestion_parameters"] = bad_memory.get("bad_suggestion_parameters")

        # ------------------------------------------------------------------
        # V10.9 parameter-exhaustion memory
        # ------------------------------------------------------------------
        # If both directions of one parameter were bad, temporarily exclude it
        # from future deterministic suggestions.
        parameter_exhaustion = update_parameter_exhaustion_memory(
            results_dir=self.paths.results_dir,
        )
        self._patch_latest_history_parameter_exhaustion(run_dir, parameter_exhaustion)

        diagnostic["qc_parameter_exhaustion_action"] = parameter_exhaustion.get("parameter_exhaustion_action")
        diagnostic["qc_parameter_exhaustion_reason"] = parameter_exhaustion.get("parameter_exhaustion_reason")
        diagnostic["qc_parameter_exhaustion_count"] = parameter_exhaustion.get("parameter_exhaustion_count")
        diagnostic["qc_parameter_exhaustion_parameters"] = parameter_exhaustion.get("parameter_exhaustion_parameters")
        diagnostic["qc_parameter_exhaustion_file"] = parameter_exhaustion.get("parameter_exhaustion_file")

        # ------------------------------------------------------------------
        # V10.9 rollback / best-parameter memory
        # ------------------------------------------------------------------
        # In single mode, accepted-best runs are saved, but worse valid runs do
        # not automatically overwrite active agent_config.xlsx. In auto mode,
        # allow_restore=True restores the best parameter set before the next
        # suggestion is generated.
        parameter_memory = apply_parameter_memory_policy(
            config_file=self.paths.config_file,
            results_dir=self.paths.results_dir,
            history_file=self.paths.history_file,
            ranking_file=self.paths.ranking_file,
            acceptance=acceptance,
            allow_restore=allow_restore,
        )
        self._patch_latest_history_parameter_memory(run_dir, parameter_memory)

        diagnostic["qc_parameter_memory_action"] = parameter_memory.get("parameter_memory_action")
        diagnostic["qc_parameter_memory_reason"] = parameter_memory.get("parameter_memory_reason")
        diagnostic["qc_best_parameters_file"] = parameter_memory.get("best_parameters_file")
        diagnostic["qc_config_backup_file"] = parameter_memory.get("config_backup_file")
        diagnostic["qc_best_source_run_folder"] = parameter_memory.get("best_source_run_folder")
        diagnostic["qc_best_source_score"] = parameter_memory.get("best_source_score")

        # ------------------------------------------------------------------
        # V11 rollback guard
        # ------------------------------------------------------------------
        # Defensive safeguard: after every evaluated run, rollback the latest
        # applied V11 suggestion if the run was valid but not a new best. This
        # is intentionally placed before no-progress/manual-review stop logic,
        # so auto mode cannot stop while leaving a bad tested value in
        # agent_config.xlsx.
        try:
            rollback_guard = rollback_latest_nonbest_suggestion(self.paths.project_dir)
        except Exception as exc:
            rollback_guard = {
                "action": "rollback_guard_failed",
                "reason": f"V11 rollback guard failed: {exc}",
            }
            log(self.paths, f"WARNING: V11 rollback guard failed: {exc}")

        self._patch_latest_history_v11_rollback_guard(run_dir, rollback_guard)
        diagnostic["qc_v11_rollback_guard_action"] = rollback_guard.get("action")
        diagnostic["qc_v11_rollback_guard_parameter"] = rollback_guard.get("parameter")
        diagnostic["qc_v11_rollback_guard_old_value"] = rollback_guard.get("old_value")
        diagnostic["qc_v11_rollback_guard_new_value"] = rollback_guard.get("new_value")
        diagnostic["qc_v11_rollback_guard_current_before"] = rollback_guard.get("current_value_before")
        diagnostic["qc_v11_rollback_guard_current_after"] = rollback_guard.get("current_value_after")

        log(
            self.paths,
            "V11 rollback guard: "
            f"{rollback_guard.get('action')} | "
            f"parameter={rollback_guard.get('parameter')} | "
            f"before={rollback_guard.get('current_value_before')} | "
            f"after={rollback_guard.get('current_value_after')}",
        )

        diagnostic["quality_run_status"] = quality.get("run_status")
        diagnostic["quality_runtime_warning"] = quality.get("runtime_warning")
        diagnostic["quality_scientific_rejection"] = quality.get("scientific_rejection")
        diagnostic["quality_reason"] = quality.get("reason")
        diagnostic["objective_score"] = objective_score
        diagnostic["previous_best_score"] = previous_best_score
        diagnostic["objective_improved"] = quality.get("objective_improved")
        diagnostic["qc_acceptance_status"] = acceptance.get("acceptance_status")
        diagnostic["qc_is_new_best"] = acceptance.get("is_new_best")
        diagnostic["qc_should_update_best"] = acceptance.get("should_update_best")
        diagnostic["qc_should_continue_auto"] = acceptance.get("should_continue_auto")
        diagnostic["qc_acceptance_reason"] = acceptance.get("acceptance_reason")

        # ------------------------------------------------------------------
        # V10.9 no-progress stop control
        # ------------------------------------------------------------------
        no_progress = evaluate_no_progress_stop(
            results_dir=self.paths.results_dir,
            history_file=self.paths.history_file,
            config_file=self.paths.config_file,
            context="after_run",
        )
        self._patch_latest_history_no_progress(run_dir, no_progress)

        diagnostic["qc_no_progress_stop_action"] = no_progress.get("no_progress_stop_action")
        diagnostic["qc_no_progress_stop_trigger"] = no_progress.get("no_progress_stop_trigger")
        diagnostic["qc_no_progress_should_stop_auto"] = no_progress.get("no_progress_should_stop_auto")
        diagnostic["qc_no_progress_reason"] = no_progress.get("no_progress_reason")
        diagnostic["qc_no_progress_consecutive_no_best_count"] = no_progress.get("consecutive_no_best_count")
        diagnostic["qc_no_progress_latest_blocked_ratio"] = no_progress.get("latest_blocked_ratio")
        diagnostic["qc_no_progress_active_parameter_count"] = no_progress.get("active_parameter_count")
        diagnostic["qc_no_progress_exhausted_active_count"] = no_progress.get("exhausted_active_count")
        diagnostic["qc_no_progress_campaign_start_file"] = no_progress.get("campaign_start_file")
        diagnostic["qc_no_progress_campaign_start_timestamp"] = no_progress.get("campaign_start_timestamp")
        diagnostic["qc_no_progress_campaign_rows_evaluated"] = no_progress.get("campaign_rows_evaluated")
        diagnostic["qc_no_progress_stop_file"] = no_progress.get("no_progress_stop_file")

        # ------------------------------------------------------------------
        # V11 automatic strategy updater
        # ------------------------------------------------------------------
        # This keeps the V10.9 deterministic safety memory, but creates a V11
        # strategy overlay before the next suggestion is generated.
        try:
            v11_strategy_update = update_v11_strategy(
                paths=self.paths,
                config=self.config,
                context="after_run",
                max_next_cycle=1,
                acceptance=acceptance,
                quality=quality,
                bad_memory=bad_memory,
                parameter_exhaustion=parameter_exhaustion,
                no_progress=no_progress,
            )
        except Exception as exc:
            v11_strategy_update = {
                "strategy_update_action": "failed",
                "strategy_update_reason": f"V11 strategy updater failed: {exc}",
                "strategy_file": str(self.paths.results_dir / "calibration_strategy_V11.xlsx"),
                "strategy_log_file": str(self.paths.results_dir / "strategy_update_log_V11.xlsx"),
                "strategy_report_file": str(self.paths.reports_dir / "strategy_update_V11.md"),
                "available_count": 0,
                "blocked_count": 0,
                "next_cycle_parameters": "",
                "blocked_parameters": "",
            }
            log(self.paths, f"WARNING: V11 strategy updater failed: {exc}")

        self._patch_latest_history_v11_strategy(run_dir, v11_strategy_update)
        diagnostic["qc_v11_strategy_update_action"] = v11_strategy_update.get("strategy_update_action")
        diagnostic["qc_v11_strategy_update_reason"] = v11_strategy_update.get("strategy_update_reason")
        diagnostic["qc_v11_strategy_file"] = v11_strategy_update.get("strategy_file")
        diagnostic["qc_v11_strategy_next_cycle_parameters"] = v11_strategy_update.get("next_cycle_parameters")
        diagnostic["qc_v11_strategy_blocked_parameters"] = v11_strategy_update.get("blocked_parameters")

        log(
            self.paths,
            "V10.9 run quality: "
            f"{quality.get('run_status')} | "
            f"objective={quality.get('objective_score')} | "
            f"previous_best={quality.get('previous_best_score')} | "
            f"improved={quality.get('objective_improved')} | "
            f"rejection={quality.get('scientific_rejection')} | "
            f"warning={quality.get('runtime_warning')}",
        )

        log(
            self.paths,
            "V10.9 acceptance: "
            f"{acceptance.get('acceptance_status')} | "
            f"new_best={acceptance.get('is_new_best')} | "
            f"score_change={acceptance.get('score_change')} | "
            f"improvement_pct={acceptance.get('percent_improvement')}",
        )

        log(
            self.paths,
            "V10.9 parameter memory: "
            f"{parameter_memory.get('parameter_memory_action')} | "
            f"{parameter_memory.get('parameter_memory_reason')}",
        )

        log(
            self.paths,
            "V10.9 bad-suggestion memory: "
            f"{bad_memory.get('bad_suggestion_action')} | "
            f"count={bad_memory.get('bad_suggestion_count')} | "
            f"{bad_memory.get('bad_suggestion_reason')}",
        )

        log(
            self.paths,
            "V10.9 parameter exhaustion: "
            f"{parameter_exhaustion.get('parameter_exhaustion_action')} | "
            f"count={parameter_exhaustion.get('parameter_exhaustion_count')} | "
            f"parameters={parameter_exhaustion.get('parameter_exhaustion_parameters')}",
        )

        log(
            self.paths,
            "V10.9 no-progress stop control: "
            f"{no_progress.get('no_progress_stop_action')} | "
            f"trigger={no_progress.get('no_progress_stop_trigger')} | "
            f"no_best={no_progress.get('consecutive_no_best_count')} | "
            f"blocked_ratio={no_progress.get('latest_blocked_ratio')}",
        )

        if no_progress.get("no_progress_should_stop_auto") is True:
            manual_review = self._write_manual_review_report(
                context="after_run_stop",
                run_dir=run_dir,
                stop_info=no_progress,
                diagnostic=diagnostic,
            )
            self._patch_latest_history_manual_review_report(run_dir, manual_review)
            diagnostic["qc_manual_review_report_action"] = manual_review.get("manual_review_report_action")
            diagnostic["qc_manual_review_report_file"] = manual_review.get("manual_review_report_file")
            diagnostic["qc_manual_review_report_reason"] = manual_review.get("manual_review_report_reason")
            diagnostic["qc_manual_review_report_stop_trigger"] = manual_review.get("manual_review_report_stop_trigger")
            log(
                self.paths,
                "V10.9 manual-review report created: "
                f"{manual_review.get('manual_review_report_file')}",
            )

        if not skip_gpt:
            self.supervisor.review()

        write_calibration_report(self.paths)

        return diagnostic


    def _latest_history_dict(self) -> dict:
        """Return the latest optimization_history row as a plain dict."""
        history_file = self.paths.history_file
        if not history_file.exists():
            return {}
        try:
            df = pd.read_excel(history_file)
        except Exception as exc:
            log(self.paths, f"WARNING: could not read history before restore check: {exc}")
            return {}
        if df.empty:
            return {}
        return df.iloc[-1].to_dict()

    def _latest_run_needs_restore(self, latest: dict) -> bool:
        """Decide whether active agent_config should be restored to best before applying a new suggestion."""
        if not latest:
            return False

        status = str(
            latest.get("qc_acceptance_status", latest.get("acceptance_status", ""))
        ).strip().lower()
        if status in {"valid_not_new_best", "rejected_scientific", "failed"}:
            return True

        objective = self._safe_float(
            latest.get("objective_total", latest.get("qc_objective_score"))
        )
        best = self._safe_float(
            latest.get("best_objective", latest.get("qc_previous_best_score"))
        )
        if objective is not None and best is not None and objective > best:
            return True

        return False

    def restore_best_config(self, rebuild_from_history: bool = False, context: str = "manual") -> dict:
        """Restore active agent_config.xlsx from the best parameter-memory file.

        If rebuild_from_history=True, first recreate best_parameters_V10_9.xlsx
        from the best scored row in run_ranking.xlsx / optimization_history.xlsx.
        """
        if rebuild_from_history:
            rebuild = create_best_parameters_from_history(
                config_file=self.paths.config_file,
                results_dir=self.paths.results_dir,
                ranking_file=self.paths.ranking_file,
                history_file=self.paths.history_file,
            )
            log(
                self.paths,
                "V11 rebuilt best-parameter memory before restore: "
                f"{rebuild.get('parameter_memory_action')} | "
                f"{rebuild.get('parameter_memory_reason')}",
            )

        latest = self._latest_history_dict()
        acceptance = {
            "acceptance_status": latest.get("qc_acceptance_status", latest.get("acceptance_status", "manual_restore")),
            "objective_score": latest.get("objective_total", latest.get("qc_objective_score")),
            "previous_best_score": latest.get("best_objective", latest.get("qc_previous_best_score")),
            "run_folder": latest.get("run_folder", ""),
        }
        info = restore_best_parameters(
            config_file=self.paths.config_file,
            results_dir=self.paths.results_dir,
            ranking_file=self.paths.ranking_file,
            history_file=self.paths.history_file,
            acceptance=acceptance,
        )
        log(
            self.paths,
            f"V11 restore-best ({context}): "
            f"{info.get('parameter_memory_action')} | "
            f"{info.get('parameter_memory_reason')}",
        )
        return info

    def _restore_best_before_applying_suggestion(self, context: str) -> dict:
        """Safeguard manual apply-suggestions mode.

        The manual loop is normally: single -> apply-suggestions -> single.
        If the previous run was valid but worse than the best, restore the
        best agent_config.xlsx before applying the next parameter change. This
        prevents accumulation of failed changes during manual calibration.
        """
        latest = self._latest_history_dict()
        if not self._latest_run_needs_restore(latest):
            info = {
                "parameter_memory_action": "restore_not_needed",
                "parameter_memory_reason": "latest history row is not worse than best, or no scored row is available",
            }
            log(self.paths, f"V11 restore-before-suggestion ({context}): {info['parameter_memory_reason']}")
            return info

        return self.restore_best_config(rebuild_from_history=True, context=context)


    def _append_selected_suggestions(
        self,
        suggestions: pd.DataFrame,
        filter_info: dict | None,
        context: str,
        applied_to_agent_config: bool,
    ) -> None:
        """
        Append only the final selected suggestion(s) after V10.9/V11 filtering.

        The engine may request a larger candidate pool so bad/exhausted moves can
        be skipped. That candidate pool is audited separately in
        candidate_suggestions_V11.xlsx. parameter_suggestions_V11.xlsx should show
        only the suggestion(s) actually selected for the next configuration update.
        """
        if suggestions is None or suggestions.empty:
            return

        out = suggestions.copy().reset_index(drop=True)
        out["timestamp"] = timestamp()
        out["selection_status"] = "selected_after_v10_9_v11_filters"
        out["selection_context"] = context
        out["applied_to_agent_config"] = bool(applied_to_agent_config)

        if filter_info:
            keep = [
                "bad_suggestion_fallback_action",
                "bad_suggestion_fallback_reason",
                "bad_suggestion_selected_count",
                "bad_suggestion_selected_parameters",
                "bad_suggestion_blocked_count",
                "bad_suggestion_kept_count",
                "parameter_exhaustion_blocked_count",
                "parameter_exhaustion_kept_count",
            ]
            for k in keep:
                if k in filter_info:
                    out[k] = filter_info.get(k)

        append_df_excel(self.paths.suggestions_file, out)

    def suggest_only(
        self,
        max_changes: int = 2,
        skip_gpt: bool = True,
    ) -> pd.DataFrame:
        candidate_pool_size = self._suggestion_candidate_pool_size(max_changes)
        candidate_suggestions = self.engine.suggest(max_changes=candidate_pool_size)
        candidate_suggestions = self._apply_v12_multiplier_scale_to_suggestions(
            candidate_suggestions,
            context="suggest_only",
        )
        exhaustion_filtered, exhaustion_info = filter_suggestions_against_parameter_exhaustion(
            candidate_suggestions,
            self.paths.results_dir,
        )
        filtered_suggestions, filter_info = filter_suggestions_against_bad_memory(
            exhaustion_filtered,
            self.paths.results_dir,
        )
        filter_info.update(exhaustion_info)
        suggestions, filter_info = select_suggestions_with_fallback(
            filtered_suggestions,
            filter_info,
            max_changes=max_changes,
            results_dir=self.paths.results_dir,
        )
        log(
            self.paths,
            "V10.9 bad-suggestion filter/fallback: "
            f"{filter_info.get('bad_suggestion_filter_action')} | "
            f"fallback={filter_info.get('bad_suggestion_fallback_action')} | "
            f"blocked={filter_info.get('bad_suggestion_blocked_count')} | "
            f"selected={filter_info.get('bad_suggestion_selected_count')}",
        )
        log(
            self.paths,
            "V10.9 parameter-exhaustion filter: "
            f"{filter_info.get('parameter_exhaustion_filter_action')} | "
            f"blocked={filter_info.get('parameter_exhaustion_blocked_count')} | "
            f"kept={filter_info.get('parameter_exhaustion_kept_count')}",
        )

        self._append_selected_suggestions(
            suggestions,
            filter_info,
            context="suggest_only",
            applied_to_agent_config=False,
        )

        write_calibration_report(self.paths)

        if not skip_gpt:
            self.supervisor.review()

        return suggestions

    def apply_suggestions(
        self,
        max_changes: int = 2,
        skip_gpt: bool = True,
    ) -> pd.DataFrame:
        # Critical V11 safeguard for the manual workflow:
        # restore the best known agent_config before applying a new suggestion
        # if the latest evaluated run was valid but not better than the best.
        self._restore_best_before_applying_suggestion(context="apply_suggestions")

        candidate_pool_size = self._suggestion_candidate_pool_size(max_changes)
        candidate_suggestions = self.engine.suggest(max_changes=candidate_pool_size)
        candidate_suggestions = self._apply_v12_multiplier_scale_to_suggestions(
            candidate_suggestions,
            context="apply_suggestions",
        )
        exhaustion_filtered, exhaustion_info = filter_suggestions_against_parameter_exhaustion(
            candidate_suggestions,
            self.paths.results_dir,
        )
        filtered_suggestions, filter_info = filter_suggestions_against_bad_memory(
            exhaustion_filtered,
            self.paths.results_dir,
        )
        filter_info.update(exhaustion_info)
        suggestions, filter_info = select_suggestions_with_fallback(
            filtered_suggestions,
            filter_info,
            max_changes=max_changes,
            results_dir=self.paths.results_dir,
        )
        log(
            self.paths,
            "V10.9 bad-suggestion filter/fallback: "
            f"{filter_info.get('bad_suggestion_filter_action')} | "
            f"fallback={filter_info.get('bad_suggestion_fallback_action')} | "
            f"blocked={filter_info.get('bad_suggestion_blocked_count')} | "
            f"selected={filter_info.get('bad_suggestion_selected_count')}",
        )
        log(
            self.paths,
            "V10.9 parameter-exhaustion filter: "
            f"{filter_info.get('parameter_exhaustion_filter_action')} | "
            f"blocked={filter_info.get('parameter_exhaustion_blocked_count')} | "
            f"kept={filter_info.get('parameter_exhaustion_kept_count')}",
        )
        if suggestions.empty:
            log(
                self.paths,
                "All suggestions were blocked by bad-suggestion memory; "
                "no parameter change applied.",
            )
            write_calibration_report(self.paths)
            return suggestions

        self._append_selected_suggestions(
            suggestions,
            filter_info,
            context="apply_suggestions",
            applied_to_agent_config=True,
        )
        self.engine.apply_suggestions(suggestions)

        write_calibration_report(self.paths)

        if not skip_gpt:
            self.supervisor.review()

        return suggestions


    def _v13_single_cycle(self) -> dict:
        """Run one MIN3P candidate without invoking any V10-V12 control layer."""
        dat_file = self.builder.build()
        run_dir, results_dir, return_code = self.runner.run(dat_file)

        diagnostic = analyze_run(run_dir, results_dir, return_code, self.config)
        diagnostic["run_folder"] = str(run_dir)
        diagnostic["results_folder"] = str(results_dir) if results_dir else ""

        if results_dir is not None and diagnostic.get("run_status") in {
            "success", "success_with_retries", "partial_success"
        }:
            try:
                self.evaluator.compare(results_dir)
            except Exception as exc:
                log(self.paths, f"WARNING: V13.2 comparison failed: {exc}")

        update_history(self.paths, self.config, run_dir, results_dir, diagnostic)
        rank_runs(self.paths, self.config)

        total_score = self._get_current_run_score(run_dir)
        diagnostic["TOTAL_SCORE"] = total_score
        return diagnostic

    def _v13_patch_history(self, run_dir: Path, score: float | None, event: dict) -> None:
        """Persist isolated V13.2 decision fields on the matching history row."""
        history_file = self.paths.history_file
        if not history_file.exists():
            return
        try:
            df = pd.read_excel(history_file)
            if df.empty:
                return
            idx = df.index[-1]
            if "run_folder" in df.columns:
                name = Path(run_dir).name
                match = df["run_folder"].astype(str).str.contains(
                    name, case=False, na=False, regex=False
                )
                if match.any():
                    idx = df[match].index[-1]

            values = {
                "TOTAL_SCORE": score,
                "v13_optimizer_version": event.get("optimizer_version", "V13.2"),
                "v13_decision": event.get("action", ""),
                "v13_accepted": event.get("accepted", False),
                "v13_baseline_score": event.get("baseline_objective"),
                "v13_candidate_score": event.get("candidate_objective", score),
                "v13_parameter": event.get("parameter", ""),
                "v13_direction": event.get("direction", ""),
                "v13_step_fraction": event.get("step_fraction"),
                "v13_trajectory_id": event.get("trajectory_id", ""),
                # Compatibility fields are written from the same V13 score,
                # not derived from old V10-V12 objective values.
                "qc_acceptance_status": "accepted_best" if event.get("accepted") else "accepted_valid_not_best",
                "qc_is_new_best": bool(event.get("accepted")),
                "qc_should_update_best": bool(event.get("accepted")),
                "qc_previous_best_score": event.get("baseline_objective"),
                "qc_objective_score": score,
                "objective_total": score,
                "best_objective": event.get("baseline_objective"),
                "qc_acceptance_reason": (
                    "V13.2 isolated TOTAL_SCORE decision"
                    if event.get("action") != "baseline_observed"
                    else "V13.2 baseline established"
                ),
            }
            for column, value in values.items():
                if column not in df.columns:
                    df[column] = pd.NA
                df.at[idx, column] = value
            df.to_excel(history_file, index=False)
        except Exception as exc:
            log(self.paths, f"WARNING: V13.2 could not patch history: {exc}")

    def _v13_ensure_best_memory(self, score: float, run_dir: Path) -> None:
        """Create dedicated V13 best memory only when it does not yet exist."""
        if v13_has_best(self.paths.results_dir):
            return
        v13_save_best(
            self.paths.config_file,
            self.paths.results_dir,
            total_score=score,
            run_folder=str(run_dir),
            objective_reference_file=str(self.paths.results_dir / "objective_reference_V13.xlsx"),
            optimizer_version="V13.2",
        )


    def _v13_4_patch_history(self, run_dir: Path, score: float | None, event: dict) -> None:
        """Persist V13.4 staged-decision fields without invoking legacy control."""
        history_file = self.paths.history_file
        if not history_file.exists():
            return
        try:
            df = pd.read_excel(history_file)
            if df.empty:
                return
            idx = df.index[-1]
            if "run_folder" in df.columns:
                run_name = Path(run_dir).name
                match = df["run_folder"].astype(str).str.contains(run_name, case=False, na=False, regex=False)
                if match.any():
                    idx = df[match].index[-1]

            accepted = bool(event.get("accepted"))
            values = {
                "TOTAL_SCORE": score,
                "v13_4_optimizer_version": event.get("optimizer_version", "V13.4"),
                "v13_4_stage": event.get("group", ""),
                "v13_4_decision": event.get("action", ""),
                "v13_4_accepted": accepted,
                "v13_4_baseline_score": event.get("baseline_objective"),
                "v13_4_candidate_score": event.get("candidate_objective", score),
                "v13_4_parameter": event.get("parameter", ""),
                "v13_4_direction": event.get("direction", ""),
                "v13_4_step_fraction": event.get("step_fraction"),
                "v13_4_tradeoff_class": event.get("tradeoff_class", ""),
                "v13_4_scientific_ok": event.get("scientific_ok"),
                "v13_4_constraint_failures": event.get("constraint_failures", ""),
                "qc_acceptance_status": "accepted_best" if accepted else "accepted_valid_not_best",
                "qc_is_new_best": accepted,
                "qc_should_update_best": accepted,
                "qc_previous_best_score": event.get("baseline_objective"),
                "qc_objective_score": score,
                "objective_total": score,
                "best_objective": event.get("baseline_objective"),
                "qc_acceptance_reason": "V13.4 staged TOTAL_SCORE decision",
            }
            for column, value in values.items():
                if column not in df.columns:
                    df[column] = pd.NA
                df.at[idx, column] = value
            df.to_excel(history_file, index=False)
        except Exception as exc:
            log(self.paths, f"WARNING: V13.4 could not patch history: {exc}")

    def _patch_latest_history_v13(self, run_dir: Path, event: dict, accepted_baseline: float | None) -> None:
        """Persist V13 trajectory audit values on the already-created V12 history row."""
        f = self.paths.history_file
        if not f.exists():
            return
        try:
            df = pd.read_excel(f)
            if df.empty:
                return
            idx = df.index[-1]
            if "run_folder" in df.columns:
                name = Path(run_dir).name
                mask = df["run_folder"].astype(str).str.contains(name, case=False, na=False, regex=False)
                if mask.any():
                    idx = df[mask].index[-1]
            values = {
                "optimizer_version": event.get("optimizer_version", "V13.1"),
                "optimizer_mode": "adaptive_coordinate_search",
                "pass_number": event.get("pass_number"),
                "trajectory_id": event.get("trajectory_id"),
                "optimizer_parameter": event.get("parameter"),
                "optimizer_direction": event.get("direction"),
                "optimizer_step_fraction": event.get("step_fraction"),
                "optimizer_baseline_value": event.get("baseline_value"),
                "optimizer_candidate_value": event.get("candidate_value"),
                "optimizer_baseline_objective": event.get("baseline_objective"),
                "optimizer_candidate_objective": event.get("candidate_objective"),
                "optimizer_event_action": event.get("action"),
                "optimizer_trajectory_state": event.get("action"),
                "optimizer_convergence_status": "running",
                "optimizer_convergence_reason": "",
            }
            for col, value in values.items():
                if col not in df.columns:
                    df[col] = pd.NA
                df.at[idx, col] = value
            df.to_excel(f, index=False)
        except Exception as exc:
            log(self.paths, f"WARNING: could not patch V13 optimizer history: {exc}")

    def auto_v13(self, max_runs: int = 10, skip_gpt: bool = True) -> None:
        """Run isolated V13.4 staged symmetric coordinate search."""
        log(self.paths, "=" * 80)
        log(self.paths, "V13.4 STAGED SYMMETRIC CALIBRATION STARTED")
        log(self.paths, "=" * 80)

        state = self.optimizer_v13._read_state()
        if not state or self.optimizer_v13.current_best_score() is None:
            baseline = self._v13_single_cycle()
            baseline_run = Path(baseline.get("run_folder", ""))
            baseline_score = self._safe_float(baseline.get("TOTAL_SCORE"))
            status = str(baseline.get("run_status", "")).lower()
            if baseline_score is None or status not in {"success", "success_with_retries", "partial_success"}:
                log(self.paths, "V13.4 stopped: baseline did not produce a valid TOTAL_SCORE.")
                return
            if not v13_4_has_best(self.paths.results_dir):
                v13_4_save_best(
                    self.paths.config_file, self.paths.results_dir,
                    total_score=baseline_score, run_folder=str(baseline_run),
                    objective_reference_file=str(self.paths.results_dir / "objective_reference_V13.xlsx"),
                    optimizer_version="V13.4", stage="baseline",
                )
            self.optimizer_v13.initialize(baseline_score, str(baseline_run))
            self._v13_4_patch_history(
                baseline_run, baseline_score,
                {"optimizer_version": "V13.4", "action": "baseline_observed", "accepted": True, "group": "baseline"},
            )

        for _ in range(max_runs):
            if self.paths.manual_stop_file.exists():
                log(self.paths, f"Manual stop file detected: {self.paths.manual_stop_file}")
                break

            suggestion = self.optimizer_v13.next_suggestion()
            if suggestion.empty:
                log(self.paths, "V13.4 convergence reached or no eligible candidate remains.")
                break

            self._append_selected_suggestions(
                suggestion,
                {"optimizer_mode": "staged_symmetric_coordinate_search"},
                "auto_v13_4",
                True,
            )
            self.engine.apply_suggestions(suggestion)
            run_result = self._v13_single_cycle()
            run_dir = Path(run_result.get("run_folder", ""))
            score = self._safe_float(run_result.get("TOTAL_SCORE"))
            run_status = str(run_result.get("run_status", "")).lower()
            valid = score is not None and run_status in {"success", "success_with_retries", "partial_success"}

            current = self.optimizer_v13._read_state()
            baseline_run = str(current.get("current_best_run_folder", ""))
            baseline_score = self._safe_float(current.get("current_best_score"))
            row = suggestion.iloc[0]

            # Constraints are evaluated inside diagnostic writing. For a failed run
            # there is no ranking row, so use safe default diagnostics.
            preliminary = {
                "scientific_ok": True,
                "scientific_penalty": 0.0,
                "constraint_failures": "",
                "tradeoff_class": "candidate_run_invalid" if not valid else "",
            }
            if valid:
                from modules.v13_objective_diagnostics import evaluate_scientific_constraints
                ranking = pd.read_excel(self.paths.ranking_file)
                candidate_rows = ranking[
                    ranking["run_folder"].astype(str).str.contains(run_dir.name, regex=False, na=False)
                ] if "run_folder" in ranking.columns else pd.DataFrame()
                preliminary = evaluate_scientific_constraints(
                    candidate_rows.iloc[0] if not candidate_rows.empty else None,
                    self.config,
                )

            event = self.optimizer_v13.observe_run(
                score, str(run_dir),
                scientific_ok=bool(preliminary.get("scientific_ok", True)),
                scientific_penalty=float(preliminary.get("scientific_penalty", 0.0) or 0.0),
                valid=valid,
                diagnostics=preliminary,
            )
            self._v13_4_patch_history(run_dir, score, event)

            # Write the diagnostic rows only after the decision is final.
            if valid:
                diag = write_candidate_diagnostics(
                    results_dir=self.paths.results_dir,
                    ranking_file=self.paths.ranking_file,
                    baseline_run_folder=baseline_run,
                    candidate_run_folder=str(run_dir),
                    baseline_total_score=baseline_score,
                    candidate_total_score=score,
                    parameter=str(row.get("parameter", "")),
                    group=str(row.get("group", "")),
                    direction=str(row.get("direction", "")),
                    step_fraction=self._safe_float(row.get("step_fraction_applied")),
                    accepted=bool(event.get("accepted")),
                    decision=str(event.get("decision", "")),
                    rejection_reason=str(event.get("rejection_reason", "")),
                    config=self.config,
                )
                event.update(diag)

            if event.get("accepted"):
                v13_4_save_best(
                    self.paths.config_file, self.paths.results_dir,
                    total_score=score, run_folder=str(run_dir),
                    objective_reference_file=str(self.paths.results_dir / "objective_reference_V13.xlsx"),
                    optimizer_version="V13.4", stage=str(event.get("group", "")),
                    diagnostics=event,
                )
                log(self.paths, f"V13.4 accepted best: TOTAL_SCORE={score}; parameter={event.get('parameter')}")
            else:
                try:
                    v13_4_restore_best(self.paths.config_file, self.paths.results_dir)
                except Exception as exc:
                    log(self.paths, f"WARNING: V13.4 best restore failed: {exc}")
                log(
                    self.paths,
                    f"V13.4 {event.get('decision', 'rejected')} candidate: "
                    f"TOTAL_SCORE={score}; parameter={event.get('parameter')}; "
                    f"reason={event.get('rejection_reason', '')}"
                )

        rank_runs(self.paths, self.config)
        write_calibration_report(self.paths)
        try:
            from modules.v13_optimizer_report import write_v13_optimizer_campaign_report
            write_v13_optimizer_campaign_report(self.paths)
        except Exception as exc:
            log(self.paths, f"WARNING: V13.4 optimizer report failed: {exc}")
        if not skip_gpt:
            self.supervisor.review()
        log(self.paths, "V13.4 staged symmetric calibration finished")

    def auto(
        self,
        max_runs: int = 10,
        max_changes: int = 1,
        skip_gpt: bool = True,
    ) -> None:
        log(self.paths, "=" * 80)
        log(self.paths, "V11 AUTOMATIC DETERMINISTIC CALIBRATION STARTED")
        log(self.paths, "=" * 80)

        for i in range(max_runs):
            if self.paths.manual_stop_file.exists():
                log(
                    self.paths,
                    f"Manual stop file detected: {self.paths.manual_stop_file}",
                )
                break

            log(self.paths, f"V11 auto iteration {i + 1}/{max_runs}")

            diagnostic = self.single_cycle(skip_gpt=True, allow_restore=True)

            if diagnostic.get("run_status") not in [
                "success",
                "success_with_retries",
                "partial_success",
            ]:
                log(
                    self.paths,
                    "Latest run failed; stopping auto mode for manual review.",
                )
                break

            if diagnostic.get("quality_scientific_rejection") is True:
                log(
                    self.paths,
                    "Latest run was scientifically rejected by V10.9 QC; "
                    "stopping auto mode for manual review.",
                )
                break

            if diagnostic.get("qc_should_continue_auto") is False:
                log(
                    self.paths,
                    "V10.9 acceptance memory requested stopping auto mode "
                    "for manual review.",
                )
                break

            if diagnostic.get("qc_no_progress_should_stop_auto") is True:
                current_run_dir = (
                    Path(diagnostic.get("run_folder", ""))
                    if diagnostic.get("run_folder")
                    else Path("")
                )
                manual_review = self._write_manual_review_report(
                    context="auto_after_run_stop",
                    run_dir=current_run_dir,
                    stop_info={
                        "no_progress_stop_trigger": diagnostic.get("qc_no_progress_stop_trigger"),
                        "no_progress_reason": diagnostic.get("qc_no_progress_reason"),
                        "no_progress_should_stop_auto": diagnostic.get("qc_no_progress_should_stop_auto"),
                        "consecutive_no_best_count": diagnostic.get("qc_no_progress_consecutive_no_best_count"),
                        "latest_blocked_ratio": diagnostic.get("qc_no_progress_latest_blocked_ratio"),
                        "active_parameter_count": diagnostic.get("qc_no_progress_active_parameter_count"),
                        "exhausted_active_count": diagnostic.get("qc_no_progress_exhausted_active_count"),
                    },
                    diagnostic=diagnostic,
                )
                self._patch_latest_history_manual_review_report(current_run_dir, manual_review)
                log(
                    self.paths,
                    "V10.9 manual-review report created: "
                    f"{manual_review.get('manual_review_report_file')}",
                )
                log(
                    self.paths,
                    "V10.9 no-progress stop control requested stopping auto mode: "
                    f"{diagnostic.get('qc_no_progress_reason')}",
                )
                break

            # --------------------------------------------------------------
            # Do not leave an unevaluated parameter change after the final
            # requested auto iteration.
            #
            # The V10 loop evaluates the current parameter state first, then
            # applies suggestions for the NEXT run. Therefore, if this is the
            # last requested iteration, applying a suggestion here would modify
            # agent_config.xlsx without evaluating that modified state.
            # --------------------------------------------------------------
            if i == max_runs - 1:
                log(
                    self.paths,
                    "Final requested auto iteration completed; no new "
                    "suggestion applied after the last run.",
                )
                break

            candidate_pool_size = self._suggestion_candidate_pool_size(max_changes)
            candidate_suggestions = self.engine.suggest(max_changes=candidate_pool_size)
            candidate_suggestions = self._apply_v12_multiplier_scale_to_suggestions(
                candidate_suggestions,
                context="auto_after_run",
            )

            # Always pass the full candidate pool through V10.9/V11 memory filters.
            # First remove temporarily exhausted parameters, then block repeated
            # bad parameter directions, then select the next valid fallback.
            exhaustion_filtered, exhaustion_info = filter_suggestions_against_parameter_exhaustion(
                candidate_suggestions,
                self.paths.results_dir,
            )
            filtered_suggestions, filter_info = filter_suggestions_against_bad_memory(
                exhaustion_filtered,
                self.paths.results_dir,
            )
            filter_info.update(exhaustion_info)
            suggestions, filter_info = select_suggestions_with_fallback(
                filtered_suggestions,
                filter_info,
                max_changes=max_changes,
                results_dir=self.paths.results_dir,
            )
            current_run_dir = (
                Path(diagnostic.get("run_folder", ""))
                if diagnostic.get("run_folder")
                else Path("")
            )
            self._patch_latest_history_parameter_exhaustion(
                current_run_dir,
                filter_info,
            )
            self._patch_latest_history_bad_suggestions(
                current_run_dir,
                filter_info,
            )

            no_progress_filter = evaluate_no_progress_stop(
                results_dir=self.paths.results_dir,
                history_file=self.paths.history_file,
                config_file=self.paths.config_file,
                filter_info=filter_info,
                context="after_suggestion_filter",
            )
            self._patch_latest_history_no_progress(current_run_dir, no_progress_filter)

            try:
                v11_filter_strategy_update = update_v11_strategy(
                    paths=self.paths,
                    config=self.config,
                    context="after_suggestion_filter",
                    max_next_cycle=max_changes,
                    filter_info=filter_info,
                    no_progress=no_progress_filter,
                )
            except Exception as exc:
                v11_filter_strategy_update = {
                    "strategy_update_action": "failed",
                    "strategy_update_reason": f"V11 strategy updater failed after suggestion filter: {exc}",
                }
                log(self.paths, f"WARNING: V11 strategy updater failed after suggestion filter: {exc}")
            self._patch_latest_history_v11_strategy(current_run_dir, v11_filter_strategy_update)

            log(
                self.paths,
                "V10.9 bad-suggestion filter/fallback: "
                f"{filter_info.get('bad_suggestion_filter_action')} | "
                f"fallback={filter_info.get('bad_suggestion_fallback_action')} | "
                f"blocked={filter_info.get('bad_suggestion_blocked_count')} | "
                f"kept={filter_info.get('bad_suggestion_kept_count')} | "
                f"selected={filter_info.get('bad_suggestion_selected_count')}",
            )
            log(
                self.paths,
                "V10.9 parameter-exhaustion filter: "
                f"{filter_info.get('parameter_exhaustion_filter_action')} | "
                f"blocked={filter_info.get('parameter_exhaustion_blocked_count')} | "
                f"kept={filter_info.get('parameter_exhaustion_kept_count')}",
            )

            log(
                self.paths,
                "V10.9 no-progress filter stop control: "
                f"{no_progress_filter.get('no_progress_stop_action')} | "
                f"trigger={no_progress_filter.get('no_progress_stop_trigger')} | "
                f"blocked_ratio={no_progress_filter.get('latest_blocked_ratio')} | "
                f"reason={no_progress_filter.get('no_progress_reason')}",
            )

            if no_progress_filter.get("no_progress_should_stop_auto") is True:
                manual_review = self._write_manual_review_report(
                    context="after_suggestion_filter_stop",
                    run_dir=current_run_dir,
                    stop_info=no_progress_filter,
                    filter_info=filter_info,
                    diagnostic=diagnostic,
                )
                self._patch_latest_history_manual_review_report(current_run_dir, manual_review)
                log(
                    self.paths,
                    "V10.9 manual-review report created: "
                    f"{manual_review.get('manual_review_report_file')}",
                )
                log(
                    self.paths,
                    "V10.9 no-progress stop control stopped auto mode before applying "
                    f"a new suggestion: {no_progress_filter.get('no_progress_reason')}",
                )
                break

            if suggestions.empty:
                if filter_info.get("bad_suggestion_fallback_action") == "all_candidates_blocked":
                    log(
                        self.paths,
                        "All deterministic candidate suggestions were blocked by V10.9 "
                        "bad-suggestion memory; stopping auto mode for manual review.",
                    )
                else:
                    log(
                        self.paths,
                        "No deterministic candidate suggestion was selected after V10.9 "
                        "filter/fallback; stopping auto mode.",
                    )
                break

            self._append_selected_suggestions(
                suggestions,
                filter_info,
                context="auto_after_run",
                applied_to_agent_config=True,
            )
            self.engine.apply_suggestions(suggestions)

        rank_runs(self.paths, self.config)
        write_calibration_report(self.paths)

        if not skip_gpt:
            self.supervisor.review()

        log(self.paths, "V11 automatic calibration finished")

    def sensitivity(
        self,
        multipliers: list[float],
        max_parameters: int | None,
        skip_gpt: bool = True,
    ) -> None:
        analyzer = SensitivityAnalyzer(
            self.paths,
            self.config,
            self.single_cycle,
        )

        analyzer.run(
            multipliers=multipliers,
            max_parameters=max_parameters,
            include_baseline=True,
        )

        write_calibration_report(self.paths)

        if not skip_gpt:
            self.supervisor.review()

    def calibration_strategy(
        self,
        max_active_parameters: int = 5,
        max_changes_per_cycle: int = 1,
    ) -> tuple[Path, Path]:
        """
        Create deterministic V11 calibration strategy from the V10.9 baseline.

        Outputs:
        - 05_reports/strategy_update_V11.md
        - 04_results/calibration_strategy_V11.xlsx

        Also writes the inherited V10.9 baseline files for traceability:
        - 05_reports/calibration_strategy_V10.md
        - 04_results/calibration_strategy_V10.xlsx
        """
        report_path, excel_path = write_calibration_strategy(
            project_dir=self.paths.project_dir,
            max_active_parameters=max_active_parameters,
            max_changes_per_cycle=max_changes_per_cycle,
        )

        log(self.paths, f"Base V10.9 calibration strategy report: {report_path}")
        log(self.paths, f"Base V10.9 calibration strategy Excel: {excel_path}")

        # V11 overlay: keep V10.9 strategy evidence, but write calibration_strategy_V11.xlsx
        # with V10.9/V11 memory, GPT advisory flags, and deterministic next-cycle selection.
        v11_excel = self.paths.results_dir / "calibration_strategy_V11.xlsx"
        v11_report = self.paths.reports_dir / "strategy_update_V11.md"

        try:
            update_v11_strategy(
                paths=self.paths,
                config=self.config,
                context="calibration_strategy_mode",
                max_next_cycle=max_changes_per_cycle,
            )
            if v11_excel.exists():
                log(self.paths, f"V11 calibration strategy Excel: {v11_excel}")
            if v11_report.exists():
                log(self.paths, f"V11 strategy update report: {v11_report}")
        except Exception as exc:
            log(self.paths, f"WARNING: V11 strategy overlay failed: {exc}")

        if v11_excel.exists():
            return v11_report, v11_excel

        # Fallback only if the V11 overlay failed.
        return report_path, excel_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "MIN3P AI Pipeline V13.4 - staged adaptive calibration engine "
            "+ process sensitivity knowledge + parameter importance "
            "+ calibration strategy + GPT supervisor"
        )
    )

    parser.add_argument(
        "--mode",
        choices=[
            "single",
            "auto",
            "auto-v13",
            "suggest-only",
            "apply-suggestions",
            "sensitivity-run",
            "report",
            "gpt-supervisor",
            "story-report",
            "process-report",
            "sensitivity-interpretation",
            "parameter-importance",
            "calibration-strategy",
            "manual-review-report",
            "restore-best",
            "rebuild-best-memory",
        ],
        default="single",
    )

    parser.add_argument("--max-runs", type=int, default=10)
    parser.add_argument(
        "--optimizer-mode",
        choices=["adaptive_coordinate_search", "legacy_v12"],
        default="adaptive_coordinate_search",
        help="V13 default uses the adaptive coordinate-search controller; legacy_v12 preserves V12 suggestion selection.",
    )
    parser.add_argument("--max-changes", type=int, default=1)
    parser.add_argument("--max-sensitivity-parameters", type=int, default=0)
    parser.add_argument("--sensitivity-multipliers", type=str, default="0.8,1.2")

    parser.add_argument(
        "--max-active-parameters",
        type=int,
        default=5,
        help="Number of top parameters marked active in calibration_strategy_V11.xlsx",
    )

    parser.add_argument(
        "--max-strategy-changes",
        type=int,
        default=1,
        help="Number of parameters marked as next-cycle changes in calibration_strategy_V11.xlsx",
    )

    parser.add_argument(
        "--skip-gpt",
        action="store_true",
        help="Skip GPT supervisor even if OPENAI_API_KEY is set",
    )

    parser.add_argument(
        "--reset-v11-campaign",
        "--reset-v10-9-campaign",
        dest="reset_v11_campaign",
        action="store_true",
        help=(
            "Reset the V11 campaign-start marker before running. "
            "Use this when starting a new calibration campaign while keeping old history."
        ),
    )

    args = parser.parse_args()

    wf = V13Workflow(reset_campaign_start=args.reset_v11_campaign)
    log(wf.paths, f"V11 campaign start: {wf.v11_campaign}")

    if args.mode == "single":
        wf.single_cycle(skip_gpt=args.skip_gpt)

    elif args.mode == "auto-v13":
        wf.auto_v13(max_runs=args.max_runs, skip_gpt=args.skip_gpt)

    elif args.mode == "auto":
        if args.optimizer_mode == "adaptive_coordinate_search":
            wf.auto_v13(max_runs=args.max_runs, skip_gpt=args.skip_gpt)
        else:
            wf.auto(
                max_runs=args.max_runs,
                max_changes=args.max_changes,
                skip_gpt=args.skip_gpt,
            )

    elif args.mode == "suggest-only":
        wf.suggest_only(
            max_changes=args.max_changes,
            skip_gpt=args.skip_gpt,
        )

    elif args.mode == "apply-suggestions":
        wf.apply_suggestions(
            max_changes=args.max_changes,
            skip_gpt=args.skip_gpt,
        )

    elif args.mode == "restore-best":
        wf.restore_best_config(rebuild_from_history=True, context="restore_best_mode")
        write_calibration_report(wf.paths)

    elif args.mode == "rebuild-best-memory":
        wf.restore_best_config(rebuild_from_history=True, context="rebuild_best_memory_mode")
        write_calibration_report(wf.paths)

    elif args.mode == "sensitivity-run":
        multipliers = [
            float(x.strip())
            for x in args.sensitivity_multipliers.split(",")
            if x.strip()
        ]

        max_params = args.max_sensitivity_parameters or None

        wf.sensitivity(
            multipliers=multipliers,
            max_parameters=max_params,
            skip_gpt=args.skip_gpt,
        )

    elif args.mode == "report":
        write_calibration_report(wf.paths)

    elif args.mode == "gpt-supervisor":
        wf.supervisor.review()

    elif args.mode == "story-report":
        report = write_calibration_story_report(
            project_dir=wf.paths.project_dir,
            history_file=wf.paths.history_file,
            ranking_file=wf.paths.ranking_file,
            parameters_file=wf.paths.config_file,
            sensitivity_coefficients_file=wf.paths.sensitivity_coefficients_file,
            reports_dir=wf.paths.reports_dir,
            results_dir=wf.paths.results_dir,
        )

        print(f"Calibration story report created: {report}")

    elif args.mode == "process-report":
        report = write_process_sensitivity_report(
            sensitivity_results_file=wf.paths.sensitivity_results_file,
            summary_file=wf.paths.process_sensitivity_summary_file,
            report_file=wf.paths.process_sensitivity_report_file,
            project_name=wf.paths.project_dir.name,
        )

        print(f"Process sensitivity report created: {report}")

    elif args.mode == "sensitivity-interpretation":
        report = write_sensitivity_interpretation_report(
            process_summary_file=wf.paths.process_sensitivity_summary_file,
            interpretation_file=wf.paths.sensitivity_interpretation_file,
            report_file=wf.paths.sensitivity_interpretation_report_file,
            project_name=wf.paths.project_dir.name,
        )

        print(f"Sensitivity interpretation report created: {report}")

    elif args.mode == "parameter-importance":
        importance = write_parameter_importance_report(
            process_summary_file=wf.paths.process_sensitivity_summary_file,
            interpretation_file=wf.paths.sensitivity_interpretation_file,
            sensitivity_coefficients_file=wf.paths.sensitivity_coefficients_file,
            parameters_file=wf.paths.config_file,
            importance_file=wf.paths.results_dir / "parameter_importance_V10.xlsx",
            report_file=wf.paths.reports_dir / "parameter_importance_V10.md",
            project_name=wf.paths.project_dir.name,
        )

        print(f"Parameter importance file created: {importance}")

    elif args.mode == "calibration-strategy":
        report_path, excel_path = wf.calibration_strategy(
            max_active_parameters=args.max_active_parameters,
            max_changes_per_cycle=args.max_strategy_changes,
        )

        print(f"V11 calibration strategy report created: {report_path}")
        print(f"V11 calibration strategy Excel created: {excel_path}")

    elif args.mode == "manual-review-report":
        report_info = wf.manual_review_report(context="manual_mode")
        print(f"Manual-review report created: {report_info.get('manual_review_report_file')}")

    log(wf.paths, f"History file: {wf.paths.history_file}")
    log(wf.paths, f"Ranking file: {wf.paths.ranking_file}")
    log(wf.paths, f"Suggestions: {wf.paths.suggestions_file}")
    log(wf.paths, f"Calibration report: {wf.paths.calibration_report_file}")
    log(wf.paths, f"GPT supervisor report: {wf.paths.gpt_supervisor_report_file}")


if __name__ == "__main__":
    main()
