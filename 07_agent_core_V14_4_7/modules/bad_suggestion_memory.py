from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

import numpy as np
import pandas as pd


BAD_MEMORY_FILE = "bad_suggestion_memory_V10_9.xlsx"
BAD_FILTER_LOG_FILE = "bad_suggestion_filter_log_V10_9.xlsx"
BEST_PARAMETERS_FILE = "best_parameters_V10_9.xlsx"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or pd.isna(value):
            return None
        return float(value)
    except Exception:
        return None


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or pd.isna(value):
        return False
    text = str(value).strip().lower()
    return text in {"true", "1", "yes", "y", "avoid", "block"}


def _direction(old_value: Any, new_value: Any, rtol: float = 1e-12, atol: float = 1e-18) -> str:
    old = _safe_float(old_value)
    new = _safe_float(new_value)
    if old is None or new is None:
        return "unknown"
    if np.isclose(old, new, rtol=rtol, atol=atol):
        return "unchanged"
    return "increase" if new > old else "decrease"


def _opposite_direction(direction: str) -> str:
    d = str(direction).strip().lower()
    if d == "increase":
        return "decrease"
    if d == "decrease":
        return "increase"
    return "unknown"


def _read_excel(path: str | Path, sheet_name: str | int | None = 0) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_excel(path, sheet_name=sheet_name)
    except Exception:
        return pd.DataFrame()


def _append_excel(path: str | Path, rows: pd.DataFrame, deduplicate_subset: Iterable[str] | None = None) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if rows is None or rows.empty:
        if not path.exists():
            pd.DataFrame().to_excel(path, index=False)
        return path

    out = rows.copy()
    if path.exists():
        try:
            old = pd.read_excel(path)
            out = pd.concat([old, out], ignore_index=True)
        except Exception:
            pass

    if deduplicate_subset:
        subset = [c for c in deduplicate_subset if c in out.columns]
        if subset:
            out = out.drop_duplicates(subset=subset, keep="last")

    out.to_excel(path, index=False)
    return path


def _find_history_row(history_file: str | Path, run_dir: str | Path | None = None) -> pd.Series | None:
    df = _read_excel(history_file)
    if df.empty:
        return None

    if run_dir is None or "run_folder" not in df.columns:
        return df.iloc[-1]

    run_name = Path(run_dir).name
    run_path = str(Path(run_dir))
    mask = (
        df["run_folder"].astype(str).str.contains(run_path, case=False, na=False, regex=False)
        | df["run_folder"].astype(str).str.contains(run_name, case=False, na=False, regex=False)
    )
    if mask.any():
        return df[mask].iloc[-1]
    return df.iloc[-1]


def _read_best_parameters(best_parameters_file: str | Path) -> pd.DataFrame:
    best = _read_excel(best_parameters_file, sheet_name="parameters")
    if best.empty or "parameter" not in best.columns or "value" not in best.columns:
        return pd.DataFrame(columns=["parameter", "value"])
    out = best[["parameter", "value"]].copy()
    out["parameter"] = out["parameter"].astype(str).str.strip()
    out = out[out["parameter"].ne("") & out["parameter"].str.lower().ne("nan")]
    return out


def infer_parameter_moves_from_history(
    history_row: pd.Series,
    best_parameters: pd.DataFrame,
    rtol: float = 1e-9,
    atol: float = 1e-15,
) -> pd.DataFrame:
    """
    Infer which parameters were tested in the current run by comparing the
    parameter values written in optimization_history.xlsx to
    best_parameters_V10_9.xlsx.
    """
    if history_row is None or best_parameters is None or best_parameters.empty:
        return pd.DataFrame()

    records: list[dict[str, Any]] = []
    for _, brow in best_parameters.iterrows():
        parameter = str(brow.get("parameter", "")).strip()
        if not parameter or parameter.lower() == "nan":
            continue
        if parameter not in history_row.index:
            continue

        best_value = _safe_float(brow.get("value"))
        tested_value = _safe_float(history_row.get(parameter))
        if best_value is None or tested_value is None:
            continue

        if np.isclose(best_value, tested_value, rtol=rtol, atol=atol):
            continue

        direction = "increase" if tested_value > best_value else "decrease"
        factor = None
        if best_value not in (None, 0):
            factor = tested_value / best_value

        records.append(
            {
                "parameter": parameter,
                "best_value": best_value,
                "tested_value": tested_value,
                "direction": direction,
                "factor": factor,
                "absolute_change": tested_value - best_value,
                "relative_change_percent": None
                if best_value == 0
                else (tested_value - best_value) / abs(best_value) * 100.0,
            }
        )

    return pd.DataFrame(records)


