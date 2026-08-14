from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable

import pandas as pd


REPORT_NAME = "V10_9_manual_review_report.md"
REPORT_LOG_NAME = "manual_review_report_memory_V10_9.xlsx"


def _timestamp_now() -> str:
    return pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")


def _safe_read_excel(path: str | Path | None, **kwargs) -> pd.DataFrame:
    if path is None:
        return pd.DataFrame()
    path = Path(path)
    if not path.exists():
        return pd.DataFrame()
    try:
        return pd.read_excel(path, **kwargs)
    except Exception:
        return pd.DataFrame()


def _safe_value(row: pd.Series | dict | None, key: str, default: Any = "") -> Any:
    if row is None:
        return default
    try:
        value = row.get(key, default)
    except Exception:
        return default
    try:
        if pd.isna(value):
            return default
    except Exception:
        pass
    return value


def _fmt(value: Any, digits: int = 6) -> str:
    try:
        if value is None or pd.isna(value):
            return "not available"
        if isinstance(value, float):
            return f"{value:.{digits}g}"
        return str(value)
    except Exception:
        return str(value)


def _split_csv(value: Any) -> list[str]:
    if value is None:
        return []
    try:
        if pd.isna(value):
            return []
    except Exception:
        pass
    out = []
    for part in str(value).split(","):
        s = part.strip()
        if s and s.lower() != "nan":
            out.append(s)
    return out


def _markdown_table(rows: list[dict[str, Any]], columns: list[str]) -> str:
    if not rows:
        return "_No records available._"
    header = "| " + " | ".join(columns) + " |"
    sep = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = []
    for row in rows:
        body.append("| " + " | ".join(str(row.get(c, "")) for c in columns) + " |")
    return "\n".join([header, sep, *body])


def _latest_history_row(history_file: Path) -> pd.Series | None:
    hist = _safe_read_excel(history_file)
    if hist.empty:
        return None
    return hist.iloc[-1]


def _best_ranking_row(ranking_file: Path) -> pd.Series | None:
    ranking = _safe_read_excel(ranking_file)
    if ranking.empty:
        return None
    if "TOTAL_SCORE" in ranking.columns:
        values = pd.to_numeric(ranking["TOTAL_SCORE"], errors="coerce")
        if values.notna().any():
            return ranking.loc[values.idxmin()]
    return ranking.iloc[0]


def _latest_score_from_history(row: pd.Series | None) -> Any:
    if row is None:
        return None
    for key in ["qc_objective_score", "TOTAL_SCORE", "objective_score", "global_score", "weighted_score"]:
        value = _safe_value(row, key, None)
        if value is not None and str(value).lower() != "nan":
            return value
    return None


def _bad_direction_summary(bad_memory_file: Path, max_rows: int = 20) -> tuple[list[dict[str, Any]], list[str]]:
    bad = _safe_read_excel(bad_memory_file)
    if bad.empty:
        return [], []

    rows: list[dict[str, Any]] = []
    for _, r in bad.tail(max_rows).iterrows():
        rows.append(
            {
                "parameter": _fmt(_safe_value(r, "parameter")),
                "direction": _fmt(_safe_value(r, "direction")),
                "factor": _fmt(_safe_value(r, "factor")),
                "score_change": _fmt(_safe_value(r, "score_change")),
                "percent_improvement": _fmt(_safe_value(r, "percent_improvement")),
            }
        )

    bad_params = sorted(set(str(x).strip() for x in bad.get("parameter", pd.Series(dtype=str)).dropna().astype(str)))
    return rows, bad_params


def _exhaustion_summary(exhaustion_file: Path) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    ex = _safe_read_excel(exhaustion_file)
    if ex.empty:
        return [], [], []

    rows: list[dict[str, Any]] = []
    exhausted: list[str] = []
    direction_limited: list[str] = []
    for _, r in ex.iterrows():
        param = str(_safe_value(r, "parameter", "")).strip()
        status = str(_safe_value(r, "parameter_status", "")).strip()
        if not param:
            continue
        if status == "temporarily_exhausted":
            exhausted.append(param)
        if status == "direction_limited":
            direction_limited.append(param)
        rows.append(
            {
                "parameter": param,
                "bad_increase": _fmt(_safe_value(r, "bad_increase_count", 0)),
                "bad_decrease": _fmt(_safe_value(r, "bad_decrease_count", 0)),
                "status": status,
                "avoid": _fmt(_safe_value(r, "avoid_parameter", "")),
            }
        )
    return rows, sorted(set(exhausted)), sorted(set(direction_limited))


