from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from min3p_ai_pipeline_V14 import V14Workflow


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def safe_text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except (TypeError, ValueError):
        pass
    return str(value).strip()


def first_text(row: pd.Series, names: Iterable[str]) -> str:
    for name in names:
        if name in row.index:
            value = safe_text(row.get(name))
            if value:
                return value
    return ""


def first_number(row: pd.Series, names: Iterable[str]) -> float | None:
    for name in names:
        if name not in row.index:
            continue

        value = pd.to_numeric(
            pd.Series([row.get(name)]),
            errors="coerce",
        ).iloc[0]

        if pd.notna(value):
            return float(value)

    return None


def normalise_path(value: Any) -> str:
    return safe_text(value).replace("/", "\\").rstrip("\\")


def path_exists(value: str) -> bool:
    try:
        return bool(value) and Path(value).exists()
    except OSError:
        return False


def extract_folder_from_json(raw_value: Any) -> str:
    """
    Search normal and nested JSON locations used by V14 diagnostics.
    """
    if not isinstance(raw_value, str) or not raw_value.strip():
        return ""

    try:
        payload = json.loads(raw_value)
    except json.JSONDecodeError:
        return ""

    if not isinstance(payload, dict):
        return ""

    direct_names = (
        "run_folder",
        "candidate_run_folder",
        "run_path",
    )

    for name in direct_names:
        value = normalise_path(payload.get(name))
        if value:
            return value

    nested_names = (
        "v14_residual_diagnostics",
        "v14_numerical_diagnostics",
        "v14_plausibility_diagnostics",
        "diagnostics",
    )

    for nested_name in nested_names:
        nested = payload.get(nested_name)

        if not isinstance(nested, dict):
            continue

        for name in direct_names:
            value = normalise_path(nested.get(name))
            if value:
                return value

    return ""


def filter_text_column(
    frame: pd.DataFrame,
    possible_columns: Iterable[str],
    required_value: str,
) -> pd.DataFrame:
    required = safe_text(required_value).casefold()

    if not required:
        return frame

    for column in possible_columns:
        if column not in frame.columns:
            continue

        matched = frame[
            frame[column]
            .fillna("")
            .astype(str)
            .str.strip()
            .str.casefold()
            .eq(required)
        ]

        if not matched.empty:
            return matched

    return frame


def filter_numeric_column(
    frame: pd.DataFrame,
    possible_columns: Iterable[str],
    required_value: float | None,
    relative_tolerance: float = 1.0e-6,
) -> pd.DataFrame:
    if required_value is None or not math.isfinite(required_value):
        return frame

    for column in possible_columns:
        if column not in frame.columns:
            continue

        numeric = pd.to_numeric(frame[column], errors="coerce")

        tolerance = max(
            1.0e-12,
            abs(required_value) * relative_tolerance,
        )

        matched = frame[
            numeric.notna()
            & (numeric - required_value).abs().le(tolerance)
        ]

        if not matched.empty:
            return matched

    return frame


def latest_row(frame: pd.DataFrame) -> pd.Series:
    working = frame.copy()

    if "timestamp" in working.columns:
        working["_repair_timestamp"] = pd.to_datetime(
            working["timestamp"],
            errors="coerce",
        )
        working = working.sort_values(
            "_repair_timestamp",
            na_position="first",
        )

    return working.iloc[-1]


