from __future__ import annotations

"""V15.3 read-only explainability layer for V14/V15 MIN3P calibration evidence.

This module reconstructs why each V14 candidate was tested from existing audit
files. It does not select candidates, run MIN3P, or modify V14 state.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from html import escape
from typing import Any, Iterable
import hashlib
import json
import math
import re

import pandas as pd

EXPLAINABILITY_VERSION = "V15.3.1"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe(value: Any) -> str:
    if value is None:
        return ""
    try:
        if isinstance(value, float) and math.isnan(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _safe(value).casefold()).strip("_")


def _to_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except Exception:
        return None


def _sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_excel(path: Path) -> pd.DataFrame:
    try:
        return pd.read_excel(path)
    except Exception:
        return pd.DataFrame()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _first_column(frame: pd.DataFrame, names: Iterable[str]) -> str | None:
    lookup = {_norm(c): str(c) for c in frame.columns}
    for name in names:
        key = _norm(name)
        if key in lookup:
            return lookup[key]
    return None


def _process_profile(parameter: Any, group: Any) -> dict[str, str]:
    name = _norm(parameter)
    group_key = _norm(group)
    profile = {
        "process_family": "Model calibration control",
        "hydrogeochemical_process": "Parameter-specific model response",
        "scientific_interpretation": "The candidate tested whether this parameter improved the composite calibration objective.",
    }
    if name in {"kz", "bottom_head", "dispersivity", "porosity", "residual_sat", "vg_alpha", "vg_n", "vg_l", "top_flux", "gas_diff"} or group_key == "hydraulic":
        profile.update({
            "process_family": "Hydraulic transport",
            "hydrogeochemical_process": "Water flow, residence time, gas access, dilution and vertical solute transport",
            "scientific_interpretation": "The candidate tested whether changing transport or residence-time controls improved the composite calibration objective.",
        })
    elif "pyrite" in name:
        profile.update({
            "process_family": "Sulfide oxidation",
            "hydrogeochemical_process": "Pyrite oxidation, acidity generation and sulfate release",
            "scientific_interpretation": "The candidate tested whether changing the pyrite oxidation control improved the multi-species calibration objective.",
        })
    elif any(token in name for token in ("calcite", "magnesite", "ankerite", "carbonate")):
        profile.update({
            "process_family": "Carbonate neutralisation",
            "hydrogeochemical_process": "Carbonate dissolution, acid neutralisation and buffering",
            "scientific_interpretation": "The candidate tested whether changing neutralisation capacity or kinetics improved overall agreement.",
        })
    elif any(token in name for token in ("feoh", "ferrihydrite", "sorption", "site_density")):
        profile.update({
            "process_family": "Fe hydroxide sorption",
            "hydrogeochemical_process": "Surface complexation, metal retention and reactive surface availability",
            "scientific_interpretation": "The candidate tested whether changing surface-complexation controls improved the composite objective.",
        })
    elif any(token in name for token in ("biotite", "albite", "chlorite")):
        profile.update({
            "process_family": "Silicate weathering",
            "hydrogeochemical_process": "Silicate dissolution, cation release and longer-term buffering",
            "scientific_interpretation": "The candidate tested whether changing silicate-weathering control improved the composite objective.",
        })
    elif name.startswith("bc_") or "boundary" in group_key:
        profile.update({
            "process_family": "Boundary chemistry",
            "hydrogeochemical_process": "Influent pH, gas fugacity, or imposed boundary chemistry",
            "scientific_interpretation": "The candidate tested whether changing an imposed boundary condition improved the calibration objective.",
        })
    elif name.startswith("scaling_ox") or "oxid" in name:
        profile.update({
            "process_family": "Redox / oxidation scaling",
            "hydrogeochemical_process": "Oxidation intensity and electron-acceptor availability",
            "scientific_interpretation": "The candidate tested whether changing redox reaction intensity improved the composite objective.",
        })
    elif group_key == "mineral_kinetics" or name.startswith("keff_"):
        profile.update({
            "process_family": "Mineral kinetics",
            "hydrogeochemical_process": "Mineral dissolution or precipitation rate control",
            "scientific_interpretation": "The candidate tested whether changing a mineral kinetic control improved the composite objective.",
        })
    return profile


def _display_group(parameter: Any, group: Any) -> str:
    """Return a report-facing optimizer group name."""
    parameter_key = _norm(parameter)
    group_text = _safe(group) or "unresolved"
    group_key = _norm(group_text)
    if parameter_key in {"s_biotite", "s_albite", "s_chlorite"} or group_key in {"other", "silicate", "silicates", "silicate_weathering"}:
        return "silicate-weathering / other"
    return group_text


def _fmt_number(value: Any, digits: int = 6) -> str:
    parsed = _to_float(value)
    if parsed is None:
        return "not available"
    return f"{parsed:.{digits}f}"


def _md_cell(value: Any) -> str:
    text = _safe(value)
    return text.replace("|", "\\|").replace("\n", "<br>")


def _html_bullets(items: Iterable[Any]) -> str:
    safe_items = [escape(_safe(item)) for item in items if _safe(item)]
    if not safe_items:
        return "<p>Not available.</p>"
    return "<ul>" + "".join(f"<li>{item}</li>" for item in safe_items) + "</ul>"


def _markdown_bullets(items: Iterable[Any]) -> str:
    safe_items = [_md_cell(item) for item in items if _safe(item)]
    if not safe_items:
        return "Not available."
    return "\n".join(f"- {item}" for item in safe_items)


@dataclass(frozen=True)
class ExplainabilityPaths:
    project_dir: Path
    agent_core_dir: Path
    input_dir: Path
    results_dir: Path
    reports_dir: Path
    output_dir: Path
    explainability_dir: Path

    @classmethod
    def from_agent_core(cls, agent_core_dir: str | Path, output_dir: str | Path | None = None) -> "ExplainabilityPaths":
        core = Path(agent_core_dir).resolve()
        project: Path | None = None
        for candidate in [core.parent, *core.parents]:
            if (candidate / "01_input").is_dir() and (candidate / "04_results").is_dir():
                project = candidate
                break
        if project is None:
            project = core.parent
        reports = project / "05_reports"
        out = Path(output_dir).resolve() if output_dir else reports / "V15_scientific_report"
        return cls(
            project_dir=project,
            agent_core_dir=core,
            input_dir=project / "01_input",
            results_dir=project / "04_results",
            reports_dir=reports,
            output_dir=out,
            explainability_dir=out / "explainability",
        )


class V15ExplainabilityBuilder:
    def __init__(self, paths: ExplainabilityPaths):
        self.paths = paths
        self.warnings: list[str] = []
        self._protected_paths = [
            paths.input_dir / "agent_config.xlsx",
            paths.results_dir / "best_parameters_V14.xlsx",
            paths.results_dir / "v14_optimizer_state.xlsx",
            paths.results_dir / "v14_optimizer_parameter_state.xlsx",
            paths.results_dir / "v14_parameter_runtime_state.json",
            paths.results_dir / "v14_step_size_state.json",
        ]

    def protected_hashes(self) -> dict[str, str | None]:
        return {str(p): _sha256(p) for p in self._protected_paths}

    def _decision_log(self) -> pd.DataFrame:
        for name in ["calibration_decision_log.xlsx", "v14_candidate_decisions.xlsx"]:
            path = self.paths.results_dir / name
            frame = _read_excel(path)
            if not frame.empty:
                return frame
        self.warnings.append("No calibration decision log found.")
        return pd.DataFrame()

    def _parameter_state(self) -> pd.DataFrame:
        return _read_excel(self.paths.results_dir / "v14_optimizer_parameter_state.xlsx")

    def _campaign_state(self) -> dict[str, Any]:
        for name in ["v14_campaign_state.json", "v14_optimizer_state.json"]:
            path = self.paths.results_dir / name
            if path.exists():
                return _read_json(path)
        return {}

    def _interaction_state(self) -> dict[str, Any]:
        return _read_json(self.paths.results_dir / "v14_interaction_runtime_state.json")

    def _gpt_config_status(self) -> str:
        config = self.paths.agent_core_dir / "config" / "gpt_supervisor_config.yaml"
        if not config.exists():
            return "configuration_not_found"
        text = config.read_text(encoding="utf-8", errors="replace").casefold()
        if re.search(r"^\s*enabled\s*:\s*true\s*$", text, flags=re.MULTILINE):
            return "enabled_in_yaml"
        if re.search(r"^\s*enabled\s*:\s*false\s*$", text, flags=re.MULTILINE):
            return "disabled_in_yaml"
        return "unknown"

    def _transaction_index(self) -> list[dict[str, Any]]:
        base = self.paths.results_dir / "v14_transactions"
        rows: list[dict[str, Any]] = []
        if not base.exists():
            return rows
        for path in sorted(base.glob("*/transaction.json")):
            data = _read_json(path)
            if not data:
                continue
            suggestion = data.get("suggestion", {}) if isinstance(data.get("suggestion", {}), dict) else {}
            rows.append({
                "transaction_folder": str(path.parent),
                "status": data.get("status"),
                "run_status": data.get("run_status"),
                "parameter": suggestion.get("parameter"),
                "direction": suggestion.get("direction"),
                "step_fraction": suggestion.get("step_fraction_applied") or suggestion.get("step_fraction"),
                "candidate_id": data.get("candidate_id") or suggestion.get("candidate_id"),
                "raw": data,
            })
        return rows

    def _state_for_parameter(self, parameter: str, state: pd.DataFrame) -> dict[str, Any]:
        if state.empty or "parameter" not in state.columns:
            return {}
        subset = state[state["parameter"].astype(str).eq(parameter)]
        if subset.empty:
            return {}
        return {str(k): v for k, v in subset.iloc[0].to_dict().items()}

    def _evaluated_candidates(self, log: pd.DataFrame) -> pd.DataFrame:
        if log.empty:
            return pd.DataFrame()
        parameter_col = _first_column(log, ["parameter"])
        decision_col = _first_column(log, ["decision"])
        if parameter_col is None or decision_col is None:
            self.warnings.append("Decision log lacks parameter or decision column.")
            return pd.DataFrame()
        frame = log.copy()
        frame = frame[frame[parameter_col].map(_safe).ne("")]
        frame = frame[frame[decision_col].map(_safe).ne("")]
        return frame.reset_index(drop=True)

    def _matching_transaction(self, row: pd.Series, txns: list[dict[str, Any]]) -> dict[str, Any] | None:
        parameter = _safe(row.get("parameter"))
        direction = _safe(row.get("direction"))
        step = _to_float(row.get("step_fraction"))
        matches: list[dict[str, Any]] = []
        for txn in txns:
            if _safe(txn.get("parameter")) != parameter:
                continue
            if direction and _safe(txn.get("direction")) != direction:
                continue
            if step is not None and _to_float(txn.get("step_fraction")) is not None:
                if abs(step - float(txn.get("step_fraction"))) > max(1e-12, abs(step) * 1e-6):
                    continue
            matches.append(txn)
        return matches[-1] if matches else None

    def _selection_reasons(
        self,
        row: pd.Series,
        state_row: dict[str, Any],
        transaction: dict[str, Any] | None,
        campaign_state: dict[str, Any],
        interaction_state: dict[str, Any],
        gpt_status: str,
    ) -> tuple[list[str], dict[str, Any]]:
        parameter = _safe(row.get("parameter"))
        group = _safe(row.get("group")) or _safe(state_row.get("group")) or "unresolved"
        direction = _safe(row.get("direction"))
        step = _to_float(row.get("step_fraction"))
        phase = _safe(row.get("phase")) or _safe(state_row.get("phase"))
        status = _safe(state_row.get("status")) or _safe(row.get("status"))
        user_status = _safe(state_row.get("user_status"))

        reasons: list[str] = []
        if parameter:
            reasons.append(f"V14 selected parameter '{parameter}' from the deterministic optimizer queue.")
        if group:
            reasons.append(f"Calibration group at selection/evaluation: {group}.")
        if user_status:
            reasons.append(f"User status permitted consideration: {user_status}.")
        elif status:
            reasons.append(f"Runtime status available in optimizer state: {status}.")
        else:
            reasons.append("Runtime state was not available in the current exported parameter-state table.")
        if phase:
            reasons.append(f"Optimizer phase/state context: {phase}.")
        if direction:
            reasons.append(f"Direction tested: {direction}; direction-pair precedence was handled by V14 before the candidate was run.")
        if step is not None:
            reasons.append(f"Adaptive step fraction applied: {step:g}.")
        if transaction:
            reasons.append("A V14 transaction manifest exists for this candidate; execution was transaction-isolated.")
        else:
            reasons.append("No matching transaction manifest was found; explanation is reconstructed from the decision log and state tables.")
        if gpt_status == "disabled_in_yaml":
            reasons.append("GPT supervisor was disabled; selection was deterministic.")
        elif gpt_status == "enabled_in_yaml":
            reasons.append("GPT supervisor was configured as enabled, but deterministic V14 precedence still governed execution.")
        else:
            reasons.append(f"GPT supervisor status: {gpt_status}.")

        context = {
            "current_pass": campaign_state.get("current_pass"),
            "accepted_improvements_this_pass": campaign_state.get("pass_accepted_improvements_count"),
            "interaction_runtime_state_available": bool(interaction_state),
            "matching_transaction_found": bool(transaction),
            "optimizer_phase": phase,
            "runtime_status": status,
            "user_status": user_status,
            "gpt_status": gpt_status,
        }
        return reasons, context

    def _candidate_explanation(self, index: int, row: pd.Series, state: pd.DataFrame, txns: list[dict[str, Any]], campaign: dict[str, Any], interactions: dict[str, Any], gpt_status: str) -> dict[str, Any]:
        parameter = _safe(row.get("parameter"))
        group = _safe(row.get("group"))
        state_row = self._state_for_parameter(parameter, state)
        if not group:
            group = _safe(state_row.get("group"))
        display_group = _display_group(parameter, group)
        transaction = self._matching_transaction(row, txns)
        _legacy_reasons, context = self._selection_reasons(row, state_row, transaction, campaign, interactions, gpt_status)
        profile = _process_profile(parameter, group)

        direction = _safe(row.get("direction"))
        step_fraction = _to_float(row.get("step_fraction"))
        baseline = _to_float(row.get("baseline_objective"))
        candidate = _to_float(row.get("candidate_objective"))
        delta = (candidate - baseline) if baseline is not None and candidate is not None else None
        accepted_raw = row.get("accepted")
        accepted = bool(_to_float(accepted_raw) == 1.0) if _to_float(accepted_raw) is not None else (_safe(row.get("decision")).casefold() == "accept")
        decision = _safe(row.get("decision"))
        rejection = _safe(row.get("rejection_reason"))

        if candidate is None:
            outcome = "unscored_or_invalid"
        elif accepted:
            outcome = "accepted_improvement"
        elif decision:
            outcome = decision
        else:
            outcome = "evaluated"

        if baseline is not None and candidate is not None:
            if delta is not None and delta > 0:
                score_reason = f"The candidate worsened TOTAL_SCORE from {_fmt_number(baseline)} to {_fmt_number(candidate)}."
            elif delta is not None and delta < 0:
                score_reason = f"The candidate improved TOTAL_SCORE from {_fmt_number(baseline)} to {_fmt_number(candidate)}."
            else:
                score_reason = f"The candidate left TOTAL_SCORE unchanged at {_fmt_number(candidate)}."
        else:
            score_reason = "The candidate objective values were not fully available in the decision log."

        selection_reasons = [
            f"{parameter} was active and eligible.",
            f"It belonged to the {display_group} parameter group.",
            "V14 selected it through the deterministic optimizer queue.",
            f"The tested direction was {direction}.",
            score_reason,
        ]

        if accepted:
            outcome_text = "Accepted; candidate parameter set became the retained best set."
        else:
            outcome_text = "Rejected; best parameter set was retained."

        if parameter == "s_chlorite" and direction.casefold() == "increase" and delta is not None and delta > 0:
            science = "Increasing chlorite weathering/scaling did not improve the composite calibration objective."
        elif delta is not None and delta > 0:
            science = f"The tested {parameter} {direction} perturbation did not improve the composite calibration objective."
        elif delta is not None and delta < 0:
            science = f"The tested {parameter} {direction} perturbation improved the composite calibration objective."
        else:
            science = profile["scientific_interpretation"]

        context.update({
            "display_group": display_group,
            "v15_3_1_interpretation": "candidate was active; parameter belonged to group; deterministic queue selected it; V14-selected direction/step were evaluated; decision followed TOTAL_SCORE comparison",
        })

        return {
            "schema_version": "V15.3.1_candidate_explanation_v1",
            "explainability_version": EXPLAINABILITY_VERSION,
            "candidate_number": index,
            "timestamp": _safe(row.get("timestamp")),
            "parameter": parameter,
            "group": group,
            "display_group": display_group,
            "direction": direction,
            "step_fraction": step_fraction,
            "old_value": _to_float(row.get("old_value")),
            "new_value": _to_float(row.get("new_value")),
            "selection_reason": selection_reasons,
            "decision_context": context,
            "candidate_result": {
                "baseline_total_score": baseline,
                "candidate_total_score": candidate,
                "delta_total_score": delta,
                "accepted": accepted,
                "decision": decision,
                "rejection_reason": rejection,
                "outcome": outcome,
                "outcome_text": outcome_text,
            },
            "scientific_interpretation": science,
            "process_family": profile["process_family"],
            "hydrogeochemical_process": profile["hydrogeochemical_process"],
            "evidence_limits": [
                "Candidate-level pH and SO4 response metrics were not recorded for every V14 parameter trial unless explicit columns or linked diagnostics exist.",
                "Selection reasons are reconstructed from the exported decision log, optimizer state, transaction manifests, and configuration files.",
            ],
            "transaction": {
                "found": bool(transaction),
                "transaction_folder": transaction.get("transaction_folder") if transaction else None,
                "status": transaction.get("status") if transaction else None,
                "run_status": transaction.get("run_status") if transaction else None,
            },
        }

    def _write_json(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _explanations_to_table(self, explanations: list[dict[str, Any]]) -> pd.DataFrame:
        rows: list[dict[str, Any]] = []
        for item in explanations:
            result = item.get("candidate_result", {})
            reasons = item.get("selection_reason", [])
            rows.append({
                "candidate_number": item.get("candidate_number"),
                "timestamp": item.get("timestamp"),
                "parameter": item.get("parameter"),
                "group": item.get("group"),
                "display_group": item.get("display_group") or _display_group(item.get("parameter"), item.get("group")),
                "direction": item.get("direction"),
                "step_fraction": item.get("step_fraction"),
                "old_value": item.get("old_value"),
                "new_value": item.get("new_value"),
                "baseline_total_score": result.get("baseline_total_score"),
                "candidate_total_score": result.get("candidate_total_score"),
                "delta_total_score": result.get("delta_total_score"),
                "decision": result.get("decision"),
                "rejection_reason": result.get("rejection_reason"),
                "accepted": result.get("accepted"),
                "outcome": result.get("outcome"),
                "outcome_text": result.get("outcome_text"),
                "process_family": item.get("process_family"),
                "hydrogeochemical_process": item.get("hydrogeochemical_process"),
                "why_selected": " | ".join(reasons),
                "why_selected_items_json": json.dumps(reasons, ensure_ascii=False),
                "scientific_interpretation": item.get("scientific_interpretation"),
                "transaction_found": item.get("transaction", {}).get("found"),
            })
        return pd.DataFrame(rows)

    def _summary_table(self, explanations: list[dict[str, Any]]) -> pd.DataFrame:
        records: list[dict[str, Any]] = []
        for item in explanations:
            for reason in item.get("selection_reason", []):
                label = reason.split(":", 1)[0].split(";", 1)[0].strip()
                records.append({"reason": label})
        if not records:
            return pd.DataFrame(columns=["reason", "count"])
        return pd.DataFrame(records).value_counts("reason").rename("count").reset_index()

    def _items_from_table_row(self, row: pd.Series) -> list[str]:
        raw = row.get("why_selected_items_json", "")
        try:
            data = json.loads(_safe(raw))
            if isinstance(data, list):
                return [_safe(item) for item in data if _safe(item)]
        except Exception:
            pass
        text = _safe(row.get("why_selected"))
        return [item.strip() for item in text.split(" | ") if item.strip()]

    def _latest_candidate_html_card(self, table: pd.DataFrame) -> str:
        if table.empty:
            return ""
        row = table.iloc[-1]
        items = self._items_from_table_row(row)
        parameter = escape(_safe(row.get("parameter")))
        direction = escape(_safe(row.get("direction")))
        step = escape(_fmt_number(row.get("step_fraction"), digits=6))
        outcome = escape(_safe(row.get("outcome_text")) or "Rejected; best parameter set was retained.")
        science = escape(_safe(row.get("scientific_interpretation")))
        return f"""
