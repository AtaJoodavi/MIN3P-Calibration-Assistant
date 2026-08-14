from __future__ import annotations

"""Evidence-only residual-pattern diagnostics for V14.

The module works from existing ranking/metric tables.  It returns explicit
``insufficient_data`` / ``unsupported_metric`` states rather than inventing a
process interpretation when the HCT2 output does not contain the needed data.
"""

from pathlib import Path
from typing import Any

import pandas as pd

from modules.v14_utils import as_float, json_safe, normalise_parameter


class CalibrationDiagnostics:
    VERSION = "V14"

    def __init__(self, paths, config):
        self.paths = paths
        self.config = config

    @staticmethod
    def _metric_columns(row: pd.Series | None) -> dict[str, float]:
        if row is None:
            return {}
        metrics: dict[str, float] = {}
        for name, value in row.items():
            number = as_float(value)
            if number is None:
                continue
            label = str(name)
            lower = label.casefold()
            if any(token in lower for token in ("rmse", "bias", "mae", "nse", "peak", "breakthrough", "recession", "flow", "ph", "redox", "fe", "so4")):
                metrics[label] = number
        return metrics

    @staticmethod
    def _lookup_ranking_row(paths, run_folder: str) -> pd.Series | None:
        try:
            ranking = pd.read_excel(paths.ranking_file)
        except Exception:
            return None
        if ranking.empty or "run_folder" not in ranking.columns:
            return None
        name = Path(str(run_folder)).name
        matches = ranking[ranking["run_folder"].astype(str).str.contains(name, regex=False, na=False)]
        return None if matches.empty else matches.iloc[-1]

    @staticmethod
    def _bias_flag(name: str, value: float) -> str | None:
        label = name.casefold()
        if "bias" not in label:
            return None
        if value > 0:
            return f"systematic_positive_bias:{name}"
        if value < 0:
            return f"systematic_negative_bias:{name}"
        return None

    @staticmethod
    def _likely_groups(flags: list[str], metric_names: list[str]) -> list[str]:
        text = " ".join(flags + metric_names).casefold()
        groups: list[str] = []
        if any(token in text for token in ("flow", "early", "late", "breakthrough", "recession", "head", "water")):
            groups.append("hydraulic")
        if any(token in text for token in ("ph", "redox", "fe", "so4", "metal", "mineral")):
            groups.append("mineral_kinetics")
        if any(token in text for token in ("sorption", "feoh", "surface", "adsorp")):
            groups.append("sorption")
        if any(token in text for token in ("inflow", "outflow", "boundary", "flux")):
            groups.append("boundary_chemistry")
        return list(dict.fromkeys(groups)) or ["other"]

    def analyze(
        self,
        *,
        paths,
        config,
        baseline_score: float | None,
        candidate_score: float | None,
        baseline_run_folder: str,
        candidate_run_folder: str,
        ranking_row: pd.Series | None = None,
    ) -> dict[str, Any]:
        baseline_row = self._lookup_ranking_row(paths, baseline_run_folder)
        candidate_row = ranking_row if ranking_row is not None else self._lookup_ranking_row(paths, candidate_run_folder)
        before = self._metric_columns(baseline_row)
        after = self._metric_columns(candidate_row)

        if not after:
            return {
                "status": "insufficient_data",
                "flags": [],
                "limitations": ["No supported RMSE/bias/peak/flow columns were found in the ranking row."],
                "likely_parameter_groups": [],
                "objective_delta": (baseline_score - candidate_score) if baseline_score is not None and candidate_score is not None else None,
            }

        flags: list[str] = []
        comparisons: list[dict[str, Any]] = []
        for name, candidate_value in after.items():
            baseline_value = before.get(name)
            delta = candidate_value - baseline_value if baseline_value is not None else None
            label = name.casefold()
            if delta is not None:
                if "early" in label:
                    flags.append("early_time_error_improved" if delta < 0 else "early_time_error_worsened")
                if "late" in label:
                    flags.append("late_time_error_improved" if delta < 0 else "late_time_error_worsened")
                if "peak" in label and "time" in label:
                    flags.append("peak_timing_improved" if delta < 0 else "peak_timing_worsened")
                elif "peak" in label:
                    flags.append("peak_magnitude_improved" if delta < 0 else "peak_magnitude_worsened")
                if "breakthrough" in label or "delay" in label:
                    flags.append("breakthrough_timing_signal")
                if "recession" in label:
                    flags.append("recession_signal")
                if "flow" in label or "discharge" in label:
                    flags.append("flow_mismatch_signal")
                if "ph" in label:
                    flags.append("ph_trend_signal")
                if "redox" in label or "eh" in label or "oxygen" in label:
                    flags.append("redox_transition_signal")
            bias = self._bias_flag(name, candidate_value)
            if bias:
                flags.append(bias)
            comparisons.append({
                "metric": name,
                "baseline": baseline_value,
                "candidate": candidate_value,
                "delta": delta,
            })

        flags = list(dict.fromkeys(flags))
        return {
            "status": "available",
            "objective_delta": (baseline_score - candidate_score) if baseline_score is not None and candidate_score is not None else None,
            "flags": flags,
            "metric_comparisons": comparisons,
            "likely_parameter_groups": self._likely_groups(flags, list(after)),
            "limitations": [
                "Diagnostics are based on available aggregate ranking metrics; time-series pattern inference requires explicitly named early/late/peak/bias metrics."
            ],
            "baseline_run_folder": str(baseline_run_folder),
            "candidate_run_folder": str(candidate_run_folder),
        }

    diagnose = analyze
    evaluate = analyze
