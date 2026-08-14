#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
MIN3P AI Assistant - V10 Deterministic Calibration Strategy Engine

Purpose
-------
Create a deterministic calibration strategy using:

Inputs expected mainly in 04_results/
1) process_sensitivity_summary_V10.xlsx
2) sensitivity_interpretation_V10.xlsx
3) parameter_importance_V10.xlsx
4) optimization_history.xlsx

Optional inputs:
5) cross_project_learning_V10.xlsx
6) cross_project_parameter_lessons.xlsx
7) agent_config.xlsx

Outputs
-------
05_reports/calibration_strategy_V10.md
04_results/calibration_strategy_V10.xlsx

Design
------
This module is deterministic. It does not call GPT.
GPT review is handled separately by gpt_supervisor.py.

Main V10.8.5 ranking:
    total_score =
        0.20 * process_sensitivity_score
      + 0.70 * parameter_importance_score
      + 0.07 * optimization_history_score
      + 0.03 * cross_project_score

V10.8.5 safety layer:
    - active parameters at min/max are kept visible but not selected for next-cycle change
    - parameters suggested repeatedly in the latest suggestions are put on cooldown
    - WATCH + do-not-change parameters are not selected for automatic change
    - pH/SO4 objective context gives a scientific boost to safer buffering parameters
    - sulfide-source changes are blocked when pH and SO4 are both underpredicted and sulfide decrease has shown trade-off risk
    - inactive parameters are excluded
    - frozen parameters are kept visible but not selected

Parameter status handling:
    active   -> used normally
    inactive -> excluded from strategy
    frozen   -> kept visible but not selected for next-cycle change

Recommended use:
    python min3p_ai_pipeline_V10.py --mode calibration-strategy --skip-gpt
