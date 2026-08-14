from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pandas as pd

from .config import ProjectPaths
from .config_reader import ConfigReader
from .diagnostics import geochemical_diagnosis
from .io_utils import log, safe_read_text


class GPTScientificSupervisor:
    """
    GPT review layer.

    V10.1 behavior:
    - Writes a narrative supervisor report.
    - Writes structured GPT recommendations to Excel.
    - Never edits agent_config.xlsx.
    - Never changes parameters directly.

    Important:
    This class is advisory only. The deterministic V11/V10 calibration strategy
    engine owns parameter ranking and next-cycle recommendations.
    """

    def __init__(
        self,
        paths: ProjectPaths,
        config: ConfigReader,
        model: str | None = None,
    ):
        self.paths = paths
        self.config = config
        self.model = model or os.getenv("OPENAI_MODEL", "gpt-5.4-mini")

    def _tail_excel(self, path: Path, n: int = 10) -> str:
        if not path.exists():
            return f"File not found: {path}"

        try:
            df = pd.read_excel(path)
        except Exception as exc:
            return f"Could not read Excel file: {path} | {exc}"

        return df.tail(n).to_string(index=False) if not df.empty else f"File empty: {path}"

    def _read_project_info(self) -> str:
        parts = []

        for name in [
            "project_summary.md",
            "conceptual_model.md",
            "calibration_strategy.md",
            "project_notes.md",
        ]:
            p = self.paths.project_info_dir / name
            if p.exists():
                parts.append(f"\n===== {name} =====\n{safe_read_text(p, 30000)}")

        # Strategy reports are created in 05_reports, not normally in project_info.
        for strategy_name in ["strategy_update_V11.md", "calibration_strategy_V10.md"]:
            strategy_report = self.paths.reports_dir / strategy_name
            if strategy_report.exists():
                parts.append(
                    f"\n===== {strategy_name} =====\n"
                    f"{safe_read_text(strategy_report, 30000)}"
                )

        return "\n".join(parts) if parts else "No project-info markdown files found."

    def _extract_json_block(self, text: str) -> dict:
        """
        Extract one JSON object from the GPT response.

        Accepted formats:
        1) fenced JSON block:
           ```json
           {"recommendations": [...]}
           ```

        2) labelled JSON:
           JSON_RECOMMENDATIONS:
           {"recommendations": [...]}

        3) any plain JSON object in the response.
        """
        text = text.strip()

        fenced = re.search(
            r"```json\s*(\{.*?\})\s*```",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if fenced:
            return json.loads(fenced.group(1))

        labelled = re.search(
            r"JSON_RECOMMENDATIONS\s*:\s*(\{.*\})",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        if labelled:
            return json.loads(labelled.group(1))

        plain = re.search(r"(\{.*\})", text, flags=re.DOTALL)
        if plain:
            return json.loads(plain.group(1))

        raise ValueError("No JSON object found in GPT supervisor response.")

    def _write_recommendations_excel(self, response_text: str) -> Path:
        """
        Create:
            04_results/gpt_supervisor_recommendations_V11.xlsx

        This file is for inspection only in V10.1.
        The deterministic calibration engine does not use it yet.
        """
        out_file = self.paths.results_dir / "gpt_supervisor_recommendations_V11.xlsx"
        out_file.parent.mkdir(parents=True, exist_ok=True)

        default_columns = [
            "parameter",
            "confidence",
            "recommended_status",
            "process_reason",
            "risk",
            "manual_check",
        ]

        try:
            data = self._extract_json_block(response_text)
            recommendations = data.get("recommendations", [])

            if not isinstance(recommendations, list):
                recommendations = []

            df = pd.DataFrame(recommendations)

            if df.empty:
                df = pd.DataFrame(columns=default_columns)

            for col in default_columns:
                if col not in df.columns:
                    df[col] = ""

            df = df[default_columns]

            # Keep confidence numeric if possible.
            df["confidence"] = pd.to_numeric(df["confidence"], errors="coerce")

        except Exception as exc:
            df = pd.DataFrame(
                [
                    {
                        "parameter": "",
                        "confidence": "",
                        "recommended_status": "review_failed",
                        "process_reason": f"Could not parse GPT recommendations: {exc}",
                        "risk": "JSON parsing failed",
                        "manual_check": (
                            "Open gpt_supervisor_report.md and inspect "
                            "recommendations manually."
                        ),
                    }
                ]
            )

        df.to_excel(out_file, index=False)
        log(self.paths, f"GPT supervisor recommendations saved: {out_file}")
        return out_file

    def review(self) -> Path | None:
        if not os.getenv("OPENAI_API_KEY"):
            log(self.paths, "OPENAI_API_KEY not set; GPT scientific supervisor skipped.")
            return None

        from openai import OpenAI

        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

        latest = None
        if self.paths.history_file.exists():
            try:
                h = pd.read_excel(self.paths.history_file)
                latest = h.iloc[-1] if not h.empty else None
            except Exception as exc:
                log(self.paths, f"WARNING: could not read optimization history: {exc}")

        diagnosis = geochemical_diagnosis(latest)

        # ------------------------------------------------------------------
        # V11/V10 files
        # ------------------------------------------------------------------
        strategy_v11 = self._tail_excel(
            self.paths.results_dir / "calibration_strategy_V11.xlsx",
            20,
        )

        strategy_v10 = self._tail_excel(
            self.paths.results_dir / "calibration_strategy_V10.xlsx",
            20,
        )

        process_sensitivity_v10 = self._tail_excel(
            self.paths.results_dir / "process_sensitivity_summary_V10.xlsx",
            20,
        )

        sensitivity_interpretation_v10 = self._tail_excel(
            self.paths.results_dir / "sensitivity_interpretation_V10.xlsx",
            20,
        )

        parameter_importance_v10 = self._tail_excel(
            self.paths.results_dir / "parameter_importance_V10.xlsx",
            20,
        )

        suggestions = self._tail_excel(self.paths.suggestions_file, 10)

        prompt = f"""
You are the GPT scientific supervisor for a MIN3P reactive transport calibration.

Your role is review-only.
Do not claim that you changed parameters.
Do not recommend direct parameter value changes.
The deterministic V11 calibration strategy engine owns all parameter priorities, parameter ranking, and next-cycle recommendations.
You may criticize, approve, or flag the V11 strategy, but you must not present yourself as the component that modifies parameters.

In V11, you must produce:
1. A concise scientific supervisor report.
2. A machine-readable JSON object containing advisory recommendations.

Important rules:
- Your recommendations are advisory only.
- Use recommended_status values only from:
  watch, manual_review, do_not_touch, support_current_strategy.
- Confidence must be between 0 and 1.
- Recommend only parameters or processes that are scientifically justified by the evidence.
- If no additional parameters are needed, return an empty recommendations list.
- The final JSON must be valid JSON.
- The final JSON must start after this label exactly:
  JSON_RECOMMENDATIONS:

Project information:
{self._read_project_info()}

Latest optimization history:
{self._tail_excel(self.paths.history_file, 10)}

Run diagnostics:
{self._tail_excel(self.paths.run_diagnostics_file, 10)}

Deterministic geochemical diagnosis:
{diagnosis.to_string(index=False)}

V11 automatically updated strategy:
{strategy_v11}

V10.9 baseline deterministic calibration strategy:
{strategy_v10}

V10 process sensitivity summary:
{process_sensitivity_v10}

V10 sensitivity interpretation:
{sensitivity_interpretation_v10}

V10 parameter importance:
{parameter_importance_v10}

Previous deterministic parameter suggestions, if available:
{suggestions}

Legacy sensitivity coefficients, if available:
{self._tail_excel(self.paths.sensitivity_coefficients_file, 20)}

Write the report with these sections:
1. Numerical trustworthiness.
2. Hydrogeochemical interpretation.
3. Whether the V11/V10 calibration strategy is scientifically reasonable.
4. Whether selected high-priority parameters are justified by process sensitivity and parameter importance.
5. Missing process representation or mineral controls to check.
6. Risk of overfitting or parameter non-identifiability.
7. Safe next calibration action.
8. Warnings for manual review.

After the report, include one valid JSON object using this exact schema:

JSON_RECOMMENDATIONS:
{{
  "recommendations": [
    {{
      "parameter": "keff_ferrihydrite",
      "confidence": 0.80,
      "recommended_status": "watch",
      "process_reason": "Fe mismatch may indicate missing or weak Fe precipitation or sorption control.",
      "risk": "May be non-identifiable if Fe observations are weak.",
      "manual_check": "Check Fe, pH, and ferrihydrite mineral trend before activating."
    }}
  ]
}}

If no additional recommendation is needed, use:

JSON_RECOMMENDATIONS:
{{
  "recommendations": []
}}
"""

        response = client.responses.create(
            model=self.model,
            input=prompt,
            max_output_tokens=3500,
        )

        self.paths.gpt_supervisor_report_file.parent.mkdir(parents=True, exist_ok=True)
        self.paths.gpt_supervisor_report_file.write_text(
            response.output_text,
            encoding="utf-8",
        )

        log(
            self.paths,
            f"GPT supervisor report saved: {self.paths.gpt_supervisor_report_file}",
        )

        self._write_recommendations_excel(response.output_text)

        return self.paths.gpt_supervisor_report_file
