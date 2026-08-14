from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

import pandas as pd


BAD_MEMORY_FILE = "bad_suggestion_memory_V10_9.xlsx"
PARAMETER_EXHAUSTION_FILE = "parameter_exhaustion_V10_9.xlsx"
PARAMETER_EXHAUSTION_FILTER_LOG_FILE = "parameter_exhaustion_filter_log_V10_9.xlsx"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    try:
        if pd.isna(value):
            return False
    except Exception:
        pass
    return str(value).strip().lower() in {"true", "1", "yes", "y", "avoid", "block"}


def _read_excel(path: str | Path) -> pd.DataFrame:
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_excel(path)
    except Exception:
        return pd.DataFrame()


def _append_excel(path: str | Path, rows: pd.DataFrame) -> Path:
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
    out.to_excel(path, index=False)
    return path


def _normalise_bad_memory(bad: pd.DataFrame) -> pd.DataFrame:
    if bad is None or bad.empty:
        return pd.DataFrame()
    required = {"parameter", "direction"}
    if not required.issubset(set(bad.columns)):
        return pd.DataFrame()

    out = bad.copy()
    out["parameter"] = out["parameter"].astype(str).str.strip()
    out["direction"] = out["direction"].astype(str).str.strip().str.lower()
    out = out[out["parameter"].ne("") & out["parameter"].str.lower().ne("nan")]
    out = out[out["direction"].isin(["increase", "decrease"])]

    if "avoid_repeat" in out.columns:
        out = out[out["avoid_repeat"].apply(_to_bool)]
    if "block_same_direction" in out.columns:
        out = out[out["block_same_direction"].apply(_to_bool)]

    return out.reset_index(drop=True)


def update_parameter_exhaustion_memory(
    results_dir: str | Path,
    bad_memory_file: str | Path | None = None,
    exhaustion_filename: str = PARAMETER_EXHAUSTION_FILE,
) -> Dict[str, Any]:
    """
    Create/update parameter_exhaustion_V10_9.xlsx.

    V10.9 exhaustion rule:
    If a parameter has both increase and decrease recorded as bad moves, mark it
    as temporarily_exhausted and avoid it in future suggestions.
    """
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    if bad_memory_file is None:
        bad_memory_file = results_dir / BAD_MEMORY_FILE
    else:
        bad_memory_file = Path(bad_memory_file)

    exhaustion_file = results_dir / exhaustion_filename
    bad = _normalise_bad_memory(_read_excel(bad_memory_file))

    if bad.empty:
        empty = pd.DataFrame(
            columns=[
                "timestamp",
                "parameter",
                "bad_increase_count",
                "bad_decrease_count",
                "total_bad_count",
                "last_tested",
                "parameter_status",
                "avoid_parameter",
                "reason",
                "recommended_action",
            ]
        )
        empty.to_excel(exhaustion_file, index=False)
        return {
            "parameter_exhaustion_action": "no_bad_memory",
            "parameter_exhaustion_reason": "bad-suggestion memory is missing or empty; no exhausted parameters detected",
            "parameter_exhaustion_count": 0,
            "parameter_exhaustion_parameters": "",
            "parameter_exhaustion_file": str(exhaustion_file),
        }

    now = _now()
    rows: list[dict[str, Any]] = []
    for parameter, group in bad.groupby("parameter", sort=True):
        directions = group["direction"].astype(str).str.lower()
        inc_count = int((directions == "increase").sum())
        dec_count = int((directions == "decrease").sum())
        total_count = int(len(group))

        last_tested = ""
        if "timestamp" in group.columns:
            try:
                last_tested = str(group["timestamp"].dropna().astype(str).iloc[-1])
            except Exception:
                last_tested = ""

        exhausted = inc_count > 0 and dec_count > 0
        if exhausted:
            status = "temporarily_exhausted"
            reason = "both increase and decrease have been recorded as bad moves"
            recommended_action = "skip this parameter temporarily and try another process/mineral"
        else:
            status = "direction_limited"
            bad_dir = "increase" if inc_count > 0 else "decrease"
            reason = f"only {bad_dir} direction is currently recorded as bad"
            recommended_action = "allow opposite direction if scientifically meaningful"

        rows.append(
            {
                "timestamp": now,
                "parameter": parameter,
                "bad_increase_count": inc_count,
                "bad_decrease_count": dec_count,
                "total_bad_count": total_count,
                "last_tested": last_tested,
                "parameter_status": status,
                "avoid_parameter": bool(exhausted),
                "reason": reason,
                "recommended_action": recommended_action,
            }
        )

    table = pd.DataFrame(rows)
    table.to_excel(exhaustion_file, index=False)

    exhausted_table = table[table["avoid_parameter"].apply(_to_bool)].copy()
    exhausted_parameters = ""
    if not exhausted_table.empty:
        exhausted_parameters = ", ".join(exhausted_table["parameter"].astype(str).tolist())

    return {
        "parameter_exhaustion_action": "updated",
        "parameter_exhaustion_reason": f"evaluated {len(table)} parameter(s); {len(exhausted_table)} temporarily exhausted",
        "parameter_exhaustion_count": int(len(exhausted_table)),
        "parameter_exhaustion_parameters": exhausted_parameters,
        "parameter_exhaustion_file": str(exhaustion_file),
    }


def _exhausted_parameters_from_file(exhaustion_file: str | Path) -> set[str]:
    table = _read_excel(exhaustion_file)
    if table.empty or "parameter" not in table.columns:
        return set()

    out = table.copy()
    if "avoid_parameter" in out.columns:
        out = out[out["avoid_parameter"].apply(_to_bool)]
    elif "parameter_status" in out.columns:
        out = out[out["parameter_status"].astype(str).str.lower().eq("temporarily_exhausted")]
    else:
        return set()

    return set(out["parameter"].astype(str).str.strip().tolist())