def recover_run_folder(
    results_dir: Path,
    decision_row: pd.Series,
    candidate_id: str,
    parameter: str,
    direction: str,
    candidate_value: float | None,
    candidate_score: float,
) -> tuple[str, str]:
    """
    Return:
        run_folder, source_description
    """

    # ------------------------------------------------------------------
    # 1. Direct columns in the decision/event row
    # ------------------------------------------------------------------
    direct_folder = first_text(
        decision_row,
        (
            "run_folder",
            "candidate_run_folder",
            "run_path",
        ),
    )

    direct_folder = normalise_path(direct_folder)

    if direct_folder:
        return direct_folder, "direct history-row column"

    # ------------------------------------------------------------------
    # 2. JSON columns in the decision/event row
    # ------------------------------------------------------------------
    for json_column in (
        "event_json",
        "diagnostics_json",
    ):
        if json_column not in decision_row.index:
            continue

        json_folder = extract_folder_from_json(
            decision_row.get(json_column)
        )

        if json_folder:
            return json_folder, json_column

    # ------------------------------------------------------------------
    # 3. External run-history/ranking workbooks
    # ------------------------------------------------------------------
    possible_sources = (
        results_dir / "optimization_history.xlsx",
        results_dir / "run_ranking.xlsx",
        results_dir / "optimization_history_V14.xlsx",
        results_dir / "run_diagnostics_V11.xlsx",
    )

    diagnostic_candidates: list[str] = []

    for source in possible_sources:
        if not source.exists():
            continue

        try:
            frame = pd.read_excel(source)
        except Exception as exc:
            diagnostic_candidates.append(
                f"{source.name}: could not read: {type(exc).__name__}: {exc}"
            )
            continue

        if frame.empty:
            continue

        working = frame.copy()

        # Candidate ID is the strongest key when available.
        id_matched = False

        for id_column in (
            "candidate_id",
            "optimizer_candidate_id",
            "trajectory_id",
        ):
            if id_column not in working.columns:
                continue

            candidate_rows = working[
                working[id_column]
                .fillna("")
                .astype(str)
                .str.strip()
                .eq(candidate_id)
            ]

            if not candidate_rows.empty:
                working = candidate_rows
                id_matched = True
                break

        # Most run-ranking rows do not carry candidate_id, so match the
        # deterministic identifying fields.
        if not id_matched:
            working = filter_text_column(
                working,
                ("optimizer_parameter", "parameter"),
                parameter,
            )

            working = filter_text_column(
                working,
                ("optimizer_direction", "direction"),
                direction,
            )

            working = filter_numeric_column(
                working,
                (
                    "qc_objective_score",
                    "optimizer_candidate_objective",
                    "candidate_objective",
                    "effective_objective",
                    "TOTAL_SCORE",
                ),
                candidate_score,
                relative_tolerance=2.0e-6,
            )

            working = filter_numeric_column(
                working,
                (
                    "optimizer_candidate_value",
                    "new_value",
                    parameter,
                ),
                candidate_value,
                relative_tolerance=2.0e-6,
            )

        if working.empty:
            continue

        folder_column = None

        for possible_column in (
            "run_folder",
            "candidate_run_folder",
            "run_path",
        ):
            if possible_column in working.columns:
                folder_column = possible_column
                break

        if folder_column is None:
            diagnostic_candidates.append(
                f"{source.name}: matching row found but no run-folder column"
            )
            continue

        with_folders = working[
            working[folder_column]
            .fillna("")
            .astype(str)
            .str.strip()
            .ne("")
        ].copy()

        if with_folders.empty:
            diagnostic_candidates.append(
                f"{source.name}: matching row found but run folder is blank"
            )
            continue

        recovered_row = latest_row(with_folders)
        recovered_folder = normalise_path(
            recovered_row.get(folder_column)
        )

        if recovered_folder:
            return recovered_folder, (
                f"{source.name}:{folder_column}"
            )

    details = "\n".join(diagnostic_candidates)

    raise RuntimeError(
        "The candidate run folder could not be recovered from any "
        "available source.\n"
        f"candidate_id={candidate_id}\n"
        f"parameter={parameter}\n"
        f"direction={direction}\n"
        f"candidate_value={candidate_value}\n"
        f"candidate_score={candidate_score}\n"
        f"Search diagnostics:\n{details}"
    )


workflow = V14Workflow()
optimizer = workflow.optimizer_v14

results_dir = Path(workflow.paths.results_dir)

memory_file = results_dir / "v14_optimizer_parameter_state.xlsx"
state_file = results_dir / "v14_optimizer_state.xlsx"
history_file = results_dir / "optimization_history_V14.xlsx"

for path in (
    memory_file,
    state_file,
    history_file,
):
    if not path.exists():
        raise FileNotFoundError(path)

memory = pd.read_excel(memory_file)
history = pd.read_excel(history_file)

if "pending" not in memory.columns:
    raise RuntimeError(
        "The optimizer parameter state has no 'pending' column."
    )

pending = memory[
    memory["pending"].map(as_bool)
].copy()

if pending.empty:
    print("No pending single-parameter candidate was found.")
    print("No files were changed.")
    raise SystemExit(0)

if len(pending) != 1:
    show_columns = [
        column
        for column in (
            "parameter",
            "pending_candidate_id",
            "pending_direction",
            "pending_candidate_value",
        )
        if column in pending.columns
    ]

    raise RuntimeError(
        f"Expected one pending candidate, found {len(pending)}:\n"
        f"{pending[show_columns].to_string(index=False)}"
    )

pending_row = pending.iloc[0]

candidate_id = first_text(
    pending_row,
    (
        "pending_candidate_id",
        "candidate_id",
    ),
)