"""

from __future__ import annotations

import argparse
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd


# =============================================================================
# Constants
# =============================================================================

VERSION = "V10.8.5-objective-aware-safe-selection"
ENGINE_NAME = "calibration_strategy"
EPS = 1e-12

RESULTS_DIRNAME = "04_results"
REPORTS_DIRNAME = "05_reports"
INPUT_DIRNAME = "01_input"

DEFAULT_INPUT_FILES = {
    "process_sensitivity": "process_sensitivity_summary_V10.xlsx",
    "sensitivity_interpretation": "sensitivity_interpretation_V10.xlsx",
    "parameter_importance": "parameter_importance_V10.xlsx",
    "optimization_history": "optimization_history.xlsx",
    "parameter_suggestions": "parameter_suggestions_V10.xlsx",
}

REPORT_REL = Path(REPORTS_DIRNAME) / "calibration_strategy_V10.md"
RESULT_REL = Path(RESULTS_DIRNAME) / "calibration_strategy_V10.xlsx"


# =============================================================================
# Basic utilities
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
    df = df.copy()
    df.columns = [norm_col(c) for c in df.columns]
    return df


def find_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    cols = set(df.columns)
    for c in candidates:
        cn = norm_col(c)
        if cn in cols:
            return cn
    return None


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


def normalize_01(series: pd.Series) -> pd.Series:
    s = pd.to_numeric(series, errors="coerce").fillna(0.0).abs()
    mx = s.max()
    if mx <= EPS:
        return pd.Series([0.0] * len(s), index=s.index)
    return s / mx


def ensure_dirs(project_dir: Path) -> None:
    (project_dir / RESULTS_DIRNAME).mkdir(parents=True, exist_ok=True)
    (project_dir / REPORTS_DIRNAME).mkdir(parents=True, exist_ok=True)


def safe_read_excel(path: Path, label: str) -> pd.DataFrame:
    if not path.exists():
        print(f"[WARN] Missing input: {label}: {path}")
        return pd.DataFrame()

    try:
        df = pd.read_excel(path)
        return normalize_columns(df)
    except Exception as exc:
        print(f"[WARN] Could not read {label}: {path} | {exc}")
        return pd.DataFrame()


def read_excel_sheet(path: Path, sheet_name: str, label: str) -> pd.DataFrame:
    if not path.exists():
        print(f"[WARN] Missing input: {label}: {path}")
        return pd.DataFrame()

    try:
        df = pd.read_excel(path, sheet_name=sheet_name)
        return normalize_columns(df)
    except Exception as exc:
        print(f"[WARN] Could not read {label}: {path} | {exc}")
        return pd.DataFrame()


def most_common_text(values) -> str:
    clean = [
        str(v).strip()
        for v in values
        if str(v).strip() and str(v).strip().lower() not in ["nan", "none"]
    ]
    if not clean:
        return "unknown"
    return pd.Series(clean).value_counts().index[0]


def join_unique(values, limit: int = 5) -> str:
    clean = []
    for v in values:
        s = safe_str(v).strip()
        if not s or s.lower() in ["nan", "none"]:
            continue
        if s not in clean:
            clean.append(s)
    return " | ".join(clean[:limit])


# =============================================================================
# Path resolution
# =============================================================================

def first_existing(paths: List[Path]) -> Optional[Path]:
    for p in paths:
        if p.exists():
            return p
    return None


def resolve_input_path(project_dir: Path, filename: str) -> Path:
    """
    Search order:
    1) project root
    2) 04_results
    3) 01_input
    """
    candidates = [
        project_dir / filename,
        project_dir / RESULTS_DIRNAME / filename,
        project_dir / INPUT_DIRNAME / filename,
    ]
    return first_existing(candidates) or candidates[1]


def resolve_agent_config_path(project_dir: Path) -> Optional[Path]:
    candidates = [
        project_dir / "agent_config.xlsx",
        project_dir / INPUT_DIRNAME / "agent_config.xlsx",
        project_dir / "01_input" / "agent_config.xlsx",
    ]
    return first_existing(candidates)


# =============================================================================
# Column detection
# =============================================================================

def detect_parameter_column(df: pd.DataFrame) -> Optional[str]:
    return find_col(df, [
        "parameter",
        "param",
        "parameter_name",
        "name",
        "changed_parameter",
        "selected_parameter",
    ])


def detect_species_column(df: pd.DataFrame) -> Optional[str]:
    return find_col(df, [
        "species",
        "component",
        "target",
        "objective",
        "observed_variable",
        "variable",
        "output",
    ])


def detect_direction_column(df: pd.DataFrame) -> Optional[str]:
    return find_col(df, [
        "direction",
        "effect_direction",
        "sensitivity_direction",
        "increase_effect",
        "sign",
        "trend",
    ])


def detect_score_column(df: pd.DataFrame, preferred: List[str]) -> Optional[str]:
    return find_col(df, preferred + [
        "score",
        "importance",
        "importance_score",
        "sensitivity",
        "sensitivity_score",
        "abs_sensitivity",
        "absolute_sensitivity",
        "normalized_sensitivity",
        "abs_normalized_sensitivity",
        "absolute_normalized_sensitivity",
        "normalized_process_sensitivity",
        "abs_normalized_process_sensitivity",
        "absolute_normalized_process_sensitivity",
        "percent_model_change",
        "rank_score",
        "coefficient",
        "slope",
        "effect",
        "abs_effect",
        "absolute_effect",
    ])


def extract_metric_columns(df: pd.DataFrame) -> List[str]:
    metric_keys = ["rmse", "mae", "error", "objective", "score", "loss"]
    out = []
    for c in df.columns:
        cl = c.lower()
        if any(k in cl for k in metric_keys):
            if pd.api.types.is_numeric_dtype(df[c]):
                out.append(c)
    return out


# =============================================================================
# Agent config reading
# =============================================================================

def read_parameter_status(project_dir: Path) -> pd.DataFrame:
    """
    Reads agent_config.xlsx / parameters sheet if available.

    Returns:
        parameter, config_status, config_value, config_min, config_max, config_group
    """
    config_path = resolve_agent_config_path(project_dir)

    if config_path is None:
        return pd.DataFrame(columns=[
            "parameter",
            "config_status",
            "config_value",
            "config_min",
            "config_max",
        "recent_change_count",
            "config_group",
        ])

    params = read_excel_sheet(config_path, "parameters", "agent_config parameters")

    if params.empty:
        return pd.DataFrame(columns=[
            "parameter",
            "config_status",
            "config_value",
            "config_min",
            "config_max",
        "recent_change_count",
            "config_group",
        ])

    pcol = detect_parameter_column(params)
    if not pcol:
        return pd.DataFrame(columns=[
            "parameter",
            "config_status",
            "config_value",
            "config_min",
            "config_max",
        "recent_change_count",
            "config_group",
        ])

    status_col = find_col(params, ["status", "active", "parameter_status"])
    value_col = find_col(params, ["value", "current_value", "initial_value"])
    min_col = find_col(params, ["min", "minimum", "lower_bound"])
    max_col = find_col(params, ["max", "maximum", "upper_bound"])
    group_col = find_col(params, ["group", "process_group", "parameter_group"])

    out = pd.DataFrame()
    out["parameter"] = params[pcol].astype(str).str.strip()
    out["config_status"] = (
        params[status_col].astype(str).str.lower().str.strip()
        if status_col else "active"
    )
    out["config_value"] = params[value_col].apply(lambda x: safe_float(x, None)) if value_col else None
    out["config_min"] = params[min_col].apply(lambda x: safe_float(x, None)) if min_col else None
    out["config_max"] = params[max_col].apply(lambda x: safe_float(x, None)) if max_col else None
    out["config_group"] = params[group_col].astype(str) if group_col else ""

    out = out.dropna(subset=["parameter"])
    out = out[out["parameter"].astype(str).str.strip() != ""]
    return out.drop_duplicates("parameter", keep="first")


# =============================================================================
# Evidence builders
# =============================================================================

def build_process_evidence(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=[
            "parameter",
            "process_group",
            "sensitivity_score",
            "direction",
            "process_evidence",
        ])

    pcol = detect_parameter_column(df)
    if not pcol:
        return pd.DataFrame(columns=[
            "parameter",
            "process_group",
            "sensitivity_score",
            "direction",
            "process_evidence",
        ])

    scol = detect_score_column(df, [
        "process_sensitivity_score",
        "sensitivity_score",
        "abs_normalized_process_sensitivity",
        "absolute_normalized_process_sensitivity",
        "normalized_process_sensitivity",
        "abs_normalized_sensitivity",
        "absolute_normalized_sensitivity",
        "normalized_sensitivity",
        "abs_effect",
        "absolute_effect",
        "normalized_effect",
        "percent_model_change",
    ])

    dcol = detect_direction_column(df)
    gcol = find_col(df, [
        "process",
        "process_group",
        "reaction",
        "mineral_group",
        "control",
        "parameter_group",
    ])
    ecol = find_col(df, [
        "interpretation",
        "reason",
        "evidence",
        "summary",
        "comment",
        "process_reason",
    ])

    out = pd.DataFrame()
    out["parameter"] = df[pcol].astype(str).str.strip()
    out["process_group"] = df[gcol].astype(str) if gcol else "unknown"
    out["sensitivity_score_raw"] = df[scol].apply(safe_float) if scol else 0.0
    out["sensitivity_score"] = normalize_01(out["sensitivity_score_raw"])
    out["direction"] = df[dcol].astype(str) if dcol else "unknown"
    out["process_evidence"] = df[ecol].astype(str) if ecol else ""

    return out.groupby("parameter", as_index=False).agg({
        "process_group": lambda x: "; ".join(sorted(set([
            str(v) for v in x
            if str(v).strip().lower() not in ["nan", "none", ""]
        ]))) or "unknown",
        "sensitivity_score": "max",
        "direction": lambda x: most_common_text(x),
        "process_evidence": lambda x: join_unique(x, limit=4),
    })


def build_interpretation_evidence(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=[
            "parameter",
            "interpretation",
            "species_targets",
            "expected_effect",
        ])

    pcol = detect_parameter_column(df)
    if not pcol:
        return pd.DataFrame(columns=[
            "parameter",
            "interpretation",
            "species_targets",
            "expected_effect",
        ])

    spcol = detect_species_column(df)
    icol = find_col(df, [
        "interpretation",
        "scientific_interpretation",
        "comment",
        "reason",
        "diagnosis",
    ])
    ecol = find_col(df, [
        "expected_effect",
        "effect",
        "model_response",
        "influence",
        "direction",
    ])

    out = pd.DataFrame()
    out["parameter"] = df[pcol].astype(str).str.strip()
    out["species_targets"] = df[spcol].astype(str) if spcol else ""
    out["interpretation"] = df[icol].astype(str) if icol else ""
    out["expected_effect"] = df[ecol].astype(str) if ecol else ""

    return out.groupby("parameter", as_index=False).agg({
        "species_targets": lambda x: join_unique(x, limit=8),
        "interpretation": lambda x: join_unique(x, limit=5),
        "expected_effect": lambda x: join_unique(x, limit=5),
    })


def build_importance_evidence(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=[
            "parameter",
            "importance_score",
            "importance_rank",
            "importance_evidence",
        ])

    pcol = detect_parameter_column(df)
    if not pcol:
        return pd.DataFrame(columns=[
            "parameter",
            "importance_score",
            "importance_rank",
            "importance_evidence",
        ])

    scol = detect_score_column(df, [
        "parameter_importance",
        "importance_score",
        "total_importance",
        "global_importance",
        "normalized_importance",
    ])

    rcol = find_col(df, [
        "rank",
        "importance_rank",
        "parameter_rank",
    ])

    ecol = find_col(df, [
        "evidence",
        "reason",
        "interpretation",
        "comment",
    ])

    out = pd.DataFrame()
    out["parameter"] = df[pcol].astype(str).str.strip()
    out["importance_score_raw"] = df[scol].apply(safe_float) if scol else 0.0
    out["importance_score"] = normalize_01(out["importance_score_raw"])
    out["importance_rank"] = df[rcol].apply(safe_float) if rcol else 9999
    out["importance_evidence"] = df[ecol].astype(str) if ecol else ""

    return out.groupby("parameter", as_index=False).agg({
        "importance_score": "max",
        "importance_rank": "min",
        "importance_evidence": lambda x: join_unique(x, limit=4),
    })


def build_history_evidence(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=[
            "parameter",
            "history_score",
            "history_direction",
            "history_evidence",
        ])

    pcol = detect_parameter_column(df)

    if not pcol:
        # Some optimization_history files store parameters as columns instead of rows.
        # If so, no safe row-wise parameter history can be inferred here.
        return pd.DataFrame(columns=[
            "parameter",
            "history_score",
            "history_direction",
            "history_evidence",
        ])

    old_col = find_col(df, ["old_value", "previous_value", "before"])
    new_col = find_col(df, ["new_value", "updated_value", "after"])
    factor_col = find_col(df, ["factor_applied", "factor_requested", "factor"])
    reason_col = find_col(df, ["reason", "evidence", "comment", "gpt_reason"])
    metric_cols = extract_metric_columns(df)

    work = df.copy()
    work["parameter"] = work[pcol].astype(str).str.strip()

    if metric_cols:
        metric = pd.to_numeric(work[metric_cols[0]], errors="coerce")
        metric_norm = normalize_01(metric)
        work["_metric_quality"] = 1.0 - metric_norm
    else:
        work["_metric_quality"] = 0.5

    if old_col and new_col:
        old = pd.to_numeric(work[old_col], errors="coerce")
        new = pd.to_numeric(work[new_col], errors="coerce")
        rel = ((new - old).abs() / old.abs().clip(lower=EPS)).fillna(0.0)
        work["_movement"] = rel.clip(upper=10.0)
    elif factor_col:
        fac = pd.to_numeric(work[factor_col], errors="coerce").fillna(1.0)
        work["_movement"] = (fac - 1.0).abs().clip(upper=10.0)
    else:
        work["_movement"] = 0.1

    work["_history_raw"] = work["_metric_quality"] * (1.0 + work["_movement"])

    def infer_dir(g: pd.DataFrame) -> str:
        if old_col and new_col:
            old = pd.to_numeric(g[old_col], errors="coerce")
            new = pd.to_numeric(g[new_col], errors="coerce")
            delta = (new - old).dropna()
            if len(delta) == 0:
                return "unknown"
            if delta.median() > 0:
                return "increase"
            if delta.median() < 0:
                return "decrease"

        if factor_col:
            fac = pd.to_numeric(g[factor_col], errors="coerce").dropna()
            if len(fac) == 0:
                return "unknown"
            if fac.median() > 1:
                return "increase"
            if fac.median() < 1:
                return "decrease"

        return "unknown"

    out = work.groupby("parameter").apply(
        lambda g: pd.Series({
            "history_score_raw": g["_history_raw"].max(),
            "history_direction": infer_dir(g),
            "history_evidence": (
                join_unique(g[reason_col], limit=4)
                if reason_col else
                f"Used in {len(g)} previous optimization-history rows."
            ),
        }),
        include_groups=False,
    ).reset_index()

    out["history_score"] = normalize_01(out["history_score_raw"])
    return out[["parameter", "history_score", "history_direction", "history_evidence"]]



def infer_change_direction_from_values(old_value, new_value) -> str:
    old = safe_float(old_value, None)
    new = safe_float(new_value, None)

    if old is None or new is None:
        return "unknown"
    if new > old:
        return "increase"
    if new < old:
        return "decrease"
    return "unchanged"


def build_suggestion_cooldown(
    df: pd.DataFrame,
    recent_window: int = 8,
    cooldown_after: int = 2,
) -> pd.DataFrame:
    """
    Build a short-memory cooldown table from parameter_suggestions_V10.xlsx.

    This prevents the strategy from repeatedly selecting the same parameter
    after it has already been changed several times.

    Typical examples:
        top_flux was changed down several times and reached min -> cooldown / bound block
        usr_pyrite was changed down repeatedly -> cooldown, avoid repeated same-direction change
    """
    empty_cols = [
        "parameter",
        "recent_change_count",
        "recent_direction",
        "last_suggestion_time",
        "suggestion_cooldown",
        "suggestion_history_evidence",
    ]

    if df.empty:
        return pd.DataFrame(columns=empty_cols)

    pcol = detect_parameter_column(df)
    if not pcol:
        return pd.DataFrame(columns=empty_cols)

    work = df.copy()
    work["parameter"] = work[pcol].astype(str).str.strip()

    tcol = find_col(work, ["timestamp", "time", "date"])
    if tcol:
        work["_timestamp_sort"] = pd.to_datetime(work[tcol], errors="coerce")
        work = work.sort_values("_timestamp_sort")
    else:
        work["_timestamp_sort"] = pd.NaT

    # Use only recent rows. Old V9/V10 experiments should not permanently suppress parameters.
    recent = work.tail(recent_window).copy()

    old_col = find_col(recent, ["old_value", "previous_value", "before"])
    new_col = find_col(recent, ["new_value", "updated_value", "after"])
    factor_col = find_col(recent, ["factor_applied", "factor_requested", "factor"])

    if old_col and new_col:
        recent["_recent_direction"] = [
            infer_change_direction_from_values(o, n)
            for o, n in zip(recent[old_col], recent[new_col])
        ]
    elif factor_col:
        def _direction_from_factor(x):
            f = safe_float(x, 1.0) or 1.0
            if f > 1.0:
                return "increase"
            if f < 1.0:
                return "decrease"
            return "unchanged"

        recent["_recent_direction"] = recent[factor_col].apply(_direction_from_factor)
    else:
        recent["_recent_direction"] = "unknown"

    def _last_time(g: pd.DataFrame) -> str:
        if tcol and tcol in g.columns:
            vals = [safe_str(v) for v in g[tcol].tolist() if safe_str(v)]
            return vals[-1] if vals else ""
        return ""

    def _last_direction(g: pd.DataFrame) -> str:
        vals = [
            safe_str(v)
            for v in g["_recent_direction"].tolist()
            if safe_str(v) and safe_str(v) not in ["unchanged", "unknown"]
        ]
        return vals[-1] if vals else "unknown"

    grouped = recent.groupby("parameter").apply(
        lambda g: pd.Series({
            "recent_change_count": len(g),
            "recent_direction": _last_direction(g),
            "last_suggestion_time": _last_time(g),
        }),
        include_groups=False,
    ).reset_index()

    grouped["suggestion_cooldown"] = grouped["recent_change_count"] >= cooldown_after
    grouped["suggestion_history_evidence"] = grouped.apply(
        lambda r: (
            f"parameter suggested {int(r['recent_change_count'])} time(s) "
            f"in the last {recent_window} suggestion rows; "
            f"last direction={r['recent_direction']}; "
            f"last suggestion={r['last_suggestion_time']}"
        ),
        axis=1,
    )

    return grouped[empty_cols]

def load_cross_project_learning(project_dir: Path) -> pd.DataFrame:
    candidates = [
        project_dir / RESULTS_DIRNAME / "cross_project_learning_V10.xlsx",
        project_dir / RESULTS_DIRNAME / "cross_project_parameter_lessons.xlsx",
        project_dir / "03_expert_knowledge" / "lessons" / "cross_project_parameter_lessons.xlsx",
    ]

    frames = []
    for path in candidates:
        if path.exists():
            df = safe_read_excel(path, f"cross-project learning: {path.name}")
            if not df.empty:
                frames.append(df)

    if not frames:
        return pd.DataFrame(columns=[
            "parameter",
            "cross_project_score",
            "cross_project_evidence",
        ])

    df = pd.concat(frames, ignore_index=True)
    pcol = detect_parameter_column(df)

    if not pcol:
        return pd.DataFrame(columns=[
            "parameter",
            "cross_project_score",
            "cross_project_evidence",
        ])

    scol = detect_score_column(df, [
        "cross_project_score",
        "similarity_score",
        "confidence",
        "lesson_confidence",
    ])

    lcol = find_col(df, [
        "lesson",
        "evidence",
        "reason",
        "interpretation",
        "similar_project",
    ])

    out = pd.DataFrame()
    out["parameter"] = df[pcol].astype(str).str.strip()
    out["cross_project_score_raw"] = df[scol].apply(safe_float) if scol else 0.0
    out["cross_project_score"] = normalize_01(out["cross_project_score_raw"])
    out["cross_project_evidence"] = df[lcol].astype(str) if lcol else ""

    return out.groupby("parameter", as_index=False).agg({
        "cross_project_score": "max",
        "cross_project_evidence": lambda x: join_unique(x, limit=5),
    })



# =============================================================================
# Objective-context guardrails (V10.8.5)
# =============================================================================

def latest_valid_history_row(history_df: pd.DataFrame) -> Optional[pd.Series]:
    """
    Return the latest successful/retried row with usable metric columns.
    """
    if history_df.empty:
        return None

    work = history_df.copy()
    tcol = find_col(work, ["timestamp", "time", "date"])
    if tcol:
        work["_timestamp_sort"] = pd.to_datetime(work[tcol], errors="coerce")
        work = work.sort_values("_timestamp_sort")

    status_col = find_col(work, ["run_status", "status"])
    if status_col:
        ok = work[status_col].astype(str).str.lower().str.strip().isin([
            "success", "success_with_retries", "ok", "normal_exit"
        ])
        if ok.any():
            work = work[ok].copy()

    metric_cols = [c for c in work.columns if c.startswith("rmse_") or c.startswith("bias_")]
    if metric_cols:
        has_metric = work[metric_cols].notna().any(axis=1)
        if has_metric.any():
            work = work[has_metric].copy()

    if work.empty:
        return None

    return work.iloc[-1]


def objective_context_from_history(history_df: pd.DataFrame) -> Dict[str, object]:
    """
    Diagnose the latest objective context from optimization_history.xlsx.

    Convention used by compare_results:
        Bias_pH    = mean(model - obs), so negative means model pH is too low.
        Bias_so4-2 = mean(model - obs), so negative means SO4 is underpredicted.
    """
    row = latest_valid_history_row(history_df)
    if row is None:
        return {
            "has_context": False,
            "bias_pH": None,
            "bias_so4_2": None,
            "pH_underpredicted": False,
            "so4_underpredicted": False,
            "context_text": "No valid latest optimization-history row was available.",
        }

    bias_pH = safe_float(row.get("bias_ph"), None)
    bias_so4 = safe_float(row.get("bias_so4_2"), None)

    pH_under = bias_pH is not None and bias_pH < -0.05
    so4_under = bias_so4 is not None and bias_so4 < 0.0

    return {
        "has_context": True,
        "bias_pH": bias_pH,
        "bias_so4_2": bias_so4,
        "pH_underpredicted": pH_under,
        "so4_underpredicted": so4_under,
        "context_text": (
            f"latest Bias_pH={bias_pH}; latest Bias_so4-2={bias_so4}; "
            f"pH_underpredicted={pH_under}; so4_underpredicted={so4_under}"
        ),
    }


def parameter_name_contains(parameter: str, keys: List[str]) -> bool:
    p = safe_str(parameter).lower()
    return any(k.lower() in p for k in keys)


def add_objective_context_to_evidence(
    evidence_df: pd.DataFrame,
    history_raw: pd.DataFrame,
) -> pd.DataFrame:
    """
    Add V10.8.5 objective-context scoring and scientific guardrails.

    Main rule for the current HCT calibration pattern:
        if pH is underpredicted and SO4 is also underpredicted,
        do not continue decreasing sulfide-source controls just to improve pH.
        Prefer buffering / pH boundary / carbonate parameters instead.
    """
    if evidence_df.empty:
        return evidence_df

    ctx = objective_context_from_history(history_raw)
    out = evidence_df.copy()

    out["objective_context_score"] = 0.0
    out["objective_context_reason"] = ""
    out["scientific_guard_block"] = False
    out["scientific_guard_reason"] = ""
    out["objective_recommended_direction"] = ""

    pH_under = bool(ctx.get("pH_underpredicted", False))
    so4_under = bool(ctx.get("so4_underpredicted", False))
    context_text = safe_str(ctx.get("context_text", ""))

    for idx, row in out.iterrows():
        parameter = safe_str(row.get("parameter", ""))
        group = safe_str(row.get("process_group", "")).lower()
        targets = safe_str(row.get("species_targets", "")).lower()
        score = 0.0
        reasons = []
        guard_reasons = []
        direction = ""

        is_buffering = (
            "buffer" in group
            or parameter_name_contains(parameter, ["calcite", "magnesite", "carbonate"])
        )
        is_boundary_pH = parameter.lower() in ["bc_top_ph", "init_ph"] or "ph" in parameter.lower() and "bc" in parameter.lower()
        is_calcite_inventory = parameter.lower() in ["phi_calcite", "phi_magnesite"]
        is_sulfide = "sulfide" in group or parameter_name_contains(
            parameter,
            ["pyrite", "pyrrhot", "chalcopy", "galena", "sphalerite"]
        )
        is_metal_only = (
            "metal" in group
            or parameter_name_contains(parameter, ["sphaler", "galena", "ferrihydrite"])
        ) and ("ph" not in targets and "so4" not in targets)

        if pH_under and so4_under:
            if is_buffering:
                score += 0.20
                direction = "increase"
                reasons.append("pH and SO4 are underpredicted; prefer carbonate/buffering adjustment over further sulfide-source reduction")
            elif is_boundary_pH:
                score += 0.18
                direction = "increase"
                reasons.append("pH is underpredicted; boundary/initial pH can adjust pH without directly reducing sulfate source")
            elif is_calcite_inventory:
                score += 0.16
                direction = "increase"
                reasons.append("pH is underpredicted; carbonate mineral inventory is safer than sulfide-source reduction")

            if is_sulfide and ("ph" in targets or "so4" in targets or parameter_name_contains(parameter, ["pyrite", "pyrrhot"])):
                out.at[idx, "scientific_guard_block"] = True
                guard_reasons.append(
                    "pH and SO4 are both underpredicted; further sulfide-source calibration is trade-off-prone and must not be auto-selected now"
                )

            if is_metal_only:
                score -= 0.08
                reasons.append("metal-only parameter is de-prioritized while protected pH/SO4 mismatch dominates")

        # Keep pH-safe parameters from being buried when all sensitivity scores are zero.
        if score != 0:
            out.at[idx, "objective_context_score"] = score
            out.at[idx, "objective_context_reason"] = "; ".join(reasons + ([context_text] if context_text else []))
        elif context_text:
            out.at[idx, "objective_context_reason"] = context_text

        if direction:
            out.at[idx, "objective_recommended_direction"] = direction

        if guard_reasons:
            out.at[idx, "scientific_guard_reason"] = "; ".join(guard_reasons)

    return out

# =============================================================================
# Strategy logic
# =============================================================================

def merge_evidence(
    process_df: pd.DataFrame,
    interp_df: pd.DataFrame,
    importance_df: pd.DataFrame,
    history_df: pd.DataFrame,
    cross_df: pd.DataFrame,
    config_df: pd.DataFrame,
    cooldown_df: pd.DataFrame,
) -> pd.DataFrame:
    params = set()

    for df in [process_df, interp_df, importance_df, history_df, cross_df, config_df, cooldown_df]:
        if not df.empty and "parameter" in df.columns:
            params.update([
                p for p in df["parameter"].astype(str).str.strip()
                if p and p.lower() not in ["nan", "none"]
            ])

    base = pd.DataFrame({"parameter": sorted(params)})

    for df in [process_df, interp_df, importance_df, history_df, cross_df, config_df, cooldown_df]:
        if not df.empty and "parameter" in df.columns:
            base = base.merge(df, on="parameter", how="left")

    numeric_cols = [
        "sensitivity_score",
        "importance_score",
        "history_score",
        "cross_project_score",
        "importance_rank",
        "config_value",
        "config_min",
        "config_max",
        "recent_change_count",
        "objective_context_score",
    ]

    for c in numeric_cols:
        if c in base.columns:
            base[c] = pd.to_numeric(base[c], errors="coerce")
        else:
            base[c] = 0.0

    for c in ["sensitivity_score", "importance_score", "history_score", "cross_project_score"]:
        base[c] = base[c].fillna(0.0)

    text_cols = [
        "process_group",
        "direction",
        "process_evidence",
        "interpretation",
        "species_targets",
        "expected_effect",
        "history_direction",
        "history_evidence",
        "importance_evidence",
        "cross_project_evidence",
        "config_status",
        "config_group",
        "recent_direction",
        "last_suggestion_time",
        "suggestion_history_evidence",
        "objective_context_reason",
        "scientific_guard_reason",
        "objective_recommended_direction",
    ]

    for c in text_cols:
        if c not in base.columns:
            base[c] = ""
        base[c] = base[c].fillna("").astype(str)

    base["config_status"] = base["config_status"].replace("", "unknown").str.lower().str.strip()

    # Prefer group from agent_config if process_group is unknown.
    mask = base["process_group"].isin(["", "unknown", "nan", "none"])
    base.loc[mask, "process_group"] = base.loc[mask, "config_group"]

    base["process_group"] = base["process_group"].replace("", "unknown")

    if "suggestion_cooldown" not in base.columns:
        base["suggestion_cooldown"] = False
    base["suggestion_cooldown"] = (
        base["suggestion_cooldown"]
        .fillna(False)
        .astype(str)
        .str.lower()
        .isin(["true", "1", "yes", "y"])
    )

    if "scientific_guard_block" not in base.columns:
        base["scientific_guard_block"] = False
    base["scientific_guard_block"] = (
        base["scientific_guard_block"]
        .fillna(False)
        .astype(str)
        .str.lower()
        .isin(["true", "1", "yes", "y"])
    )

    return base


def filter_parameter_status(df: pd.DataFrame) -> pd.DataFrame:
    """
    Remove inactive parameters from strategy.
    Frozen parameters remain visible but cannot be next-cycle changes.
    """
    if df.empty:
        return df

    out = df.copy()

    inactive_mask = out["config_status"].isin([
        "inactive",
        "no",
        "false",
        "0",
        "off",
        "disabled",
        "exclude",
    ])

    out = out[~inactive_mask].copy()

    out["is_frozen"] = out["config_status"].isin([
        "frozen",
        "temporary_frozen",
        "temp_frozen",
    ])

    return out



def annotate_selection_blocks(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add automatic blocking flags for parameters that should remain visible but
    should not be selected for the immediate next calibration cycle.

    Block types:
        frozen          -> user/agent set status=frozen
        lower_bound     -> config_value is at config_min
        upper_bound     -> config_value is at config_max
        cooldown        -> repeated recent suggestions
    """
    if df.empty:
        return df

    out = df.copy()

    for c in ["config_value", "config_min", "config_max"]:
        if c not in out.columns:
            out[c] = None
        out[c] = pd.to_numeric(out[c], errors="coerce")

    def _close_to_lower(row):
        v = row.get("config_value")
        mn = row.get("config_min")
        if pd.isna(v) or pd.isna(mn):
            return False
        scale = max(abs(float(mn)), 1.0)
        return float(v) <= float(mn) + 1e-9 * scale

    def _close_to_upper(row):
        v = row.get("config_value")
        mx = row.get("config_max")
        if pd.isna(v) or pd.isna(mx):
            return False
        scale = max(abs(float(mx)), 1.0)
        return float(v) >= float(mx) - 1e-9 * scale

    out["at_lower_bound"] = out.apply(_close_to_lower, axis=1)
    out["at_upper_bound"] = out.apply(_close_to_upper, axis=1)
    out["at_any_bound"] = out["at_lower_bound"] | out["at_upper_bound"]

    if "suggestion_cooldown" not in out.columns:
        out["suggestion_cooldown"] = False

    if "is_frozen" not in out.columns:
        out["is_frozen"] = False
    if "scientific_guard_block" not in out.columns:
        out["scientific_guard_block"] = False

    out["is_selection_blocked"] = (
        out["is_frozen"].astype(bool)
        | out["at_any_bound"].astype(bool)
        | out["suggestion_cooldown"].astype(bool)
        | out["scientific_guard_block"].astype(bool)
    )

    def _block_reason(row):
        reasons = []
        if bool(row.get("is_frozen", False)):
            reasons.append("frozen in agent_config")
        if bool(row.get("at_lower_bound", False)):
            reasons.append("at lower bound")
        if bool(row.get("at_upper_bound", False)):
            reasons.append("at upper bound")
        if bool(row.get("suggestion_cooldown", False)):
            reasons.append("recent repeated suggestions")
        if bool(row.get("scientific_guard_block", False)):
            txt = safe_str(row.get("scientific_guard_reason", "")).strip()
            reasons.append(txt if txt else "blocked by scientific guard")
        return "; ".join(reasons)

    out["selection_block_reason"] = out.apply(_block_reason, axis=1)
    return out

