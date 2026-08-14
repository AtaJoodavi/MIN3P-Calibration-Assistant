from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import numpy as np
import pandas as pd

from .config import ProjectPaths
from .config_reader import ConfigReader
from .evaluator import metrics_to_history_dict
from .io_utils import append_row_excel, log, safe_float, timestamp


def current_parameters(config: ConfigReader) -> Dict[str, Any]:
    params = config.parameters()
    out: Dict[str, Any] = {}
    for _, row in params.iterrows():
        p = str(row.get("parameter", "")).strip()
        if not p or p.lower() == "nan":
            continue
        out[p] = row.get("value")
        out[f"{p}_status"] = row.get("status", "")
        out[f"{p}_group"] = row.get("group", "")
    return out


def update_history(paths: ProjectPaths, config: ConfigReader, run_dir: Path, results_dir: Path | None, diagnostic: Dict[str, Any]) -> Path:
    log(paths, "V9 STEP 4 - Update optimization history")
    row: Dict[str, Any] = {"timestamp": timestamp(), "run_folder": str(run_dir), "results_folder": str(results_dir) if results_dir else ""}
    history_columns = [
        "run_status",
        "error_type",
        "error_message",
        "run_health_score",
        "warnings",
        "failed_time_steps",
        "total_time_steps",
        "failed_step_fraction",
        "cpu_time_sec",
        "coupling_iterations_per_step",
        "initial_pH",
        "initial_charge_balance_error_percent",
        "gen_normal_exit",
        "log_normal_exit",
        # V10.9 run-quality classifier fields
        "qc_run_status",
        "qc_normal_exit",
        "qc_failed_step_fraction",
        "qc_runtime_warning",
        "qc_objective_score",
        "qc_previous_best_score",
        "qc_objective_improved",
        "qc_scientific_rejection",
        "qc_reason",
        "qc_log_file",
        # V10.9 accept/reject memory fields
        "qc_acceptance_status",
        "qc_is_new_best",
        "qc_should_update_best",
        "qc_should_continue_auto",
        "qc_score_change",
        "qc_percent_improvement",
        "qc_acceptance_reason",
        # V10.9 rollback / best-parameter memory fields
        "qc_parameter_memory_action",
        "qc_parameter_memory_reason",
        "qc_best_parameters_file",
        "qc_config_backup_file",
        "qc_best_source_run_folder",
        "qc_best_source_score",
        # V10.9 bad-suggestion memory fields
        "qc_bad_suggestion_action",
        "qc_bad_suggestion_reason",
        "qc_bad_suggestion_count",
        "qc_bad_suggestion_file",
        "qc_bad_suggestion_parameters",
        "qc_bad_suggestion_filter_action",
        "qc_bad_suggestion_candidate_count",
        "qc_bad_suggestion_blocked_count",
        "qc_bad_suggestion_kept_count",
        "qc_bad_suggestion_blocked_parameters",
        "qc_bad_suggestion_allowed_parameters",
        "qc_bad_suggestion_filter_reason",
        "qc_bad_suggestion_filter_log_file",
        "qc_bad_suggestion_fallback_action",
        "qc_bad_suggestion_fallback_used",
        "qc_bad_suggestion_fallback_reason",
        "qc_bad_suggestion_selected_count",
        "qc_bad_suggestion_selected_parameters",
        "qc_bad_suggestion_requested_changes",
        "qc_bad_suggestion_first_candidate_blocked",
        "qc_bad_suggestion_opposite_retry_action",
        "qc_bad_suggestion_opposite_retry_used",
        "qc_bad_suggestion_opposite_retry_count",
        "qc_bad_suggestion_opposite_retry_parameters",
        "qc_bad_suggestion_opposite_retry_directions",
        "qc_bad_suggestion_opposite_retry_selected_count",
        "qc_bad_suggestion_opposite_retry_selected_parameters",
        "qc_bad_suggestion_opposite_retry_selected_directions",
        "qc_bad_suggestion_opposite_retry_reason",
        # V10.9 parameter-exhaustion memory fields
        "qc_parameter_exhaustion_action",
        "qc_parameter_exhaustion_reason",
        "qc_parameter_exhaustion_count",
        "qc_parameter_exhaustion_parameters",
        "qc_parameter_exhaustion_file",
        "qc_parameter_exhaustion_filter_action",
        "qc_parameter_exhaustion_candidate_count",
        "qc_parameter_exhaustion_blocked_count",
        "qc_parameter_exhaustion_kept_count",
        "qc_parameter_exhaustion_blocked_parameters",
        "qc_parameter_exhaustion_allowed_parameters",
        "qc_parameter_exhaustion_filter_reason",
        "qc_parameter_exhaustion_filter_log_file",
        # V10.9 no-progress stop-control fields
        "qc_no_progress_stop_action",
        "qc_no_progress_stop_trigger",
        "qc_no_progress_should_stop_auto",
        "qc_no_progress_reason",
        "qc_no_progress_consecutive_no_best_count",
        "qc_no_progress_max_consecutive_no_best",
        "qc_no_progress_latest_candidate_count",
        "qc_no_progress_latest_blocked_count",
        "qc_no_progress_latest_kept_count",
        "qc_no_progress_latest_blocked_ratio",
        "qc_no_progress_latest_blocked_parameters",
        "qc_no_progress_blocked_ratio_limit",
        "qc_no_progress_active_parameter_count",
        "qc_no_progress_exhausted_active_count",
        "qc_no_progress_direction_limited_active_count",
        "qc_no_progress_exhausted_active_parameters",
        "qc_no_progress_direction_limited_active_parameters",
        "qc_no_progress_active_exhausted_ratio",
        "qc_no_progress_campaign_start_action",
        "qc_no_progress_campaign_start_file",
        "qc_no_progress_campaign_start_timestamp",
        "qc_no_progress_campaign_filter_applied",
        "qc_no_progress_campaign_rows_evaluated",
        "qc_no_progress_stop_file",
        # V10.9 manual-review report fields
        "qc_manual_review_report_action",
        "qc_manual_review_report_context",
        "qc_manual_review_report_file",
        "qc_manual_review_report_log_file",
        "qc_manual_review_report_reason",
        "qc_manual_review_report_stop_trigger",
        "qc_manual_review_report_timestamp",
        "qc_manual_review_exhausted_parameters",
        "qc_manual_review_direction_limited_parameters",
        "qc_manual_review_latest_blocked_parameters",
        # V13 adaptive coordinate-search audit fields
        "optimizer_version",
        "optimizer_mode",
        "campaign_id",
        "pass_number",
        "trajectory_id",
        "optimizer_parameter",
        "optimizer_direction",
        "optimizer_step_fraction",
        "optimizer_baseline_value",
        "optimizer_candidate_value",
        "optimizer_baseline_objective",
        "optimizer_candidate_objective",
        "optimizer_event_action",
        "optimizer_trajectory_state",
        "optimizer_convergence_status",
        "optimizer_convergence_reason",
    ]

    for c in history_columns:
        row[c] = diagnostic.get(c)
    row.update(current_parameters(config))
    row.update(metrics_to_history_dict(results_dir))
    append_row_excel(paths.history_file, row)
    append_row_excel(paths.run_diagnostics_file, {"timestamp": timestamp(), **diagnostic})
    if diagnostic.get("run_status") == "failed":
        append_row_excel(paths.failed_runs_file, {"timestamp": timestamp(), **diagnostic})
    return paths.history_file