def update_bad_suggestion_memory(
    results_dir: str | Path,
    history_file: str | Path,
    best_parameters_file: str | Path | None,
    acceptance: Dict[str, Any],
    run_dir: str | Path | None = None,
    filename: str = BAD_MEMORY_FILE,
) -> Dict[str, Any]:
    """
    Record parameter moves that made the objective worse or were scientifically
    rejected.
    """
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    if best_parameters_file is None:
        best_parameters_file = results_dir / BEST_PARAMETERS_FILE
    else:
        best_parameters_file = Path(best_parameters_file)

    memory_file = results_dir / filename

    status = str(acceptance.get("acceptance_status", "")).strip().lower()
    score_change = _safe_float(acceptance.get("score_change"))

    record_statuses = {"accepted_valid_not_best", "rejected_scientific", "failed"}
    should_record = status in record_statuses

    # For safety, do not record neutral/non-worse valid scored runs.
    if status == "accepted_valid_not_best" and score_change is not None and score_change <= 0:
        should_record = False

    if not should_record:
        return {
            "bad_suggestion_action": "not_recorded",
            "bad_suggestion_reason": f"acceptance_status={status}; no bad move recorded",
            "bad_suggestion_count": 0,
            "bad_suggestion_file": str(memory_file),
        }

    history_row = _find_history_row(history_file, run_dir=run_dir)
    best_params = _read_best_parameters(best_parameters_file)
    moves = infer_parameter_moves_from_history(history_row, best_params)

    if moves.empty:
        return {
            "bad_suggestion_action": "no_parameter_change_detected",
            "bad_suggestion_reason": "run was not a new best, but no parameter difference from best_parameters_V10_9.xlsx was detected",
            "bad_suggestion_count": 0,
            "bad_suggestion_file": str(memory_file),
        }

    timestamp = _now()
    rows = []
    for _, move in moves.iterrows():
        rows.append(
            {
                "timestamp": timestamp,
                "run_folder": str(run_dir) if run_dir is not None else str(acceptance.get("run_folder", "")),
                "parameter": move.get("parameter"),
                "direction": move.get("direction"),
                "best_value": move.get("best_value"),
                "tested_value": move.get("tested_value"),
                "factor": move.get("factor"),
                "absolute_change": move.get("absolute_change"),
                "relative_change_percent": move.get("relative_change_percent"),
                "objective_score": acceptance.get("objective_score"),
                "previous_best_score": acceptance.get("previous_best_score"),
                "score_change": acceptance.get("score_change"),
                "percent_improvement": acceptance.get("percent_improvement"),
                "acceptance_status": acceptance.get("acceptance_status"),
                "acceptance_reason": acceptance.get("acceptance_reason"),
                "avoid_repeat": True,
                "block_same_direction": True,
            }
        )

    rows_df = pd.DataFrame(rows)
    _append_excel(
        memory_file,
        rows_df,
        deduplicate_subset=["run_folder", "parameter", "direction", "tested_value"],
    )

    return {
        "bad_suggestion_action": "recorded_bad_moves",
        "bad_suggestion_reason": f"recorded {len(rows_df)} parameter move(s) that did not improve the objective",
        "bad_suggestion_count": int(len(rows_df)),
        "bad_suggestion_file": str(memory_file),
        "bad_suggestion_parameters": ", ".join(rows_df["parameter"].astype(str).tolist()),
    }


