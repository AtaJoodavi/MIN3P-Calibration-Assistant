#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MIN3P AI Assistant - V10.8.6 Latest-Suggestion Strict Scientific Calibration Controller

Core fix in V10.8.4
-------------------
The controller evaluates the latest COMPLETED parameter transition, not simply
the latest row in parameter_suggestions_V10.xlsx.

Why this matters:
    In the current pipeline, a new suggestion may be generated immediately after
    a run. Then the latest suggestion is not yet run, while the latest run
    corresponds to the previous suggestion.

Example:
    suggestion row A: imr_pyrite 7.0E-5  -> 6.3E-5
    run result      : imr_pyrite 6.3E-5

    suggestion row B: imr_pyrite 6.3E-5  -> 5.67E-5
    run result      : imr_pyrite 5.67E-5

    latest suggestion row C: imr_pyrite 5.67E-5 -> 5.103E-5
    no run yet

V10.8.3 checks row C and says SUGGESTION_NOT_APPLIED.
V10.8.4 searches backwards and evaluates row B, because row B is the latest
completed transition.

Status logic:
    active   = user allows calibration
    inactive = user excludes parameter
    frozen   = AI temporarily blocks parameter after bound/repetition/tradeoff/failure

Core safety change in V10.8.5
-----------------------------
The controller no longer accepts exact_anywhere or direction_anywhere matches as
decision evidence. A calibration decision must be based on a run that occurred
after the corresponding suggestion timestamp. Older sensitivity/history matches
are diagnostic only and must not trigger ACCEPT/REJECT/FREEZE actions.

Core safety change in V10.8.6
-----------------------------
The controller evaluates ONLY the newest valid suggestion row in
parameter_suggestions_V10.xlsx. This prevents older suggestions, whose new values
are still carried by later runs, from being re-evaluated instead of the current
manual calibration step. If the newest suggestion has not been run, the controller
returns SUGGESTION_NOT_APPLIED.

