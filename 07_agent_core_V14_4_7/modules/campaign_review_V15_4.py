from __future__ import annotations

"""V15.4 read-only calibration-campaign analysis and GPT scientific review.

The module deliberately separates deterministic evidence extraction from GPT
interpretation.  Python calculates every count, score, bias, and diagnostic.
GPT receives only the compact JSON evidence and is not allowed to recalculate
or invent campaign facts.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
import json
import math
import os
import re

import numpy as np
import pandas as pd

from modules.v14_utils import as_bool, json_safe, read_simple_yaml


CAMPAIGN_REVIEW_VERSION = "V15.4"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _safe(value: Any) -> str:
    if value is None:
        return ""
    try:
        if pd.isna(value):
            return ""
    except Exception:
        pass
    return str(value).strip()


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", _safe(value).casefold()).strip("_")


def _float(value: Any) -> float | None:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except Exception:
        return None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not pd.isna(value):
        return bool(value)
    return _norm(value) in {"1", "true", "yes", "y", "accepted", "accept", "success", "verified"}


def _read_excel(path: Path) -> pd.DataFrame:
    try:
        return pd.read_excel(path) if path.exists() else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def _column(frame: pd.DataFrame, candidates: Iterable[str]) -> str | None:
    by_norm = {_norm(col): str(col) for col in frame.columns}
    for candidate in candidates:
        hit = by_norm.get(_norm(candidate))
        if hit is not None:
            return hit
    return None


def _finite_series(frame: pd.DataFrame, column: str | None) -> pd.Series:
    if frame.empty or column is None or column not in frame.columns:
        return pd.Series(dtype=float)
    values = pd.to_numeric(frame[column], errors="coerce")
    return values[np.isfinite(values)]


def _json_dump(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(payload), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _excel_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(json_safe(value), ensure_ascii=False)
    return json_safe(value)


def _flatten(prefix: str, value: Any, rows: list[dict[str, Any]]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _flatten(f"{prefix}.{key}" if prefix else str(key), child, rows)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _flatten(f"{prefix}[{index}]", child, rows)
    else:
        rows.append({"field": prefix, "value": _excel_value(value)})


@dataclass(frozen=True)
class CampaignReviewPaths:
    agent_core_dir: Path
    project_dir: Path
    results_dir: Path
    runs_dir: Path
    output_dir: Path
    config_file: Path

    @classmethod
    def from_agent_core(
        cls,
        agent_core_dir: str | Path,
        *,
        output_dir: str | Path | None = None,
        config_file: str | Path | None = None,
    ) -> "CampaignReviewPaths":
        core = Path(agent_core_dir).resolve()
        project: Path | None = None
        for candidate in [core.parent, *core.parents]:
            if (candidate / "01_input").is_dir() or (candidate / "04_results").is_dir():
                project = candidate
                break
        if project is None:
            project = core.parent
        out = Path(output_dir).resolve() if output_dir else project / "05_reports" / "V15_scientific_report"
        config = Path(config_file).resolve() if config_file else core / "config" / "gpt_campaign_review_V15_4.yaml"
        return cls(
            agent_core_dir=core,
            project_dir=project,
            results_dir=project / "04_results",
            runs_dir=project / "03_runs",
            output_dir=out,
            config_file=config,
        )


class CampaignAnalysisV15_4:
    """Build a compact, reproducible campaign evidence package."""

    HISTORY_CANDIDATES = (
        "optimization_history.xlsx",
        "run_ranking.xlsx",
        "optimization_history_V14.xlsx",
    )
    DECISION_CANDIDATES = (
        "calibration_decision_log.xlsx",
        "v14_candidate_decisions.xlsx",
    )

    def __init__(self, paths: CampaignReviewPaths):
        self.paths = paths
        self.warnings: list[str] = []

    def _first_table(self, names: Iterable[str]) -> tuple[pd.DataFrame, Path | None]:
        for name in names:
            path = self.paths.results_dir / name
            frame = _read_excel(path)
            if not frame.empty:
                return frame, path
        return pd.DataFrame(), None

    def _history(self) -> tuple[pd.DataFrame, Path | None]:
        return self._first_table(self.HISTORY_CANDIDATES)

    def _decisions(self) -> tuple[pd.DataFrame, Path | None]:
        return self._first_table(self.DECISION_CANDIDATES)

    @staticmethod
    def _evaluated_decisions(raw: pd.DataFrame) -> pd.DataFrame:
        if raw.empty:
            return raw.copy()
        frame = raw.copy()
        parameter_col = _column(frame, ["parameter"])
        phase_col = _column(frame, ["phase"])
        decision_col = _column(frame, ["decision"])
        candidate_score_col = _column(frame, ["candidate_objective", "candidate_TOTAL_SCORE"])
        mask = pd.Series(True, index=frame.index)
        if parameter_col:
            mask &= frame[parameter_col].map(_safe).ne("")
        if phase_col:
            phase_mask = frame[phase_col].map(_norm).isin({"candidate_evaluated", "evaluated", "candidate_decision"})
            if phase_mask.any():
                mask &= phase_mask
        elif decision_col:
            mask &= frame[decision_col].map(_safe).ne("")
        elif candidate_score_col:
            mask &= pd.to_numeric(frame[candidate_score_col], errors="coerce").notna()
        return frame.loc[mask].copy().reset_index(drop=True)

    @staticmethod
    def _unique_runs(history: pd.DataFrame) -> pd.DataFrame:
        if history.empty:
            return history.copy()
        run_col = _column(history, ["run_folder"])
        if run_col is None:
            return history.copy().reset_index(drop=True)
        frame = history[history[run_col].map(_safe).ne("")].copy()
        if frame.empty:
            return history.copy().reset_index(drop=True)
        return frame.drop_duplicates(subset=[run_col], keep="last").reset_index(drop=True)

    def _run_summary(self, history: pd.DataFrame) -> dict[str, Any]:
        frame = self._unique_runs(history)
        status_col = _column(frame, ["run_status", "qc_run_status"])
        failed_steps_col = _column(frame, ["failed_time_steps"])
        restoration_statuses = frame[status_col].map(_norm) if status_col else pd.Series("", index=frame.index)
        failed_steps = pd.to_numeric(frame[failed_steps_col], errors="coerce").fillna(0) if failed_steps_col else pd.Series(0, index=frame.index)

        retries_mask = restoration_statuses.str.contains("retr") | failed_steps.gt(0)
        success_mask = restoration_statuses.isin({"success", "successful", "normal", "completed"}) & ~retries_mask
        success_retry_mask = restoration_statuses.str.contains("success") & retries_mask
        if status_col is None:
            success_mask = pd.Series(True, index=frame.index) & ~retries_mask
            success_retry_mask = retries_mask
        failed_mask = ~(success_mask | success_retry_mask)

        warning_col = _column(frame, ["warnings", "runtime_warning", "qc_runtime_warning"])
        warning_counts: dict[str, int] = {}
        if warning_col:
            for item in frame[warning_col].map(_safe):
                for token in re.split(r"[;,|]+", item):
                    token = token.strip()
                    if token:
                        warning_counts[token] = warning_counts.get(token, 0) + 1

        charge_col = _column(frame, ["initial_charge_balance_error_percent", "charge_balance_error_percent"])
        charge = _finite_series(frame, charge_col)
        charge_warning_count = 0
        if warning_col:
            charge_warning_count = int(frame[warning_col].map(_norm).str.contains("charge_balance").sum())
        if charge_warning_count == 0 and not charge.empty:
            charge_warning_count = int((charge.abs() > 5.0).sum())

        failed_fraction_col = _column(frame, ["failed_step_fraction", "qc_failed_step_fraction"])
        failed_fraction = _finite_series(frame, failed_fraction_col)
        health_col = _column(frame, ["run_health_score"])
        health = _finite_series(frame, health_col)

        return {
            "total_runs": int(len(frame)),
            "successful_runs": int(success_mask.sum()),
            "successful_with_retries": int(success_retry_mask.sum()),
            "failed_runs": int(failed_mask.sum()),
            "runs_with_retried_or_failed_timesteps": int(retries_mask.sum()),
            "total_failed_timesteps": int(failed_steps.sum()),
            "maximum_failed_step_fraction": float(failed_fraction.max()) if not failed_fraction.empty else None,
            "mean_run_health_score": float(health.mean()) if not health.empty else None,
            "warning_counts": warning_counts,
            "charge_balance_warning_count": charge_warning_count,
            "initial_charge_balance_error_percent": {
                "first": float(charge.iloc[0]) if not charge.empty else None,
                "median": float(charge.median()) if not charge.empty else None,
                "maximum_absolute": float(charge.abs().max()) if not charge.empty else None,
            },
        }

    def _decision_summary(self, decisions: pd.DataFrame) -> dict[str, Any]:
        if decisions.empty:
            return {
                "candidate_count": 0,
                "accepted_candidates": 0,
                "rejected_candidates": 0,
                "unresolved_candidates": 0,
                "restoration_verified_count": 0,
                "restoration_verified_fraction": None,
                "rejection_reasons": {},
                "tradeoff_classes": {},
                "species_tradeoff_count": 0,
            }
        accepted_col = _column(decisions, ["accepted"])
        decision_col = _column(decisions, ["decision"])
        reason_col = _column(decisions, ["rejection_reason"])
        restore_col = _column(decisions, ["restoration_verified"])
        tradeoff_col = _column(decisions, ["tradeoff_class"])

        accepted = decisions[accepted_col].map(_bool) if accepted_col else decisions[decision_col].map(_norm).eq("accept")
        rejected = decisions[decision_col].map(_norm).isin({"reject", "rejected"}) if decision_col else ~accepted
        unresolved = ~(accepted | rejected)
        restoration = decisions[restore_col].map(_bool) if restore_col else pd.Series(False, index=decisions.index)
        reasons: dict[str, int] = {}
        if reason_col:
            for reason in decisions.loc[rejected, reason_col].map(_safe):
                reason = reason or "not_recorded"
                reasons[reason] = reasons.get(reason, 0) + 1
        tradeoff_classes: dict[str, int] = {}
        if tradeoff_col:
            for label in decisions[tradeoff_col].map(_safe):
                label = label or "not_recorded"
                tradeoff_classes[label] = tradeoff_classes.get(label, 0) + 1
        return {
            "candidate_count": int(len(decisions)),
            "accepted_candidates": int(accepted.sum()),
            "rejected_candidates": int(rejected.sum()),
            "unresolved_candidates": int(unresolved.sum()),
            "restoration_verified_count": int(restoration.sum()),
            "restoration_verified_fraction": float(restoration.mean()) if len(restoration) else None,
            "rejection_reasons": reasons,
            "tradeoff_classes": tradeoff_classes,
            "species_tradeoff_count": int(sum(count for label, count in tradeoff_classes.items() if "species_tradeoff" in _norm(label))),
        }

    def _objective_summary(self, history: pd.DataFrame, decisions: pd.DataFrame) -> dict[str, Any]:
        history_score_col = _column(history, ["TOTAL_SCORE", "objective_total", "qc_objective_score"])
        history_scores = _finite_series(history, history_score_col)
        baseline_col = _column(decisions, ["baseline_objective", "baseline_TOTAL_SCORE"])
        candidate_col = _column(decisions, ["candidate_objective", "candidate_TOTAL_SCORE", "effective_objective"])
        current_best_col = _column(decisions, ["current_best_objective", "current_best_score"])
        accepted_col = _column(decisions, ["accepted"])
        decision_col = _column(decisions, ["decision"])

        baselines = _finite_series(decisions, baseline_col)
        candidate_scores = _finite_series(decisions, candidate_col)
        initial = float(baselines.iloc[0]) if not baselines.empty else (float(history_scores.iloc[0]) if not history_scores.empty else None)

        accepted_mask = pd.Series(False, index=decisions.index)
        if not decisions.empty:
            if accepted_col:
                accepted_mask = decisions[accepted_col].map(_bool)
            elif decision_col:
                accepted_mask = decisions[decision_col].map(_norm).isin({"accept", "accepted"})
        accepted_scores = pd.Series(dtype=float)
        if candidate_col and not decisions.empty:
            accepted_scores = pd.to_numeric(decisions.loc[accepted_mask, candidate_col], errors="coerce")
            accepted_scores = accepted_scores[np.isfinite(accepted_scores)]

        current_best_values = _finite_series(decisions, current_best_col)
        best_candidates: list[float] = []
        if initial is not None:
            best_candidates.append(initial)
        best_candidates.extend(float(x) for x in accepted_scores.tolist())
        if not current_best_values.empty:
            best_candidates.append(float(current_best_values.iloc[-1]))
        best = min(best_candidates) if best_candidates else (float(history_scores.min()) if not history_scores.empty else None)

        stats = {
            "minimum": float(candidate_scores.min()) if not candidate_scores.empty else None,
            "median": float(candidate_scores.median()) if not candidate_scores.empty else None,
            "mean": float(candidate_scores.mean()) if not candidate_scores.empty else None,
            "maximum": float(candidate_scores.max()) if not candidate_scores.empty else None,
            "standard_deviation": float(candidate_scores.std(ddof=0)) if len(candidate_scores) > 1 else 0.0 if len(candidate_scores) == 1 else None,
        }
        absolute = (initial - best) if initial is not None and best is not None else None
        percent = (100.0 * absolute / abs(initial)) if absolute is not None and initial not in {None, 0.0} else None
        mode_col = _column(history, ["TOTAL_SCORE_OBJECTIVE_MODE"])
        included_col = _column(history, ["TOTAL_SCORE_INCLUDED_METRICS"])
        weights: dict[str, float] = {}
        if not history.empty:
            row = history.iloc[0]
            for col in history.columns:
                if _norm(col).startswith("weighted_rmse_"):
                    suffix = str(col)[len("WEIGHTED_RMSE_"):]
                    norm_col = next((c for c in history.columns if _norm(c) == _norm(f"NORM_RMSE_{suffix}")), None)
                    weighted = _float(row.get(col))
                    normalized = _float(row.get(norm_col)) if norm_col else None
                    if weighted is not None and normalized not in {None, 0.0}:
                        weights[suffix] = weighted / normalized
        return {
            "objective_mode": _safe(history.iloc[0].get(mode_col)) if mode_col and not history.empty else None,
            "included_metrics": [x.strip() for x in _safe(history.iloc[0].get(included_col)).split(",") if x.strip()] if included_col and not history.empty else [],
            "metric_weights": weights,
            "initial_score": initial,
            "best_score": best,
            "absolute_improvement": absolute,
            "percent_improvement": percent,
            "candidate_score_statistics": stats,
        }

    def _closest_candidates(self, decisions: pd.DataFrame, limit: int = 10) -> list[dict[str, Any]]:
        if decisions.empty:
            return []
        parameter_col = _column(decisions, ["parameter"])
        group_col = _column(decisions, ["group"])
        direction_col = _column(decisions, ["direction"])
        step_col = _column(decisions, ["step_fraction"])
        old_col = _column(decisions, ["old_value"])
        new_col = _column(decisions, ["new_value"])
        baseline_col = _column(decisions, ["baseline_objective", "baseline_TOTAL_SCORE"])
        candidate_col = _column(decisions, ["candidate_objective", "candidate_TOTAL_SCORE", "effective_objective"])
        accepted_col = _column(decisions, ["accepted"])
        decision_col = _column(decisions, ["decision"])
        type_col = _column(decisions, ["candidate_type"])
        if candidate_col is None:
            return []
        frame = decisions.copy()
        frame["__candidate"] = pd.to_numeric(frame[candidate_col], errors="coerce")
        frame["__baseline"] = pd.to_numeric(frame[baseline_col], errors="coerce") if baseline_col else np.nan
        frame = frame[np.isfinite(frame["__candidate"])].sort_values("__candidate", ascending=True).head(limit)
        rows: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            accepted = _bool(row.get(accepted_col)) if accepted_col else _norm(row.get(decision_col)) in {"accept", "accepted"}
            candidate = float(row["__candidate"])
            baseline = _float(row["__baseline"])
            rows.append({
                "parameter": _safe(row.get(parameter_col)) if parameter_col else "",
                "group": _safe(row.get(group_col)) if group_col else "",
                "candidate_type": _safe(row.get(type_col)) if type_col else "single_parameter",
                "direction": _safe(row.get(direction_col)) if direction_col else "",
                "step_fraction": _float(row.get(step_col)) if step_col else None,
                "old_value": _float(row.get(old_col)) if old_col else None,
                "new_value": _float(row.get(new_col)) if new_col else None,
                "baseline_score": baseline,
                "candidate_score": candidate,
                "candidate_minus_baseline": candidate - baseline if baseline is not None else None,
                "accepted": accepted,
            })
        return rows

    @staticmethod
    def _species_name(metric_column: str, prefix: str) -> str:
        return metric_column[len(prefix):] if metric_column.startswith(prefix) else metric_column

    def _species_summary(self, history: pd.DataFrame, objective: dict[str, Any]) -> list[dict[str, Any]]:
        if history.empty:
            return []
        score_col = _column(history, ["TOTAL_SCORE", "objective_total", "qc_objective_score"])
        baseline_row = history.iloc[0]
        best_row = baseline_row
        best_score = _float(objective.get("best_score"))
        if score_col and best_score is not None:
            scores = pd.to_numeric(history[score_col], errors="coerce")
            finite = scores[np.isfinite(scores)]
            if not finite.empty:
                idx = (finite - best_score).abs().idxmin()
                best_row = history.loc[idx]

        rmse_cols = [str(c) for c in history.columns if str(c).startswith("RMSE_")]
        weights = objective.get("metric_weights", {}) if isinstance(objective.get("metric_weights"), dict) else {}
        rows: list[dict[str, Any]] = []
        for rmse_col in rmse_cols:
            species = self._species_name(rmse_col, "RMSE_")
            mae_col = next((c for c in history.columns if str(c) == f"MAE_{species}"), None)
            bias_col = next((c for c in history.columns if str(c) == f"Bias_{species}"), None)
            obs_col = next((c for c in history.columns if str(c) == f"Mean_obs_{species}"), None)
            model_col = next((c for c in history.columns if str(c) == f"Mean_model_{species}"), None)
            rmse = _float(best_row.get(rmse_col))
            mae = _float(best_row.get(mae_col)) if mae_col else None
            bias = _float(best_row.get(bias_col)) if bias_col else None
            mean_obs = _float(best_row.get(obs_col)) if obs_col else None
            mean_model = _float(best_row.get(model_col)) if model_col else None
            if bias is None and mean_obs is not None and mean_model is not None:
                bias = mean_model - mean_obs
            relative_bias = None
            if bias is not None and mean_obs not in {None, 0.0}:
                relative_bias = bias / abs(mean_obs)
            if bias is None:
                status = "not_available"
            elif relative_bias is not None and abs(relative_bias) <= 0.05:
                status = "approximately_unbiased"
            elif bias < 0:
                status = "underpredicted"
            elif bias > 0:
                status = "overpredicted"
            else:
                status = "approximately_unbiased"
            weight = _float(weights.get(species))
            rows.append({
                "species": species,
                "weight": weight,
                "rmse": rmse,
                "mae": mae,
                "bias_model_minus_observed": bias,
                "relative_bias_fraction": relative_bias,
                "mean_observed": mean_obs,
                "mean_modelled": mean_model,
                "prediction_status": status,
            })
        return rows

    def _search_summary(self, decisions: pd.DataFrame, objective: dict[str, Any], decision_summary: dict[str, Any]) -> dict[str, Any]:
        parameter_col = _column(decisions, ["parameter"])
        group_col = _column(decisions, ["group"])
        direction_col = _column(decisions, ["direction"])
        step_col = _column(decisions, ["step_fraction"])
        type_col = _column(decisions, ["candidate_type"])
        parameters = sorted({x for x in decisions[parameter_col].map(_safe) if x}) if parameter_col else []
        groups = sorted({x for x in decisions[group_col].map(_safe) if x}) if group_col else []
        directions = sorted({x for x in decisions[direction_col].map(_safe) if x}) if direction_col else []
        steps = sorted({_float(x) for x in decisions[step_col].tolist() if _float(x) is not None}, reverse=True) if step_col else []
        types = decisions[type_col].map(_norm) if type_col else pd.Series("single_parameter", index=decisions.index)
        pair_mask = types.str.contains("pair|interaction")
        def counts(column_name: str | None, *, numeric: bool = False) -> dict[str, int]:
            if not column_name:
                return {}
            result: dict[str, int] = {}
            for raw in decisions[column_name].tolist():
                if numeric:
                    parsed = _float(raw)
                    label = f"{parsed:g}" if parsed is not None else "not_recorded"
                else:
                    label = _safe(raw) or "not_recorded"
                result[label] = result.get(label, 0) + 1
            return result
        candidate_stats = objective.get("candidate_score_statistics", {})
        initial = _float(objective.get("initial_score"))
        minimum = _float(candidate_stats.get("minimum")) if isinstance(candidate_stats, dict) else None
        accepted = int(decision_summary.get("accepted_candidates", 0) or 0)
        candidate_count = int(decision_summary.get("candidate_count", 0) or 0)
        tolerance = max(1e-8, abs(initial or 0.0) * 1e-6)
        no_candidate_better = initial is not None and minimum is not None and minimum >= initial - tolerance
        local_stagnation = candidate_count >= 10 and accepted == 0 and no_candidate_better
        return {
            "tested_parameters": parameters,
            "tested_parameter_count": len(parameters),
            "tested_groups": groups,
            "tested_directions": directions,
            "tested_step_fractions": steps,
            "step_fraction_counts": counts(step_col, numeric=True),
            "parameter_trial_counts": counts(parameter_col),
            "group_trial_counts": counts(group_col),
            "direction_trial_counts": counts(direction_col),
            "candidate_type_counts": counts(type_col),
            "single_parameter_candidates": int((~pair_mask).sum()),
            "interaction_or_pair_candidates": int(pair_mask.sum()),
            "local_stagnation_detected": local_stagnation,
            "no_candidate_better_than_initial_within_tolerance": no_candidate_better,
            "same_configuration_additional_runs_recommended": not local_stagnation,
        }

    def build(self) -> dict[str, Any]:
        history, history_path = self._history()
        raw_decisions, decision_path = self._decisions()
        decisions = self._evaluated_decisions(raw_decisions)
        if history.empty:
            self.warnings.append("No optimization history was found; run-level and species summaries are incomplete.")
        if decisions.empty:
            self.warnings.append("No evaluated candidate decisions were found; optimizer-search conclusions are incomplete.")

        run_summary = self._run_summary(history)
        decision_summary = self._decision_summary(decisions)
        objective_summary = self._objective_summary(history, decisions)
        species = self._species_summary(history, objective_summary)
        search = self._search_summary(decisions, objective_summary, decision_summary)
        closest = self._closest_candidates(decisions)

        accepted = decision_summary["accepted_candidates"]
        campaign_status = "failed"
        if run_summary["total_runs"] > 0 and run_summary["failed_runs"] == 0:
            campaign_status = "improved" if accepted > 0 and (_float(objective_summary.get("absolute_improvement")) or 0) > 0 else "stagnant"
        elif run_summary["total_runs"] > 0:
            campaign_status = "completed_with_failures"

        systematic_under = [row["species"] for row in species if row["prediction_status"] == "underpredicted"]
        systematic_over = [row["species"] for row in species if row["prediction_status"] == "overpredicted"]
        charge_max = _float(run_summary["initial_charge_balance_error_percent"].get("maximum_absolute"))
        numerical_flags: list[str] = []
        if charge_max is not None and charge_max > 5.0:
            numerical_flags.append("initial_charge_balance_error_above_5_percent")
        if run_summary["runs_with_retried_or_failed_timesteps"]:
            numerical_flags.append("runs_with_retried_or_failed_timesteps")
        if run_summary["failed_runs"]:
            numerical_flags.append("failed_runs_present")

        baseline_runs = max(int(run_summary.get("total_runs", 0) or 0) - int(decision_summary.get("candidate_count", 0) or 0), 0)
        payload = {
            "schema_version": "1.0",
            "reviewer_version": CAMPAIGN_REVIEW_VERSION,
            "generated_at": _now(),
            "project": {
                "name": self.paths.project_dir.name,
                "project_dir": str(self.paths.project_dir),
                "agent_core_dir": str(self.paths.agent_core_dir),
            },
            "sources": {
                "optimization_history": str(history_path) if history_path else None,
                "decision_log": str(decision_path) if decision_path else None,
                "objective_reference": str(self.paths.results_dir / "objective_reference_V13.xlsx") if (self.paths.results_dir / "objective_reference_V13.xlsx").exists() else None,
            },
            "campaign_summary": {
                "status": campaign_status,
                "baseline_runs": baseline_runs,
                "candidate_runs": int(decision_summary.get("candidate_count", 0) or 0),
                **run_summary,
                **decision_summary,
            },
            "objective_summary": objective_summary,
            "closest_candidates": closest,
            "species_summary": species,
            "bias_summary": {
                "systematically_underpredicted": systematic_under,
                "systematically_overpredicted": systematic_over,
            },
            "search_summary": search,
            "numerical_summary": {
                "flags": numerical_flags,
                "charge_balance_requires_review": bool(charge_max is not None and charge_max > 5.0),
                "convergence_retry_fraction": (
                    run_summary["runs_with_retried_or_failed_timesteps"] / run_summary["total_runs"]
                    if run_summary["total_runs"] else None
                ),
            },
            "evidence_rules": {
                "lower_objective_is_better": True,
                "gpt_may_not_modify_numbers": True,
                "gpt_may_not_modify_calibration_state": True,
                "optimizer_failure_not_implied_by_zero_acceptances": True,
            },
            "warnings": self.warnings.copy(),
        }
        return json_safe(payload)

    def write(self, payload: dict[str, Any] | None = None) -> dict[str, str]:
        payload = payload or self.build()
        self.paths.output_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.paths.output_dir / "campaign_analysis_V15_4.json"
        xlsx_path = self.paths.output_dir / "campaign_analysis_V15_4.xlsx"
        _json_dump(json_path, payload)
        flat_rows: list[dict[str, Any]] = []
        _flatten("", payload, flat_rows)
        with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
            pd.DataFrame(flat_rows).to_excel(writer, sheet_name="analysis_flat", index=False)
            pd.DataFrame(payload.get("closest_candidates", [])).map(_excel_value).to_excel(writer, sheet_name="closest_candidates", index=False)
            pd.DataFrame(payload.get("species_summary", [])).map(_excel_value).to_excel(writer, sheet_name="species_summary", index=False)
        return {"campaign_analysis_json": str(json_path), "campaign_analysis_xlsx": str(xlsx_path)}


# ----------------------------- GPT review -----------------------------


def campaign_review_schema() -> dict[str, Any]:
    finding = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "finding": {"type": "string"},
            "importance": {"type": "string", "enum": ["high", "medium", "low"]},
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["finding", "importance", "evidence"],
    }
    species_item = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "species": {"type": "string"},
            "status": {"type": "string"},
            "interpretation": {"type": "string"},
            "possible_controlling_processes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["species", "status", "interpretation", "possible_controlling_processes"],
    }
    action = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "priority": {"type": "integer", "minimum": 1},
            "action": {"type": "string"},
            "reason": {"type": "string"},
            "expected_benefit": {"type": "string"},
        },
        "required": ["priority", "action", "reason", "expected_benefit"],
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "executive_summary": {"type": "string"},
            "campaign_assessment": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "status": {"type": "string", "enum": ["improved", "stagnant", "partially_improved", "failed", "incomplete"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "summary": {"type": "string"},
                },
                "required": ["status", "confidence", "summary"],
            },
            "optimizer_assessment": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "implementation_status": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                    "possible_software_issues": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["implementation_status", "evidence", "possible_software_issues"],
            },
            "scientific_findings": {"type": "array", "items": finding},
            "species_assessment": {"type": "array", "items": species_item},
            "stagnation_analysis": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "local_minimum_likely": {"type": "boolean"},
                    "reasoning": {"type": "array", "items": {"type": "string"}},
                    "more_runs_same_configuration_recommended": {"type": "boolean"},
                },
                "required": ["local_minimum_likely", "reasoning", "more_runs_same_configuration_recommended"],
            },
            "numerical_health": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "status": {"type": "string"},
                    "important_warnings": {"type": "array", "items": {"type": "string"}},
                    "interpretation": {"type": "string"},
                },
                "required": ["status", "important_warnings", "interpretation"],
            },
            "recommended_actions": {"type": "array", "items": action},
            "paper_ready_conclusion": {"type": "string"},
            "limitations": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "executive_summary", "campaign_assessment", "optimizer_assessment",
            "scientific_findings", "species_assessment", "stagnation_analysis",
            "numerical_health", "recommended_actions", "paper_ready_conclusion", "limitations",
        ],
    }


def validate_campaign_review(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Campaign review must be a JSON object.")
    required = set(campaign_review_schema()["required"])
    missing = sorted(required.difference(payload))
    extra = sorted(set(payload).difference(required))
    if missing:
        raise ValueError(f"Campaign review missing fields: {', '.join(missing)}")
    if extra:
        raise ValueError(f"Campaign review has unexpected fields: {', '.join(extra)}")
    confidence = payload.get("campaign_assessment", {}).get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
        raise ValueError("campaign_assessment.confidence must be between 0 and 1.")
    return payload


def campaign_review_system_prompt() -> str:
    return """You are a scientific reviewer specializing in MIN3P reactive-transport modelling, humidity-cell tests, hydrogeochemistry, calibration, and multi-objective optimization.

