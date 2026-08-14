from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict
import shutil

import pandas as pd


BEST_PARAMETERS_FILENAME = "best_parameters_V10_9.xlsx"
MEMORY_FILENAME = "parameter_state_memory_V10_9.xlsx"
BACKUP_DIRNAME = "parameter_state_backups_V10_9"


def _timestamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _safe_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"true", "1", "yes", "y"}


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _append_memory(results_dir: Path, row: Dict[str, Any]) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    memory_file = results_dir / MEMORY_FILENAME
    row = {"timestamp": _timestamp(), **row}
    df_new = pd.DataFrame([row])

    if memory_file.exists():
        try:
            df_old = pd.read_excel(memory_file)
            df_new = pd.concat([df_old, df_new], ignore_index=True)
        except Exception:
            pass

    df_new.to_excel(memory_file, index=False)
    return memory_file


def _backup_file(config_file: Path, results_dir: Path, label: str) -> Path | None:
    if not config_file.exists():
        return None

    backup_dir = results_dir / BACKUP_DIRNAME
    backup_dir.mkdir(parents=True, exist_ok=True)

    backup_file = backup_dir / f"{config_file.stem}_{label}_{_stamp()}{config_file.suffix}"
    shutil.copy2(config_file, backup_file)
    return backup_file


def _read_excel_sheets(file_path: Path) -> dict[str, pd.DataFrame]:
    xls = pd.ExcelFile(file_path)
    return {sheet: pd.read_excel(file_path, sheet_name=sheet) for sheet in xls.sheet_names}


def _write_excel_sheets(file_path: Path, sheets: dict[str, pd.DataFrame]) -> None:
    with pd.ExcelWriter(file_path, engine="openpyxl") as writer:
        for sheet, df in sheets.items():
            df.to_excel(writer, sheet_name=sheet, index=False)


def _find_parameter_sheet(sheets: dict[str, pd.DataFrame]) -> str | None:
    for sheet, df in sheets.items():
        lower = {str(c).strip().lower(): c for c in df.columns}
        if "parameter" in lower and "value" in lower:
            return sheet
    return None


def _is_good_history_row(row: pd.Series) -> bool:
    run_status = str(row.get("run_status", "success")).strip().lower()
    if run_status and run_status not in {"success", "success_with_retries", "partial_success", "nan"}:
        return False

    qc_rejection = row.get("qc_scientific_rejection")
    if _safe_bool(qc_rejection):
        return False

    return True