<!-- V15_LATEST_CANDIDATE_START -->
<section id="latest-candidate-explanation" class="latest-candidate-card">
<h2>Latest candidate explanation</h2>
<p><strong>Latest candidate tested:</strong><br>{parameter} {direction}, step {step}</p>
<p><strong>Why selected:</strong></p>
{_html_bullets(items)}
<p><strong>Outcome:</strong><br>{outcome}</p>
<p><strong>Scientific interpretation:</strong><br>{science}</p>
</section>
<!-- V15_LATEST_CANDIDATE_END -->
""".strip()

    def _latest_candidate_markdown_card(self, table: pd.DataFrame) -> str:
        if table.empty:
            return ""
        row = table.iloc[-1]
        items = self._items_from_table_row(row)
        return "\n".join([
            "<!-- V15_LATEST_CANDIDATE_START -->",
            "## Latest candidate explanation",
            "",
            "**Latest candidate tested:**",
            f"{_md_cell(row.get('parameter'))} {_md_cell(row.get('direction'))}, step {_fmt_number(row.get('step_fraction'), digits=6)}",
            "",
            "**Why selected:**",
            _markdown_bullets(items),
            "",
            "**Outcome:**",
            _md_cell(row.get("outcome_text") or "Rejected; best parameter set was retained."),
            "",
            "**Scientific interpretation:**",
            _md_cell(row.get("scientific_interpretation")),
            "<!-- V15_LATEST_CANDIDATE_END -->",
        ]) + "\n"

    def _html_section(self, table: pd.DataFrame) -> str:
        latest = table.tail(12).copy()
        if latest.empty:
            rows_html = "<tr><td colspan='7'>No evaluated candidate explanations were available.</td></tr>"
        else:
            rows = []
            for _, row in latest.iterrows():
                rows.append(
                    "<tr>"
                    f"<td>{escape(_safe(row.get('candidate_number','')))}</td>"
                    f"<td>{escape(_safe(row.get('parameter','')))}</td>"
                    f"<td>{escape(_safe(row.get('direction','')))}</td>"
                    f"<td>{escape(_fmt_number(row.get('step_fraction'), digits=6))}</td>"
                    f"<td>{escape(_fmt_number(row.get('candidate_total_score'), digits=6))}</td>"
                    f"<td>{escape(_safe(row.get('decision','')))}</td>"
                    f"<td>{_html_bullets(self._items_from_table_row(row))}</td>"
                    "</tr>"
                )
            rows_html = "\n".join(rows)
        return f"""