def compute_strategy(
    evidence: pd.DataFrame,
    max_active_parameters: int = 5,
    max_changes_per_cycle: int = 1,
) -> pd.DataFrame:
    if evidence.empty:
        return pd.DataFrame()

    df = filter_parameter_status(evidence)

    if df.empty:
        return pd.DataFrame()

    df = annotate_selection_blocks(df)

    # V10.8.5 score: base evidence + objective-context adjustment.
    # This prevents a WATCH metal parameter from being selected only because all
    # sensitivity/history scores are zero, while pH/SO4 protected targets remain unresolved.
    df["base_score"] = (
        0.20 * df["sensitivity_score"]
        + 0.70 * df["importance_score"]
        + 0.07 * df["history_score"]
        + 0.03 * df["cross_project_score"]
    )
    if "objective_context_score" not in df.columns:
        df["objective_context_score"] = 0.0
    df["objective_context_score"] = pd.to_numeric(df["objective_context_score"], errors="coerce").fillna(0.0)
    df["total_score"] = (df["base_score"] + df["objective_context_score"]).clip(lower=0.0, upper=1.0)

    df["priority"] = df["total_score"].apply(priority_from_score)
    df["recommended_direction"] = df.apply(resolve_direction, axis=1)

    # If objective context gives a clear safer direction, use it.
    if "objective_recommended_direction" in df.columns:
        mask_obj_dir = df["objective_recommended_direction"].astype(str).str.strip() != ""
        df.loc[mask_obj_dir, "recommended_direction"] = df.loc[mask_obj_dir, "objective_recommended_direction"]

    df["recommended_action"] = df.apply(
        lambda r: action_from_priority_and_direction(
            priority=r["priority"],
            direction=r["recommended_direction"],
            is_frozen=bool(r.get("is_frozen", False)),
            selection_block_reason=safe_str(r.get("selection_block_reason", "")),
        ),
        axis=1,
    )

    df["strategy_status"] = "candidate"

    selectable = df[~df["is_selection_blocked"]].copy()
    selected = selectable.sort_values(
        ["total_score", "objective_context_score", "sensitivity_score", "importance_score", "history_score"],
        ascending=False,
    ).head(max_active_parameters).index

    df.loc[selected, "strategy_status"] = "active"
    df.loc[df["is_frozen"], "strategy_status"] = "frozen"
    df.loc[df["at_lower_bound"] | df["at_upper_bound"], "strategy_status"] = "bound_blocked"
    df.loc[df["suggestion_cooldown"], "strategy_status"] = "cooldown"
    if "scientific_guard_block" in df.columns:
        df.loc[df["scientific_guard_block"].astype(bool), "strategy_status"] = "scientific_guard"

    df["next_cycle_change"] = "no"

    # Automatic changes are allowed only for non-blocked active parameters with
    # LOW/MEDIUM/HIGH priority. WATCH parameters must not be marked yes with
    # action='do not change now'.
    df["auto_action_allowed"] = (
        (df["strategy_status"] == "active")
        & (~df["is_selection_blocked"])
        & (df["priority"].isin(["HIGH", "MEDIUM", "LOW"]))
    )

    change_idx = df[df["auto_action_allowed"]].sort_values(
        ["total_score", "objective_context_score", "sensitivity_score", "importance_score"],
        ascending=False,
    ).head(max_changes_per_cycle).index

    # If no LOW/MEDIUM/HIGH candidate exists, allow a single explicitly labelled
    # exploratory test. This avoids the contradictory WATCH + do-not-change + yes.
    if len(change_idx) == 0 and max_changes_per_cycle > 0:
        exploratory = df[
            (df["strategy_status"] == "active")
            & (~df["is_selection_blocked"])
        ].sort_values(
            ["objective_context_score", "total_score", "importance_score"],
            ascending=False,
        )
        if not exploratory.empty:
            change_idx = exploratory.head(1).index
            df.loc[change_idx, "priority"] = "EXPLORATORY"
            df.loc[change_idx, "recommended_action"] = (
                "exploratory one-step test only; controller must evaluate immediately after the run"
            )

    df.loc[change_idx, "next_cycle_change"] = "yes"

    # Safety assertion: no WATCH/do-not-change row can be selected.
    bad = (df["next_cycle_change"] == "yes") & (df["recommended_action"].str.startswith("do not change now"))
    df.loc[bad, "next_cycle_change"] = "no"

    df["reason"] = df.apply(make_reason, axis=1)
    df["evidence"] = df.apply(make_evidence_text, axis=1)

    status_order = {
        "active": 0,
        "candidate": 1,
        "scientific_guard": 2,
        "cooldown": 3,
        "bound_blocked": 4,
        "frozen": 5,
    }
    df["_status_order"] = df["strategy_status"].map(status_order).fillna(9)

    df = df.sort_values(
        ["_status_order", "next_cycle_change", "total_score", "objective_context_score", "sensitivity_score", "importance_score"],
        ascending=[True, False, False, False, False, False],
    ).drop(columns=["_status_order"])

    out_cols = [
        "parameter",
        "config_status",
        "strategy_status",
        "priority",
        "next_cycle_change",
        "recommended_action",
        "recommended_direction",
        "total_score",
        "base_score",
        "objective_context_score",
        "sensitivity_score",
        "importance_score",
        "history_score",
        "cross_project_score",
        "process_group",
        "species_targets",
        "expected_effect",
        "config_value",
        "config_min",
        "config_max",
        "recent_change_count",
        "selection_block_reason",
        "objective_context_reason",
        "scientific_guard_reason",
        "reason",
        "evidence",
    ]

    for c in out_cols:
        if c not in df.columns:
            df[c] = ""

    return df[out_cols]

