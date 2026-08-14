from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import List

import numpy as np
import pandas as pd


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


def build_parameter_change_history(
    history: pd.DataFrame,
    parameters: pd.DataFrame,
) -> pd.DataFrame:
    if history.empty or parameters.empty or "parameter" not in parameters.columns:
        return pd.DataFrame()

    param_names = [
        str(p).strip()
        for p in parameters["parameter"].dropna().tolist()
        if str(p).strip() in history.columns
    ]

    rows = []

    for i in range(1, len(history)):
        previous = history.iloc[i - 1]
        current = history.iloc[i]

        for p in param_names:
            old = _safe_float(previous.get(p))
            new = _safe_float(current.get(p))

            if old is None or new is None:
                continue

            if old == new:
                continue

            factor = new / old if old != 0 else None
            percent_change = 100.0 * (new - old) / old if old != 0 else None

            rows.append(
                {
                    "step": i,
                    "timestamp": current.get("timestamp"),
                    "run_folder": current.get("run_folder"),
                    "parameter": p,
                    "old_value": old,
                    "new_value": new,
                    "factor": factor,
                    "percent_change": percent_change,
                    "run_status": current.get("run_status"),
                    "RMSE_pH": current.get("RMSE_pH"),
                    "RMSE_so4-2": current.get("RMSE_so4-2"),
                    "RMSE_zn+2": current.get("RMSE_zn+2"),
                    "Bias_pH": current.get("Bias_pH"),
                    "Bias_so4-2": current.get("Bias_so4-2"),
                    "Bias_zn+2": current.get("Bias_zn+2"),
                }
            )

    return pd.DataFrame(rows)


def build_calibration_pathway(
    history: pd.DataFrame,
    ranking: pd.DataFrame,
) -> pd.DataFrame:
    if history.empty:
        return pd.DataFrame()

    rows = []
    best_score_so_far = None

    for i, row in history.iterrows():
        total_score = _safe_float(row.get("TOTAL_SCORE"))

        if (
            total_score is None
            and "TOTAL_SCORE" in ranking.columns
            and "run_folder" in ranking.columns
        ):
            match = ranking[
                ranking["run_folder"].astype(str)
                == str(row.get("run_folder"))
            ]

            if not match.empty:
                total_score = _safe_float(match.iloc[0].get("TOTAL_SCORE"))

        if total_score is not None:
            if best_score_so_far is None or total_score < best_score_so_far:
                best_score_so_far = total_score
                improvement_status = "new_best"
            else:
                improvement_status = "not_best"
        else:
            improvement_status = "unknown"

        rows.append(
            {
                "step": i + 1,
                "timestamp": row.get("timestamp"),
                "run_folder": row.get("run_folder"),
                "run_status": row.get("run_status"),
                "error_type": row.get("error_type"),
                "run_health_score": row.get("run_health_score"),
                "TOTAL_SCORE": total_score,
                "improvement_status": improvement_status,
                "RMSE_pH": row.get("RMSE_pH"),
                "Bias_pH": row.get("Bias_pH"),
                "RMSE_so4-2": row.get("RMSE_so4-2"),
                "Bias_so4-2": row.get("Bias_so4-2"),
                "RMSE_zn+2": row.get("RMSE_zn+2"),
                "Bias_zn+2": row.get("Bias_zn+2"),
                "RMSE_cu+2": row.get("RMSE_cu+2"),
                "RMSE_pb+2": row.get("RMSE_pb+2"),
                "RMSE_cd+2": row.get("RMSE_cd+2"),
            }
        )

    return pd.DataFrame(rows)