def latest_successful_row(paths: ProjectPaths) -> pd.Series | None:
    if not paths.history_file.exists():
        return None
    df = pd.read_excel(paths.history_file)
    if df.empty:
        return None
    if "run_status" in df.columns:
        good = df[df["run_status"].astype(str).str.lower().isin(["success", "success_with_retries", "partial_success"])]
        if not good.empty:
            return good.iloc[-1]
    return df.iloc[-1]


def _active_species_scoring_config(config: ConfigReader) -> list[dict[str, Any]]:
    """
    Read active species and weights from agent_config.xlsx / species.

    Required columns:
        species | active | weight

    A species named ``so4-2`` maps to history metric ``RMSE_so4-2``.
    """
    try:
        species = config.species().copy()
    except Exception as exc:
        raise RuntimeError(
            "Could not read the required 'species' sheet from agent_config.xlsx. "
            "Expected columns: species, active, weight."
        ) from exc

    required = {"species", "active", "weight"}
    missing = required - set(species.columns)
    if missing:
        raise ValueError(
            "The 'species' sheet is missing required column(s): "
            + ", ".join(sorted(missing))
        )

    active_values = species["active"].astype(str).str.strip().str.lower()
    active = species.loc[active_values.isin({"yes", "y", "true", "1"})].copy()

    scoring: list[dict[str, Any]] = []
    for _, row in active.iterrows():
        species_name = str(row["species"]).strip()
        weight = safe_float(row["weight"])

        if not species_name or species_name.lower() == "nan":
            continue
        if weight is None:
            raise ValueError(
                f"Species '{species_name}' is active but has no valid numeric weight."
            )
        if float(weight) < 0:
            raise ValueError(
                f"Species '{species_name}' has a negative weight ({weight})."
            )
        if float(weight) == 0:
            continue

        scoring.append(
            {
                "species": species_name,
                "metric": f"RMSE_{species_name}",
                "weight": float(weight),
            }
        )

    if not scoring:
        raise ValueError(
            "No active species with positive weights were found in the 'species' sheet."
        )
    return scoring