def _read_bad_memory(results_dir: str | Path, filename: str = BAD_MEMORY_FILE) -> pd.DataFrame:
    return _read_excel(Path(results_dir) / filename)


def _suggestion_direction(row: pd.Series) -> str:
    for old_col, new_col in [
        ("old_value", "new_value"),
        ("current_value", "suggested_value"),
        ("value_old", "value_new"),
    ]:
        if old_col in row.index and new_col in row.index:
            return _direction(row.get(old_col), row.get(new_col))
    return "unknown"


def _value_columns(row: pd.Series) -> tuple[str | None, str | None]:
    for old_col, new_col in [
        ("old_value", "new_value"),
        ("current_value", "suggested_value"),
        ("value_old", "value_new"),
    ]:
        if old_col in row.index and new_col in row.index:
            return old_col, new_col
    return None, None


def _write_filter_audit_log(
    results_dir: str | Path,
    rows: pd.DataFrame,
    log_filename: str = BAD_FILTER_LOG_FILE,
) -> Path:
    """Append a filter-audit row even when nothing is blocked."""
    log_file = Path(results_dir) / log_filename
    if rows is None:
        rows = pd.DataFrame()
    if rows.empty:
        rows = pd.DataFrame([{"timestamp": _now(), "filter_status": "empty_log_row"}])
    return _append_excel(log_file, rows)


def _build_bad_pairs(active_bad: pd.DataFrame) -> set[tuple[str, str]]:
    if active_bad.empty or "parameter" not in active_bad.columns or "direction" not in active_bad.columns:
        return set()
    return set(
        zip(
            active_bad["parameter"].astype(str).str.strip(),
            active_bad["direction"].astype(str).str.strip().str.lower(),
        )
    )


