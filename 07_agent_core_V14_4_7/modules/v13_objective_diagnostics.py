from __future__ import annotations

"""V13.4 objective, species-trade-off, and scientific-constraint diagnostics."""

from datetime import datetime
from pathlib import Path
from typing import Any
import pandas as pd


DIAGNOSTIC_FILE = "v13_4_objective_diagnostics.xlsx"


def _float(value: Any, default=None):
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def _read(path: Path) -> pd.DataFrame:
    try:
        return pd.read_excel(path) if path.exists() else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _match_run(df: pd.DataFrame, run_folder: str) -> pd.Series | None:
    if df.empty or "run_folder" not in df.columns:
        return None
    run_name = Path(str(run_folder)).name
    matches = df[df["run_folder"].astype(str).str.contains(run_name, regex=False, na=False)]
    return None if matches.empty else matches.iloc[0]


def rmse_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns if str(c).upper().startswith("RMSE_")]


def evaluate_scientific_constraints(candidate_row: pd.Series | None, config) -> dict[str, Any]:
    """Read optional sheet `v13_scientific_constraints`.

    Columns: metric | min_value | max_value | hard | penalty_weight.
    A hard violation rejects the candidate. A soft violation adds a penalty.
    """
    output = {
        "scientific_ok": True,
        "scientific_penalty": 0.0,
        "constraint_failures": "",
        "constraint_count": 0,
    }
    if candidate_row is None:
        return output
    try:
        rules = config.sheet("v13_scientific_constraints")
    except Exception:
        rules = pd.DataFrame()
    if rules.empty or "metric" not in rules.columns:
        return output

    failures: list[str] = []
    penalty = 0.0
    for _, rule in rules.iterrows():
        metric = str(rule.get("metric", "")).strip()
        if not metric or metric not in candidate_row.index:
            continue
        value = _float(candidate_row.get(metric))
        low, high = _float(rule.get("min_value")), _float(rule.get("max_value"))
        if value is None:
            continue
        violation = max((low - value) if low is not None else 0.0, (value - high) if high is not None else 0.0, 0.0)
        if violation <= 0:
            continue
        hard = str(rule.get("hard", "yes")).strip().lower() in {"1", "true", "yes", "y"}
        weight = _float(rule.get("penalty_weight"), 0.0) or 0.0
        failures.append(f"{metric}={value:g}")
        penalty += violation * weight
        if hard:
            output["scientific_ok"] = False

    output["scientific_penalty"] = penalty
    output["constraint_failures"] = "; ".join(failures)
    output["constraint_count"] = len(failures)
    return output


def write_candidate_diagnostics(
    *,
    results_dir: str | Path,
    ranking_file: str | Path,
    baseline_run_folder: str,
    candidate_run_folder: str,
    baseline_total_score: float | None,
    candidate_total_score: float | None,
    parameter: str,
    group: str,
    direction: str,
    step_fraction: float | None,
    accepted: bool,
    decision: str,
    rejection_reason: str,
    config,
) -> dict[str, Any]:
    """Append immutable species-level audit rows after final candidate decision."""
    results_dir = Path(results_dir)
    ranking = _read(Path(ranking_file))
    baseline = _match_run(ranking, baseline_run_folder)
    candidate = _match_run(ranking, candidate_run_folder)
    constraints = evaluate_scientific_constraints(candidate, config)

    rows: list[dict[str, Any]] = []
    improved = worsened = 0
    if baseline is not None and candidate is not None:
        for metric in sorted(set(rmse_columns(ranking))):
            before, after = _float(baseline.get(metric)), _float(candidate.get(metric))
            if before is None or after is None:
                continue
            delta = after - before
            effect = "improved" if delta < 0 else ("worsened" if delta > 0 else "unchanged")
            improved += int(effect == "improved")
            worsened += int(effect == "worsened")
            rows.append({
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "baseline_run_folder": baseline_run_folder,
                "candidate_run_folder": candidate_run_folder,
                "parameter": parameter,
                "group": group,
                "direction": direction,
                "step_fraction": step_fraction,
                "accepted": bool(accepted),
                "decision": decision,
                "rejection_reason": rejection_reason,
                "baseline_TOTAL_SCORE": baseline_total_score,
                "candidate_TOTAL_SCORE": candidate_total_score,
                "metric": metric,
                "baseline_rmse": before,
                "candidate_rmse": after,
                "delta_rmse": delta,
                "effect": effect,
            })

    if improved and not worsened:
        tradeoff = "globally_consistent_improvement"
    elif improved and worsened:
        tradeoff = "species_tradeoff"
    elif worsened:
        tradeoff = "global_degradation"
    else:
        tradeoff = "no_metric_detail_available"

    output = results_dir / DIAGNOSTIC_FILE
    frame = pd.DataFrame(rows)
    if not frame.empty:
        existing = _read(output)
        pd.concat([existing, frame], ignore_index=True).to_excel(output, index=False)

    return {
        "diagnostic_file": str(output),
        "species_improved_count": improved,
        "species_worsened_count": worsened,
        "tradeoff_class": tradeoff,
        **constraints,
    }