def priority_from_score(score: float) -> str:
    score = safe_float(score, 0.0) or 0.0

    if score >= 0.75:
        return "HIGH"
    if score >= 0.45:
        return "MEDIUM"
    if score >= 0.20:
        return "LOW"
    return "WATCH"


def resolve_direction(row: pd.Series) -> str:
    d1 = safe_str(row.get("direction", "")).lower()
    d2 = safe_str(row.get("history_direction", "")).lower()
    d3 = safe_str(row.get("expected_effect", "")).lower()

    for d in [d1, d2, d3]:
        if "increase" in d or d in ["+", "positive", "up"]:
            return "increase"
        if "decrease" in d or d in ["-", "negative", "down"]:
            return "decrease"

    return "test_both_directions"


def action_from_priority_and_direction(
    priority: str,
    direction: str,
    is_frozen: bool = False,
    selection_block_reason: str = "",
) -> str:
    if selection_block_reason:
        return f"do not change now; blocked because {selection_block_reason}"

    if is_frozen:
        return "do not change now; parameter is temporarily frozen"

    if priority == "HIGH":
        return f"change conservatively; preferred direction: {direction}"
    if priority == "MEDIUM":
        return f"test after high-priority parameters; direction: {direction}"
    if priority == "LOW":
        return "test one small conservative step; controller must evaluate immediately"
    if priority == "EXPLORATORY":
        return "exploratory one-step test only; controller must evaluate immediately after the run"
    return "do not change now; monitor evidence"


