from __future__ import annotations

"""
parameter_importance.py

MIN3P AI Assistant - V10.3 Parameter Importance Builder

Purpose
-------
Create:
    04_results/parameter_importance_V10.xlsx
    05_reports/parameter_importance_V10.md

from:
    04_results/sensitivity_results_V10.xlsx
    04_results/sensitivity_coefficients_V10.xlsx
    04_results/process_sensitivity_summary_V10.xlsx
    04_results/sensitivity_interpretation_V10.xlsx

Optional:
    01_input/agent_config.xlsx

Role in V10 workflow
--------------------
sensitivity-run
    ↓
process-report
    ↓
sensitivity-interpretation
    ↓
parameter-importance      ← this module
    ↓
calibration-strategy
    ↓
gpt-supervisor

Important V10.3 change
----------------------
This version reads the actual perturbation statistics directly from
sensitivity_results_V10.xlsx:

    base_value
    test_value
    relative_parameter_change
    clipped_to_bounds
    sensitivity_case

Therefore the output contains traceability columns showing the real parameter
values used in the sensitivity analysis, including min/max tested values and
whether a perturbation was clipped by agent_config.xlsx bounds.

This module is deterministic. It does not call GPT.
"""

import math
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd


VERSION = "V10.3-actual-perturbation-aware"
EPS = 1e-12


# =============================================================================
# Basic utilities
# =============================================================================

def timestamp() -> str:
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


def safe_float(x, default: float = 0.0) -> float:
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


def find_col(df: pd.DataFrame, candidates: List[str]) -> Optional[str]:
    cols = set(df.columns)
    for c in candidates:
        cn = norm_col(c)
        if cn in cols:
            return cn
    return None


def detect_parameter_column(df: pd.DataFrame) -> Optional[str]:
    return find_col(
        df,
        [
            "parameter",
            "param",
            "parameter_name",
            "name",
            "changed_parameter",
            "selected_parameter",
        ],
    )


def detect_species_column(df: pd.DataFrame) -> Optional[str]:
    return find_col(
        df,
        [
            "species",
            "component",
            "target",
            "variable",
            "observed_variable",
            "output",
        ],
    )


def detect_score_column(df: pd.DataFrame, preferred: List[str]) -> Optional[str]:
    return find_col(
        df,
        preferred
        + [
            "score",
            "importance",
            "importance_score",
            "sensitivity",
            "sensitivity_score",
            "abs_sensitivity",
            "absolute_sensitivity",
            "normalized_sensitivity",
            "coefficient",
            "sensitivity_coefficient",
            "effect",
            "abs_effect",
            "absolute_effect",
            "rank_score",
        ],
    )


def read_excel_file(path: Path, label: str) -> pd.DataFrame:
    if path is None or not Path(path).exists():
        print(f"[WARN] Missing {label}: {path}")
        return pd.DataFrame()

    try:
        return normalize_columns(pd.read_excel(path))
    except Exception as exc:
        print(f"[WARN] Could not read {label}: {path} | {exc}")
        return pd.DataFrame()


def read_excel_sheet(path: Path, sheet_name: str, label: str) -> pd.DataFrame:
    if path is None or not Path(path).exists():
        print(f"[WARN] Missing {label}: {path}")
        return pd.DataFrame()

    try:
        return normalize_columns(pd.read_excel(path, sheet_name=sheet_name))
    except Exception as exc:
        print(f"[WARN] Could not read {label}: {path} | {exc}")
        return pd.DataFrame()


def join_unique(values, limit: int = 8) -> str:
    out = []
    for v in values:
        s = safe_str(v).strip()
        if not s or s.lower() in ["nan", "none"]:
            continue
        if s not in out:
            out.append(s)
    return " | ".join(out[:limit])


# =============================================================================
# Agent config readers
# =============================================================================

