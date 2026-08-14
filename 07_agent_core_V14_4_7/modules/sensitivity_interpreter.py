from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Dict, List

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


def _classify_parameter(parameter: str) -> str:
    p = str(parameter).lower()

    if "pyrite" in p or "pyrrhot" in p:
        return "sulfide oxidation / acid generation"

    if "calcite" in p or "dolomite" in p or "magnesite" in p:
        return "carbonate buffering / neutralization"

    if "sphalerite" in p:
        return "Zn/Cd sulfide source"

    if "galena" in p:
        return "Pb sulfide source"

    if "chalcopyr" in p:
        return "Cu sulfide source"

    if "feoh" in p or "ferrihydrite" in p or "sorption" in p:
        return "sorption / secondary Fe phases"

    if p in ["top_flux", "bottom_head", "kz", "porosity"] or "flux" in p or "head" in p:
        return "hydrologic transport / residence time"

    if "vg_" in p or "residual" in p:
        return "unsaturated flow / moisture distribution"

    if p.startswith("init_") or p.startswith("bc_"):
        return "initial or boundary chemistry"

    if "scaling_ox" in p or "oxygen" in p or "po2" in p:
        return "oxygen supply / redox control"

    return "other"


def _dominant_process_sentence(species: str, top_params: List[str]) -> str:
    params = " ".join([str(p).lower() for p in top_params])

    if species == "pH":
        if "pyrite" in params and "calcite" in params:
            return (
                "pH is mainly controlled by the balance between sulfide-driven acid generation "
                "and carbonate buffering."
            )
        if "pyrite" in params:
            return "pH is mainly controlled by sulfide oxidation and acid generation."
        if "calcite" in params:
            return "pH is mainly controlled by carbonate buffering."

    if species in ["so4-2", "so4", "sulfate"]:
        if "pyrite" in params or "pyrrhot" in params:
            return "Sulfate is mainly controlled by sulfide oxidation, especially pyrite-related parameters."
        return "Sulfate is controlled by a combination of source strength and transport."

    if species in ["zn+2", "zn"]:
        if "sphalerite" in params:
            return "Zn is mainly controlled by sphalerite kinetics and hydrologic transport."
        return "Zn is controlled by acidity, transport, and removal processes."

    if species in ["cd+2", "cd"]:
        if "sphalerite" in params:
            return "Cd is mainly linked to sphalerite behavior, consistent with Cd substitution in sphalerite."
        return "Cd is controlled by metal source strength, acidity, and transport."

    if species in ["pb+2", "pb"]:
        if "galena" in params:
            return "Pb is mainly controlled by galena kinetics and hydrologic transport."
        return "Pb is controlled by source strength, transport, and sorption/removal."

    if species in ["cu+2", "cu"]:
        if "chalcopyr" in params:
            return "Cu is mainly controlled by chalcopyrite kinetics."
        return "Cu is controlled by sulfide oxidation, sorption, and transport."

    if species in ["al+3", "al"]:
        if "calcite" in params or "pyrite" in params:
            return "Al is controlled indirectly through pH, buffering, and acid generation."
        return "Al is controlled by pH-dependent solubility and secondary mineral reactions."

    if species in ["ca+2", "ca"]:
        if "calcite" in params:
            return "Ca is mainly controlled by carbonate dissolution and hydrologic flushing."
        return "Ca is controlled by mineral dissolution and transport."

    return "This species is controlled by the highest-ranked parameters listed below."


def _priority_from_species(species: str) -> int:
    if species in ["pH", "so4-2"]:
        return 1
    if species in ["zn+2", "cu+2", "pb+2", "cd+2", "al+3"]:
        return 2
    return 3


def build_species_interpretation(
    process_summary: pd.DataFrame,
    top_n: int = 8,
) -> pd.DataFrame:
    if process_summary.empty:
        return pd.DataFrame()

    required = {
        "species",
        "parameter",
        "normalized_process_sensitivity",
        "abs_normalized_process_sensitivity",
    }

    if not required.issubset(set(process_summary.columns)):
        return pd.DataFrame()

    df = process_summary.copy()

    df["abs_normalized_process_sensitivity"] = pd.to_numeric(
        df["abs_normalized_process_sensitivity"],
        errors="coerce",
    )

    rows = []

    for species in sorted(df["species"].dropna().astype(str).unique()):
        sub = df[df["species"].astype(str) == species].copy()

        sub = sub.sort_values(
            "abs_normalized_process_sensitivity",
            ascending=False,
        )

        top = sub.head(top_n).copy()
        top_params = top["parameter"].astype(str).tolist()

        dominant_sentence = _dominant_process_sentence(species, top_params)

        for rank, (_, r) in enumerate(top.iterrows(), start=1):
            parameter = str(r.get("parameter"))
            response = _safe_float(r.get("normalized_process_sensitivity"))
            percent_change = _safe_float(r.get("percent_model_change"))

            if response is None:
                direction = "unknown"
            elif response > 0:
                direction = "positive"
            elif response < 0:
                direction = "negative"
            else:
                direction = "neutral"

            rows.append(
                {
                    "species": species,
                    "priority": _priority_from_species(species),
                    "rank": rank,
                    "parameter": parameter,
                    "process_group": _classify_parameter(parameter),
                    "sensitivity_case": r.get("sensitivity_case"),
                    "normalized_process_sensitivity": response,
                    "abs_normalized_process_sensitivity": r.get(
                        "abs_normalized_process_sensitivity"
                    ),
                    "percent_model_change": percent_change,
                    "response_direction": direction,
                    "hydrogeochemical_interpretation": dominant_sentence,
                }
            )

    out = pd.DataFrame(rows)

    if not out.empty:
        out = out.sort_values(
            ["priority", "species", "rank"],
            ascending=[True, True, True],
        )

    return out