def make_reason(row: pd.Series) -> str:
    parts = []

    if safe_float(row.get("sensitivity_score"), 0.0):
        parts.append(f"process sensitivity={row['sensitivity_score']:.2f}")

    if safe_float(row.get("importance_score"), 0.0):
        parts.append(f"parameter importance={row['importance_score']:.2f}")

    if safe_float(row.get("history_score"), 0.0):
        parts.append(f"history support={row['history_score']:.2f}")

    if safe_float(row.get("cross_project_score"), 0.0):
        parts.append(f"cross-project support={row['cross_project_score']:.2f}")

    if safe_float(row.get("objective_context_score"), 0.0):
        parts.append(f"objective-context adjustment={row['objective_context_score']:.2f}")

    obj_reason = safe_str(row.get("objective_context_reason", "")).strip()
    if obj_reason:
        parts.append(obj_reason)

    guard_reason = safe_str(row.get("scientific_guard_reason", "")).strip()
    if guard_reason:
        parts.append(f"scientific guard={guard_reason}")

    status = safe_str(row.get("config_status", "")).strip()
    if status:
        parts.append(f"agent_config status={status}")

    block_reason = safe_str(row.get("selection_block_reason", "")).strip()
    if block_reason:
        parts.append(f"selection blocked={block_reason}")

    if not parts:
        return "Insufficient quantitative evidence; keep as watch parameter."

    return "; ".join(parts)


