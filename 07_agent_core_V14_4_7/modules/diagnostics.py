from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from .io_utils import safe_float, timestamp


def relative_bias(row: pd.Series, species: str) -> float | None:
    b = safe_float(row.get(f"Bias_{species}"))
    m = safe_float(row.get(f"Mean_obs_{species}"))
    if b is None or m is None or abs(m) <= 0:
        return None
    return b / abs(m)


def geochemical_diagnosis(row: pd.Series | None) -> pd.DataFrame:
    findings: List[Dict[str, Any]] = []

    def add(priority: int, issue: str, interpretation: str, candidates: List[str], direction: str) -> None:
        findings.append({
            "timestamp": timestamp(), "priority": priority, "issue": issue,
            "interpretation": interpretation, "candidate_parameters": ", ".join(candidates),
            "recommended_direction": direction,
        })

    if row is None:
        add(1, "No calibration history", "Run baseline or sensitivity analysis before calibration.", [], "run_baseline")
        return pd.DataFrame(findings)

    pH_bias = safe_float(row.get("Bias_pH"))
    so4_rel = relative_bias(row, "so4-2")
    status = str(row.get("run_status", "")).lower()
    warnings = str(row.get("warnings", "")).lower()

    if status not in ["success", "success_with_retries", "partial_success", ""]:
        add(1, "Run not numerically trustworthy", "Fix model execution/stability before interpreting RMSE.", ["time_step_controls", "database", "initial_solution"], "fix_numerics_first")
    if "charge_balance" in warnings:
        add(1, "Charge-balance warning", "Initial solution or database/speciation setup may be inconsistent.", ["initial_solution", "redox", "basis_species"], "review_chemistry")

    if pH_bias is not None:
        if pH_bias > 0.40:
            add(1, "pH overpredicted", "Model is too alkaline: buffering too strong or acid generation/O2 supply too weak.", ["keff_calcite", "keff_dolomite", "keff_magnesite", "keff_pyrite", "gas_diff"], "decrease_buffering_or_increase_acid_generation")
        elif pH_bias < -0.40:
            add(1, "pH underpredicted", "Model is too acidic: acid generation too strong or neutralization too weak.", ["keff_calcite", "keff_dolomite", "keff_magnesite", "keff_pyrite"], "increase_buffering_or_decrease_acid_generation")

    if so4_rel is not None:
        if so4_rel < -0.20:
            add(1, "SO4 underpredicted", "Sulfate source, sulfide oxidation, O2 supply, or gypsum contribution is too weak.", ["keff_pyrite", "keff_pyrrhot", "keff_sphalerite", "gas_diff", "gypsum_amount"], "increase_sulfate_source_or_o2_supply")
        elif so4_rel > 0.20:
            add(1, "SO4 overpredicted", "Sulfate generation is too strong or dilution/residence time is wrong.", ["keff_pyrite", "keff_pyrrhot", "top_flux", "gypsum_amount"], "decrease_sulfate_source_or_review_flow")

    pH_ok = pH_bias is None or abs(pH_bias) < 0.50
    so4_ok = so4_rel is None or abs(so4_rel) < 0.35
    metal_map = {
        "zn+2": ["keff_sphalerite", "sphalerite_amount", "ferrihydrite", "sorption_sites"],
        "cu+2": ["keff_chalcopyr", "chalcopyrite_amount", "sorption_sites"],
        "pb+2": ["keff_galena", "galena_amount", "anglesite", "sorption_sites"],
        "cd+2": ["keff_sphalerite", "sphalerite_amount", "otavite", "sorption_sites"],
        "al+3": ["gibbsite", "aloh3", "pH_buffering", "sorption_sites"],
        "mg+2": ["s_chlorite", "keff_magnesite", "keff_dolomite"],
    }
    for sp, candidates in metal_map.items():
        b = safe_float(row.get(f"Bias_{sp}"))
        if b is None:
            continue
        priority = 3 if pH_ok and so4_ok else 5
        if b > 0:
            add(priority, f"{sp} overpredicted", f"{sp} source/mobility is too high; postpone fine tuning if pH/SO4 remain biased.", candidates, "decrease_source_or_increase_removal")
        elif b < 0:
            add(priority, f"{sp} underpredicted", f"{sp} source is too weak or removal too strong; postpone fine tuning if pH/SO4 remain biased.", candidates, "increase_source_or_decrease_removal")

    if not findings:
        add(10, "No strong deterministic mismatch", "Continue sensitivity analysis or visual time-series review.", [], "review_timeseries")
    return pd.DataFrame(findings).sort_values(["priority", "issue"])


def runtime_state(row: pd.Series | None, history: pd.DataFrame | None = None) -> Dict[str, Any]:
    """
    Runtime controller used by the deterministic suggestion engine.

    TP3-specific policy:
    - failed_time_steps / total_time_steps behaves like a retry ratio, not a true
      fraction. It can exceed 1.0 because MIN3P may retry one accepted timestep
      multiple times.
    - Therefore, do NOT stop automatic suggestions only because of this retry
      ratio.
    - Use the retry ratio to make changes conservative.
    - Hard stop is reserved for extreme CPU growth, not failed-step fraction alone.
    """
    if row is None:
        return {
            "runtime_risk": "NO_HISTORY",
            "force_single_change": True,
            "max_up": 1.05,
            "max_down": 0.95,
            "stop_auto": False,
            "reason": "No row available",
        }

    failed = safe_float(row.get("failed_time_steps"))
    total = safe_float(row.get("total_time_steps"))
    cpu = safe_float(row.get("cpu_time_sec"))

    # This is a retry ratio, not a bounded fraction.
    frac = failed / total if failed is not None and total not in [None, 0] else None

    ratio = None
    if history is not None and not history.empty and "cpu_time_sec" in history.columns:
        cpus = pd.to_numeric(history["cpu_time_sec"], errors="coerce").dropna().tail(10)
        cpus = cpus[cpus > 0]
        if cpu is not None and not cpus.empty:
            ratio = cpu / float(cpus.median())

    risk = "LOW"
    reasons = []

    # Failed-step retry ratio makes the next step conservative, but does not stop auto.
    if frac is not None:
        if frac > 2.0:
            risk = "HIGH"
            reasons.append(f"failed timestep retry ratio is extreme: {frac:.1%}")
        elif frac > 0.40:
            risk = "MODERATE"
            reasons.append(f"failed timestep retry ratio is high: {frac:.1%}")

    # CPU explosion can still stop automation.
    stop_auto = False
    if ratio is not None:
        if ratio > 5:
            risk = "CRITICAL"
            stop_auto = True
            reasons.append(f"CPU ratio {ratio:.2f}x recent median")
        elif ratio > 3 and risk not in ["CRITICAL"]:
            risk = "HIGH"
            reasons.append(f"CPU ratio {ratio:.2f}x recent median")
        elif ratio > 1.5 and risk == "LOW":
            risk = "MODERATE"
            reasons.append(f"CPU ratio {ratio:.2f}x recent median")

    return {
        "runtime_risk": risk,
        "failed_step_fraction": frac,
        "cpu_ratio_to_median": ratio,
        "force_single_change": risk in ["MODERATE", "HIGH", "CRITICAL"],
        "max_up": 1.02 if risk == "CRITICAL" else 1.05 if risk == "HIGH" else 1.08 if risk == "MODERATE" else 1.12,
        "max_down": 0.98 if risk == "CRITICAL" else 0.95 if risk == "HIGH" else 0.92 if risk == "MODERATE" else 0.88,
        "stop_auto": stop_auto,
        "reason": "; ".join(reasons) if reasons else "Runtime indicators acceptable",
    }