def _write_filter_log(results_dir: str | Path, rows: pd.DataFrame, log_filename: str) -> Path:
    log_file = Path(results_dir) / log_filename
    if rows is None or rows.empty:
        rows = pd.DataFrame([{"timestamp": _now(), "filter_status": "empty_log_row"}])
    return _append_excel(log_file, rows)


def filter_suggestions_against_parameter_exhaustion(
    suggestions: pd.DataFrame,
    results_dir: str | Path,
    exhaustion_filename: str = PARAMETER_EXHAUSTION_FILE,
    log_filename: str = PARAMETER_EXHAUSTION_FILTER_LOG_FILE,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    """
    Remove suggestions for parameters marked temporarily_exhausted.
    """
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    exhaustion_file = results_dir / exhaustion_filename
    log_file = results_dir / log_filename

    if suggestions is None:
        suggestions = pd.DataFrame()

    if suggestions.empty:
        rows = pd.DataFrame(
            [
                {
                    "timestamp": _now(),
                    "filter_status": "no_suggestions",
                    "parameter": "",
                    "blocked": False,
                    "blocked_reason": "suggestions dataframe is empty",
                }
            ]
        )
        _write_filter_log(results_dir, rows, log_filename)
        return suggestions, {
            "parameter_exhaustion_filter_action": "no_suggestions",
            "parameter_exhaustion_candidate_count": 0,
            "parameter_exhaustion_blocked_count": 0,
            "parameter_exhaustion_kept_count": 0,
            "parameter_exhaustion_blocked_parameters": "",
            "parameter_exhaustion_allowed_parameters": "",
            "parameter_exhaustion_filter_reason": "suggestions dataframe is empty",
            "parameter_exhaustion_filter_log_file": str(log_file),
        }

    exhausted = _exhausted_parameters_from_file(exhaustion_file)
    if not exhausted:
        rows = suggestions.copy().reset_index(drop=True)
        rows.insert(0, "candidate_rank", range(1, len(rows) + 1))
        if "timestamp" not in rows.columns:
            rows.insert(0, "timestamp", _now())
        else:
            rows["timestamp"] = _now()
        rows["filter_status"] = "allowed_no_exhausted_parameters"
        rows["blocked"] = False
        rows["blocked_reason"] = "no temporarily exhausted parameter was found"
        _write_filter_log(results_dir, rows, log_filename)

        allowed_parameters = ""
        if "parameter" in suggestions.columns:
            allowed_parameters = ", ".join(suggestions["parameter"].astype(str).tolist())

        return suggestions.reset_index(drop=True), {
            "parameter_exhaustion_filter_action": "no_exhausted_parameters",
            "parameter_exhaustion_candidate_count": int(len(suggestions)),
            "parameter_exhaustion_blocked_count": 0,
            "parameter_exhaustion_kept_count": int(len(suggestions)),
            "parameter_exhaustion_blocked_parameters": "",
            "parameter_exhaustion_allowed_parameters": allowed_parameters,
            "parameter_exhaustion_filter_reason": "no temporarily exhausted parameters found; all suggestions allowed",
            "parameter_exhaustion_filter_log_file": str(log_file),
        }

    kept_rows = []
    blocked_rows = []
    audit_rows = []
    suggestions = suggestions.reset_index(drop=True)

    for rank, row in suggestions.iterrows():
        candidate_rank = int(rank) + 1
        parameter = str(row.get("parameter", "")).strip()
        blocked = parameter in exhausted

        audit = row.to_dict()
        audit["timestamp"] = _now()
        audit["candidate_rank"] = candidate_rank
        audit["filter_status"] = "blocked_exhausted_parameter" if blocked else "allowed"
        audit["blocked"] = bool(blocked)

        if blocked:
            reason = f"parameter temporarily exhausted: {parameter}"
            audit["blocked_reason"] = reason
            audit["blocked_parameter"] = parameter
            blocked_rows.append(audit.copy())
        else:
            audit["blocked_reason"] = "allowed; parameter is not temporarily exhausted"
            audit["blocked_parameter"] = ""
            kept_rows.append(row)

        audit_rows.append(audit)

    kept = pd.DataFrame(kept_rows).reset_index(drop=True) if kept_rows else suggestions.iloc[0:0].copy()
    blocked_df = pd.DataFrame(blocked_rows)
    _write_filter_log(results_dir, pd.DataFrame(audit_rows), log_filename)

    blocked_parameters = ""
    if not blocked_df.empty and "blocked_parameter" in blocked_df.columns:
        blocked_parameters = ", ".join(blocked_df["blocked_parameter"].astype(str).tolist())

    allowed_parameters = ""
    if not kept.empty and "parameter" in kept.columns:
        allowed_parameters = ", ".join(kept["parameter"].astype(str).tolist())

    action = "filtered_exhausted_parameters" if len(blocked_df) else "no_exhausted_candidates"
    if kept.empty and len(blocked_df):
        action = "all_candidates_exhausted"

    return kept, {
        "parameter_exhaustion_filter_action": action,
        "parameter_exhaustion_candidate_count": int(len(suggestions)),
        "parameter_exhaustion_blocked_count": int(len(blocked_df)),
        "parameter_exhaustion_kept_count": int(len(kept)),
        "parameter_exhaustion_blocked_parameters": blocked_parameters,
        "parameter_exhaustion_allowed_parameters": allowed_parameters,
        "parameter_exhaustion_filter_reason": "blocked suggestions for temporarily exhausted parameters"
        if len(blocked_df)
        else "suggestions checked; none used temporarily exhausted parameters",
        "parameter_exhaustion_filter_log_file": str(log_file),
    }