def build_process_group_summary(
    species_interpretation: pd.DataFrame,
) -> pd.DataFrame:
    if species_interpretation.empty:
        return pd.DataFrame()

    grouped = (
        species_interpretation
        .groupby(["species", "process_group"], as_index=False)
        .agg(
            max_abs_sensitivity=(
                "abs_normalized_process_sensitivity",
                "max",
            ),
            mean_abs_sensitivity=(
                "abs_normalized_process_sensitivity",
                "mean",
            ),
            n_parameters=("parameter", "nunique"),
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

    return grouped


def _format_table(df: pd.DataFrame, columns: List[str], n: int | None = None) -> str:
    if df.empty:
        return "No data available."

    cols = [c for c in columns if c in df.columns]
    show = df[cols].copy()

    if n is not None:
        show = show.head(n)

    return show.to_markdown(index=False)


def write_sensitivity_interpretation_report(
    process_summary_file: Path,
    interpretation_file: Path,
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

    process_summary = _read_excel(process_summary_file)

    interpretation_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.parent.mkdir(parents=True, exist_ok=True)

    interpretation = build_species_interpretation(process_summary)
    group_summary = build_process_group_summary(interpretation)

    interpretation.to_excel(interpretation_file, index=False)

    group_file = interpretation_file.parent / "process_group_summary_V9_2.xlsx"
    group_summary.to_excel(group_file, index=False)

    lines = []

    lines.append("# MIN3P Sensitivity Interpretation Report — V9.2")
    lines.append("")
    lines.append(f"Generated: {dt.datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"Project: `{project_name}`")
    lines.append("")

    lines.append("## 1. Purpose")
    lines.append("")
    lines.append(
        "This report converts the numerical process-sensitivity results into "
        "hydrogeochemical interpretation. It identifies the dominant controls on "
        "pH, sulfate, and metals, and translates parameter sensitivity into "
        "calibration priorities."
    )
    lines.append("")

    if process_summary.empty:
        lines.append("No process sensitivity summary was found.")
        report_file.write_text("\n".join(lines), encoding="utf-8")
        return report_file

    if interpretation.empty:
        lines.append(
            "No interpretation could be generated. Check that the process sensitivity "
            "summary contains species, parameter, and normalized sensitivity columns."
        )
        report_file.write_text("\n".join(lines), encoding="utf-8")
        return report_file

    lines.append("## 2. Executive interpretation")
    lines.append("")

    for species in focus_species:
        sub = interpretation[
            interpretation["species"].astype(str).str.lower() == species.lower()
        ].copy()

        if sub.empty:
            continue

        first_sentence = sub.iloc[0].get("hydrogeochemical_interpretation", "")

        top_params = sub.sort_values("rank").head(4)["parameter"].astype(str).tolist()

        lines.append(f"### {species}")
        lines.append("")
        lines.append(f"**Dominant interpretation:** {first_sentence}")
        lines.append("")
        lines.append(
            "**Top controlling parameters:** "
            + ", ".join([f"`{p}`" for p in top_params])
        )
        lines.append("")

    lines.append("## 3. Species-specific controls")
    lines.append("")

    for species in focus_species:
        sub = interpretation[
            interpretation["species"].astype(str).str.lower() == species.lower()
        ].copy()

        if sub.empty:
            continue

        sub = sub.sort_values("rank")

        lines.append(f"### {species}")
        lines.append("")
        lines.append(
            _format_table(
                sub,
                [
                    "rank",
                    "parameter",
                    "process_group",
                    "sensitivity_case",
                    "normalized_process_sensitivity",
                    "percent_model_change",
                    "response_direction",
                ],
                n=10,
            )
        )
        lines.append("")

    lines.append("## 4. Process-group controls")
    lines.append("")

    for species in focus_species:
        sub = group_summary[
            group_summary["species"].astype(str).str.lower() == species.lower()
        ].copy()

        if sub.empty:
            continue

        sub = sub.sort_values("rank_within_species")

        lines.append(f"### {species}")
        lines.append("")
        lines.append(
            _format_table(
                sub,
                [
                    "rank_within_species",
                    "process_group",
                    "max_abs_sensitivity",
                    "mean_abs_sensitivity",
                    "n_parameters",
                ],
                n=10,
            )
        )
        lines.append("")

    lines.append("## 5. Calibration priorities inferred from process sensitivity")
    lines.append("")

    lines.append("### Priority 1 — pH and sulfate system")
    lines.append("")
    lines.append(
        "Calibrate acid generation, buffering, and flow before trace metals. "
        "The most important parameter groups are usually sulfide oxidation, "
        "carbonate buffering, oxygen supply, and hydrologic residence time."
    )
    lines.append("")

    lines.append("### Priority 2 — Metal source terms")
    lines.append("")
    lines.append(
        "After pH and sulfate are reasonable, calibrate metal-source minerals such as "
        "sphalerite for Zn/Cd, galena for Pb, and chalcopyrite for Cu."
    )
    lines.append("")

    lines.append("### Priority 3 — Sorption and secondary phases")
    lines.append("")
    lines.append(
        "Use sorption and secondary Fe/Al phases mainly for fine-tuning metal mobility "
        "after the primary acid-generation and metal-source terms are constrained."
    )
    lines.append("")

    lines.append("## 6. Files created")
    lines.append("")
    lines.append(f"- `{interpretation_file}`")
    lines.append(f"- `{group_file}`")
    lines.append(f"- `{report_file}`")
    lines.append("")

    report_file.write_text("\n".join(lines), encoding="utf-8")

    return report_file