def make_evidence_text(row: pd.Series) -> str:
    items = []

    for c in [
        "process_evidence",
        "interpretation",
        "importance_evidence",
        "history_evidence",
        "cross_project_evidence",
        "suggestion_history_evidence",
        "objective_context_reason",
        "scientific_guard_reason",
        "objective_recommended_direction",
    ]:
        txt = safe_str(row.get(c, "")).strip()
        if txt:
            items.append(txt)

    return " || ".join(items)


# =============================================================================
# Export
# =============================================================================

def write_excel(
    path: Path,
    strategy_df: pd.DataFrame,
    evidence_df: pd.DataFrame,
    meta: Dict[str, str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        strategy_df.to_excel(writer, sheet_name="calibration_strategy", index=False)
        evidence_df.to_excel(writer, sheet_name="merged_evidence", index=False)
        pd.DataFrame([meta]).to_excel(writer, sheet_name="metadata", index=False)

        wb = writer.book
        for ws in wb.worksheets:
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions

            for col in ws.columns:
                max_len = 12
                col_letter = col[0].column_letter

                for cell in col:
                    try:
                        max_len = max(max_len, min(len(str(cell.value)), 70))
                    except Exception:
                        pass

                ws.column_dimensions[col_letter].width = max_len + 2


def write_markdown(
    path: Path,
    strategy_df: pd.DataFrame,
    meta: Dict[str, str],
    max_rows: int = 25,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    lines = []

    lines.append(f"# Calibration Strategy {VERSION}")
    lines.append("")
    lines.append(f"Generated: {meta['generated_at']}")
    lines.append(f"Engine version: `{meta['engine_version']}`")
    lines.append(f"Project directory: `{meta['project_dir']}`")
    lines.append("")

    lines.append("## Purpose")
    lines.append("")
    lines.append(
        "This file defines the deterministic calibration strategy for the next MIN3P calibration cycles. "
        "The strategy combines process sensitivity knowledge, sensitivity interpretation, parameter importance, "
        "optimization history, and optional cross-project learning."
    )
    lines.append("")
    lines.append("GPT is not used in this module. GPT review is handled separately by `gpt_supervisor.py`.")
    lines.append("")

    lines.append("## Deterministic ranking rule")
    lines.append("")
    lines.append("- 20% process sensitivity")
    lines.append("- 70% parameter importance")
    lines.append("- 7% optimization-history support")
    lines.append("- 3% cross-project learning support")
    lines.append("")
    lines.append("Inactive parameters are excluded. Frozen, bound-limited, and cooldown parameters are shown but cannot be selected for next-cycle change.")
    lines.append("")

    lines.append("## Recommended next-cycle changes")
    lines.append("")

    next_df = strategy_df[strategy_df["next_cycle_change"] == "yes"]

    if next_df.empty:
        lines.append("No parameter was selected for automatic change in the next cycle.")
    else:
        for _, r in next_df.iterrows():
            lines.append(f"### {r['parameter']}")
            lines.append("")
            lines.append(f"- Config status: `{r.get('config_status', '')}`")
            lines.append(f"- Strategy status: `{r.get('strategy_status', '')}`")
            lines.append(f"- Priority: **{r['priority']}**")
            lines.append(f"- Recommended action: {r['recommended_action']}")
            lines.append(f"- Recommended direction: {r['recommended_direction']}")
            lines.append(f"- Total score: {safe_float(r['total_score'], 0.0):.3f}")
            lines.append(f"- Reason: {r['reason']}")
            evidence = safe_str(r.get("evidence", "")).strip()
            if evidence:
                lines.append(f"- Evidence: {evidence}")
            lines.append("")

    lines.append("## Full parameter ranking")
    lines.append("")

    show = strategy_df.head(max_rows).copy()

    if not show.empty:
        cols = [
            "parameter",
            "config_status",
            "strategy_status",
            "priority",
            "next_cycle_change",
            "recommended_direction",
            "total_score",
            "recommended_action",
        ]
        available_cols = [c for c in cols if c in show.columns]
        lines.append(show[available_cols].to_markdown(index=False))
    else:
        lines.append("No strategy rows were created.")
    lines.append("")

    lines.append("## Manual review checklist")
    lines.append("")
    lines.append("- Confirm that selected parameters are `active` in `agent_config.xlsx`.")
    lines.append("- Do not change `inactive` parameters.")
    lines.append("- Do not change `frozen` parameters unless the agent explicitly unfreezes them.")
    lines.append("- Check whether the proposed direction is chemically meaningful.")
    lines.append("- Avoid changing multiple strongly correlated mineral kinetic parameters in the same cycle.")
    lines.append("- After each run, update `optimization_history.xlsx` and regenerate this strategy.")
    lines.append("")

    path.write_text("\n".join(lines), encoding="utf-8")


# =============================================================================
# Main execution
# =============================================================================

def run_v10(
    project_dir: Path,
    max_active_parameters: int = 5,
    max_changes_per_cycle: int = 1,
) -> Tuple[Path, Path]:
    project_dir = project_dir.resolve()
    ensure_dirs(project_dir)

    input_paths = {
        key: resolve_input_path(project_dir, filename)
        for key, filename in DEFAULT_INPUT_FILES.items()
    }

    process_raw = safe_read_excel(
        input_paths["process_sensitivity"],
        "process sensitivity",
    )

    interp_raw = safe_read_excel(
        input_paths["sensitivity_interpretation"],
        "sensitivity interpretation",
    )

    importance_raw = safe_read_excel(
        input_paths["parameter_importance"],
        "parameter importance",
    )

    history_raw = safe_read_excel(
        input_paths["optimization_history"],
        "optimization history",
    )

    suggestions_raw = safe_read_excel(
        input_paths["parameter_suggestions"],
        "parameter suggestions",
    )

    process_df = build_process_evidence(process_raw)
    interp_df = build_interpretation_evidence(interp_raw)
    importance_df = build_importance_evidence(importance_raw)
    history_df = build_history_evidence(history_raw)
    cooldown_df = build_suggestion_cooldown(suggestions_raw)
    cross_df = load_cross_project_learning(project_dir)
    config_df = read_parameter_status(project_dir)

    evidence_df = merge_evidence(
        process_df=process_df,
        interp_df=interp_df,
        importance_df=importance_df,
        history_df=history_df,
        cross_df=cross_df,
        config_df=config_df,
        cooldown_df=cooldown_df,
    )

    evidence_df = add_objective_context_to_evidence(
        evidence_df=evidence_df,
        history_raw=history_raw,
    )

    strategy_df = compute_strategy(
        evidence=evidence_df,
        max_active_parameters=max_active_parameters,
        max_changes_per_cycle=max_changes_per_cycle,
    )

    meta = {
        "engine": ENGINE_NAME,
        "engine_version": VERSION,
        "generated_at": now_stamp(),
        "project_dir": str(project_dir),
        "process_sensitivity_file": str(input_paths["process_sensitivity"]),
        "sensitivity_interpretation_file": str(input_paths["sensitivity_interpretation"]),
        "parameter_importance_file": str(input_paths["parameter_importance"]),
        "optimization_history_file": str(input_paths["optimization_history"]),
        "parameter_suggestions_file": str(input_paths["parameter_suggestions"]),
        "agent_config_file": str(resolve_agent_config_path(project_dir) or ""),
        "max_active_parameters": str(max_active_parameters),
        "max_changes_per_cycle": str(max_changes_per_cycle),
        "gpt_used": "false",
    }

    report_path = project_dir / REPORT_REL
    result_path = project_dir / RESULT_REL

    write_excel(result_path, strategy_df, evidence_df, meta)
    write_markdown(report_path, strategy_df, meta)

    print(f"[OK] Strategy Excel written: {result_path}")
    print(f"[OK] Strategy Markdown written: {report_path}")

    return report_path, result_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="V10.7 deterministic MIN3P calibration strategy generator"
    )

    parser.add_argument(
        "--project-dir",
        type=str,
        default=".",
        help="MIN3P calibration project directory",
    )

    parser.add_argument(
        "--max-active-parameters",
        type=int,
        default=5,
        help="Number of top parameters marked active in the strategy",
    )

    parser.add_argument(
        "--max-changes-per-cycle",
        type=int,
        default=1,
        help="Number of parameters recommended for the next calibration cycle",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    run_v10(
        project_dir=Path(args.project_dir),
        max_active_parameters=args.max_active_parameters,
        max_changes_per_cycle=args.max_changes_per_cycle,
    )