def _recommendations(stop_trigger: str, exhausted: list[str], direction_limited: list[str], blocked_ratio: Any) -> list[str]:
    recs: list[str] = []

    if stop_trigger == "high_blocked_candidate_ratio":
        recs.append(
            "Review the deterministic calibration strategy because most candidate moves are now blocked by V10.9 memory."
        )
        recs.append(
            "Increase the diversity of active parameters or allow a larger candidate pool before the next auto campaign."
        )
    elif stop_trigger == "consecutive_no_best":
        recs.append(
            "Review objective weights and calibration priorities because several valid runs did not improve the best score."
        )
        recs.append(
            "Run or refresh sensitivity analysis before continuing automatic calibration."
        )
    elif stop_trigger in {"all_active_parameters_exhausted", "all_candidates_blocked"}:
        recs.append(
            "Pause automatic calibration and revise the active parameter set because the current candidate space is exhausted."
        )
    else:
        recs.append("Review the latest V10.9 memory tables before continuing automatic calibration.")

    if exhausted:
        recs.append(
            "Keep temporarily exhausted parameters inactive for the next short campaign unless there is a scientific reason to retest them."
        )
    if direction_limited:
        recs.append(
            "For direction-limited parameters, test only the untried or scientifically justified opposite direction."
        )

    try:
        br = float(blocked_ratio)
        if br >= 0.7:
            recs.append(
                "Because the blocked-candidate ratio is high, consider activating secondary process parameters or changing calibration focus/species priorities."
            )
    except Exception:
        pass

    # Remove duplicates while preserving order.
    seen = set()
    unique = []
    for item in recs:
        if item not in seen:
            unique.append(item)
            seen.add(item)
    return unique


def _append_report_log(results_dir: Path, row: Dict[str, Any]) -> str:
    out = results_dir / REPORT_LOG_NAME
    df_new = pd.DataFrame([row])
    if out.exists():
        try:
            old = pd.read_excel(out)
            df_new = pd.concat([old, df_new], ignore_index=True)
        except Exception:
            pass
    df_new.to_excel(out, index=False)
    return str(out)