Version: V10.8.6
"""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
from openpyxl import load_workbook


VERSION = "V10.8.6"

RESULTS_DIRNAME = "04_results"
REPORTS_DIRNAME = "05_reports"
INPUT_DIRNAME = "01_input"

OPTIMIZATION_HISTORY_FILE = "optimization_history.xlsx"
PARAMETER_SUGGESTIONS_FILE = "parameter_suggestions_V10.xlsx"
AGENT_CONFIG_FILE = "agent_config.xlsx"

CONTROLLER_DECISION_XLSX = "controller_decision_V10_8.xlsx"
CONTROLLER_DECISION_MD = "controller_decision_V10_8.md"

EPS = 1e-30

DEFAULT_SPECIES_WEIGHTS: Dict[str, float] = {
    "pH": 2.0,
    "so4_2": 2.0,
    "zn+2": 1.0,
    "cu+2": 1.0,
    "pb+2": 1.0,
    "cd+2": 1.0,
    "al+3": 1.0,
    "ca+2": 0.5,
}

PROTECTED_SPECIES = ["pH", "so4_2"]

MIN_REL_IMPROVEMENT = 0.01
MAX_REL_WORSENING = 0.02
TRADEOFF_TOLERANCE = 0.25
COOLDOWN_AFTER_N_CHANGES = 2
RECENT_SUGGESTION_WINDOW = 8


# =============================================================================
# Utilities
# =============================================================================

def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def norm_col(name: str) -> str:
    return (
        str(name)
        .strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
        .replace("/", "_")
        .replace("(", "")
        .replace(")", "")
    )


def normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [norm_col(c) for c in out.columns]
    return out


def safe_float(x, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if pd.isna(x):
            return default
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return default
        return v
    except Exception:
        return default


def safe_str(x, default: str = "") -> str:
    try:
        if pd.isna(x):
            return default
        return str(x)
    except Exception:
        return default


def values_close(a, b, rel_tol: float = 1e-6, abs_tol: float = 1e-30) -> bool:
    av = safe_float(a, None)
    bv = safe_float(b, None)
    if av is None or bv is None:
        return False
    return abs(av - bv) <= max(abs_tol, rel_tol * max(abs(av), abs(bv), 1.0))


def change_direction(old_value: Optional[float], new_value: Optional[float]) -> str:
    old = safe_float(old_value, None)
    new = safe_float(new_value, None)
    if old is None or new is None:
        return "unknown"
    if values_close(old, new):
        return "unchanged"
    return "increase" if new > old else "decrease"


def direction_is_compatible(
    old_value: Optional[float],
    suggested_new_value: Optional[float],
    actual_new_value: Optional[float],
) -> bool:
    suggested_dir = change_direction(old_value, suggested_new_value)
    actual_dir = change_direction(old_value, actual_new_value)
    if actual_dir == "unchanged":
        return False
    if suggested_dir in ["unknown", "unchanged"]:
        return actual_dir in ["increase", "decrease"]
    return suggested_dir == actual_dir


def find_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    cols = set(df.columns)
    for c in candidates:
        cn = norm_col(c)
        if cn in cols:
            return cn
    return None


def detect_parameter_column(df: pd.DataFrame) -> Optional[str]:
    return find_col(df, [
        "parameter",
        "param",
        "parameter_name",
        "name",
        "changed_parameter",
        "selected_parameter",
    ])


def read_excel(path: Path, label: str) -> pd.DataFrame:
    if not path.exists():
        print(f"[WARN] Missing {label}: {path}")
        return pd.DataFrame()
    try:
        return normalize_columns(pd.read_excel(path))
    except Exception as exc:
        print(f"[WARN] Could not read {label}: {path} | {exc}")
        return pd.DataFrame()


def resolve_project_file(project_dir: Path, filename: str) -> Path:
    candidates = [
        project_dir / filename,
        project_dir / RESULTS_DIRNAME / filename,
        project_dir / INPUT_DIRNAME / filename,
    ]
    for p in candidates:
        if p.exists():
            return p
    return project_dir / RESULTS_DIRNAME / filename


def resolve_agent_config(project_dir: Path) -> Path:
    candidates = [
        project_dir / INPUT_DIRNAME / AGENT_CONFIG_FILE,
        project_dir / AGENT_CONFIG_FILE,
    ]
    for p in candidates:
        if p.exists():
            return p
    return project_dir / INPUT_DIRNAME / AGENT_CONFIG_FILE


def ensure_dirs(project_dir: Path) -> None:
    (project_dir / RESULTS_DIRNAME).mkdir(parents=True, exist_ok=True)
    (project_dir / REPORTS_DIRNAME).mkdir(parents=True, exist_ok=True)


def is_success_status(status: str) -> bool:
    s = safe_str(status).lower().strip()
    return s in ["success", "success_with_retries", "ok", "normal_exit"]


def is_failed_status(status: str) -> bool:
    s = safe_str(status).lower().strip()
    return s in ["failed", "error", "crashed", "run_not_successful"]


# =============================================================================
# Data structures
# =============================================================================

@dataclass
class CompletedTransition:
    parameter: str
    old_value: Optional[float]
    suggested_new_value: Optional[float]
    suggestion_timestamp: str
    suggestion_row_index: int
    latest_run: Optional[pd.Series]
    match_type: str
    match_evidence: str


@dataclass
class ControllerDecision:
    generated_at: str
    version: str
    project_dir: str
    parameter: str
    old_value: Optional[float]
    suggested_new_value: Optional[float]
    latest_run_value: Optional[float]
    run_status: str
    latest_run_timestamp: str
    suggestion_timestamp: str
    suggestion_row_index: Optional[int]
    controller_match_type: str
    baseline_run_timestamp: str
    baseline_objective: Optional[float]
    latest_objective: Optional[float]
    objective_delta: Optional[float]
    relative_improvement: Optional[float]
    decision: str
    action: str
    update_value_to: Optional[float]
    update_status_to: Optional[str]
    reason: str
    evidence: str
    apply_updates: bool


# =============================================================================
# Agent config
# =============================================================================

def read_agent_config_parameters(config_path: Path) -> pd.DataFrame:
    if not config_path.exists():
        return pd.DataFrame(columns=["parameter", "status", "value", "min", "max", "group"])

    try:
        df = pd.read_excel(config_path, sheet_name="parameters")
    except Exception:
        return pd.DataFrame(columns=["parameter", "status", "value", "min", "max", "group"])

    df = normalize_columns(df)
    pcol = detect_parameter_column(df)
    if not pcol:
        return pd.DataFrame(columns=["parameter", "status", "value", "min", "max", "group"])

    status_col = find_col(df, ["status", "parameter_status"])
    value_col = find_col(df, ["value", "current_value", "initial_value"])
    min_col = find_col(df, ["min", "minimum", "lower_bound"])
    max_col = find_col(df, ["max", "maximum", "upper_bound"])
    group_col = find_col(df, ["group", "process_group", "parameter_group"])

    out = pd.DataFrame()
    out["parameter"] = df[pcol].astype(str).str.strip()
    out["status"] = df[status_col].astype(str).str.strip().str.lower() if status_col else "active"
    out["value"] = df[value_col].apply(lambda x: safe_float(x, None)) if value_col else None
    out["min"] = df[min_col].apply(lambda x: safe_float(x, None)) if min_col else None
    out["max"] = df[max_col].apply(lambda x: safe_float(x, None)) if max_col else None
    out["group"] = df[group_col].astype(str) if group_col else ""
    return out.drop_duplicates("parameter", keep="first")


def get_parameter_bounds(config_df: pd.DataFrame, parameter: str) -> Tuple[Optional[float], Optional[float]]:
    if config_df.empty:
        return None, None
    row = config_df[config_df["parameter"].astype(str) == str(parameter)]
    if row.empty:
        return None, None
    return safe_float(row.iloc[0].get("min"), None), safe_float(row.iloc[0].get("max"), None)


def is_at_bound(value: Optional[float], lower: Optional[float], upper: Optional[float]) -> Tuple[bool, str]:
    if value is None:
        return False, ""
    reasons = []
    if lower is not None:
        scale = max(abs(lower), 1.0)
        if value <= lower + 1e-9 * scale:
            reasons.append("at lower bound")
    if upper is not None:
        scale = max(abs(upper), 1.0)
        if value >= upper - 1e-9 * scale:
            reasons.append("at upper bound")
    return bool(reasons), "; ".join(reasons)


def update_agent_config_parameter(
    config_path: Path,
    parameter: str,
    value: Optional[float] = None,
    status: Optional[str] = None,
) -> None:
    if not config_path.exists():
        raise FileNotFoundError(f"agent_config.xlsx not found: {config_path}")

    wb = load_workbook(config_path)
    if "parameters" not in wb.sheetnames:
        raise ValueError(f"No 'parameters' sheet found in {config_path}")

    ws = wb["parameters"]
    header_row = 1

    headers = {}
    for cell in ws[header_row]:
        if cell.value is not None:
            headers[norm_col(cell.value)] = cell.column

    p_col = None
    for c in ["parameter", "param", "parameter_name", "name"]:
        if c in headers:
            p_col = headers[c]
            break
    if p_col is None:
        raise ValueError("Could not find parameter column in agent_config.xlsx")

    if "value" not in headers and value is not None:
        new_col = ws.max_column + 1
        ws.cell(header_row, new_col).value = "value"
        headers["value"] = new_col

    if "status" not in headers and status is not None:
        new_col = ws.max_column + 1
        ws.cell(header_row, new_col).value = "status"
        headers["status"] = new_col

    target_row = None
    for r in range(2, ws.max_row + 1):
        if str(ws.cell(r, p_col).value).strip() == str(parameter):
            target_row = r
            break
    if target_row is None:
        raise ValueError(f"Parameter '{parameter}' not found in agent_config.xlsx")

    if value is not None:
        ws.cell(target_row, headers["value"]).value = float(value)
    if status is not None:
        ws.cell(target_row, headers["status"]).value = str(status)

    wb.save(config_path)


# =============================================================================
# Suggestion/run matching
# =============================================================================

def prepare_history(history_df: pd.DataFrame) -> pd.DataFrame:
    if history_df.empty:
        return history_df
    work = history_df.copy()
    tcol = find_col(work, ["timestamp", "time", "date"])
    if tcol:
        work["_timestamp_sort"] = pd.to_datetime(work[tcol], errors="coerce")
        work = work.sort_values("_timestamp_sort")
    else:
        work["_timestamp_sort"] = pd.NaT
    return work.reset_index(drop=True)


def prepare_suggestions(suggestions_df: pd.DataFrame) -> pd.DataFrame:
    if suggestions_df.empty:
        return suggestions_df

    work = suggestions_df.copy()
    pcol = detect_parameter_column(work)
    if pcol and pcol != "parameter":
        work["parameter"] = work[pcol].astype(str).str.strip()

    tcol = find_col(work, ["timestamp", "time", "date"])
    if tcol:
        work["_timestamp_sort"] = pd.to_datetime(work[tcol], errors="coerce")
        work = work.sort_values("_timestamp_sort")
    else:
        work["_timestamp_sort"] = pd.NaT

    work["_suggestion_row_index"] = range(len(work))
    return work.reset_index(drop=True)


def get_suggestion_fields(row: pd.Series) -> Dict[str, object]:
    old_col = "old_value" if "old_value" in row.index else None
    new_col = "new_value" if "new_value" in row.index else None
    tcol = "timestamp" if "timestamp" in row.index else None

    return {
        "parameter": safe_str(row.get("parameter", row.get("param", ""))).strip(),
        "old_value": safe_float(row.get(old_col), None) if old_col else None,
        "new_value": safe_float(row.get(new_col), None) if new_col else None,
        "timestamp": safe_str(row.get(tcol, "")) if tcol else "",
        "row_index": int(row.get("_suggestion_row_index", -1)),
    }


def valid_run_subset(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    out = df.copy()

    if "run_status" in out.columns:
        ok = out["run_status"].apply(is_success_status)
        if ok.any():
            out = out[ok].copy()

    metric_cols = [c for c in out.columns if c.startswith("rmse_") or c.startswith("bias_")]
    if metric_cols:
        has_metric = out[metric_cols].notna().any(axis=1)
        if has_metric.any():
            out = out[has_metric].copy()

    return out


def match_runs_for_suggestion(
    history: pd.DataFrame,
    parameter: str,
    old_value: Optional[float],
    new_value: Optional[float],
    suggestion_ts: str,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Return (exact_matches, direction_matches), with debug columns.
    """
    if history.empty or parameter not in history.columns:
        return pd.DataFrame(), pd.DataFrame()

    work = valid_run_subset(history)
    if work.empty:
        work = history.copy()

    ts = pd.to_datetime(suggestion_ts, errors="coerce")
    if "_timestamp_sort" in work.columns and not pd.isna(ts):
        work["_after_suggestion"] = work["_timestamp_sort"] >= ts
    else:
        work["_after_suggestion"] = True

    work["_parameter_value"] = pd.to_numeric(work[parameter], errors="coerce")
    work["_exact_match"] = work["_parameter_value"].apply(lambda v: values_close(v, new_value))
    work["_direction_match"] = work["_parameter_value"].apply(
        lambda v: direction_is_compatible(old_value, new_value, safe_float(v, None))
    )
    work["_suggested_direction"] = change_direction(old_value, new_value)
    work["_actual_direction"] = work["_parameter_value"].apply(
        lambda v: change_direction(old_value, safe_float(v, None))
    )

    exact = work[work["_exact_match"].astype(bool)].copy()
    direction = work[work["_direction_match"].astype(bool)].copy()

    return exact, direction