def _species_metric_weights(config: ConfigReader) -> dict[str, float]:
    """Backward-compatible active-metric/weight map."""
    return {
        item["metric"]: float(item["weight"])
        for item in _active_species_scoring_config(config)
    }


def _objective_reference_file(paths: ProjectPaths) -> Path:
    return paths.results_dir / "objective_reference_V13.xlsx"


def _valid_run_mask(df: pd.DataFrame) -> pd.Series:
    if "run_status" not in df.columns:
        return pd.Series(True, index=df.index)
    return df["run_status"].astype(str).str.strip().str.lower().isin(
        {"success", "success_with_retries", "partial_success"}
    )


def _select_reference_baseline(
    df: pd.DataFrame, scoring: list[dict[str, Any]]
) -> pd.Series:
    """
    Select the latest successful history row with valid RMSE values for every
    active target. In a fresh project this is the baseline run just completed.
    """
    valid = df.loc[_valid_run_mask(df)].copy()
    if valid.empty:
        raise ValueError(
            "Cannot create objective references: no numerically successful run "
            "exists in optimization_history.xlsx."
        )

    metrics = [item["metric"] for item in scoring]
    missing_columns = [metric for metric in metrics if metric not in valid.columns]
    if missing_columns:
        raise ValueError(
            "Cannot create objective references because history is missing RMSE "
            "column(s): " + ", ".join(missing_columns)
        )

    complete = pd.Series(True, index=valid.index)
    for metric in metrics:
        values = pd.to_numeric(valid[metric], errors="coerce")
        complete &= values.notna() & np.isfinite(values) & (values >= 0.0)

    candidates = valid.loc[complete]
    if candidates.empty:
        raise ValueError(
            "Cannot create objective references: no successful history row has "
            "valid RMSE values for every active species."
        )
    return candidates.iloc[-1]


