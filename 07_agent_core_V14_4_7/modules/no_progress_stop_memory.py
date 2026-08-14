from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pandas as pd


VALID_NOT_BEST = "accepted_valid_not_best"
ACCEPTED_BEST = "accepted_best"
REJECTED_SCIENTIFIC = "rejected_scientific"
FAILED = "failed"
CAMPAIGN_START_FILENAME = "V11_campaign_start.txt"


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or pd.isna(value):
            return default
        return int(float(value))
    except Exception:
        return default


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return False
    s = str(value).strip().lower()
    return s in {"true", "1", "yes", "y", "t"}


def _timestamp_now() -> str:
    return pd.Timestamp.now().strftime("%Y-%m-%d %H:%M:%S")


def ensure_campaign_start_marker(
    results_dir: str | Path,
    *,
    reset: bool = False,
    timestamp_value: str | pd.Timestamp | None = None,
) -> Dict[str, Any]:
    """
    Ensure that the V11 campaign start marker exists.

    The marker is used only for no-progress counting. It prevents old rows in
    optimization_history.xlsx from prematurely triggering the no-progress stop
    rule in a new calibration campaign.

    Parameters
    ----------
    results_dir
        Project 04_results directory.
    reset
        If True, overwrite the marker with the current timestamp.
    timestamp_value
        Optional explicit timestamp. Mostly useful for tests or manual resets.
    """
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    marker = results_dir / CAMPAIGN_START_FILENAME

    action = "existing_campaign_start"
    if reset or not marker.exists():
        ts = pd.Timestamp(timestamp_value) if timestamp_value is not None else pd.Timestamp.now()
        marker.write_text(ts.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
        action = "reset_campaign_start" if reset else "created_campaign_start"

    try:
        text = marker.read_text(encoding="utf-8").strip()
        ts = pd.Timestamp(text)
        timestamp_text = ts.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        ts = pd.Timestamp.now()
        timestamp_text = ts.strftime("%Y-%m-%d %H:%M:%S")
        marker.write_text(timestamp_text, encoding="utf-8")
        action = "repaired_campaign_start"

    return {
        "campaign_start_action": action,
        "campaign_start_file": str(marker),
        "campaign_start_timestamp": timestamp_text,
    }


def reset_campaign_start_marker(results_dir: str | Path) -> Dict[str, Any]:
    """Reset the V11 campaign start marker to the current time."""
    return ensure_campaign_start_marker(results_dir, reset=True)


def read_campaign_start_timestamp(results_dir: str | Path) -> pd.Timestamp | None:
    """Read the V11 campaign-start timestamp, creating the marker if missing."""
    info = ensure_campaign_start_marker(results_dir, reset=False)
    try:
        return pd.Timestamp(info.get("campaign_start_timestamp"))
    except Exception:
        return None


def _append_log(results_dir: Path, row: Dict[str, Any]) -> str:
    results_dir = Path(results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)
    out_file = results_dir / "no_progress_stop_memory_V10_9.xlsx"
    df_new = pd.DataFrame([row])
    if out_file.exists():
        try:
            old = pd.read_excel(out_file)
            df_new = pd.concat([old, df_new], ignore_index=True)
        except Exception:
            pass
    df_new.to_excel(out_file, index=False)
    return str(out_file)


def _filter_history_by_campaign(df: pd.DataFrame, campaign_start: pd.Timestamp | None) -> tuple[pd.DataFrame, bool]:
    if campaign_start is None or df.empty or "timestamp" not in df.columns:
        return df, False

    ts = pd.to_datetime(df["timestamp"], errors="coerce")
    filtered = df.loc[ts >= campaign_start].copy()
    return filtered, True


def _consecutive_no_best(
    history_file: Path,
    max_rows: int | None = None,
    campaign_start: pd.Timestamp | None = None,
) -> tuple[int, int, bool]:
    """
    Count consecutive valid-not-best rows since the campaign start marker.

    Returns
    -------
    count, rows_evaluated, campaign_filter_applied
    """
    history_file = Path(history_file)
    if not history_file.exists():
        return 0, 0, False
    try:
        df = pd.read_excel(history_file)
    except Exception:
        return 0, 0, False
    if df.empty or "qc_acceptance_status" not in df.columns:
        return 0, 0, False

    df, campaign_filter_applied = _filter_history_by_campaign(df, campaign_start)
    if df.empty:
        return 0, 0, campaign_filter_applied

    if max_rows is not None and max_rows > 0:
        df = df.tail(max_rows)

    rows_evaluated = len(df)
    count = 0
    for _, row in df.iloc[::-1].iterrows():
        status = str(row.get("qc_acceptance_status", "")).strip()
        run_status = str(row.get("run_status", "")).strip().lower()

        # Only count numerically valid, scientifically accepted runs that did
        # not improve the objective. Stop counting at a new best or invalid run.
        if status == VALID_NOT_BEST:
            if run_status in {"success", "success_with_retries", "partial_success", ""}:
                count += 1
                continue
            break
        if status == ACCEPTED_BEST:
            break
        if status in {REJECTED_SCIENTIFIC, FAILED}:
            break
        # Older rows may not have V10.9 status; ignore trailing empty rows only
        # if no accepted status has been seen yet.
        if not status or status.lower() == "nan":
            if count == 0:
                continue
        break
    return count, rows_evaluated, campaign_filter_applied


def _active_exhaustion_status(config_file: Path, exhaustion_file: Path) -> Dict[str, Any]:
    active_params: list[str] = []
    exhausted_active: list[str] = []
    direction_limited_active: list[str] = []

    try:
        params = pd.read_excel(config_file, sheet_name="parameters")
        if {"parameter", "status"}.issubset(params.columns):
            mask = params["status"].astype(str).str.lower().eq("active")
            active_params = (
                params.loc[mask, "parameter"].astype(str).str.strip().replace("nan", pd.NA).dropna().tolist()
            )
    except Exception:
        active_params = []

    if active_params and Path(exhaustion_file).exists():
        try:
            ex = pd.read_excel(exhaustion_file)
            if {"parameter", "parameter_status"}.issubset(ex.columns):
                tmp = ex.copy()
                tmp["parameter"] = tmp["parameter"].astype(str).str.strip()
                tmp["parameter_status"] = tmp["parameter_status"].astype(str).str.strip()
                exhausted_active = tmp[
                    tmp["parameter"].isin(active_params)
                    & tmp["parameter_status"].eq("temporarily_exhausted")
                ]["parameter"].tolist()
                direction_limited_active = tmp[
                    tmp["parameter"].isin(active_params)
                    & tmp["parameter_status"].eq("direction_limited")
                ]["parameter"].tolist()
        except Exception:
            pass

    active_count = len(active_params)
    exhausted_count = len(set(exhausted_active))
    direction_limited_count = len(set(direction_limited_active))
    active_exhausted_ratio = (exhausted_count / active_count) if active_count else 0.0

    return {
        "active_parameter_count": active_count,
        "exhausted_active_count": exhausted_count,
        "direction_limited_active_count": direction_limited_count,
        "exhausted_active_parameters": ", ".join(sorted(set(exhausted_active))) if exhausted_active else "",
        "direction_limited_active_parameters": ", ".join(sorted(set(direction_limited_active))) if direction_limited_active else "",
        "active_exhausted_ratio": active_exhausted_ratio,
    }


def _blocked_ratio_from_filter_info(filter_info: Dict[str, Any] | None) -> Dict[str, Any]:
    if not isinstance(filter_info, dict):
        return {
            "latest_candidate_count": None,
            "latest_blocked_count": None,
            "latest_kept_count": None,
            "latest_blocked_ratio": None,
            "latest_blocked_parameters": "",
        }

    # Prefer the final filter statistics if available. Otherwise, combine
    # parameter exhaustion and bad-direction blocking counts conservatively.
    candidate_count = _safe_int(filter_info.get("bad_suggestion_candidate_count"), default=0)
    if candidate_count <= 0:
        candidate_count = _safe_int(filter_info.get("parameter_exhaustion_candidate_count"), default=0)

    bad_blocked = _safe_int(filter_info.get("bad_suggestion_blocked_count"), default=0)
    exhausted_blocked = _safe_int(filter_info.get("parameter_exhaustion_blocked_count"), default=0)
    # These filters are sequential; summing is useful for the operational load.
    blocked_count = bad_blocked + exhausted_blocked

    kept_count = _safe_int(filter_info.get("bad_suggestion_kept_count"), default=0)
    if kept_count <= 0:
        kept_count = _safe_int(filter_info.get("parameter_exhaustion_kept_count"), default=0)

    ratio = (blocked_count / candidate_count) if candidate_count > 0 else None

    blocked_params = []
    for key in ["parameter_exhaustion_blocked_parameters", "bad_suggestion_blocked_parameters"]:
        val = filter_info.get(key)
        if val is None or (isinstance(val, float) and pd.isna(val)):
            continue
        for part in str(val).split(","):
            part = part.strip()
            if part:
                blocked_params.append(part)

    return {
        "latest_candidate_count": candidate_count if candidate_count > 0 else None,
        "latest_blocked_count": blocked_count if candidate_count > 0 else None,
        "latest_kept_count": kept_count if candidate_count > 0 else None,
        "latest_blocked_ratio": ratio,
        "latest_blocked_parameters": ", ".join(sorted(set(blocked_params))) if blocked_params else "",
    }


def evaluate_no_progress_stop(
    results_dir: str | Path,
    history_file: str | Path,
    config_file: str | Path,
    *,
    filter_info: Dict[str, Any] | None = None,
    max_consecutive_no_best: int = 5,
    blocked_ratio_limit: float = 0.70,
    min_candidates_for_block_ratio: int = 3,
    stop_if_all_active_exhausted: bool = True,
    context: str = "after_run",
    append_log: bool = True,
    use_campaign_start: bool = True,
) -> Dict[str, Any]:
    """
    V10.9 stop-control memory.

    Stop signals:
    1. No new best after N consecutive valid-but-not-best runs since the V10.9 campaign start.
    2. Candidate filtering is mostly blocked/exhausted.
    3. All active parameters are temporarily exhausted.
    """
    results_dir = Path(results_dir)
    history_file = Path(history_file)
    config_file = Path(config_file)
    exhaustion_file = results_dir / "parameter_exhaustion_V10_9.xlsx"

    campaign = ensure_campaign_start_marker(results_dir) if use_campaign_start else {
        "campaign_start_action": "disabled",
        "campaign_start_file": "",
        "campaign_start_timestamp": "",
    }
    campaign_start = None
    if use_campaign_start:
        try:
            campaign_start = pd.Timestamp(campaign.get("campaign_start_timestamp"))
        except Exception:
            campaign_start = None

    consecutive_no_best, campaign_rows_evaluated, campaign_filter_applied = _consecutive_no_best(
        history_file,
        campaign_start=campaign_start,
    )
    exhaustion = _active_exhaustion_status(config_file, exhaustion_file)
    blocked = _blocked_ratio_from_filter_info(filter_info)

    reasons: list[str] = []
    should_stop = False
    stop_trigger = "none"

    if consecutive_no_best >= int(max_consecutive_no_best):
        should_stop = True
        stop_trigger = "consecutive_no_best"
        reasons.append(
            f"no new best after {consecutive_no_best} consecutive valid run(s) "
            f"since V10.9 campaign start"
        )

    cand = blocked.get("latest_candidate_count")
    ratio = blocked.get("latest_blocked_ratio")
    if cand is not None and ratio is not None:
        if cand >= int(min_candidates_for_block_ratio) and ratio > float(blocked_ratio_limit):
            should_stop = True
            if stop_trigger == "none":
                stop_trigger = "high_blocked_candidate_ratio"
            reasons.append(
                f"blocked/exhausted candidate ratio is {ratio:.1%} "
                f"({blocked.get('latest_blocked_count')}/{cand})"
            )

    active_count = _safe_int(exhaustion.get("active_parameter_count"), 0)
    exhausted_active_count = _safe_int(exhaustion.get("exhausted_active_count"), 0)
    if stop_if_all_active_exhausted and active_count > 0 and exhausted_active_count >= active_count:
        should_stop = True
        if stop_trigger == "none":
            stop_trigger = "all_active_parameters_exhausted"
        reasons.append("all active parameters are temporarily exhausted")

    action = "stop_auto_for_manual_review" if should_stop else "continue_auto"
    reason = "; ".join(reasons) if reasons else "progress stop criteria not triggered"

    row: Dict[str, Any] = {
        "timestamp": _timestamp_now(),
        "context": context,
        "no_progress_stop_action": action,
        "no_progress_stop_trigger": stop_trigger,
        "no_progress_should_stop_auto": should_stop,
        "no_progress_reason": reason,
        "consecutive_no_best_count": consecutive_no_best,
        "max_consecutive_no_best": int(max_consecutive_no_best),
        "blocked_ratio_limit": float(blocked_ratio_limit),
        "min_candidates_for_block_ratio": int(min_candidates_for_block_ratio),
        "campaign_start_action": campaign.get("campaign_start_action"),
        "campaign_start_file": campaign.get("campaign_start_file"),
        "campaign_start_timestamp": campaign.get("campaign_start_timestamp"),
        "campaign_filter_applied": campaign_filter_applied,
        "campaign_rows_evaluated": campaign_rows_evaluated,
        **blocked,
        **exhaustion,
    }

    if append_log:
        row["no_progress_stop_file"] = _append_log(results_dir, row)
    else:
        row["no_progress_stop_file"] = str(results_dir / "no_progress_stop_memory_V10_9.xlsx")

    return row