def select_latest_completed_transition(
    suggestions_df: pd.DataFrame,
    history_df: pd.DataFrame,
) -> CompletedTransition:
    """
    Evaluate ONLY the newest valid suggestion row.

    V10.8.6 safety rule:
        - The controller must not search backward and evaluate older suggestions
          whose values are still present in later runs.
        - A valid decision requires a completed run AFTER the newest suggestion
          timestamp.
        - If the newest suggestion has not been applied/run, return
          SUGGESTION_NOT_APPLIED.

    This is stricter and more appropriate for the current manual workflow:
        calibration-strategy -> suggest-only -> auto max-runs 1 -> controller
    """
    suggestions = prepare_suggestions(suggestions_df)
    history = prepare_history(history_df)

    if suggestions.empty:
        return CompletedTransition(
            parameter="",
            old_value=None,
            suggested_new_value=None,
            suggestion_timestamp="",
            suggestion_row_index=-1,
            latest_run=None,
            match_type="NO_SUGGESTION",
            match_evidence="No suggestion rows available.",
        )

    # Pick the newest valid suggestion only.
    latest_sf = None
    for _, srow in suggestions.iloc[::-1].iterrows():
        sf = get_suggestion_fields(srow)
        parameter = safe_str(sf["parameter"]).strip()
        new_value = safe_float(sf["new_value"], None)
        if parameter and new_value is not None:
            latest_sf = sf
            break

    if latest_sf is None:
        latest = suggestions.iloc[-1]
        latest_sf = get_suggestion_fields(latest)

    parameter = safe_str(latest_sf["parameter"]).strip()
    old_value = safe_float(latest_sf["old_value"], None)
    new_value = safe_float(latest_sf["new_value"], None)
    suggestion_ts = safe_str(latest_sf["timestamp"])
    row_index = int(latest_sf["row_index"])

    if not parameter or new_value is None:
        latest_run = history.iloc[-1] if not history.empty else None
        return CompletedTransition(
            parameter=parameter,
            old_value=old_value,
            suggested_new_value=new_value,
            suggestion_timestamp=suggestion_ts,
            suggestion_row_index=row_index,
            latest_run=latest_run,
            match_type="SUGGESTION_NOT_APPLIED",
            match_evidence="Newest suggestion row is incomplete or invalid.",
        )

    exact, direction = match_runs_for_suggestion(
        history=history,
        parameter=parameter,
        old_value=old_value,
        new_value=new_value,
        suggestion_ts=suggestion_ts,
    )

    exact_after = exact[exact["_after_suggestion"].astype(bool)] if not exact.empty else pd.DataFrame()
    direction_after = direction[direction["_after_suggestion"].astype(bool)] if not direction.empty else pd.DataFrame()

    if not exact_after.empty:
        run = exact_after.iloc[-1]
        return CompletedTransition(
            parameter=parameter,
            old_value=old_value,
            suggested_new_value=new_value,
            suggestion_timestamp=suggestion_ts,
            suggestion_row_index=row_index,
            latest_run=run,
            match_type="exact_after_suggestion",
            match_evidence=f"matched newest suggestion row {row_index}: exact value after suggestion",
        )

    if not direction_after.empty:
        run = direction_after.iloc[-1]
        return CompletedTransition(
            parameter=parameter,
            old_value=old_value,
            suggested_new_value=new_value,
            suggestion_timestamp=suggestion_ts,
            suggestion_row_index=row_index,
            latest_run=run,
            match_type="direction_after_suggestion",
            match_evidence=f"matched newest suggestion row {row_index}: same direction after suggestion",
        )

    latest_run = history.iloc[-1] if not history.empty else None
    return CompletedTransition(
        parameter=parameter,
        old_value=old_value,
        suggested_new_value=new_value,
        suggestion_timestamp=suggestion_ts,
        suggestion_row_index=row_index,
        latest_run=latest_run,
        match_type="SUGGESTION_NOT_APPLIED",
        match_evidence=(
            "No completed run after the newest suggestion matched the suggested value/direction. "
            "V10.8.6 intentionally ignores older suggestion rows."
        ),
    )

