from __future__ import annotations

"""
Run acceptance memory for MIN3P AI calibration.

V12.5 safety patch
------------------
The previous V10.9/V11 logic trusted quality["previous_best_score"]. In the
HCT2 V12 campaign, this value was sometimes stale, which allowed a run with a
worse objective to be marked as accepted_best.

This version recomputes the true previous best from optimization_history.xlsx
when run_dir is available:

    true previous best = minimum objective among previous accepted_best rows

Lower objective score is assumed better.
"""

from pathlib import Path
from typing import Any, Dict

import pandas as pd


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


def _safe_float(value: Any) -> float | None:
    """Return float(value), or None for empty/invalid values."""
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _row_objective(row: pd.Series) -> float | None:
    """Extract the objective from a history row using known objective columns."""
    for col in OBJECTIVE_COLUMNS:
        if col in row.index:
            value = _safe_float(row.get(col))
            if value is not None:
                return value
    return None


def _row_status(row: pd.Series) -> str:
    """Extract the acceptance/status label from a history row."""
    for col in STATUS_COLUMNS:
        if col in row.index:
            value = str(row.get(col, "")).strip().lower()
            if value and value != "nan":
                return value
    return ""


def _normalize_path_text(value: Any) -> str:
    """Normalize path-like strings for robust comparison."""
    try:
        s = str(value).strip()
        if not s or s.lower() == "nan":
            return ""
        return str(Path(s).resolve()).lower()
    except Exception:
        return str(value).strip().lower()


def _infer_project_dir_from_run_dir(run_dir: str | Path | None) -> Path | None:
    """Infer <project_dir> from <project_dir>/03_runs/run_xxx."""
    if run_dir is None:
        return None

    try:
        rd = Path(run_dir).resolve()
        if rd.parent.name == "03_runs":
            return rd.parent.parent

        for parent in rd.parents:
            if parent.name == "03_runs":
                return parent.parent
    except Exception:
        return None

    return None