def read_parameter_config(parameters_file: Path) -> pd.DataFrame:
    """
    Reads agent_config.xlsx / parameters.

    Returns:
        parameter, status, value, min, max, sensitivity_mode,
        sensitivity_multiplier, group
    """
    columns = [
        "parameter",
        "status",
        "value",
        "min",
        "max",
        "sensitivity_mode",
        "sensitivity_multiplier",
        "group",
    ]

    params = read_excel_sheet(parameters_file, "parameters", "agent_config parameters")

    if params.empty:
        return pd.DataFrame(columns=columns)

    pcol = detect_parameter_column(params)
    if not pcol:
        return pd.DataFrame(columns=columns)

    status_col = find_col(params, ["status", "active", "parameter_status"])
    value_col = find_col(params, ["value", "current_value", "initial_value"])
    min_col = find_col(params, ["min", "minimum", "lower_bound"])
    max_col = find_col(params, ["max", "maximum", "upper_bound"])
    mode_col = find_col(params, ["sensitivity_mode", "mode", "perturbation_mode"])
    multiplier_col = find_col(
        params,
        ["sensitivity_multiplier", "multiplier", "perturbation_multiplier"],
    )
    group_col = find_col(params, ["group", "process_group", "parameter_group"])

    out = pd.DataFrame()
    out["parameter"] = params[pcol].astype(str).str.strip()
    out["status"] = (
        params[status_col].astype(str).str.lower().str.strip()
        if status_col
        else "active"
    )
    out["value"] = (
        params[value_col].apply(lambda x: safe_float(x, float("nan")))
        if value_col
        else float("nan")
    )
    out["min"] = (
        params[min_col].apply(lambda x: safe_float(x, float("nan")))
        if min_col
        else float("nan")
    )
    out["max"] = (
        params[max_col].apply(lambda x: safe_float(x, float("nan")))
        if max_col
        else float("nan")
    )
    out["sensitivity_mode"] = (
        params[mode_col].astype(str).str.lower().str.strip()
        if mode_col
        else "multiplier"
    )
    out["sensitivity_multiplier"] = (
        params[multiplier_col].apply(lambda x: safe_float(x, 0.25))
        if multiplier_col
        else 0.25
    )
    out["group"] = params[group_col].astype(str) if group_col else ""

    out = out[out["parameter"].astype(str).str.strip() != ""].copy()
    return out.drop_duplicates("parameter", keep="first")


def read_species_weights(parameters_file: Path) -> Dict[str, float]:
    """
    Reads agent_config.xlsx / species.

    Expected useful columns:
        species, weight

    If unavailable, all species have weight 1.
    """
    species = read_excel_sheet(parameters_file, "species", "agent_config species")

    if species.empty:
        return {}

    sp_col = detect_species_column(species)
    weight_col = find_col(species, ["weight", "calibration_weight", "objective_weight"])

    if not sp_col:
        return {}

    weights: Dict[str, float] = {}

    for _, row in species.iterrows():
        sp = safe_str(row.get(sp_col, "")).strip()
        if not sp:
            continue

        w = safe_float(row.get(weight_col), 1.0) if weight_col else 1.0
        weights[sp.lower()] = w if w is not None else 1.0

    return weights


# =============================================================================
# Evidence from actual sensitivity results
# =============================================================================