def _best_row_from_table(df: pd.DataFrame) -> pd.Series | None:
    if df is None or df.empty:
        return None

    good_rows = []
    for idx, row in df.iterrows():
        if _is_good_history_row(row):
            good_rows.append(idx)

    if good_rows:
        df = df.loc[good_rows].copy()

    score_columns = [
        "TOTAL_SCORE",
        "qc_objective_score",
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

    for col in score_columns:
        if col in df.columns:
            scores = pd.to_numeric(df[col], errors="coerce")
            if scores.notna().any():
                return df.loc[scores.idxmin()]

    # Fallback: latest good row if no score is available.
    if not df.empty:
        return df.iloc[-1]

    return None


def get_best_history_row(
    ranking_file: str | Path | None,
    history_file: str | Path | None,
) -> pd.Series | None:
    """Return the best previous row using run_ranking.xlsx first, then optimization_history.xlsx."""
    for file_path in [ranking_file, history_file]:
        if file_path is None:
            continue
        file_path = Path(file_path)
        if not file_path.exists():
            continue
        try:
            df = pd.read_excel(file_path)
        except Exception:
            continue
        row = _best_row_from_table(df)
        if row is not None:
            return row
    return None




def _parameter_names_from_config_sheets(sheets: dict[str, pd.DataFrame]) -> list[str]:
    """Return parameter names from the parameter/value sheet of agent_config.xlsx."""
    parameter_sheet = _find_parameter_sheet(sheets)
    if parameter_sheet is None:
        return []
    df = sheets[parameter_sheet]
    lower = {str(c).strip().lower(): c for c in df.columns}
    param_col = lower.get("parameter")
    if param_col is None:
        return []
    names = []
    for value in df[param_col].tolist():
        name = str(value).strip()
        if name and name.lower() != "nan":
            names.append(name)
    return names


def _count_parameter_values_in_row(row: pd.Series | None, parameter_names: list[str]) -> int:
    """Count how many calibration parameter values are available in a history/ranking row."""
    if row is None:
        return 0
    count = 0
    for parameter in parameter_names:
        if parameter in row.index and pd.notna(row.get(parameter)):
            count += 1
    return count


def _best_history_row_with_parameter_values(
    history_file: str | Path | None,
    parameter_names: list[str],
) -> pd.Series | None:
    """Return the best optimization_history row that actually contains parameter values.

    V10.9 run_ranking.xlsx is useful for scores, but it may not contain the full
    parameter state. For rollback, the best memory must be reconstructed from a
    row that contains the calibrated parameter columns. Otherwise only some
    values are restored and bad changes can accumulate.
    """
    if history_file is None:
        return None
    history_file = Path(history_file)
    if not history_file.exists():
        return None
    try:
        df = pd.read_excel(history_file)
    except Exception:
        return None
    if df.empty or not parameter_names:
        return None

    good_idx = []
    for idx, row in df.iterrows():
        if _is_good_history_row(row) and _count_parameter_values_in_row(row, parameter_names) > 0:
            good_idx.append(idx)
    if not good_idx:
        return None

    df = df.loc[good_idx].copy()
    score_columns = [
        "TOTAL_SCORE",
        "objective_total",
        "qc_objective_score",
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
    for col in score_columns:
        if col in df.columns:
            scores = pd.to_numeric(df[col], errors="coerce")
            if scores.notna().any():
                return df.loc[scores.idxmin()]

    return df.iloc[-1]


def _matching_history_row_for_ranking_row(
    ranking_row: pd.Series | None,
    history_file: str | Path | None,
    parameter_names: list[str],
) -> pd.Series | None:
    """Map a best row from run_ranking.xlsx back to optimization_history.xlsx.

    Ranking rows sometimes lack calibration parameter columns. When possible,
    use run_folder/run_id/timestamp to find the corresponding history row.
    """
    if ranking_row is None or history_file is None:
        return None
    history_file = Path(history_file)
    if not history_file.exists():
        return None
    try:
        hist = pd.read_excel(history_file)
    except Exception:
        return None
    if hist.empty:
        return None

    candidates = hist.copy()
    for key in ["run_folder", "run_id", "timestamp"]:
        if key in ranking_row.index and key in candidates.columns and pd.notna(ranking_row.get(key)):
            value = str(ranking_row.get(key))
            mask = candidates[key].astype(str).eq(value)
            if not mask.any() and key == "run_folder":
                # Be tolerant of absolute/relative path differences.
                run_name = Path(value).name
                mask = candidates[key].astype(str).str.contains(run_name, case=False, na=False, regex=False)
            if mask.any():
                subset = candidates[mask].copy()
                subset = subset[
                    subset.apply(lambda r: _count_parameter_values_in_row(r, parameter_names) > 0, axis=1)
                ]
                if not subset.empty:
                    return subset.iloc[-1]
    return None

def create_best_parameters_from_history(
    config_file: str | Path,
    results_dir: str | Path,
    ranking_file: str | Path | None,
    history_file: str | Path | None,
) -> Dict[str, Any]:
    """
    Bootstrap best_parameters_V10_9.xlsx from the best row in run_ranking/history.

    This is important when old runs were deleted but optimization_history.xlsx still
    contains the parameter values of earlier good runs.
    """
    config_file = Path(config_file)
    results_dir = Path(results_dir)
    best_file = results_dir / BEST_PARAMETERS_FILENAME

    if not config_file.exists():
        return {
            "parameter_memory_action": "bootstrap_failed",
            "parameter_memory_reason": f"config file does not exist: {config_file}",
            "best_parameters_file": str(best_file),
        }

    best_row = get_best_history_row(ranking_file, history_file)
    if best_row is None:
        shutil.copy2(config_file, best_file)
        return {
            "parameter_memory_action": "initialized_best_from_current_config",
            "parameter_memory_reason": "no scored history/ranking row was available; copied current config",
            "best_parameters_file": str(best_file),
        }

    try:
        sheets = _read_excel_sheets(config_file)
        parameter_sheet = _find_parameter_sheet(sheets)
        if parameter_sheet is None:
            shutil.copy2(config_file, best_file)
            return {
                "parameter_memory_action": "initialized_best_from_current_config",
                "parameter_memory_reason": "could not find a parameter/value sheet; copied current config",
                "best_parameters_file": str(best_file),
            }

        df_params = sheets[parameter_sheet].copy()
        lower = {str(c).strip().lower(): c for c in df_params.columns}
        param_col = lower["parameter"]
        value_col = lower["value"]
        parameter_names = [
            str(v).strip()
            for v in df_params[param_col].tolist()
            if str(v).strip() and str(v).strip().lower() != "nan"
        ]

        # run_ranking.xlsx is authoritative for objective selection. Map that
        # exact ranked-best run back to history to obtain complete parameters.
        # Do not replace it with a separately selected legacy history row.
        ranking_mapped_row = _matching_history_row_for_ranking_row(
            best_row, history_file, parameter_names
        )
        mapped_count = _count_parameter_values_in_row(
            ranking_mapped_row, parameter_names
        )

        if mapped_count > 0:
            best_source = "ranking_row_mapped_to_history"
            best_update_row = ranking_mapped_row
            best_count = mapped_count
        else:
            # Fallback only if the selected ranking run cannot be mapped.
            history_parameter_row = _best_history_row_with_parameter_values(
                history_file, parameter_names
            )
            best_candidates = [
                ("optimization_history_parameter_row", history_parameter_row),
                ("generic_best_row", best_row),
            ]
            best_source = "generic_best_row"
            best_update_row = best_row
            best_count = -1
            for source_name, candidate_row in best_candidates:
                count = _count_parameter_values_in_row(candidate_row, parameter_names)
                if count > best_count:
                    best_count = count
                    best_source = source_name
                    best_update_row = candidate_row

        updated = 0
        for idx, row in df_params.iterrows():
            parameter = str(row.get(param_col, "")).strip()
            if not parameter or parameter.lower() == "nan":
                continue
            if best_update_row is not None and parameter in best_update_row.index and pd.notna(best_update_row.get(parameter)):
                df_params.at[idx, value_col] = best_update_row.get(parameter)
                updated += 1

        sheets[parameter_sheet] = df_params
        _write_excel_sheets(best_file, sheets)

        score = None
        if best_update_row is not None:
            for score_col in ["TOTAL_SCORE", "objective_total", "qc_objective_score", "objective_score"]:
                if score_col in best_update_row.index:
                    score = _safe_float(best_update_row.get(score_col))
                    if score is not None:
                        break

        return {
            "parameter_memory_action": "bootstrapped_best_from_history",
            "parameter_memory_reason": (
                f"created best parameter file from {best_source}; "
                f"updated {updated}/{len(parameter_names)} parameter values"
            ),
            "best_parameters_file": str(best_file),
            "best_source_run_folder": str(best_update_row.get("run_folder", "")) if best_update_row is not None else "",
            "best_source_score": score,
            "best_source_table": best_source,
            "best_parameter_values_updated": updated,
            "best_parameter_values_available": best_count,
        }

    except Exception as exc:
        shutil.copy2(config_file, best_file)
        return {
            "parameter_memory_action": "initialized_best_from_current_config",
            "parameter_memory_reason": f"bootstrap from history failed ({exc}); copied current config",
            "best_parameters_file": str(best_file),
        }


def ensure_best_parameter_file(
    config_file: str | Path,
    results_dir: str | Path,
    ranking_file: str | Path | None = None,
    history_file: str | Path | None = None,
) -> Dict[str, Any]:
    config_file = Path(config_file)
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    best_file = results_dir / BEST_PARAMETERS_FILENAME

    if best_file.exists():
        return {
            "parameter_memory_action": "best_file_exists",
            "parameter_memory_reason": "best parameter file already exists",
            "best_parameters_file": str(best_file),
        }

    return create_best_parameters_from_history(config_file, results_dir, ranking_file, history_file)


def save_current_as_best_parameters(
    config_file: str | Path,
    results_dir: str | Path,
    acceptance: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Save current agent_config.xlsx as 04_results/best_parameters_V10_9.xlsx."""
    config_file = Path(config_file)
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    best_file = results_dir / BEST_PARAMETERS_FILENAME

    if not config_file.exists():
        out = {
            "parameter_memory_action": "save_best_failed",
            "parameter_memory_reason": f"config file does not exist: {config_file}",
            "best_parameters_file": str(best_file),
        }
        _append_memory(results_dir, out | {"run_folder": (acceptance or {}).get("run_folder", "")})
        return out

    old_best_backup = None
    if best_file.exists():
        old_best_backup = _backup_file(best_file, results_dir, "previous_best")

    shutil.copy2(config_file, best_file)

    out = {
        "parameter_memory_action": "saved_current_as_best",
        "parameter_memory_reason": "run accepted as new best; current agent_config copied to best parameter memory",
        "best_parameters_file": str(best_file),
        "config_backup_file": "",
        "old_best_backup_file": str(old_best_backup) if old_best_backup else "",
    }
    _append_memory(
        results_dir,
        out
        | {
            "run_folder": (acceptance or {}).get("run_folder", ""),
            "acceptance_status": (acceptance or {}).get("acceptance_status"),
            "objective_score": (acceptance or {}).get("objective_score"),
            "previous_best_score": (acceptance or {}).get("previous_best_score"),
        },
    )
    return out




def _history_last_new_best_timestamp(history_file: str | Path | None) -> pd.Timestamp | None:
    """Return timestamp of the latest *true* new-best row.

    Important V11 bug fix:
    the old test used string contains("best"), which incorrectly matched
    "accepted_valid_not_best". That made every valid-but-worse run look like a
    new best, so rollback ignored the corresponding suggestion rows.
    """
    if history_file is None:
        return None
    history_file = Path(history_file)
    if not history_file.exists():
        return None
    try:
        hist = pd.read_excel(history_file)
    except Exception:
        return None
    if hist.empty or "timestamp" not in hist.columns:
        return None

    mask = pd.Series(False, index=hist.index)

    # Explicit boolean flags only.
    for col in ["qc_is_new_best", "is_new_best", "new_best", "should_update_best"]:
        if col in hist.columns:
            mask = mask | hist[col].astype(str).str.strip().str.lower().isin(
                ["true", "1", "1.0", "yes", "y"]
            )

    # Exact status/action labels only. Never use contains("best") because
    # accepted_valid_not_best contains the word "best" but is not a new best.
    exact_new_best_labels = {
        "accepted_best",
        "new_best",
        "accepted_new_best",
        "accept_new_best",
        "saved_current_as_best",
    }
    for col in ["qc_acceptance_status", "acceptance_status", "decision_action"]:
        if col in hist.columns:
            values = hist[col].astype(str).str.strip().str.lower()
            mask = mask | values.isin(exact_new_best_labels)

    if not mask.any():
        return None
    times = pd.to_datetime(hist.loc[mask, "timestamp"], errors="coerce").dropna()
    if times.empty:
        return None
    return times.max()


def _same_numeric_value(a: Any, b: Any, rel_tol: float = 1.0e-9, abs_tol: float = 1.0e-12) -> bool:
    """Robust numeric comparison for Excel-read parameter values."""
    af = _safe_float(a)
    bf = _safe_float(b)
    if af is None or bf is None:
        return str(a).strip() == str(b).strip()
    scale = max(abs(af), abs(bf), 1.0)
    return abs(af - bf) <= max(abs_tol, rel_tol * scale)


def _undo_recent_nonbest_v11_suggestions(
    config_file: Path,
    results_dir: Path,
    history_file: str | Path | None = None,
) -> Dict[str, Any]:
    """Undo selected V11 suggestions that remained in agent_config after non-best runs.

    This is a conservative rollback safeguard.

    Logic:
    1. Read parameter_suggestions_V11.xlsx.
    2. Keep only selected/applied suggestions.
    3. If a true new-best timestamp exists, only consider suggestions after it.
    4. Process latest suggestion per parameter.
    5. Restore old_value only when the active config value still equals the
       logged new_value. This prevents overwriting values that were already
       restored correctly from best_parameters_V10_9.xlsx.

    Example:
    - phi_calcite may already be correctly restored by best_parameters_V10_9.xlsx;
      skip it if current value no longer equals the bad new_value.
    - feoh_s_area remained 604.8, and selected row says old=672.0/new=604.8;
      restore it to 672.0.
    """
    suggestion_file = results_dir / "parameter_suggestions_V11.xlsx"
    if not suggestion_file.exists() or not config_file.exists():
        return {
            "undo_action": "not_needed",
            "undo_reason": "missing parameter_suggestions_V11.xlsx or agent_config.xlsx",
            "undo_count": 0,
            "undo_parameters": "",
            "undo_skipped_count": 0,
            "undo_skipped_parameters": "",
        }

    try:
        suggestions = pd.read_excel(suggestion_file)
    except Exception as exc:
        return {
            "undo_action": "failed",
            "undo_reason": f"could not read parameter suggestions: {exc}",
            "undo_count": 0,
            "undo_parameters": "",
            "undo_skipped_count": 0,
            "undo_skipped_parameters": "",
        }

    if suggestions.empty or "parameter" not in suggestions.columns:
        return {
            "undo_action": "not_needed",
            "undo_reason": "suggestion log is empty or has no parameter column",
            "undo_count": 0,
            "undo_parameters": "",
            "undo_skipped_count": 0,
            "undo_skipped_parameters": "",
        }

    # Keep only suggestions that really changed agent_config.xlsx.
    if "applied_to_agent_config" in suggestions.columns:
        applied = suggestions["applied_to_agent_config"].astype(str).str.strip().str.lower().isin(
            ["true", "1", "1.0", "yes", "y"]
        )
        suggestions = suggestions[applied].copy()
    if "selection_status" in suggestions.columns:
        selected = suggestions["selection_status"].astype(str).str.contains("selected", case=False, na=False)
        suggestions = suggestions[selected].copy()
    if suggestions.empty:
        return {
            "undo_action": "not_needed",
            "undo_reason": "no selected/applied V11 suggestions found",
            "undo_count": 0,
            "undo_parameters": "",
            "undo_skipped_count": 0,
            "undo_skipped_parameters": "",
        }

    required = {"parameter", "old_value", "new_value"}
    if not required.issubset(set(suggestions.columns)):
        return {
            "undo_action": "not_needed",
            "undo_reason": "suggestion log lacks old_value/new_value columns",
            "undo_count": 0,
            "undo_parameters": "",
            "undo_skipped_count": 0,
            "undo_skipped_parameters": "",
        }

    # Consider only suggestions after a true new-best, if known. If there has
    # not been a true new-best row, consider all selected/applied V11 suggestions.
    last_best_ts = _history_last_new_best_timestamp(history_file)
    if last_best_ts is not None and "timestamp" in suggestions.columns:
        st = pd.to_datetime(suggestions["timestamp"], errors="coerce")
        suggestions = suggestions[st > last_best_ts].copy()

    if suggestions.empty:
        return {
            "undo_action": "not_needed",
            "undo_reason": "no selected/applied V11 suggestions after latest true new-best",
            "undo_count": 0,
            "undo_parameters": "",
            "undo_skipped_count": 0,
            "undo_skipped_parameters": "",
        }

    try:
        sheets = _read_excel_sheets(config_file)
        parameter_sheet = _find_parameter_sheet(sheets)
        if parameter_sheet is None:
            return {
                "undo_action": "failed",
                "undo_reason": "could not find parameter/value sheet in agent_config.xlsx",
                "undo_count": 0,
                "undo_parameters": "",
                "undo_skipped_count": 0,
                "undo_skipped_parameters": "",
            }

        params = sheets[parameter_sheet].copy()
        lower = {str(c).strip().lower(): c for c in params.columns}
        pcol = lower["parameter"]
        vcol = lower["value"]

        if "timestamp" in suggestions.columns:
            suggestions = suggestions.copy()
            suggestions["_ts"] = pd.to_datetime(suggestions["timestamp"], errors="coerce")
            suggestions = suggestions.sort_values("_ts", ascending=False)
        else:
            suggestions = suggestions.iloc[::-1].copy()

        restored: list[str] = []
        skipped: list[str] = []
        seen: set[str] = set()

        for _, srow in suggestions.iterrows():
            parameter = str(srow.get("parameter", "")).strip()
            if not parameter or parameter.lower() == "nan" or parameter in seen:
                continue
            seen.add(parameter)

            old_value = srow.get("old_value")
            new_value = srow.get("new_value")
            if _safe_float(old_value) is None and str(old_value).strip().lower() in {"", "nan", "none"}:
                skipped.append(parameter)
                continue

            mask = params[pcol].astype(str).str.strip().eq(parameter)
            if not mask.any():
                skipped.append(parameter)
                continue

            current_value = params.loc[mask, vcol].iloc[0]

            # Critical safety: only undo if the bad new_value is still present.
            # If best-file restore already fixed the parameter, do not overwrite it
            # with old_value from an intermediate non-best suggestion.
            if not _same_numeric_value(current_value, new_value):
                skipped.append(parameter)
                continue

            params.loc[mask, vcol] = old_value
            restored.append(parameter)

        if restored:
            sheets[parameter_sheet] = params
            _write_excel_sheets(config_file, sheets)
            return {
                "undo_action": "undid_recent_v11_nonbest_suggestions",
                "undo_reason": (
                    "restored old_value only where active value still matched the logged bad new_value"
                ),
                "undo_count": len(restored),
                "undo_parameters": ", ".join(restored),
                "undo_skipped_count": len(skipped),
                "undo_skipped_parameters": ", ".join(skipped),
            }

        return {
            "undo_action": "not_needed",
            "undo_reason": "no active config values matched logged bad new_value; nothing to undo",
            "undo_count": 0,
            "undo_parameters": "",
            "undo_skipped_count": len(skipped),
            "undo_skipped_parameters": ", ".join(skipped),
        }

    except Exception as exc:
        return {
            "undo_action": "failed",
            "undo_reason": f"undo failed: {exc}",
            "undo_count": 0,
            "undo_parameters": "",
            "undo_skipped_count": 0,
            "undo_skipped_parameters": "",
        }

def restore_best_parameters(
    config_file: str | Path,
    results_dir: str | Path,
    ranking_file: str | Path | None = None,
    history_file: str | Path | None = None,
    acceptance: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Restore 04_results/best_parameters_V10_9.xlsx into active agent_config.xlsx."""
    config_file = Path(config_file)
    results_dir = Path(results_dir)
    best_info = ensure_best_parameter_file(config_file, results_dir, ranking_file, history_file)
    best_file = Path(best_info.get("best_parameters_file", results_dir / BEST_PARAMETERS_FILENAME))

    if not best_file.exists():
        out = {
            "parameter_memory_action": "restore_failed",
            "parameter_memory_reason": f"best parameter file does not exist: {best_file}",
            "best_parameters_file": str(best_file),
            "config_backup_file": "",
        }
        _append_memory(results_dir, out | {"run_folder": (acceptance or {}).get("run_folder", "")})
        return out

    config_backup = _backup_file(config_file, results_dir, "before_restore")
    shutil.copy2(best_file, config_file)

    # V13 uses the frozen-reference TOTAL_SCORE ranking as the source of truth.
    # A V11 undo log may contain changes labelled "not best" under the old,
    # incompatible objective scale. Never apply that legacy undo after a
    # TOTAL_SCORE-based rebuild/restore.
    ranking_has_total_score = False
    ranking_path = Path(ranking_file) if ranking_file is not None else None
    if ranking_path is not None and ranking_path.exists():
        try:
            ranking_df = pd.read_excel(ranking_path, nrows=1)
            ranking_has_total_score = "TOTAL_SCORE" in ranking_df.columns
        except Exception:
            ranking_has_total_score = False

    if ranking_has_total_score:
        undo_info = {
            "undo_action": "skipped_legacy_undo_total_score_ranking",
            "undo_reason": (
                "legacy V11 suggestion undo skipped because run_ranking.xlsx "
                "contains authoritative TOTAL_SCORE"
            ),
            "undo_count": 0,
            "undo_parameters": "",
            "undo_skipped_count": 0,
            "undo_skipped_parameters": "",
        }
    else:
        undo_info = _undo_recent_nonbest_v11_suggestions(
            config_file, results_dir, history_file
        )

    # Refresh best memory only if a genuine legacy undo was applied.
    if int(undo_info.get("undo_count", 0) or 0) > 0:
        _backup_file(best_file, results_dir, "before_undo_refresh")
        shutil.copy2(config_file, best_file)

    bootstrap_reason = best_info.get("parameter_memory_reason", "")
    reason = "restored active agent_config from best parameter memory"
    if int(undo_info.get("undo_count", 0) or 0) > 0:
        reason = reason + f"; {undo_info.get('undo_reason')} [{undo_info.get('undo_parameters')}]"
    if best_info.get("parameter_memory_action") not in {"best_file_exists", None}:
        reason = f"{reason}; {bootstrap_reason}"

    out = {
        "parameter_memory_action": "restored_best_parameters",
        "parameter_memory_reason": reason,
        "best_parameters_file": str(best_file),
        "config_backup_file": str(config_backup) if config_backup else "",
        "best_source_run_folder": best_info.get("best_source_run_folder", ""),
        "best_source_score": best_info.get("best_source_score"),
        "undo_action": undo_info.get("undo_action"),
        "undo_reason": undo_info.get("undo_reason"),
        "undo_count": undo_info.get("undo_count"),
        "undo_parameters": undo_info.get("undo_parameters"),
    }

    _append_memory(
        results_dir,
        out
        | {
            "run_folder": (acceptance or {}).get("run_folder", ""),
            "acceptance_status": (acceptance or {}).get("acceptance_status"),
            "objective_score": (acceptance or {}).get("objective_score"),
            "previous_best_score": (acceptance or {}).get("previous_best_score"),
        },
    )
    return out


def apply_parameter_memory_policy(
    config_file: str | Path,
    results_dir: str | Path,
    history_file: str | Path | None,
    ranking_file: str | Path | None,
    acceptance: Dict[str, Any],
    allow_restore: bool = False,
) -> Dict[str, Any]:
    """
    Apply V10.9 best-parameter memory policy.

    Policy:
    - accepted_best: save active agent_config.xlsx as best_parameters_V10_9.xlsx
    - accepted_valid_not_best / rejected_scientific / failed: restore best only when
      allow_restore=True, usually in auto mode before the next suggestion
    - accepted_valid_unscored: do not modify config
    """
    status = str(acceptance.get("acceptance_status", "")).strip().lower()

    if _safe_bool(acceptance.get("should_update_best")) or status == "accepted_best":
        return save_current_as_best_parameters(config_file, results_dir, acceptance)

    if status in {"accepted_valid_not_best", "rejected_scientific", "failed"}:
        if allow_restore:
            return restore_best_parameters(
                config_file=config_file,
                results_dir=results_dir,
                ranking_file=ranking_file,
                history_file=history_file,
                acceptance=acceptance,
            )

        # Even in single mode, create/ensure the best-parameter file exists.
        # We only defer restoring agent_config.xlsx; the reference best file
        # must still be available for later auto-mode rollback.
        best_info = ensure_best_parameter_file(
            config_file=config_file,
            results_dir=results_dir,
            ranking_file=ranking_file,
            history_file=history_file,
        )

        out = {
            "parameter_memory_action": "restore_deferred",
            "parameter_memory_reason": (
                "run is not new best; active config not restored because "
                "allow_restore=False; best parameter file ensured"
            ),
            "best_parameters_file": best_info.get(
                "best_parameters_file", str(Path(results_dir) / BEST_PARAMETERS_FILENAME)
            ),
            "config_backup_file": "",
            "best_source_run_folder": best_info.get("best_source_run_folder", ""),
            "best_source_score": best_info.get("best_source_score"),
            "best_bootstrap_action": best_info.get("parameter_memory_action"),
            "best_bootstrap_reason": best_info.get("parameter_memory_reason"),
        }
        _append_memory(Path(results_dir), out | {"run_folder": acceptance.get("run_folder", "")})
        return out

    out = {
        "parameter_memory_action": "no_action",
        "parameter_memory_reason": "run acceptance status does not require saving or restoring parameters",
        "best_parameters_file": str(Path(results_dir) / BEST_PARAMETERS_FILENAME),
        "config_backup_file": "",
    }
    _append_memory(Path(results_dir), out | {"run_folder": acceptance.get("run_folder", "")})
    return out