def build_parameter_importance(
    sensitivity_coefficients: pd.DataFrame,
    parameter_change_history: pd.DataFrame,
) -> pd.DataFrame:
    rows = []

    if (
        not sensitivity_coefficients.empty
        and "parameter" in sensitivity_coefficients.columns
    ):
        df = sensitivity_coefficients.copy()

        sens_col = None

        for candidate in [
            "abs_normalized_sensitivity",
            "abs_sensitivity",
            "normalized_sensitivity",
        ]:
            if candidate in df.columns:
                sens_col = candidate
                break

        if sens_col:
            df[sens_col] = pd.to_numeric(
                df[sens_col],
                errors="coerce",
            ).abs()

            grouped = (
                df.groupby("parameter", as_index=False)
                .agg(
                    max_abs_sensitivity=(sens_col, "max"),
                    mean_abs_sensitivity=(sens_col, "mean"),
                    n_sensitive_metrics=(
                        ("metric", "nunique")
                        if "metric" in df.columns
                        else (sens_col, "count")
                    ),
                )
            )

            for _, r in grouped.iterrows():
                rows.append(
                    {
                        "parameter": r["parameter"],
                        "max_abs_sensitivity": r["max_abs_sensitivity"],
                        "mean_abs_sensitivity": r["mean_abs_sensitivity"],
                        "n_sensitive_metrics": r["n_sensitive_metrics"],
                        "n_parameter_changes": 0,
                        "importance_score": r["max_abs_sensitivity"],
                        "evidence_type": "sensitivity",
                    }
                )

    out = pd.DataFrame(rows)

    if (
        not parameter_change_history.empty
        and "parameter" in parameter_change_history.columns
    ):
        change_counts = (
            parameter_change_history
            .groupby("parameter", as_index=False)
            .agg(n_parameter_changes=("parameter", "count"))
        )

        if out.empty:
            out = change_counts.copy()
            out["max_abs_sensitivity"] = np.nan
            out["mean_abs_sensitivity"] = np.nan
            out["n_sensitive_metrics"] = np.nan
            out["importance_score"] = out["n_parameter_changes"]
            out["evidence_type"] = "change_history"

        else:
            out = out.merge(
                change_counts,
                on="parameter",
                how="outer",
                suffixes=("", "_from_changes"),
            )

            out["n_parameter_changes"] = (
                out["n_parameter_changes_from_changes"]
                .fillna(out["n_parameter_changes"])
                .fillna(0)
            )

            out = out.drop(
                columns=[
                    c
                    for c in ["n_parameter_changes_from_changes"]
                    if c in out.columns
                ]
            )

            out["importance_score"] = (
                pd.to_numeric(
                    out["max_abs_sensitivity"],
                    errors="coerce",
                ).fillna(0)
                + 0.1
                * pd.to_numeric(
                    out["n_parameter_changes"],
                    errors="coerce",
                ).fillna(0)
            )

            out["evidence_type"] = "sensitivity_and_change_history"

    if not out.empty:
        out = out.sort_values(
            "importance_score",
            ascending=False,
        )

    return out


def build_species_control_summary(
    sensitivity_coefficients: pd.DataFrame,
    focus_metrics: List[str] | None = None,
    top_n: int = 10,
) -> pd.DataFrame:
    if sensitivity_coefficients.empty:
        return pd.DataFrame()

    if "metric" not in sensitivity_coefficients.columns:
        return pd.DataFrame()

    if "parameter" not in sensitivity_coefficients.columns:
        return pd.DataFrame()

    df = sensitivity_coefficients.copy()

    sens_col = None

    for candidate in [
        "abs_normalized_sensitivity",
        "abs_sensitivity",
        "normalized_sensitivity",
    ]:
        if candidate in df.columns:
            sens_col = candidate
            break

    if sens_col is None:
        return pd.DataFrame()

    df[sens_col] = pd.to_numeric(df[sens_col], errors="coerce").abs()

    focus_metrics = focus_metrics or [
        "Bias_pH",
        "RMSE_pH",
        "Bias_so4-2",
        "RMSE_so4-2",
        "Bias_zn+2",
        "RMSE_zn+2",
        "Bias_cu+2",
        "RMSE_cu+2",
        "Bias_pb+2",
        "RMSE_pb+2",
        "Bias_cd+2",
        "RMSE_cd+2",
        "Bias_al+3",
        "RMSE_al+3",
    ]

    df = df[df["metric"].astype(str).isin(focus_metrics)].copy()

    if df.empty:
        return pd.DataFrame()

    df["species"] = (
        df["metric"]
        .astype(str)
        .str.replace(r"^(RMSE_|MAE_|Bias_)", "", regex=True)
    )

    grouped = (
        df.groupby(["species", "parameter"], as_index=False)
        .agg(
            max_abs_sensitivity=(sens_col, "max"),
            mean_abs_sensitivity=(sens_col, "mean"),
            n_metrics=("metric", "nunique"),
        )
    )

    grouped = grouped.sort_values(
        ["species", "max_abs_sensitivity"],
        ascending=[True, False],
    )

    grouped["rank_within_species"] = (
        grouped.groupby("species")["max_abs_sensitivity"]
        .rank(method="first", ascending=False)
        .astype(int)
    )

    return grouped[grouped["rank_within_species"] <= top_n].copy()


def _bias_sentence(species: str, bias_value) -> str:
    b = _safe_float(bias_value)

    if b is None:
        return f"{species}: no bias value available."

    if species == "pH":
        if b > 0:
            return f"{species}: model is too alkaline by {b:.3g} pH units."
        return f"{species}: model is too acidic by {abs(b):.3g} pH units."

    if b > 0:
        return f"{species}: model overpredicts observations by {b:.3e}."

    return f"{species}: model underpredicts observations by {abs(b):.3e}."