parameter = first_text(
    pending_row,
    (
        "parameter",
        "pending_parameter",
    ),
)

direction = first_text(
    pending_row,
    (
        "pending_direction",
        "direction",
    ),
)

pending_candidate_value = first_number(
    pending_row,
    (
        "pending_candidate_value",
        "candidate_value",
        "new_value",
    ),
)

if not candidate_id:
    raise RuntimeError(
        "The pending optimizer row has no candidate ID."
    )

# Find all V14 history rows for this candidate.
candidate_id_column = None

for possible_column in (
    "candidate_id",
    "optimizer_candidate_id",
    "trajectory_id",
):
    if possible_column in history.columns:
        candidate_id_column = possible_column
        break

if candidate_id_column is None:
    raise RuntimeError(
        "optimization_history_V14.xlsx has no candidate-ID column."
    )

matching = history[
    history[candidate_id_column]
    .fillna("")
    .astype(str)
    .str.strip()
    .eq(candidate_id)
].copy()

if matching.empty:
    raise RuntimeError(
        f"Candidate {candidate_id} was not found in "
        "optimization_history_V14.xlsx."
    )

# Prefer candidate_evaluated, but fall back to the latest matching row.
evaluated = matching.copy()

if "phase" in evaluated.columns:
    evaluated_rows = evaluated[
        evaluated["phase"]
        .fillna("")
        .astype(str)
        .str.strip()
        .eq("candidate_evaluated")
    ]

    if not evaluated_rows.empty:
        evaluated = evaluated_rows

decision_row = latest_row(evaluated)

candidate_score = first_number(
    decision_row,
    (
        "candidate_objective",
        "effective_objective",
        "optimizer_candidate_objective",
        "qc_objective_score",
    ),
)

if candidate_score is None:
    raise RuntimeError(
        f"No candidate objective was found for {candidate_id}."
    )

if not parameter:
    parameter = first_text(
        decision_row,
        ("parameter", "optimizer_parameter"),
    )

if not direction:
    direction = first_text(
        decision_row,
        ("direction", "optimizer_direction"),
    )

candidate_value = pending_candidate_value

if candidate_value is None:
    candidate_value = first_number(
        decision_row,
        (
            "new_value",
            "optimizer_candidate_value",
        ),
    )

run_folder, folder_source = recover_run_folder(
    results_dir=results_dir,
    decision_row=decision_row,
    candidate_id=candidate_id,
    parameter=parameter,
    direction=direction,
    candidate_value=candidate_value,
    candidate_score=candidate_score,
)

run_status = first_text(
    decision_row,
    (
        "MIN3P_run_status",
        "run_status",
        "qc_run_status",
    ),
) or "success"

print("Recovered pending candidate")
print(f"  candidate_id:     {candidate_id}")
print(f"  parameter:        {parameter}")
print(f"  direction:        {direction}")
print(f"  candidate_value:  {candidate_value}")
print(f"  candidate_score:  {candidate_score}")
print(f"  run_folder:       {run_folder}")
print(f"  folder_source:    {folder_source}")
print(f"  folder_exists:    {path_exists(run_folder)}")
print(f"  run_status:       {run_status}")

if not path_exists(run_folder):
    raise RuntimeError(
        "The run folder was recovered, but it does not exist:\n"
        f"{run_folder}\n"
        "Do not finalize the candidate until the correct run folder "
        "has been confirmed."
    )

event = optimizer.observe_run(
    float(candidate_score),
    run_folder,
    scientific_ok=True,
    scientific_penalty=0.0,
    valid=True,
    diagnostics={
        "state_repair": True,
        "repair_reason": (
            "Previous optimizer.observe_run was interrupted by an "
            "Excel PermissionError after the candidate MIN3P run "
            "and objective evaluation had completed."
        ),
        "recovered_run_folder_source": folder_source,
    },
    min3p_run_status=run_status,
)

print("\nOptimizer repair result:")
print(json.dumps(event, indent=2, default=str))

memory_after = pd.read_excel(memory_file)

pending_after = memory_after[
    memory_after["pending"].map(as_bool)
]

if not pending_after.empty:
    show_columns = [
        column
        for column in (
            "parameter",
            "pending_candidate_id",
            "pending_direction",
        )
        if column in pending_after.columns
    ]

    raise RuntimeError(
        "The repair completed, but a pending candidate remains:\n"
        f"{pending_after[show_columns].to_string(index=False)}"
    )

print("\nSUCCESS")
print("The stale candidate was finalized.")
print("The protected best configuration was not reset.")