def build_from_sensitivity_results(results_df: pd.DataFrame) -> pd.DataFrame:
    """
    Read actual perturbation statistics from sensitivity_results_V10.xlsx.

    This function uses the real values tested by SensitivityAnalyzer:
        base_value
        test_value
        relative_parameter_change
        clipped_to_bounds
        sensitivity_case

    Output columns:
        parameter
        actual_base_value
        actual_test_min
        actual_test_max
        actual_relative_change_min
        actual_relative_change_max
        max_abs_actual_relative_change
        mean_abs_actual_relative_change
        actual_perturbation_component
        n_sensitivity_tests
        was_clipped_in_sensitivity
        tested_cases
    """
    out_cols = [
        "parameter",
        "actual_base_value",
        "actual_test_min",
        "actual_test_max",
        "actual_relative_change_min",
        "actual_relative_change_max",
        "max_abs_actual_relative_change",
        "mean_abs_actual_relative_change",
        "actual_perturbation_component",
        "n_sensitivity_tests",
        "was_clipped_in_sensitivity",
        "tested_cases",
    ]

    if results_df.empty:
        return pd.DataFrame(columns=out_cols)

    pcol = detect_parameter_column(results_df)
    if not pcol:
        return pd.DataFrame(columns=out_cols)

    role_col = find_col(results_df, ["sensitivity_role"])
    base_col = find_col(results_df, ["base_value"])
    test_col = find_col(results_df, ["test_value"])
    rel_col = find_col(results_df, ["relative_parameter_change"])
    clipped_col = find_col(results_df, ["clipped_to_bounds"])
    case_col = find_col(results_df, ["sensitivity_case", "case", "multiplier"])
    status_col = find_col(results_df, ["run_status"])

    work = results_df.copy()
    work["parameter"] = work[pcol].astype(str).str.strip()

    if role_col:
        work = work[work[role_col].astype(str).str.lower().str.strip() == "perturbation"]

    work = work[
        ~work["parameter"].astype(str).str.upper().isin(["", "BASELINE", "NAN", "NONE"])
    ].copy()

    if work.empty:
        return pd.DataFrame(columns=out_cols)

    work["_base_value"] = work[base_col].apply(lambda x: safe_float(x, float("nan"))) if base_col else float("nan")
    work["_test_value"] = work[test_col].apply(lambda x: safe_float(x, float("nan"))) if test_col else float("nan")

    if rel_col:
        work["_relative_change"] = work[rel_col].apply(lambda x: safe_float(x, float("nan")))
    else:
        work["_relative_change"] = (work["_test_value"] - work["_base_value"]) / work["_base_value"].abs().clip(lower=EPS)

    if clipped_col:
        work["_clipped"] = (
            work[clipped_col]
            .astype(str)
            .str.lower()
            .str.strip()
            .isin(["true", "1", "yes", "y"])
        )
    else:
        work["_clipped"] = False

    work["_case"] = work[case_col].astype(str) if case_col else ""

    if status_col:
        work["_successful"] = work[status_col].astype(str).str.lower().isin(
            ["success", "success_with_retries", "partial_success"]
        )
    else:
        work["_successful"] = True

    # Keep all perturbations for traceability, but successful perturbations dominate.
    # If a parameter has no successful perturbation, keep its rows with lower confidence.
    successful_params = set(work.loc[work["_successful"], "parameter"])
    work["_usable"] = work.apply(
        lambda r: bool(r["_successful"]) or r["parameter"] not in successful_params,
        axis=1,
    )
    work = work[work["_usable"]].copy()

    rows = []
    for parameter, g in work.groupby("parameter"):
        rel = pd.to_numeric(g["_relative_change"], errors="coerce").dropna()
        test_values = pd.to_numeric(g["_test_value"], errors="coerce").dropna()
        base_values = pd.to_numeric(g["_base_value"], errors="coerce").dropna()

        if rel.empty or test_values.empty:
            continue

        max_abs_rel = float(rel.abs().max())
        mean_abs_rel = float(rel.abs().mean())

        rows.append(
            {
                "parameter": parameter,
                "actual_base_value": float(base_values.iloc[0]) if not base_values.empty else float("nan"),
                "actual_test_min": float(test_values.min()),
                "actual_test_max": float(test_values.max()),
                "actual_relative_change_min": float(rel.min()),
                "actual_relative_change_max": float(rel.max()),
                "max_abs_actual_relative_change": max_abs_rel,
                "mean_abs_actual_relative_change": mean_abs_rel,
                "n_sensitivity_tests": int(len(g)),
                "was_clipped_in_sensitivity": bool(g["_clipped"].any()),
                "tested_cases": join_unique(g["_case"], limit=10),
            }
        )

    out = pd.DataFrame(rows)

    if out.empty:
        return pd.DataFrame(columns=out_cols)

    # Normalize by actual maximum tested perturbation across the project.
    # This is not the main sensitivity term. It is only a small traceability
    # component showing how much actual movement was possible/tested.
    out["actual_perturbation_component"] = normalize_01(
        out["max_abs_actual_relative_change"]
    )

    return out[out_cols]


# =============================================================================
# Evidence from sensitivity coefficients / process summary / interpretation
# =============================================================================

def build_from_sensitivity_coefficients(
    coeff_df: pd.DataFrame,
    species_weights: Dict[str, float],
) -> pd.DataFrame:
    """
    Build numerical sensitivity component from sensitivity_coefficients_V10.xlsx.

    This file already reflects actual perturbations because SensitivityAnalyzer
    computes normalized sensitivity using relative_parameter_change from the
    real tested/clipped value.
    """
    out_cols = [
        "parameter",
        "sensitivity_component",
        "dominant_species",
        "species_coverage",
        "coefficient_evidence",
    ]

    if coeff_df.empty:
        return pd.DataFrame(columns=out_cols)

    pcol = detect_parameter_column(coeff_df)
    if not pcol:
        return pd.DataFrame(columns=out_cols)

    spcol = detect_species_column(coeff_df)

    score_col = detect_score_column(
        coeff_df,
        [
            "abs_normalized_sensitivity",
            "absolute_normalized_sensitivity",
            "abs_sensitivity",
            "absolute_sensitivity",
            "normalized_sensitivity",
            "sensitivity_coefficient",
            "coefficient",
            "sensitivity",
            "effect",
        ],
    )

    metric_col = find_col(coeff_df, ["metric"])
    status_col = find_col(coeff_df, ["run_status"])

    work = coeff_df.copy()
    work["parameter"] = work[pcol].astype(str).str.strip()

    if spcol:
        work["_species"] = work[spcol].astype(str).str.strip()
    elif metric_col:
        work["_species"] = (
            work[metric_col]
            .astype(str)
            .str.replace(r"^(RMSE_|MAE_|Bias_)", "", regex=True)
        )
    else:
        work["_species"] = ""

    work["_species_weight"] = work["_species"].str.lower().map(species_weights).fillna(1.0)

    if score_col:
        work["_raw_score"] = work[score_col].apply(safe_float).abs()
    else:
        work["_raw_score"] = 0.0

    if status_col:
        ok = work[status_col].astype(str).str.lower().isin(
            ["success", "success_with_retries", "partial_success"]
        )
        # Failed sensitivity runs are not removed completely, but strongly down-weighted.
        work["_status_weight"] = ok.map({True: 1.0, False: 0.2})
    else:
        work["_status_weight"] = 1.0

    work["_weighted_score"] = (
        work["_raw_score"] * work["_species_weight"] * work["_status_weight"]
    )

    def dominant_species(g: pd.DataFrame) -> str:
        if "_species" not in g.columns or g["_species"].eq("").all():
            return ""
        idx = g["_weighted_score"].idxmax()
        return safe_str(g.loc[idx, "_species"])

    out = work.groupby("parameter", as_index=False).agg(
        sensitivity_raw=("_weighted_score", "sum"),
        sensitivity_max=("_weighted_score", "max"),
        species_coverage=("_species", lambda x: len(set([
            safe_str(v).strip()
            for v in x
            if safe_str(v).strip().lower() not in ["", "nan", "none"]
        ]))),
        coefficient_evidence=("_species", lambda x: join_unique(x, limit=8)),
    )

    dom = work.groupby("parameter").apply(dominant_species).reset_index()
    dom.columns = ["parameter", "dominant_species"]
    out = out.merge(dom, on="parameter", how="left")

    out["sensitivity_component"] = normalize_01(out["sensitivity_raw"])
    out["coefficient_evidence"] = (
        "dominant species: "
        + out["dominant_species"].fillna("")
        + "; species affected: "
        + out["coefficient_evidence"].fillna("")
    )

    return out[out_cols]


