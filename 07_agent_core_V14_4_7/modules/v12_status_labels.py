"""
V12 status-label utilities.

Purpose:
- Keep old V11/V10 history readable.
- Replace unclear label:
    accepted_valid_not_best -> valid_not_new_best
- Avoid changing core engine mechanics.
"""

from __future__ import annotations

from typing import Any, Iterable, MutableMapping


VALID_NOT_NEW_BEST = "valid_not_new_best"

STATUS_LABEL_RENAMES = {
    "accepted_valid_not_best": VALID_NOT_NEW_BEST,
    "accepted-not-best": VALID_NOT_NEW_BEST,
    "valid_not_best": VALID_NOT_NEW_BEST,
}


def _is_nan(value: Any) -> bool:
    try:
        return value != value
    except Exception:
        return False


def normalize_status_label(value: Any) -> Any:
    """
    Convert legacy or unclear status labels to V12 canonical labels.

    Non-string values are returned unchanged.
    """
    if value is None or _is_nan(value):
        return value

    if not isinstance(value, str):
        return value

    key = value.strip()
    return STATUS_LABEL_RENAMES.get(key, key)


def normalize_status_record(
    record: MutableMapping[str, Any],
    keys: Iterable[str] | None = None,
) -> MutableMapping[str, Any]:
    """
    Normalize selected status-like fields in a dict-like history record.
    """
    if keys is None:
        keys = (
            "status",
            "run_status",
            "candidate_status",
            "acceptance_status",
            "decision_status",
            "qc_status",
            "qc_no_progress_stop_action",
        )

    for key in keys:
        if key in record:
            record[key] = normalize_status_label(record[key])

    return record


def normalize_status_records(records: list[MutableMapping[str, Any]]) -> list[MutableMapping[str, Any]]:
    """
    Normalize status fields in a list of history records.
    """
    return [normalize_status_record(r) for r in records]


def normalize_status_dataframe(df, columns: Iterable[str] | None = None):
    """
    Normalize status-like columns in a pandas DataFrame.

    This function does not import pandas directly, so the module remains lightweight.
    """
    if columns is None:
        columns = (
            "status",
            "run_status",
            "candidate_status",
            "acceptance_status",
            "decision_status",
            "qc_status",
            "qc_no_progress_stop_action",
        )

    for col in columns:
        if col in df.columns:
            df[col] = df[col].map(normalize_status_label)

    return df