def find_baseline_run(
    history_df: pd.DataFrame,
    parameter: str,
    old_value: Optional[float],
    latest_run: pd.Series,
) -> Optional[pd.Series]:
    if history_df.empty or latest_run is None:
        return None

    work = prepare_history(history_df)
    latest_ts = latest_run.get("_timestamp_sort")

    if latest_ts is not None and not pd.isna(latest_ts):
        before = work[work["_timestamp_sort"] < latest_ts].copy()
    else:
        before = work.iloc[:-1].copy()

    if before.empty:
        return None

    if parameter in before.columns and old_value is not None:
        mask = before[parameter].apply(lambda x: values_close(x, old_value))
        candidates = before[mask].copy()
    else:
        candidates = before.copy()

    candidates = valid_run_subset(candidates)
    if not candidates.empty:
        return candidates.iloc[-1]

    before = valid_run_subset(before)
    if not before.empty:
        return before.iloc[-1]

    return None


def recent_suggestion_count(
    suggestions_df: pd.DataFrame,
    parameter: str,
    recent_window: int = RECENT_SUGGESTION_WINDOW,
) -> int:
    if suggestions_df.empty:
        return 0
    work = prepare_suggestions(suggestions_df)
    recent = work.tail(recent_window)
    if "parameter" not in recent.columns:
        return 0
    return int((recent["parameter"].astype(str) == str(parameter)).sum())