def build_from_process_summary(process_df: pd.DataFrame) -> pd.DataFrame:
    out_cols = [
        "parameter",
        "process_component",
        "process_group",
        "process_evidence",
    ]

    if process_df.empty:
        return pd.DataFrame(columns=out_cols)

    pcol = detect_parameter_column(process_df)
    if not pcol:
        return pd.DataFrame(columns=out_cols)

    score_col = detect_score_column(
        process_df,
        [
            "process_sensitivity_score",
            "sensitivity_score",
            "normalized_effect",
            "absolute_effect",
            "abs_effect",
            "score",
        ],
    )

    group_col = find_col(
        process_df,
        [
            "process_group",
            "process",
            "reaction",
            "control",
            "mineral_group",
            "parameter_group",
            "group",
        ],
    )

    evidence_col = find_col(
        process_df,
        [
            "evidence",
            "reason",
            "interpretation",
            "summary",
            "comment",
            "process_reason",
        ],
    )

    work = process_df.copy()
    work["parameter"] = work[pcol].astype(str).str.strip()
    work["_score"] = work[score_col].apply(safe_float).abs() if score_col else 0.0
    work["_group"] = work[group_col].astype(str) if group_col else ""
    work["_evidence"] = work[evidence_col].astype(str) if evidence_col else ""

    out = work.groupby("parameter", as_index=False).agg(
        process_raw=("_score", "max"),
        process_group=("_group", lambda x: join_unique(x, limit=4)),
        process_evidence=("_evidence", lambda x: join_unique(x, limit=4)),
    )

    out["process_component"] = normalize_01(out["process_raw"])
    out["process_group"] = out["process_group"].replace("", "unknown")

    return out[out_cols]


def build_from_interpretation(interpretation_df: pd.DataFrame) -> pd.DataFrame:
    out_cols = [
        "parameter",
        "interpretation_component",
        "interpretation_evidence",
        "interpretation_targets",
    ]

    if interpretation_df.empty:
        return pd.DataFrame(columns=out_cols)

    pcol = detect_parameter_column(interpretation_df)
    if not pcol:
        return pd.DataFrame(columns=out_cols)

    spcol = detect_species_column(interpretation_df)

    score_col = detect_score_column(
        interpretation_df,
        [
            "interpretation_score",
            "importance_score",
            "process_importance",
            "sensitivity_score",
            "score",
        ],
    )

    evidence_col = find_col(
        interpretation_df,
        [
            "interpretation",
            "scientific_interpretation",
            "reason",
            "evidence",
            "comment",
            "expected_effect",
        ],
    )

    work = interpretation_df.copy()
    work["parameter"] = work[pcol].astype(str).str.strip()

    if score_col:
        work["_score"] = work[score_col].apply(safe_float).abs()
    else:
        # If there is no explicit score, each interpretation row gives weak evidence.
        work["_score"] = 1.0

    work["_target"] = work[spcol].astype(str) if spcol else ""
    work["_evidence"] = work[evidence_col].astype(str) if evidence_col else ""

    out = work.groupby("parameter", as_index=False).agg(
        interpretation_raw=("_score", "sum"),
        interpretation_targets=("_target", lambda x: join_unique(x, limit=8)),
        interpretation_evidence=("_evidence", lambda x: join_unique(x, limit=5)),
    )

    out["interpretation_component"] = normalize_01(out["interpretation_raw"])

    return out[out_cols]