You receive a deterministic JSON summary calculated by Python. Treat it as the only source of campaign facts.

Tasks:
1. Evaluate whether the calibration campaign improved the model.
2. Distinguish optimizer implementation performance from model-conceptualization and search-space problems.
3. Identify systematic model biases, species trade-offs, and important numerical warnings.
4. Assess whether the evidence suggests local stagnation or insufficient parameter-space exploration.
5. Recommend prioritized, scientifically defensible next actions.
6. Write a concise publication-quality conclusion.

Rules:
- Never invent or recalculate runs, parameters, scores, statistics, warnings, or processes.
- Clearly separate direct evidence from scientific interpretation.
- Do not call the optimizer a failure merely because no candidate was accepted.
- Use rollback/restoration evidence when assessing optimizer behavior.
- Do not recommend more runs with unchanged settings when repeated stagnation is documented.
- Mention material charge-balance, convergence, or failed-timestep warnings.
- Treat possible geochemical mechanisms as hypotheses unless directly demonstrated.
- Return valid JSON only and exactly match the supplied schema."""


def campaign_review_user_prompt(analysis: dict[str, Any]) -> str:
    return "Review this MIN3P humidity-cell calibration campaign and produce the requested structured scientific assessment.\n\n" + json.dumps(analysis, ensure_ascii=False, indent=2)


class GPTCampaignReviewerV15_4:
    def __init__(self, paths: CampaignReviewPaths):
        self.paths = paths
        raw = read_simple_yaml(paths.config_file)
        self.settings = {
            "enabled": bool(as_bool(raw.get("enabled"), False)),
            "model": str(raw.get("model") or "gpt-5.5"),
            "reasoning_effort": str(raw.get("reasoning_effort") or "medium"),
            "max_output_tokens": max(int(raw.get("max_output_tokens") or 5000), 1200),
            "verbosity": str(raw.get("verbosity") or "medium"),
        }

    def review_with_gpt(self, analysis: dict[str, Any]) -> dict[str, Any]:
        if not self.settings["enabled"]:
            raise RuntimeError(f"GPT campaign review is disabled in {self.paths.config_file.name}.")
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set; no GPT campaign-review call was made.")
        try:
            from openai import OpenAI
        except Exception as exc:
            raise RuntimeError("The OpenAI Python package is required for GPT campaign review.") from exc
        client = OpenAI()
        response = client.responses.create(
            model=self.settings["model"],
            instructions=campaign_review_system_prompt(),
            input=campaign_review_user_prompt(analysis),
            store=False,
            reasoning={"effort": self.settings["reasoning_effort"]},
            max_output_tokens=self.settings["max_output_tokens"],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "min3p_v15_4_campaign_review",
                    "strict": True,
                    "schema": campaign_review_schema(),
                },
                "verbosity": self.settings["verbosity"],
            },
        )
        raw = getattr(response, "output_text", "")
        if not raw:
            raise RuntimeError("Responses API returned no output_text for campaign review.")
        return validate_campaign_review(json.loads(raw))

    @staticmethod
    def deterministic_review(analysis: dict[str, Any]) -> dict[str, Any]:
        campaign = analysis.get("campaign_summary", {})
        objective = analysis.get("objective_summary", {})
        search = analysis.get("search_summary", {})
        numerical = analysis.get("numerical_summary", {})
        species = analysis.get("species_summary", [])
        total = int(campaign.get("total_runs", 0) or 0)
        candidates = int(campaign.get("candidate_count", 0) or 0)
        accepted = int(campaign.get("accepted_candidates", 0) or 0)
        rejected = int(campaign.get("rejected_candidates", 0) or 0)
        failed = int(campaign.get("failed_runs", 0) or 0)
        initial = _float(objective.get("initial_score"))
        best = _float(objective.get("best_score"))
        improvement = _float(objective.get("absolute_improvement")) or 0.0
        restore_fraction = _float(campaign.get("restoration_verified_fraction"))
        stagnant = bool(search.get("local_stagnation_detected"))
        pair_count = int(search.get("interaction_or_pair_candidates", 0) or 0)

        if total == 0:
            status = "incomplete"
        elif improvement > 0 and accepted > 0:
            status = "improved"
        elif stagnant:
            status = "stagnant"
        else:
            status = "partially_improved" if accepted > 0 else "incomplete"

        optimizer_evidence = [
            f"{candidates} evaluated candidates: {accepted} accepted and {rejected} rejected.",
        ]
        if restore_fraction is not None:
            optimizer_evidence.append(f"Rollback restoration was verified for {restore_fraction:.1%} of evaluated candidates.")
        if search.get("tested_directions"):
            optimizer_evidence.append("Recorded directions: " + ", ".join(map(str, search["tested_directions"])) + ".")
        if search.get("tested_step_fractions"):
            optimizer_evidence.append("Recorded step fractions: " + ", ".join(f"{x:g}" for x in search["tested_step_fractions"]) + ".")

        findings: list[dict[str, Any]] = []
        if stagnant:
            findings.append({
                "finding": "The tested one-at-a-time parameter neighborhood did not contain an accepted improvement.",
                "importance": "high",
                "evidence": [
                    f"Initial objective: {initial!r}; final/best objective: {best!r}.",
                    f"Accepted candidates: {accepted} of {candidates}.",
                ],
            })
        species_tradeoff_count = int(campaign.get("species_tradeoff_count", 0) or 0)
        if species_tradeoff_count > 0:
            findings.append({
                "finding": "Species trade-offs repeatedly prevented improvement of the composite objective.",
                "importance": "high",
                "evidence": [f"The decision log classified {species_tradeoff_count} evaluated candidates as species trade-offs."],
            })
        if pair_count == 0 and candidates > 0:
            findings.append({
                "finding": "The campaign evidence contains no evaluated interaction-pair candidates.",
                "importance": "high",
                "evidence": [f"Single-parameter candidates: {search.get('single_parameter_candidates', 0)}; pair candidates: 0."],
            })
        under = analysis.get("bias_summary", {}).get("systematically_underpredicted", [])
        over = analysis.get("bias_summary", {}).get("systematically_overpredicted", [])
        if under or over:
            evidence = []
            if under:
                evidence.append("Underpredicted: " + ", ".join(under) + ".")
            if over:
                evidence.append("Overpredicted: " + ", ".join(over) + ".")
            findings.append({
                "finding": "The baseline/best model exhibits systematic species-specific bias.",
                "importance": "high",
                "evidence": evidence,
            })

        species_review = []
        for row in species:
            species_name = str(row.get("species", ""))
            status_text = str(row.get("prediction_status", "not_available"))
            bias = _float(row.get("bias_model_minus_observed"))
            obs = _float(row.get("mean_observed"))
            model = _float(row.get("mean_modelled"))
            interpretation = f"The deterministic summary classifies {species_name} as {status_text}."
            if bias is not None:
                interpretation += f" Model-minus-observed bias is {bias:.6g}."
            if obs is not None and model is not None:
                interpretation += f" Mean observed and modelled values are {obs:.6g} and {model:.6g}, respectively."
            species_review.append({
                "species": species_name,
                "status": status_text,
                "interpretation": interpretation,
                "possible_controlling_processes": [],
            })

        warnings = list(numerical.get("flags", []))
        numerical_status = "requires_review" if warnings else "acceptable_from_recorded_checks"
        numerical_interpretation = (
            "The simulations produced usable campaign evidence, but the recorded numerical warnings should be resolved before publication-quality calibration."
            if warnings else
            "No material numerical-health flag was extracted from the available campaign files."
        )

        actions: list[dict[str, Any]] = []
        priority = 1
        if numerical.get("charge_balance_requires_review"):
            actions.append({
                "priority": priority,
                "action": "Correct and document the initial aqueous charge balance before restarting the final calibration campaign.",
                "reason": "A material initial charge-balance warning can confound parameter calibration and run-health interpretation.",
                "expected_benefit": "A chemically consistent initial condition and a more defensible numerical baseline.",
            })
            priority += 1
        if pair_count == 0 and candidates > 0:
            actions.append({
                "priority": priority,
                "action": "Evaluate scientifically selected parameter interactions after the single-parameter neighborhood is exhausted.",
                "reason": "Compensating hydraulic, kinetic, buffering, and sorption effects may be invisible to one-at-a-time tests.",
                "expected_benefit": "A larger effective search space without discarding deterministic safety and rollback rules.",
            })
            priority += 1
        actions.append({
            "priority": priority,
            "action": "Review active parameter groups and model structure against the dominant species biases before adding more unchanged runs.",
            "reason": "Persistent multi-species bias is more consistent with restricted search space or conceptual-model limitations than with insufficient repetition alone.",
            "expected_benefit": "More targeted calibration and fewer uninformative model evaluations.",
        })

        initial_text = "not available" if initial is None else f"{initial:.6g}"
        best_text = "not available" if best is None else f"{best:.6g}"
        executive = (
            f"The campaign recorded {total} unique model runs and {candidates} evaluated candidates. "
            f"The objective changed from {initial_text} to {best_text}; {accepted} candidates were accepted. "
            + ("The evidence therefore indicates local stagnation within the tested configuration." if stagnant else "The available evidence does not establish a fully converged local search.")
        )
        paper = (
            f"The V14 calibration campaign evaluated {candidates} candidate parameter configurations while preserving deterministic rollback controls. "
            f"No accepted improvement was identified and the best composite objective remained {best_text}. "
            "The results indicate that the tested local, primarily single-parameter search space was insufficient to resolve the observed multi-species biases. "
            "Further calibration should therefore prioritize correction of material numerical-input warnings, review of active process controls, and targeted interaction testing rather than simply extending the unchanged campaign."
            if stagnant else
            f"The calibration campaign evaluated {candidates} candidate configurations and achieved a best composite objective of {best_text}. Further interpretation should account for the recorded numerical warnings and species-specific biases."
        )
        return validate_campaign_review({
            "executive_summary": executive,
            "campaign_assessment": {
                "status": status,
                "confidence": 0.95 if total and candidates else 0.55,
                "summary": "The campaign completed without accepted objective improvement." if stagnant else "Campaign status was determined from the available deterministic evidence.",
            },
            "optimizer_assessment": {
                "implementation_status": "behaved_consistently_with_recorded_rules" if restore_fraction in {None, 1.0} and failed == 0 else "requires_review",
                "evidence": optimizer_evidence,
                "possible_software_issues": [] if restore_fraction in {None, 1.0} else ["Rollback restoration was not verified for every evaluated candidate."],
            },
            "scientific_findings": findings,
            "species_assessment": species_review,
            "stagnation_analysis": {
                "local_minimum_likely": stagnant,
                "reasoning": [
                    f"{accepted} of {candidates} candidates were accepted.",
                    f"Minimum candidate score was {objective.get('candidate_score_statistics', {}).get('minimum')} versus initial score {initial}.",
                    f"Interaction-pair candidates recorded: {pair_count}.",
                ],
                "more_runs_same_configuration_recommended": bool(search.get("same_configuration_additional_runs_recommended", True)),
            },
            "numerical_health": {
                "status": numerical_status,
                "important_warnings": warnings,
                "interpretation": numerical_interpretation,
            },
            "recommended_actions": actions,
            "paper_ready_conclusion": paper,
            "limitations": [
                "This review uses aggregate campaign metrics and cannot infer unrecorded time-series residual patterns.",
                "Possible controlling processes require confirmation against mineralogy, aqueous chemistry, and model reaction definitions.",
            ],
        })

    def review(self, analysis: dict[str, Any], *, mode: str = "auto") -> tuple[dict[str, Any], dict[str, Any]]:
        mode_norm = _norm(mode) or "auto"
        if mode_norm not in {"auto", "gpt", "deterministic", "off"}:
            raise ValueError("Campaign review mode must be auto, gpt, deterministic, or off.")
        metadata: dict[str, Any] = {
            "requested_mode": mode_norm,
            "config_file": str(self.paths.config_file),
            "gpt_enabled": self.settings["enabled"],
            "model": self.settings["model"],
            "generated_at": _now(),
        }
        if mode_norm == "off":
            return {}, {**metadata, "used_mode": "off", "status": "not_generated"}
        if mode_norm in {"gpt", "auto"}:
            try:
                review = self.review_with_gpt(analysis)
                return review, {**metadata, "used_mode": "gpt", "status": "success"}
            except Exception as exc:
                if mode_norm == "gpt":
                    raise
                metadata["gpt_error"] = f"{type(exc).__name__}: {exc}"
        review = self.deterministic_review(analysis)
        return review, {**metadata, "used_mode": "deterministic", "status": "success_with_fallback" if "gpt_error" in metadata else "success"}

    @staticmethod
    def markdown(review: dict[str, Any], metadata: dict[str, Any]) -> str:
        if not review:
            return "# V15.4 Campaign Review\n\nCampaign review was not generated.\n"
        lines = [
            "# V15.4 Calibration Campaign Review",
            "",
            f"**Generated:** {metadata.get('generated_at', _now())}",
            f"**Review source:** `{metadata.get('used_mode', 'unknown')}`",
            f"**Requested mode:** `{metadata.get('requested_mode', 'unknown')}`",
            "",
            "## Executive summary",
            "",
            str(review.get("executive_summary", "")),
            "",
            "## Campaign assessment",
            "",
            f"- Status: `{review.get('campaign_assessment', {}).get('status', '')}`",
            f"- Confidence: `{review.get('campaign_assessment', {}).get('confidence', '')}`",
            f"- {review.get('campaign_assessment', {}).get('summary', '')}",
            "",
            "## Optimizer assessment",
            "",
            f"**Implementation status:** `{review.get('optimizer_assessment', {}).get('implementation_status', '')}`",
        ]
        lines.extend(f"- {item}" for item in review.get("optimizer_assessment", {}).get("evidence", []))
        issues = review.get("optimizer_assessment", {}).get("possible_software_issues", [])
        if issues:
            lines.extend(["", "**Possible software issues:**", *[f"- {item}" for item in issues]])
        lines.extend(["", "## Scientific findings", ""])
        for item in review.get("scientific_findings", []):
            lines.append(f"### {item.get('finding', '')} [{item.get('importance', '')}]")
            lines.extend(f"- {evidence}" for evidence in item.get("evidence", []))
            lines.append("")
        lines.extend(["## Species assessment", ""])
        for item in review.get("species_assessment", []):
            lines.append(f"### {item.get('species', '')}: {item.get('status', '')}")
            lines.append(str(item.get("interpretation", "")))
            processes = item.get("possible_controlling_processes", [])
            if processes:
                lines.append("Possible controlling processes: " + "; ".join(map(str, processes)))
            lines.append("")
        stagnation = review.get("stagnation_analysis", {})
        lines.extend([
            "## Stagnation and search-space assessment", "",
            f"- Local minimum/stagnation likely: `{stagnation.get('local_minimum_likely', False)}`",
            f"- More runs with unchanged configuration recommended: `{stagnation.get('more_runs_same_configuration_recommended', False)}`",
        ])
        lines.extend(f"- {item}" for item in stagnation.get("reasoning", []))
        health = review.get("numerical_health", {})
        lines.extend(["", "## Numerical health", "", f"**Status:** `{health.get('status', '')}`", "", str(health.get("interpretation", ""))])
        lines.extend(f"- {item}" for item in health.get("important_warnings", []))
        lines.extend(["", "## Recommended actions", ""])
        for item in sorted(review.get("recommended_actions", []), key=lambda x: x.get("priority", 999)):
            lines.extend([
                f"### Priority {item.get('priority')}: {item.get('action', '')}",
                f"**Reason:** {item.get('reason', '')}",
                f"**Expected benefit:** {item.get('expected_benefit', '')}",
                "",
            ])
        lines.extend(["## Paper-ready conclusion", "", str(review.get("paper_ready_conclusion", "")), "", "## Limitations", ""])
        lines.extend(f"- {item}" for item in review.get("limitations", []))
        if metadata.get("gpt_error"):
            lines.extend(["", "## GPT fallback note", "", f"GPT was unavailable and the deterministic review was used: `{metadata['gpt_error']}`"])
        return "\n".join(lines).rstrip() + "\n"

    def write(self, review: dict[str, Any], metadata: dict[str, Any]) -> dict[str, str]:
        self.paths.output_dir.mkdir(parents=True, exist_ok=True)
        json_path = self.paths.output_dir / "campaign_review_V15_4.json"
        md_path = self.paths.output_dir / "campaign_review_V15_4.md"
        manifest_path = self.paths.output_dir / "campaign_review_manifest_V15_4.json"
        _json_dump(json_path, review)
        md_path.write_text(self.markdown(review, metadata), encoding="utf-8")
        _json_dump(manifest_path, metadata)
        return {
            "campaign_review_json": str(json_path),
            "campaign_review_markdown": str(md_path),
            "campaign_review_manifest": str(manifest_path),
        }


def generate_campaign_review_package(
    agent_core_dir: str | Path,
    *,
    output_dir: str | Path | None = None,
    config_file: str | Path | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    paths = CampaignReviewPaths.from_agent_core(agent_core_dir, output_dir=output_dir, config_file=config_file)
    analyzer = CampaignAnalysisV15_4(paths)
    analysis = analyzer.build()
    artifacts: dict[str, Any] = analyzer.write(analysis)
    reviewer = GPTCampaignReviewerV15_4(paths)
    review, metadata = reviewer.review(analysis, mode=mode)
    artifacts.update(reviewer.write(review, metadata))
    return {
        "version": CAMPAIGN_REVIEW_VERSION,
        "analysis": analysis,
        "review": review,
        "review_metadata": metadata,
        "artifacts": artifacts,
    }