# =============================================================================
# Objective
# =============================================================================

def metric_col(row: pd.Series, prefix: str, species_key: str) -> Optional[str]:
    if species_key == "pH":
        candidates = [f"{prefix}_ph"]
    else:
        candidates = [f"{prefix}_{species_key}"]

    for c in candidates:
        if c in row.index:
            return c
    return None


def compute_components(row: pd.Series) -> Dict[str, float]:
    components = {}

    for species, weight in DEFAULT_SPECIES_WEIGHTS.items():
        rmse_col = metric_col(row, "rmse", species)
        mean_obs_col = metric_col(row, "mean_obs", species)

        if rmse_col is None:
            continue

        rmse = safe_float(row.get(rmse_col), None)
        if rmse is None:
            continue

        if species == "pH":
            comp = abs(rmse)
        else:
            mean_obs = safe_float(row.get(mean_obs_col), None) if mean_obs_col else None
            denom = max(abs(mean_obs), EPS) if mean_obs is not None else 1.0
            comp = abs(rmse) / denom

        components[species] = weight * comp

    return components


def compute_total_objective(row: Optional[pd.Series]) -> Tuple[Optional[float], Dict[str, float]]:
    if row is None:
        return None, {}
    comps = compute_components(row)
    if not comps:
        return None, {}
    return float(sum(comps.values())), comps


def compare_protected_components(
    baseline_components: Dict[str, float],
    latest_components: Dict[str, float],
) -> Tuple[bool, List[str]]:
    warnings = []
    for sp in PROTECTED_SPECIES:
        if sp not in baseline_components or sp not in latest_components:
            continue
        base = baseline_components[sp]
        latest = latest_components[sp]
        if base <= EPS:
            continue
        rel = (latest - base) / base
        if rel > TRADEOFF_TOLERANCE:
            warnings.append(
                f"{sp} objective worsened by {rel:.1%} ({base:.6g} -> {latest:.6g})"
            )
    return bool(warnings), warnings


def runtime_risk(latest_row: pd.Series, history_df: pd.DataFrame) -> Tuple[bool, List[str]]:
    warnings = []

    failed_frac = safe_float(latest_row.get("failed_step_fraction"), 0.0) or 0.0
    # TP3: failed_step_fraction behaves like a retry ratio, not a true fraction.
    # Do not stop/freeze calibration only because of this metric.
    # Keep warning only for extreme cases.
    if failed_frac > 2:
        warnings.append(f"failed timestep fraction is high: {failed_frac:.2%}")

    cpu = safe_float(latest_row.get("cpu_time_sec"), None)
    if cpu is not None and "cpu_time_sec" in history_df.columns:
        recent_cpu = pd.to_numeric(history_df["cpu_time_sec"], errors="coerce").dropna()
        recent_cpu = recent_cpu[recent_cpu > 0]
        if len(recent_cpu) >= 5:
            med = float(recent_cpu.tail(10).median())
            if med > 0 and cpu > 2.0 * med:
                warnings.append(f"CPU time increased strongly: {cpu:.2f}s vs recent median {med:.2f}s")

    return bool(warnings), warnings


# =============================================================================
# Controller
# =============================================================================