# =============================================================================
# Range metrics and importance calculation
# =============================================================================

def _valid_range(value: float, mn: float, mx: float) -> bool:
    if any(pd.isna(v) for v in [value, mn, mx]):
        return False
    if mx <= mn:
        return False
    return True


def calculate_range_metrics(row: pd.Series) -> pd.Series:
    """
    Calculate calibration-range metrics from current agent_config.xlsx.

    This is separate from actual perturbation statistics. The range metrics show
    current calibration freedom; actual perturbation metrics show what was really
    tested in sensitivity_results_V10.xlsx.
    """
    value = safe_float(row.get("value"), float("nan"))
    mn = safe_float(row.get("min"), float("nan"))
    mx = safe_float(row.get("max"), float("nan"))
    mode = safe_str(row.get("sensitivity_mode", "multiplier")).lower().strip()
    mult = safe_float(row.get("sensitivity_multiplier"), 0.25)

    if not _valid_range(value, mn, mx):
        return pd.Series(
            {
                "range_component": 0.0,
                "range_span": 0.0,
                "range_position": float("nan"),
                "bound_warning": "no_valid_range",
                "configured_perturbation_fraction": 0.0,
            }
        )

    if mode == "multiplier" and value > 0 and mn > 0 and mx > 0:
        log_span = math.log10(mx / mn)

        if log_span <= EPS:
            return pd.Series(
                {
                    "range_component": 0.0,
                    "range_span": 0.0,
                    "range_position": float("nan"),
                    "bound_warning": "fixed_or_zero_range",
                    "configured_perturbation_fraction": 0.0,
                }
            )

        # Cap at 8 log units so very large ranges do not dominate.
        range_component = min(log_span, 8.0) / 8.0

        range_position = math.log10(value / mn) / log_span
        range_position = max(0.0, min(1.0, range_position))

        if range_position <= 0.05:
            bound_warning = "near_lower_bound"
        elif range_position >= 0.95:
            bound_warning = "near_upper_bound"
        else:
            bound_warning = "within_range"

        if mult > 0 and mult < 1:
            up_target = value * (1.0 + mult)
            down_target = value * (1.0 - mult)

            up_value = min(mx, up_target)
            down_value = max(mn, down_target)

            up_frac = max(0.0, (up_value - value) / max(abs(value), EPS))
            down_frac = max(0.0, (value - down_value) / max(abs(value), EPS))
            configured_perturbation_fraction = max(up_frac, down_frac)
        else:
            configured_perturbation_fraction = 0.0

        return pd.Series(
            {
                "range_component": range_component,
                "range_span": log_span,
                "range_position": range_position,
                "bound_warning": bound_warning,
                "configured_perturbation_fraction": configured_perturbation_fraction,
            }
        )

    # Linear range for pH, head, dispersivity, and non-positive values.
    span = mx - mn
    scale = max(abs(value), EPS)
    range_component = min(abs(span) / scale, 1.0)

    range_position = (value - mn) / span
    range_position = max(0.0, min(1.0, range_position))

    if range_position <= 0.05:
        bound_warning = "near_lower_bound"
    elif range_position >= 0.95:
        bound_warning = "near_upper_bound"
    else:
        bound_warning = "within_range"

    if mult > 0:
        if mode == "minmax":
            configured_perturbation_fraction = range_component
        else:
            up_target = value * (1.0 + mult)
            down_target = value * (1.0 - mult)
            up_value = min(mx, up_target)
            down_value = max(mn, down_target)
            configured_perturbation_fraction = max(
                abs(up_value - value),
                abs(value - down_value),
            ) / scale
    else:
        configured_perturbation_fraction = range_component

    return pd.Series(
        {
            "range_component": range_component,
            "range_span": span,
            "range_position": range_position,
            "bound_warning": bound_warning,
            "configured_perturbation_fraction": configured_perturbation_fraction,
        }
    )


def status_multiplier(status: str) -> float:
    s = safe_str(status).lower().strip()

    if s in ["inactive", "no", "false", "0", "disabled", "exclude"]:
        return 0.0

    if s in ["frozen", "temporary_frozen", "temp_frozen"]:
        return 0.5

    return 1.0


def classify_priority(score: float) -> str:
    score = safe_float(score, 0.0)

    if score >= 0.75:
        return "HIGH"
    if score >= 0.45:
        return "MEDIUM"
    if score >= 0.20:
        return "LOW"
    return "WATCH"