def _create_objective_references(
    paths: ProjectPaths,
    df: pd.DataFrame,
    scoring: list[dict[str, Any]],
) -> pd.DataFrame:
    """
    Create one frozen reference workbook. Values are never automatically
    recalculated after this file exists.
    """
    baseline = _select_reference_baseline(df, scoring)
    floor = 1.0e-12
    created_at = timestamp()
    campaign_id = f"V13_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}"

    rows: list[dict[str, Any]] = []
    for item in scoring:
        metric = item["metric"]
        raw = pd.to_numeric(
            pd.Series([baseline.get(metric)]), errors="coerce"
        ).iloc[0]
        baseline_rmse = float(raw)
        reference_rmse = max(abs(baseline_rmse), floor)
        rows.append(
            {
                "campaign_id": campaign_id,
                "species": item["species"],
                "metric": metric,
                "weight": item["weight"],
                "baseline_rmse": baseline_rmse,
                "reference_rmse": reference_rmse,
                "reference_floor": floor,
                "baseline_run_folder": str(baseline.get("run_folder", "")),
                "baseline_timestamp": baseline.get("timestamp", ""),
                "created_at": created_at,
                "reference_status": "frozen_auto_created",
            }
        )

    refs = pd.DataFrame(rows)
    ref_file = _objective_reference_file(paths)
    ref_file.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(ref_file, engine="openpyxl") as writer:
        refs.to_excel(writer, sheet_name="objective_reference", index=False)

    log(
        paths,
        "Created frozen V13 objective references from baseline run: "
        f"{baseline.get('run_folder', '')}",
    )
    return refs


def _load_or_create_objective_references(
    paths: ProjectPaths,
    df: pd.DataFrame,
    scoring: list[dict[str, Any]],
) -> pd.DataFrame:
    """
    Use existing frozen scales. Create them only when absent.

    To deliberately start a new scientific campaign, archive or rename
    objective_reference_V13.xlsx and then run a baseline before auto mode.
    """
    ref_file = _objective_reference_file(paths)
    expected = [item["metric"] for item in scoring]

    if not ref_file.exists():
        return _create_objective_references(paths, df, scoring)

    try:
        refs = pd.read_excel(ref_file, sheet_name="objective_reference")
    except Exception as exc:
        raise RuntimeError(
            f"Could not read frozen objective reference file: {ref_file}"
        ) from exc

    required = {"metric", "reference_rmse", "weight"}
    missing = required - set(refs.columns)
    if missing:
        raise ValueError(
            "Objective reference file is missing required column(s): "
            + ", ".join(sorted(missing))
        )

    refs["metric"] = refs["metric"].astype(str).str.strip()
    missing_metrics = [metric for metric in expected if metric not in set(refs["metric"])]
    if missing_metrics:
        raise ValueError(
            "Frozen objective reference file does not contain active metric(s): "
            + ", ".join(missing_metrics)
        )

    refs = refs[refs["metric"].isin(expected)].copy()
    refs["reference_rmse"] = pd.to_numeric(
        refs["reference_rmse"], errors="coerce"
    )
    invalid = refs[
        refs["reference_rmse"].isna() | (refs["reference_rmse"] <= 0)
    ]
    if not invalid.empty:
        raise ValueError(
            "Frozen objective reference file has invalid reference_rmse for: "
            + ", ".join(invalid["metric"].astype(str).tolist())
        )
    return refs


