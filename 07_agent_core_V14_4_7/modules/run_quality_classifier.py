from pathlib import Path
import re
import numpy as np
import pandas as pd


def read_log_text(log_file: Path) -> str:
    if not log_file or not Path(log_file).exists():
        return ""
    return Path(log_file).read_text(errors="ignore")


def has_normal_exit(log_text: str) -> bool:
    return "normal exit" in log_text.lower()


def extract_failed_step_fraction(log_text: str):
    """
    Tries to estimate failed timestep fraction from MIN3P log.
    Returns None if not detected.
    """

    text = log_text.lower()

    failed_match = re.search(r"failed\s+steps?\s*[:=]?\s*(\d+)", text)
    total_match = re.search(r"total\s+steps?\s*[:=]?\s*(\d+)", text)

    if failed_match and total_match:
        failed = int(failed_match.group(1))
        total = int(total_match.group(1))
        if total > 0:
            return failed / total

    return None


def classify_runtime_warning(failed_step_fraction):
    """
    Failed timestep fraction is treated as a soft warning only.
    """

    if failed_step_fraction is None:
        return "unknown"

    if failed_step_fraction > 0.40:
        return "high"
    elif failed_step_fraction > 0.25:
        return "moderate"
    elif failed_step_fraction > 0.15:
        return "low"
    else:
        return "none"


def detect_nonphysical_chemistry(results_df: pd.DataFrame):
    """
    Basic scientific rejection checks.
    Expects a dataframe containing model output concentrations or pH.

    Flexible column names:
    - pH, ph
    - concentration columns for species
    """

    reasons = []

    if results_df is None or results_df.empty:
        return False, reasons

    lower_cols = {c.lower(): c for c in results_df.columns}

    # pH check
    if "ph" in lower_cols:
        ph_col = lower_cols["ph"]
        ph_values = pd.to_numeric(results_df[ph_col], errors="coerce")
        if ph_values.min() < 2:
            reasons.append("pH below 2")
        if ph_values.max() > 10:
            reasons.append("pH above 10")

    # negative concentration check
    for col in results_df.columns:
        col_lower = col.lower()

        if col_lower in ["time", "day", "days", "t", "ph"]:
            continue

        values = pd.to_numeric(results_df[col], errors="coerce")

        if values.notna().sum() == 0:
            continue

        if values.min() < -1e-12:
            reasons.append(f"negative values in {col}")

        finite_values = values.replace([np.inf, -np.inf], np.nan).dropna()
        if finite_values.empty:
            reasons.append(f"non-finite values in {col}")
            continue

        if finite_values.max() > 1e3:
            reasons.append(f"extremely high value in {col}")

    return len(reasons) > 0, reasons


def classify_run_quality(
    run_dir,
    objective_score=None,
    previous_best_score=None,
    results_df=None,
    log_filename=None,
):
    """
    Main V10.9 run-quality classifier.

    Parameters
    ----------
    run_dir : str or Path
        MIN3P run directory.
    objective_score : float, optional
        Current run objective score. Lower is better.
    previous_best_score : float, optional
        Previous best objective score. Lower is better.
    results_df : pandas.DataFrame, optional
        Model output dataframe for chemistry checks.
    log_filename : str, optional
        Specific log filename. If None, searches for *.log.

    Returns
    -------
    dict
    """

    run_dir = Path(run_dir)

    # --------------------------------------------------
    # Find log file
    # --------------------------------------------------
    if log_filename:
        log_file = run_dir / log_filename
    else:
        log_files = list(run_dir.glob("*.log"))
        log_file = log_files[0] if log_files else None

    log_text = read_log_text(log_file) if log_file else ""

    normal_exit = has_normal_exit(log_text)
    failed_step_fraction = extract_failed_step_fraction(log_text)
    runtime_warning = classify_runtime_warning(failed_step_fraction)

    # --------------------------------------------------
    # Objective improvement
    # --------------------------------------------------
    objective_improved = None

    if objective_score is not None and previous_best_score is not None:
        try:
            objective_improved = float(objective_score) < float(previous_best_score)
        except Exception:
            objective_improved = None

    # --------------------------------------------------
    # Scientific chemistry checks
    # --------------------------------------------------
    nonphysical, scientific_reasons = detect_nonphysical_chemistry(results_df)

    reasons = []

    if not normal_exit:
        reasons.append("MIN3P did not report normal exit")

    if runtime_warning not in ["none", "unknown"]:
        reasons.append(f"runtime warning: failed timestep fraction = {failed_step_fraction}")

    if objective_improved is True:
        reasons.append("objective improved")
    elif objective_improved is False:
        reasons.append("objective did not improve")

    reasons.extend(scientific_reasons)

    # --------------------------------------------------
    # Rejection logic
    # --------------------------------------------------
    scientific_rejection = False

    if not normal_exit:
        scientific_rejection = True

    if nonphysical:
        scientific_rejection = True

    # Failed timestep fraction is soft:
    # reject only if very high AND objective is worse
    if failed_step_fraction is not None:
        if failed_step_fraction > 0.40 and objective_improved is False:
            scientific_rejection = True
            reasons.append(
                "failed timestep fraction > 40% and objective did not improve"
            )

    # --------------------------------------------------
    # Final run status
    # --------------------------------------------------
    if scientific_rejection:
        if normal_exit:
            run_status = "valid_unstable"
        else:
            run_status = "failed"

    elif runtime_warning in ["moderate", "high"]:
        run_status = "valid_suspicious"

    else:
        run_status = "valid_good"

    return {
        "run_status": run_status,
        "normal_exit": normal_exit,
        "failed_step_fraction": failed_step_fraction,
        "runtime_warning": runtime_warning,
        "objective_score": objective_score,
        "previous_best_score": previous_best_score,
        "objective_improved": objective_improved,
        "scientific_rejection": scientific_rejection,
        "reason": "; ".join(reasons),
        "log_file": str(log_file) if log_file else None,
    }


def classify_and_save(
    run_dir,
    objective_score=None,
    previous_best_score=None,
    results_df=None,
    output_file=None,
):
    """
    Convenience function to classify one run and save result to Excel.
    """

    result = classify_run_quality(
        run_dir=run_dir,
        objective_score=objective_score,
        previous_best_score=previous_best_score,
        results_df=results_df,
    )

    if output_file is None:
        output_file = Path(run_dir) / "run_quality_V10_9.xlsx"

    df = pd.DataFrame([result])
    df.to_excel(output_file, index=False)

    return result