def merge_importance_evidence(
    sensitivity_df: pd.DataFrame,
    process_df: pd.DataFrame,
    interpretation_df: pd.DataFrame,
    perturbation_df: pd.DataFrame,
    config_df: pd.DataFrame,
) -> pd.DataFrame:
    params = set()

    for df in [sensitivity_df, process_df, interpretation_df, perturbation_df, config_df]:
        if not df.empty and "parameter" in df.columns:
            params.update(
                [
                    safe_str(p).strip()
                    for p in df["parameter"]
                    if safe_str(p).strip().lower() not in ["", "nan", "none"]
                ]
            )

    base = pd.DataFrame({"parameter": sorted(params)})

    for df in [sensitivity_df, process_df, interpretation_df, perturbation_df, config_df]:
        if not df.empty and "parameter" in df.columns:
            base = base.merge(df, on="parameter", how="left")

    numeric_cols = [
        "sensitivity_component",
        "process_component",
        "interpretation_component",
        "actual_perturbation_component",
        "species_coverage",
        "actual_base_value",
        "actual_test_min",
        "actual_test_max",
        "actual_relative_change_min",
        "actual_relative_change_max",
        "max_abs_actual_relative_change",
        "mean_abs_actual_relative_change",
        "n_sensitivity_tests",
        "value",
        "min",
        "max",
        "sensitivity_multiplier",
    ]

    for col in numeric_cols:
        if col not in base.columns:
            base[col] = 0.0
        base[col] = pd.to_numeric(base[col], errors="coerce").fillna(0.0)

    text_cols = [
        "dominant_species",
        "coefficient_evidence",
        "process_group",
        "process_evidence",
        "interpretation_evidence",
        "interpretation_targets",
        "was_clipped_in_sensitivity",
        "tested_cases",
        "status",
        "group",
        "sensitivity_mode",
    ]

    for col in text_cols:
        if col not in base.columns:
            base[col] = ""
        base[col] = base[col].fillna("").astype(str)

    base["status"] = base["status"].replace("", "unknown").str.lower().str.strip()

    # Prefer agent_config group if process group is missing.
    missing_group = base["process_group"].isin(["", "unknown", "nan", "none"])
    base.loc[missing_group, "process_group"] = base.loc[missing_group, "group"]
    base["process_group"] = base["process_group"].replace("", "unknown")

    return base


def make_importance_evidence(row: pd.Series) -> str:
    parts = []

    if safe_float(row.get("sensitivity_component"), 0.0) > 0:
        parts.append(f"sensitivity={safe_float(row.get('sensitivity_component'), 0.0):.2f}")

    if safe_float(row.get("process_component"), 0.0) > 0:
        parts.append(f"process={safe_float(row.get('process_component'), 0.0):.2f}")

    if safe_float(row.get("interpretation_component"), 0.0) > 0:
        parts.append(
            f"interpretation={safe_float(row.get('interpretation_component'), 0.0):.2f}"
        )

    if safe_float(row.get("range_component"), 0.0) > 0:
        parts.append(f"range={safe_float(row.get('range_component'), 0.0):.2f}")

    if safe_float(row.get("actual_perturbation_component"), 0.0) > 0:
        parts.append(
            "actual_perturbation="
            f"{safe_float(row.get('actual_perturbation_component'), 0.0):.2f}"
        )

    bw = safe_str(row.get("bound_warning", "")).strip()
    if bw and bw not in ["within_range", "nan", "none"]:
        parts.append(f"bound_warning={bw}")

    clipped = safe_str(row.get("was_clipped_in_sensitivity", "")).lower().strip()
    if clipped in ["true", "1", "yes"]:
        parts.append("sensitivity_clipped=true")

    ds = safe_str(row.get("dominant_species", "")).strip()
    if ds:
        parts.append(f"dominant_species={ds}")

    pg = safe_str(row.get("process_group", "")).strip()
    if pg and pg.lower() not in ["unknown", "nan", "none"]:
        parts.append(f"process_group={pg}")

    st = safe_str(row.get("status", "")).strip()
    if st:
        parts.append(f"status={st}")

    return "; ".join(parts) if parts else "No strong parameter-importance evidence."