def rank_runs(paths: ProjectPaths, config: ConfigReader) -> Path:
    """
    Universal stable objective driven by the species sheet.

    One active species:
        TOTAL_SCORE = RMSE_<species>

    Two or more active species:
        TOTAL_SCORE = sum(weight * RMSE / frozen_reference_rmse)

    The frozen reference scales are automatically created once in:
        04_results/objective_reference_V13.xlsx
    """
    if not paths.history_file.exists():
        return paths.ranking_file

    df = pd.read_excel(paths.history_file)
    if df.empty:
        return paths.ranking_file

    out = df.copy()
    scoring = _active_species_scoring_config(config)
    configured_metrics = [item["metric"] for item in scoring]

    failed_run_penalty_weight = 0.0
    run_health_weight = 0.0

    if len(scoring) == 1:
        metric = scoring[0]["metric"]
        if metric not in out.columns:
            raise ValueError(
                f"Single active species requires '{metric}' in "
                "optimization_history.xlsx, but that column was not found."
            )

        rmse = pd.to_numeric(out[metric], errors="coerce")
        out["TOTAL_SCORE"] = rmse.fillna(np.inf)
        out["TOTAL_SCORE_OBJECTIVE_MODE"] = "single_species_direct_rmse"
        out["TOTAL_SCORE_RMSE_COMPONENT"] = out["TOTAL_SCORE"]
        out["TOTAL_SCORE_INCLUDED_METRICS"] = metric
        out["TOTAL_SCORE_CONFIGURED_METRICS"] = metric
        out["TOTAL_SCORE_REFERENCE_FILE"] = ""
        out["TOTAL_SCORE_REFERENCE_SCALES"] = ""
        out[f"NORM_{metric}"] = np.nan
        out[f"WEIGHTED_{metric}"] = rmse
    else:
        refs = _load_or_create_objective_references(paths, out, scoring)
        ref_map = dict(zip(refs["metric"], refs["reference_rmse"]))
        weight_map = {item["metric"]: float(item["weight"]) for item in scoring}

        total = np.zeros(len(out), dtype=float)
        reference_scales: list[str] = []

        for metric in configured_metrics:
            weight = weight_map[metric]
            reference_rmse = float(ref_map[metric])
            reference_scales.append(f"{metric}={reference_rmse:g}")

            if metric not in out.columns:
                normalized = pd.Series(np.inf, index=out.index, dtype=float)
            else:
                values = pd.to_numeric(out[metric], errors="coerce")
                normalized = (values / reference_rmse).fillna(np.inf).clip(lower=0.0)

            out[f"NORM_{metric}"] = normalized
            out[f"WEIGHTED_{metric}"] = normalized * weight
            total += out[f"WEIGHTED_{metric}"].to_numpy()

        out["TOTAL_SCORE"] = total
        out["TOTAL_SCORE_OBJECTIVE_MODE"] = (
            "multi_species_frozen_reference_weighted_rmse"
        )
        out["TOTAL_SCORE_RMSE_COMPONENT"] = total
        out["TOTAL_SCORE_INCLUDED_METRICS"] = ", ".join(configured_metrics)
        out["TOTAL_SCORE_CONFIGURED_METRICS"] = ", ".join(configured_metrics)
        out["TOTAL_SCORE_REFERENCE_FILE"] = str(_objective_reference_file(paths))
        out["TOTAL_SCORE_REFERENCE_SCALES"] = ", ".join(reference_scales)

    ok = out.get("run_status", "success").astype(str).str.lower().isin(
        ["success", "success_with_retries", "partial_success"]
    )
    health = pd.to_numeric(
        out.get("run_health_score", 0), errors="coerce"
    ).fillna(0.0)

    out["TOTAL_SCORE_FAILED_RUN_COMPONENT"] = (
        np.where(ok, 0.0, 1.0) * failed_run_penalty_weight
    )
    out["TOTAL_SCORE_RUN_HEALTH_COMPONENT"] = health * run_health_weight
    out["TOTAL_SCORE_FAILED_RUN_PENALTY_WEIGHT"] = failed_run_penalty_weight
    out["TOTAL_SCORE_RUN_HEALTH_WEIGHT"] = run_health_weight

    out["TOTAL_SCORE"] = (
        out["TOTAL_SCORE"]
        + out["TOTAL_SCORE_FAILED_RUN_COMPONENT"]
        + out["TOTAL_SCORE_RUN_HEALTH_COMPONENT"]
    )

    out = out.sort_values("TOTAL_SCORE", ascending=True, na_position="last")
    out.to_excel(paths.ranking_file, index=False)
    return paths.ranking_file
