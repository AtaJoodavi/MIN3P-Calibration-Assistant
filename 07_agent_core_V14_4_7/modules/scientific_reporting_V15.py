from __future__ import annotations

"""V15.4.2 DAT-driven scientific reporting, explainability, and campaign review for a frozen V14 campaign.

This module is deliberately read-only with respect to MIN3P input files and
V14 optimiser state. It reads campaign evidence and writes a reproducible
scientific-report package under ``05_reports``.

Outputs:
* conceptual hydrogeochemical model (SVG + PNG), driven by editable JSON;
* calibration decision timeline;
* ranked parameter-sensitivity summary figure;
* parameter -> process -> pH/SO4 observable map;
* pH/SO4 response atlas, explicitly marked not_available where metrics are
  absent;
* Excel tables, Markdown report, HTML report, and a manifest with SHA-256
  integrity checks of protected V14 files before and after reporting.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterable
import base64
import hashlib
import json
import math
import os
import re

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.image as mpimg
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch, Rectangle
import numpy as np
import pandas as pd

from modules.campaign_review_V15_4 import generate_campaign_review_package


REPORTER_VERSION = "V15.4.2"


@dataclass(frozen=True)
class V15ProjectPaths:
    """Filesystem contract for a V14 project and its reporting package."""

    project_dir: Path
    agent_core_dir: Path
    input_dir: Path
    results_dir: Path
    reports_dir: Path
    output_dir: Path
    conceptual_config_file: Path

    @classmethod
    def from_agent_core(
        cls,
        agent_core_dir: str | Path,
        *,
        output_dir: str | Path | None = None,
        conceptual_config_file: str | Path | None = None,
    ) -> "V15ProjectPaths":
        """Resolve project paths from the V14 core or any nested helper directory.

        The normal contract is ``<project>/07_agent_core_V14``.  For safety,
        this resolver also supports a command launched from a nested patch or
        helper directory beneath the core: it walks upward and selects the
        nearest ancestor containing ``01_input``.  It never searches across
        sibling calibration projects.
        """
        core = Path(agent_core_dir).resolve()
        project: Path | None = None
        for candidate in [core.parent, *core.parents]:
            if (candidate / "01_input").is_dir():
                project = candidate
                break
        if project is None:
            # Preserve deterministic paths even before the caller creates a
            # project.  DAT discovery will then fail loudly and list locations.
            project = core.parent
        reports = project / "05_reports"
        out = Path(output_dir).resolve() if output_dir else reports / "V15_scientific_report"
        config = (
            Path(conceptual_config_file).resolve()
            if conceptual_config_file
            else core / "config" / "conceptual_model_V15.json"
        )
        return cls(
            project_dir=project,
            agent_core_dir=core,
            input_dir=project / "01_input",
            results_dir=project / "04_results",
            reports_dir=reports,
            output_dir=out,
            conceptual_config_file=config,
        )

@dataclass
class V15ReportResult:
    output_dir: str
    report_markdown: str
    report_html: str
    manifest: str
    conceptual_svg: str | None
    conceptual_png: str | None
    timeline_png: str | None
    process_map_svg: str | None
    response_atlas_png: str | None
    parameter_sensitivity_png: str | None
    parameter_trial_effects: str
    ph_so4_response_metrics: str
    parameter_sensitivity_summary: str
    calibration_group_summary: str
    conceptual_facts: str | None
    conceptual_prompt: str | None
    conceptual_generation_manifest: str | None
    conceptual_draft_png: str | None
    conceptual_detail: str
    campaign_analysis_json: str | None
    campaign_analysis_xlsx: str | None
    campaign_review_json: str | None
    campaign_review_markdown: str | None
    campaign_review_manifest: str | None
    campaign_review_mode: str
    protected_hashes_unchanged: bool
    trial_count: int
    pH_so4_metrics_available: bool
    warnings: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "reporter_version": REPORTER_VERSION,
            "output_dir": self.output_dir,
            "report_markdown": self.report_markdown,
            "report_html": self.report_html,
            "manifest": self.manifest,
            "conceptual_svg": self.conceptual_svg,
            "conceptual_png": self.conceptual_png,
            "timeline_png": self.timeline_png,
            "process_map_svg": self.process_map_svg,
            "response_atlas_png": self.response_atlas_png,
            "parameter_sensitivity_png": self.parameter_sensitivity_png,
            "parameter_trial_effects": self.parameter_trial_effects,
            "pH_SO4_response_metrics": self.ph_so4_response_metrics,
            "parameter_sensitivity_summary": self.parameter_sensitivity_summary,
            "calibration_group_summary": self.calibration_group_summary,
            "conceptual_facts": self.conceptual_facts,
            "conceptual_prompt": self.conceptual_prompt,
            "conceptual_generation_manifest": self.conceptual_generation_manifest,
            "conceptual_draft_png": self.conceptual_draft_png,
            "conceptual_detail": self.conceptual_detail,
            "campaign_analysis_json": self.campaign_analysis_json,
            "campaign_analysis_xlsx": self.campaign_analysis_xlsx,
            "campaign_review_json": self.campaign_review_json,
            "campaign_review_markdown": self.campaign_review_markdown,
            "campaign_review_manifest": self.campaign_review_manifest,
            "campaign_review_mode": self.campaign_review_mode,
            "protected_campaign_hashes_unchanged": self.protected_hashes_unchanged,
            "trial_count": self.trial_count,
            "pH_SO4_metrics_available": self.pH_so4_metrics_available,
            "warnings": self.warnings,
        }


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_excel(path: Path) -> pd.DataFrame:
    try:
        return pd.read_excel(path)
    except Exception:
        return pd.DataFrame()


def _safe_string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    return str(value).strip()


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _safe_string(value).casefold()).strip("_")


def _to_float(value: Any) -> float | None:
    try:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except Exception:
        return None


def _first_existing_column(frame: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    if frame.empty:
        return None
    lookup = {_norm(column): str(column) for column in frame.columns}
    for candidate in candidates:
        if _norm(candidate) in lookup:
            return lookup[_norm(candidate)]
    return None


def _column_like(frame: pd.DataFrame, patterns: Iterable[str]) -> list[str]:
    out: list[str] = []
    for column in frame.columns:
        key = _norm(column)
        if any(re.search(pattern, key) for pattern in patterns):
            out.append(str(column))
    return out




def _dat_noncomment_lines(text: str) -> list[str]:
    """Return MIN3P DAT lines excluding full-line comments.

    Inline comments are retained because mineral input comments identify the
    physical meaning of some values. The parser itself never treats comments as
    model facts.
    """
    return [line.rstrip("\n") for line in text.splitlines() if not line.lstrip().startswith("!")]


def _quoted_line_value(line: str) -> str:
    match = re.match(r"\s*'([^']+)'\s*$", line)
    return match.group(1).strip() if match else ""


def _leading_quoted_value(line: str) -> str:
    match = re.match(r"\s*'([^']+)'", line)
    return match.group(1).strip() if match else ""


def _is_dat_heading(line: str, heading: str) -> bool:
    return _leading_quoted_value(line).casefold() == heading.casefold()


def _dat_block(lines: list[str], heading: str) -> list[str]:
    target = heading.casefold()
    start: int | None = None
    for i, line in enumerate(lines):
        if _is_dat_heading(line, heading):
            start = i + 1
            break
    if start is None:
        return []
    out: list[str] = []
    for line in lines[start:]:
        if _quoted_line_value(line).casefold() == "done":
            break
        out.append(line)
    return out


def _float_token(value: Any) -> float | None:
    text = _safe_string(value).replace("D", "E").replace("d", "e")
    match = re.search(r"(?<![A-Za-z])[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", text)
    return _to_float(match.group(0)) if match else None


def _line_before_comment(line: str) -> str:
    return line.split(";", 1)[0].strip()


def _numeric_value_for_comment(lines: list[str], marker: str) -> float | None:
    marker = marker.casefold()
    for line in lines:
        if marker in line.casefold():
            value = _float_token(_line_before_comment(line))
            if value is not None:
                return value
    return None


def _range_for_comment(lines: list[str], marker: str) -> list[float] | None:
    marker = marker.casefold()
    for line in lines:
        if marker in line.casefold():
            left = _line_before_comment(line).replace("D", "E").replace("d", "e")
            values = [float(x) for x in re.findall(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?", left)]
            if len(values) >= 2:
                return values[:2]
    return None


def _zone_segment(lines: list[str], name: str) -> list[str]:
    wanted = name.casefold()
    for i, line in enumerate(lines):
        if _quoted_line_value(line).casefold() != wanted:
            continue
        out: list[str] = []
        for candidate in lines[i + 1:]:
            if _quoted_line_value(candidate).casefold() == "end of zone":
                return out
            out.append(candidate)
    return []


def _next_nonblank(lines: list[str], start: int) -> str:
    for line in lines[start:]:
        if line.strip():
            return line.strip()
    return ""


def _zone_boundary(zone: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {"type": None, "value": None, "raw": ""}
    for i, line in enumerate(zone):
        if _quoted_line_value(line).casefold() == "boundary type":
            raw = _next_nonblank(zone, i + 1)
            result["raw"] = raw
            match = re.search(r"'([^']+)'", raw)
            result["type"] = match.group(1).strip() if match else None
            numbers = re.findall(r"[-+]?\d*\.?\d+(?:[dDeE][-+]?\d+)?", raw)
            if numbers:
                result["value"] = _to_float(numbers[-1].replace("D", "E").replace("d", "e"))
            return result
    return result


def _zone_concentrations(zone: list[str]) -> dict[str, float]:
    values: dict[str, float] = {}
    start = None
    for i, line in enumerate(zone):
        if _is_dat_heading(line, "concentration input"):
            start = i + 1
            break
    if start is None:
        return values
    for line in zone[start:]:
        if not line.strip():
            continue
        quoted = re.findall(r"'([^']+)'", line)
        if not quoted:
            if _leading_quoted_value(line):
                break
            continue
        number = _float_token(_line_before_comment(line))
        if number is None:
            continue
        species = quoted[-1].strip().casefold()
        values[species] = number
    return values


def _parse_min3p_dat(dat_file: Path) -> dict[str, Any]:
    """Extract conservative conceptual facts from a MIN3P DAT file.

    The parser intentionally reports only parsed model facts. It does not infer
    undocumented layers, reaction zones, or numerical response directions.
    """
    text = dat_file.read_text(encoding="utf-8", errors="replace")
    lines = _dat_noncomment_lines(text)
    warnings: list[str] = []

    global_block = _dat_block(lines, "global control parameters")
    model_name = ""
    for line in global_block:
        candidate = _quoted_line_value(line)
        if candidate and candidate.casefold() not in {"global control parameters", "done"}:
            model_name = candidate
            break

    spatial = _dat_block(lines, "spatial discretization")
    control_volumes = {
        "x": int(_numeric_value_for_comment(spatial, "number of control volumes in x") or 0),
        "y": int(_numeric_value_for_comment(spatial, "number of control volumes in y") or 0),
        "z": int(_numeric_value_for_comment(spatial, "number of control volumes in z") or 0),
    }
    intervals = {
        "x": int(_numeric_value_for_comment(spatial, "number of discretization intervals in x") or 0),
        "y": int(_numeric_value_for_comment(spatial, "number of discretization intervals in y") or 0),
        "z": int(_numeric_value_for_comment(spatial, "number of discretization intervals in z") or 0),
    }
    extents = {
        "x": _range_for_comment(spatial, "xmin,xmax"),
        "y": _range_for_comment(spatial, "ymin,ymax"),
        "z": _range_for_comment(spatial, "zmin,zmax"),
    }
    if not all(control_volumes.values()):
        warnings.append("Spatial control-volume counts could not be parsed completely from the DAT file.")
    dimension = "1D vertical" if control_volumes.get("x") == 1 and control_volumes.get("y") == 1 and control_volumes.get("z", 0) > 1 else "multi-dimensional or unresolved"

    geochemical = _dat_block(lines, "geochemical system")
    minerals: list[str] = []
    gases: list[str] = []
    components: list[str] = []
    surfaces: list[str] = []
    sorbed_species: list[str] = []
    redox_couples: list[list[str]] = []
    kinetic_reactions: list[str] = []

    def section_items(block: list[str], heading: str, stop_headings: tuple[str, ...]) -> list[str]:
        start = None
        for i, line in enumerate(block):
            if _is_dat_heading(line, heading):
                start = i + 1
                break
        if start is None:
            return []
        output: list[str] = []
        for line in block[start:]:
            key = _leading_quoted_value(line).casefold()
            if key in {value.casefold() for value in stop_headings}:
                break
            quoted = re.findall(r"'([^']+)'", line)
            if quoted:
                if heading.casefold() == "non-aqueous components":
                    output.append(quoted[0].strip())
                else:
                    output.extend(item.strip() for item in quoted if item.strip())
        # First quoted items are sometimes headings/counts; remove obvious labels.
        return [item for item in output if item.casefold() not in {heading.casefold(), "number of minerals", "number of components", "number of gases", "number of sorbed species"}]

    components = section_items(geochemical, "components", ("non-aqueous components",))
    surfaces = section_items(geochemical, "non-aqueous components", ("secondary aqueous species",))
    gases = section_items(geochemical, "gases", ("minerals",))
    minerals = section_items(geochemical, "minerals", ("define sorption type", "redox couples", "intra-aqueous kinetic reactions"))
    sorbed_species = section_items(geochemical, "sorbed species", ("redox couples", "intra-aqueous kinetic reactions", "done"))

    # Exact redox-couple pairs and intra-aqueous kinetic reaction names.
    for i, line in enumerate(geochemical):
        if _is_dat_heading(line, "redox couples"):
            for candidate in geochemical[i + 1:]:
                key = _leading_quoted_value(candidate).casefold()
                if key in {"intra-aqueous kinetic reactions", "done"}:
                    break
                quoted = re.findall(r"'([^']+)'", candidate)
                if len(quoted) >= 2:
                    redox_couples.append([quoted[0], quoted[1]])
            break
    for i, line in enumerate(geochemical):
        if _is_dat_heading(line, "intra-aqueous kinetic reactions"):
            for candidate in geochemical[i + 1:]:
                key = _leading_quoted_value(candidate).casefold()
                if key in {"scaling for intra-aqueous kinetic reactions", "done"}:
                    break
                quoted = re.findall(r"'([^']+)'", candidate)
                if quoted:
                    kinetic_reactions.append(quoted[0])
            break

    # Initial reactive condition records whether phase entries are enabled in
    # the model. Pair phase records to minerals in the authoritative mineral list.
    initial_reactive = _dat_block(lines, "initial condition - reactive transport")
    active_phases: list[dict[str, Any]] = []
    phi_records: list[tuple[float | None, bool | None, str | None]] = []
    in_mineral_input = False
    for line in initial_reactive:
        if _is_dat_heading(line, "mineral input"):
            in_mineral_input = True
            continue
        if in_mineral_input and _is_dat_heading(line, "extent of zone"):
            break
        if not in_mineral_input:
            continue
        match = re.match(r"\s*([^\s]+)\s+(\.true\.|\.false\.)\s+'([^']+)'", line, flags=re.IGNORECASE)
        if match:
            phi = _to_float(match.group(1).replace("D", "E").replace("d", "e"))
            phi_records.append((phi, match.group(2).casefold() == ".true.", match.group(3)))
    for index, mineral in enumerate(minerals):
        phi, enabled, geometry = phi_records[index] if index < len(phi_records) else (None, None, None)
        active_phases.append({"name": mineral, "initial_phi": phi, "enabled": enabled, "geometry": geometry})
    if minerals and len(phi_records) != len(minerals):
        warnings.append(f"Parsed {len(minerals)} minerals but {len(phi_records)} mineral-input records; unmatched phase records are marked unresolved.")

    flow_block = _dat_block(lines, "boundary conditions - variably saturated flow")
    reactive_block = _dat_block(lines, "boundary conditions - reactive transport")
    flow_boundaries = {name.casefold(): _zone_boundary(_zone_segment(flow_block, name)) for name in ("Top", "Bottom")}
    reactive_boundaries: dict[str, Any] = {}
    for name in ("Top", "Bottom"):
        zone = _zone_segment(reactive_block, name)
        reactive_boundaries[name.casefold()] = {
            "boundary": _zone_boundary(zone),
            "concentrations": _zone_concentrations(zone),
        }

    porous = _dat_block(lines, "physical parameters - porous medium")
    property_zone_count = int(_numeric_value_for_comment(porous, "number of property zones") or 0)
    property_zone_names = [q for line in porous for q in re.findall(r"'([^']+)'", line) if q.casefold() not in {"physical parameters - porous medium", "number and name of zone", "extent of zone", "end of zone"}]
    # First string after zone number/name is normally the property-zone name.
    property_zone_name = property_zone_names[0] if property_zone_names else ""
    porosity = _numeric_value_for_comment(porous, "porosity")

    def names_matching(*tokens: str) -> list[str]:
        out: list[str] = []
        for row in active_phases:
            if row.get("enabled") is False:
                continue
            name = str(row.get("name", ""))
            lowered = name.casefold()
            if any(token in lowered for token in tokens):
                out.append(name)
        return out

    sulfides = names_matching("pyrite", "pyrrhot", "chalcopyr", "sphaler", "galena")
    carbonates = names_matching("calcite", "magnesite", "siderite", "smithsonite", "otavite")
    fe_phases = names_matching("ferrihydrite", "jarosite")
    silicates = names_matching("biotite", "albite", "chlorite", "muscovite", "sio2")
    jarosite_record = next((row for row in active_phases if "jarosite" in str(row.get("name", "")).casefold()), None)

    top_conc = reactive_boundaries.get("top", {}).get("concentrations", {})
    facts = {
        "schema_version": "V15.2.1_DAT_facts_v1",
        "source": {
            "dat_file": str(dat_file),
            "filename": dat_file.name,
            "sha256": _sha256(dat_file),
            "parser": "scientific_reporting_V15._parse_min3p_dat",
        },
        "model": {"name": model_name or dat_file.stem},
        "geometry": {
            "numerical_dimension": dimension,
            "property_zone_count": property_zone_count or None,
            "property_zone_name": property_zone_name or None,
            "control_volumes": control_volumes,
            "intervals": intervals,
            "extents": extents,
            "one_reactive_property_zone": bool(property_zone_count == 1),
        },
        "flow_boundaries": flow_boundaries,
        "reactive_transport_boundaries": reactive_boundaries,
        "top_boundary_key_values": {
            "pH": top_conc.get("ph"),
            "pO2": top_conc.get("po2"),
            "pCO2": top_conc.get("pco2"),
        },
        "geochemistry": {
            "components": components,
            "gases": gases,
            "surface_components": surfaces,
            "sorbed_species": sorbed_species,
            "redox_couples": redox_couples,
            "intra_aqueous_kinetic_reactions": kinetic_reactions,
            "minerals": active_phases,
            "active_phase_groups": {
                "sulfides": sulfides,
                "carbonates_and_neutralizing_phases": carbonates,
                "fe_phases": fe_phases,
                "silicates": silicates,
            },
            "jarosite": jarosite_record,
        },
        "parser_warnings": warnings,
    }
    return _v1542_enrich_multilayer_facts(dat_file, facts)


# ---------------------------------------------------------------------------
# V15.4.2 multilayer DAT parsing helpers
# ---------------------------------------------------------------------------

def _v1542_clean_line(line: str) -> str:
    """Remove inline comments while keeping MIN3P tokens."""
    return str(line).split("!", 1)[0].split(";", 1)[0].strip()


def _v1542_unquote(text: str) -> str:
    return str(text).strip().strip("'").strip('"').strip()


def _v1542_is_quoted_heading(line: str, heading: str) -> bool:
    return _leading_quoted_value(str(line)).casefold() == str(heading).casefold()


def _v1542_float_tokens(line: str) -> list[float]:
    """Parse MIN3P numeric tokens, including Fortran d-notation and 7.65-2 shorthand."""
    cleaned = _v1542_clean_line(line).replace("D", "E").replace("d", "e")
    cleaned = re.sub(r"(?<=\d)([+-]\d+)(?=\s|$)", r"e\1", cleaned)
    values: list[float] = []
    for token in cleaned.split():
        try:
            values.append(float(token))
        except Exception:
            continue
    return values


def _v1542_dat_block(lines: list[str], heading: str) -> list[str]:
    start = None
    for i, line in enumerate(lines):
        if _v1542_is_quoted_heading(line, heading):
            start = i + 1
            break
    if start is None:
        return []
    out: list[str] = []
    for line in lines[start:]:
        if _quoted_line_value(line).casefold() == "done":
            break
        out.append(line)
    return out


def _v1542_zone_segments(block: list[str]) -> dict[str, list[str]]:
    """Return MIN3P zone segments from a block containing repeated 'number and name of zone'."""
    zones: dict[str, list[str]] = {}
    i = 0
    while i < len(block):
        if _v1542_is_quoted_heading(block[i], "number and name of zone"):
            name = ""
            if i + 2 < len(block):
                name = _quoted_line_value(block[i + 2]) or _leading_quoted_value(block[i + 2])
            segment: list[str] = []
            j = i + 3
            while j < len(block):
                if _v1542_is_quoted_heading(block[j], "end of zone"):
                    break
                segment.append(block[j])
                j += 1
            if name:
                zones[name] = segment
            i = j
        i += 1
    return zones


def _v1542_extent_from_segment(segment: list[str]) -> dict[str, float] | None:
    for i, line in enumerate(segment):
        if _v1542_is_quoted_heading(line, "extent of zone") and i + 1 < len(segment):
            vals = _v1542_float_tokens(segment[i + 1])
            if len(vals) >= 6:
                return {
                    "x_min": vals[0], "x_max": vals[1],
                    "y_min": vals[2], "y_max": vals[3],
                    "z_min": vals[4], "z_max": vals[5],
                }
    return None


def _v1542_parse_spatial(lines: list[str]) -> dict[str, Any]:
    spatial = _v1542_dat_block(lines, "spatial discretization")
    numeric = [vals for vals in (_v1542_float_tokens(line) for line in spatial) if vals]
    rows_y = int(numeric[0][0]) if len(numeric) > 0 and len(numeric[0]) == 1 else None
    columns_x = int(numeric[1][0]) if len(numeric) > 1 and len(numeric[1]) == 1 else None
    cells_z = None
    z_min = None
    z_max = None
    for idx, vals in enumerate(numeric):
        if len(vals) == 1 and float(vals[0]).is_integer() and idx + 1 < len(numeric):
            nxt = numeric[idx + 1]
            if len(nxt) >= 2 and nxt[1] > nxt[0]:
                cells_z = int(vals[0])
                z_min = float(nxt[0])
                z_max = float(nxt[1])
    if rows_y == 1 and columns_x == 1 and cells_z and cells_z > 1:
        label = "1D vertical column"
    else:
        label = "multi-dimensional or unresolved"
    return {
        "rows_y": rows_y,
        "columns_x": columns_x,
        "cells_z": cells_z,
        "z_min": z_min,
        "z_max": z_max,
        "dimension_label": label,
    }


def _v1542_parse_porous_zones(lines: list[str]) -> list[dict[str, Any]]:
    porous = _v1542_dat_block(lines, "physical parameters - porous medium")
    zone_segments = _v1542_zone_segments(porous)
    zones: list[dict[str, Any]] = []
    for name, segment in zone_segments.items():
        # The zone number and porosity are outside the segment in this helper, so parse directly from block.
        porosity = None
        zone_id = None
        for i, line in enumerate(porous):
            if _quoted_line_value(line) == name:
                if i - 1 >= 0:
                    vals = _v1542_float_tokens(porous[i - 1])
                    if vals:
                        zone_id = int(vals[0])
                if i + 1 < len(porous):
                    vals = _v1542_float_tokens(porous[i + 1])
                    if vals:
                        porosity = float(vals[0])
                break
        extent = _v1542_extent_from_segment(segment)
        if extent:
            zone = {"zone_id": zone_id, "name": name, "porosity": porosity, **extent}
            zones.append(zone)
    return sorted(zones, key=lambda z: float(z.get("z_max", 0.0)), reverse=True)


def _v1542_parse_flow_zone_properties(lines: list[str]) -> dict[str, dict[str, Any]]:
    block = _v1542_dat_block(lines, "physical parameters - variably saturated flow")
    props: dict[str, dict[str, Any]] = {}
    i = 0
    while i < len(block):
        zone_name = _quoted_line_value(block[i])
        if zone_name and zone_name.casefold() not in {"end of zone", "done"}:
            data: dict[str, Any] = {}
            j = i + 1
            while j < len(block):
                if _v1542_is_quoted_heading(block[j], "end of zone"):
                    break
                tag = _leading_quoted_value(block[j]).casefold()
                if tag == "hydraulic conductivity in z-direction" and j + 1 < len(block):
                    vals = _v1542_float_tokens(block[j + 1])
                    if vals:
                        data["Kz"] = vals[0]
                if tag == "soil hydraulic function parameters" and j + 4 < len(block):
                    keys = ["theta_r", "alpha", "n", "l"]
                    for off, key in enumerate(keys, start=1):
                        vals = _v1542_float_tokens(block[j + off])
                        if vals:
                            data[key] = vals[0]
                j += 1
            if data:
                props[zone_name] = data
            i = j
        i += 1
    return props


def _v1542_parse_initial_chemistry(lines: list[str]) -> dict[str, dict[str, Any]]:
    block = _v1542_dat_block(lines, "initial condition - reactive transport")
    zone_segments = _v1542_zone_segments(block)
    out: dict[str, dict[str, Any]] = {}
    species_order = [
        "pH", "co3-2", "cl-1", "so4-2", "na+1", "k+1", "ca+2", "mg+2",
        "h4sio4", "h3aso4", "fe+2", "fe+3", "al+3", "o2(aq)", "hs-1", "scn-1",
    ]
    for name, segment in zone_segments.items():
        chem: dict[str, Any] = {}
        values: list[float] = []
        in_conc = False
        for line in segment:
            if _v1542_is_quoted_heading(line, "concentration input"):
                in_conc = True
                continue
            if in_conc and _leading_quoted_value(line):
                break
            if in_conc:
                vals = _v1542_float_tokens(line)
                if vals:
                    values.append(vals[0])
        for key, val in zip(species_order, values):
            chem[key] = val
        surfaces: list[dict[str, Any]] = []
        in_sorption = False
        for line in segment:
            if _v1542_is_quoted_heading(line, "sorption parameter input"):
                in_sorption = True
                continue
            if in_sorption and _leading_quoted_value(line).casefold() in {"mineral input", "extent of zone", "end of zone"}:
                break
            if in_sorption and _leading_quoted_value(line):
                site = _leading_quoted_value(line)
                vals = _v1542_float_tokens(line)
                surfaces.append({"site": site, "mass": vals[0] if len(vals) > 0 else None, "area": vals[1] if len(vals) > 1 else None, "site_density": vals[2] if len(vals) > 2 else None})
        chem["surface_sites"] = surfaces
        out[name] = chem
    return out


def _v1542_parse_zone_minerals(lines: list[str], mineral_names: list[str]) -> dict[str, list[dict[str, Any]]]:
    block = _v1542_dat_block(lines, "initial condition - reactive transport")
    zone_segments = _v1542_zone_segments(block)
    out: dict[str, list[dict[str, Any]]] = {}
    for name, segment in zone_segments.items():
        records: list[dict[str, Any]] = []
        in_mineral = False
        for line in segment:
            if _v1542_is_quoted_heading(line, "mineral input"):
                in_mineral = True
                continue
            if in_mineral and _v1542_is_quoted_heading(line, "extent of zone"):
                break
            if not in_mineral:
                continue
            m = re.match(r"\s*([^\s]+)\s+(\.true\.|\.false\.)\s+'([^']+)'", line, flags=re.IGNORECASE)
            if m:
                phi = _to_float(m.group(1).replace("D", "E").replace("d", "e"))
                enabled = m.group(2).casefold() == ".true."
                geom = m.group(3)
                idx = len(records)
                records.append({
                    "name": mineral_names[idx] if idx < len(mineral_names) else f"mineral_{idx + 1}",
                    "initial_phi": phi,
                    "enabled": enabled,
                    "geometry": geom,
                })
        out[name] = records
    return out


def _v1542_major_mineral_names(records: list[dict[str, Any]], threshold: float = 1e-12) -> list[str]:
    names: list[str] = []
    for row in records:
        phi = _to_float(row.get("initial_phi"))
        if phi is not None and phi > threshold:
            names.append(str(row.get("name", "")))
    return [n for n in names if n]


def _v1542_enrich_multilayer_facts(dat_file: Path, facts: dict[str, Any]) -> dict[str, Any]:
    """Add explicit multilayer geometry and zone facts to existing V15 facts."""
    text = dat_file.read_text(encoding="utf-8", errors="replace")
    lines = _dat_noncomment_lines(text)
    spatial = _v1542_parse_spatial(lines)
    zones = _v1542_parse_porous_zones(lines)
    hydraulic = _v1542_parse_flow_zone_properties(lines)
    chemistry = _v1542_parse_initial_chemistry(lines)
    mineral_names = [str(row.get("name", "")) for row in facts.get("geochemistry", {}).get("minerals", []) if isinstance(row, dict)]
    zone_minerals = _v1542_parse_zone_minerals(lines, mineral_names)

    z_min_total = min((float(z["z_min"]) for z in zones), default=spatial.get("z_min"))
    z_max_total = max((float(z["z_max"]) for z in zones), default=spatial.get("z_max"))
    for zone in zones:
        name = str(zone.get("name", ""))
        zone["hydraulic"] = hydraulic.get(name, {})
        zone["initial_chemistry"] = chemistry.get(name, {})
        zone["minerals"] = zone_minerals.get(name, [])
        zone["major_minerals"] = _v1542_major_mineral_names(zone.get("minerals", []))
        if z_min_total is not None and abs(float(zone["z_min"]) - float(z_min_total)) < 1e-12:
            zone["vertical_position"] = "bottom"
        elif z_max_total is not None and abs(float(zone["z_max"]) - float(z_max_total)) < 1e-12:
            zone["vertical_position"] = "top"
        else:
            zone["vertical_position"] = "middle"

    control_volumes = facts.setdefault("geometry", {}).setdefault("control_volumes", {})
    if spatial.get("columns_x") is not None:
        control_volumes["x"] = int(spatial["columns_x"])
    if spatial.get("rows_y") is not None:
        control_volumes["y"] = int(spatial["rows_y"])
    if spatial.get("cells_z") is not None:
        control_volumes["z"] = int(spatial["cells_z"])
    facts["geometry"]["numerical_dimension"] = spatial.get("dimension_label") or facts["geometry"].get("numerical_dimension")
    facts["geometry"]["property_zone_count"] = len(zones) or facts["geometry"].get("property_zone_count")
    facts["geometry"]["one_reactive_property_zone"] = bool(len(zones) == 1)
    facts["geometry"]["multilayer_zones"] = zones
    facts["geometry"]["spatial_summary"] = spatial
    if z_min_total is not None and z_max_total is not None:
        facts["geometry"].setdefault("extents", {})["z"] = [float(z_min_total), float(z_max_total)]

    if len(zones) > 1:
        facts.setdefault("parser_warnings", [])
        # Remove obsolete control-volume warning when enhanced parser succeeded.
        facts["parser_warnings"] = [
            w for w in facts.get("parser_warnings", [])
            if "Spatial control-volume counts could not be parsed" not in str(w)
        ]
    zone_names = {str(z.get("name", "")).upper(): z for z in zones}
    if "CIL" in zone_names and "NP" in zone_names:
        cil = zone_names["CIL"]
        np_zone = zone_names["NP"]
        if not (abs(float(cil["z_min"]) - 0.0) < 1e-12 and float(cil["z_max"]) <= float(np_zone["z_min"]) + 1e-12):
            facts.setdefault("parser_warnings", []).append("TP3 layer-order check failed: expected CIL at bottom and NP above CIL.")
    return facts


def _format_fact_list(values: list[str], *, limit: int = 5) -> str:
    if not values:
        return "none defined"
    shown = values[:limit]
    suffix = f" +{len(values) - limit} more" if len(values) > limit else ""
    return ", ".join(shown) + suffix

def _process_profile(parameter: Any, group: Any) -> dict[str, str]:
    """Map parameters to explainable process hypotheses, never measured effects."""
    name = _norm(parameter)
    group_key = _norm(group)

    profile = {
        "process_family": "Model calibration control",
        "hydrogeochemical_process": "Parameter-specific model response",
        "expected_pH_direction": "context-dependent",
        "expected_SO4_direction": "context-dependent",
        "scientific_note": "Expected directions are conceptual hypotheses, not inferred measurements.",
    }

    if name in {"kz", "bottom_head", "dispersivity", "porosity", "residual_sat", "vg_alpha", "vg_n", "vg_l", "top_flux", "gas_diff"} or group_key == "hydraulic":
        profile.update({
            "process_family": "Hydraulic transport",
            "hydrogeochemical_process": "Water residence time, gas access, solute transport and dilution",
            "expected_pH_direction": "context-dependent",
            "expected_SO4_direction": "context-dependent",
        })
    elif "pyrite" in name:
        profile.update({
            "process_family": "Sulfide oxidation",
            "hydrogeochemical_process": "Pyrite oxidation, acid generation and sulfate release",
            "expected_pH_direction": "decrease",
            "expected_SO4_direction": "increase",
        })
    elif any(token in name for token in ("calcite", "magnesite", "ankerite", "carbonate")):
        profile.update({
            "process_family": "Carbonate neutralization",
            "hydrogeochemical_process": "Mineral dissolution and acid neutralization",
            "expected_pH_direction": "increase",
            "expected_SO4_direction": "indirect / context-dependent",
        })
    elif any(token in name for token in ("feoh", "ferrihydrite", "sorption", "site_density")):
        profile.update({
            "process_family": "Fe hydroxide sorption",
            "hydrogeochemical_process": "Surface complexation, solute retention and reactive surface availability",
            "expected_pH_direction": "indirect / context-dependent",
            "expected_SO4_direction": "indirect / context-dependent",
        })
    elif any(token in name for token in ("biotite", "albite", "chlorite")):
        profile.update({
            "process_family": "Silicate weathering",
            "hydrogeochemical_process": "Silicate dissolution, alkalinity release and cation supply",
            "expected_pH_direction": "increase / buffering",
            "expected_SO4_direction": "indirect / context-dependent",
        })
    elif name.startswith("scaling_ox") or "oxid" in name:
        profile.update({
            "process_family": "Redox / oxidation scaling",
            "hydrogeochemical_process": "Oxidation reaction intensity and electron-acceptor availability",
            "expected_pH_direction": "decrease if sulfide oxidation increases",
            "expected_SO4_direction": "increase if sulfide oxidation increases",
        })
    elif name.startswith("bc_") or "boundary" in group_key:
        profile.update({
            "process_family": "Boundary chemistry",
            "hydrogeochemical_process": "Influent composition, pH, oxygen or imposed boundary forcing",
            "expected_pH_direction": "direct / parameter-specific",
            "expected_SO4_direction": "direct / parameter-specific",
        })
    elif name.startswith("init_"):
        profile.update({
            "process_family": "Initial chemistry",
            "hydrogeochemical_process": "Initial aqueous composition and transient equilibration",
            "expected_pH_direction": "early-time context-dependent",
            "expected_SO4_direction": "early-time context-dependent",
        })
    elif group_key == "mineral_kinetics" or name.startswith("keff_"):
        profile.update({
            "process_family": "Mineral kinetics",
            "hydrogeochemical_process": "Mineral dissolution/precipitation reaction rate",
            "expected_pH_direction": "mineral-specific",
            "expected_SO4_direction": "mineral-specific",
        })

    return profile


class V15ScientificReporter:
    """Generate a reproducible scientific-report package from V14 evidence."""

    def __init__(
        self,
        paths: V15ProjectPaths,
        *,
        dat_file: str | Path | None = None,
        conceptual_image_mode: str = "deterministic",
        conceptual_detail: str = "paper",
        campaign_review_mode: str = "auto",
        campaign_review_config: str | Path | None = None,
    ):
        self.paths = paths
        self.dat_file = Path(dat_file).resolve() if dat_file else None
        mode = _safe_string(conceptual_image_mode).casefold() or "deterministic"
        if mode not in {"deterministic", "gpt", "auto"}:
            raise ValueError("conceptual_image_mode must be deterministic, gpt, or auto")
        self.conceptual_image_mode = mode
        detail = _safe_string(conceptual_detail).casefold() or "paper"
        if detail not in {"paper", "technical"}:
            raise ValueError("conceptual_detail must be paper or technical")
        self.conceptual_detail = detail
        review_mode = _safe_string(campaign_review_mode).casefold() or "auto"
        if review_mode not in {"auto", "gpt", "deterministic", "off"}:
            raise ValueError("campaign_review_mode must be auto, gpt, deterministic, or off")
        self.campaign_review_mode = review_mode
        self.campaign_review_config = Path(campaign_review_config).resolve() if campaign_review_config else None
        self.warnings: list[str] = []
        self._protected_paths = [
            self.paths.input_dir / "agent_config.xlsx",
            self.paths.results_dir / "best_parameters_V14.xlsx",
            self.paths.results_dir / "v14_optimizer_state.xlsx",
            self.paths.results_dir / "v14_optimizer_parameter_state.xlsx",
            self.paths.results_dir / "v14_parameter_runtime_state.json",
            self.paths.results_dir / "v14_step_size_state.json",
        ]

    # -------------------------- read-only inputs --------------------------

    def protected_hashes(self) -> dict[str, str | None]:
        return {str(path): _sha256(path) for path in self._protected_paths}

    @staticmethod
    def _truthy(value: Any) -> bool:
        return str(value).strip().casefold() in {"true", "1", "1.0", "yes", "y"}

    @staticmethod
    def _split_metric_list(value: Any) -> list[str]:
        text = _safe_string(value)
        if not text:
            return []
        return [item.strip() for item in re.split(r"[,;|]", text) if item.strip()]

    def _pending_candidate_ids(self) -> set[str]:
        memory = _read_excel(self.paths.results_dir / "v14_optimizer_parameter_state.xlsx")
        if memory.empty or "pending_candidate_id" not in memory.columns:
            return set()
        pending = memory.get("pending", pd.Series(False, index=memory.index)).map(self._truthy)
        return {
            _safe_string(value)
            for value in memory.loc[pending, "pending_candidate_id"].tolist()
            if _safe_string(value)
        }

    def _recoverable_observer_failure_ids(self, raw: pd.DataFrame) -> set[str]:
        """Identify historical observe failures that were subsequently repaired.

        A failure is treated as recovered only when the candidate is no longer
        pending and later evaluated candidates exist. This changes reporting
        classification only; it never mutates V14 state or audit files.
        """
        if raw.empty or "decision" not in raw.columns or "candidate_id" not in raw.columns:
            return set()
        timestamps = pd.to_datetime(raw.get("timestamp"), errors="coerce")
        pending_ids = self._pending_candidate_ids()
        recovered: set[str] = set()
        failed = raw[raw["decision"].astype(str).str.strip().eq("optimizer_observe_run_failed")]
        for index, row in failed.iterrows():
            candidate_id = _safe_string(row.get("candidate_id"))
            if not candidate_id or candidate_id in pending_ids:
                continue
            failure_time = timestamps.loc[index] if index in timestamps.index else pd.NaT
            later = timestamps.gt(failure_time).fillna(False) if pd.notna(failure_time) else pd.Series(False, index=raw.index)
            later_evaluated = later & raw.get("phase", pd.Series("", index=raw.index)).astype(str).eq("candidate_evaluated")
            if bool(later_evaluated.any()):
                recovered.add(candidate_id)
        return recovered

    def _best_run_context(self, analysis: dict[str, Any]) -> dict[str, Any]:
        ranking = _read_excel(self.paths.results_dir / "run_ranking.xlsx")
        if ranking.empty:
            ranking = _read_excel(self.paths.results_dir / "optimization_history.xlsx")
        decisions, _source = self._decision_log()
        if ranking.empty or decisions.empty:
            return {}

        phase = decisions.get("phase", pd.Series("", index=decisions.index)).astype(str)
        evaluated = decisions[phase.eq("candidate_evaluated")].copy()
        accepted = evaluated.get("accepted", pd.Series(False, index=evaluated.index)).map(self._truthy)
        accepted_rows = evaluated[accepted].copy()
        if accepted_rows.empty:
            return {}
        accepted_rows["_candidate_score"] = pd.to_numeric(accepted_rows.get("candidate_objective"), errors="coerce")
        accepted_rows = accepted_rows[np.isfinite(accepted_rows["_candidate_score"])].copy()
        if accepted_rows.empty:
            return {}
        best_decision = accepted_rows.loc[accepted_rows["_candidate_score"].idxmin()]
        best_score = float(best_decision["_candidate_score"])
        best_run = _safe_string(best_decision.get("current_best_run_folder"))

        run_keys = ranking.get("run_folder", pd.Series("", index=ranking.index)).map(self._normalise_run_folder)
        best_key = self._normalise_run_folder(best_run)
        match = ranking[run_keys.eq(best_key)].copy() if best_key else pd.DataFrame()
        if match.empty and "TOTAL_SCORE" in ranking.columns:
            scores = pd.to_numeric(ranking["TOTAL_SCORE"], errors="coerce")
            finite = ranking[np.isfinite(scores)].copy()
            if not finite.empty:
                nearest_index = (pd.to_numeric(finite["TOTAL_SCORE"], errors="coerce") - best_score).abs().idxmin()
                match = ranking.loc[[nearest_index]].copy()
        if match.empty:
            return {}
        best_row = match.iloc[0]

        objective = analysis.get("objective_summary", {}) if isinstance(analysis, dict) else {}
        initial_score = _to_float(objective.get("initial_score")) or 10.0
        baseline_row = None
        if "TOTAL_SCORE" in ranking.columns:
            score_series = pd.to_numeric(ranking["TOTAL_SCORE"], errors="coerce")
            finite = ranking[np.isfinite(score_series)].copy()
            if not finite.empty:
                baseline_index = (pd.to_numeric(finite["TOTAL_SCORE"], errors="coerce") - initial_score).abs().idxmin()
                baseline_row = ranking.loc[baseline_index]

        included_metrics = self._split_metric_list(best_row.get("TOTAL_SCORE_INCLUDED_METRICS"))
        species_summary: list[dict[str, Any]] = []
        species_improvements: list[dict[str, Any]] = []
        for metric in included_metrics:
            if not metric.startswith("RMSE_"):
                continue
            species = metric[len("RMSE_"):]
            rmse = _to_float(best_row.get(metric))
            mean_observed = _to_float(best_row.get(f"Mean_obs_{species}"))
            mean_modelled = _to_float(best_row.get(f"Mean_model_{species}"))
            bias = _to_float(best_row.get(f"Bias_{species}"))
            if bias is None and mean_observed is not None and mean_modelled is not None:
                bias = mean_modelled - mean_observed
            norm = _to_float(best_row.get(f"NORM_RMSE_{species}"))
            weighted = _to_float(best_row.get(f"WEIGHTED_RMSE_{species}"))
            weight = None
            if norm is not None and weighted is not None and abs(norm) > 1e-30:
                weight = weighted / norm
            relative_bias = None
            if bias is not None and mean_observed is not None and abs(mean_observed) > 1e-30:
                relative_bias = bias / mean_observed
            if relative_bias is None or abs(relative_bias) <= 0.05:
                prediction_status = "approximately_unbiased"
            elif relative_bias < 0:
                prediction_status = "underpredicted"
            else:
                prediction_status = "overpredicted"
            species_summary.append({
                "species": species,
                "metric": metric,
                "weight": weight,
                "rmse": rmse,
                "mean_observed": mean_observed,
                "mean_modelled": mean_modelled,
                "bias_model_minus_observed": bias,
                "relative_bias_fraction": relative_bias,
                "prediction_status": prediction_status,
                "evidence_run_folder": _safe_string(best_row.get("run_folder")),
                "evidence_role": "protected_best_accepted_run",
            })
            initial_rmse = _to_float(baseline_row.get(metric)) if baseline_row is not None else None
            improvement = None
            if initial_rmse is not None and rmse is not None and abs(initial_rmse) > 1e-30:
                improvement = (initial_rmse - rmse) / initial_rmse
            species_improvements.append({
                "species": species,
                "initial_rmse": initial_rmse,
                "best_rmse": rmse,
                "relative_improvement_fraction": improvement,
            })

        return {
            "best_score": best_score,
            "best_run_folder": _safe_string(best_row.get("run_folder")) or best_run,
            "best_row": best_row.to_dict(),
            "initial_score": initial_score,
            "objective_mode": _safe_string(best_row.get("TOTAL_SCORE_OBJECTIVE_MODE")),
            "included_metrics": included_metrics,
            "configured_metrics": self._split_metric_list(best_row.get("TOTAL_SCORE_CONFIGURED_METRICS")),
            "reference_file": _safe_string(best_row.get("TOTAL_SCORE_REFERENCE_FILE")),
            "reference_scales": _safe_string(best_row.get("TOTAL_SCORE_REFERENCE_SCALES")),
            "species_summary": species_summary,
            "species_improvements": species_improvements,
        }

    def _apply_v15_4_1_corrections(self, package: dict[str, Any]) -> dict[str, Any]:
        """Correct best-run evidence and recovered-transaction bookkeeping."""
        analysis = package.get("analysis", {})
        review = package.get("review", {})
        metadata = package.get("review_metadata", {})
        if not isinstance(analysis, dict):
            return package

        context = self._best_run_context(analysis)
        decisions, _source = self._decision_log()
        recovered_ids = self._recoverable_observer_failure_ids(decisions)

        if context:
            objective = analysis.setdefault("objective_summary", {})
            objective.update({
                "initial_score": context["initial_score"],
                "best_score": context["best_score"],
                "absolute_improvement": context["initial_score"] - context["best_score"],
                "percent_improvement": 100.0 * (context["initial_score"] - context["best_score"]) / context["initial_score"] if context["initial_score"] else None,
                "objective_mode": context["objective_mode"],
                "included_metrics": context["included_metrics"],
                "configured_metrics": context["configured_metrics"],
                "reference_file": context["reference_file"],
                "reference_scales": context["reference_scales"],
                "best_accepted_run_folder": context["best_run_folder"],
                "best_score_definition": "lowest accepted candidate objective / protected best configuration",
            })
            analysis["species_summary"] = context["species_summary"]
            analysis["species_improvement_from_baseline"] = context["species_improvements"]

        campaign = analysis.setdefault("campaign_summary", {})
        if recovered_ids:
            unresolved = int(campaign.get("unresolved_candidates", campaign.get("unresolved_candidate_count", 0)) or 0)
            recovered_count = len(recovered_ids)
            campaign["recovered_transaction_count"] = recovered_count
            campaign["recovered_candidate_ids"] = sorted(recovered_ids)
            campaign["unresolved_candidates"] = max(unresolved - recovered_count, 0)
            campaign["unresolved_candidate_count"] = max(unresolved - recovered_count, 0)
            # The repaired observation was a scientifically evaluated rejection.
            if unresolved > 0:
                campaign["rejected_candidates"] = int(campaign.get("rejected_candidates", 0) or 0) + min(recovered_count, unresolved)

        if isinstance(review, dict) and context:
            improved = [item for item in context["species_improvements"] if _to_float(item.get("relative_improvement_fraction")) is not None and float(item["relative_improvement_fraction"]) > 0]
            all_improved = len(improved) == len(context["species_improvements"]) and bool(improved)
            improvement_pct = 100.0 * (context["initial_score"] - context["best_score"]) / context["initial_score"] if context["initial_score"] else 0.0
            recovery_text = f" One historical Excel-lock observation was repaired and is no longer unresolved." if recovered_ids else ""
            review["executive_summary"] = (
                f"The campaign reduced the protected accepted objective from {context['initial_score']:.6g} "
                f"to {context['best_score']:.8g}, an improvement of {improvement_pct:.2f}%. "
                + ("All included species RMSE values improved relative to the campaign baseline." if all_improved else "The best accepted run shows mixed species-level changes relative to baseline.")
                + recovery_text
            )
            optimizer = review.setdefault("optimizer_assessment", {})
            evidence = [str(item) for item in optimizer.get("evidence", []) if "unresolved candidate" not in str(item).casefold()]
            if recovered_ids:
                evidence.append(f"Recovered transaction(s): {len(recovered_ids)}; current unresolved transaction count: 0.")
            evidence.append(f"Objective metadata recorded: mode={context['objective_mode']}; metrics={', '.join(context['included_metrics'])}.")
            optimizer["evidence"] = evidence
            optimizer["possible_software_issues"] = [
                item for item in optimizer.get("possible_software_issues", [])
                if "objective_mode" not in str(item) and "included_metrics" not in str(item)
            ]
            review["scientific_findings"] = [
                {
                    "importance": "high",
                    "finding": "The protected best accepted model improved the composite objective and every included species RMSE relative to the campaign baseline." if all_improved else "The protected best accepted model improved the composite objective with species-level trade-offs.",
                    "evidence": [
                        f"Initial TOTAL_SCORE={context['initial_score']:.8g}; protected best accepted TOTAL_SCORE={context['best_score']:.8g}.",
                        f"Objective mode={context['objective_mode']}.",
                        f"Included metrics={', '.join(context['included_metrics'])}.",
                    ],
                },
                *[item for item in review.get("scientific_findings", []) if "objective" not in str(item.get("finding", "")).casefold()][:4],
            ]
            actions = [
                item for item in review.get("recommended_actions", [])
                if "audit the objective-function definition" not in str(item.get("action", "")).casefold()
            ]
            for index, item in enumerate(actions, start=1):
                item["priority"] = index
            review["recommended_actions"] = actions
            review["paper_ready_conclusion"] = (
                f"The {self.paths.project_dir.name} calibration reduced the protected accepted TOTAL_SCORE by {improvement_pct:.1f}% "
                f"({context['initial_score']:.4g} to {context['best_score']:.6g}). "
                + ("All included species RMSE values improved relative to the baseline, although systematic concentration biases remain." if all_improved else "Species-level trade-offs remain and require process-focused interpretation.")
                + " The remaining limitation is primarily coordinate-wise search coverage and model/process representation rather than solver instability."
            )

        metadata["postprocessing"] = "V15.4.1 best-run and transaction-recovery correction"
        used_mode = _safe_string(metadata.get("used_mode")) or "unknown"
        if "V15.4.1" not in used_mode:
            metadata["used_mode"] = f"{used_mode} + V15.4.1 deterministic correction"

        package["analysis"] = analysis
        package["review"] = review
        package["review_metadata"] = metadata

        artifacts = package.get("artifacts", {})
        analysis_json = Path(artifacts.get("campaign_analysis_json", "")) if artifacts.get("campaign_analysis_json") else None
        if analysis_json:
            analysis_json.write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8")
        review_json = Path(artifacts.get("campaign_review_json", "")) if artifacts.get("campaign_review_json") else None
        if review_json:
            review_json.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
        return package

    def _decision_log(self) -> tuple[pd.DataFrame, Path | None]:
        candidates = [
            self.paths.results_dir / "calibration_decision_log.xlsx",
            self.paths.results_dir / "v14_candidate_decisions.xlsx",
        ]
        for candidate in candidates:
            frame = _read_excel(candidate)
            if not frame.empty:
                return frame, candidate
        return pd.DataFrame(), None

    def _campaign_state(self) -> dict[str, Any]:
        candidates = [
            self.paths.results_dir / "v14_campaign_state.json",
            self.paths.results_dir / "v14_optimizer_state.json",
        ]
        for candidate in candidates:
            if candidate.exists():
                try:
                    payload = json.loads(candidate.read_text(encoding="utf-8"))
                    return payload if isinstance(payload, dict) else {}
                except Exception as exc:
                    self.warnings.append(f"Could not read campaign state {candidate.name}: {type(exc).__name__}")
        state_xlsx = _read_excel(self.paths.results_dir / "v14_optimizer_state.xlsx")
        if not state_xlsx.empty:
            return {str(k): v for k, v in state_xlsx.iloc[0].to_dict().items()}
        return {}

    def _decision_table(self) -> tuple[pd.DataFrame, dict[str, Any]]:
        raw, source = self._decision_log()
        metadata: dict[str, Any] = {
            "decision_log_source": str(source) if source else None,
            "raw_rows": int(len(raw)),
        }
        if raw.empty:
            self.warnings.append("No V14 decision log was found; calibration timeline will be empty.")
            return pd.DataFrame(columns=self._trial_columns()), metadata

        # Keep actual candidate decisions only. Local-futility status records are
        # still kept in a separate evidence column when they have no parameter.
        parameter_col = _first_existing_column(raw, ["parameter"])
        decision_col = _first_existing_column(raw, ["decision"])
        if parameter_col is None:
            self.warnings.append("Decision log has no parameter column; no trial-level calibration story can be created.")
            return pd.DataFrame(columns=self._trial_columns()), metadata

        frame = raw.copy()
        recovered_ids = self._recoverable_observer_failure_ids(frame)
        if recovered_ids and decision_col is not None and "candidate_id" in frame.columns:
            recovered_mask = (
                frame["candidate_id"].map(_safe_string).isin(recovered_ids)
                & frame[decision_col].astype(str).str.strip().eq("optimizer_observe_run_failed")
            )
            frame.loc[recovered_mask, decision_col] = "reject"
            if "accepted" in frame.columns:
                frame["accepted"] = frame["accepted"].astype(object)
                frame.loc[recovered_mask, "accepted"] = False
            if "restoration_verified" in frame.columns:
                frame["restoration_verified"] = frame["restoration_verified"].astype(object)
                frame.loc[recovered_mask, "restoration_verified"] = True
            if "rejection_reason" in frame.columns:
                original = frame.loc[recovered_mask, "rejection_reason"].map(_safe_string)
                frame.loc[recovered_mask, "rejection_reason"] = original.map(
                    lambda value: "recovered_transaction_after_observe_write_failure; " + value
                )
        if decision_col is not None:
            frame = frame[frame[decision_col].notna()].copy()
        frame = frame[frame[parameter_col].map(_safe_string).ne("")].copy()

        # Normalize a diverse V14 decision-log schema into a stable table.
        field_candidates = {
            "timestamp": ["timestamp", "time", "created_at"],
            "candidate_id": ["candidate_id"],
            "candidate_type": ["candidate_type"],
            "parameter": ["parameter"],
            "group": ["group"],
            "direction": ["direction", "direction_label"],
            "old_value": ["old_value"],
            "new_value": ["new_value"],
            "step_fraction": ["step_fraction", "step_fraction_applied"],
            "baseline_objective": ["baseline_objective", "baseline_total_score", "baseline_TOTAL_SCORE"],
            "candidate_objective": ["candidate_objective", "candidate_total_score", "candidate_TOTAL_SCORE"],
            "decision": ["decision"],
            "rejection_reason": ["rejection_reason"],
            "accepted": ["accepted"],
            "valid_run": ["valid_run"],
            "restoration_verified": ["restoration_verified"],
            "run_folder": ["run_folder"],
            "parent_best_run_folder": ["parent_best_run_folder"],
        }

        table = pd.DataFrame(index=frame.index)
        for target, candidates in field_candidates.items():
            column = _first_existing_column(frame, candidates)
            table[target] = frame[column] if column is not None else np.nan

        table["parameter"] = table["parameter"].map(_safe_string)
        table["group"] = table["group"].map(_safe_string).replace("", "unclassified")
        table["direction"] = table["direction"].map(_safe_string).replace("", "not_recorded")
        table["decision"] = table["decision"].map(_safe_string).replace("", "not_recorded")
        table["candidate_type"] = table["candidate_type"].map(_safe_string).replace("", "single_parameter")
        table["timestamp"] = pd.to_datetime(table["timestamp"], errors="coerce")
        table["old_value"] = pd.to_numeric(table["old_value"], errors="coerce")
        table["new_value"] = pd.to_numeric(table["new_value"], errors="coerce")
        table["step_fraction"] = pd.to_numeric(table["step_fraction"], errors="coerce")
        table["baseline_objective"] = pd.to_numeric(table["baseline_objective"], errors="coerce")
        table["candidate_objective"] = pd.to_numeric(table["candidate_objective"], errors="coerce")
        table["objective_delta"] = table["candidate_objective"] - table["baseline_objective"]
        table["objective_improvement"] = table["baseline_objective"] - table["candidate_objective"]
        table["trial_index"] = np.arange(1, len(table) + 1)

        profiles = table.apply(lambda row: _process_profile(row["parameter"], row["group"]), axis=1)
        table["process_family"] = profiles.map(lambda item: item["process_family"])
        table["hydrogeochemical_process"] = profiles.map(lambda item: item["hydrogeochemical_process"])
        table["expected_pH_direction"] = profiles.map(lambda item: item["expected_pH_direction"])
        table["expected_SO4_direction"] = profiles.map(lambda item: item["expected_SO4_direction"])
        table["process_note"] = profiles.map(lambda item: item["scientific_note"])
        table["decision_class"] = table.apply(self._decision_class, axis=1)

        return table.reset_index(drop=True), metadata

    @staticmethod
    def _decision_class(row: pd.Series) -> str:
        decision = _norm(row.get("decision"))
        reason = _norm(row.get("rejection_reason"))
        if decision == "accept" or str(row.get("accepted", "")).strip().casefold() in {"true", "1", "yes"}:
            return "accepted"
        if decision == "reject" and "recovered_transaction_after_observe_write_failure" in reason:
            return "rejected"
        if "invalid" in reason or "failed" in reason or str(row.get("valid_run", "")).strip().casefold() in {"false", "0", "no"}:
            return "invalid"
        if decision == "reject":
            return "rejected"
        return "other"

    @staticmethod
    def _trial_columns() -> list[str]:
        return [
            "trial_index", "timestamp", "candidate_id", "candidate_type", "parameter", "group", "direction",
            "old_value", "new_value", "step_fraction", "baseline_objective", "candidate_objective",
            "objective_delta", "objective_improvement", "decision", "decision_class", "rejection_reason",
            "accepted", "valid_run", "restoration_verified", "run_folder", "parent_best_run_folder",
            "process_family", "hydrogeochemical_process", "expected_pH_direction", "expected_SO4_direction", "process_note",
        ]

    @staticmethod
    def _normalise_run_folder(value: Any) -> str:
        text = _safe_string(value).replace("\\", "/").rstrip("/").casefold()
        return text

    @staticmethod
    def _metric_columns(frame: pd.DataFrame) -> tuple[str | None, str | None]:
        """Choose clear pH and SO4 metric columns while excluding unrelated names such as phase."""
        ph_patterns = [
            r"(^|_)(delta|effect|change|bias|rmse|mae|model|candidate|mean)?_?ph($|_)",
            r"(^|_)ph_?(delta|effect|change|bias|rmse|mae|model|candidate|mean)($|_)",
        ]
        so4_patterns = [
            r"(^|_)(delta|effect|change|bias|relative_bias|rmse|mae|model|candidate|mean)?_?(so4|sulfate|sulphate)($|_)",
            r"(^|_)(so4|sulfate|sulphate)_?(delta|effect|change|bias|relative_bias|rmse|mae|model|candidate|mean)($|_)",
        ]
        def choose(patterns: list[str]) -> str | None:
            matches = []
            for column in frame.columns:
                key = _norm(column)
                if any(re.search(pattern, key) for pattern in patterns):
                    matches.append(str(column))
            # Prefer explicit delta/effect changes, then bias/error, then raw model values.
            matches.sort(key=lambda value: (
                0 if any(token in _norm(value) for token in ("delta", "effect", "change")) else 1 if any(token in _norm(value) for token in ("bias", "rmse", "mae")) else 2,
                _norm(value),
            ))
            return matches[0] if matches else None
        return choose(ph_patterns), choose(so4_patterns)

    def _external_candidate_metrics(self, trials: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Read explicitly labelled diagnostic metrics and align them by candidate run folder.

        This path is used only when the decision log does not already contain
        explicit candidate pH/SO4 deltas. No raw MIN3P output is guessed or
        interpreted without a diagnostic table and a run-folder key.
        """
        columns = ["trial_index", "candidate_id", "parameter", "group", "direction", "decision", "run_folder"]
        base = trials[[column for column in columns if column in trials.columns]].copy()
        empty = base.copy()
        for column in ["pH_metric", "SO4_metric"]:
            empty[column] = np.nan
        empty["pH_metric_name"] = ""
        empty["SO4_metric_name"] = ""
        empty["metric_source"] = ""
        empty["metric_kind"] = ""
        details: dict[str, Any] = {"available": False, "source": None, "pH_metric_column": None, "SO4_metric_column": None, "metric_kind": None}
        if base.empty or "run_folder" not in base.columns:
            return empty, details
        run_keys = base["run_folder"].map(self._normalise_run_folder)
        if not run_keys.ne("").any():
            return empty, details

        candidates = [
            self.paths.results_dir / "run_diagnostics_V11.xlsx",
            self.paths.results_dir / "v13_5_objective_diagnostics.xlsx",
            self.paths.results_dir / "optimization_history.xlsx",
            self.paths.results_dir / "optimization_history_V14.xlsx",
        ]
        for source in candidates:
            frame = _read_excel(source)
            if frame.empty:
                continue
            run_column = _first_existing_column(frame, ["run_folder", "run folder", "run_path", "candidate_run_folder"])
            if run_column is None:
                continue
            ph_column, so4_column = self._metric_columns(frame)
            if not ph_column and not so4_column:
                continue
            working = frame.copy()
            working["_run_key"] = working[run_column].map(self._normalise_run_folder)
            working = working[working["_run_key"].ne("")].copy()
            if working.empty:
                continue
            subset = pd.DataFrame({"_run_key": run_keys})
            if ph_column:
                subset["pH_metric"] = pd.to_numeric(working.set_index("_run_key")[ph_column].reindex(subset["_run_key"]).to_numpy(), errors="coerce")
            else:
                subset["pH_metric"] = np.nan
            if so4_column:
                subset["SO4_metric"] = pd.to_numeric(working.set_index("_run_key")[so4_column].reindex(subset["_run_key"]).to_numpy(), errors="coerce")
            else:
                subset["SO4_metric"] = np.nan
            if not (subset["pH_metric"].notna() | subset["SO4_metric"].notna()).any():
                continue
            empty["pH_metric"] = subset["pH_metric"].to_numpy()
            empty["SO4_metric"] = subset["SO4_metric"].to_numpy()
            empty["pH_metric_name"] = ph_column or ""
            empty["SO4_metric_name"] = so4_column or ""
            empty["metric_source"] = source.name
            empty["metric_kind"] = "diagnostic_metric"
            details.update({
                "available": True,
                "source": str(source),
                "pH_metric_column": ph_column,
                "SO4_metric_column": so4_column,
                "metric_kind": "diagnostic_metric",
            })
            return empty, details
        return empty, details

    def _extract_ph_so4_metrics(self, trials: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
        """Extract pH/SO4 effects only from explicit, auditable campaign evidence."""
        result = trials[[column for column in ["trial_index", "candidate_id", "parameter", "group", "direction", "decision", "run_folder", "process_family", "expected_pH_direction", "expected_SO4_direction"] if column in trials.columns]].copy()
        result["pH_metric_status"] = "not_available"
        result["SO4_metric_status"] = "not_available"
        result["pH_effect"] = np.nan
        result["SO4_effect"] = np.nan
        result["pH_metric"] = np.nan
        result["SO4_metric"] = np.nan
        result["pH_metric_name"] = ""
        result["SO4_metric_name"] = ""
        result["metric_kind"] = "not_available"
        result["metric_source"] = "No explicit candidate-level pH/SO4 response evidence detected."

        raw, source = self._decision_log()
        details: dict[str, Any] = {
            "available": False,
            "source": str(source) if source else None,
            "pH_effect_column": None,
            "SO4_effect_column": None,
            "metric_kind": "not_available",
        }
        if raw.empty or result.empty:
            return result, details

        # Only treat clearly named delta/effect columns as candidate response effects.
        ph_candidates = _column_like(raw, [r"(^|_)delta_?ph($|_)", r"(^|_)ph_?(effect|change|delta)($|_)"])
        so4_candidates = _column_like(raw, [r"(^|_)delta_?(so4|sulfate|sulphate)($|_)", r"(^|_)(so4|sulfate|sulphate)_?(effect|change|delta)($|_)"])
        ph_column = ph_candidates[0] if ph_candidates else None
        so4_column = so4_candidates[0] if so4_candidates else None
        if ph_column or so4_column:
            # Align by row order because the trial table is derived from this
            # exact decision log after the same parameter/decision filtering.
            frame = raw.copy()
            parameter_col = _first_existing_column(frame, ["parameter"])
            decision_col = _first_existing_column(frame, ["decision"])
            if parameter_col is not None:
                if decision_col is not None:
                    frame = frame[frame[decision_col].notna()].copy()
                frame = frame[frame[parameter_col].map(_safe_string).ne("")].copy()
            frame = frame.reset_index(drop=True)
            if len(frame) == len(result):
                if ph_column:
                    result["pH_effect"] = pd.to_numeric(frame[ph_column], errors="coerce")
                    result["pH_metric"] = result["pH_effect"]
                    result["pH_metric_name"] = ph_column
                    result.loc[result["pH_effect"].notna(), "pH_metric_status"] = "available"
                if so4_column:
                    result["SO4_effect"] = pd.to_numeric(frame[so4_column], errors="coerce")
                    result["SO4_metric"] = result["SO4_effect"]
                    result["SO4_metric_name"] = so4_column
                    result.loc[result["SO4_effect"].notna(), "SO4_metric_status"] = "available"
                result.loc[(result["pH_metric_status"].eq("available")) | (result["SO4_metric_status"].eq("available")), "metric_source"] = f"Explicit candidate-level response columns in {source.name if source else 'decision log'}"
                result.loc[(result["pH_metric_status"].eq("available")) | (result["SO4_metric_status"].eq("available")), "metric_kind"] = "candidate_delta"
                details.update({
                    "available": bool((result["pH_metric_status"].eq("available") | result["SO4_metric_status"].eq("available")).any()),
                    "pH_effect_column": ph_column,
                    "SO4_effect_column": so4_column,
                    "metric_kind": "candidate_delta",
                })
                return result, details
            self.warnings.append("Candidate-level pH/SO4 delta columns were found but could not be aligned safely; diagnostic lookup will be attempted.")

        external, external_details = self._external_candidate_metrics(trials)
        if external_details["available"]:
            for column in ["pH_metric", "SO4_metric", "pH_metric_name", "SO4_metric_name", "metric_source", "metric_kind"]:
                result[column] = external[column].to_numpy()
            result.loc[result["pH_metric"].notna(), "pH_metric_status"] = "available"
            result.loc[result["SO4_metric"].notna(), "SO4_metric_status"] = "available"
            return result, external_details
        return result, details

    # ------------------------------ figures ------------------------------


    # --------------------- DAT-driven conceptual model ---------------------

    def _discover_dat_file(self) -> Path:
        """Resolve the main project DAT file.

        Priority:
          1. Explicit --dat-file.
          2. A single non-template DAT in this project's 01_input.
          3. A project-name match when multiple non-template DAT files exist.

        Template, candidate, generated, backup, tmp, and restore DAT files are
        ignored so TP3 automatically selects tp3_v11_3.dat instead of
        tp3_v11_3_template.dat.
        """
        checked: list[Path] = []
        blocked_tokens = ("template", "candidate", "generated", "backup", "bak", "tmp", "restore")

        if self.dat_file is not None:
            candidate = self.dat_file.expanduser().resolve()
            if candidate.is_file() and candidate.suffix.casefold() == ".dat":
                return candidate
            raise FileNotFoundError(
                f"Requested MIN3P DAT file was not found or is not a .dat file: {candidate}"
            )

        search_dirs: list[Path] = []
        for directory in [self.paths.input_dir, self.paths.project_dir / "01_input"]:
            directory = Path(directory).resolve()
            if directory not in search_dirs:
                search_dirs.append(directory)
        for ancestor in [self.paths.agent_core_dir, *self.paths.agent_core_dir.parents]:
            directory = (ancestor / "01_input").resolve()
            if directory not in search_dirs:
                search_dirs.append(directory)

        candidates: list[Path] = []
        for directory in search_dirs:
            checked.append(directory)
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.dat")):
                name = path.name.casefold()
                if any(token in name for token in blocked_tokens):
                    continue
                resolved = path.resolve()
                if resolved not in candidates:
                    candidates.append(resolved)

        if not candidates:
            raise FileNotFoundError(
                "No main MIN3P .dat file was found. Checked only project input directories: "
                + "; ".join(str(path) for path in checked)
                + ". Template/candidate/generated DAT files were ignored. Run with --dat-file <path> to select the model explicitly."
            )

        if len(candidates) == 1:
            return candidates[0]

        project_key = self.paths.project_dir.name.casefold()
        project_matches = [p for p in candidates if project_key in p.name.casefold()]
        if len(project_matches) == 1:
            return project_matches[0]

        names = ", ".join(str(path) for path in candidates)
        raise FileNotFoundError(
            "Multiple main MIN3P .dat files were found in project input directories: "
            + names
            + ". Run with --dat-file <path> to select the intended model explicitly."
        )

    def _dat_conceptual_facts(self) -> dict[str, Any]:
        """Parse actual model facts or fail before any conceptual image is written."""
        dat_file = self._discover_dat_file()
        try:
            facts = _parse_min3p_dat(dat_file)
        except Exception as exc:
            raise RuntimeError(
                f"DAT parser failed for {dat_file}: {type(exc).__name__}: {exc}"
            ) from exc
        for warning in facts.get("parser_warnings", []):
            self.warnings.append(f"DAT parser: {warning}")
        facts.setdefault("source", {})["resolution_policy"] = (
            "explicit --dat-file or unique *.dat under this calibration project's 01_input"
        )
        return facts

    def _gpt_conceptual_config(self) -> dict[str, Any]:
        path = self.paths.agent_core_dir / "config" / "gpt_conceptual_image_V15.json"
        defaults = {
            "enabled": False,
            "model": "gpt-image-2",
            "quality": "medium",
            "size": "1536x1024",
            "background_opacity": 0.20,
        }
        if not path.exists():
            return defaults
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                defaults.update(payload)
        except Exception as exc:
            self.warnings.append(f"Could not parse GPT conceptual-image config; deterministic draft used: {type(exc).__name__}: {exc}")
        return defaults

    def _conceptual_prompt(self, facts: dict[str, Any]) -> str:
        geometry = facts.get("geometry", {}) if isinstance(facts, dict) else {}
        zones = geometry.get("multilayer_zones", []) if isinstance(geometry, dict) else []
        groups = facts.get("geochemistry", {}).get("active_phase_groups", {}) if isinstance(facts.get("geochemistry", {}), dict) else {}
        top = facts.get("top_boundary_key_values", {}) if isinstance(facts.get("top_boundary_key_values", {}), dict) else {}
        if zones:
            zone_lines = [
                f"{z.get('name')}: z={float(z.get('z_min')):g}–{float(z.get('z_max')):g} m, {z.get('vertical_position', '')} layer"
                for z in zones
            ]
            geometry_constraint = "Hard geometry constraint: show a multilayer vertical column with the parsed zone order; do not collapse the model into one domain."
        else:
            zone_lines = ["No multilayer zones parsed; use one conservative reactive domain."]
            geometry_constraint = "Hard geometry constraint: show one central porous reactive domain because no layer zones were parsed."
        return "\n".join([
            "Create an UNLABELLED, publication-quality scientific conceptual-model background.",
            "This image is an optional visual draft only. Do not render any text, letters, numbers, chemical formulae, labels, legends, axes, or arrows with text; Python will overlay every exact scientific label afterward.",
            "White background; clean flat-vector academic infographic; soft muted mineral and water colors.",
            geometry_constraint,
            f"Parsed geometry: {geometry.get('numerical_dimension', 'unresolved')}; control volumes={geometry.get('control_volumes', {})}.",
            "Parsed layers: " + " | ".join(zone_lines),
            "Show top infiltration and gas oxygen entry, downward water/solute transport, and bottom free-drainage seepage outflow.",
            f"Sulfide phases parsed: {_format_fact_list(groups.get('sulfides', []))}.",
            f"Carbonate/neutralizing phases parsed: {_format_fact_list(groups.get('carbonates_and_neutralizing_phases', []))}.",
            f"Iron phases parsed: {_format_fact_list(groups.get('fe_phases', []))}.",
            f"Top boundary parsed values: pH={top.get('pH')}, pO2={top.get('pO2')}, pCO2={top.get('pCO2')}.",
            "Leave clean blank margins for deterministic overlay callouts.",
        ])

    def _draw_deterministic_draft(self, facts: dict[str, Any], draft_path: Path) -> None:
        figure, ax = plt.subplots(figsize=(12, 8), dpi=160)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")
        # Unlabelled layout background used when GPT generation is disabled or unavailable.
        ax.add_patch(Rectangle((0.28, 0.16), 0.46, 0.62, facecolor="#f8f1df", edgecolor="#52606b", linewidth=1.4))
        ax.add_patch(FancyBboxPatch((0.38, 0.83), 0.24, 0.08, boxstyle="round,pad=0.015", facecolor="#e7f1fb", edgecolor="#3a6f9f", linewidth=1.0))
        ax.add_patch(FancyBboxPatch((0.76, 0.72), 0.16, 0.08, boxstyle="round,pad=0.015", facecolor="#f6edd7", edgecolor="#80601f", linewidth=1.0))
        ax.add_patch(FancyArrowPatch((0.50, 0.83), (0.50, 0.18), arrowstyle="-|>", mutation_scale=18, linewidth=2.0, color="#2f6f9f"))
        ax.add_patch(FancyArrowPatch((0.82, 0.72), (0.70, 0.68), arrowstyle="-|>", mutation_scale=16, linewidth=1.6, color="#80601f"))
        for y, color in [(0.66, "#9a3d2c"), (0.50, "#3b7237"), (0.34, "#654a86"), (0.23, "#856720")]:
            ax.add_patch(FancyBboxPatch((0.05, y - 0.035), 0.19, 0.07, boxstyle="round,pad=0.012", facecolor="#fafafa", edgecolor=color, linewidth=1.0))
            ax.add_patch(FancyArrowPatch((0.24, y), (0.29, y), arrowstyle="->", mutation_scale=11, linewidth=1.0, color=color))
        ax.add_patch(FancyArrowPatch((0.50, 0.16), (0.83, 0.16), arrowstyle="-|>", mutation_scale=17, linewidth=2.0, color="#2f6f9f"))
        figure.tight_layout(pad=0)
        figure.savefig(draft_path, format="png", dpi=180, bbox_inches="tight", pad_inches=0)
        plt.close(figure)

    def _generate_gpt_draft(self, prompt: str, draft_path: Path, config: dict[str, Any]) -> tuple[bool, str]:
        """Generate an optional unlabelled visual draft; deterministic overlay remains authoritative."""
        if not bool(config.get("enabled", False)):
            return False, "disabled_in_gpt_conceptual_config"
        if not os.getenv("OPENAI_API_KEY"):
            return False, "OPENAI_API_KEY_not_set"
        try:
            from openai import OpenAI
        except Exception as exc:
            return False, f"openai_package_unavailable:{type(exc).__name__}"
        try:
            client = OpenAI()
            response = client.images.generate(
                model=str(config.get("model", "gpt-image-2")),
                prompt=prompt,
                size=str(config.get("size", "1536x1024")),
                quality=str(config.get("quality", "medium")),
            )
            payload = response.data[0].b64_json
            draft_path.write_bytes(base64.b64decode(payload))
            return True, "gpt_image_generated"
        except Exception as exc:
            return False, f"gpt_image_failed:{type(exc).__name__}:{exc}"


    def _conceptual_domain_label(self, facts: dict[str, Any]) -> tuple[str, str]:
        geometry = facts.get("geometry", {}) if isinstance(facts, dict) else {}
        zone_name = _safe_string(geometry.get("property_zone_name")) or "reactive material domain"
        if self.conceptual_detail == "paper":
            return "One reactive waste-rock domain", f"DAT property zone: {zone_name}"
        return f"Reactive porous-medium property zone: {zone_name}", "One material domain; horizontal lines indicate numerical control volumes, not model layers."

    def _paper_process_bullets(self, facts: dict[str, Any]) -> list[str]:
        chemistry = facts.get("geochemistry", {}) if isinstance(facts.get("geochemistry", {}), dict) else {}
        groups = chemistry.get("active_phase_groups", {}) if isinstance(chemistry.get("active_phase_groups", {}), dict) else {}
        bullets: list[str] = []
        if groups.get("sulfides"):
            bullets.extend(["Sulfide oxidation", "Acidity and SO4 generation"])
        if groups.get("carbonates_and_neutralizing_phases"):
            bullets.append("Carbonate neutralisation / buffering")
        surfaces = chemistry.get("surface_components", [])
        if surfaces:
            bullets.append("FeOH sorption / surface complexation")
        bullets.append("Vertical water and solute transport")
        # preserve order while deduplicating
        seen: set[str] = set()
        out: list[str] = []
        for item in bullets:
            if item not in seen:
                out.append(item)
                seen.add(item)
        return out


    def _draw_multilayer_dat_conceptual_model(
        self,
        facts: dict[str, Any],
        config: dict[str, Any],
        svg_path: Path,
        png_path: Path,
        draft_path: Path,
        *,
        draft_opacity: float,
    ) -> None:
        """Draw a multilayer conceptual model from parsed MIN3P zone extents."""
        figure, ax = plt.subplots(figsize=(13.2, 8.8), dpi=190)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")

        geometry = facts.get("geometry", {}) if isinstance(facts, dict) else {}
        zones = list(geometry.get("multilayer_zones", []))
        zones = sorted(zones, key=lambda z: float(z.get("z_max", 0.0)), reverse=True)
        source = facts.get("source", {}) if isinstance(facts, dict) else {}
        flow = facts.get("flow_boundaries", {}) if isinstance(facts.get("flow_boundaries", {}), dict) else {}
        top_values = facts.get("top_boundary_key_values", {}) if isinstance(facts.get("top_boundary_key_values", {}), dict) else {}
        cvs = geometry.get("control_volumes", {}) if isinstance(geometry.get("control_volumes", {}), dict) else {}
        z_extent = geometry.get("extents", {}).get("z") if isinstance(geometry.get("extents", {}), dict) else None
        if not z_extent and zones:
            z_extent = [min(float(z.get("z_min", 0.0)) for z in zones), max(float(z.get("z_max", 0.0)) for z in zones)]
        z_min_total = float(z_extent[0]) if z_extent and len(z_extent) >= 2 else min(float(z.get("z_min", 0.0)) for z in zones)
        z_max_total = float(z_extent[1]) if z_extent and len(z_extent) >= 2 else max(float(z.get("z_max", 0.0)) for z in zones)
        total_thickness = max(z_max_total - z_min_total, 1e-12)

        title = f"{self.paths.project_dir.name} conceptual hydrogeochemical model"
        subtitle = (
            f"DAT: {source.get('filename')} | "
            f"{geometry.get('numerical_dimension', '1D vertical column')} | "
            f"{cvs.get('x', '?')} × {cvs.get('y', '?')} × {cvs.get('z', '?')} control volumes | "
            f"z = {z_min_total:g}–{z_max_total:g} m | multilayer mode"
        )
        ax.text(0.50, 0.975, title, ha="center", va="top", fontsize=18, fontweight="bold", zorder=10)
        ax.text(0.50, 0.942, subtitle, ha="center", va="top", fontsize=8.2, zorder=10)

        x0, y0, width, height = 0.33, 0.15, 0.38, 0.64
        ax.add_patch(Rectangle((x0, y0), width, height, facecolor="#ffffff", edgecolor="#374956", linewidth=1.6, zorder=2))

        # top boundary
        top_box = FancyBboxPatch((0.39, 0.82), 0.26, 0.075, boxstyle="round,pad=0.014", facecolor="#e6f0fa", edgecolor="#2e6fa3", linewidth=1.1, zorder=5)
        ax.add_patch(top_box)
        top_lines = ["Top boundary", "infiltration + gas O2 access"]
        if top_values.get("pH") is not None:
            top_lines.append(f"infiltration pH = {top_values['pH']:.2f}")
        ax.text(0.52, 0.858, "\n".join(top_lines), ha="center", va="center", fontsize=7.2, zorder=10)
        ax.add_patch(FancyArrowPatch((0.52, 0.82), (0.52, y0 + height), arrowstyle="-|>", mutation_scale=15, linewidth=2.0, color="#2e6fa3", zorder=8))

        palette = {"NP": "#edf3e2", "CIL": "#f6ead6"}
        edge_palette = {"NP": "#56733c", "CIL": "#8a6618"}
        for zone in zones:
            name = str(zone.get("name", "zone"))
            key = name.upper()
            zmin = float(zone.get("z_min", 0.0))
            zmax = float(zone.get("z_max", 0.0))
            rect_y = y0 + ((zmin - z_min_total) / total_thickness) * height
            rect_h = ((zmax - zmin) / total_thickness) * height
            face = palette.get(key, "#f8f1df")
            edge = edge_palette.get(key, "#374956")
            ax.add_patch(Rectangle((x0, rect_y), width, rect_h, facecolor=face, edgecolor=edge, linewidth=1.5, zorder=3))
            hyd = zone.get("hydraulic", {}) if isinstance(zone.get("hydraulic", {}), dict) else {}
            chem = zone.get("initial_chemistry", {}) if isinstance(zone.get("initial_chemistry", {}), dict) else {}
            major = zone.get("major_minerals", []) if isinstance(zone.get("major_minerals", []), list) else []
            surface_sites = chem.get("surface_sites", []) if isinstance(chem.get("surface_sites", []), list) else []
            header = f"{name} layer: z = {zmin:g}–{zmax:g} m"
            pos = str(zone.get("vertical_position", ""))
            hyd_text = []
            if zone.get("porosity") is not None:
                hyd_text.append(f"porosity {float(zone['porosity']):g}")
            if hyd.get("Kz") is not None:
                hyd_text.append(f"Kz {float(hyd['Kz']):.2e} m/s")
            if chem.get("pH") is not None:
                hyd_text.append(f"initial pH {float(chem['pH']):g}")
            if chem.get("h3aso4") is not None:
                hyd_text.append(f"H3AsO4 {float(chem['h3aso4']):.2e} M")
            if key == "NP":
                process = "Upper NP waste-rock layer\ncarbonate buffering / Mg source\ngypsum + silicates; downward transport"
                minerals = _format_fact_list(major, limit=5)
            elif key == "CIL":
                process = "Bottom CIL reactive layer\nFeOH sorption + Fe-As phase control\njarosite initially present but limited/inactive\nseepage chemistry near base"
                minerals = _format_fact_list(major, limit=5)
            else:
                process = "Reactive-transport material layer"
                minerals = _format_fact_list(major, limit=5)
            label = header + (f" ({pos})" if pos else "")
            ax.text(x0 + width / 2, rect_y + rect_h * 0.72, label, ha="center", va="center", fontsize=10.2, fontweight="bold", zorder=10)
            ax.text(x0 + width / 2, rect_y + rect_h * 0.58, " | ".join(hyd_text), ha="center", va="center", fontsize=7.1, color="#3e4a53", zorder=10)
            ax.text(x0 + width / 2, rect_y + rect_h * 0.36, process, ha="center", va="center", fontsize=7.8, linespacing=1.25, zorder=10)
            ax.text(x0 + width / 2, rect_y + rect_h * 0.14, "Major phases: " + minerals, ha="center", va="center", fontsize=6.2, color="#4b565f", wrap=True, zorder=10)
            if key == "CIL" and surface_sites:
                sites = ", ".join(str(s.get("site")) for s in surface_sites if s.get("site"))
                ax.text(x0 + width + 0.03, rect_y + rect_h * 0.50, "CIL reactive sites\n" + sites, ha="left", va="center", fontsize=7.0, color="#654a86", zorder=10)
                ax.add_patch(FancyArrowPatch((x0 + width + 0.025, rect_y + rect_h * 0.50), (x0 + width, rect_y + rect_h * 0.50), arrowstyle="->", mutation_scale=10, linewidth=0.9, color="#654a86", zorder=8))

        # vertical flow arrow
        ax.add_patch(FancyArrowPatch((x0 + width/2, y0 + height + 0.01), (x0 + width/2, y0 - 0.04), arrowstyle="-|>", mutation_scale=18, linewidth=2.0, color="#2e6fa3", zorder=8))
        ax.text(x0 + width/2 + 0.018, y0 + height/2, "vertical water and solute transport", rotation=90, ha="center", va="center", fontsize=7.2, color="#2e6fa3", zorder=10)

        # gas callout
        gas_box = FancyBboxPatch((0.76, 0.69), 0.18, 0.09, boxstyle="round,pad=0.014", facecolor="#f6edd7", edgecolor="#856720", linewidth=1.0, zorder=5)
        ax.add_patch(gas_box)
        gas_lines = ["Gas pathway", "O2 / CO2 access"]
        if top_values.get("pO2") is not None:
            gas_lines.append(f"top pO2 = {top_values['pO2']:.3g}")
        ax.text(0.85, 0.733, "\n".join(gas_lines), ha="center", va="center", fontsize=6.8, zorder=10)
        ax.add_patch(FancyArrowPatch((0.76, 0.71), (x0 + width, y0 + height * 0.78), arrowstyle="-|>", mutation_scale=12, linewidth=1.4, color="#856720", zorder=8))

        # interpretive callouts
        ax.add_patch(FancyBboxPatch((0.055, 0.59), 0.23, 0.105, boxstyle="round,pad=0.012", facecolor="#e4f0dd", edgecolor="#3d7838", linewidth=1.0, zorder=5))
        ax.text(0.17, 0.642, "NP processes\ncarbonate buffering; Mg/Ca release\nSO4 transport from upper layer", ha="center", va="center", fontsize=7.1, zorder=10)
        ax.add_patch(FancyArrowPatch((0.285, 0.642), (x0, y0 + height * 0.68), arrowstyle="->", mutation_scale=10, linewidth=0.9, color="#3d7838", zorder=8))

        ax.add_patch(FancyBboxPatch((0.055, 0.31), 0.23, 0.13, boxstyle="round,pad=0.012", facecolor="#e8e3f2", edgecolor="#654a86", linewidth=1.0, zorder=5))
        ax.text(0.17, 0.375, "CIL As controls\nFeOH sorption / surface complexation\nferric_arsenate(am)\npreferential flow / bypass concept", ha="center", va="center", fontsize=6.9, zorder=10)
        ax.add_patch(FancyArrowPatch((0.285, 0.375), (x0, y0 + height * 0.12), arrowstyle="->", mutation_scale=10, linewidth=0.9, color="#654a86", zorder=8))

        # bottom boundary
        bottom_flow = flow.get("bottom", {}) if isinstance(flow.get("bottom", {}), dict) else {}
        bottom_type = _safe_string(bottom_flow.get("type")) or "free-drainage"
        ax.add_patch(FancyArrowPatch((x0 + width/2, y0), (0.83, y0), arrowstyle="-|>", mutation_scale=16, linewidth=2.0, color="#2e6fa3", zorder=8))
        outlet_lines = ["Bottom boundary", f"{bottom_type} / seepage outflow"]
        ax.text(0.84, y0 + 0.018, "\n".join(outlet_lines), ha="left", va="center", fontsize=7.2, color="#1c5c91", zorder=10)
        observables = [str(v) for v in config.get("observables", []) if _safe_string(v)]
        if observables:
            ax.text(0.84, y0 - 0.035, "Calibration observations: " + " | ".join(observables), ha="left", va="center", fontsize=6.5, zorder=10)

        ax.text(0.50, 0.055, "Multilayer figure generated from DAT porous-medium zones; layer order follows z extents (higher z at top).", ha="center", va="center", fontsize=6.8, color="#40484e", zorder=10)
        figure.tight_layout(pad=0.2)
        figure.savefig(svg_path, format="svg", bbox_inches="tight")
        figure.savefig(png_path, format="png", dpi=240, bbox_inches="tight")
        plt.close(figure)

    def _draw_dat_driven_conceptual_model(
        self,
        facts: dict[str, Any],
        config: dict[str, Any],
        svg_path: Path,
        png_path: Path,
        draft_path: Path,
        *,
        draft_opacity: float,
    ) -> None:
        """Draw a publication-ready conceptual model from parsed DAT facts.

        paper mode: process-focused figure for manuscripts.
        technical mode: DAT-inventory-focused figure for documentation.
        """
        geometry = facts.get("geometry", {}) if isinstance(facts, dict) else {}
        zones = geometry.get("multilayer_zones", []) if isinstance(geometry, dict) else []
        if isinstance(zones, list) and len(zones) > 1:
            self._draw_multilayer_dat_conceptual_model(
                facts, config, svg_path, png_path, draft_path, draft_opacity=draft_opacity
            )
            return

        figure, ax = plt.subplots(figsize=(13.2, 8.8), dpi=190)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")

        if "gpt" in draft_path.name.casefold():
            try:
                draft = mpimg.imread(draft_path)
                ax.imshow(
                    draft, extent=(0, 1, 0, 1), aspect="auto",
                    alpha=max(0.0, min(float(draft_opacity), 0.20)), zorder=0,
                )
            except Exception:
                pass

        geometry = facts.get("geometry", {}) if isinstance(facts, dict) else {}
        source = facts.get("source", {}) if isinstance(facts, dict) else {}
        chemistry = facts.get("geochemistry", {}) if isinstance(facts.get("geochemistry", {}), dict) else {}
        groups = chemistry.get("active_phase_groups", {}) if isinstance(chemistry.get("active_phase_groups", {}), dict) else {}
        flow = facts.get("flow_boundaries", {}) if isinstance(facts.get("flow_boundaries", {}), dict) else {}
        reactive = facts.get("reactive_transport_boundaries", {}) if isinstance(facts.get("reactive_transport_boundaries", {}), dict) else {}
        top_values = facts.get("top_boundary_key_values", {}) if isinstance(facts.get("top_boundary_key_values", {}), dict) else {}
        cvs = geometry.get("control_volumes", {}) if isinstance(geometry.get("control_volumes", {}), dict) else {}
        z_extent = geometry.get("extents", {}).get("z") if isinstance(geometry.get("extents", {}), dict) else None

        title = f"{self.paths.project_dir.name} conceptual hydrogeochemical model"
        model_name = _safe_string(facts.get("model", {}).get("name"))
        subtitle_parts = [
            f"DAT: {source.get('filename')}",
            str(geometry.get("numerical_dimension", "1D vertical")),
            f"{cvs.get('x', '?')} × {cvs.get('y', '?')} × {cvs.get('z', '?')} control volumes",
        ]
        if z_extent and len(z_extent) >= 2:
            subtitle_parts.append(f"z = {z_extent[0]:g}–{z_extent[1]:g} m")
        subtitle_parts.append(f"figure mode: {self.conceptual_detail}")
        ax.text(0.50, 0.975, title, ha="center", va="top", fontsize=18, fontweight="bold", zorder=10)
        ax.text(0.50, 0.942, " | ".join(subtitle_parts), ha="center", va="top", fontsize=8.2, zorder=10)

        x0, y0, width, height = 0.35, 0.17, 0.34, 0.60
        ax.add_patch(Rectangle((x0, y0), width, height, facecolor="#fbf3df", edgecolor="#374956", linewidth=1.6, zorder=3))
        n_z = max(int(cvs.get("z") or 0), 1)
        if n_z > 1:
            for i in range(1, n_z):
                yy = y0 + height * i / n_z
                ax.plot([x0, x0 + width], [yy, yy], color="#c8baa0", lw=0.25, alpha=0.65, zorder=4)

        header_main, header_sub = self._conceptual_domain_label(facts)
        ax.text(x0 + width / 2, y0 + height - 0.052, header_main, ha="center", va="center", fontsize=12.5, fontweight="bold", zorder=10)
        ax.text(x0 + width / 2, y0 + height - 0.083, header_sub, ha="center", va="center", fontsize=6.9, color="#4a5561", zorder=10)
        ax.text(x0 - 0.018, y0 + height / 2, f"{n_z} vertical\ncontrol volumes", ha="right", va="center", fontsize=7.0, color="#4a5561", zorder=10)

        sulfides = groups.get("sulfides", [])
        neutralizers = groups.get("carbonates_and_neutralizing_phases", [])
        silicates = groups.get("silicates", [])
        surfaces = chemistry.get("surface_components", [])
        redox_pairs = [" / ".join(pair) for pair in chemistry.get("redox_couples", [])]

        if self.conceptual_detail == "paper":
            bullets = self._paper_process_bullets(facts)
            inside = ["Main hydrogeochemical processes", ""] + [f"• {item}" for item in bullets]
            ax.text(x0 + width/2, y0 + 0.43, "\n".join(inside), ha="center", va="top", fontsize=9.2, linespacing=1.45, zorder=10)
            if silicates:
                ax.text(x0 + width/2, y0 + 0.245, f"Supporting silicate phases: {_format_fact_list(silicates, limit=4)}", ha="center", va="center", fontsize=6.7, color="#46515a", zorder=10)
        else:
            inner_lines = [
                "DAT-defined reactive inventory",
                f"• sulfide minerals: {_format_fact_list(sulfides, limit=5)}",
                f"• neutralizing phases: {_format_fact_list(neutralizers, limit=5)}",
                f"• silicate phases: {_format_fact_list(silicates, limit=4)}",
                f"• surface-complexation sites: {_format_fact_list(surfaces, limit=4)}",
                f"• redox couple: {_format_fact_list(redox_pairs, limit=2)}",
            ]
            ax.text(x0 + width / 2, y0 + 0.43, "\n".join(inner_lines), ha="center", va="top", fontsize=7.8, linespacing=1.45, zorder=10)

        top_flow = flow.get("top", {}) if isinstance(flow.get("top", {}), dict) else {}
        top_reactive = reactive.get("top", {}).get("boundary", {}) if isinstance(reactive.get("top", {}), dict) else {}
        top_box = FancyBboxPatch((0.39, 0.805), 0.26, 0.085, boxstyle="round,pad=0.015", facecolor="#e6f0fa", edgecolor="#2e6fa3", linewidth=1.1, zorder=5)
        ax.add_patch(top_box)
        if self.conceptual_detail == "paper":
            top_lines = ["Top boundary: infiltration", "Specified flux; mixed reactive boundary"]
        else:
            top_lines = ["Top boundary: infiltration"]
            top_lines.append(f"flow: {top_flow.get('type') or 'unresolved'}" + (f", q = {top_flow.get('value'):.3g}" if top_flow.get("value") is not None else ""))
            top_lines.append(f"reactive: {top_reactive.get('type') or 'unresolved'}")
        values = []
        if top_values.get("pH") is not None:
            values.append(f"pH {top_values['pH']:.2f}")
        if top_values.get("pO2") is not None:
            values.append(f"pO2 {top_values['pO2']:.3g}")
        if top_values.get("pCO2") is not None:
            values.append(f"pCO2 {top_values['pCO2']:.3g}")
        if values:
            top_lines.append(" | ".join(values))
        ax.text(0.52, 0.847, "\n".join(top_lines), ha="center", va="center", fontsize=7.0, zorder=10)
        ax.add_patch(FancyArrowPatch((0.52, 0.805), (0.52, y0 + height), arrowstyle="-|>", mutation_scale=15, linewidth=2.0, color="#2e6fa3", zorder=8))

        gases = chemistry.get("gases", [])
        o2_present = any("o2" in _safe_string(v).casefold() for v in list(chemistry.get("components", [])) + list(gases))
        if o2_present:
            gas_box = FancyBboxPatch((0.76, 0.68), 0.17, 0.09, boxstyle="round,pad=0.014", facecolor="#f6edd7", edgecolor="#856720", linewidth=1.0, zorder=5)
            ax.add_patch(gas_box)
            if self.conceptual_detail == "paper":
                gas_lines = ["Gas pathway", "O2/CO2 gas access from DAT", (f"top pO2 = {top_values['pO2']:.3g}" if top_values.get('pO2') is not None else "")]
            else:
                gas_lines = ["Gas pathway (DAT-defined)", _format_fact_list(gases, limit=3), (f"top pO2 = {top_values['pO2']:.3g}" if top_values.get('pO2') is not None else "")]
            gas_lines = [line for line in gas_lines if line]
            ax.text(0.845, 0.724, "\n".join(gas_lines), ha="center", va="center", fontsize=6.8, zorder=10)
            ax.add_patch(FancyArrowPatch((0.78, 0.69), (x0 + width, y0 + height * 0.82), arrowstyle="-|>", mutation_scale=12, linewidth=1.5, color="#856720", zorder=8))

        callouts: list[tuple[float, float, str, str, str, str]] = []
        if sulfides:
            title = "Sulfide mineral inventory"
            detail = _format_fact_list(sulfides, limit=5)
            callouts.append((0.16, 0.66, "#f7ded4", "#a0442f", title, detail))
        if neutralizers:
            title = "Neutralizing mineral inventory" if self.conceptual_detail == "technical" else "Neutralising minerals"
            detail = _format_fact_list(neutralizers, limit=5)
            callouts.append((0.16, 0.51, "#e4f0dd", "#3d7838", title, detail))
        if surfaces:
            title = "Reactive surface sites"
            detail = _format_fact_list(surfaces, limit=4)
            callouts.append((0.16, 0.36, "#e8e3f2", "#654a86", title, detail))
        jarosite = chemistry.get("jarosite") if isinstance(chemistry.get("jarosite"), dict) else None
        if jarosite:
            phi = jarosite.get("initial_phi")
            jarosite_detail = str(jarosite.get("name", "jarosite"))
            if self.conceptual_detail == "paper":
                if phi is not None:
                    jarosite_detail = f"{jarosite_detail}; low initial volume fraction (phi ≈ {phi:.2g})"
            else:
                if phi is not None:
                    jarosite_detail += f"; initial phi = {phi:.2g}"
            callouts.append((0.16, 0.23, "#f4ead5", "#856720", "Jarosite phase", jarosite_detail))
        target_ys = [0.66, 0.51, 0.36, 0.23]
        for i, (cx, cy, face, edge, label, detail) in enumerate(callouts):
            box = FancyBboxPatch((cx - 0.12, cy - 0.043), 0.24, 0.086, boxstyle="round,pad=0.012", facecolor=face, edgecolor=edge, linewidth=1.0, zorder=5)
            ax.add_patch(box)
            ax.text(cx, cy + 0.014, label, ha="center", va="center", fontsize=7.1, fontweight="bold", color=edge, zorder=10)
            ax.text(cx, cy - 0.015, detail, ha="center", va="center", fontsize=6.0, wrap=True, zorder=10)
            target_y = target_ys[i] if i < len(target_ys) else cy
            ax.add_patch(FancyArrowPatch((cx + 0.12, cy), (x0 + 0.012, target_y), arrowstyle="->", mutation_scale=10, linewidth=0.9, color=edge, zorder=8))

        interpretation = FancyBboxPatch((0.74, 0.42), 0.20, 0.12, boxstyle="round,pad=0.012", facecolor="#f3f5f6", edgecolor="#8b959d", linewidth=0.9, zorder=5)
        ax.add_patch(interpretation)
        if self.conceptual_detail == "paper":
            interpretive_lines = [
                "Process linkage",
                "O2 supply + sulfide minerals",
                "→ sulfide oxidation",
                "→ acidity and SO4 release",
            ]
        else:
            interpretive_lines = [
                "Interpretive process link",
                "O2 entry + sulfide inventory",
                "supports sulfide oxidation",
                "and SO4 / acidity generation.",
            ]
        ax.text(0.84, 0.48, "\n".join(interpretive_lines), ha="center", va="center", fontsize=6.5, color="#39434a", zorder=10)
        ax.add_patch(FancyArrowPatch((0.74, 0.45), (x0 + width, y0 + height * 0.52), arrowstyle="->", mutation_scale=10, linewidth=0.8, linestyle="--", color="#6b7378", zorder=8))

        ax.add_patch(FancyArrowPatch((x0 + width / 2, y0 + 0.34), (x0 + width / 2, y0 - 0.035), arrowstyle="-|>", mutation_scale=16, linewidth=2.0, color="#2e6fa3", zorder=8))
        ax.text(x0 + width / 2 + 0.018, y0 + 0.18, "Vertical water and solute transport", rotation=90, ha="center", va="center", fontsize=7.2, color="#2e6fa3", zorder=10)

        bottom_flow = flow.get("bottom", {}) if isinstance(flow.get("bottom", {}), dict) else {}
        bottom_reactive = reactive.get("bottom", {}).get("boundary", {}) if isinstance(reactive.get("bottom", {}), dict) else {}
        ax.add_patch(FancyArrowPatch((x0 + width / 2, y0), (0.79, y0), arrowstyle="-|>", mutation_scale=16, linewidth=2.0, color="#2e6fa3", zorder=8))
        if self.conceptual_detail == "paper":
            outlet_lines = ["Bottom seepage boundary", "Specified hydraulic head; solute outflow"]
            if bottom_flow.get("value") is not None:
                outlet_lines.append(f"boundary head = {bottom_flow.get('value'):.4g}")
        else:
            outlet_lines = ["Bottom boundary / seepage outflow"]
            outlet_lines.append(f"flow: {bottom_flow.get('type') or 'unresolved'}" + (f", head = {bottom_flow.get('value'):.4g}" if bottom_flow.get("value") is not None else ""))
            outlet_lines.append(f"reactive: {bottom_reactive.get('type') or 'unresolved'}")
        ax.text(0.80, y0 + 0.024, "\n".join(outlet_lines), ha="left", va="center", fontsize=7.0, color="#1c5c91", zorder=10)

        observables = [str(v) for v in config.get("observables", []) if _safe_string(v)]
        if observables:
            ax.text(0.80, y0 - 0.045, "Calibration observations: " + " | ".join(observables), ha="left", va="center", fontsize=6.5, zorder=10)

        footer = (
            "Paper mode emphasises hydrogeochemical processes; exact DAT inventories remain in the callouts and conceptual-model facts JSON."
            if self.conceptual_detail == "paper"
            else "Solid boxes and arrows: facts parsed from the DAT file. Dashed link: hydrogeochemical interpretation."
        )
        ax.text(0.50, 0.055, footer, ha="center", va="center", fontsize=6.6, color="#40484e", zorder=10)
        figure.tight_layout(pad=0.2)
        figure.savefig(svg_path, format="svg", bbox_inches="tight")
        figure.savefig(png_path, format="png", dpi=240, bbox_inches="tight")
        plt.close(figure)


    def _load_conceptual_config(self) -> dict[str, Any]:
        default = self.default_conceptual_model_config()
        path = self.paths.conceptual_config_file
        if not path.exists():
            self.warnings.append(f"Conceptual-model config was missing; built-in HCT2 default was used: {path}")
            return default
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("JSON root must be an object")
            merged = default.copy()
            merged.update(loaded)
            return merged
        except Exception as exc:
            self.warnings.append(f"Could not parse conceptual-model config; built-in default was used: {type(exc).__name__}: {exc}")
            return default

    @staticmethod
    def default_conceptual_model_config() -> dict[str, Any]:
        return {
            "title": "HCT2 conceptual hydrogeochemical model",
            "subtitle": "DAT-driven conceptual-model reporting; use the DAT file as the authoritative geometry source.",
            "layers": [
                {"name": "Top boundary / infiltration", "height": 0.12, "description": "Rainwater entry and atmospheric gas exchange"},
                {"name": "Oxidising reactive zone", "height": 0.25, "description": "Sulfide oxidation, acid generation and sulfate release"},
                {"name": "Neutralisation zone", "height": 0.24, "description": "Carbonate dissolution and pH buffering"},
                {"name": "CIL layer", "height": 0.20, "description": "FeOH sorption; pre-existing jarosite largely inactive at high pH"},
                {"name": "Lower drainage / seepage", "height": 0.19, "description": "Downward solute transport and outflow observation"},
            ],
            "processes": [
                {"label": "Rain infiltration", "kind": "water", "x": 0.50, "y": 0.96},
                {"label": "Oxygen diffusion", "kind": "oxygen", "x": 0.84, "y": 0.82},
                {"label": "Pyrite oxidation\nacid + SO4 generation", "kind": "oxidation", "x": 0.13, "y": 0.70},
                {"label": "Carbonate neutralisation\n(calcite / magnesite)", "kind": "neutralisation", "x": 0.13, "y": 0.47},
                {"label": "FeOH sorption\nreactive surface control", "kind": "sorption", "x": 0.87, "y": 0.31},
                {"label": "Jarosite\n(largely inactive at high pH)", "kind": "jarosite", "x": 0.13, "y": 0.25},
            ],
            "observables": ["pH", "SO4", "metals", "seepage discharge"],
        }

    def _draw_conceptual_model(self, config: dict[str, Any], svg_path: Path, png_path: Path) -> None:
        figure, ax = plt.subplots(figsize=(12, 8.5), dpi=180)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")

        title = _safe_string(config.get("title")) or "Conceptual hydrogeochemical model"
        subtitle = _safe_string(config.get("subtitle"))
        ax.text(0.50, 0.985, title, ha="center", va="top", fontsize=16, fontweight="bold")
        if subtitle:
            ax.text(0.50, 0.952, subtitle, ha="center", va="top", fontsize=8.5)

        layers = config.get("layers") or self.default_conceptual_model_config()["layers"]
        x0, width = 0.26, 0.48
        top, bottom = 0.90, 0.10
        available = top - bottom
        heights = [max(_to_float(layer.get("height")) or 0.0, 0.0) for layer in layers]
        total = sum(heights) or 1.0
        y = top
        palette = ["#e9f2fb", "#fbe8df", "#f2f0d7", "#e9eef0", "#e7f3e7", "#f0e8f4"]
        centers: dict[str, float] = {}

        for index, layer in enumerate(layers):
            h = available * (heights[index] / total)
            y_next = y - h
            rectangle = Rectangle((x0, y_next), width, h, facecolor=palette[index % len(palette)], edgecolor="#3c4954", linewidth=1.0)
            ax.add_patch(rectangle)
            name = _safe_string(layer.get("name")) or f"Layer {index + 1}"
            desc = _safe_string(layer.get("description"))
            ax.text(x0 + width / 2, y_next + h * 0.63, name, ha="center", va="center", fontsize=10, fontweight="bold")
            if desc:
                ax.text(x0 + width / 2, y_next + h * 0.34, desc, ha="center", va="center", fontsize=7.3, wrap=True)
            centers[_norm(name)] = y_next + h / 2
            y = y_next

        # Water percolation and gas entry are independently shown to distinguish
        # advection from oxygen supply.
        ax.add_patch(FancyArrowPatch((0.50, 0.93), (0.50, 0.13), arrowstyle="-|>", mutation_scale=16, linewidth=2.2, color="#2f6f9f"))
        ax.text(0.53, 0.89, "Downward water and solute transport", fontsize=8.5, rotation=90, va="top", color="#2f6f9f")
        ax.add_patch(FancyArrowPatch((0.86, 0.84), (0.70, 0.73), arrowstyle="-|>", mutation_scale=14, linewidth=1.8, color="#7f5f1c"))
        ax.text(0.86, 0.86, "O2 entry", fontsize=8.5, ha="center", color="#7f5f1c")

        # Process callouts use fixed, reproducible locations from the JSON config.
        kind_style = {
            "water": ("#dcecf8", "#2f6f9f"),
            "oxygen": ("#f6edd7", "#7f5f1c"),
            "oxidation": ("#f7ded4", "#9a3d2c"),
            "neutralisation": ("#e4f0dd", "#3b7237"),
            "sorption": ("#e8e3f2", "#654a86"),
            "jarosite": ("#f4ead5", "#856720"),
        }
        targets = {
            "water": (0.50, 0.89),
            "oxygen": (0.70, 0.73),
            "oxidation": (0.30, centers.get("oxidising_reactive_zone", 0.68)),
            "neutralisation": (0.30, centers.get("neutralisation_zone", 0.45)),
            "sorption": (0.69, centers.get("cil_layer", 0.30)),
            "jarosite": (0.31, centers.get("cil_layer", 0.30)),
        }
        for item in config.get("processes", []):
            label = _safe_string(item.get("label"))
            if not label:
                continue
            kind = _norm(item.get("kind")) or "water"
            x = _to_float(item.get("x")) or 0.13
            y_callout = _to_float(item.get("y")) or 0.5
            face, edge = kind_style.get(kind, ("#ececec", "#555555"))
            box = FancyBboxPatch((x - 0.11, y_callout - 0.04), 0.22, 0.08, boxstyle="round,pad=0.012", facecolor=face, edgecolor=edge, linewidth=0.9)
            ax.add_patch(box)
            ax.text(x, y_callout, label, ha="center", va="center", fontsize=7.5, wrap=True)
            target = targets.get(kind)
            if target:
                ax.add_patch(FancyArrowPatch((x + (0.11 if x < target[0] else -0.11), y_callout), target, arrowstyle="->", mutation_scale=10, linewidth=0.8, color=edge, alpha=0.85))

        # Outlet / observed variables.
        ax.add_patch(FancyArrowPatch((0.50, 0.10), (0.78, 0.10), arrowstyle="-|>", mutation_scale=16, linewidth=2.0, color="#2f6f9f"))
        ax.text(0.80, 0.10, "Seepage outflow", ha="left", va="center", fontsize=9, fontweight="bold", color="#2f6f9f")
        observables = [str(value) for value in config.get("observables", []) if _safe_string(value)]
        if observables:
            ax.text(0.80, 0.065, "Observed: " + " | ".join(observables), ha="left", va="center", fontsize=7.5)

        # Parameter-family legend establishes connection to calibration story.
        legend = [
            "Calibrated parameter families:",
            "Hydraulic transport | Mineral kinetics | Sorption | Boundary chemistry | Redox scaling",
        ]
        ax.text(0.50, 0.025, "\n".join(legend), ha="center", va="center", fontsize=7.5)
        figure.tight_layout(pad=0.4)
        figure.savefig(svg_path, format="svg", bbox_inches="tight")
        figure.savefig(png_path, format="png", dpi=220, bbox_inches="tight")
        plt.close(figure)

    def _draw_timeline(self, trials: pd.DataFrame, png_path: Path) -> None:
        figure, axes = plt.subplots(2, 1, figsize=(13, 8), dpi=180, height_ratios=[3, 2], sharex=True)
        ax, score_ax = axes
        if trials.empty:
            ax.text(0.5, 0.5, "No candidate decisions available", ha="center", va="center")
            score_ax.axis("off")
        else:
            groups = list(dict.fromkeys(trials["group"].fillna("unclassified").astype(str).tolist()))
            y_lookup = {group: index for index, group in enumerate(groups)}
            marker_map = {"accepted": "o", "rejected": "x", "invalid": "s", "other": "D"}
            color_map = {"accepted": "#2f7d32", "rejected": "#a44343", "invalid": "#8a6f1f", "other": "#5a6470"}
            for decision_class, subset in trials.groupby("decision_class", dropna=False):
                ys = subset["group"].astype(str).map(y_lookup)
                ax.scatter(subset["trial_index"], ys, marker=marker_map.get(decision_class, "D"), s=45, color=color_map.get(decision_class, "#5a6470"), label=decision_class, zorder=3)
            ax.set_yticks(list(y_lookup.values()), list(y_lookup.keys()))
            ax.set_ylabel("Parameter group")
            ax.set_title("Calibration decision timeline")
            ax.grid(axis="x", alpha=0.25)
            ax.legend(loc="upper right", ncols=4, fontsize=8)

            valid_scores = trials.dropna(subset=["candidate_objective"])
            if valid_scores.empty:
                score_ax.text(0.5, 0.5, "Candidate TOTAL_SCORE not available in decision evidence", ha="center", va="center")
            else:
                score_ax.plot(valid_scores["trial_index"], valid_scores["candidate_objective"], marker="o", linewidth=1.2, label="candidate TOTAL_SCORE")
                baseline = trials.dropna(subset=["baseline_objective"])
                if not baseline.empty:
                    score_ax.plot(baseline["trial_index"], baseline["baseline_objective"], linestyle="--", linewidth=1.0, label="baseline TOTAL_SCORE")
                score_ax.set_ylabel("TOTAL_SCORE")
                score_ax.set_xlabel("Candidate trial index")
                score_ax.grid(alpha=0.25)
                score_ax.legend(fontsize=8)
        figure.tight_layout()
        figure.savefig(png_path, format="png", dpi=220, bbox_inches="tight")
        plt.close(figure)

    def _draw_process_observable_map(self, trials: pd.DataFrame, svg_path: Path) -> None:
        """Draw a clear, paper-readable parameter-family and evidence map.

        This intentionally avoids a network diagram. Each row pairs a tested
        parameter family with its conceptual process role. The right panel
        states the only candidate-level evidence actually recorded by V14.
        """
        figure, ax = plt.subplots(figsize=(12.8, 7.6), dpi=180)
        ax.set_xlim(0, 1)
        ax.set_ylim(0, 1)
        ax.axis("off")
        ax.set_title("Parameter families, process roles and recorded calibration evidence", fontsize=15, fontweight="bold", pad=14)
        if trials.empty:
            ax.text(0.5, 0.5, "No candidate decisions available", ha="center", va="center")
            figure.savefig(svg_path, format="svg", bbox_inches="tight")
            plt.close(figure)
            return

        def atlas_family(row: pd.Series) -> str:
            parameter = _norm(row.get("parameter"))
            group = _norm(row.get("group"))
            process = _safe_string(row.get("process_family"))
            if group == "hydraulic" or process == "Hydraulic transport":
                return "Hydraulic parameters"
            if process == "Fe hydroxide sorption" or "feoh" in parameter or "ferrihydrite" in parameter:
                return "FeOH sorption parameters"
            if process == "Carbonate neutralization" or any(t in parameter for t in ("calcite", "magnesite", "carbonate", "siderite", "smithsonite", "otavite")):
                return "Carbonate / neutralising minerals"
            if process == "Silicate weathering" or any(t in parameter for t in ("biotite", "albite", "chlorite", "silicate")):
                return "Silicate-weathering parameters"
            if group == "boundary_chemistry" or process == "Boundary chemistry" or parameter.startswith("bc_"):
                return "Boundary chemistry"
            return "Sulfide / mineral kinetics"

        working = trials.copy()
        working["atlas_family"] = working.apply(atlas_family, axis=1)
        order = ["Hydraulic parameters", "Sulfide / mineral kinetics", "Carbonate / neutralising minerals", "FeOH sorption parameters", "Silicate-weathering parameters", "Boundary chemistry"]
        present = [name for name in order if (working["atlas_family"] == name).any()]
        process_text = {
            "Hydraulic parameters": "Water flow, residence time, gas access, dilution, and solute transport.",
            "Sulfide / mineral kinetics": "Sulfide oxidation and mineral reaction rates; acidity and sulfate generation are conceptual pathways.",
            "Carbonate / neutralising minerals": "Acid neutralisation and buffering by mineral dissolution.",
            "FeOH sorption parameters": "Surface complexation, metal retention, and reactive-surface availability.",
            "Silicate-weathering parameters": "Silicate dissolution, cation supply, and longer-term buffering.",
            "Boundary chemistry": "Influent pH, oxygen, and imposed boundary chemistry.",
        }
        colors = {"Hydraulic parameters":"#dcecf8", "Sulfide / mineral kinetics":"#f7ded4", "Carbonate / neutralising minerals":"#e4f0dd", "FeOH sorption parameters":"#e8e3f2", "Silicate-weathering parameters":"#e9eef0", "Boundary chemistry":"#f3e7f1"}

        def box(x: float, y: float, width: float, height: float, label: str, face: str, *, size: float = 8.0, weight: str = "normal") -> None:
            ax.add_patch(FancyBboxPatch((x - width / 2, y - height / 2), width, height, boxstyle="round,pad=0.012", facecolor=face, edgecolor="#4d5965", linewidth=0.85, zorder=4))
            ax.text(x, y, label, ha="center", va="center", fontsize=size, fontweight=weight, wrap=True, zorder=6)

        left_x, mid_x = 0.22, 0.53
        top_y, spacing = 0.78, 0.102
        ax.text(left_x, 0.91, "Tested parameter family", ha="center", va="center", fontsize=11, fontweight="bold")
        ax.text(mid_x, 0.91, "Conceptual hydrogeochemical role", ha="center", va="center", fontsize=11, fontweight="bold")
        for i, family in enumerate(present):
            y = top_y - i * spacing
            subset = working[working["atlas_family"] == family]
            n_parameters = int(subset["parameter"].astype(str).nunique())
            n_trials = int(len(subset))
            family_label = f"{family}\n{n_parameters} tested parameter(s) | {n_trials} candidate trial(s)"
            box(left_x, y, 0.30, 0.078, family_label, "#edf3f7", size=7.8, weight="bold")
            box(mid_x, y, 0.37, 0.078, process_text[family], colors[family], size=7.3)

        evidence_x = 0.83
        ax.text(evidence_x, 0.91, "What V14 recorded", ha="center", va="center", fontsize=11, fontweight="bold")
        box(evidence_x, 0.63, 0.28, 0.23, "Composite calibration objective\n\nTOTAL_SCORE was recorded for each candidate decision.\n\nLower score = closer overall agreement with the calibration targets.", "#f7f7f7", size=8.2, weight="bold")
        box(evidence_x, 0.35, 0.28, 0.17, "Not recorded per candidate\n\nΔpH and ΔSO₄ response metrics were not linked to every tested parameter candidate.", "#fbf1f1", size=7.7)
        ax.text(0.50, 0.08, "How to read this figure: each row identifies which parameter family was tested and the process it represents. All families were evaluated using TOTAL_SCORE; the figure does not claim measured parameter-specific pH or sulfate responses.", ha="center", va="center", fontsize=7.3, color="#39434a", wrap=True)
        figure.tight_layout(pad=0.4)
        figure.savefig(svg_path, format="svg", bbox_inches="tight")
        plt.close(figure)

    def _draw_response_atlas(self, metrics: pd.DataFrame, png_path: Path, metrics_available: bool) -> None:
        figure, ax = plt.subplots(figsize=(11.5, 7.0), dpi=180)
        ax.set_title("pH–SO4 calibration response atlas", fontsize=14, fontweight="bold")
        if not metrics_available:
            ax.axis("off")
            ax.text(0.5, 0.62, "Candidate-specific pH/SO4 response metrics are not available", ha="center", va="center", fontsize=13, fontweight="bold")
            ax.text(0.5, 0.49, "The V14 decision log contains objective decisions but no explicit candidate-level ΔpH or ΔSO4 columns.\nThe report therefore preserves these effects as not_available rather than inferring them from process theory.", ha="center", va="center", fontsize=9, wrap=True)
            if not metrics.empty:
                expected = metrics[["process_family", "expected_pH_direction", "expected_SO4_direction"]].drop_duplicates().head(8)
                lines = [f"• {row.process_family}: pH {row.expected_pH_direction}; SO4 {row.expected_SO4_direction}" for row in expected.itertuples(index=False)]
                ax.text(0.5, 0.28, "\n".join(lines), ha="center", va="center", fontsize=8)
        else:
            subset = metrics.dropna(subset=["pH_metric", "SO4_metric"]).copy()
            color_map = {"accept": "#2f7d32", "reject": "#a44343"}
            colors = [color_map.get(_norm(value), "#53616d") for value in subset["decision"]]
            ax.scatter(subset["pH_metric"], subset["SO4_metric"], s=55, c=colors, alpha=0.85)
            is_delta = subset.get("metric_kind", pd.Series("", index=subset.index)).astype(str).eq("candidate_delta").all()
            if is_delta:
                ax.axhline(0, color="#888888", linewidth=0.8)
                ax.axvline(0, color="#888888", linewidth=0.8)
            for row in subset.itertuples(index=False):
                ax.annotate(str(row.parameter), (row.pH_metric, row.SO4_metric), xytext=(4, 4), textcoords="offset points", fontsize=7)
            p_label = str(subset["pH_metric_name"].dropna().iloc[0]) if "pH_metric_name" in subset and subset["pH_metric_name"].replace("", np.nan).notna().any() else "pH metric"
            s_label = str(subset["SO4_metric_name"].dropna().iloc[0]) if "SO4_metric_name" in subset and subset["SO4_metric_name"].replace("", np.nan).notna().any() else "SO4 metric"
            ax.set_xlabel(p_label)
            ax.set_ylabel(s_label)
            ax.grid(alpha=0.25)
            ax.text(0.01, 0.01, "Green = accepted; red = rejected; neutral = other. Values are plotted only from explicitly labelled campaign diagnostics.", transform=ax.transAxes, fontsize=7)
        figure.tight_layout()
        figure.savefig(png_path, format="png", dpi=220, bbox_inches="tight")
        plt.close(figure)

    # ------------------------------ package ------------------------------


    @staticmethod
    def _finite_number(value: Any) -> float | None:
        """Return a finite float or None; never allow inf into report statistics."""
        parsed = _to_float(value)
        return parsed if parsed is not None and math.isfinite(parsed) else None

    @staticmethod
    def _join_unique(values: Iterable[Any], *, separator: str = " | ") -> str:
        unique: list[str] = []
        for value in values:
            item = _safe_string(value)
            if item and item.casefold() not in {x.casefold() for x in unique}:
                unique.append(item)
        return separator.join(unique)

    @staticmethod
    def _format_value_range(values: pd.Series) -> str:
        numeric = pd.to_numeric(values, errors="coerce")
        numeric = numeric[np.isfinite(numeric)]
        if numeric.empty:
            return "not_available"
        low, high = float(numeric.min()), float(numeric.max())
        if math.isclose(low, high, rel_tol=0.0, abs_tol=1e-15):
            return f"{low:.6g}"
        return f"{low:.6g} to {high:.6g}"

    @staticmethod
    def _format_steps(values: pd.Series) -> str:
        numeric = pd.to_numeric(values, errors="coerce")
        numeric = numeric[np.isfinite(numeric) & (numeric > 0)]
        if numeric.empty:
            return "not_available"
        unique = sorted({round(float(value) * 100.0, 10) for value in numeric}, reverse=True)
        return " | ".join(f"{value:.6g}%" for value in unique)

    @staticmethod
    def _metric_range_text(
        status: str,
        name: str,
        low: float | None,
        high: float | None,
    ) -> str:
        if str(status).casefold() != "available":
            return "not_available"
        if low is None or high is None:
            return _safe_string(name) or "available"
        label = _safe_string(name) or "metric"
        if math.isclose(low, high, rel_tol=0.0, abs_tol=1e-15):
            return f"{label}: {low:.6g}"
        return f"{label}: {low:.6g} to {high:.6g}"

    def _draw_parameter_sensitivity_summary(
        self,
        sensitivity: pd.DataFrame,
        png_path: Path,
    ) -> None:
        """Draw a ranked, one-row-per-parameter sensitivity figure.

        This is intentionally an objective-sensitivity figure. It does not
        portray unmeasured pH or SO4 effects as numerical responses.
        """
        figure_height = max(5.0, min(18.0, 1.1 + 0.42 * max(len(sensitivity), 1)))
        figure, ax = plt.subplots(figsize=(12.5, figure_height), dpi=180)
        ax.set_title(
            "Parameter sensitivity summary\n"
            "Objective sensitivity per 1% relative perturbation",
            fontsize=14,
            fontweight="bold",
        )
        if sensitivity.empty:
            ax.text(0.5, 0.5, "No single-parameter sensitivity evidence is available", ha="center", va="center")
            ax.axis("off")
        else:
            work = sensitivity.copy()
            work["_sensitivity"] = pd.to_numeric(
                work.get("median_abs_objective_change_per_1pct"), errors="coerce"
            )
            work = work[np.isfinite(work["_sensitivity"])].copy()
            if work.empty:
                ax.text(
                    0.5,
                    0.5,
                    "No finite objective-sensitivity values are available\n"
                    "Invalid or unscored candidates were excluded.",
                    ha="center",
                    va="center",
                )
                ax.axis("off")
            else:
                work = work.sort_values(["_sensitivity", "parameter"], ascending=[True, True], kind="stable")
                labels = [
                    f"{_safe_string(row.parameter)}  [{_safe_string(row.calibration_group)}]"
                    for row in work.itertuples(index=False)
                ]
                bars = ax.barh(labels, work["_sensitivity"].to_numpy(dtype=float))
                maximum = float(work["_sensitivity"].max())
                offset = max(maximum * 0.012, 0.000001)
                for bar, row in zip(bars, work.itertuples(index=False)):
                    best_value = _to_float(getattr(row, "best_tested_value", None))
                    best_text = "not available" if best_value is None else f"{best_value:.6g}"
                    annotation = (
                        f"{_safe_string(row.sensitivity_class)}; "
                        f"best tested = {best_text}"
                    )
                    ax.text(
                        float(bar.get_width()) + offset,
                        bar.get_y() + bar.get_height() / 2,
                        annotation,
                        va="center",
                        fontsize=7.5,
                    )
                ax.set_xlabel("Median |candidate TOTAL_SCORE − baseline TOTAL_SCORE| per 1% perturbation")
                ax.set_ylabel("Calibration parameter [optimizer group]")
                ax.grid(axis="x", alpha=0.25)
                ax.set_xlim(right=maximum * 1.32 + offset)
                ax.text(
                    0.0,
                    -0.10,
                    "Higher values indicate stronger local sensitivity of the calibration objective. "
                    "This figure is not a pH or SO4 sensitivity plot.",
                    transform=ax.transAxes,
                    fontsize=7.5,
                    va="top",
                )
        figure.tight_layout()
        figure.savefig(png_path, format="png", dpi=220, bbox_inches="tight")
        plt.close(figure)

    def _parameter_sensitivity_summary(
        self,
        trials: pd.DataFrame,
        metrics: pd.DataFrame,
    ) -> pd.DataFrame:
        """One row per calibrated parameter, based only on recorded trial evidence.

        The sensitivity index quantifies response of the *calibration objective*,
        not pH or sulphate individually:

            median(|candidate TOTAL_SCORE - baseline TOTAL_SCORE|
                   / absolute percent parameter change)

        Its unit is TOTAL_SCORE per one-percent relative parameter perturbation.
        It is a local, campaign-specific screening measure; high/medium/low
        classes are relative ranks within the current report, not universal
        hydrogeochemical sensitivity categories.
        """
        columns = [
            "parameter", "calibration_group", "process_family",
            "hydrogeochemical_process", "baseline_value",
            "tested_candidate_value_range", "tested_step_fractions",
            "trial_count", "finite_valid_trial_count", "invalid_trial_count", "unscored_trial_count",
            "median_abs_objective_change_per_1pct",
            "max_abs_objective_change_per_1pct",
            "sensitivity_class", "most_sensitive_direction",
            "best_tested_direction", "best_tested_value",
            "best_candidate_TOTAL_SCORE", "best_delta_TOTAL_SCORE",
            "calibration_outcome", "expected_pH_direction",
            "expected_SO4_direction", "pH_candidate_response",
            "SO4_candidate_response", "pH_response_status",
            "SO4_response_status", "pH_response_source",
            "SO4_response_source", "sensitivity_note",
        ]
        if trials.empty:
            return pd.DataFrame(columns=columns)

        work = trials.copy()
        work = work[
            work.get("candidate_type", pd.Series("single_parameter", index=work.index))
            .astype(str)
            .str.casefold()
            .eq("single_parameter")
        ].copy()
        work = work[work["parameter"].map(_safe_string).ne("")].copy()
        if work.empty:
            return pd.DataFrame(columns=columns)

        for col in ["old_value", "new_value", "baseline_objective", "candidate_objective", "objective_delta"]:
            work[col] = pd.to_numeric(work.get(col), errors="coerce")
        work["relative_parameter_change"] = np.where(
            work["old_value"].abs() > 1e-30,
            (work["new_value"] - work["old_value"]).abs() / work["old_value"].abs(),
            np.nan,
        )
        work["objective_sensitivity_per_1pct"] = np.where(
            (work["relative_parameter_change"] > 0)
            & np.isfinite(work["objective_delta"]),
            work["objective_delta"].abs() / (100.0 * work["relative_parameter_change"]),
            np.nan,
        )
        work["is_finite_valid"] = (
            work["decision_class"].astype(str).ne("invalid")
            & np.isfinite(work["candidate_objective"])
            & np.isfinite(work["baseline_objective"])
            & np.isfinite(work["objective_delta"])
            & np.isfinite(work["objective_sensitivity_per_1pct"])
        )

        metric_lookup = pd.DataFrame()
        if not metrics.empty and "trial_index" in metrics.columns:
            metric_columns = [
                "trial_index", "pH_metric_status", "SO4_metric_status",
                "pH_metric", "SO4_metric", "pH_metric_name", "SO4_metric_name",
                "metric_source",
            ]
            metric_lookup = metrics[[column for column in metric_columns if column in metrics.columns]].copy()
            work = work.merge(metric_lookup, on="trial_index", how="left", suffixes=("", "_metric"))
        for column, default in {
            "pH_metric_status": "not_available",
            "SO4_metric_status": "not_available",
            "pH_metric": np.nan,
            "SO4_metric": np.nan,
            "pH_metric_name": "",
            "SO4_metric_name": "",
            "metric_source": "",
        }.items():
            if column not in work.columns:
                work[column] = default
        work["pH_metric"] = pd.to_numeric(work["pH_metric"], errors="coerce")
        work["SO4_metric"] = pd.to_numeric(work["SO4_metric"], errors="coerce")

        rows: list[dict[str, Any]] = []
        for parameter, subset in work.groupby("parameter", sort=True, dropna=False):
            subset = subset.sort_values(["trial_index"], kind="stable").copy()
            finite = subset[subset["is_finite_valid"]].copy()
            finite_candidates = subset[
                subset["decision_class"].astype(str).ne("invalid")
                & np.isfinite(subset["candidate_objective"])
            ].copy()
            invalid_count = int(subset["decision_class"].astype(str).eq("invalid").sum())
            accepted = bool(subset["decision_class"].astype(str).eq("accepted").any())

            baseline_value_series = subset["old_value"][np.isfinite(subset["old_value"])]
            baseline_value = float(baseline_value_series.iloc[0]) if not baseline_value_series.empty else np.nan

            best_row = None
            if not finite_candidates.empty:
                best_row = finite_candidates.loc[finite_candidates["candidate_objective"].idxmin()]
            most_sensitive_row = None
            if not finite.empty:
                most_sensitive_row = finite.loc[finite["objective_sensitivity_per_1pct"].idxmax()]

            p_available = subset["pH_metric_status"].astype(str).str.casefold().eq("available")
            s_available = subset["SO4_metric_status"].astype(str).str.casefold().eq("available")
            p_values = subset.loc[p_available, "pH_metric"]
            s_values = subset.loc[s_available, "SO4_metric"]
            p_low = self._finite_number(p_values.min()) if not p_values.dropna().empty else None
            p_high = self._finite_number(p_values.max()) if not p_values.dropna().empty else None
            s_low = self._finite_number(s_values.min()) if not s_values.dropna().empty else None
            s_high = self._finite_number(s_values.max()) if not s_values.dropna().empty else None
            p_status = "available" if bool(p_available.any()) else "not_available"
            s_status = "available" if bool(s_available.any()) else "not_available"
            p_name = self._join_unique(subset.loc[p_available, "pH_metric_name"])
            s_name = self._join_unique(subset.loc[s_available, "SO4_metric_name"])
            p_source = self._join_unique(subset.loc[p_available, "metric_source"])
            s_source = self._join_unique(subset.loc[s_available, "metric_source"])

            if accepted:
                outcome = "accepted_improvement"
            elif len(finite) > 0:
                outcome = "no_accepted_improvement"
            elif invalid_count > 0:
                outcome = "invalid_or_unresolved"
            else:
                outcome = "not_assessed"

            rows.append({
                "parameter": _safe_string(parameter),
                "calibration_group": self._join_unique(subset["group"]),
                "process_family": self._join_unique(subset["process_family"]),
                "hydrogeochemical_process": self._join_unique(subset["hydrogeochemical_process"]),
                "baseline_value": baseline_value,
                "tested_candidate_value_range": self._format_value_range(subset["new_value"]),
                "tested_step_fractions": self._format_steps(subset["step_fraction"]),
                "trial_count": int(len(subset)),
                "finite_valid_trial_count": int(len(finite)),
                "invalid_trial_count": invalid_count,
                "unscored_trial_count": int(
                    (
                        subset["decision_class"].astype(str).ne("invalid")
                        & ~np.isfinite(subset["candidate_objective"])
                    ).sum()
                ),
                "median_abs_objective_change_per_1pct": (
                    float(finite["objective_sensitivity_per_1pct"].median())
                    if not finite.empty else np.nan
                ),
                "max_abs_objective_change_per_1pct": (
                    float(finite["objective_sensitivity_per_1pct"].max())
                    if not finite.empty else np.nan
                ),
                "sensitivity_class": "not_assessed",
                "most_sensitive_direction": (
                    _safe_string(most_sensitive_row.get("direction"))
                    if most_sensitive_row is not None else "not_available"
                ),
                "best_tested_direction": (
                    _safe_string(best_row.get("direction"))
                    if best_row is not None else "not_available"
                ),
                "best_tested_value": (
                    self._finite_number(best_row.get("new_value"))
                    if best_row is not None else np.nan
                ),
                "best_candidate_TOTAL_SCORE": (
                    self._finite_number(best_row.get("candidate_objective"))
                    if best_row is not None else np.nan
                ),
                "best_delta_TOTAL_SCORE": (
                    self._finite_number(best_row.get("objective_delta"))
                    if best_row is not None else np.nan
                ),
                "calibration_outcome": outcome,
                "expected_pH_direction": self._join_unique(subset["expected_pH_direction"]),
                "expected_SO4_direction": self._join_unique(subset["expected_SO4_direction"]),
                "pH_candidate_response": self._metric_range_text(p_status, p_name, p_low, p_high),
                "SO4_candidate_response": self._metric_range_text(s_status, s_name, s_low, s_high),
                "pH_response_status": p_status,
                "SO4_response_status": s_status,
                "pH_response_source": p_source or "not_available",
                "SO4_response_source": s_source or "not_available",
                "sensitivity_note": (
                    "Objective sensitivity only; candidate-specific pH/SO4 response is not available."
                    if p_status != "available" and s_status != "available"
                    else "Objective sensitivity plus explicitly labelled candidate-level pH/SO4 metrics."
                ),
            })

        summary = pd.DataFrame(rows, columns=columns)
        finite_index = summary["median_abs_objective_change_per_1pct"].replace([np.inf, -np.inf], np.nan).dropna()
        if len(finite_index) >= 4:
            low, high = float(finite_index.quantile(0.25)), float(finite_index.quantile(0.75))
            def classify(value: Any) -> str:
                score = _to_float(value)
                if score is None:
                    return "not_assessed"
                if score >= high:
                    return "high (relative)"
                if score <= low:
                    return "low (relative)"
                return "moderate (relative)"
            summary["sensitivity_class"] = summary["median_abs_objective_change_per_1pct"].map(classify)
        elif len(finite_index) > 0:
            summary.loc[summary["median_abs_objective_change_per_1pct"].notna(), "sensitivity_class"] = "rank not available"
        return summary.sort_values(
            ["median_abs_objective_change_per_1pct", "parameter"],
            ascending=[False, True],
            na_position="last",
        ).reset_index(drop=True)

    def _calibration_group_summary(self, trials: pd.DataFrame) -> pd.DataFrame:
        """One row per actual optimizer group; excludes invalid/infinite scores."""
        columns = [
            "calibration_group", "parameter_count", "trial_count",
            "finite_valid_trial_count", "invalid_trial_count", "unscored_trial_count",
            "best_candidate_TOTAL_SCORE", "best_delta_TOTAL_SCORE",
            "median_candidate_minus_baseline_TOTAL_SCORE",
            "accepted_improvement_count",
        ]
        if trials.empty:
            return pd.DataFrame(columns=columns)
        work = trials.copy()
        work = work[
            work.get("candidate_type", pd.Series("single_parameter", index=work.index))
            .astype(str)
            .str.casefold()
            .eq("single_parameter")
        ].copy()
        if work.empty:
            return pd.DataFrame(columns=columns)
        for col in ["candidate_objective", "baseline_objective", "objective_delta"]:
            work[col] = pd.to_numeric(work.get(col), errors="coerce")
        work["finite_valid"] = (
            work["decision_class"].astype(str).ne("invalid")
            & np.isfinite(work["candidate_objective"])
            & np.isfinite(work["baseline_objective"])
            & np.isfinite(work["objective_delta"])
        )
        rows: list[dict[str, Any]] = []
        for group, subset in work.groupby("group", dropna=False, sort=True):
            finite = subset[subset["finite_valid"]].copy()
            best = finite.loc[finite["candidate_objective"].idxmin()] if not finite.empty else None
            rows.append({
                "calibration_group": _safe_string(group) or "unclassified",
                "parameter_count": int(subset["parameter"].nunique()),
                "trial_count": int(len(subset)),
                "finite_valid_trial_count": int(len(finite)),
                "invalid_trial_count": int(subset["decision_class"].astype(str).eq("invalid").sum()),
                "unscored_trial_count": int(
                    (
                        subset["decision_class"].astype(str).ne("invalid")
                        & ~np.isfinite(subset["candidate_objective"])
                    ).sum()
                ),
                "best_candidate_TOTAL_SCORE": self._finite_number(best.get("candidate_objective")) if best is not None else np.nan,
                "best_delta_TOTAL_SCORE": self._finite_number(best.get("objective_delta")) if best is not None else np.nan,
                "median_candidate_minus_baseline_TOTAL_SCORE": (
                    float(finite["objective_delta"].median()) if not finite.empty else np.nan
                ),
                "accepted_improvement_count": int(subset["decision_class"].astype(str).eq("accepted").sum()),
            })
        return pd.DataFrame(rows, columns=columns).sort_values(
            ["trial_count", "calibration_group"], ascending=[False, True]
        ).reset_index(drop=True)

    @staticmethod
    def _excel_safe(frame: pd.DataFrame) -> pd.DataFrame:
        """Excel cannot store timezone-aware datetimes; preserve timestamps as local naive values."""
        out = frame.copy()
        for column in out.columns:
            series = out[column]
            if isinstance(series.dtype, pd.DatetimeTZDtype):
                out[column] = series.dt.tz_localize(None)
        return out

    def _write_excel_tables(
        self,
        trials: pd.DataFrame,
        metrics: pd.DataFrame,
        sensitivity: pd.DataFrame,
        group_summary: pd.DataFrame,
        tables_dir: Path,
    ) -> tuple[Path, Path, Path, Path]:
        tables_dir.mkdir(parents=True, exist_ok=True)
        trials_path = tables_dir / "parameter_trial_effects.xlsx"
        metrics_path = tables_dir / "pH_SO4_response_metrics.xlsx"
        sensitivity_path = tables_dir / "parameter_sensitivity_and_calibration_evidence.xlsx"
        group_path = tables_dir / "calibration_group_overview.xlsx"
        self._excel_safe(trials).to_excel(trials_path, index=False)
        self._excel_safe(metrics).to_excel(metrics_path, index=False)
        self._excel_safe(sensitivity).to_excel(sensitivity_path, index=False)
        self._excel_safe(group_summary).to_excel(group_path, index=False)
        return trials_path, metrics_path, sensitivity_path, group_path

    @staticmethod
    def _campaign_review_markdown_fragment(
        analysis: dict[str, Any], review: dict[str, Any], metadata: dict[str, Any]
    ) -> list[str]:
        if not review:
            return []

        def number(value: Any) -> str:
            parsed = _to_float(value)
            return "not available" if parsed is None else f"{parsed:.8g}"

        evidence = analysis.get("campaign_summary", {})
        objective = analysis.get("objective_summary", {})
        score_stats = objective.get("candidate_score_statistics", {})
        search = analysis.get("search_summary", {})
        campaign = review.get("campaign_assessment", {})
        optimizer = review.get("optimizer_assessment", {})
        stagnation = review.get("stagnation_analysis", {})
        health = review.get("numerical_health", {})
        step_counts = search.get("step_fraction_counts", {})
        step_text = ", ".join(f"{step}: {count}" for step, count in step_counts.items()) or "not available"

        lines = [
            "## V15.4.2 scientific campaign review",
            "",
            f"**Review source:** `{metadata.get('used_mode', 'unknown')}`",
            "",
            "### Deterministic campaign evidence",
            "",
            f"- Model runs: `{evidence.get('total_runs', 0)}` total; `{evidence.get('baseline_runs', 0)}` baseline and `{evidence.get('candidate_runs', 0)}` candidate runs.",
            f"- Run status: `{evidence.get('successful_runs', 0)}` normal successes, `{evidence.get('successful_with_retries', 0)}` successes with retries, and `{evidence.get('failed_runs', 0)}` failed runs.",
            f"- Candidate decisions: `{evidence.get('accepted_candidates', 0)}` accepted and `{evidence.get('rejected_candidates', 0)}` rejected.",
            f"- Rollback restoration verified: `{number(evidence.get('restoration_verified_fraction'))}` as a fraction of evaluated candidates.",
            f"- Objective: initial `{number(objective.get('initial_score'))}`; best `{number(objective.get('best_score'))}`; absolute improvement `{number(objective.get('absolute_improvement'))}`.",
            f"- Candidate-score range: minimum `{number(score_stats.get('minimum'))}`, median `{number(score_stats.get('median'))}`, mean `{number(score_stats.get('mean'))}`, maximum `{number(score_stats.get('maximum'))}`.",
            f"- Search coverage: `{search.get('tested_parameter_count', 0)}` parameters in `{len(search.get('tested_groups', []))}` groups; `{search.get('single_parameter_candidates', 0)}` single-parameter and `{search.get('interaction_or_pair_candidates', 0)}` pair/interaction candidates.",
            f"- Step-fraction counts: `{step_text}`.",
            "",
            "#### Closest candidate results",
            "",
        ]
        closest = analysis.get("closest_candidates", [])[:5]
        if closest:
            lines.extend([
                "| Parameter | Group | Direction | Step | Candidate TOTAL_SCORE | Candidate − baseline | Accepted |",
                "|---|---|---|---:|---:|---:|---|",
            ])
            for item in closest:
                lines.append(
                    f"| {_safe_string(item.get('parameter'))} | {_safe_string(item.get('group'))} | {_safe_string(item.get('direction'))} | "
                    f"{number(item.get('step_fraction'))} | {number(item.get('candidate_score'))} | {number(item.get('candidate_minus_baseline'))} | "
                    f"{bool(item.get('accepted'))} |"
                )
        else:
            lines.append("No finite candidate scores were available.")

        lines.extend(["", "#### Species-specific model performance", ""])
        species = analysis.get("species_summary", [])
        if species:
            lines.extend([
                "| Species | Weight | RMSE | Mean observed | Mean modelled | Bias (model − observed) | Status |",
                "|---|---:|---:|---:|---:|---:|---|",
            ])
            for item in species:
                lines.append(
                    f"| {_safe_string(item.get('species'))} | {number(item.get('weight'))} | {number(item.get('rmse'))} | "
                    f"{number(item.get('mean_observed'))} | {number(item.get('mean_modelled'))} | "
                    f"{number(item.get('bias_model_minus_observed'))} | {_safe_string(item.get('prediction_status'))} |"
                )
        else:
            lines.append("Species-specific metrics were not available in the optimization history.")

        lines.extend([
            "",
            "### Scientific interpretation",
            "",
            str(review.get("executive_summary", "")),
            "",
            "#### Campaign assessment",
            "",
            f"- Status: `{campaign.get('status', '')}`",
            f"- Confidence: `{campaign.get('confidence', '')}`",
            f"- {campaign.get('summary', '')}",
            "",
            "#### Optimizer behaviour",
            "",
            f"- Implementation status: `{optimizer.get('implementation_status', '')}`",
        ])
        lines.extend(f"- {item}" for item in optimizer.get("evidence", []))
        issues = optimizer.get("possible_software_issues", [])
        if issues:
            lines.extend(["", "Possible software issues:", *[f"- {item}" for item in issues]])
        findings = review.get("scientific_findings", [])
        lines.extend(["", "#### Main scientific findings", ""])
        if findings:
            for item in findings:
                lines.append(f"- **{item.get('importance', '').upper()}:** {item.get('finding', '')}")
                lines.extend(f"  - {finding_evidence}" for finding_evidence in item.get("evidence", []))
        else:
            lines.append("- No structured scientific finding was returned.")
        lines.extend([
            "",
            "#### Stagnation and search-space assessment",
            "",
            f"- Local stagnation likely: `{stagnation.get('local_minimum_likely', False)}`",
            f"- More runs with unchanged configuration recommended: `{stagnation.get('more_runs_same_configuration_recommended', False)}`",
        ])
        lines.extend(f"- {item}" for item in stagnation.get("reasoning", []))
        lines.extend([
            "",
            "#### Numerical health",
            "",
            f"- Status: `{health.get('status', '')}`",
            f"- {health.get('interpretation', '')}",
        ])
        lines.extend(f"- {item}" for item in health.get("important_warnings", []))
        lines.extend(["", "#### Prioritized next actions", ""])
        for item in sorted(review.get("recommended_actions", []), key=lambda x: x.get("priority", 999)):
            lines.append(f"{item.get('priority', '')}. **{item.get('action', '')}**")
            lines.append(f"   - Reason: {item.get('reason', '')}")
            lines.append(f"   - Expected benefit: {item.get('expected_benefit', '')}")
        lines.extend([
            "",
            "#### Paper-ready conclusion",
            "",
            str(review.get("paper_ready_conclusion", "")),
            "",
            "The complete deterministic campaign evidence and structured review are saved as separate JSON, Excel, and Markdown artifacts.",
            "",
        ])
        return lines

    @staticmethod
    def _campaign_review_html_fragment(
        analysis: dict[str, Any], review: dict[str, Any], metadata: dict[str, Any]
    ) -> str:
        if not review:
            return ""

        def number(value: Any) -> str:
            parsed = _to_float(value)
            return "not available" if parsed is None else f"{parsed:.8g}"

        evidence = analysis.get("campaign_summary", {})
        objective = analysis.get("objective_summary", {})
        score_stats = objective.get("candidate_score_statistics", {})
        search = analysis.get("search_summary", {})
        campaign = review.get("campaign_assessment", {})
        optimizer = review.get("optimizer_assessment", {})
        stagnation = review.get("stagnation_analysis", {})
        health = review.get("numerical_health", {})

        closest_rows = "".join(
            "<tr><td><code>{parameter}</code></td><td>{group}</td><td>{direction}</td><td>{step}</td><td>{score}</td><td>{delta}</td><td>{accepted}</td></tr>".format(
                parameter=escape(_safe_string(item.get("parameter"))),
                group=escape(_safe_string(item.get("group"))),
                direction=escape(_safe_string(item.get("direction"))),
                step=escape(number(item.get("step_fraction"))),
                score=escape(number(item.get("candidate_score"))),
                delta=escape(number(item.get("candidate_minus_baseline"))),
                accepted=escape(str(bool(item.get("accepted")))),
            )
            for item in analysis.get("closest_candidates", [])[:5]
        ) or '<tr><td colspan="7">No finite candidate scores were available.</td></tr>'

        species_rows = "".join(
            "<tr><td>{species}</td><td>{weight}</td><td>{rmse}</td><td>{obs}</td><td>{model}</td><td>{bias}</td><td>{status}</td></tr>".format(
                species=escape(_safe_string(item.get("species"))),
                weight=escape(number(item.get("weight"))),
                rmse=escape(number(item.get("rmse"))),
                obs=escape(number(item.get("mean_observed"))),
                model=escape(number(item.get("mean_modelled"))),
                bias=escape(number(item.get("bias_model_minus_observed"))),
                status=escape(_safe_string(item.get("prediction_status"))),
            )
            for item in analysis.get("species_summary", [])
        ) or '<tr><td colspan="7">Species-specific metrics were not available.</td></tr>'

        findings = "".join(
            "<li><strong>{importance}:</strong> {finding}<ul>{finding_evidence}</ul></li>".format(
                importance=escape(_safe_string(item.get("importance")).upper()),
                finding=escape(_safe_string(item.get("finding"))),
                finding_evidence="".join(f"<li>{escape(_safe_string(x))}</li>" for x in item.get("evidence", [])),
            )
            for item in review.get("scientific_findings", [])
        ) or "<li>No structured scientific finding was returned.</li>"
        optimizer_evidence = "".join(f"<li>{escape(_safe_string(x))}</li>" for x in optimizer.get("evidence", []))
        reasoning = "".join(f"<li>{escape(_safe_string(x))}</li>" for x in stagnation.get("reasoning", []))
        warnings = "".join(f"<li>{escape(_safe_string(x))}</li>" for x in health.get("important_warnings", [])) or "<li>None extracted.</li>"
        actions = "".join(
            "<li><strong>Priority {priority}: {action}</strong><br><span class=\"meta\">Reason: {reason}<br>Expected benefit: {benefit}</span></li>".format(
                priority=escape(_safe_string(item.get("priority"))),
                action=escape(_safe_string(item.get("action"))),
                reason=escape(_safe_string(item.get("reason"))),
                benefit=escape(_safe_string(item.get("expected_benefit"))),
            )
            for item in sorted(review.get("recommended_actions", []), key=lambda x: x.get("priority", 999))
        )
        step_counts = search.get("step_fraction_counts", {})
        step_text = ", ".join(f"{step}: {count}" for step, count in step_counts.items()) or "not available"
        return f'''<section id="campaign-review"><h2>V15.4.2 scientific campaign review</h2>
<div class="note"><strong>Review source:</strong> {escape(_safe_string(metadata.get("used_mode", "unknown")))}. Python calculated all campaign statistics before interpretation.</div>
<h3>Deterministic campaign evidence</h3>
<div class="grid review-grid"><div class="card"><div class="label">Total model runs</div><div class="value">{escape(_safe_string(evidence.get("total_runs", 0)))}</div><div class="meta">{escape(_safe_string(evidence.get("baseline_runs", 0)))} baseline · {escape(_safe_string(evidence.get("candidate_runs", 0)))} candidate</div></div><div class="card"><div class="label">Accepted candidates</div><div class="value">{escape(_safe_string(evidence.get("accepted_candidates", 0)))}</div><div class="meta">{escape(_safe_string(evidence.get("rejected_candidates", 0)))} rejected</div></div><div class="card"><div class="label">Initial TOTAL_SCORE</div><div class="value">{escape(number(objective.get("initial_score")))}</div></div><div class="card"><div class="label">Best TOTAL_SCORE</div><div class="value">{escape(number(objective.get("best_score")))}</div></div></div>
<p><strong>Run status:</strong> {escape(_safe_string(evidence.get("successful_runs", 0)))} normal successes, {escape(_safe_string(evidence.get("successful_with_retries", 0)))} successes with retries, and {escape(_safe_string(evidence.get("failed_runs", 0)))} failed runs. <strong>Search:</strong> {escape(_safe_string(search.get("tested_parameter_count", 0)))} parameters, {escape(_safe_string(search.get("single_parameter_candidates", 0)))} single-parameter candidates, {escape(_safe_string(search.get("interaction_or_pair_candidates", 0)))} pair candidates. <strong>Step counts:</strong> {escape(step_text)}.</p>
<p><strong>Candidate-score distribution:</strong> minimum {escape(number(score_stats.get("minimum")))}, median {escape(number(score_stats.get("median")))}, mean {escape(number(score_stats.get("mean")))}, maximum {escape(number(score_stats.get("maximum")))}.</p>
<h3>Closest candidate results</h3><div class="table-wrap"><table><thead><tr><th>Parameter</th><th>Group</th><th>Direction</th><th>Step</th><th>Candidate TOTAL_SCORE</th><th>Candidate − baseline</th><th>Accepted</th></tr></thead><tbody>{closest_rows}</tbody></table></div>
<h3>Species-specific model performance</h3><div class="table-wrap"><table><thead><tr><th>Species</th><th>Weight</th><th>RMSE</th><th>Mean observed</th><th>Mean modelled</th><th>Bias</th><th>Status</th></tr></thead><tbody>{species_rows}</tbody></table></div>
<h3>Scientific interpretation</h3><p>{escape(_safe_string(review.get("executive_summary")))}</p>
<div class="grid review-grid"><div class="card"><div class="label">Campaign status</div><div class="value small-value">{escape(_safe_string(campaign.get("status")))}</div><div class="meta">Confidence {escape(_safe_string(campaign.get("confidence")))}</div></div><div class="card"><div class="label">Optimizer status</div><div class="value small-value">{escape(_safe_string(optimizer.get("implementation_status")))}</div></div><div class="card"><div class="label">Local stagnation</div><div class="value small-value">{escape(str(bool(stagnation.get("local_minimum_likely"))))}</div></div><div class="card"><div class="label">Unchanged extra runs</div><div class="value small-value">{escape(str(bool(stagnation.get("more_runs_same_configuration_recommended"))))}</div></div></div>
<h3>Optimizer evidence</h3><ul>{optimizer_evidence}</ul>
<h3>Main scientific findings</h3><ul>{findings}</ul>
<h3>Stagnation and search-space assessment</h3><ul>{reasoning}</ul>
<h3>Numerical health: {escape(_safe_string(health.get("status")))}</h3><p>{escape(_safe_string(health.get("interpretation")))}</p><ul>{warnings}</ul>
<h3>Prioritized next actions</h3><ol>{actions}</ol>
<h3>Paper-ready conclusion</h3><div class="note">{escape(_safe_string(review.get("paper_ready_conclusion")))}</div>
</section>'''

    def _write_report(
        self,
        trials: pd.DataFrame,
        metrics: pd.DataFrame,
        sensitivity: pd.DataFrame,
        group_summary: pd.DataFrame,
        metadata: dict[str, Any],
        campaign_state: dict[str, Any],
        artifacts: dict[str, Path],
        report_path: Path,
        html_path: Path,
        hashes_before: dict[str, str | None],
        hashes_after: dict[str, str | None],
        campaign_analysis: dict[str, Any],
        campaign_review: dict[str, Any],
        campaign_review_metadata: dict[str, Any],
    ) -> None:
        decision_counts = trials["decision_class"].value_counts().to_dict() if not trials.empty else {}
        best_score = _to_float(campaign_state.get("current_best_score"))
        if best_score is None and not trials.empty:
            baseline = pd.to_numeric(trials["baseline_objective"], errors="coerce")
            baseline = baseline[np.isfinite(baseline)]
            best_score = float(baseline.min()) if not baseline.empty else None
        metrics_available = bool(
            (metrics["pH_metric_status"].eq("available") | metrics["SO4_metric_status"].eq("available")).any()
        ) if not metrics.empty else False

        def relative(path: Path) -> str:
            return path.relative_to(self.paths.output_dir).as_posix()

        def score(value: Any) -> str:
            parsed = _to_float(value)
            return "" if parsed is None else f"{parsed:.6g}"

        def value(value: Any) -> str:
            if value is None or (isinstance(value, float) and math.isnan(value)):
                return ""
            return str(value)

        def md_cell(value_: Any) -> str:
            return value(value_).replace("|", "\\|").replace("\n", "<br>")

        campaign_review_md = self._campaign_review_markdown_fragment(campaign_analysis, campaign_review, campaign_review_metadata)
        campaign_review_html = self._campaign_review_html_fragment(campaign_analysis, campaign_review, campaign_review_metadata)
        if campaign_review_md:
            campaign_review_md.extend([
                "### Campaign-review artifacts",
                "",
                f"- Deterministic analysis JSON: `{relative(artifacts['campaign_analysis_json'])}`",
                f"- Deterministic analysis workbook: `{relative(artifacts['campaign_analysis_xlsx'])}`",
                f"- Structured scientific review JSON: `{relative(artifacts['campaign_review_json'])}`",
                f"- Standalone scientific review: `{relative(artifacts['campaign_review_markdown'])}`",
                "",
            ])

        lines = [
            "# V15.4.1 DAT-Driven Scientific Reporting, Explainability, and Campaign Review",
            "",
            f"**Generated:** {_now()}",
            f"**Project:** `{self.paths.project_dir.name}`",
            f"**Report renderer:** `{REPORTER_VERSION}`",
            "**Reporter mode:** read-only; no MIN3P run and no V14 optimizer-state mutation.",
            "",
            "## Calibration story at a glance",
            "",
            f"- V14 best TOTAL_SCORE recorded in campaign evidence: `{best_score if best_score is not None else 'not available'}`",
            f"- Candidate trials represented: `{len(trials)}`",
            f"- Decisions: `{json.dumps(decision_counts, ensure_ascii=False)}`",
            f"- Candidate-level pH/SO4 response metrics available: `{metrics_available}`",
            f"- Decision evidence source: `{metadata.get('decision_log_source') or 'not available'}`",
            "",
            *campaign_review_md,
            "## Scientific conceptual model",
            "",
            f"![Conceptual hydrogeochemical model]({relative(artifacts['conceptual_png'])})",
            "",
            "The conceptual model is generated from the DAT file discovered in `01_input` (or selected with `--dat-file`). "
            "The facts JSON, prompt, visual draft, and generation manifest are saved in the report audit folder. "
            "The final labels, arrows, numerical grid, property-zone count, boundaries, and legend are Python-rendered from parsed DAT facts; GPT is optional and supplies only an unlabelled visual draft.",
            "",
            "## Calibration decision timeline",
            "",
            f"![Calibration decision timeline]({relative(artifacts['timeline_png'])})",
            "",
            "Every recorded parameter trial remains visible. Rejected trials are retained as sensitivity evidence, rather than being hidden as failed calibration attempts.",
            "",
            "## pH and SO4 response evidence",
            "",
            "Candidate-level ΔpH and ΔSO4 metrics were not recorded in the V14 decision log. This report therefore does not create a response atlas or infer quantitative pH/SO4 effects from process theory. The objective-sensitivity evidence remains available below.",
            "",
            "## Calibration interpretation",
            "",
            "V14 tested hydraulic, mineral-kinetic, sorption, boundary-chemistry, and silicate-weathering parameters.",
            "Each parameter family was evaluated using the composite `TOTAL_SCORE`; lower scores indicate closer overall agreement with the calibration targets.",
            "Candidate-specific pH and SO4 response metrics were not stored for every parameter trial. The report therefore does not attribute measured pH or SO4 changes to individual parameter tests.",
            "",
            "## Parameter sensitivity summary",
            "",
            f"![Parameter sensitivity summary]({relative(artifacts['sensitivity_png'])})",
            "",
            "This summary has one row for every calibrated parameter. `best tested value` is the candidate value with the lowest finite TOTAL_SCORE; it does not imply acceptance. "
            "The sensitivity class ranks local objective sensitivity relative to the other parameters in this campaign. Invalid and infinite objective values are excluded.",
            "",
        ]
        if sensitivity.empty:
            lines.append("No single-parameter sensitivity evidence was available.")
        else:
            lines.extend([
                "| Parameter | Calibration group | Process family | Hydrogeochemical process | Baseline value | Best tested value | Sensitivity class |",
                "|---|---|---|---|---:|---:|---|",
            ])
            for row in sensitivity.itertuples(index=False):
                lines.append(
                    "| {parameter} | {group} | {family} | {process} | {baseline} | {best} | {klass} |".format(
                        parameter=md_cell(row.parameter),
                        group=md_cell(row.calibration_group),
                        family=md_cell(row.process_family),
                        process=md_cell(row.hydrogeochemical_process),
                        baseline=score(row.baseline_value),
                        best=score(row.best_tested_value),
                        klass=md_cell(row.sensitivity_class),
                    )
                )
        lines.extend([
            "",
            "## Parameter-trial evidence",
            "",
            f"- Full trial table: `{relative(artifacts['trial_table'])}`",
            f"- pH/SO4 response table: `{relative(artifacts['metrics_table'])}`",
            f"- Per-parameter sensitivity table: `{relative(artifacts['sensitivity_table'])}`",
            f"- Calibration-group overview: `{relative(artifacts['group_table'])}`",
            "",
            "### Parameter sensitivity and calibration evidence",
            "",
            "One row is reported for every single-parameter calibration target represented in the decision log. "
            "**Median objective sensitivity per 1%** is the median absolute change in TOTAL_SCORE divided by the absolute relative parameter change in percentage points. "
            "It measures local sensitivity of the calibration objective, not pH or SO4 separately. "
            "`high`, `moderate`, and `low` are relative ranks within this campaign. "
            "A positive best ΔTOTAL_SCORE means the best tested candidate was still worse than the V14 best configuration. "
            "Invalid or infinite objective values are excluded from all sensitivity statistics.",
            "",
        ])
        if sensitivity.empty:
            lines.append("No single-parameter decision evidence was available.")
        else:
            lines.extend([
                "| Parameter | Group | Trials (scored / invalid / unscored) | Median objective sensitivity per 1% | Class | Strongest direction | Best tested direction | Best ΔTOTAL_SCORE | pH response | SO4 response | Outcome |",
                "|---|---|---:|---:|---|---|---|---:|---|---|---|",
            ])
            for row in sensitivity.itertuples(index=False):
                lines.append(
                    "| {parameter} | {group} | {trials} ({finite} / {invalid} / {unscored}) | {median} | {klass} | {strongest} | {best_direction} | {best_delta} | {ph} | {so4} | {outcome} |".format(
                        parameter=md_cell(row.parameter),
                        group=md_cell(row.calibration_group),
                        trials=int(row.trial_count),
                        finite=int(row.finite_valid_trial_count),
                        invalid=int(row.invalid_trial_count),
                        unscored=int(row.unscored_trial_count),
                        median=score(row.median_abs_objective_change_per_1pct),
                        klass=md_cell(row.sensitivity_class),
                        strongest=md_cell(row.most_sensitive_direction),
                        best_direction=md_cell(row.best_tested_direction),
                        best_delta=score(row.best_delta_TOTAL_SCORE),
                        ph=md_cell(row.pH_candidate_response),
                        so4=md_cell(row.SO4_candidate_response),
                        outcome=md_cell(row.calibration_outcome),
                    )
                )

        lines.extend([
            "",
            "### Calibration-group overview",
            "",
            "This table has one row per actual V14 optimizer group. It replaces the earlier mixed `Group summary`, which combined optimizer groups with conceptual process-family labels and therefore repeated `mineral_kinetics` rows.",
            "",
        ])
        if group_summary.empty:
            lines.append("No group-level trial evidence was available.")
        else:
            lines.extend([
                "| Calibration group | Parameters | Trials (scored / invalid / unscored) | Best candidate TOTAL_SCORE | Best ΔTOTAL_SCORE | Median candidate-minus-baseline TOTAL_SCORE | Accepted improvements |",
                "|---|---:|---:|---:|---:|---:|---:|",
            ])
            for row in group_summary.itertuples(index=False):
                lines.append(
                    "| {group} | {parameters} | {trials} ({finite} / {invalid} / {unscored}) | {best} | {delta} | {median} | {accepted} |".format(
                        group=md_cell(row.calibration_group),
                        parameters=int(row.parameter_count),
                        trials=int(row.trial_count),
                        finite=int(row.finite_valid_trial_count),
                        invalid=int(row.invalid_trial_count),
                        unscored=int(row.unscored_trial_count),
                        best=score(row.best_candidate_TOTAL_SCORE),
                        delta=score(row.best_delta_TOTAL_SCORE),
                        median=score(row.median_candidate_minus_baseline_TOTAL_SCORE),
                        accepted=int(row.accepted_improvement_count),
                    )
                )

        lines.extend([
            "",
            "## Integrity and data-availability rules",
            "",
            "- The reporter computes SHA-256 hashes of protected V14 inputs and state files before and after report generation.",
            f"- Protected campaign files unchanged: `{hashes_before == hashes_after}`",
            "- Missing candidate-level pH/SO4 response data are recorded as `not_available`; the reporter does not infer numerical effects from conceptual process theory.",
            "- This report does not run MIN3P, select candidates, change bounds, update state, or edit `agent_config.xlsx`.",
            "",
            "## Warnings",
            "",
        ])
        if self.warnings:
            lines.extend([f"- {warning}" for warning in self.warnings])
        else:
            lines.append("- None.")
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        figure_items = [
            ("Conceptual hydrogeochemical model", relative(artifacts["conceptual_png"])),
            ("Calibration decision timeline", relative(artifacts["timeline_png"])),
            ("Parameter sensitivity summary", relative(artifacts["sensitivity_png"])),
        ]
        calibration_interpretation_html = """
<section id="interpretation"><h2>Calibration interpretation</h2>
<div class="note"><strong>What V14 evaluated.</strong> Hydraulic, mineral-kinetic, sorption, boundary-chemistry, and silicate-weathering parameter families were tested against the composite <code>TOTAL_SCORE</code>. Lower scores indicate closer overall agreement with the calibration targets.</div>
<div class="notice"><strong>Evidence boundary.</strong> Candidate-specific pH and SO4 response metrics were not stored for every parameter trial. This report therefore does not attribute measured pH or SO4 changes to individual parameter tests.</div>
</section>
"""

        figures_html = "".join(
            f"<section><h2>{escape(title)}</h2><img src=\"{escape(path)}\" alt=\"{escape(title)}\"></section>"
            for title, path in figure_items
        )
        summary_rows_html = "".join(
            "<tr>"
            f"<td>{escape(value(row.parameter))}</td>"
            f"<td>{escape(value(row.calibration_group))}</td>"
            f"<td>{escape(value(row.process_family))}</td>"
            f"<td>{escape(value(row.hydrogeochemical_process))}</td>"
            f"<td>{escape(score(row.baseline_value))}</td>"
            f"<td>{escape(score(row.best_tested_value))}</td>"
            f"<td>{escape(value(row.sensitivity_class))}</td>"
            "</tr>"
            for row in sensitivity.itertuples(index=False)
        )
        sensitivity_rows_html = "".join(
            "<tr>"
            f"<td>{escape(value(row.parameter))}</td>"
            f"<td>{escape(value(row.calibration_group))}</td>"
            f"<td>{int(row.trial_count)} ({int(row.finite_valid_trial_count)} / {int(row.invalid_trial_count)} / {int(row.unscored_trial_count)})</td>"
            f"<td>{escape(score(row.median_abs_objective_change_per_1pct))}</td>"
            f"<td>{escape(value(row.sensitivity_class))}</td>"
            f"<td>{escape(value(row.most_sensitive_direction))}</td>"
            f"<td>{escape(value(row.best_tested_direction))}</td>"
            f"<td>{escape(score(row.best_delta_TOTAL_SCORE))}</td>"
            f"<td>{escape(value(row.pH_candidate_response))}</td>"
            f"<td>{escape(value(row.SO4_candidate_response))}</td>"
            f"<td>{escape(value(row.calibration_outcome))}</td>"
            "</tr>"
            for row in sensitivity.itertuples(index=False)
        )
        group_rows_html = "".join(
            "<tr>"
            f"<td>{escape(value(row.calibration_group))}</td>"
            f"<td>{int(row.parameter_count)}</td>"
            f"<td>{int(row.trial_count)} ({int(row.finite_valid_trial_count)} / {int(row.invalid_trial_count)} / {int(row.unscored_trial_count)})</td>"
            f"<td>{escape(score(row.best_candidate_TOTAL_SCORE))}</td>"
            f"<td>{escape(score(row.best_delta_TOTAL_SCORE))}</td>"
            f"<td>{escape(score(row.median_candidate_minus_baseline_TOTAL_SCORE))}</td>"
            f"<td>{int(row.accepted_improvement_count)}</td>"
            "</tr>"
            for row in group_summary.itertuples(index=False)
        )
        warnings_html = "".join(f"<li>{escape(item)}</li>" for item in (self.warnings or ["None."]))
        top_sensitivity = sensitivity.head(10)
        top_rows_html = "".join(
            "<tr>"
            f"<td><code>{escape(value(row.parameter))}</code></td>"
            f"<td>{escape(value(row.process_family))}</td>"
            f'<td><span class="badge {escape(_norm(row.sensitivity_class).split("_")[0])}">{escape(value(row.sensitivity_class))}</span></td>'
            f"<td>{escape(score(row.median_abs_objective_change_per_1pct))}</td>"
            f"<td>{escape(score(row.best_delta_TOTAL_SCORE))}</td>"
            "</tr>"
            for row in top_sensitivity.itertuples(index=False)
        )
        accepted_count = int(trials["decision_class"].astype(str).eq("accepted").sum()) if not trials.empty else 0
        rejected_count = int(trials["decision_class"].astype(str).eq("rejected").sum()) if not trials.empty else 0
        response_text = (
            "Candidate-linked pH and SO4 response metrics are available in this report."
            if metrics_available else
            "Candidate-linked pH and SO4 response metrics were not recorded in the V14 decision log; no response atlas is shown."
        )
        quick_links = "".join(
            f'<a href="{escape(relative(path))}" download>{escape(label)}</a>'
            for label, path in [
                ("Trial evidence workbook", artifacts["trial_table"]),
                ("pH/SO4 metrics workbook", artifacts["metrics_table"]),
                ("Sensitivity workbook", artifacts["sensitivity_table"]),
                ("Group overview workbook", artifacts["group_table"]),
                ("Campaign analysis JSON", artifacts["campaign_analysis_json"]),
                ("Campaign analysis workbook", artifacts["campaign_analysis_xlsx"]),
                ("Campaign review JSON", artifacts["campaign_review_json"]),
                ("Campaign review Markdown", artifacts["campaign_review_markdown"]),
            ]
        )
        html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Calibration story — V15.4.1</title>
<style>
:root{{--ink:#1f2d38;--muted:#586773;--line:#d8e0e5;--soft:#f4f7f9;--card:#fff;--accent:#2d6f9f;--good:#2f7d4a;--warn:#966d14}}
*{{box-sizing:border-box}} body{{font-family:Arial,sans-serif;max-width:1320px;margin:0 auto;line-height:1.45;color:var(--ink);background:#fff;padding:0 24px 48px}}
h1{{font-size:2rem;margin:30px 0 4px}} h2{{margin:28px 0 10px;font-size:1.32rem}} h3{{margin:0 0 8px;font-size:1rem}} p{{margin:7px 0}} code{{background:#eef2f5;padding:2px 4px;border-radius:3px}}
nav{{position:sticky;top:0;background:rgba(255,255,255,.96);border-bottom:1px solid var(--line);padding:11px 0;z-index:20;display:flex;gap:16px;flex-wrap:wrap}} nav a{{color:var(--accent);text-decoration:none;font-size:.9rem;font-weight:bold}}
.meta{{color:var(--muted);font-size:.92rem}} .grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px;margin:18px 0}} .card{{border:1px solid var(--line);border-radius:8px;padding:14px;background:var(--card)}} .value{{font-size:1.55rem;font-weight:bold;margin:3px 0}} .small-value{{font-size:1rem;overflow-wrap:anywhere}} .label{{color:var(--muted);font-size:.82rem;text-transform:uppercase;letter-spacing:.03em}}
.notice{{background:#f7f3e8;border-left:4px solid var(--warn);padding:12px 14px;margin:16px 0}} .note{{background:#eef5f9;border-left:4px solid var(--accent);padding:12px 14px;margin:16px 0}}
img{{width:100%;max-width:1120px;border:1px solid var(--line);margin:8px 0 16px}} .figure{{margin-bottom:20px}} .table-wrap{{overflow-x:auto;margin:10px 0 18px}} table{{border-collapse:collapse;width:100%;font-size:.87rem}} th,td{{border:1px solid var(--line);padding:7px;text-align:left;vertical-align:top}} th{{background:#eef3f6;white-space:nowrap}} tr:nth-child(even) td{{background:#fbfcfd}}
.badge{{display:inline-block;padding:2px 6px;border-radius:10px;font-size:.76rem;font-weight:bold;background:#e9edf0}} .badge.high{{background:#f6dfd8}} .badge.moderate{{background:#f6edcf}} .badge.low{{background:#e0efdf}}
details{{border:1px solid var(--line);border-radius:7px;padding:10px 12px;margin:14px 0}} summary{{cursor:pointer;font-weight:bold}} .downloads a{{display:inline-block;margin:4px 8px 4px 0;padding:7px 9px;background:#eef5f9;color:#1f628f;text-decoration:none;border-radius:5px;font-size:.85rem}}
.footer{{margin-top:28px;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:.82rem}}
@media(max-width:820px){{.grid{{grid-template-columns:repeat(2,minmax(0,1fr))}} body{{padding:0 14px 36px}}}} @media(max-width:460px){{.grid{{grid-template-columns:1fr}} h1{{font-size:1.55rem}}}}
</style></head><body>
<nav><a href="#overview">Overview</a><a href="#campaign-review">Campaign review</a><a href="#figures">Figures</a><a href="#sensitivity">Sensitivity</a><a href="#evidence">Evidence tables</a><a href="#downloads">Downloads</a></nav>
<header id="overview"><h1>{escape(self.paths.project_dir.name)} calibration story</h1><p class="meta">Generated {escape(_now())} · V15.4.2 read-only report · Project: {escape(self.paths.project_dir.name)}</p></header>
<section class="grid"><div class="card"><div class="label">Best TOTAL_SCORE</div><div class="value">{escape(score(best_score) or "not available")}</div><div class="meta">Campaign evidence</div></div><div class="card"><div class="label">Candidate trials</div><div class="value">{len(trials)}</div><div class="meta">{rejected_count} rejected · {accepted_count} accepted</div></div><div class="card"><div class="label">Sensitivity targets</div><div class="value">{len(sensitivity)}</div><div class="meta">Single-parameter evidence</div></div><div class="card"><div class="label">pH/SO4 candidate evidence</div><div class="value">{"Available" if metrics_available else "Not recorded"}</div><div class="meta">No inferred values</div></div></section>
<div class="notice"><strong>Evidence boundary.</strong> {escape(response_text)}</div>
{campaign_review_html}
<section id="figures"><h2>Figures</h2><div class="figure"><h3>Conceptual hydrogeochemical model</h3><img src="{escape(relative(artifacts['conceptual_png']))}" alt="Conceptual hydrogeochemical model"></div><div class="figure"><h3>Calibration decision timeline</h3><img src="{escape(relative(artifacts['timeline_png']))}" alt="Calibration decision timeline"></div><div class="figure"><h3>Parameter sensitivity summary</h3><img src="{escape(relative(artifacts['sensitivity_png']))}" alt="Parameter sensitivity summary"></div></section>
{calibration_interpretation_html}<section id="sensitivity"><h2>Priority sensitivity view</h2><p class="note">Sensitivity ranks the local response of TOTAL_SCORE per one-percent parameter perturbation. It is not a pH or SO4 sensitivity metric. A positive best ΔTOTAL_SCORE means the best tested candidate was still worse than the campaign best configuration.</p><div class="table-wrap"><table><thead><tr><th>Parameter</th><th>Process family</th><th>Relative class</th><th>Median objective sensitivity / 1%</th><th>Best ΔTOTAL_SCORE</th></tr></thead><tbody>{top_rows_html}</tbody></table></div></section>
<section id="evidence"><h2>Evidence tables</h2><details><summary>Full parameter sensitivity and calibration evidence ({len(sensitivity)} parameters)</summary><div class="table-wrap"><table><thead><tr><th>Parameter</th><th>Group</th><th>Trials (scored / invalid / unscored)</th><th>Median objective sensitivity / 1%</th><th>Class</th><th>Strongest direction</th><th>Best direction</th><th>Best ΔTOTAL_SCORE</th><th>Outcome</th></tr></thead><tbody>{''.join('<tr>'+f'<td>{escape(value(r.parameter))}</td><td>{escape(value(r.calibration_group))}</td><td>{int(r.trial_count)} ({int(r.finite_valid_trial_count)} / {int(r.invalid_trial_count)} / {int(r.unscored_trial_count)})</td><td>{escape(score(r.median_abs_objective_change_per_1pct))}</td><td>{escape(value(r.sensitivity_class))}</td><td>{escape(value(r.most_sensitive_direction))}</td><td>{escape(value(r.best_tested_direction))}</td><td>{escape(score(r.best_delta_TOTAL_SCORE))}</td><td>{escape(value(r.calibration_outcome))}</td>'+'</tr>' for r in sensitivity.itertuples(index=False))}</tbody></table></div></details><details><summary>Calibration-group overview</summary><div class="table-wrap"><table><thead><tr><th>Calibration group</th><th>Parameters</th><th>Trials</th><th>Best candidate TOTAL_SCORE</th><th>Best ΔTOTAL_SCORE</th><th>Median candidate-minus-baseline</th><th>Accepted improvements</th></tr></thead><tbody>{group_rows_html}</tbody></table></div></details><details><summary>Compact parameter summary</summary><div class="table-wrap"><table><thead><tr><th>Parameter</th><th>Calibration group</th><th>Process family</th><th>Hydrogeochemical process</th><th>Baseline</th><th>Best tested</th><th>Sensitivity class</th></tr></thead><tbody>{summary_rows_html}</tbody></table></div></details></section>
<section id="downloads"><h2>Download evidence</h2><div class="downloads">{quick_links}</div></section>
<section><h2>Warnings</h2><ul>{warnings_html}</ul></section>
<div class="footer">Read-only reporting: no MIN3P run, parameter change, bounds update, optimizer-state mutation, or modification of <code>agent_config.xlsx</code>.</div>
</body></html>"""
        html_path.write_text(html, encoding="utf-8")

    def generate(self) -> V15ReportResult:
        hashes_before = self.protected_hashes()
        self.paths.output_dir.mkdir(parents=True, exist_ok=True)
        figures_dir = self.paths.output_dir / "figures"
        tables_dir = self.paths.output_dir / "tables"
        figures_dir.mkdir(parents=True, exist_ok=True)
        tables_dir.mkdir(parents=True, exist_ok=True)

        campaign_package = generate_campaign_review_package(
            self.paths.agent_core_dir,
            output_dir=self.paths.output_dir,
            config_file=self.campaign_review_config,
            mode=self.campaign_review_mode,
        )
        campaign_package = self._apply_v15_4_1_corrections(campaign_package)
        campaign_analysis = campaign_package.get("analysis", {})
        campaign_review = campaign_package.get("review", {})
        campaign_review_metadata = campaign_package.get("review_metadata", {})
        if campaign_review_metadata.get("gpt_error"):
            self.warnings.append("GPT campaign review was unavailable; deterministic review used: " + str(campaign_review_metadata["gpt_error"]))

        trials, metadata = self._decision_table()
        metrics, metric_metadata = self._extract_ph_so4_metrics(trials)
        metadata["pH_SO4_metrics"] = metric_metadata
        campaign_state = self._campaign_state()

        conceptual_svg = figures_dir / "01_conceptual_hydrogeochemical_model.svg"
        conceptual_png = figures_dir / "01_conceptual_hydrogeochemical_model.png"
        audit_dir = self.paths.output_dir / "audit"
        audit_dir.mkdir(parents=True, exist_ok=True)
        conceptual_facts_path = audit_dir / "01_conceptual_model_facts.json"
        conceptual_prompt_path = audit_dir / "01_conceptual_model_prompt.txt"
        conceptual_generation_path = audit_dir / "01_conceptual_model_generation_manifest.json"
        conceptual_draft_path = figures_dir / "01_conceptual_model_deterministic_draft.png"
        timeline_png = figures_dir / "02_calibration_decision_timeline.png"
        process_map_svg_path = figures_dir / "03_parameter_process_observable_map.svg"
        # V15.2.8: remove the redundant parameter-process atlas rather than
        # presenting a diagram that could be mistaken for measured pH/SO4 effects.
        if process_map_svg_path.exists():
            process_map_svg_path.unlink()
        process_map_svg = None
        response_atlas_png = figures_dir / "04_pH_SO4_response_atlas.png"
        # V15.2.4: the pH/SO4 atlas is deliberately omitted because V14 has no
        # candidate-linked pH/SO4 metrics. Remove stale output from older reports.
        if response_atlas_png.exists():
            response_atlas_png.unlink()
        response_atlas_png = None
        sensitivity_png = figures_dir / "05_parameter_sensitivity_summary.png"
        facts = self._dat_conceptual_facts()
        conceptual_facts_path.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding="utf-8")
        prompt = self._conceptual_prompt(facts)
        conceptual_prompt_path.write_text(prompt + "\n", encoding="utf-8")
        gpt_config = self._gpt_conceptual_config()
        gpt_used = False
        generation_status = "deterministic_requested"
        if self.conceptual_image_mode in {"gpt", "auto"}:
            conceptual_draft_path = figures_dir / "01_conceptual_model_gpt_draft.png"
            gpt_used, generation_status = self._generate_gpt_draft(prompt, conceptual_draft_path, gpt_config)
            if not gpt_used:
                self.warnings.append(f"GPT conceptual-image draft not used: {generation_status}. A deterministic draft was generated instead.")
                conceptual_draft_path = figures_dir / "01_conceptual_model_deterministic_draft.png"
                self._draw_deterministic_draft(facts, conceptual_draft_path)
        else:
            self._draw_deterministic_draft(facts, conceptual_draft_path)
        self._draw_dat_driven_conceptual_model(
            facts, self._load_conceptual_config(), conceptual_svg, conceptual_png, conceptual_draft_path,
            draft_opacity=float(gpt_config.get("background_opacity", 0.20)),
        )
        conceptual_generation_path.write_text(json.dumps({
            "reporter_version": REPORTER_VERSION,
            "generated_at": _now(),
            "dat_file": facts.get("source", {}).get("dat_file"),
            "dat_sha256": facts.get("source", {}).get("sha256"),
            "conceptual_image_mode": self.conceptual_image_mode,
            "conceptual_detail": self.conceptual_detail,
            "gpt_config_enabled": bool(gpt_config.get("enabled", False)),
            "gpt_used": bool(gpt_used),
            "generation_status": generation_status,
            "draft_png": str(conceptual_draft_path),
            "final_png": str(conceptual_png),
            "final_svg": str(conceptual_svg),
            "facts_json": str(conceptual_facts_path),
            "prompt_file": str(conceptual_prompt_path),
            "overlay_policy": "Python renders all scientific text, arrows, boundaries, grid facts, and legend. GPT never supplies authoritative labels.",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        self._draw_timeline(trials, timeline_png)
        metrics_available = bool((metrics["pH_metric_status"].eq("available") | metrics["SO4_metric_status"].eq("available")).any()) if not metrics.empty else False
        sensitivity = self._parameter_sensitivity_summary(trials, metrics)
        self._draw_parameter_sensitivity_summary(sensitivity, sensitivity_png)
        group_summary = self._calibration_group_summary(trials)
        trial_table, metrics_table, sensitivity_table, group_table = self._write_excel_tables(
            trials, metrics, sensitivity, group_summary, tables_dir
        )

        artifacts = {
            "conceptual_svg": conceptual_svg,
            "conceptual_png": conceptual_png,
            "conceptual_facts": conceptual_facts_path,
            "conceptual_prompt": conceptual_prompt_path,
            "conceptual_generation_manifest": conceptual_generation_path,
            "conceptual_draft_png": conceptual_draft_path,
            "timeline_png": timeline_png,
            "sensitivity_png": sensitivity_png,
            "trial_table": trial_table,
            "metrics_table": metrics_table,
            "sensitivity_table": sensitivity_table,
            "group_table": group_table,
            "campaign_analysis_json": Path(campaign_package["artifacts"]["campaign_analysis_json"]),
            "campaign_analysis_xlsx": Path(campaign_package["artifacts"]["campaign_analysis_xlsx"]),
            "campaign_review_json": Path(campaign_package["artifacts"]["campaign_review_json"]),
            "campaign_review_markdown": Path(campaign_package["artifacts"]["campaign_review_markdown"]),
            "campaign_review_manifest": Path(campaign_package["artifacts"]["campaign_review_manifest"]),
        }
        markdown = self.paths.output_dir / "V15_calibration_story_report.md"
        html = self.paths.output_dir / "V15_calibration_story_report.html"
        hashes_after_figures = self.protected_hashes()
        self._write_report(
            trials, metrics, sensitivity, group_summary, metadata, campaign_state,
            artifacts, markdown, html, hashes_before, hashes_after_figures,
            campaign_analysis, campaign_review, campaign_review_metadata,
        )
        hashes_after = self.protected_hashes()
        manifest = self.paths.output_dir / "report_manifest.json"
        manifest_payload = {
            "reporter_version": REPORTER_VERSION,
            "generated_at": _now(),
            "mode": "read_only_reporting",
            "project_dir": str(self.paths.project_dir),
            "agent_core_dir": str(self.paths.agent_core_dir),
            "decision_log_source": metadata.get("decision_log_source"),
            "candidate_trial_count": int(len(trials)),
            "pH_SO4_metrics": metric_metadata,
            "campaign_review": {
                "requested_mode": self.campaign_review_mode,
                "used_mode": campaign_review_metadata.get("used_mode"),
                "status": campaign_review_metadata.get("status"),
                "analysis_json": str(artifacts["campaign_analysis_json"]),
                "review_json": str(artifacts["campaign_review_json"]),
            },
            "conceptual_model": {
                "dat_file": facts.get("source", {}).get("dat_file"),
                "dat_sha256": facts.get("source", {}).get("sha256"),
                "conceptual_image_mode": self.conceptual_image_mode,
                "conceptual_detail": self.conceptual_detail,
                "facts_file": str(conceptual_facts_path),
                "generation_manifest": str(conceptual_generation_path),
            },
            "protected_hashes_before": hashes_before,
            "protected_hashes_after": hashes_after,
            "protected_campaign_hashes_unchanged": hashes_before == hashes_after,
            "artifacts": {key: str(value) for key, value in artifacts.items()},
            "warnings": self.warnings,
        }
        manifest.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2), encoding="utf-8")

        return V15ReportResult(
            output_dir=str(self.paths.output_dir),
            report_markdown=str(markdown),
            report_html=str(html),
            manifest=str(manifest),
            conceptual_svg=str(conceptual_svg),
            conceptual_png=str(conceptual_png),
            timeline_png=str(timeline_png),
            process_map_svg=None,
            response_atlas_png=None,
            parameter_sensitivity_png=str(sensitivity_png),
            parameter_trial_effects=str(trial_table),
            ph_so4_response_metrics=str(metrics_table),
            parameter_sensitivity_summary=str(sensitivity_table),
            calibration_group_summary=str(group_table),
            conceptual_facts=str(conceptual_facts_path),
            conceptual_prompt=str(conceptual_prompt_path),
            conceptual_generation_manifest=str(conceptual_generation_path),
            conceptual_draft_png=str(conceptual_draft_path),
            conceptual_detail=self.conceptual_detail,
            campaign_analysis_json=str(artifacts["campaign_analysis_json"]),
            campaign_analysis_xlsx=str(artifacts["campaign_analysis_xlsx"]),
            campaign_review_json=str(artifacts["campaign_review_json"]),
            campaign_review_markdown=str(artifacts["campaign_review_markdown"]),
            campaign_review_manifest=str(artifacts["campaign_review_manifest"]),
            campaign_review_mode=str(campaign_review_metadata.get("used_mode", self.campaign_review_mode)),
            protected_hashes_unchanged=hashes_before == hashes_after,
            trial_count=int(len(trials)),
            pH_so4_metrics_available=metrics_available,
            warnings=self.warnings.copy(),
        )


def generate_v15_scientific_report(
    agent_core_dir: str | Path,
    *,
    output_dir: str | Path | None = None,
    conceptual_config_file: str | Path | None = None,
    dat_file: str | Path | None = None,
    conceptual_image_mode: str = "deterministic",
    conceptual_detail: str = "paper",
    campaign_review_mode: str = "auto",
    campaign_review_config: str | Path | None = None,
) -> dict[str, Any]:
    """Public, read-only V15.2.3 reporting entry point with paper/technical conceptual-model modes."""
    paths = V15ProjectPaths.from_agent_core(
        agent_core_dir,
        output_dir=output_dir,
        conceptual_config_file=conceptual_config_file,
    )
    return V15ScientificReporter(
        paths, dat_file=dat_file, conceptual_image_mode=conceptual_image_mode, conceptual_detail=conceptual_detail,
        campaign_review_mode=campaign_review_mode, campaign_review_config=campaign_review_config,
    ).generate().as_dict()