def compute_parameter_importance(evidence: pd.DataFrame) -> pd.DataFrame:
    if evidence.empty:
        return pd.DataFrame()

    df = evidence.copy()

    range_metrics = df.apply(calculate_range_metrics, axis=1)
    df = pd.concat([df, range_metrics], axis=1)

    # V10.3 actual-perturbation-aware deterministic importance.
    #
    # sensitivity_component:
    #     from sensitivity_coefficients_V10.xlsx; already uses real relative
    #     changes from actual/clipped sensitivity test values.
    #
    # actual_perturbation_component:
    #     from sensitivity_results_V10.xlsx; explicitly records how much the
    #     parameter was actually moved in the sensitivity analysis.
    #
    # range_component:
    #     from current agent_config min/max; current calibration freedom.
    #
    # The actual perturbation and range terms are deliberately small. They
    # improve traceability but do not dominate hydrogeochemical sensitivity.
    df["importance_score_raw"] = (
        0.45 * df["sensitivity_component"]
        + 0.23 * df["process_component"]
        + 0.18 * df["interpretation_component"]
        + 0.07 * df["actual_perturbation_component"]
        + 0.07 * df["range_component"]
    )

    df["status_multiplier"] = df["status"].apply(status_multiplier)
    df["importance_score"] = df["importance_score_raw"] * df["status_multiplier"]
    df["parameter_importance"] = df["importance_score"]
    df["priority"] = df["importance_score"].apply(classify_priority)

    df = df.sort_values(
        [
            "importance_score",
            "sensitivity_component",
            "interpretation_component",
            "actual_perturbation_component",
            "range_component",
        ],
        ascending=False,
    ).reset_index(drop=True)

    df["importance_rank"] = range(1, len(df) + 1)
    df["importance_evidence"] = df.apply(make_importance_evidence, axis=1)

    out_cols = [
        "importance_rank",
        "parameter",
        "status",
        "priority",
        "parameter_importance",
        "importance_score",
        "importance_score_raw",
        "sensitivity_component",
        "process_component",
        "interpretation_component",
        "actual_perturbation_component",
        "range_component",
        "range_span",
        "range_position",
        "bound_warning",
        "configured_perturbation_fraction",
        "actual_base_value",
        "actual_test_min",
        "actual_test_max",
        "actual_relative_change_min",
        "actual_relative_change_max",
        "max_abs_actual_relative_change",
        "mean_abs_actual_relative_change",
        "n_sensitivity_tests",
        "was_clipped_in_sensitivity",
        "tested_cases",
        "species_coverage",
        "dominant_species",
        "process_group",
        "interpretation_targets",
        "importance_evidence",
        "coefficient_evidence",
        "process_evidence",
        "interpretation_evidence",
        "value",
        "min",
        "max",
        "sensitivity_mode",
        "sensitivity_multiplier",
    ]

    for col in out_cols:
        if col not in df.columns:
            df[col] = ""

    return df[out_cols]


# =============================================================================
# Export
# =============================================================================

def write_excel(
    output_file: Path,
    importance_df: pd.DataFrame,
    merged_evidence_df: pd.DataFrame,
    metadata: Dict[str, str],
) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with pd.ExcelWriter(output_file, engine="openpyxl") as writer:
        importance_df.to_excel(writer, sheet_name="parameter_importance", index=False)
        merged_evidence_df.to_excel(writer, sheet_name="merged_evidence", index=False)
        pd.DataFrame([metadata]).to_excel(writer, sheet_name="metadata", index=False)

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


def write_markdown_report(
    report_file: Path,
    importance_df: pd.DataFrame,
    metadata: Dict[str, str],
    max_rows: int = 25,
) -> None:
    report_file.parent.mkdir(parents=True, exist_ok=True)

    lines = []
    lines.append("# Parameter Importance V10")
    lines.append("")
    lines.append(f"Generated: {metadata.get('generated_at', '')}")
    lines.append(f"Project: `{metadata.get('project_name', '')}`")
    lines.append(f"Engine version: `{metadata.get('engine_version', '')}`")
    lines.append("")
    lines.append("## Purpose")
    lines.append("")
    lines.append(
        "This report ranks calibration parameters using deterministic evidence "
        "from real sensitivity perturbations, sensitivity coefficients, process "
        "sensitivity, sensitivity interpretation, and current agent_config ranges."
    )
    lines.append("")
    lines.append("## Scoring rule")
    lines.append("")
    lines.append("- 45% numerical sensitivity component")
    lines.append("- 23% process sensitivity component")
    lines.append("- 18% interpretation component")
    lines.append("- 7% actual perturbation component from sensitivity_results_V10.xlsx")
    lines.append("- 7% current range component from agent_config min/max")
    lines.append("- inactive parameters receive zero importance")
    lines.append("- frozen parameters retain evidence but are down-weighted")
    lines.append("")
    lines.append("## Actual perturbation traceability")
    lines.append("")
    lines.append(
        "The columns `actual_test_min`, `actual_test_max`, "
        "`actual_relative_change_min`, `actual_relative_change_max`, "
        "`max_abs_actual_relative_change`, and `was_clipped_in_sensitivity` are "
        "read directly from `sensitivity_results_V10.xlsx`."
    )
    lines.append("")

    if importance_df.empty:
        lines.append("No parameter importance rows were created.")
    else:
        lines.append("## Top parameters")
        lines.append("")
        cols = [
            "importance_rank",
            "parameter",
            "status",
            "priority",
            "importance_score",
            "sensitivity_component",
            "interpretation_component",
            "actual_perturbation_component",
            "range_component",
            "was_clipped_in_sensitivity",
            "bound_warning",
            "process_group",
        ]
        show_cols = [c for c in cols if c in importance_df.columns]
        lines.append(importance_df.head(max_rows)[show_cols].to_markdown(index=False))

    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    lines.append(
        "High-ranked parameters are those that produce strong measurable changes "
        "in the model outputs, are connected to interpretable hydrogeochemical "
        "processes, and have traceable sensitivity perturbations. This file is "
        "used by `calibration_strategy.py` to build the V10 calibration strategy."
    )
    lines.append("")

    report_file.write_text("\n".join(lines), encoding="utf-8")


