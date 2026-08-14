from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import List

import pandas as pd
import numpy as np


def _safe_float(x):
    try:
        v = float(x)
        if np.isnan(v) or np.isinf(v):
            return None
        return v
    except Exception:
        return None


def _read_excel(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_excel(path)
    except Exception:
        return pd.DataFrame()


def build_process_sensitivity_summary(
    sensitivity_results: pd.DataFrame,
) -> pd.DataFrame:
    if sensitivity_results.empty:
        return pd.DataFrame()

    if "sensitivity_role" not in sensitivity_results.columns:
        return pd.DataFrame()

    base_rows = sensitivity_results[
        sensitivity_results["sensitivity_role"].astype(str) == "baseline"
    ]

    if base_rows.empty:
        return pd.DataFrame()

    baseline = base_rows.iloc[-1]

    model_cols = [
        c for c in sensitivity_results.columns
        if str(c).startswith("Mean_model_")
    ]

    if not model_cols:
        return pd.DataFrame()

    rows = []

    perturbed = sensitivity_results[
        sensitivity_results["sensitivity_role"].astype(str) == "perturbation"
    ]

    for _, r in perturbed.iterrows():
        parameter = r.get("parameter")
        case = r.get("sensitivity_case", r.get("multiplier"))
        base_value = _safe_float(r.get("base_value"))
        test_value = _safe_float(r.get("test_value"))
        rel_param_change = _safe_float(r.get("relative_parameter_change"))

        for col in model_cols:
            species = str(col).replace("Mean_model_", "")

            base_model = _safe_float(baseline.get(col))
            test_model = _safe_float(r.get(col))

            if base_model is None or test_model is None:
                continue

            delta = test_model - base_model

            if abs(base_model) > 1e-30:
                percent_change = 100.0 * delta / abs(base_model)
                normalized_response = (
                    (delta / abs(base_model)) / rel_param_change
                    if rel_param_change not in [None, 0]
                    else None
                )
            else:
                percent_change = None
                normalized_response = None

            rows.append(
                {
                    "parameter": parameter,
                    "sensitivity_case": case,
                    "species": species,
                    "base_parameter_value": base_value,
                    "test_parameter_value": test_value,
                    "relative_parameter_change": rel_param_change,
                    "base_model_value": base_model,
                    "test_model_value": test_model,
                    "delta_model_value": delta,
                    "percent_model_change": percent_change,
                    "normalized_process_sensitivity": normalized_response,
                    "abs_normalized_process_sensitivity": (
                        abs(normalized_response)
                        if normalized_response is not None
                        else None
                    ),
                    "run_status": r.get("run_status"),
                    "run_folder": r.get("run_folder"),
                }
            )

    out = pd.DataFrame(rows)

    if not out.empty:
        out = out.sort_values(
            "abs_normalized_process_sensitivity",
            ascending=False,
        )

    return out


def _top_table(
    df: pd.DataFrame,
    species: str,
    n: int = 10,
) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    sub = df[df["species"].astype(str).str.lower() == species.lower()].copy()

    if sub.empty:
        return pd.DataFrame()

    sub = sub.sort_values(
        "abs_normalized_process_sensitivity",
        ascending=False,
    )

    cols = [
        "parameter",
        "sensitivity_case",
        "base_model_value",
        "test_model_value",
        "delta_model_value",
        "percent_model_change",
        "normalized_process_sensitivity",
    ]

    cols = [c for c in cols if c in sub.columns]

    return sub[cols].head(n)


def write_process_sensitivity_report(
    sensitivity_results_file: Path,
    summary_file: Path,
    report_file: Path,
    project_name: str,
    focus_species: List[str] | None = None,
) -> Path:
    focus_species = focus_species or [
        "pH",
        "so4-2",
        "zn+2",
        "cu+2",
        "pb+2",
        "cd+2",
        "al+3",
        "ca+2",
    ]

    sensitivity_results = _read_excel(sensitivity_results_file)

    summary_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.parent.mkdir(parents=True, exist_ok=True)

    summary = build_process_sensitivity_summary(sensitivity_results)
    summary.to_excel(summary_file, index=False)

    lines = []

    lines.append("# MIN3P Process Sensitivity Report — V9.0")
    lines.append("")
    lines.append(f"Generated: {dt.datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Project: `{project_name}`")
    lines.append("")

    lines.append("## 1. Purpose")
    lines.append("")
    lines.append(
        "This report evaluates how each parameter changes the model outputs directly. "
        "It does not require observed data. It compares each perturbation run against "
        "the baseline run using model-predicted values such as pH, sulfate, and metals."
    )
    lines.append("")

    if sensitivity_results.empty:
        lines.append("No sensitivity results file was found.")
        report_file.write_text("\n".join(lines), encoding="utf-8")
        return report_file

    if summary.empty:
        lines.append(
            "No process sensitivity summary could be created. "
            "Check that `sensitivity_results_V9_0.xlsx` contains `Mean_model_*` columns."
        )
        report_file.write_text("\n".join(lines), encoding="utf-8")
        return report_file

    lines.append("## 2. Strongest overall process controls")
    lines.append("")

    show_cols = [
        "parameter",
        "sensitivity_case",
        "species",
        "percent_model_change",
        "normalized_process_sensitivity",
        "run_status",
    ]

    show_cols = [c for c in show_cols if c in summary.columns]

    lines.append(summary[show_cols].head(20).to_markdown(index=False))
    lines.append("")

    lines.append("## 3. Main species-specific controls")
    lines.append("")

    for species in focus_species:
        table = _top_table(summary, species, n=10)

        if table.empty:
            continue

        lines.append(f"### {species}")
        lines.append("")
        lines.append(table.to_markdown(index=False))
        lines.append("")

    lines.append("## 4. Interpretation guide")
    lines.append("")
    lines.append("- Positive response means the model output increased when the parameter changed.")
    lines.append("- Negative response means the model output decreased when the parameter changed.")
    lines.append("- Large absolute normalized sensitivity means the parameter strongly controls that model output.")
    lines.append("- This report is independent of observations and describes model behavior only.")
    lines.append("")

    lines.append("## 5. Files created")
    lines.append("")
    lines.append(f"- `{summary_file}`")
    lines.append(f"- `{report_file}`")
    lines.append("")

    report_file.write_text("\n".join(lines), encoding="utf-8")

    return report_file