from __future__ import annotations

"""
V11 automatic strategy updater.

This module is intentionally deterministic. It does not run GPT and it does not
change parameter values in agent_config.xlsx. Its job is to translate the V10.9
calibration memory into a safer next-cycle strategy file:

    04_results/calibration_strategy_V11.xlsx
    04_results/strategy_update_log_V11.xlsx
    05_reports/strategy_update_V11.md

The deterministic decision engine then reads calibration_strategy_V11.xlsx first
and falls back to calibration_strategy_V10.xlsx only when the V11 file is absent.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable

try:
    from modules.v12_campaign_phase_manager import (
        V12CampaignPhaseManager,
        phase_for_parameter,
        recommendation_as_dict,
    )
except Exception:
    from v12_campaign_phase_manager import (
        V12CampaignPhaseManager,
        phase_for_parameter,
        recommendation_as_dict,
    )

import math
import pandas as pd

from .config import ProjectPaths
from .config_reader import ConfigReader
from .io_utils import log, safe_float, timestamp


EPS = 1e-15


@dataclass
class StrategyUpdateResult:
    strategy_update_action: str
    strategy_update_reason: str
    strategy_file: str
    strategy_log_file: str
    strategy_report_file: str
    available_count: int
    blocked_count: int
    next_cycle_parameters: str
    blocked_parameters: str

    def as_dict(self) -> Dict[str, Any]:
        return self.__dict__.copy()


def _norm_col(name: Any) -> str:
    return (
        str(name)
        .strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
        .replace("/", "_")
        .replace("(", "")
        .replace(")", "")
    )


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out.columns = [_norm_col(c) for c in out.columns]
    return out


def _safe_read_excel(path: Path, sheet_name: str | None = None) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    try:
        if sheet_name:
            return pd.read_excel(path, sheet_name=sheet_name)
        return pd.read_excel(path)
    except Exception:
        try:
            return pd.read_excel(path)
        except Exception:
            return pd.DataFrame()


def _read_strategy(results_dir: Path) -> tuple[pd.DataFrame, str]:
    """Read V11 strategy first, then V10 strategy; return dataframe and source."""
    v11 = results_dir / "calibration_strategy_V11.xlsx"
    v10 = results_dir / "calibration_strategy_V10.xlsx"

    for path, source in [(v11, "V11"), (v10, "V10")]:
        if not path.exists():
            continue
        df = _safe_read_excel(path, sheet_name="calibration_strategy")
        if not df.empty:
            return _normalize_columns(df), source

    return pd.DataFrame(), "none"


def _strategy_from_config(config: ConfigReader) -> pd.DataFrame:
    params = config.parameters().copy()
    if params.empty:
        return pd.DataFrame()

    rows = []
    for _, row in params.iterrows():
        p = str(row.get("parameter", "")).strip()
        if not p or p.lower() == "nan":
            continue
        status = str(row.get("status", "active")).strip().lower()
        rows.append(
            {
                "parameter": p,
                "config_status": status,
                "strategy_status": "active" if status == "active" else status,
                "process_group": row.get("group", ""),
                "group": row.get("group", ""),
                "value": row.get("value", ""),
                "min": row.get("min", ""),
                "max": row.get("max", ""),
                "total_score": 0.0,
                "recommended_direction": "test_both_directions",
                "next_cycle_change": "no",
                "priority": "",
            }
        )
    return _normalize_columns(pd.DataFrame(rows))


def _config_status_map(config: ConfigReader) -> Dict[str, Dict[str, Any]]:
    params = config.parameters().copy()
    out: Dict[str, Dict[str, Any]] = {}
    for _, row in params.iterrows():
        p = str(row.get("parameter", "")).strip()
        if not p or p.lower() == "nan":
            continue
        out[p] = {
            "status": str(row.get("status", "active")).strip().lower(),
            "value": safe_float(row.get("value")),
            "min": safe_float(row.get("min")),
            "max": safe_float(row.get("max")),
            "group": row.get("group", ""),
        }
    return out


def _join(values: Iterable[Any], limit: int = 40) -> str:
    clean = []
    for v in values:
        s = str(v).strip()
        if not s or s.lower() in ["nan", "none"]:
            continue
        if s not in clean:
            clean.append(s)
        if len(clean) >= limit:
            break
    return ", ".join(clean)


def _near_lower(value: float | None, mn: float | None) -> bool:
    if value is None or mn is None:
        return False
    tol = max(abs(mn) * 1e-10, 1e-15)
    return value <= mn + tol


def _near_upper(value: float | None, mx: float | None) -> bool:
    if value is None or mx is None:
        return False
    tol = max(abs(mx) * 1e-10, 1e-15)
    return value >= mx - tol


def _read_bad_direction_table(results_dir: Path) -> pd.DataFrame:
    bad = _safe_read_excel(results_dir / "bad_suggestion_memory_V10_9.xlsx")
    if bad.empty:
        return pd.DataFrame(columns=["parameter", "bad_increase_count", "bad_decrease_count"])
    bad = _normalize_columns(bad)
    if "parameter" not in bad.columns:
        return pd.DataFrame(columns=["parameter", "bad_increase_count", "bad_decrease_count"])
    if "direction" not in bad.columns:
        bad["direction"] = ""
    rows = []
    for p, g in bad.groupby("parameter", sort=True):
        dirs = g["direction"].astype(str).str.lower().str.strip()
        rows.append(
            {
                "parameter": str(p).strip(),
                "bad_increase_count": int((dirs == "increase").sum()),
                "bad_decrease_count": int((dirs == "decrease").sum()),
                "total_bad_count": int(len(g)),
            }
        )
    return pd.DataFrame(rows)


def _read_exhaustion(results_dir: Path) -> pd.DataFrame:
    ex = _safe_read_excel(results_dir / "parameter_exhaustion_V10_9.xlsx")
    if ex.empty:
        return pd.DataFrame(columns=["parameter", "avoid_parameter", "parameter_status"])
    ex = _normalize_columns(ex)
    if "parameter" not in ex.columns:
        return pd.DataFrame(columns=["parameter", "avoid_parameter", "parameter_status"])
    if "avoid_parameter" not in ex.columns:
        ex["avoid_parameter"] = False
    if "parameter_status" not in ex.columns:
        ex["parameter_status"] = ""
    return ex


def _read_gpt_advice(results_dir: Path) -> pd.DataFrame:
    # V11 treats GPT output as advisory only. Read the V11 file first,
    # then fall back to the inherited V10.9 recommendation filename.
    gpt = _safe_read_excel(results_dir / "gpt_supervisor_recommendations_V11.xlsx")
    if gpt.empty:
        gpt = _safe_read_excel(results_dir / "gpt_supervisor_recommendations.xlsx")
    if gpt.empty:
        return pd.DataFrame(columns=["parameter", "recommended_status", "confidence"])
    gpt = _normalize_columns(gpt)
    if "parameter" not in gpt.columns:
        return pd.DataFrame(columns=["parameter", "recommended_status", "confidence"])
    if "recommended_status" not in gpt.columns:
        gpt["recommended_status"] = ""
    if "confidence" not in gpt.columns:
        gpt["confidence"] = 0.0
    gpt["confidence"] = pd.to_numeric(gpt["confidence"], errors="coerce").fillna(0.0)
    return gpt




def _is_valid_not_best_history_row(row: pd.Series) -> bool:
    """Return True when the latest run was valid but scientifically worse than best."""
    status = str(row.get("qc_acceptance_status", row.get("acceptance_status", ""))).strip().lower()
    if status in {"accepted_valid_not_best", "valid_run_not_new_best", "rejected_scientific", "failed"}:
        return True
    objective = safe_float(row.get("objective_total", row.get("qc_objective_score")))
    best = safe_float(row.get("best_objective", row.get("qc_previous_best_score")))
    return objective is not None and best is not None and objective > best


def _recent_valid_not_best_count(history_file: Path, max_rows: int = 20) -> int:
    """Count consecutive recent runs that were valid but not new best."""
    hist = _safe_read_excel(history_file)
    if hist.empty:
        return 0
    count = 0
    for _, row in hist.tail(max_rows).iloc[::-1].iterrows():
        if _is_valid_not_best_history_row(row):
            count += 1
        else:
            break
    return count


def _suggestion_direction_from_row(row: pd.Series) -> str:
    old = safe_float(row.get("old_value"))
    new = safe_float(row.get("new_value"))
    if old is None or new is None:
        return "unknown"
    if math.isclose(old, new, rel_tol=1e-12, abs_tol=1e-18):
        return "unchanged"
    return "increase" if new > old else "decrease"


def _read_recent_nonbest_parameters(results_dir: Path, history_file: Path, max_cooldown: int = 8) -> pd.DataFrame:
    """Read recently applied suggestions that led to valid-not-best runs.

    This is an additional V11 safety layer. It does not replace V10.9 bad
    suggestion memory; it covers cases where bad memory cannot infer the tested
    parameter because the older best-parameter snapshot was incomplete.
    """
    n_bad = _recent_valid_not_best_count(history_file, max_rows=max_cooldown + 2)
    if n_bad <= 0:
        return pd.DataFrame(columns=["parameter", "recent_bad_count", "recent_bad_direction"])

    suggestions = _safe_read_excel(results_dir / "parameter_suggestions_V11.xlsx")
    if suggestions.empty:
        return pd.DataFrame(columns=["parameter", "recent_bad_count", "recent_bad_direction"])
    suggestions = _normalize_columns(suggestions)
    if "parameter" not in suggestions.columns:
        return pd.DataFrame(columns=["parameter", "recent_bad_count", "recent_bad_direction"])

    if "applied_to_agent_config" in suggestions.columns:
        applied = suggestions["applied_to_agent_config"].astype(str).str.lower().isin(["true", "1", "1.0", "yes", "y"])
        suggestions = suggestions[applied].copy()
    if "selection_status" in suggestions.columns:
        selected = suggestions["selection_status"].astype(str).str.contains("selected", case=False, na=False)
        suggestions = suggestions[selected].copy()
    if suggestions.empty:
        return pd.DataFrame(columns=["parameter", "recent_bad_count", "recent_bad_direction"])

    recent = suggestions.tail(min(n_bad, max_cooldown)).copy()
    recent["recent_bad_direction"] = recent.apply(_suggestion_direction_from_row, axis=1)
    rows = []
    for parameter, g in recent.groupby("parameter", sort=False):
        dirs = [d for d in g["recent_bad_direction"].astype(str).tolist() if d and d != "unknown"]
        rows.append(
            {
                "parameter": str(parameter).strip(),
                "recent_bad_count": int(len(g)),
                "recent_bad_direction": dirs[-1] if dirs else "unknown",
                "recent_bad_directions": ", ".join(dirs),
            }
        )
    return pd.DataFrame(rows)


def _append_log(path: Path, row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame([row])
    if path.exists():
        try:
            old = pd.read_excel(path)
            if old.empty:
                out = new
            else:
                out = pd.concat([old, new], ignore_index=True)
        except Exception:
            out = new
    else:
        out = new
    out.to_excel(path, index=False)


def _write_report(
    report_file: Path,
    *,
    source: str,
    available: pd.DataFrame,
    blocked: pd.DataFrame,
    next_cycle: pd.DataFrame,
    context: str,
    reason: str,
) -> None:
    report_file.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append("# MIN3P AI V11 automatic strategy update")
    lines.append("")
    lines.append(f"Timestamp: {timestamp()}")
    lines.append(f"Context: {context}")
    lines.append(f"Base strategy source: {source}")
    lines.append(f"Update reason: {reason}")
    lines.append("")
    lines.append("## Next-cycle parameters")
    if next_cycle.empty:
        lines.append("No automatic next-cycle parameter was selected.")
    else:
        for _, row in next_cycle.iterrows():
            lines.append(
                f"- {row.get('parameter')}: direction={row.get('recommended_direction')}, "
                f"score={row.get('v11_auto_score')}, reason={row.get('v11_reason')}"
            )
    lines.append("")
    lines.append("## Blocked / skipped parameters")
    if blocked.empty:
        lines.append("No parameters were blocked by the V11 strategy updater.")
    else:
        for _, row in blocked.head(50).iterrows():
            lines.append(f"- {row.get('parameter')}: {row.get('v11_reason')}")
    lines.append("")
    lines.append("## Available parameter count")
    lines.append(str(len(available)))
    report_file.write_text("\n".join(lines), encoding="utf-8")


def update_v11_strategy(
    *,
    paths: ProjectPaths,
    config: ConfigReader,
    context: str = "after_run",
    max_next_cycle: int = 1,
    acceptance: Dict[str, Any] | None = None,
    quality: Dict[str, Any] | None = None,
    bad_memory: Dict[str, Any] | None = None,
    parameter_exhaustion: Dict[str, Any] | None = None,
    no_progress: Dict[str, Any] | None = None,
    filter_info: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """
    Update calibration_strategy_V11.xlsx from V10.9 memory.

    GPT, if used, is only advisory through gpt_supervisor_recommendations.xlsx.
    This function validates those recommendations and turns only safe statuses into
    strategy metadata. It never changes numeric parameter values.
    """
    results_dir = paths.results_dir
    reports_dir = paths.reports_dir
    results_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)

    strategy, source = _read_strategy(results_dir)
    if strategy.empty:
        strategy = _strategy_from_config(config)
        source = "agent_config"

    if strategy.empty or "parameter" not in strategy.columns:
        reason = "no strategy rows and no parameters available"
        result = StrategyUpdateResult(
            strategy_update_action="not_updated",
            strategy_update_reason=reason,
            strategy_file=str(results_dir / "calibration_strategy_V11.xlsx"),
            strategy_log_file=str(results_dir / "strategy_update_log_V11.xlsx"),
            strategy_report_file=str(reports_dir / "strategy_update_V11.md"),
            available_count=0,
            blocked_count=0,
            next_cycle_parameters="",
            blocked_parameters="",
        )
        _append_log(Path(result.strategy_log_file), {"timestamp": timestamp(), "context": context, **result.as_dict()})
        return result.as_dict()

    config_map = _config_status_map(config)
    bad_dirs = _read_bad_direction_table(results_dir)
    exhaustion = _read_exhaustion(results_dir)
    gpt = _read_gpt_advice(results_dir)
    recent_nonbest = _read_recent_nonbest_parameters(results_dir, paths.history_file)

    # ------------------------------------------------------------------
    # V12 scientific phase-manager overlay
    # ------------------------------------------------------------------
    # Strategy layer only:
    # - does not change agent_config.xlsx
    # - does not change parameter values
    # - does not run MIN3P
    # - only changes ranking/blocking metadata in calibration_strategy_V11.xlsx
    try:
        history_for_v12 = _safe_read_excel(paths.history_file)
        v12_manager = V12CampaignPhaseManager(
            state_path=results_dir / "v12_campaign_state.json",
            non_improvement_limit=3,
            cooldown_runs=2,
            recent_window=10,
        )
        v12_recommendation = v12_manager.recommend(history_for_v12)
        v12_recommendation_dict = recommendation_as_dict(v12_recommendation)
        v12_active_phase = str(v12_recommendation.active_phase)
        v12_cooldown_phases = set(v12_recommendation.cooldown_phases.keys())
        v12_multiplier_scale = float(v12_recommendation.multiplier_scale)
        v12_phase_reason = str(v12_recommendation.reason)
    except Exception as exc:
        v12_recommendation_dict = {
            "active_phase": "unknown",
            "allowed_phases": [],
            "cooldown_phases": {},
            "multiplier_scale": 1.0,
            "reason": f"V12 phase manager failed: {exc}",
        }
        v12_active_phase = "unknown"
        v12_cooldown_phases = set()
        v12_multiplier_scale = 1.0
        v12_phase_reason = f"V12 phase manager failed: {exc}"

    bad_map = {str(r["parameter"]): r for _, r in bad_dirs.iterrows()}
    ex_map = {str(r["parameter"]): r for _, r in exhaustion.iterrows()}
    gpt_map = {str(r["parameter"]): r for _, r in gpt.iterrows()}
    recent_map = {str(r["parameter"]): r for _, r in recent_nonbest.iterrows()}

    df = strategy.copy()
    for col in [
        "config_status",
        "strategy_status",
        "total_score",
        "recommended_direction",
        "next_cycle_change",
        "process_group",
        "v12_phase",
        "v12_active_phase",
        "v12_phase_status",
        "v12_multiplier_scale",
        "v12_phase_reason",
    ]:
        if col not in df.columns:
            df[col] = "" if col != "total_score" else 0.0

    df["total_score"] = pd.to_numeric(df["total_score"], errors="coerce").fillna(0.0)

    rows = []
    for _, row in df.iterrows():
        p = str(row.get("parameter", "")).strip()
        if not p or p.lower() == "nan":
            continue

        c = config_map.get(p, {})
        config_status = str(c.get("status", row.get("config_status", "active"))).strip().lower()
        value = c.get("value")
        mn = c.get("min")
        mx = c.get("max")
        group = c.get("group", row.get("process_group", row.get("group", "")))

        v12_phase = phase_for_parameter(p)
        v12_phase_status = "normal"

        base_score = safe_float(row.get("total_score")) or 0.0
        adjustment = 0.0
        reasons: list[str] = []
        blocked = False
        status = "active"
        recommended_direction = str(row.get("recommended_direction", "test_both_directions")).strip()
        if not recommended_direction or recommended_direction.lower() in ["nan", "none"]:
            recommended_direction = "test_both_directions"

        if config_status not in ["active", "yes", "true", "1"]:
            blocked = True
            status = config_status if config_status else "inactive"
            reasons.append(f"config status is {config_status}")

        at_lower = _near_lower(value, mn)
        at_upper = _near_upper(value, mx)

        if at_lower or at_upper:
            # V12.3: a parameter at a bound must only move away from the bound.
            # This also handles "test_both_directions", which would otherwise default
            # to increase and can create no-op or invalid suggestions at the upper bound.
            direction_lower = recommended_direction.lower()

            both_direction_labels = [
                "test_both_directions",
                "both",
                "test_both",
                "both_directions",
                "auto",
                "",
                "nan",
                "none",
            ]

            if at_upper and direction_lower in [
                "increase",
                "up",
                "positive",
                "increase_value",
                *both_direction_labels,
            ]:
                recommended_direction = "decrease"
                reasons.append("at upper bound; V12.3 forced direction to decrease")

            elif at_lower and direction_lower in [
                "decrease",
                "down",
                "negative",
                "decrease_value",
                *both_direction_labels,
            ]:
                recommended_direction = "increase"
                reasons.append("at lower bound; V12.3 forced direction to increase")

            else:
                reasons.append("near one parameter bound")

        ex = ex_map.get(p)
        if ex is not None:
            avoid = str(ex.get("avoid_parameter", "False")).strip().lower() in ["true", "1", "yes", "y"]
            ex_status = str(ex.get("parameter_status", "")).strip().lower()
            if avoid or ex_status == "temporarily_exhausted":
                blocked = True
                status = "blocked"
                adjustment -= 1e6
                reasons.append("temporarily exhausted by V10.9 memory")
            elif ex_status == "direction_limited":
                reasons.append("direction-limited by V10.9 memory")
                adjustment -= 0.10

        bd = bad_map.get(p)
        if bd is not None:
            bad_inc = int(safe_float(bd.get("bad_increase_count")) or 0)
            bad_dec = int(safe_float(bd.get("bad_decrease_count")) or 0)
            total_bad = int(safe_float(bd.get("total_bad_count")) or 0)
            adjustment -= min(0.50, 0.10 * total_bad)
            if bad_inc > 0 and bad_dec == 0:
                recommended_direction = "decrease"
                reasons.append("increase direction was previously bad; trying decrease if selected")
            elif bad_dec > 0 and bad_inc == 0:
                recommended_direction = "increase"
                reasons.append("decrease direction was previously bad; trying increase if selected")
            elif bad_inc > 0 and bad_dec > 0:
                blocked = True
                status = "blocked"
                reasons.append("both directions previously bad")

        recent_bad = recent_map.get(p)
        if recent_bad is not None:
            # Cool down parameters that were just tested and produced a valid
            # but worse run. This prevents immediate reselection when the older
            # bad-suggestion memory cannot infer the changed parameter.
            blocked = True
            status = "blocked"
            adjustment -= 1e6
            reasons.append(
                "recent V11 valid-not-best test; temporarily cooled down to avoid immediate repeat"
            )

        ga = gpt_map.get(p)
        if ga is not None:
            rec = str(ga.get("recommended_status", "")).strip().lower()
            conf = safe_float(ga.get("confidence")) or 0.0
            if rec in ["do_not_touch", "manual_review"] and conf >= 0.60:
                blocked = True
                status = "blocked"
                adjustment -= 1e6
                reasons.append(f"GPT advisory status={rec} with confidence={conf:.2f}")
            elif rec == "watch":
                adjustment -= 0.05
                reasons.append("GPT advisory status=watch")
            elif rec == "support_current_strategy":
                adjustment += 0.05 * min(max(conf, 0.0), 1.0)
                reasons.append("GPT supports current strategy")

                # ------------------------------------------------------------------
        # V12.4 final bound-direction enforcement
        # ------------------------------------------------------------------
        # Earlier memory logic may override the initial V12.3 bound-safe
        # direction. Re-apply the bound rule here so the final exported
        # recommended_direction always moves away from hard bounds.
        final_direction_lower = str(recommended_direction).strip().lower()

        if at_upper and final_direction_lower in [
            "increase",
            "up",
            "positive",
            "increase_value",
            "test_both_directions",
            "both",
            "test_both",
            "both_directions",
            "auto",
            "",
            "nan",
            "none",
        ]:
            recommended_direction = "decrease"
            reasons.append("at upper bound; V12.4 final direction forced to decrease")

        elif at_lower and final_direction_lower in [
            "decrease",
            "down",
            "negative",
            "decrease_value",
            "test_both_directions",
            "both",
            "test_both",
            "both_directions",
            "auto",
            "",
            "nan",
            "none",
        ]:
            recommended_direction = "increase"
            reasons.append("at lower bound; V12.4 final direction forced to increase")
        # ------------------------------------------------------------------
        # V12 group-level scientific phase control
        # ------------------------------------------------------------------
        # Cooldown is temporary and strategy-file only. It does not change the
        # user's active/inactive/frozen status in agent_config.xlsx.
        if not blocked:
            if v12_phase in v12_cooldown_phases:
                blocked = True
                status = "blocked"
                v12_phase_status = "cooldown"
                adjustment -= 1e6
                reasons.append(f"V12 phase cooldown: {v12_phase}")

            elif (
                v12_active_phase
                and v12_active_phase not in ["unknown", "manual_review"]
                and v12_phase == v12_active_phase
            ):
                v12_phase_status = "active_phase_priority"
                adjustment += 0.25
                reasons.append(f"V12 active scientific phase priority: {v12_active_phase}")

            elif (
                v12_active_phase
                and v12_active_phase not in ["unknown", "manual_review"]
                and v12_phase != "unknown"
            ):
                v12_phase_status = "non_active_phase_deprioritized"
                adjustment -= 0.05
                reasons.append(f"V12 non-active phase mildly deprioritized; active phase is {v12_active_phase}")

        if blocked:
            next_change = "no"
        else:
            next_change = "candidate"
            status = "active"

        if not reasons:
            reasons.append("available for deterministic V11 selection")

        r = row.to_dict()
        r.update(
            {
                "parameter": p,
                "config_status": config_status,
                "strategy_status": status,
                "process_group": group,
                "recommended_direction": recommended_direction,
                "next_cycle_change": next_change,
                "v11_auto_score": float(base_score + adjustment),
                "v11_score_adjustment": float(adjustment),
                "v11_bound_lower": bool(at_lower),
                "v11_bound_upper": bool(at_upper),
                "v11_reason": "; ".join(reasons),
                "v12_phase": v12_phase,
                "v12_active_phase": v12_active_phase,
                "v12_phase_status": v12_phase_status,
                "v12_multiplier_scale": v12_multiplier_scale,
                "v12_phase_reason": v12_phase_reason,
                "v11_context": context,
                "v11_last_update": timestamp(),
            }
        )
        rows.append(r)

    out = pd.DataFrame(rows)
    if out.empty:
        reason = "all rows empty after normalization"
        result = StrategyUpdateResult(
            strategy_update_action="not_updated",
            strategy_update_reason=reason,
            strategy_file=str(results_dir / "calibration_strategy_V11.xlsx"),
            strategy_log_file=str(results_dir / "strategy_update_log_V11.xlsx"),
            strategy_report_file=str(reports_dir / "strategy_update_V11.md"),
            available_count=0,
            blocked_count=0,
            next_cycle_parameters="",
            blocked_parameters="",
        )
        _append_log(Path(result.strategy_log_file), {"timestamp": timestamp(), "context": context, **result.as_dict()})
        return result.as_dict()

    available = out[out["next_cycle_change"].astype(str).str.lower().eq("candidate")].copy()
    blocked_df = out[~out.index.isin(available.index)].copy()

    if not available.empty:
        available = available.sort_values("v11_auto_score", ascending=False)
        selected_idx = available.head(max(int(max_next_cycle), 1)).index
        out.loc[:, "next_cycle_change"] = "no"
        out.loc[selected_idx, "next_cycle_change"] = "yes"
        out["v11_next_cycle_rank"] = pd.Series([""] * len(out), index=out.index, dtype="object")
        for rank, idx in enumerate(selected_idx, start=1):
            out.loc[idx, "v11_next_cycle_rank"] = rank
    else:
        out.loc[:, "next_cycle_change"] = "no"
        out["v11_next_cycle_rank"] = pd.Series([""] * len(out), index=out.index, dtype="object")

    out = out.sort_values(
        ["next_cycle_change", "strategy_status", "v11_auto_score"],
        ascending=[False, True, False],
    )

    strategy_file = results_dir / "calibration_strategy_V11.xlsx"
    log_file = results_dir / "strategy_update_log_V11.xlsx"
    report_file = reports_dir / "strategy_update_V11.md"

    with pd.ExcelWriter(strategy_file, engine="openpyxl") as writer:
        out.to_excel(writer, sheet_name="calibration_strategy", index=False)
        bad_dirs.to_excel(writer, sheet_name="bad_direction_summary", index=False)
        exhaustion.to_excel(writer, sheet_name="parameter_exhaustion", index=False)
        recent_nonbest.to_excel(writer, sheet_name="recent_nonbest_cooldown", index=False)
        gpt.to_excel(writer, sheet_name="gpt_advice", index=False)
        pd.DataFrame([v12_recommendation_dict]).to_excel(
            writer,
            sheet_name="v12_phase_manager",
            index=False,
        )

    selected = out[out["next_cycle_change"].astype(str).str.lower().eq("yes")].copy()
    blocked_now = out[out["strategy_status"].astype(str).str.lower().isin(["blocked", "inactive", "frozen"])].copy()

    reason = (
        f"updated from {source}; selected {len(selected)} next-cycle parameter(s); "
        f"blocked/skipped {len(blocked_now)} parameter(s); "
        f"V12 active_phase={v12_active_phase}; "
        f"V12 cooldown_phases={sorted(v12_cooldown_phases)}; "
        f"V12 multiplier_scale={v12_multiplier_scale}"
    )
    result = StrategyUpdateResult(
        strategy_update_action="updated",
        strategy_update_reason=reason,
        strategy_file=str(strategy_file),
        strategy_log_file=str(log_file),
        strategy_report_file=str(report_file),
        available_count=int(len(out) - len(blocked_now)),
        blocked_count=int(len(blocked_now)),
        next_cycle_parameters=_join(selected["parameter"].tolist()),
        blocked_parameters=_join(blocked_now["parameter"].tolist()),
    )

    _append_log(log_file, {"timestamp": timestamp(), "context": context, **result.as_dict()})
    _write_report(
        report_file,
        source=source,
        available=out[~out.index.isin(blocked_now.index)].copy(),
        blocked=blocked_now,
        next_cycle=selected,
        context=context,
        reason=reason,
    )

    log(paths, f"V11 automatic strategy updater: {reason}")
    return result.as_dict()