# =============================================================================
# Main public function
# =============================================================================

def write_parameter_importance_report(
    process_summary_file: Path,
    interpretation_file: Path,
    sensitivity_coefficients_file: Path,
    parameters_file: Path,
    importance_file: Path,
    report_file: Path,
    project_name: str = "",
    sensitivity_results_file: Path | None = None,
) -> Path:
    """
    Main function used by min3p_ai_pipeline_V10_1.py.

    Backward compatible:
    - If sensitivity_results_file is not passed by the pipeline, it is inferred
      from sensitivity_coefficients_file.parent / sensitivity_results_V10.xlsx.

    Returns:
        importance_file
    """
    if sensitivity_results_file is None:
        sensitivity_results_file = (
            Path(sensitivity_coefficients_file).parent / "sensitivity_results_V10.xlsx"
        )

    process_raw = read_excel_file(process_summary_file, "process sensitivity summary")
    interpretation_raw = read_excel_file(interpretation_file, "sensitivity interpretation")
    coeff_raw = read_excel_file(sensitivity_coefficients_file, "sensitivity coefficients")
    results_raw = read_excel_file(sensitivity_results_file, "actual sensitivity results")

    config_df = read_parameter_config(parameters_file)
    species_weights = read_species_weights(parameters_file)

    coeff_evidence = build_from_sensitivity_coefficients(
        coeff_df=coeff_raw,
        species_weights=species_weights,
    )

    process_evidence = build_from_process_summary(process_raw)
    interpretation_evidence = build_from_interpretation(interpretation_raw)
    perturbation_evidence = build_from_sensitivity_results(results_raw)

    merged = merge_importance_evidence(
        sensitivity_df=coeff_evidence,
        process_df=process_evidence,
        interpretation_df=interpretation_evidence,
        perturbation_df=perturbation_evidence,
        config_df=config_df,
    )

    importance_df = compute_parameter_importance(merged)

    metadata = {
        "engine": "parameter_importance",
        "engine_version": VERSION,
        "generated_at": timestamp(),
        "project_name": project_name,
        "sensitivity_results_file": str(sensitivity_results_file),
        "process_summary_file": str(process_summary_file),
        "interpretation_file": str(interpretation_file),
        "sensitivity_coefficients_file": str(sensitivity_coefficients_file),
        "parameters_file": str(parameters_file),
        "importance_file": str(importance_file),
        "report_file": str(report_file),
    }

    write_excel(
        output_file=importance_file,
        importance_df=importance_df,
        merged_evidence_df=merged,
        metadata=metadata,
    )

    write_markdown_report(
        report_file=report_file,
        importance_df=importance_df,
        metadata=metadata,
    )

    print(f"[OK] Parameter importance Excel created: {importance_file}")
    print(f"[OK] Parameter importance report created: {report_file}")

    return importance_file


# =============================================================================
# Optional command-line use
# =============================================================================

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Create parameter_importance_V10.xlsx from V10 sensitivity outputs, "
            "including actual perturbation statistics."
        )
    )

    parser.add_argument(
        "--project-dir",
        type=str,
        default=".",
        help="Calibration project directory",
    )

    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve()
    results_dir = project_dir / "04_results"
    reports_dir = project_dir / "05_reports"
    input_dir = project_dir / "01_input"

    write_parameter_importance_report(
        sensitivity_results_file=results_dir / "sensitivity_results_V10.xlsx",
        process_summary_file=results_dir / "process_sensitivity_summary_V10.xlsx",
        interpretation_file=results_dir / "sensitivity_interpretation_V10.xlsx",
        sensitivity_coefficients_file=results_dir / "sensitivity_coefficients_V10.xlsx",
        parameters_file=input_dir / "agent_config.xlsx",
        importance_file=results_dir / "parameter_importance_V10.xlsx",
        report_file=reports_dir / "parameter_importance_V10.md",
        project_name=project_dir.name,
    )


if __name__ == "__main__":
    main()