def evaluate_latest_run_and_update_agent_state(
    project_dir: Path,
    apply_updates: bool = False,
) -> ControllerDecision:
    project_dir = Path(project_dir).resolve()
    ensure_dirs(project_dir)

    history_path = resolve_project_file(project_dir, OPTIMIZATION_HISTORY_FILE)
    suggestions_path = resolve_project_file(project_dir, PARAMETER_SUGGESTIONS_FILE)
    config_path = resolve_agent_config(project_dir)

    history = read_excel(history_path, "optimization history")
    suggestions = read_excel(suggestions_path, "parameter suggestions")
    config_df = read_agent_config_parameters(config_path)

    transition = select_latest_completed_transition(
        suggestions_df=suggestions,
        history_df=history,
    )

    parameter = transition.parameter
    old_value = transition.old_value
    new_value = transition.suggested_new_value
    latest_run = transition.latest_run

    if latest_run is None or transition.match_type in ["NO_SUGGESTION", "SUGGESTION_NOT_APPLIED"]:
        decision = ControllerDecision(
            generated_at=now_stamp(),
            version=VERSION,
            project_dir=str(project_dir),
            parameter=parameter,
            old_value=old_value,
            suggested_new_value=new_value,
            latest_run_value=None if latest_run is None else safe_float(latest_run.get(parameter), None),
            run_status="" if latest_run is None else safe_str(latest_run.get("run_status", "")),
            latest_run_timestamp="" if latest_run is None else safe_str(latest_run.get("timestamp", latest_run.get("_timestamp_sort", ""))),
            suggestion_timestamp=transition.suggestion_timestamp,
            suggestion_row_index=transition.suggestion_row_index,
            controller_match_type=transition.match_type,
            baseline_run_timestamp="",
            baseline_objective=None,
            latest_objective=None,
            objective_delta=None,
            relative_improvement=None,
            decision="SUGGESTION_NOT_APPLIED",
            action="do_nothing",
            update_value_to=None,
            update_status_to=None,
            reason="No completed parameter transition was found for the available suggestions.",
            evidence=transition.match_evidence,
            apply_updates=apply_updates,
        )
        write_controller_outputs(project_dir, decision, None, None, {}, {})
        return decision

    latest_value = safe_float(latest_run.get(parameter), None) if parameter in latest_run.index else None
    latest_status = safe_str(latest_run.get("run_status", "unknown"))
    latest_ts = safe_str(latest_run.get("timestamp", latest_run.get("_timestamp_sort", "")))

    baseline_run = find_baseline_run(
        history_df=history,
        parameter=parameter,
        old_value=old_value,
        latest_run=latest_run,
    )

    baseline_obj, baseline_components = compute_total_objective(baseline_run)
    latest_obj, latest_components = compute_total_objective(latest_run)

    baseline_ts = (
        safe_str(baseline_run.get("timestamp", baseline_run.get("_timestamp_sort", "")))
        if baseline_run is not None else ""
    )

    run_failed = is_failed_status(latest_status)
    run_risky, runtime_warnings = runtime_risk(latest_run, history)

    lower, upper = get_parameter_bounds(config_df, parameter)
    at_bound, bound_reason = is_at_bound(latest_value, lower, upper)

    repeated_count = recent_suggestion_count(suggestions, parameter)
    repeated_recently = repeated_count >= COOLDOWN_AFTER_N_CHANGES

    reason_parts = []
    evidence_parts = [
        transition.match_evidence,
        f"controller_match_type={transition.match_type}",
        f"{parameter}: old={old_value}, suggested_new_value={new_value}, latest_run_value={latest_value}",
    ]

    decision_label = ""
    action = ""
    update_value_to = None
    update_status_to = None

    if run_failed:
        decision_label = "REJECT_FAILED_RUN"
        action = "rollback_and_freeze"
        update_value_to = old_value
        update_status_to = "frozen"
        reason_parts.append("latest matched run failed or did not converge")

    elif run_risky:
        decision_label = "REJECT_RUNTIME_RISK"
        action = "rollback_and_freeze"
        update_value_to = old_value
        update_status_to = "frozen"
        reason_parts.extend(runtime_warnings)

    elif baseline_obj is None or latest_obj is None:
        decision_label = "INSUFFICIENT_METRICS"
        action = "freeze_for_manual_review"
        update_value_to = latest_value
        update_status_to = "frozen"
        reason_parts.append("could not compute objective from available metric columns")

    else:
        objective_delta = baseline_obj - latest_obj
        rel_improvement = objective_delta / max(abs(baseline_obj), EPS)
        tradeoff, tradeoff_warnings = compare_protected_components(
            baseline_components=baseline_components,
            latest_components=latest_components,
        )

        evidence_parts.append(f"baseline_objective={baseline_obj:.6g}")
        evidence_parts.append(f"latest_objective={latest_obj:.6g}")
        evidence_parts.append(f"relative_improvement={rel_improvement:.2%}")

        if rel_improvement >= MIN_REL_IMPROVEMENT and not tradeoff:
            decision_label = "ACCEPT"
            action = "keep_change"
            update_value_to = latest_value
            update_status_to = None
            reason_parts.append("total objective improved and no protected target strongly worsened")

            if at_bound:
                action = "keep_change_and_freeze_bound"
                update_status_to = "frozen"
                reason_parts.append(bound_reason)

            elif repeated_recently:
                action = "keep_change_and_freeze_cooldown"
                update_status_to = "frozen"
                reason_parts.append(f"parameter suggested {repeated_count} time(s) recently; cooldown applied")

        elif tradeoff:
            reason_parts.extend(tradeoff_warnings)

            if rel_improvement >= 2.0 * MIN_REL_IMPROVEMENT:
                decision_label = "ACCEPT_BUT_FREEZE_TRADEOFF"
                action = "keep_change_and_freeze_tradeoff"
                update_value_to = latest_value
                update_status_to = "frozen"
                reason_parts.append("total objective improved, but protected target trade-off detected")
            else:
                decision_label = "REJECT_TRADEOFF"
                action = "rollback_and_freeze_tradeoff"
                update_value_to = old_value
                update_status_to = "frozen"
                reason_parts.append("change caused unacceptable trade-off among protected targets")

        elif rel_improvement < -MAX_REL_WORSENING:
            decision_label = "REJECT_WORSE_OBJECTIVE"
            action = "rollback_and_freeze"
            update_value_to = old_value
            update_status_to = "frozen"
            reason_parts.append("total objective worsened beyond tolerance")

        else:
            decision_label = "NEUTRAL_COOLDOWN"
            action = "keep_change_and_freeze_cooldown"
            update_value_to = latest_value
            update_status_to = "frozen"
            reason_parts.append("change was neutral/small; freeze temporarily to test other parameters")

    if baseline_obj is not None and latest_obj is not None:
        obj_delta_report = baseline_obj - latest_obj
        rel_report = obj_delta_report / max(abs(baseline_obj), EPS)
    else:
        obj_delta_report = None
        rel_report = None

    if transition.match_type.endswith("timestamp_mismatch"):
        reason_parts.append(
            "matched a compatible run outside the post-suggestion timestamp window; "
            "manual testing is acceptable, but the integrated auto loop should call the controller immediately after each run"
        )

    decision = ControllerDecision(
        generated_at=now_stamp(),
        version=VERSION,
        project_dir=str(project_dir),
        parameter=parameter,
        old_value=old_value,
        suggested_new_value=new_value,
        latest_run_value=latest_value,
        run_status=latest_status,
        latest_run_timestamp=latest_ts,
        suggestion_timestamp=transition.suggestion_timestamp,
        suggestion_row_index=transition.suggestion_row_index,
        controller_match_type=transition.match_type,
        baseline_run_timestamp=baseline_ts,
        baseline_objective=baseline_obj,
        latest_objective=latest_obj,
        objective_delta=obj_delta_report,
        relative_improvement=rel_report,
        decision=decision_label,
        action=action,
        update_value_to=update_value_to,
        update_status_to=update_status_to,
        reason="; ".join([p for p in reason_parts if p]),
        evidence="; ".join([p for p in evidence_parts if p]),
        apply_updates=apply_updates,
    )

    if apply_updates:
        update_agent_config_parameter(
            config_path=config_path,
            parameter=parameter,
            value=decision.update_value_to,
            status=decision.update_status_to,
        )

    write_controller_outputs(
        project_dir=project_dir,
        decision=decision,
        latest_run=latest_run,
        baseline_run=baseline_run,
        latest_components=latest_components,
        baseline_components=baseline_components,
    )

    return decision