def filter_suggestions_against_bad_memory(
    suggestions: pd.DataFrame,
    results_dir: str | Path,
    filename: str = BAD_MEMORY_FILE,
    log_filename: str = BAD_FILTER_LOG_FILE,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Remove suggestions that repeat a parameter + direction previously recorded
    as a bad move. The opposite direction is not blocked here; it can be tested
    by the opposite-direction retry layer.
    """
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    memory_file = results_dir / filename
    log_file = results_dir / log_filename

    if suggestions is None:
        suggestions = pd.DataFrame()

    if suggestions.empty:
        log_rows = pd.DataFrame(
            [
                {
                    "timestamp": _now(),
                    "filter_status": "no_suggestions",
                    "parameter": "",
                    "direction": "",
                    "blocked": False,
                    "reason": "suggestions dataframe is empty",
                }
            ]
        )
        _write_filter_audit_log(results_dir, log_rows, log_filename=log_filename)
        return suggestions, {
            "bad_suggestion_filter_action": "no_suggestions",
            "bad_suggestion_candidate_count": 0,
            "bad_suggestion_blocked_count": 0,
            "bad_suggestion_kept_count": 0,
            "bad_suggestion_blocked_parameters": "",
            "bad_suggestion_allowed_parameters": "",
            "bad_suggestion_filter_file": str(memory_file),
            "bad_suggestion_filter_log_file": str(log_file),
            "bad_suggestion_filter_reason": "suggestions dataframe is empty",
            "bad_suggestion_first_candidate_blocked": False,
            "_blocked_candidates": pd.DataFrame(),
            "_bad_pairs": set(),
        }

    bad = _read_bad_memory(results_dir, filename=filename)

    if bad.empty or "parameter" not in bad.columns or "direction" not in bad.columns:
        log_rows = suggestions.copy().reset_index(drop=True)

        if "candidate_rank" not in log_rows.columns:
            log_rows.insert(0, "candidate_rank", range(1, len(log_rows) + 1))
        else:
            log_rows["candidate_rank"] = range(1, len(log_rows) + 1)

        if "timestamp" not in log_rows.columns:
            log_rows.insert(0, "timestamp", _now())
        else:
            log_rows["timestamp"] = _now()

        log_rows["filter_status"] = "allowed_no_bad_memory"
        log_rows["suggestion_direction"] = log_rows.apply(_suggestion_direction, axis=1)
        log_rows["blocked"] = False
        log_rows["blocked_reason"] = "bad-suggestion memory file is missing or empty"
        _write_filter_audit_log(results_dir, log_rows, log_filename=log_filename)

        allowed_parameters = (
            ", ".join(suggestions.get("parameter", pd.Series(dtype=str)).astype(str).tolist())
            if "parameter" in suggestions.columns
            else ""
        )
        return suggestions.reset_index(drop=True), {
            "bad_suggestion_filter_action": "no_bad_memory",
            "bad_suggestion_candidate_count": int(len(suggestions)),
            "bad_suggestion_blocked_count": 0,
            "bad_suggestion_kept_count": int(len(suggestions)),
            "bad_suggestion_blocked_parameters": "",
            "bad_suggestion_allowed_parameters": allowed_parameters,
            "bad_suggestion_filter_file": str(memory_file),
            "bad_suggestion_filter_log_file": str(log_file),
            "bad_suggestion_filter_reason": "bad-suggestion memory file is missing or empty; all suggestions allowed",
            "bad_suggestion_first_candidate_blocked": False,
            "_blocked_candidates": pd.DataFrame(),
            "_bad_pairs": set(),
        }

    active_bad = bad.copy()
    if "avoid_repeat" in active_bad.columns:
        active_bad = active_bad[active_bad["avoid_repeat"].apply(_to_bool)]
    if "block_same_direction" in active_bad.columns:
        active_bad = active_bad[active_bad["block_same_direction"].apply(_to_bool)]

    bad_pairs = _build_bad_pairs(active_bad)

    kept_rows = []
    blocked_rows = []
    audit_rows = []
    first_candidate_blocked = False

    suggestions = suggestions.reset_index(drop=True)

    for candidate_rank, row in suggestions.iterrows():
        candidate_rank = int(candidate_rank) + 1
        parameter = str(row.get("parameter", "")).strip()
        direction = _suggestion_direction(row).strip().lower()
        key = (parameter, direction)

        blocked = bool(parameter and direction not in {"unknown", "unchanged"} and key in bad_pairs)
        if candidate_rank == 1:
            first_candidate_blocked = blocked

        audit = row.to_dict()
        audit["timestamp"] = _now()
        audit["candidate_rank"] = candidate_rank
        audit["filter_status"] = "blocked" if blocked else "allowed"
        audit["suggestion_direction"] = direction
        audit["blocked"] = blocked

        if blocked:
            reason = f"repeats bad move: {parameter} {direction}"
            b = row.to_dict()
            b["candidate_rank"] = candidate_rank
            b["blocked_reason"] = reason
            b["blocked_parameter"] = parameter
            b["blocked_direction"] = direction
            blocked_rows.append(b)
            audit["blocked_reason"] = reason
            audit["blocked_parameter"] = parameter
            audit["blocked_direction"] = direction
        else:
            kept_rows.append(row)
            audit["blocked_reason"] = "allowed; does not repeat bad parameter direction"
            audit["blocked_parameter"] = ""
            audit["blocked_direction"] = ""

        audit_rows.append(audit)

    kept = pd.DataFrame(kept_rows).reset_index(drop=True) if kept_rows else suggestions.iloc[0:0].copy()
    blocked = pd.DataFrame(blocked_rows)
    audit_df = pd.DataFrame(audit_rows)

    _write_filter_audit_log(results_dir, audit_df, log_filename=log_filename)

    blocked_parameters = ""
    if not blocked.empty and "blocked_parameter" in blocked.columns:
        blocked_parameters = ", ".join(blocked["blocked_parameter"].astype(str).tolist())

    allowed_parameters = ""
    if not kept.empty and "parameter" in kept.columns:
        allowed_parameters = ", ".join(kept["parameter"].astype(str).tolist())

    action = "filtered" if len(blocked_rows) else "no_blocked_suggestions"
    if kept.empty and len(blocked_rows):
        action = "all_suggestions_blocked"

    return kept, {
        "bad_suggestion_filter_action": action,
        "bad_suggestion_candidate_count": int(len(suggestions)),
        "bad_suggestion_blocked_count": int(len(blocked_rows)),
        "bad_suggestion_kept_count": int(len(kept)),
        "bad_suggestion_blocked_parameters": blocked_parameters,
        "bad_suggestion_allowed_parameters": allowed_parameters,
        "bad_suggestion_filter_file": str(memory_file),
        "bad_suggestion_filter_log_file": str(log_file),
        "bad_suggestion_filter_reason": "blocked suggestions that repeated known bad parameter directions"
        if len(blocked_rows)
        else "suggestions checked; no suggested move repeated bad-suggestion memory",
        "bad_suggestion_first_candidate_blocked": bool(first_candidate_blocked),
        "_blocked_candidates": blocked,
        "_bad_pairs": bad_pairs,
    }


def _make_opposite_retry_candidate(blocked_row: pd.Series, bad_pairs: set[tuple[str, str]]) -> dict[str, Any] | None:
    parameter = str(blocked_row.get("blocked_parameter", blocked_row.get("parameter", ""))).strip()
    source_direction = str(blocked_row.get("blocked_direction", _suggestion_direction(blocked_row))).strip().lower()
    retry_direction = _opposite_direction(source_direction)
    if not parameter or retry_direction == "unknown":
        return None
    if (parameter, retry_direction) in bad_pairs:
        return None

    old_col, new_col = _value_columns(blocked_row)
    if old_col is None or new_col is None:
        return None

    old_value = _safe_float(blocked_row.get(old_col))
    source_new_value = _safe_float(blocked_row.get(new_col))
    if old_value is None or source_new_value is None:
        return None

    # Multiplicative retry. For a bad 1.1 increase, test 1/1.1 = 0.9091.
    # For zero-valued parameters, a multiplicative opposite direction is not
    # scientifically well-defined, so skip the retry.
    if np.isclose(old_value, 0.0, rtol=0.0, atol=1e-30):
        return None

    source_factor = source_new_value / old_value
    if source_factor <= 0 or np.isclose(source_factor, 1.0, rtol=1e-12, atol=1e-18):
        return None

    retry_factor = 1.0 / source_factor
    retry_new_value = old_value * retry_factor

    retry = blocked_row.to_dict()
    retry[new_col] = retry_new_value
    retry["opposite_retry"] = True
    retry["retry_parameter"] = parameter
    retry["retry_source_direction"] = source_direction
    retry["retry_direction"] = retry_direction
    retry["retry_source_factor"] = source_factor
    retry["retry_factor"] = retry_factor
    retry["blocked"] = False
    retry["filter_status"] = "opposite_retry_generated"
    retry["suggestion_direction"] = retry_direction
    retry["decision_source"] = "V10_9_opposite_direction_retry"

    if "factor_requested" in retry:
        retry["factor_requested"] = retry_factor
    if "factor_applied" in retry:
        retry["factor_applied"] = retry_factor
    if "priority" in retry:
        try:
            retry["priority"] = float(retry["priority"])
        except Exception:
            pass

    reason = str(retry.get("reason", "")).strip()
    retry_note = f"V10.9 opposite-direction retry: {parameter} {retry_direction} after blocked bad {source_direction}"
    retry["reason"] = f"{retry_note}. {reason}" if reason else retry_note

    evidence = str(retry.get("evidence", "")).strip()
    retry_evidence = f"blocked bad move was {parameter} {source_direction}; testing opposite direction with factor={retry_factor:.6g}"
    retry["evidence"] = f"{retry_evidence}; {evidence}" if evidence else retry_evidence

    retry["blocked_reason"] = ""
    retry["blocked_parameter"] = ""
    retry["blocked_direction"] = ""

    return retry


def _generate_opposite_retry_candidates(filter_info: Dict[str, Any]) -> pd.DataFrame:
    blocked = filter_info.get("_blocked_candidates") if isinstance(filter_info, dict) else pd.DataFrame()
    bad_pairs = filter_info.get("_bad_pairs", set()) if isinstance(filter_info, dict) else set()

    if blocked is None or not isinstance(blocked, pd.DataFrame) or blocked.empty:
        return pd.DataFrame()

    retry_rows = []
    seen: set[tuple[str, str]] = set()
    if "candidate_rank" in blocked.columns:
        blocked = blocked.sort_values("candidate_rank")

    for _, brow in blocked.iterrows():
        retry = _make_opposite_retry_candidate(brow, bad_pairs)
        if retry is None:
            continue
        key = (str(retry.get("retry_parameter", retry.get("parameter", ""))), str(retry.get("retry_direction", "")))
        if key in seen:
            continue
        seen.add(key)
        retry_rows.append(retry)

    return pd.DataFrame(retry_rows)


def _append_opposite_retry_audit(
    results_dir: str | Path,
    retry_candidates: pd.DataFrame,
    log_filename: str = BAD_FILTER_LOG_FILE,
) -> None:
    if retry_candidates is None or retry_candidates.empty:
        return
    rows = retry_candidates.copy().reset_index(drop=True)
    if "timestamp" not in rows.columns:
        rows.insert(0, "timestamp", _now())
    else:
        rows["timestamp"] = _now()
    rows["filter_status"] = "opposite_retry_generated"
    rows["blocked"] = False
    rows["blocked_reason"] = "opposite direction generated from blocked bad move"
    _write_filter_audit_log(results_dir, rows, log_filename=log_filename)


def select_suggestions_with_fallback(
    filtered_suggestions: pd.DataFrame,
    filter_info: Dict[str, Any],
    max_changes: int = 1,
    results_dir: str | Path | None = None,
    log_filename: str = BAD_FILTER_LOG_FILE,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Select suggestions after V10.9 filtering.

    New V10.9 rule:
    - If the highest-priority candidate is blocked because that direction was
      previously bad, generate one opposite-direction retry for that parameter
      before falling back to unrelated parameters.
    - If the opposite direction is also recorded as bad, skip it and use the
      next unblocked candidate.
    """
    if filtered_suggestions is None:
        filtered_suggestions = pd.DataFrame()
    if filter_info is None:
        filter_info = {}

    max_changes = max(int(max_changes or 1), 1)
    blocked_count = int(_safe_float(filter_info.get("bad_suggestion_blocked_count")) or 0)
    kept_count = int(_safe_float(filter_info.get("bad_suggestion_kept_count")) or 0)
    candidate_count = int(_safe_float(filter_info.get("bad_suggestion_candidate_count")) or (blocked_count + kept_count))
    first_blocked = bool(filter_info.get("bad_suggestion_first_candidate_blocked"))

    retry_candidates = pd.DataFrame()
    if first_blocked:
        retry_candidates = _generate_opposite_retry_candidates(filter_info)
        if results_dir is not None:
            _append_opposite_retry_audit(results_dir, retry_candidates, log_filename=log_filename)

    if first_blocked and not retry_candidates.empty:
        selection_pool = pd.concat([retry_candidates, filtered_suggestions], ignore_index=True)
    else:
        selection_pool = filtered_suggestions.copy().reset_index(drop=True)

    selected = selection_pool.head(max_changes).copy().reset_index(drop=True)

    selected_parameters = ""
    if not selected.empty and "parameter" in selected.columns:
        selected_parameters = ", ".join(selected["parameter"].astype(str).tolist())

    retry_count = int(len(retry_candidates))
    retry_parameters = ""
    retry_directions = ""
    if retry_count:
        if "retry_parameter" in retry_candidates.columns:
            retry_parameters = ", ".join(retry_candidates["retry_parameter"].astype(str).tolist())
        elif "parameter" in retry_candidates.columns:
            retry_parameters = ", ".join(retry_candidates["parameter"].astype(str).tolist())
        if "retry_direction" in retry_candidates.columns:
            retry_directions = ", ".join(retry_candidates["retry_direction"].astype(str).tolist())

    selected_retry_count = 0
    selected_retry_parameters = ""
    selected_retry_directions = ""
    if not selected.empty and "opposite_retry" in selected.columns:
        selected_retry_mask = selected["opposite_retry"].apply(_to_bool)
        selected_retry_count = int(selected_retry_mask.sum())
        if selected_retry_count:
            retry_selected = selected[selected_retry_mask]
            if "retry_parameter" in retry_selected.columns:
                selected_retry_parameters = ", ".join(retry_selected["retry_parameter"].astype(str).tolist())
            elif "parameter" in retry_selected.columns:
                selected_retry_parameters = ", ".join(retry_selected["parameter"].astype(str).tolist())
            if "retry_direction" in retry_selected.columns:
                selected_retry_directions = ", ".join(retry_selected["retry_direction"].astype(str).tolist())

    if selected.empty:
        if blocked_count > 0 and kept_count == 0:
            fallback_action = "all_candidates_blocked"
            fallback_reason = "all candidate suggestions were blocked by bad-suggestion memory"
        else:
            fallback_action = "no_candidate_selected"
            fallback_reason = "no candidate suggestion was available for selection"
        fallback_used = False
    elif selected_retry_count > 0:
        fallback_action = "selected_opposite_direction_retry"
        fallback_reason = "top blocked bad move skipped; opposite direction selected before unrelated candidates"
        fallback_used = True
    elif blocked_count > 0 and first_blocked:
        fallback_action = "selected_next_unblocked"
        fallback_reason = "top blocked candidate skipped; no valid opposite retry was available, so next unblocked suggestion selected"
        fallback_used = True
    elif blocked_count > 0:
        fallback_action = "selected_top_allowed_with_lower_blocked"
        fallback_reason = "top suggestion was allowed; lower-priority blocked suggestions were ignored"
        fallback_used = False
    else:
        fallback_action = "selected_top_allowed"
        fallback_reason = "top suggestion was allowed; fallback was not needed"
        fallback_used = False

    if retry_count == 0:
        if first_blocked:
            retry_action = "not_generated"
            retry_reason = "top suggestion was blocked, but no valid opposite-direction retry could be generated"
        else:
            retry_action = "not_needed"
            retry_reason = "top suggestion was not blocked"
    elif selected_retry_count > 0:
        retry_action = "generated_and_selected"
        retry_reason = "opposite-direction retry was generated and selected"
    else:
        retry_action = "generated_not_selected"
        retry_reason = "opposite-direction retry was generated but not selected within max_changes"

    out_info = dict(filter_info)
    out_info.update(
        {
            "bad_suggestion_fallback_action": fallback_action,
            "bad_suggestion_fallback_used": bool(fallback_used),
            "bad_suggestion_fallback_reason": fallback_reason,
            "bad_suggestion_candidate_count": candidate_count,
            "bad_suggestion_selected_count": int(len(selected)),
            "bad_suggestion_selected_parameters": selected_parameters,
            "bad_suggestion_requested_changes": max_changes,
            "bad_suggestion_opposite_retry_action": retry_action,
            "bad_suggestion_opposite_retry_used": bool(selected_retry_count > 0),
            "bad_suggestion_opposite_retry_count": retry_count,
            "bad_suggestion_opposite_retry_parameters": retry_parameters,
            "bad_suggestion_opposite_retry_directions": retry_directions,
            "bad_suggestion_opposite_retry_selected_count": selected_retry_count,
            "bad_suggestion_opposite_retry_selected_parameters": selected_retry_parameters,
            "bad_suggestion_opposite_retry_selected_directions": selected_retry_directions,
            "bad_suggestion_opposite_retry_reason": retry_reason,
            "bad_suggestion_first_candidate_blocked": bool(first_blocked),
        }
    )

    out_info.pop("_blocked_candidates", None)
    out_info.pop("_bad_pairs", None)

    return selected, out_info
