from __future__ import annotations

"""
V11/V12 rollback guard for MIN3P AI calibration.

V12.5 safety patch
------------------
Older logic skipped rollback whenever qc_acceptance_status == "accepted_best".
That is unsafe if the acceptance label was produced using a stale best_objective.

This version recomputes the true previous best from optimization_history.xlsx
before deciding whether the latest applied suggestion should be kept.
"""

from pathlib import Path
from datetime import datetime
from typing import Any

import pandas as pd


NOT_BEST_MARKERS = (
    "accepted_valid_not_best",
    "valid_not_best",
    "valid_not_new_best",
    "not_best",
    "rejected",
    "failed",
)

OBJECTIVE_COLUMNS = [
    "TOTAL_SCORE",
    "objective_total",
    "qc_objective_score",
    "objective_score",
]

ACCEPTED_BEST_MARKERS = {
    "accepted_best",
    "new_best",
    "accepted_new_best",
}

STATUS_COLUMNS = [
    "qc_acceptance_status",
    "acceptance_status",
    "status",
]


def _as_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _close(a: Any, b: Any, rel_tol: float = 1e-8, abs_tol: float = 1e-30) -> bool:
    """Robust numeric equality check for both normal and very small MIN3P parameters.

    V12.6 fix:
    The old rollback guard used max(1.0, abs(a), abs(b)), which created an
    absolute tolerance of ~1e-10. That is too large for kinetic parameters
    around 1e-11 and caused values like 1.48e-11 and 1.60e-11 to be treated
    as equal.

    This version uses a very small absolute floor plus relative tolerance.
    """
    af = _as_float(a)
    bf = _as_float(b)
    if af is None or bf is None:
        return False
    return abs(af - bf) <= max(abs_tol, rel_tol * max(abs(af), abs(bf)))


def _row_objective(row: pd.Series) -> float | None:
    for col in OBJECTIVE_COLUMNS:
        if col in row.index:
            value = _as_float(row.get(col))
            if value is not None:
                return value
    return None


def _row_status(row: pd.Series) -> str:
    for col in STATUS_COLUMNS:
        if col in row.index:
            value = str(row.get(col, "")).strip().lower()
            if value and value != "nan":
                return value
    return ""


def true_previous_best_before_latest(history: pd.DataFrame, project_dir: str | Path | None = None) -> float | None:
    """
    Compute the prior best objective on the active V13 scoring scale.

    Prefer run_ranking.xlsx TOTAL_SCORE and exclude the latest run by its
    run_folder. This avoids stale V10-V12 objective values from controlling
    rollback decisions.
    """
    if history.empty or len(history) < 2:
        return None

    latest = history.iloc[-1]
    latest_run = str(latest.get("run_folder", "")).strip()

    if project_dir is not None:
        ranking_file = Path(project_dir) / "04_results" / "run_ranking.xlsx"
        if ranking_file.exists():
            try:
                ranking = pd.read_excel(ranking_file)
                if not ranking.empty and "TOTAL_SCORE" in ranking.columns:
                    if latest_run and "run_folder" in ranking.columns:
                        latest_name = Path(latest_run).name
                        ranking = ranking[
                            ~ranking["run_folder"].astype(str).str.contains(
                                latest_name, case=False, na=False, regex=False
                            )
                        ].copy()
                    if "run_status" in ranking.columns:
                        valid = ranking["run_status"].astype(str).str.strip().str.lower().isin(
                            {"success", "success_with_retries", "partial_success"}
                        )
                        ranking = ranking[valid].copy()
                    values = pd.to_numeric(ranking["TOTAL_SCORE"], errors="coerce").dropna()
                    if not values.empty:
                        return float(values.min())
            except Exception:
                pass

    previous = history.iloc[:-1].copy()
    if "TOTAL_SCORE" in previous.columns:
        values = pd.to_numeric(previous["TOTAL_SCORE"], errors="coerce").dropna()
        if not values.empty:
            return float(values.min())

    all_values: list[float] = []
    for _, row in previous.iterrows():
        obj = _row_objective(row)
        if obj is not None:
            all_values.append(obj)

    return min(all_values) if all_values else None