<!-- V15_EXPLAINABILITY_START -->
<section id="candidate-explainability">
<h2>Why candidates were tested</h2>
<p class="note">V15.3.1 reconstructs candidate-selection rationale from V14 decision logs, optimizer state, transaction manifests, and GPT configuration. It is read-only and does not alter calibration state.</p>
<div class="table-wrap"><table><thead><tr><th>#</th><th>Parameter</th><th>Direction</th><th>Step</th><th>Candidate TOTAL_SCORE</th><th>Decision</th><th>Why selected</th></tr></thead><tbody>
{rows_html}
</tbody></table></div>
<p>Full explainability outputs: <code>explainability/candidate_explanation.xlsx</code>, <code>explainability/explainability_summary.xlsx</code>, and per-candidate JSON files.</p>
</section>
<!-- V15_EXPLAINABILITY_END -->
""".strip()

    def _markdown_section(self, table: pd.DataFrame) -> str:
        latest = table.tail(12).copy()
        lines = [
            "<!-- V15_EXPLAINABILITY_START -->",
            "## Why candidates were tested",
            "",
            "V15.3.1 reconstructs candidate-selection rationale from V14 decision logs, optimizer state, transaction manifests, and GPT configuration. It is read-only and does not alter calibration state.",
            "",
            "| # | Parameter | Direction | Step | Candidate TOTAL_SCORE | Decision | Why selected |",
            "|---:|---|---|---:|---:|---|---|",
        ]
        if latest.empty:
            lines.append("|  | No evaluated candidates available |  |  |  |  |  |")
        else:
            for _, row in latest.iterrows():
                why = "<br>".join(f"- {_md_cell(item)}" for item in self._items_from_table_row(row))
                lines.append(
                    f"| {_md_cell(row.get('candidate_number',''))} | {_md_cell(row.get('parameter',''))} | {_md_cell(row.get('direction',''))} | {_fmt_number(row.get('step_fraction'), digits=6)} | {_fmt_number(row.get('candidate_total_score'), digits=6)} | {_md_cell(row.get('decision',''))} | {why} |"
                )
        lines.extend([
            "",
            "Full explainability outputs are written to `explainability/candidate_explanation.xlsx`, `explainability/explainability_summary.xlsx`, and per-candidate JSON files.",
            "<!-- V15_EXPLAINABILITY_END -->",
        ])
        return "\n".join(lines) + "\n"

    def _replace_marked_section(self, text: str, start: str, end: str, replacement: str, before_marker: str | None = None) -> str:
        pattern = re.compile(re.escape(start) + r"[\s\S]*?" + re.escape(end), flags=re.MULTILINE)
        if pattern.search(text):
            return pattern.sub(replacement, text)
        if before_marker and before_marker in text:
            return text.replace(before_marker, replacement + "\n" + before_marker)
        return text + "\n" + replacement + "\n"

    def _update_reports(self, table: pd.DataFrame) -> None:
        html = self.paths.output_dir / "V15_calibration_story_report.html"
        md = self.paths.output_dir / "V15_calibration_story_report.md"
        css = """