def write_calibration_story_report(
    project_dir: Path,
    history_file: Path,
    ranking_file: Path,
    parameters_file: Path,
    sensitivity_coefficients_file: Path,
    reports_dir: Path,
    results_dir: Path,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    history = _read_excel(history_file)
    ranking = _read_excel(ranking_file)
    sensitivity = _read_excel(sensitivity_coefficients_file)

    try:
        parameters = pd.read_excel(
            parameters_file,
            sheet_name="parameters",
        )
    except Exception:
        parameters = pd.DataFrame()

    parameter_change_history = build_parameter_change_history(
        history,
        parameters,
    )

    calibration_pathway = build_calibration_pathway(
        history,
        ranking,
    )

    parameter_importance = build_parameter_importance(
        sensitivity,
        parameter_change_history,
    )

    species_control_summary = build_species_control_summary(
        sensitivity,
    )

    change_file = results_dir / "parameter_change_history_V9_2.xlsx"
    pathway_file = results_dir / "calibration_pathway_V9_2.xlsx"
    importance_file = results_dir / "parameter_importance_V9_2.xlsx"
    species_control_file = results_dir / "species_control_summary_V9_2.xlsx"

    parameter_change_history.to_excel(change_file, index=False)
    calibration_pathway.to_excel(pathway_file, index=False)
    parameter_importance.to_excel(importance_file, index=False)
    species_control_summary.to_excel(species_control_file, index=False)

    report_file = reports_dir / "calibration_story_V9_2.md"
    summary_file = reports_dir / "calibration_story_summary_V9_2.txt"

    lines = []

    lines.append("# MIN3P Calibration Story Report — V9.2")
    lines.append("")
    lines.append(f"Generated: {dt.datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Project: `{project_dir.name}`")
    lines.append("")

    lines.append("## 1. Purpose")
    lines.append("")
    lines.append(
        "This report summarizes the scientific calibration pathway. "
        "It explains how model-observation mismatch evolved, which parameters were tested, "
        "which parameters controlled the calibration targets, and what should be tested next."
    )
    lines.append("")

    if history.empty:
        lines.append(
            "No optimization history was found. "
            "Run at least one baseline model before creating the calibration story."
        )
        report_file.write_text("\n".join(lines), encoding="utf-8")
        summary_file.write_text("No optimization history found.", encoding="utf-8")
        return report_file

    first = history.iloc[0]
    latest = history.iloc[-1]

    if not ranking.empty:
        if "TOTAL_SCORE" in ranking.columns:
            ranking = ranking.sort_values("TOTAL_SCORE")
        best = ranking.iloc[0]
    else:
        best = latest

    lines.append("## 2. Initial model behavior")
    lines.append("")
    lines.append(f"- First run status: `{first.get('run_status', 'unknown')}`")
    lines.append(f"- First run folder: `{first.get('run_folder', '')}`")
    lines.append(f"- {_bias_sentence('pH', first.get('Bias_pH'))}")
    lines.append(f"- {_bias_sentence('SO4', first.get('Bias_so4-2'))}")
    lines.append(f"- {_bias_sentence('Zn', first.get('Bias_zn+2'))}")
    lines.append("")

    lines.append("## 3. Latest model behavior")
    lines.append("")
    lines.append(f"- Latest run status: `{latest.get('run_status', 'unknown')}`")
    lines.append(f"- Latest run folder: `{latest.get('run_folder', '')}`")
    lines.append(f"- {_bias_sentence('pH', latest.get('Bias_pH'))}")
    lines.append(f"- {_bias_sentence('SO4', latest.get('Bias_so4-2'))}")
    lines.append(f"- {_bias_sentence('Zn', latest.get('Bias_zn+2'))}")
    lines.append("")

    lines.append("## 4. Best run so far")
    lines.append("")
    lines.append(f"- Best run folder: `{best.get('run_folder', '')}`")
    lines.append(f"- Run status: `{best.get('run_status', 'unknown')}`")

    if "TOTAL_SCORE" in best.index:
        lines.append(f"- Total score: `{best.get('TOTAL_SCORE')}`")

    for key in [
        "RMSE_pH",
        "RMSE_so4-2",
        "RMSE_zn+2",
        "RMSE_cu+2",
        "RMSE_pb+2",
        "RMSE_cd+2",
    ]:
        if key in best.index:
            lines.append(f"- {key}: `{best.get(key)}`")

    lines.append("")

    lines.append("## 5. Calibration pathway")
    lines.append("")

    if calibration_pathway.empty:
        lines.append("No calibration pathway could be reconstructed.")
    else:
        show_cols = [
            c
            for c in [
                "step",
                "run_status",
                "improvement_status",
                "RMSE_pH",
                "Bias_pH",
                "RMSE_so4-2",
                "Bias_so4-2",
                "RMSE_zn+2",
                "Bias_zn+2",
            ]
            if c in calibration_pathway.columns
        ]

        lines.append(calibration_pathway[show_cols].to_markdown(index=False))

    lines.append("")

    lines.append("## 6. Parameter changes")
    lines.append("")

    if parameter_change_history.empty:
        lines.append("No parameter changes were detected in the history.")
    else:
        show_cols = [
            c
            for c in [
                "step",
                "parameter",
                "old_value",
                "new_value",
                "factor",
                "percent_change",
                "RMSE_pH",
                "RMSE_so4-2",
                "RMSE_zn+2",
            ]
            if c in parameter_change_history.columns
        ]

        lines.append(parameter_change_history[show_cols].tail(20).to_markdown(index=False))

    lines.append("")

    lines.append("## 7. Parameter importance")
    lines.append("")

    if parameter_importance.empty:
        lines.append(
            "No parameter-importance ranking could be created. "
            "Run sensitivity analysis first."
        )
    else:
        show_cols = [
            c
            for c in [
                "parameter",
                "importance_score",
                "max_abs_sensitivity",
                "mean_abs_sensitivity",
                "n_sensitive_metrics",
                "n_parameter_changes",
                "evidence_type",
            ]
            if c in parameter_importance.columns
        ]

        lines.append(parameter_importance[show_cols].head(20).to_markdown(index=False))

    lines.append("")

    lines.append("## 8. Species-specific sensitivity controls")
    lines.append("")

    if species_control_summary.empty:
        lines.append(
            "No species-specific sensitivity summary could be created."
        )
    else:
        for species in sorted(species_control_summary["species"].dropna().unique()):
            sub = species_control_summary[
                species_control_summary["species"].astype(str) == str(species)
            ].copy()

            sub = sub.sort_values("rank_within_species")

            show_cols = [
                c
                for c in [
                    "rank_within_species",
                    "parameter",
                    "max_abs_sensitivity",
                    "mean_abs_sensitivity",
                    "n_metrics",
                ]
                if c in sub.columns
            ]

            lines.append(f"### {species}")
            lines.append("")
            lines.append(sub[show_cols].to_markdown(index=False))
            lines.append("")

    lines.append("## 9. Scientific interpretation")
    lines.append("")
    lines.append("- pH and sulfate should remain the first-order calibration targets.")
    lines.append(
        "- Trace-metal calibration should be interpreted only after pH and sulfate "
        "are reasonably represented."
    )
    lines.append(
        "- Parameters with high sensitivity and repeated successful use are the "
        "strongest calibration levers."
    )
    lines.append(
        "- Parameters that increase runtime risk or failed timestep fraction should "
        "be changed conservatively."
    )
    lines.append("")

    lines.append("## 10. Recommended next step")
    lines.append("")

    if parameter_importance.empty:
        lines.append(
            "Run deterministic sensitivity analysis before further automatic calibration."
        )
    else:
        top = parameter_importance.iloc[0]
        lines.append(
            f"The next calibration step should focus on `{top.get('parameter')}`, "
            "but only if the direction of change is consistent with the current "
            "pH/SO4 bias."
        )

    lines.append("")

    lines.append("## 11. Files created")
    lines.append("")
    lines.append(f"- `{change_file}`")
    lines.append(f"- `{pathway_file}`")
    lines.append(f"- `{importance_file}`")
    lines.append(f"- `{species_control_file}`")
    lines.append("")

    report_file.write_text("\n".join(lines), encoding="utf-8")

    summary_lines = [
        f"Project: {project_dir.name}",
        f"Number of history rows: {len(history)}",
        f"Best run: {best.get('run_folder', '')}",
        f"Latest run: {latest.get('run_folder', '')}",
        f"Story report: {report_file}",
    ]

    if not parameter_importance.empty:
        summary_lines.append("Top parameters:")

        for _, r in parameter_importance.head(5).iterrows():
            summary_lines.append(
                f"- {r.get('parameter')}: "
                f"importance_score={r.get('importance_score')}"
            )

    summary_file.write_text("\n".join(summary_lines), encoding="utf-8")

    return report_file