def rollback_latest_nonbest_suggestion(project_dir: str | Path) -> dict[str, Any]:
    """Rollback the latest applied V11 suggestion if latest run is not a true new best.

    Uses parameter_suggestions_V11.xlsx as the undo log.

    V12.5 rule:
    - Recompute true previous best from history before trusting accepted_best.
    - If latest objective is not lower than the true previous best, rollback.
    """
    project_dir = Path(project_dir).resolve()
    input_xlsx = project_dir / "01_input" / "agent_config.xlsx"
    history_xlsx = project_dir / "04_results" / "optimization_history.xlsx"
    suggestions_xlsx = project_dir / "04_results" / "parameter_suggestions_V11.xlsx"
    log_xlsx = project_dir / "04_results" / "v11_rollback_log.xlsx"

    for path in [input_xlsx, history_xlsx, suggestions_xlsx]:
        if not path.exists():
            return {"action": "missing_file", "file": str(path)}

    history = pd.read_excel(history_xlsx)
    suggestions = pd.read_excel(suggestions_xlsx)
    sheets = pd.read_excel(input_xlsx, sheet_name=None)

    if history.empty:
        return {"action": "empty_history"}
    if "parameters" not in sheets:
        return {"action": "missing_parameters_sheet"}
    if "applied_to_agent_config" not in suggestions.columns:
        return {"action": "missing_applied_column"}

    latest = history.iloc[-1]
    acceptance = str(latest.get("qc_acceptance_status", "")).lower()
    stop_action = str(latest.get("qc_no_progress_stop_action", "")).lower()
    objective = _row_objective(latest)
    row_best_objective = _as_float(latest.get("best_objective"))
    true_previous_best = true_previous_best_before_latest(history, project_dir=project_dir)

    if objective is not None and true_previous_best is not None:
        is_new_best = objective < true_previous_best
    else:
        # Fallback for incomplete histories only.
        is_new_best = acceptance == "accepted_best" or (
            objective is not None
            and row_best_objective is not None
            and objective < row_best_objective
        )

    if is_new_best:
        return {
            "action": "no_rollback_new_best",
            "acceptance": acceptance,
            "objective_total": objective,
            "row_best_objective": row_best_objective,
            "true_previous_best_objective": true_previous_best,
        }

    should_rollback = (
        any(marker in acceptance for marker in NOT_BEST_MARKERS)
        or acceptance == "accepted_best"
        or stop_action == "stop_auto_for_manual_review"
    )

    if not should_rollback:
        return {
            "action": "no_rollback_status",
            "acceptance": acceptance,
            "stop_action": stop_action,
            "objective_total": objective,
            "row_best_objective": row_best_objective,
            "true_previous_best_objective": true_previous_best,
        }

    applied = suggestions[
        pd.to_numeric(suggestions["applied_to_agent_config"], errors="coerce")
        .fillna(0)
        .eq(1)
    ].copy()

    if applied.empty:
        return {"action": "no_applied_suggestions"}

    sug = applied.iloc[-1]
    parameter = str(sug.get("parameter"))
    old_value = _as_float(sug.get("old_value"))
    new_value = _as_float(sug.get("new_value"))
    if old_value is None or new_value is None:
        return {"action": "non_numeric_suggestion", "parameter": parameter}

    params = sheets["parameters"].copy()
    mask = params["parameter"].astype(str).eq(parameter)
    if not mask.any():
        return {"action": "parameter_not_found", "parameter": parameter}

    current_value = _as_float(params.loc[mask, "value"].iloc[0])
    if _close(current_value, old_value):
        action = "already_rolled_back"
        after = current_value
    elif _close(current_value, new_value):
        params.loc[mask, "value"] = old_value
        sheets["parameters"] = params
        with pd.ExcelWriter(input_xlsx, engine="openpyxl") as writer:
            for sheet_name, df in sheets.items():
                df.to_excel(writer, sheet_name=sheet_name, index=False)
        action = "rolled_back_latest_nonbest"
        after = old_value
    else:
        action = "manual_check_needed"
        after = current_value

    result = {
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "parameter": parameter,
        "old_value": old_value,
        "new_value": new_value,
        "current_value_before": current_value,
        "current_value_after": after,
        "acceptance": acceptance,
        "stop_action": stop_action,
        "objective_total": objective,
        "row_best_objective": row_best_objective,
        "true_previous_best_objective": true_previous_best,
    }

    row = pd.DataFrame([result])
    if log_xlsx.exists():
        log = pd.concat([pd.read_excel(log_xlsx), row], ignore_index=True)
    else:
        log = row
    log.to_excel(log_xlsx, index=False)
    return result