# =============================================================================
# Reports
# =============================================================================

def component_comparison_frame(baseline_components: Dict[str, float], latest_components: Dict[str, float]) -> pd.DataFrame:
    species = sorted(set(baseline_components) | set(latest_components))
    rows = []
    for sp in species:
        b = baseline_components.get(sp)
        l = latest_components.get(sp)
        rel = None if b is None or l is None or abs(b) <= EPS else (l - b) / b
        rows.append({
            "species": sp,
            "baseline_component": b,
            "latest_component": l,
            "delta_latest_minus_baseline": None if b is None or l is None else l - b,
            "relative_change": rel,
        })
    return pd.DataFrame(rows)


def write_controller_outputs(
    project_dir: Path,
    decision: ControllerDecision,
    latest_run: Optional[pd.Series],
    baseline_run: Optional[pd.Series],
    latest_components: Dict[str, float],
    baseline_components: Dict[str, float],
) -> None:
    results_dir = project_dir / RESULTS_DIRNAME
    reports_dir = project_dir / REPORTS_DIRNAME
    results_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    decision_xlsx = results_dir / CONTROLLER_DECISION_XLSX
    decision_md = reports_dir / CONTROLLER_DECISION_MD

    decision_df = pd.DataFrame([decision.__dict__])
    comp_df = component_comparison_frame(baseline_components, latest_components)
    latest_df = pd.DataFrame([latest_run.to_dict()]) if latest_run is not None else pd.DataFrame()
    baseline_df = pd.DataFrame([baseline_run.to_dict()]) if baseline_run is not None else pd.DataFrame()

    if decision_xlsx.exists():
        try:
            old_log = pd.read_excel(decision_xlsx, sheet_name="decision_log")
            log_df = pd.concat([old_log, decision_df], ignore_index=True)
        except Exception:
            log_df = decision_df.copy()
    else:
        log_df = decision_df.copy()

    with pd.ExcelWriter(decision_xlsx, engine="openpyxl") as writer:
        log_df.to_excel(writer, sheet_name="decision_log", index=False)
        decision_df.to_excel(writer, sheet_name="latest_decision", index=False)
        comp_df.to_excel(writer, sheet_name="component_comparison", index=False)
        latest_df.to_excel(writer, sheet_name="latest_run", index=False)
        baseline_df.to_excel(writer, sheet_name="baseline_run", index=False)

        wb = writer.book
        for ws in wb.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for col in ws.columns:
                max_len = 12
                col_letter = col[0].column_letter
                for cell in col:
                    try:
                        max_len = max(max_len, min(len(str(cell.value)), 80))
                    except Exception:
                        pass
                ws.column_dimensions[col_letter].width = max_len + 2

    lines = []
    lines.append(f"# Controller Decision {VERSION}")
    lines.append("")
    lines.append(f"Generated: {decision.generated_at}")
    lines.append(f"Project: `{decision.project_dir}`")
    lines.append("")
    lines.append("## Latest decision")
    lines.append("")
    lines.append(f"- Parameter: `{decision.parameter}`")
    lines.append(f"- Decision: **{decision.decision}**")
    lines.append(f"- Action: `{decision.action}`")
    lines.append(f"- Apply updates: `{decision.apply_updates}`")
    lines.append(f"- Controller match type: `{decision.controller_match_type}`")
    lines.append(f"- Suggestion row index: `{decision.suggestion_row_index}`")
    lines.append(f"- Suggestion timestamp: `{decision.suggestion_timestamp}`")
    lines.append(f"- Old value: `{decision.old_value}`")
    lines.append(f"- Suggested new value: `{decision.suggested_new_value}`")
    lines.append(f"- Latest run value: `{decision.latest_run_value}`")
    lines.append(f"- Update value to: `{decision.update_value_to}`")
    lines.append(f"- Update status to: `{decision.update_status_to}`")
    lines.append("")
    lines.append("## Objective comparison")
    lines.append("")
    lines.append(f"- Baseline run timestamp: `{decision.baseline_run_timestamp}`")
    lines.append(f"- Latest run timestamp: `{decision.latest_run_timestamp}`")
    lines.append(f"- Baseline objective: `{decision.baseline_objective}`")
    lines.append(f"- Latest objective: `{decision.latest_objective}`")
    lines.append(f"- Relative improvement: `{decision.relative_improvement}`")
    lines.append("")
    lines.append("## Reason")
    lines.append("")
    lines.append(decision.reason or "No reason recorded.")
    lines.append("")
    lines.append("## Evidence")
    lines.append("")
    lines.append(decision.evidence or "No evidence recorded.")
    lines.append("")
    if not comp_df.empty:
        lines.append("## Species-level objective components")
        lines.append("")
        lines.append(comp_df.to_markdown(index=False))
        lines.append("")

    decision_md.write_text("\n".join(lines), encoding="utf-8")

    print(f"[OK] Controller decision Excel: {decision_xlsx}")
    print(f"[OK] Controller decision Markdown: {decision_md}")
    print(f"[OK] Decision: {decision.decision} | Action: {decision.action}")
    print(f"[OK] Match: {decision.controller_match_type}")


# =============================================================================
# CLI
# =============================================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="V10.8.5 fresh-match scientific calibration controller")
    parser.add_argument("--project-dir", type=str, default=".", help="MIN3P calibration project directory")
    parser.add_argument("--apply", action="store_true", help="Apply accept/rollback/freeze updates to agent_config.xlsx")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate only. Same as not using --apply.")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    apply_updates = bool(args.apply) and not bool(args.dry_run)

    decision = evaluate_latest_run_and_update_agent_state(
        project_dir=Path(args.project_dir),
        apply_updates=apply_updates,
    )

    print("")
    print("Controller summary")
    print("------------------")
    print(f"version  : {decision.version}")
    print(f"parameter: {decision.parameter}")
    print(f"decision : {decision.decision}")
    print(f"action   : {decision.action}")
    print(f"match    : {decision.controller_match_type}")
    print(f"reason   : {decision.reason}")