.latest-candidate-card {
    border: 1px solid #d0d7de;
    border-radius: 10px;
    padding: 16px 20px;
    margin: 18px 0 24px 0;
    background: #f6f8fa;
}
.latest-candidate-card h2 { margin-top: 0; }
.latest-candidate-card ul { margin-top: 6px; margin-bottom: 12px; }
.latest-candidate-card li { margin-bottom: 6px; }
#candidate-explainability td ul { margin: 0; padding-left: 18px; }
#candidate-explainability td li { margin-bottom: 4px; }
""".strip()
        if html.exists():
            text = html.read_text(encoding="utf-8", errors="replace")
            if ".latest-candidate-card" not in text and "</style>" in text:
                text = text.replace("</style>", css + "\n</style>")
            latest_card = self._latest_candidate_html_card(table)
            if latest_card:
                text = self._replace_marked_section(
                    text,
                    "<!-- V15_LATEST_CANDIDATE_START -->",
                    "<!-- V15_LATEST_CANDIDATE_END -->",
                    latest_card,
                    before_marker='<section id="figures">',
                )
            section = self._html_section(table)
            text = self._replace_marked_section(
                text,
                "<!-- V15_EXPLAINABILITY_START -->",
                "<!-- V15_EXPLAINABILITY_END -->",
                section,
                before_marker="<h2>Warnings</h2>",
            )
            text = text.replace("V15.3 reconstructs", "V15.3.1 reconstructs")
            html.write_text(text, encoding="utf-8")
        if md.exists():
            text = md.read_text(encoding="utf-8", errors="replace")
            latest_card = self._latest_candidate_markdown_card(table)
            if latest_card:
                text = self._replace_marked_section(
                    text,
                    "<!-- V15_LATEST_CANDIDATE_START -->",
                    "<!-- V15_LATEST_CANDIDATE_END -->",
                    latest_card,
                    before_marker="## Scientific conceptual model",
                )
            section = self._markdown_section(table)
            text = self._replace_marked_section(
                text,
                "<!-- V15_EXPLAINABILITY_START -->",
                "<!-- V15_EXPLAINABILITY_END -->",
                section,
                before_marker="## Warnings",
            )
            text = text.replace("V15.3 reconstructs", "V15.3.1 reconstructs")
            md.write_text(text, encoding="utf-8")

    def build(self) -> dict[str, Any]:
        before = self.protected_hashes()
        self.paths.explainability_dir.mkdir(parents=True, exist_ok=True)
        log = self._decision_log()
        evaluated = self._evaluated_candidates(log)
        state = self._parameter_state()
        campaign = self._campaign_state()
        interactions = self._interaction_state()
        txns = self._transaction_index()
        gpt_status = self._gpt_config_status()

        explanations: list[dict[str, Any]] = []
        for i, (_, row) in enumerate(evaluated.iterrows(), start=1):
            item = self._candidate_explanation(i, row, state, txns, campaign, interactions, gpt_status)
            explanations.append(item)
            parameter = _norm(item.get("parameter")) or "parameter"
            direction = _norm(item.get("direction")) or "direction"
            path = self.paths.explainability_dir / f"candidate_{i:04d}_{parameter}_{direction}_explanation.json"
            self._write_json(path, item)

        self._write_json(self.paths.explainability_dir / "candidate_explanations.json", explanations)
        table = self._explanations_to_table(explanations)
        summary = self._summary_table(explanations)
        xlsx_path = self.paths.explainability_dir / "candidate_explanation.xlsx"
        summary_path = self.paths.explainability_dir / "explainability_summary.xlsx"
        table.to_excel(xlsx_path, index=False)
        summary.to_excel(summary_path, index=False)
        self._update_reports(table)
        manifest = {
            "explainability_version": EXPLAINABILITY_VERSION,
            "generated": _now(),
            "candidate_count": len(explanations),
            "outputs": {
                "candidate_explanations_json": str(self.paths.explainability_dir / "candidate_explanations.json"),
                "candidate_explanation_xlsx": str(xlsx_path),
                "explainability_summary_xlsx": str(summary_path),
                "html_report": str(self.paths.output_dir / "V15_calibration_story_report.html"),
                "markdown_report": str(self.paths.output_dir / "V15_calibration_story_report.md"),
            },
            "protected_hashes_before": before,
            "protected_hashes_after": self.protected_hashes(),
            "protected_campaign_hashes_unchanged": before == self.protected_hashes(),
            "warnings": self.warnings,
        }
        self._write_json(self.paths.explainability_dir / "explainability_manifest.json", manifest)
        return manifest


def build_v15_candidate_explainability(agent_core_dir: str | Path = ".", *, output_dir: str | Path | None = None) -> dict[str, Any]:
    paths = ExplainabilityPaths.from_agent_core(agent_core_dir, output_dir)
    return V15ExplainabilityBuilder(paths).build()