def true_previous_best_score_from_history(run_dir: str | Path | None) -> float | None:
    """
    Compute the prior best objective on the current scoring scale.

    V13 rule:
    1. Prefer run_ranking.xlsx TOTAL_SCORE, excluding the current run.
       This is the authoritative frozen-reference objective.
    2. Fall back to optimization_history.xlsx only for legacy projects that
       do not yet have a usable ranking table.

    Historical accepted_best labels are not used to define the prior best,
    because they may have been created under an older objective scale.
    """
    project_dir = _infer_project_dir_from_run_dir(run_dir)
    if project_dir is None:
        return None

    current_run = _normalize_path_text(run_dir) if run_dir is not None else ""

    ranking_file = project_dir / "04_results" / "run_ranking.xlsx"
    if ranking_file.exists():
        try:
            ranking = pd.read_excel(ranking_file)
            if not ranking.empty and "TOTAL_SCORE" in ranking.columns:
                if current_run and "run_folder" in ranking.columns:
                    ranking = ranking[
                        ~ranking["run_folder"].map(_normalize_path_text).eq(current_run)
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

    history_file = project_dir / "04_results" / "optimization_history.xlsx"
    if not history_file.exists():
        return None

    try:
        hist = pd.read_excel(history_file)
    except Exception:
        return None

    if hist.empty:
        return None

    if current_run and "run_folder" in hist.columns:
        hist = hist[~hist["run_folder"].map(_normalize_path_text).eq(current_run)].copy()

    if "TOTAL_SCORE" in hist.columns:
        values = pd.to_numeric(hist["TOTAL_SCORE"], errors="coerce").dropna()
        if not values.empty:
            return float(values.min())

    all_values: list[float] = []
    for _, row in hist.iterrows():
        obj = _row_objective(row)
        if obj is not None:
            all_values.append(obj)

    return min(all_values) if all_values else None

def classify_acceptance(
    quality: Dict[str, Any],
    run_dir: str | Path | None = None,
) -> Dict[str, Any]:
    """
    V10.9/V12 accept/reject memory classifier.

    This layer is intentionally separate from run_quality_classifier.py:
    - run_quality_classifier answers: is the run numerically/scientifically valid?
    - run_acceptance_memory answers: should this run be treated as a new best?

    Lower objective score is assumed better.

    V12.5 rule:
    - Prefer the true previous best from optimization_history.xlsx.
    - Only fall back to quality["previous_best_score"] when history is unavailable.
    """
    q_status = str(quality.get("run_status", "")).strip().lower()
    scientific_rejection = bool(quality.get("scientific_rejection", False))
    normal_exit = quality.get("normal_exit")

    objective_score = _safe_float(quality.get("objective_score"))
    reported_previous_best_score = _safe_float(quality.get("previous_best_score"))
    true_previous_best_score = true_previous_best_score_from_history(run_dir)

    previous_best_score = (
        true_previous_best_score
        if true_previous_best_score is not None
        else reported_previous_best_score
    )

    score_change = None
    percent_improvement = None
    objective_improved = None

    if objective_score is not None and previous_best_score is not None:
        score_change = objective_score - previous_best_score
        if previous_best_score != 0:
            percent_improvement = (
                (previous_best_score - objective_score)
                / abs(previous_best_score)
                * 100.0
            )
        objective_improved = objective_score < previous_best_score

    reasons: list[str] = []

    if true_previous_best_score is not None:
        reasons.append(
            f"V13 prior best on active ranking scale = {true_previous_best_score}"
        )
    elif reported_previous_best_score is not None:
        reasons.append(
            f"using reported previous best = {reported_previous_best_score}"
        )

    if q_status == "failed" or normal_exit is False:
        acceptance_status = "failed"
        is_new_best = False
        should_update_best = False
        should_continue_auto = False
        reasons.append("run failed or MIN3P did not report normal exit")

    elif scientific_rejection:
        acceptance_status = "rejected_scientific"
        is_new_best = False
        should_update_best = False
        should_continue_auto = False
        reasons.append("run rejected by scientific QC")

    elif objective_improved is True:
        acceptance_status = "accepted_best"
        is_new_best = True
        should_update_best = True
        should_continue_auto = True
        reasons.append("objective score improved relative to ranked prior best")

    elif objective_improved is False:
        acceptance_status = "accepted_valid_not_best"
        is_new_best = False
        should_update_best = False
        should_continue_auto = True
        reasons.append(
            "run is valid, but objective score did not improve against ranked prior best"
        )

    else:
        acceptance_status = "accepted_valid_unscored"
        is_new_best = False
        should_update_best = False
        should_continue_auto = True
        reasons.append("run is valid, but objective score or previous best is not available")

    qc_reason = quality.get("reason")
    if qc_reason:
        reasons.append(str(qc_reason))

    return {
        "run_folder": str(run_dir) if run_dir is not None else "",
        "acceptance_status": acceptance_status,
        "is_new_best": is_new_best,
        "should_update_best": should_update_best,
        "should_continue_auto": should_continue_auto,
        "objective_score": objective_score,
        "previous_best_score": previous_best_score,
        "reported_previous_best_score": reported_previous_best_score,
        "true_previous_best_score": true_previous_best_score,
        "score_change": score_change,
        "percent_improvement": percent_improvement,
        "objective_improved": objective_improved,
        "scientific_rejection": scientific_rejection,
        "run_quality_status": quality.get("run_status"),
        "runtime_warning": quality.get("runtime_warning"),
        "acceptance_reason": "; ".join(reasons),
    }


def append_acceptance_memory(
    results_dir: str | Path,
    acceptance: Dict[str, Any],
    filename: str = "acceptance_memory_V10_9.xlsx",
) -> Path:
    """Append one acceptance decision to 04_results/acceptance_memory_V10_9.xlsx."""
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    output_file = results_dir / filename

    row = pd.DataFrame([acceptance])
    if output_file.exists():
        try:
            old = pd.read_excel(output_file)
            row = pd.concat([old, row], ignore_index=True)
        except Exception:
            pass

    row.to_excel(output_file, index=False)
    return output_file