def write_v10_9_manual_review_report(
    *,
    project_dir: str | Path,
    results_dir: str | Path,
    reports_dir: str | Path,
    history_file: str | Path,
    ranking_file: str | Path,
    config_file: str | Path | None = None,
    stop_info: Dict[str, Any] | None = None,
    filter_info: Dict[str, Any] | None = None,
    diagnostic: Dict[str, Any] | None = None,
    run_dir: str | Path | None = None,
    context: str = "manual",
) -> Dict[str, Any]:
    """
    Create a V10.9 manual-review report after the calibration agent stops.

    The report is intentionally deterministic and file-based. It does not call GPT;
    it summarizes existing V10.9 memory tables so the user can decide the next
    scientific action.
    """
    project_dir = Path(project_dir)
    results_dir = Path(results_dir)
    reports_dir = Path(reports_dir)
    history_file = Path(history_file)
    ranking_file = Path(ranking_file)
    config_file = Path(config_file) if config_file is not None else None
    reports_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    stop_info = stop_info or {}
    filter_info = filter_info or {}
    diagnostic = diagnostic or {}

    latest = _latest_history_row(history_file)
    best = _best_ranking_row(ranking_file)

    stop_trigger = str(
        stop_info.get("no_progress_stop_trigger")
        or diagnostic.get("qc_no_progress_stop_trigger")
        or _safe_value(latest, "qc_no_progress_stop_trigger", "manual_review")
    )
    stop_reason = str(
        stop_info.get("no_progress_reason")
        or diagnostic.get("qc_no_progress_reason")
        or _safe_value(latest, "qc_no_progress_reason", "manual review requested")
    )
    blocked_ratio = (
        stop_info.get("latest_blocked_ratio")
        if stop_info.get("latest_blocked_ratio") is not None
        else _safe_value(latest, "qc_no_progress_latest_blocked_ratio", None)
    )

    latest_score = _latest_score_from_history(latest)
    best_score = _safe_value(best, "TOTAL_SCORE", None)
    best_run = _safe_value(best, "run_folder", _safe_value(best, "run", "not available"))
    latest_run = str(run_dir) if run_dir else _safe_value(latest, "run_folder", "not available")

    bad_rows, bad_params = _bad_direction_summary(results_dir / "bad_suggestion_memory_V10_9.xlsx")
    exhaustion_rows, exhausted, direction_limited = _exhaustion_summary(results_dir / "parameter_exhaustion_V10_9.xlsx")

    latest_blocked_params = _split_csv(
        stop_info.get("latest_blocked_parameters")
        or filter_info.get("bad_suggestion_blocked_parameters")
        or filter_info.get("parameter_exhaustion_blocked_parameters")
        or _safe_value(latest, "qc_no_progress_latest_blocked_parameters", "")
    )

    active_count = stop_info.get("active_parameter_count") or _safe_value(latest, "qc_no_progress_active_parameter_count", "not available")
    exhausted_count = stop_info.get("exhausted_active_count") or _safe_value(latest, "qc_no_progress_exhausted_active_count", "not available")
    no_best_count = stop_info.get("consecutive_no_best_count") or _safe_value(latest, "qc_no_progress_consecutive_no_best_count", "not available")

    recs = _recommendations(stop_trigger, exhausted, direction_limited, blocked_ratio)

    created_at = _timestamp_now()
    report_file = reports_dir / REPORT_NAME

    md: list[str] = []
    md.append("# V10.9 Manual Review Report")
    md.append("")
    md.append(f"**Created:** {created_at}")
    md.append(f"**Project:** `{project_dir.name}`")
    md.append(f"**Context:** `{context}`")
    md.append("")

    md.append("## Stop summary")
    md.append("")
    md.append(f"- **Stop trigger:** `{stop_trigger}`")
    md.append(f"- **Reason:** {stop_reason}")
    md.append(f"- **Consecutive valid-not-best runs in campaign:** {_fmt(no_best_count)}")
    md.append(f"- **Latest blocked-candidate ratio:** {_fmt(blocked_ratio)}")
    md.append(f"- **Latest run:** `{latest_run}`")
    md.append("")

    md.append("## Score summary")
    md.append("")
    md.append(f"- **Best TOTAL_SCORE:** {_fmt(best_score)}")
    md.append(f"- **Latest objective score:** {_fmt(latest_score)}")
    md.append(f"- **Best run folder:** `{best_run}`")
    md.append("")

    md.append("## Parameter memory summary")
    md.append("")
    md.append(f"- **Active parameters:** {_fmt(active_count)}")
    md.append(f"- **Exhausted active parameters:** {_fmt(exhausted_count)}")
    md.append(f"- **Temporarily exhausted:** {', '.join(exhausted) if exhausted else 'none'}")
    md.append(f"- **Direction-limited:** {', '.join(direction_limited) if direction_limited else 'none'}")
    md.append(f"- **Latest blocked parameters:** {', '.join(latest_blocked_params) if latest_blocked_params else 'none'}")
    md.append("")

    md.append("## Exhaustion table")
    md.append("")
    md.append(_markdown_table(exhaustion_rows, ["parameter", "bad_increase", "bad_decrease", "status", "avoid"]))
    md.append("")

    md.append("## Recent bad parameter directions")
    md.append("")
    md.append(_markdown_table(bad_rows, ["parameter", "direction", "factor", "score_change", "percent_improvement"]))
    md.append("")

    md.append("## Recommended manual actions")
    md.append("")
    for i, rec in enumerate(recs, 1):
        md.append(f"{i}. {rec}")
    md.append("")

    md.append("## Suggested next technical checks")
    md.append("")
    md.append("1. Inspect `bad_suggestion_memory_V10_9.xlsx` and confirm whether blocked directions are scientifically reasonable.")
    md.append("2. Inspect `parameter_exhaustion_V10_9.xlsx` and decide whether exhausted parameters should remain temporarily excluded.")
    md.append("3. Review `calibration_strategy_V10.xlsx` and activate alternative parameters if the candidate pool is too restricted.")
    md.append("4. Start a new campaign with `--reset-v10-9-campaign` after manual changes are made.")
    md.append("")

    report_file.write_text("\n".join(md), encoding="utf-8")

    row = {
        "timestamp": created_at,
        "context": context,
        "manual_review_report_action": "created_manual_review_report",
        "manual_review_report_file": str(report_file),
        "manual_review_report_reason": stop_reason,
        "manual_review_report_stop_trigger": stop_trigger,
        "latest_run": latest_run,
        "latest_score": latest_score,
        "best_score": best_score,
        "best_run": best_run,
        "exhausted_parameters": ", ".join(exhausted),
        "direction_limited_parameters": ", ".join(direction_limited),
        "latest_blocked_parameters": ", ".join(latest_blocked_params),
    }
    log_file = _append_report_log(results_dir, row)

    return {
        "manual_review_report_action": "created_manual_review_report",
        "manual_review_report_context": context,
        "manual_review_report_file": str(report_file),
        "manual_review_report_log_file": log_file,
        "manual_review_report_reason": stop_reason,
        "manual_review_report_stop_trigger": stop_trigger,
        "manual_review_report_timestamp": created_at,
        "manual_review_exhausted_parameters": ", ".join(exhausted),
        "manual_review_direction_limited_parameters": ", ".join(direction_limited),
        "manual_review_latest_blocked_parameters": ", ".join(latest_blocked_params),
    }
