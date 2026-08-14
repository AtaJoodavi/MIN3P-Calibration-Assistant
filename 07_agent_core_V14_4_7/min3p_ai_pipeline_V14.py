from __future__ import annotations
"""
MIN3P AI Pipeline V14
======================

V14 is an incremental extension of V13.5.  It deliberately keeps the V13.5
candidate execution and rollback loop intact while adding protected integration
points for:

- adaptive step sizing,
- parameter runtime states and safe reactivation,
- bounded interaction-pair search,
- residual-pattern and numerical diagnostics,
- GPT Scientific Supervisor advisory evidence,
- separate V14 audit files.

This file is intentionally an orchestration layer.  Scientific behaviour that
selects and evaluates a candidate remains inside the coordinate optimiser.

Important compatibility contract for modules.adaptive_coordinate_optimizer_V14:

    AdaptiveCoordinateOptimizerV14(paths, config)
        .initialize(score, run_folder, baseline_source=...)
        .next_suggestion() -> pandas.DataFrame
        .observe_run(score, run_folder, scientific_ok, scientific_penalty,
                     valid, diagnostics, min3p_run_status) -> dict
        .finalize_decision(event, restoration_verified) -> dict

The V14 optimiser must preserve the V13.5 directional invariants:

1. A pending direction pair has precedence over every other candidate.
2. Rejected increase -> immediately test decrease of the same parameter.
3. Rejected decrease -> immediately test increase of the same parameter.
4. Accepted increase/decrease continues in the same direction until bounded,
   rejected, numerically unsafe, or the optimiser's evidence rules say stop.
5. No GPT, interaction-pair, state-management, or group-selection logic can
   interrupt an incomplete individual direction pair.

Until the V14 optimiser module exists, --mode preflight works and auto mode is
blocked by default.  The explicit --allow-v13-5-fallback flag exists only for
controlled regression testing of the inherited V13.5 loop.  It does NOT claim
that V14 step, state, interaction, diagnostic, or GPT features are active.
"""

import argparse
import importlib
import json
import traceback
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from min3p_ai_pipeline_V13_5 import V135Workflow, VALID
from modules.history import rank_runs, update_history
from modules.dat_builder import DatBuilder
from modules.evaluator import ResultEvaluator
from modules.log_parser import analyze_run
from modules.v14_transaction_manager import (
    CandidateTransaction,
    TransactionError,
    TransactionRecoveryRequired,
    V14TransactionManager,
)
from modules.v14_transaction_runner import CandidateRunCancelled, V14TransactionalMin3pRunner
from modules.io_utils import log
from modules.v14_utils import atomic_copy_file, write_excel_atomic, write_json_atomic
from modules.v13_5_objective_diagnostics import (
    evaluate_scientific_constraints,
    write_candidate_diagnostics,
)
from modules.v13_5_parameter_state import (
    metadata as v13_5_metadata,
    restore_best as v13_5_restore_best,
)


V14_VERSION = "V14.4.7"
_REQUIRED_V14_COMPONENTS = {
    "optimizer": ("modules.adaptive_coordinate_optimizer_V14", "AdaptiveCoordinateOptimizerV14"),
    "state_manager": ("modules.parameter_state_manager", "ParameterStateManager"),
    "step_controller": ("modules.step_size_controller", "StepSizeController"),
    "pair_optimizer": ("modules.parameter_pair_optimizer", "ParameterPairOptimizer"),
    "diagnostics": ("modules.calibration_diagnostics", "CalibrationDiagnostics"),
    "stability": ("modules.numerical_stability_checker", "NumericalStabilityChecker"),
    "plausibility": ("modules.scientific_plausibility_checker", "ScientificPlausibilityChecker"),
    "strategy": ("modules.calibration_strategy_manager", "CalibrationStrategyManager"),
    "history": ("modules.history_manager", "V14HistoryManager"),
    "report": ("modules.report_generator_V14", "write_v14_calibration_report"),
}
_OPTIONAL_V14_COMPONENTS = {
    "gpt_supervisor": ("modules.gpt_scientific_supervisor", "GPTScientificSupervisorV14"),
}


def _utc_now() -> str:
    """Return a timezone-aware audit timestamp."""
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _json_safe(value: Any) -> Any:
    """Convert common pandas/path/numpy values into JSON-safe representations."""
    if value is None:
        return None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and pd.isna(value):
            return None
        return value
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return str(value)


def _safe_float(value: Any) -> float | None:
    """Return a finite float, otherwise None."""
    try:
        if value is None or pd.isna(value):
            return None
        value = float(value)
        return value if pd.notna(value) else None
    except Exception:
        return None


class V14Workflow(V135Workflow):
    """
    V14 workflow.

    The inherited V13.5 class remains responsible for the known-good baseline
    discovery, MIN3P execution (`_v13_single_cycle`), and directional optimiser
    compatibility.  This class adds V14-only state and audit outputs; it never
    replaces or overwrites V13.5 historical files.
    """

    def __init__(self, reset_campaign_start: bool = False):
        super().__init__(reset_campaign_start=reset_campaign_start)

        self.v14_results_dir = self.paths.results_dir
        self.v14_reports_dir = self.paths.reports_dir
        self.v14_campaign_state_file = self.v14_results_dir / "v14_campaign_state.json"
        self.v14_best_file = self.v14_results_dir / "best_parameters_V14.xlsx"
        self.v14_best_metadata_file = self.v14_results_dir / "best_parameters_V14_metadata.json"
        self.v14_history_file = self.v14_results_dir / "optimization_history_V14.xlsx"
        self.v14_state_history_file = self.v14_results_dir / "parameter_state_history.xlsx"
        self.v14_interaction_history_file = self.v14_results_dir / "interaction_history.xlsx"
        self.v14_decision_log_file = self.v14_results_dir / "calibration_decision_log.xlsx"
        self.v14_gpt_log_file = self.v14_results_dir / "gpt_supervisor_log.jsonl"
        self.v14_report_file = self.v14_reports_dir / "V14_calibration_report.md"
        # V14.3.7: candidate workbooks/DAT files run in isolated transactions.
        # The canonical agent_config.xlsx remains the accepted V14 best state.
        self.transaction_manager = V14TransactionManager(
            self.paths,
            best_config_file=self.v14_best_file,
        )

        self.v14_results_dir.mkdir(parents=True, exist_ok=True)
        self.v14_reports_dir.mkdir(parents=True, exist_ok=True)

        self.v14_components: dict[str, Any] = {}
        self.v14_component_status: dict[str, dict[str, Any]] = {}
        self._load_v14_components()
        self.optimizer_v14, self.using_v13_5_optimizer_fallback = self._build_v14_optimizer()
        self._latest_gpt_advice: dict[str, Any] | None = None

        self._attach_v14_services_to_optimizer()

    # ------------------------------------------------------------------
    # Component discovery and controlled dependency injection
    # ------------------------------------------------------------------

    def _load_component(
        self,
        key: str,
        module_name: str,
        attribute_name: str,
        *,
        required: bool,
    ) -> None:
        """Load a V14 component without making preflight unusable."""
        try:
            module = importlib.import_module(module_name)
            component = getattr(module, attribute_name)
            self.v14_components[key] = component
            self.v14_component_status[key] = {
                "available": True,
                "module": module_name,
                "attribute": attribute_name,
                "required": required,
                "reason": "",
            }
        except Exception as exc:
            self.v14_component_status[key] = {
                "available": False,
                "module": module_name,
                "attribute": attribute_name,
                "required": required,
                "reason": f"{type(exc).__name__}: {exc}",
            }

    def _load_v14_components(self) -> None:
        """Discover current V14 modules. Missing modules are reported by preflight."""
        for key, (module_name, attribute_name) in _REQUIRED_V14_COMPONENTS.items():
            self._load_component(key, module_name, attribute_name, required=True)
        for key, (module_name, attribute_name) in _OPTIONAL_V14_COMPONENTS.items():
            self._load_component(key, module_name, attribute_name, required=False)

    def _instantiate_component(self, key: str) -> Any | None:
        """Instantiate class components using the standard `(paths, config)` contract."""
        component = self.v14_components.get(key)
        if component is None:
            return None
        if not isinstance(component, type):
            return component
        try:
            return component(self.paths, self.config)
        except TypeError:
            # Only allow a no-argument fallback for report/helper components.
            try:
                return component()
            except Exception as exc:
                log(self.paths, f"WARNING: V14 component '{key}' could not be instantiated: {exc}")
                return None
        except Exception as exc:
            log(self.paths, f"WARNING: V14 component '{key}' could not be instantiated: {exc}")
            return None

    def _build_v14_optimizer(self) -> tuple[Any, bool]:
        """Prefer the V14 optimiser; retain V13.5 only as an explicit test fallback."""
        optimizer_cls = self.v14_components.get("optimizer")
        if optimizer_cls is None:
            return self.optimizer_v13_5, True

        try:
            return optimizer_cls(self.paths, self.config), False
        except Exception as exc:
            log(
                self.paths,
                "WARNING: V14 optimiser could not be created; V13.5 fallback is "
                f"available only with --allow-v13-5-fallback. Cause: {exc}",
            )
            return self.optimizer_v13_5, True

    def _attach_v14_services_to_optimizer(self) -> None:
        """
        Give a completed V14 optimiser access to services without changing its
        core API.  Implement `attach_services(**services)` in the optimiser when
        V14 modules are created.
        """
        if self.using_v13_5_optimizer_fallback:
            return

        services = {
            "state_manager": self._instantiate_component("state_manager"),
            "step_controller": self._instantiate_component("step_controller"),
            "pair_optimizer": self._instantiate_component("pair_optimizer"),
            "diagnostics": self._instantiate_component("diagnostics"),
            "stability_checker": self._instantiate_component("stability"),
            "plausibility_checker": self._instantiate_component("plausibility"),
            "strategy_manager": self._instantiate_component("strategy"),
            "history_manager": self._instantiate_component("history"),
        }
        services = {key: value for key, value in services.items() if value is not None}

        if hasattr(self.optimizer_v14, "attach_services"):
            try:
                self.optimizer_v14.attach_services(**services)
            except Exception as exc:
                log(self.paths, f"WARNING: V14 optimiser service attachment failed: {exc}")

    # ------------------------------------------------------------------
    # Separate V14 campaign best-state handling
    # ------------------------------------------------------------------

    def _read_json(self, path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
        if not path.exists():
            return default or {}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            return loaded if isinstance(loaded, dict) else (default or {})
        except Exception as exc:
            log(self.paths, f"WARNING: could not read JSON state {path.name}: {exc}")
            return default or {}

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> Path:
        """Safely replace JSON state with retry/backoff for transient Windows locks."""
        return write_json_atomic(Path(path), _json_safe(payload))

    def _read_v14_state(self) -> dict[str, Any]:
        return self._read_json(self.v14_campaign_state_file, default={})

    def _write_v14_state(self, **updates: Any) -> dict[str, Any]:
        state = self._read_v14_state()
        state.update(_json_safe(updates))
        state["updated_at"] = _utc_now()
        state["optimizer_version"] = V14_VERSION
        self._write_json_atomic(self.v14_campaign_state_file, state)
        return state

    def _read_v14_best_metadata(self) -> dict[str, Any]:
        return self._read_json(self.v14_best_metadata_file, default={})

    def _save_v14_best(
        self,
        *,
        total_score: float,
        run_folder: str,
        stage: str,
        diagnostics: dict[str, Any] | None = None,
        baseline_source: str = "",
        source_config_file: Path | None = None,
    ) -> None:
        """
        Snapshot the current agent configuration as a V14-only best state.

        This deliberately does not call V13.5 save_best: V14 must not overwrite
        V13.5 best-memory files.  The configuration workbook is copied byte-for-
        byte, and metadata are stored in a V14 JSON sidecar.
        """
        source = Path(source_config_file or self.paths.config_file)
        if not source.exists():
            raise FileNotFoundError(f"Cannot save V14 best: config file not found: {source}")

        atomic_copy_file(source, self.v14_best_file)

        best_metadata = {
            "optimizer_version": V14_VERSION,
            "total_score": total_score,
            "run_folder": str(run_folder),
            "stage": str(stage),
            "saved_at": _utc_now(),
            "objective_reference_file": str(self.paths.results_dir / "objective_reference_V13.xlsx"),
            "baseline_source": baseline_source,
            "diagnostics": _json_safe(diagnostics or {}),
        }
        self._write_json_atomic(self.v14_best_metadata_file, best_metadata)
        self._write_v14_state(
            current_best_score=total_score,
            current_best_run_folder=str(run_folder),
            current_best_stage=str(stage),
            best_parameters_file=str(self.v14_best_file),
            baseline_source=baseline_source,
        )

    def _restore_v14_best(self) -> None:
        """Restore exact V14 best workbook to active agent_config.xlsx."""
        if not self.v14_best_file.exists():
            raise FileNotFoundError(
                f"V14 best configuration does not exist: {self.v14_best_file}"
            )
        atomic_copy_file(self.v14_best_file, Path(self.paths.config_file))

    def _v14_current_best(self) -> tuple[float | None, str]:
        state = self._read_v14_state()
        score = _safe_float(state.get("current_best_score"))
        run = str(state.get("current_best_run_folder") or "")
        if score is None:
            meta = self._read_v14_best_metadata()
            score = _safe_float(meta.get("total_score"))
            run = run or str(meta.get("run_folder") or "")
        return score, run

    def _ensure_v14_baseline(self) -> bool:
        """
        Establish a V14 baseline by copying the verified V13.5 best state.

        V13.5 provides the trusted objective and safe best configuration.  V14
        creates its own snapshot from that configuration; it does not edit V13.5
        state files.
        """
        score, run = self._v14_current_best()
        if score is not None and run and self.v14_best_file.exists():
            return True

        if not self._ensure_v13_5_baseline():
            return False

        # Explicitly restore the V13.5 best before taking the V14 snapshot.
        try:
            v13_5_restore_best(self.paths.config_file, self.paths.results_dir)
        except Exception as exc:
            log(self.paths, f"WARNING: could not restore V13.5 best before V14 baseline: {exc}")
            return False

        meta = {}
        try:
            meta = v13_5_metadata(self.paths.results_dir) or {}
        except Exception as exc:
            log(self.paths, f"WARNING: could not read V13.5 best metadata: {exc}")

        score = _safe_float(
            meta.get("total_score", meta.get("TOTAL_SCORE", meta.get("current_best_score")))
        )
        run = str(
            meta.get("run_folder", meta.get("source_run_folder", meta.get("current_best_run_folder", "")))
            or ""
        )
        if score is None or not run:
            ranking_score, ranking_run = self._ranking_best()
            score = score if score is not None else ranking_score
            run = run or str(ranking_run or "")

        if score is None or not run:
            log(self.paths, "V14 baseline could not be established from V13.5 best state.")
            return False

        try:
            self._save_v14_best(
                total_score=score,
                run_folder=run,
                stage="baseline",
                baseline_source="verified_V13_5_best_memory",
            )
        except Exception as exc:
            log(self.paths, f"V14 baseline snapshot failed: {exc}")
            return False

        if not self.using_v13_5_optimizer_fallback:
            try:
                existing = (
                    self.optimizer_v14._read_state()
                    if hasattr(self.optimizer_v14, "_read_state")
                    else {}
                )
                current = (
                    _safe_float(existing.get("current_best_score"))
                    if isinstance(existing, dict)
                    else None
                )
                if current is None and hasattr(self.optimizer_v14, "initialize"):
                    self.optimizer_v14.initialize(
                        score, run, baseline_source="verified_V13_5_best_memory"
                    )
            except Exception as exc:
                log(self.paths, f"WARNING: V14 optimiser baseline initialization failed: {exc}")
                return False

        self._audit_state_snapshot(
            action="baseline_initialized",
            event={
                "accepted": True,
                "parameter": "",
                "group": "baseline",
                "baseline_TOTAL_SCORE": score,
                "candidate_TOTAL_SCORE": score,
            },
        )
        return True

    # ------------------------------------------------------------------
    # Audit output helpers: V14 files only, never overwrite V13.5 history
    # ------------------------------------------------------------------

    def _append_excel_safe(
        self,
        path: Path,
        rows: list[dict[str, Any]] | pd.DataFrame,
        *,
        label: str,
    ) -> Path:
        """
        Append rows by rebuilding an isolated workbook atomically.

        If Excel has the workbook open, a clearly named pending workbook is
        written instead.  The active V13/V13.5 history remains untouched.
        """
        new = rows.copy() if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
        if new.empty:
            return path
        new = new.map(_json_safe) if hasattr(new, "map") else new.applymap(_json_safe)

        try:
            if path.exists():
                existing = pd.read_excel(path)
                if existing.empty or existing.dropna(how="all").empty:
                    out = new.copy()
                else:
                    # pandas 2.x warns about future dtype inference changes when
                    # some audit columns are entirely NA. This audit workbook is
                    # schema-flexible by design; suppress only that compatibility
                    # warning while preserving the existing append semantics.
                    with warnings.catch_warnings():
                        warnings.filterwarnings(
                            "ignore",
                            message="The behavior of DataFrame concatenation with empty or all-NA entries is deprecated.*",
                            category=FutureWarning,
                        )
                        out = pd.concat([existing.dropna(how="all"), new], ignore_index=True, sort=False)
            else:
                out = new.copy()

            write_excel_atomic(path, out)
            return path
        except PermissionError:
            pending = path.with_name(
                f"{path.stem}_PENDING_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
            )
            new.to_excel(pending, index=False)
            log(
                self.paths,
                f"WARNING: {label} is open or locked. Wrote pending V14 audit file: {pending}",
            )
            return pending
        except Exception as exc:
            log(self.paths, f"WARNING: could not append {label}: {exc}")
            return path

    def _append_jsonl(self, path: Path, record: dict[str, Any]) -> None:
        """Append a single JSON audit record.  Never include API keys or secrets."""
        safe = _json_safe(record)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, ensure_ascii=False) + "\n")

    def _optimizer_state_for_audit(self) -> dict[str, Any]:
        if hasattr(self.optimizer_v14, "_read_state"):
            try:
                state = self.optimizer_v14._read_state()
                return _json_safe(state if isinstance(state, dict) else {})
            except Exception as exc:
                return {"state_read_error": f"{type(exc).__name__}: {exc}"}
        return {}

    def _audit_state_snapshot(self, *, action: str, event: dict[str, Any]) -> None:
        """Write a separate V14 runtime state history record."""
        state = self._read_v14_state()
        optimizer_state = self._optimizer_state_for_audit()
        row = {
            "timestamp": _utc_now(),
            "optimizer_version": V14_VERSION,
            "action": action,
            "runtime_mode": (
                "V13.5_fallback_regression_only"
                if self.using_v13_5_optimizer_fallback
                else "V14"
            ),
            "parameter": event.get("parameter", ""),
            "group": event.get("group", ""),
            "direction": event.get("direction_label", event.get("direction", "")),
            "accepted": event.get("accepted"),
            "decision": event.get("decision", event.get("action", "")),
            "current_best_score": state.get("current_best_score"),
            "current_best_run_folder": state.get("current_best_run_folder"),
            "state_json": json.dumps(_json_safe(state), ensure_ascii=False),
            "optimizer_state_json": json.dumps(optimizer_state, ensure_ascii=False),
        }
        self._append_excel_safe(
            self.v14_state_history_file, [row], label="parameter_state_history.xlsx"
        )

    def _audit_interaction(
        self,
        *,
        action: str,
        suggestion: pd.Series | None,
        event: dict[str, Any] | None = None,
        reason: str = "",
    ) -> None:
        """Create an interaction audit record even before pair optimisation exists."""
        suggestion = suggestion if suggestion is not None else pd.Series(dtype=object)
        event = event or {}
        row = {
            "timestamp": _utc_now(),
            "optimizer_version": V14_VERSION,
            "action": action,
            "pair_id": suggestion.get("pair_id", ""),
            "parameter_a": suggestion.get("parameter_a", suggestion.get("parameter", "")),
            "parameter_b": suggestion.get("parameter_b", ""),
            "direction_pattern": suggestion.get("pair_direction", suggestion.get("direction_label", "")),
            "enabled": suggestion.get("interaction_enabled", False),
            "accepted": event.get("accepted"),
            "decision": event.get("decision", event.get("action", "")),
            "reason": reason,
            "event_json": json.dumps(_json_safe(event), ensure_ascii=False),
        }
        self._append_excel_safe(
            self.v14_interaction_history_file, [row], label="interaction_history.xlsx"
        )

    def _audit_decision(
        self,
        *,
        phase: str,
        suggestion: pd.Series | None,
        event: dict[str, Any] | None,
        diagnostics: dict[str, Any] | None,
        restoration_verified: bool | None,
        decision_source: str,
        notes: str = "",
    ) -> None:
        """Persist one traceable candidate-selection or post-run decision."""
        suggestion = suggestion if suggestion is not None else pd.Series(dtype=object)
        event = event or {}
        diagnostics = diagnostics or {}
        audit_event = dict(event)
        for key in (
            "move_space",
            "step_basis",
            "range_normalized",
            "configured_log_range_decades",
            "factor_applied",
            "initial_search_mode",
            "initial_search_source",
            "initial_factor_requested",
            "initial_range_fraction_requested",
            "initial_step_fraction_resolved",
            "initial_step_fraction_clamped",
            "initial_factor_effective",
            "cache_hit",
            "cache_source_candidate_id",
            "cache_source_transaction",
            "cache_source",
            "priority_local_refinement",
            "priority_refinement_rounds_remaining",
            "next_step_fraction",
        ):
            value = suggestion.get(key) if key in suggestion.index else None
            if value is not None and not (isinstance(value, float) and pd.isna(value)):
                audit_event.setdefault(key, _json_safe(value))
        best_score, best_run = self._v14_current_best()

        row = {
            "timestamp": _utc_now(),
            "optimizer_version": V14_VERSION,
            "phase": phase,
            "decision_source": decision_source,
            "candidate_id": suggestion.get("candidate_id", event.get("candidate_id", "")),
            "candidate_type": suggestion.get("candidate_type", "single_parameter"),
            "parameter": suggestion.get("parameter", event.get("parameter", "")),
            "group": suggestion.get("group", event.get("group", "")),
            "direction": suggestion.get("direction_label", suggestion.get("direction", event.get("direction", ""))),
            "old_value": suggestion.get("old_value"),
            "new_value": suggestion.get("new_value"),
            "step_fraction": suggestion.get(
                "step_fraction_applied", suggestion.get("step_fraction", event.get("step_fraction"))
            ),
            "move_space": suggestion.get("move_space", event.get("move_space", "")),
            "step_basis": suggestion.get("step_basis", event.get("step_basis", "")),
            "range_normalized": suggestion.get(
                "range_normalized", event.get("range_normalized")
            ),
            "configured_log_range_decades": suggestion.get(
                "configured_log_range_decades", event.get("configured_log_range_decades")
            ),
            "factor_applied": suggestion.get(
                "factor_applied", event.get("factor_applied")
            ),
            "initial_search_mode": suggestion.get(
                "initial_search_mode", event.get("initial_search_mode", "automatic_fallback")
            ),
            "initial_search_source": suggestion.get(
                "initial_search_source", event.get("initial_search_source", "automatic_global_fallback")
            ),
            "initial_factor_requested": suggestion.get(
                "initial_factor_requested", event.get("initial_factor_requested")
            ),
            "initial_range_fraction_requested": suggestion.get(
                "initial_range_fraction_requested", event.get("initial_range_fraction_requested")
            ),
            "initial_step_fraction_resolved": suggestion.get(
                "initial_step_fraction_resolved", event.get("initial_step_fraction_resolved")
            ),
            "initial_step_fraction_clamped": suggestion.get(
                "initial_step_fraction_clamped", event.get("initial_step_fraction_clamped", False)
            ),
            "initial_factor_effective": suggestion.get(
                "initial_factor_effective", event.get("initial_factor_effective")
            ),
            "priority_local_refinement": suggestion.get(
                "priority_local_refinement", event.get("priority_local_refinement", False)
            ),
            "priority_refinement_rounds_remaining": suggestion.get(
                "priority_refinement_rounds_remaining",
                event.get("priority_refinement_rounds_remaining", 0),
            ),
            "next_step_fraction": event.get("next_step_fraction"),
            "cache_hit": bool(event.get("cache_hit", False)),
            "cache_source_candidate_id": event.get("cache_source_candidate_id", ""),
            "cache_source_transaction": event.get("cache_source_transaction", ""),
            "cache_source": event.get("cache_source", ""),
            "baseline_objective": event.get(
                "baseline_TOTAL_SCORE", event.get("baseline_objective")
            ),
            "candidate_objective": event.get(
                "candidate_TOTAL_SCORE", event.get("candidate_objective")
            ),
            "effective_objective": event.get(
                "effective_TOTAL_SCORE", event.get("effective_objective")
            ),
            "accepted": event.get("accepted"),
            "decision": event.get("decision", event.get("action", "")),
            "rejection_reason": event.get("rejection_reason", ""),
            "restoration_verified": restoration_verified,
            "current_best_objective": best_score,
            "current_best_run_folder": best_run,
            "scientific_ok": diagnostics.get("scientific_ok"),
            "scientific_penalty": diagnostics.get("scientific_penalty"),
            "constraint_failures": diagnostics.get("constraint_failures", ""),
            "tradeoff_class": diagnostics.get("tradeoff_class", ""),
            "numerical_status": diagnostics.get("v14_numerical_status", ""),
            "plausibility_status": diagnostics.get("v14_plausibility_status", ""),
            "residual_status": diagnostics.get("v14_residual_status", ""),
            "gpt_advisory_available": bool(self._latest_gpt_advice),
            "notes": notes,
            "event_json": json.dumps(_json_safe(audit_event), ensure_ascii=False),
            "diagnostics_json": json.dumps(_json_safe(diagnostics), ensure_ascii=False),
        }
        self._append_excel_safe(
            self.v14_decision_log_file, [row], label="calibration_decision_log.xlsx"
        )
        self._append_excel_safe(
            self.v14_history_file, [row], label="optimization_history_V14.xlsx"
        )

    # ------------------------------------------------------------------
    # Diagnostics and deterministic candidate selection
    # ------------------------------------------------------------------

    def _call_optional_method(
        self,
        component_key: str,
        method_names: list[str],
        **kwargs: Any,
    ) -> tuple[Any | None, str]:
        """
        Call a V14 service when it is present.

        Services should accept keyword arguments.  A service failure is logged
        and represented in the audit trail; it never bypasses V13.5 safety.
        """
        component = self._instantiate_component(component_key)
        if component is None:
            return None, "component_unavailable"

        for name in method_names:
            method = getattr(component, name, None)
            if callable(method):
                try:
                    return method(**kwargs), "available"
                except TypeError:
                    # Surface incompatible module contracts explicitly.
                    return None, f"incompatible_method_signature:{component_key}.{name}"
                except Exception as exc:
                    log(self.paths, f"WARNING: V14 {component_key}.{name} failed: {exc}")
                    return None, f"component_error:{type(exc).__name__}:{exc}"
        return None, "method_not_implemented"

    def _candidate_scientific_diagnostics(
        self,
        *,
        run_dir: Path,
        score: float | None,
        valid: bool,
        baseline_score: float | None,
    ) -> dict[str, Any]:
        """
        Preserve the V13.5 scientific constraint result, then add V14 diagnostic
        evidence as structured fields.  New V14 evidence may flag a candidate
        but cannot make an invalid run acceptable.
        """
        preliminary: dict[str, Any] = {
            "scientific_ok": True,
            "scientific_penalty": 0.0,
            "constraint_failures": "",
            "tradeoff_class": "candidate_run_invalid" if not valid else "",
            "v14_residual_status": "not_available",
            "v14_numerical_status": "not_available",
            "v14_plausibility_status": "not_available",
        }

        ranking_row = None
        if valid:
            try:
                ranking = pd.read_excel(self.paths.ranking_file)
                if "run_folder" in ranking.columns:
                    matches = ranking[
                        ranking["run_folder"].astype(str).str.contains(
                            run_dir.name, regex=False, na=False
                        )
                    ]
                    if not matches.empty:
                        ranking_row = matches.iloc[0]
                preliminary.update(
                    evaluate_scientific_constraints(ranking_row, self.config)
                )
            except Exception as exc:
                preliminary["constraint_failures"] = (
                    f"V13.5 scientific-constraint evaluation failed: {exc}"
                )
                preliminary["scientific_ok"] = False

        residual, residual_status = self._call_optional_method(
            "diagnostics",
            ["analyze", "diagnose", "evaluate"],
            paths=self.paths,
            config=self.config,
            baseline_score=baseline_score,
            candidate_score=score,
            baseline_run_folder=self._v14_current_best()[1],
            candidate_run_folder=str(run_dir),
            ranking_row=ranking_row,
        )
        preliminary["v14_residual_status"] = residual_status
        preliminary["v14_residual_diagnostics"] = _json_safe(residual or {})

        numerical, numerical_status = self._call_optional_method(
            "stability",
            ["evaluate", "check", "assess"],
            paths=self.paths,
            config=self.config,
            run_folder=str(run_dir),
            score=score,
            run_valid=valid,
        )
        preliminary["v14_numerical_status"] = numerical_status
        preliminary["v14_numerical_diagnostics"] = _json_safe(numerical or {})

        # A numerical checker may make a deterministic hard-reject finding.
        # Soft warnings remain evidence for the optimiser and report only.
        if isinstance(numerical, dict) and numerical.get("hard_reject") is True:
            preliminary["scientific_ok"] = False
            preliminary["constraint_failures"] = (
                str(preliminary.get("constraint_failures", "")).strip("; ")
                + "; V14 numerical hard-reject"
            ).strip("; ")
            preliminary["tradeoff_class"] = "numerical_instability"

        plausibility, plausibility_status = self._call_optional_method(
            "plausibility",
            ["evaluate", "check", "assess"],
            paths=self.paths,
            config=self.config,
            run_folder=str(run_dir),
            score=score,
            residual_diagnostics=residual or {},
            numerical_diagnostics=numerical or {},
        )
        preliminary["v14_plausibility_status"] = plausibility_status
        preliminary["v14_plausibility_diagnostics"] = _json_safe(plausibility or {})

        # Only an explicit deterministic hard constraint can reject a candidate.
        if isinstance(plausibility, dict) and plausibility.get("hard_reject") is True:
            preliminary["scientific_ok"] = False
            preliminary["constraint_failures"] = (
                str(preliminary.get("constraint_failures", "")).strip("; ")
                + "; V14 physical-plausibility hard-reject"
            ).strip("; ")
            preliminary["tradeoff_class"] = "physical_plausibility_failure"

        return preliminary

    def _validate_candidate_precedence(self, suggestion: pd.DataFrame) -> tuple[bool, str]:
        """
        Do not introduce a second candidate selector outside the optimiser.

        A completed V14 optimiser can expose `validate_suggestion_precedence`.
        Its positive validation is required for V14 auto mode.  The V13.5
        fallback is accepted only when explicitly enabled for regression testing.
        """
        if suggestion is None or suggestion.empty:
            return False, "empty_suggestion"

        if self.using_v13_5_optimizer_fallback:
            return True, "delegated_to_verified_V13_5_optimizer"

        validator = getattr(self.optimizer_v14, "validate_suggestion_precedence", None)
        if not callable(validator):
            return False, "V14_optimizer_missing_validate_suggestion_precedence"

        try:
            result = validator(suggestion=suggestion)
        except Exception as exc:
            return False, f"V14_precedence_validation_error:{type(exc).__name__}:{exc}"

        if isinstance(result, dict):
            return bool(result.get("ok")), str(result.get("reason", ""))
        return bool(result), "V14_optimizer_validation"

    def _next_v14_suggestion(self) -> tuple[pd.DataFrame, str]:
        """
        Select the next legal candidate.

        The optimiser is the only source of candidate selection.  This avoids
        accidental interruption of a pending V13.5 direction pair by a state,
        pair, GPT, or reporting layer.
        """
        if not hasattr(self.optimizer_v14, "next_suggestion"):
            return pd.DataFrame(), "optimizer_has_no_next_suggestion"

        try:
            suggestion = self.optimizer_v14.next_suggestion()
        except Exception as exc:
            log(self.paths, f"WARNING: V14 next_suggestion failed: {exc}")
            return pd.DataFrame(), f"optimizer_error:{type(exc).__name__}:{exc}"

        if suggestion is None:
            return pd.DataFrame(), "optimizer_returned_none"
        if not isinstance(suggestion, pd.DataFrame):
            return pd.DataFrame(), "optimizer_returned_non_dataframe"

        ok, reason = self._validate_candidate_precedence(suggestion)
        if not ok:
            return pd.DataFrame(), reason
        return suggestion, reason

    # ------------------------------------------------------------------
    # V14.3.7 transactional candidate execution and safe interruption
    # ------------------------------------------------------------------

    def _v14_transactional_single_cycle(self, transaction: CandidateTransaction) -> dict[str, Any]:
        """Run MIN3P from an isolated candidate workbook/DAT file.

        The method intentionally mirrors the trusted V13 single-cycle sequence,
        but it uses a transaction-specific ConfigReader.  The canonical
        ``agent_config.xlsx`` is never written during candidate preparation,
        execution, or rejection.
        """
        candidate_config = self.transaction_manager.candidate_config_reader(transaction)
        builder = DatBuilder(self.paths, candidate_config)
        runner = V14TransactionalMin3pRunner(self.paths, candidate_config)
        evaluator = ResultEvaluator(self.paths, candidate_config)

        dat_file = builder.build()
        self.transaction_manager.mark_running(transaction)
        run_dir, results_dir, return_code = runner.run(dat_file)

        diagnostic = analyze_run(run_dir, results_dir, return_code, candidate_config)
        diagnostic["run_folder"] = str(run_dir)
        diagnostic["results_folder"] = str(results_dir) if results_dir else ""
        diagnostic["candidate_execution_mode"] = getattr(
            runner, "execution_mode", "unknown"
        )

        if results_dir is not None and diagnostic.get("run_status") in VALID:
            try:
                metrics_file, timeseries_file = evaluator.compare(results_dir)
                diagnostic["objective_metrics_file"] = str(metrics_file)
                diagnostic["objective_timeseries_file"] = str(timeseries_file)
            except Exception as exc:
                diagnostic["objective_extraction_error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
                log(
                    self.paths,
                    f"WARNING: {V14_VERSION} fast candidate objective extraction failed: {exc}",
                )

        update_history(self.paths, candidate_config, run_dir, results_dir, diagnostic)
        rank_runs(self.paths, candidate_config)
        diagnostic["TOTAL_SCORE"] = self._get_current_run_score(run_dir)
        self.transaction_manager.attach_run_artifacts(
            transaction,
            run_folder=run_dir,
            run_status=str(diagnostic.get("run_status", "")),
            return_code=return_code,
        )
        return diagnostic

    def _v14_cancelled_transaction_event(
        self,
        *,
        transaction: CandidateTransaction,
        row: pd.Series,
        reason: str,
        run_folder: str = "",
    ) -> dict[str, Any]:
        """Recover pre-selection optimizer state and write a cancellation audit."""
        recovery = self.transaction_manager.cancel_and_recover(
            transaction,
            reason=reason,
        )
        hashes = self.transaction_manager.canonical_best_hashes()
        event = {
            "optimizer_version": V14_VERSION,
            "candidate_id": str(row.get("candidate_id", "")),
            "candidate_type": str(row.get("candidate_type", "single_parameter")),
            "parameter": str(row.get("parameter", "")),
            "group": str(row.get("group", "")),
            "direction": str(row.get("direction_label", row.get("direction", ""))),
            "baseline_TOTAL_SCORE": self._v14_current_best()[0],
            "candidate_TOTAL_SCORE": None,
            "accepted": False,
            "decision": "cancelled_before_candidate_evaluation",
            "rejection_reason": reason,
            "run_folder": run_folder,
            "restoration_verified": bool(hashes.get("match")),
            "transaction_recovery": _json_safe(recovery.get("recovery", {})),
        }
        self._audit_decision(
            phase="candidate_cancelled",
            suggestion=row,
            event=event,
            diagnostics=None,
            restoration_verified=bool(hashes.get("match")),
            decision_source="transaction_recovery",
            notes=reason,
        )
        self._audit_state_snapshot(action="candidate_cancelled", event=event)
        return event

    def recover_interrupted_transactions_v14(self) -> dict[str, Any]:
        """Recover unambiguous interrupted transactions using the pre-selection checkpoint."""
        summary = self.transaction_manager.recover_interrupted_transactions()
        self._write_json_atomic(
            self.v14_results_dir / "V14_interrupted_recovery_summary.json",
            summary,
        )
        return summary

    def postprocess_v14_best(self) -> dict[str, Any]:
        """Create the complete plots/report bundle for the current V14 best run.

        Candidate evaluations intentionally use only MIN3P plus compact GBT/GBM
        objective processing. This explicit mode performs the heavy full
        plotsV46.py post-processing without rerunning MIN3P.
        """
        _score, best_run = self._v14_current_best()
        run_dir = Path(best_run)
        if not best_run or not run_dir.exists():
            raise FileNotFoundError(
                "Current V14 best run folder is unavailable: "
                f"{best_run or '<not established>'}"
            )

        runner = V14TransactionalMin3pRunner(self.paths, self.config)
        results_dir, return_code = runner.run_full_postprocess_existing(run_dir)
        summary = {
            "timestamp": _utc_now(),
            "optimizer_version": V14_VERSION,
            "mode": "postprocess-best",
            "best_run_folder": str(run_dir),
            "results_folder": str(results_dir) if results_dir else "",
            "return_code": int(return_code),
            "solver_executed": False,
        }
        self._write_json_atomic(
            self.v14_results_dir / "V14_full_postprocess_best.json",
            summary,
        )
        return summary

    # ------------------------------------------------------------------
    # V14.3 local-futility audit and explicit stage transition
    # ------------------------------------------------------------------

    def apply_local_futility_v14(self) -> dict[str, Any]:
        # Apply local-futility completion without running MIN3P.  This changes
        # only optimizer runtime state; canonical and best workbooks are not modified.
        if self.using_v13_5_optimizer_fallback:
            raise RuntimeError("V14.3 local futility requires AdaptiveCoordinateOptimizerV14.")
        method = getattr(self.optimizer_v14, "apply_local_futility_gate", None)
        if not callable(method):
            raise RuntimeError("Installed optimizer does not provide apply_local_futility_gate().")
        summary = _json_safe(method(advance_stage=True))
        self._write_json_atomic(self.v14_results_dir / "V14_local_futility_summary.json", summary)
        self._audit_decision(
            phase="local_futility_gate", suggestion=None,
            event={"accepted": False, "decision": "local_futility_gate_applied", "rejection_reason": "", "group": summary.get("group_scope", "sorption")},
            diagnostics=None,
            restoration_verified=bool(self.transaction_manager.canonical_best_hashes().get("match")),
            decision_source="deterministic_local_futility_gate",
            notes=json.dumps(summary, ensure_ascii=False),
        )
        self._audit_state_snapshot(action="local_futility_gate_applied", event={"group": summary.get("group_scope", "sorption")})
        return summary

    # ------------------------------------------------------------------
    # GPT advisory supervision
    # ------------------------------------------------------------------

    def _gpt_evidence(
        self,
        *,
        event: dict[str, Any],
        diagnostics: dict[str, Any],
        recent_suggestion: pd.Series,
    ) -> dict[str, Any]:
        """Build the restricted structured evidence package for GPT."""
        best_score, best_run = self._v14_current_best()
        return {
            "optimizer_version": V14_VERSION,
            "current_best_objective": best_score,
            "current_best_run_folder": best_run,
            "candidate": _json_safe(recent_suggestion.to_dict()),
            "event": _json_safe(event),
            "objective_components": _json_safe(
                diagnostics.get("objective_components", {})
            ),
            "residual_diagnostics": _json_safe(
                diagnostics.get("v14_residual_diagnostics", {})
            ),
            "stability_metrics": _json_safe(
                diagnostics.get("v14_numerical_diagnostics", {})
            ),
            "plausibility_flags": _json_safe(
                diagnostics.get("v14_plausibility_diagnostics", {})
            ),
            "parameter_states": self._optimizer_state_for_audit(),
            "interaction_history_file": str(self.v14_interaction_history_file),
            "allowed_decision_scope": {
                "advisory_only": True,
                "cannot_edit_dat": True,
                "cannot_override_user_constraints": True,
                "cannot_override_bounds": True,
                "cannot_override_rollback": True,
                "cannot_override_numerical_rules": True,
                "cannot_override_hard_stop": True,
            },
        }

    def _should_call_gpt(
        self,
        *,
        completed_valid_runs: int,
        event: dict[str, Any],
        diagnostics: dict[str, Any],
        disable_gpt: bool,
    ) -> tuple[bool, str]:
        """Apply the YAML-configured GPT advisory cadence without an API call."""
        if disable_gpt:
            return False, "disabled_by_cli"
        if "gpt_supervisor" not in self.v14_components:
            return False, "gpt_supervisor_module_unavailable"

        supervisor = self._instantiate_component("gpt_supervisor")
        if supervisor is None:
            return False, "supervisor_initialization_failed"
        settings = getattr(supervisor, "settings", {})
        if not isinstance(settings, dict):
            return False, "supervisor_settings_unavailable"
        if not bool(settings.get("enabled", False)):
            return False, "disabled_in_gpt_config"

        try:
            interval = max(int(settings.get("valid_run_interval", 4) or 4), 1)
        except (TypeError, ValueError):
            interval = 4

        accepted = bool(event.get("accepted", False))
        scientific_ok = bool(diagnostics.get("scientific_ok", True))
        action = str(event.get("action", "")).strip().casefold()
        decision = str(event.get("decision", "")).strip().casefold()
        group_transition = ("stage" in action or "group_transition" in action) and (
            "advance" in action or "transition" in action
        )
        possible_stop = "stop" in action or "converg" in action
        rejected_valid = decision == "reject" and bool(event.get("valid_run", True))

        if accepted and bool(settings.get("call_on_new_best", True)):
            return True, "new_best"
        if not scientific_ok and bool(settings.get("call_on_numerical_warning", True)):
            return True, "numerical_or_scientific_warning"
        if group_transition and bool(settings.get("call_on_group_transition", True)):
            return True, "group_transition"
        if possible_stop and bool(settings.get("call_on_possible_stop", True)):
            return True, "possible_stop"
        if (
            rejected_valid
            and bool(settings.get("call_on_repeated_failures", True))
            and completed_valid_runs > 0
            and completed_valid_runs % interval == 0
        ):
            return True, f"repeated_valid_failures_interval_{interval}"
        return False, "cadence_not_reached"


    def _request_gpt_advisory(
        self,
        *,
        event: dict[str, Any],
        diagnostics: dict[str, Any],
        suggestion: pd.Series,
        completed_valid_runs: int,
        disable_gpt: bool,
    ) -> None:
        should_call, reason = self._should_call_gpt(
            completed_valid_runs=completed_valid_runs,
            event=event,
            diagnostics=diagnostics,
            disable_gpt=disable_gpt,
        )
        record = {
            "timestamp": _utc_now(),
            "optimizer_version": V14_VERSION,
            "trigger": reason,
            "called": should_call,
        }
        if not should_call:
            self._append_jsonl(self.v14_gpt_log_file, record)
            return

        supervisor = self._instantiate_component("gpt_supervisor")
        if supervisor is None:
            record["called"] = False
            record["reason"] = "supervisor_initialization_failed"
            self._append_jsonl(self.v14_gpt_log_file, record)
            return

        evidence = self._gpt_evidence(
            event=event, diagnostics=diagnostics, recent_suggestion=suggestion
        )
        try:
            method: Callable[..., Any] | None = getattr(supervisor, "review_evidence", None)
            if not callable(method):
                method = getattr(supervisor, "supervise", None)
            if not callable(method):
                raise AttributeError(
                    "GPTScientificSupervisorV14 needs review_evidence(...) or supervise(...)."
                )
            advice = method(evidence=evidence)
            if not isinstance(advice, dict):
                raise ValueError("GPT supervisor returned a non-dict response.")

            required = {
                "scientific_assessment",
                "recommended_group",
                "recommended_action",
                "suggested_parameter",
                "suggested_pair",
                "suggested_direction",
                "confidence",
                "reasoning_summary",
                "freeze_suggestions",
                "reactivation_suggestions",
                "stop_continue_recommendation",
                "warnings",
            }
            missing = sorted(required.difference(advice))
            if missing:
                raise ValueError(f"GPT response missing required fields: {', '.join(missing)}")

            self._latest_gpt_advice = _json_safe(advice)
            record.update(
                {
                    "status": "valid_advisory_received",
                    "evidence": evidence,
                    "advice": advice,
                }
            )

            # The optimiser may store this advice only as a low-priority ranking
            # input after it has applied all deterministic safety checks.
            setter = getattr(self.optimizer_v14, "set_gpt_advisory", None)
            if callable(setter) and not self.using_v13_5_optimizer_fallback:
                setter(_json_safe(advice))
        except Exception as exc:
            self._latest_gpt_advice = None
            record.update(
                {
                    "status": "invalid_or_failed_advisory",
                    "reason": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(limit=2),
                }
            )

        self._append_jsonl(self.v14_gpt_log_file, record)

    def _v14_optimizer_event_count(self, action: str) -> int:
        """Count optimizer audit events without affecting optimizer state."""
        event_file = getattr(self.optimizer_v14, "event_file", None)
        if not event_file:
            return 0
        path = Path(event_file)
        if not path.exists():
            return 0
        try:
            frame = pd.read_excel(path)
        except Exception:
            return 0
        if frame.empty or "action" not in frame.columns:
            return 0
        return int(frame["action"].astype(str).eq(str(action)).sum())

    def _v14_optimizer_status_snapshot(self) -> dict[str, Any]:
        """Return a read-only convergence/status snapshot for invocation reporting."""
        status_method = getattr(self.optimizer_v14, "local_futility_status", None)
        if callable(status_method) and not self.using_v13_5_optimizer_fallback:
            try:
                status = status_method()
                if isinstance(status, dict):
                    return _json_safe(status)
            except Exception:
                pass
        state_file = getattr(self.optimizer_v14, "state_file", None)
        if state_file and Path(state_file).exists():
            try:
                frame = pd.read_excel(state_file)
                if not frame.empty:
                    row = frame.iloc[0].to_dict()
                    return {
                        "convergence_status": str(row.get("convergence_status", "")),
                        "convergence_reason": str(row.get("convergence_reason", "")),
                        "current_stage": str(row.get("current_stage", "")),
                    }
            except Exception:
                pass
        return {}

    def _persist_v14_invocation_summary(self, summary: dict[str, Any]) -> None:
        """Persist invocation accounting in the optimizer state workbook."""
        recorder = getattr(self.optimizer_v14, "record_invocation_summary", None)
        if callable(recorder) and not self.using_v13_5_optimizer_fallback:
            try:
                recorder(summary)
                return
            except Exception as exc:
                log(self.paths, f"WARNING: could not persist V14 invocation summary: {exc}")

    def _log_v14_invocation_summary(self, summary: dict[str, Any]) -> None:
        status = str(summary.get("optimizer_status", "") or "unknown").upper()
        reason = str(summary.get("last_invocation_stop_reason", "unknown"))
        log(self.paths, "=" * 72)
        log(self.paths, f"{V14_VERSION} CAMPAIGN INVOCATION SUMMARY")
        log(self.paths, "=" * 72)
        log(self.paths, f"Candidate cycles selected : {summary.get('last_invocation_candidate_cycles_selected', 0)}")
        log(self.paths, f"Candidate cycles completed: {summary.get('last_invocation_candidate_cycles_completed', 0)}")
        log(self.paths, f"Physical MIN3P runs       : {summary.get('last_invocation_physical_runs', 0)}")
        log(self.paths, f"Cache hits                : {summary.get('last_invocation_cache_hits', 0)}")
        log(self.paths, f"Bound/no-op skips         : {summary.get('last_invocation_bound_skips', 0)}")
        log(self.paths, f"Accepted candidates       : {summary.get('last_invocation_accepted', 0)}")
        log(self.paths, f"Rejected candidates       : {summary.get('last_invocation_rejected', 0)}")
        log(self.paths, f"Invalid candidates        : {summary.get('last_invocation_invalid', 0)}")
        log(self.paths, f"Best objective before     : {summary.get('last_invocation_best_objective_before')}")
        log(self.paths, f"Best objective after      : {summary.get('last_invocation_best_objective_after')}")
        log(self.paths, f"Stop reason               : {reason}")
        log(self.paths, f"Optimizer status          : {status}")
        detail = str(summary.get("last_invocation_stop_detail", "") or "")
        if detail:
            log(self.paths, f"Stop detail               : {detail}")
        if reason in {"max_candidates_reached", "max_physical_runs_reached"} and status != "CONVERGED":
            log(self.paths, "Calibration is paused by the invocation limit; run again to continue.")
        elif reason == "true_convergence" or status == "CONVERGED":
            log(self.paths, "Calibration reports deterministic convergence.")
        elif reason == "temporarily_blocked":
            log(self.paths, "Calibration is blocked, not converged; inspect the stop detail before continuing.")
        log(self.paths, "=" * 72)

    # ------------------------------------------------------------------
    # V14 campaign loop
    # ------------------------------------------------------------------

    def auto_v14(
        self,
        *,
        max_runs: int | None = None,
        max_candidates: int | None = None,
        max_physical_runs: int | None = None,
        max_changes: int = 1,
        disable_gpt: bool = True,
        allow_v13_5_fallback: bool = False,
    ) -> None:
        """Run V14 with isolated, cancellation-safe candidate transactions."""
        del max_changes  # Candidate-pair integrity requires one evaluated candidate per loop.

        # V14.4.7 CLI semantics:
        # - max_candidates counts optimizer candidate cycles, including cache hits.
        # - max_physical_runs counts only cache-miss MIN3P executions.
        # - max_runs is retained as the deprecated/backward-compatible alias for
        #   max_candidates.  Direct callers that provide no limit retain the old
        #   default of 10 candidate cycles.
        if max_candidates is not None and max_runs is not None:
            raise ValueError("Specify only one of max_candidates or max_runs; --max-runs is an alias for --max-candidates.")
        candidate_limit = max_candidates if max_candidates is not None else max_runs
        if candidate_limit is None and max_physical_runs is None:
            candidate_limit = 10
        if candidate_limit is not None:
            candidate_limit = max(int(candidate_limit), 0)
        if max_physical_runs is not None:
            max_physical_runs = max(int(max_physical_runs), 0)

        log(self.paths, "=" * 80)
        log(self.paths, f"{V14_VERSION} TRANSACTIONAL ADAPTIVE DIRECTIONAL CALIBRATION STARTED")
        log(self.paths, "=" * 80)

        if self.using_v13_5_optimizer_fallback and not allow_v13_5_fallback:
            log(
                self.paths,
                "V14 auto mode blocked: adaptive_coordinate_optimizer_V14.py is not "
                "available. Run --mode preflight, then add V14 modules. "
                "Use --allow-v13-5-fallback only for a controlled V13.5 regression test.",
            )
            self.write_v14_report()
            return

        if not self._ensure_v14_baseline():
            log(self.paths, "V14 stopped: baseline was not established safely.")
            self.write_v14_report()
            return

        # V14.3.7: automatically restore only unambiguous pre-decision
        # transactions. Decisions already observed or accepted commits pending
        # remain blocked for manual reconciliation.
        recovery_summary = self.transaction_manager.recover_interrupted_transactions(
            reason="auto_startup_recovery"
        )
        if recovery_summary.get("recovered"):
            log(
                self.paths,
                f"{V14_VERSION} automatically recovered "
                f"{len(recovery_summary['recovered'])} unfinished pre-decision transaction(s).",
            )
        unresolved = self.transaction_manager.unfinished_transactions()
        if unresolved:
            log(
                self.paths,
                f"{V14_VERSION} auto mode blocked: transaction(s) require manual reconciliation. "
                "Run --mode recover-interrupted and review the recovery summary.",
            )
            self._write_json_atomic(
                self.v14_results_dir / "V14_unfinished_transactions.json",
                {"timestamp": _utc_now(), "transactions": unresolved},
            )
            self.write_v14_report()
            return

        try:
            self.transaction_manager.assert_canonical_matches_best()
        except Exception as exc:
            log(self.paths, f"{V14_VERSION} auto mode blocked by canonical/best hash guard: {exc}")
            self.write_v14_report()
            return

        # V14.4.1: seed the exact-candidate cache with the currently accepted
        # baseline.  This allows a later opposite-direction move that returns
        # exactly to a previously accepted/baseline configuration to reuse its
        # known objective instead of launching MIN3P again.
        baseline_score, baseline_run = self._v14_current_best()
        self.transaction_manager.register_evaluated_configuration(
            config_file=self.v14_best_file,
            total_score=baseline_score,
            run_folder=baseline_run,
            run_status="success",
            source="accepted_baseline",
            valid=baseline_score is not None,
        )

        completed_valid_runs = 0
        active_transaction: CandidateTransaction | None = None
        active_row: pd.Series | None = None

        invocation_started_at = _utc_now()
        invocation_best_before, _ = self._v14_current_best()
        candidate_cycles_selected = 0
        candidate_cycles_completed = 0
        physical_runs_started = 0
        cache_hits = 0
        accepted_count = 0
        rejected_count = 0
        invalid_count = 0
        stop_reason = "unknown"
        stop_detail = ""
        bound_skips_before = self._v14_optimizer_event_count("candidate_blocked_by_bound_noop")

        try:
            while True:
                if candidate_limit is not None and candidate_cycles_selected >= candidate_limit:
                    stop_reason = "max_candidates_reached"
                    stop_detail = f"candidate cycle limit {candidate_limit} reached"
                    break
                if max_physical_runs is not None and physical_runs_started >= max_physical_runs:
                    stop_reason = "max_physical_runs_reached"
                    stop_detail = f"physical MIN3P run limit {max_physical_runs} reached"
                    break
                if self.paths.manual_stop_file.exists():
                    stop_reason = "manual_stop_file"
                    stop_detail = str(self.paths.manual_stop_file)
                    log(self.paths, f"Manual stop file detected: {self.paths.manual_stop_file}")
                    break
                if self.transaction_manager.stop_requested():
                    log(
                        self.paths,
                        f"{V14_VERSION} safe-stop request detected before candidate selection. "
                        "No candidate transaction was started.",
                    )
                    self._audit_decision(
                        phase="safe_stop_requested",
                        suggestion=None,
                        event=None,
                        diagnostics=None,
                        restoration_verified=True,
                        decision_source="transaction_safe_stop",
                        notes=str(self.transaction_manager.stop_file),
                    )
                    stop_reason = "safe_stop_requested"
                    stop_detail = str(self.transaction_manager.stop_file)
                    break

                checkpoint = self.transaction_manager.create_selection_checkpoint()
                suggestion, selection_reason = self._next_v14_suggestion()
                if suggestion.empty:
                    self.transaction_manager.discard_checkpoint(checkpoint)
                    optimizer_snapshot = self._v14_optimizer_status_snapshot()
                    convergence_status = str(optimizer_snapshot.get("convergence_status", "")).strip().casefold()
                    convergence_reason = str(optimizer_snapshot.get("convergence_reason", "") or "")
                    if convergence_status == "converged":
                        stop_reason = "true_convergence"
                        stop_detail = convergence_reason or selection_reason
                    else:
                        stop_reason = "temporarily_blocked"
                        stop_detail = selection_reason or convergence_reason
                    log(
                        self.paths,
                        "V14 has no legal candidate. This can mean convergence, an "
                        f"incomplete/blocked direction pair, or a V14 precedence check: {selection_reason}",
                    )
                    self._audit_decision(
                        phase="selection_blocked",
                        suggestion=None,
                        event=None,
                        diagnostics=None,
                        restoration_verified=None,
                        decision_source="deterministic_optimizer",
                        notes=selection_reason,
                    )
                    break

                candidate_cycles_selected += 1
                iteration = candidate_cycles_selected
                active_row = suggestion.iloc[0].copy()
                active_transaction = self.transaction_manager.begin_transaction(
                    checkpoint,
                    suggestion=active_row.to_dict(),
                    selection_reason=selection_reason,
                )
                self._audit_decision(
                    phase="candidate_selected",
                    suggestion=active_row,
                    event=None,
                    diagnostics=None,
                    restoration_verified=None,
                    decision_source="deterministic_optimizer",
                    notes=selection_reason,
                )
                self._audit_interaction(
                    action="candidate_selected",
                    suggestion=active_row,
                    reason=(
                        "single-parameter directional candidate"
                        if not str(active_row.get("candidate_type", "")).startswith("interaction")
                        else "interaction candidate selected by optimiser"
                    ),
                )

                # Candidate values are written only to the transaction workbook.
                self.transaction_manager.prepare_candidate(active_transaction)
                self.transaction_manager.apply_suggestion_to_candidate(
                    active_transaction,
                    suggestion,
                )

                cache_record = self.transaction_manager.find_cached_candidate(active_transaction)
                cache_hit = bool(cache_record)
                if cache_hit:
                    cache_hits += 1
                    self.transaction_manager.record_cache_hit(active_transaction, cache_record)
                    run_result = dict(cache_record)
                    log(
                        self.paths,
                        f"{V14_VERSION} candidate cache hit | "
                        f"parameter={active_row.get('parameter', '')} | "
                        f"score={cache_record.get('TOTAL_SCORE')} | "
                        f"source={cache_record.get('source_candidate_id') or cache_record.get('cache_source') or cache_record.get('run_folder', '')}",
                    )
                else:
                    physical_runs_started += 1
                    run_result = self._v14_transactional_single_cycle(active_transaction)

                run_dir = Path(run_result.get("run_folder", ""))
                score = _safe_float(run_result.get("TOTAL_SCORE"))
                status = str(run_result.get("run_status", "")).lower()
                valid = score is not None and status in VALID

                if valid and not cache_hit:
                    self.transaction_manager.register_evaluated_configuration(
                        config_file=active_transaction.candidate_config_file,
                        total_score=score,
                        run_folder=run_dir,
                        run_status=str(run_result.get("run_status", "")),
                        source="min3p_candidate",
                        source_candidate_id=str(active_row.get("candidate_id", "")),
                        source_transaction=str(active_transaction.root),
                        valid=True,
                    )

                baseline_score, baseline_run = self._v14_current_best()
                diagnostics = self._candidate_scientific_diagnostics(
                    run_dir=run_dir,
                    score=score,
                    valid=valid,
                    baseline_score=baseline_score,
                )

                observation_payload = {
                    "candidate_id": str(active_row.get("candidate_id", "")),
                    "parameter": str(active_row.get("parameter", "")),
                    "group": str(active_row.get("group", "")),
                    "direction": str(active_row.get("direction_label", active_row.get("direction", ""))),
                    "baseline_TOTAL_SCORE": baseline_score,
                    "candidate_TOTAL_SCORE": score,
                    "run_folder": str(run_dir),
                    "run_status": str(run_result.get("run_status", "")),
                    "valid": bool(valid),
                    "scientific_ok": bool(diagnostics.get("scientific_ok", True)),
                    "scientific_penalty": float(diagnostics.get("scientific_penalty", 0.0) or 0.0),
                    "diagnostics": diagnostics,
                }
                self.transaction_manager.mark_observation_pending(
                    active_transaction,
                    observation_payload,
                )
                try:
                    event = self.optimizer_v14.observe_run(
                        score,
                        str(run_dir),
                        scientific_ok=observation_payload["scientific_ok"],
                        scientific_penalty=observation_payload["scientific_penalty"],
                        valid=valid,
                        diagnostics=diagnostics,
                        min3p_run_status=observation_payload["run_status"],
                        count_as_valid_run=not cache_hit,
                    )
                except Exception as exc:
                    self.transaction_manager.mark_observation_failed(
                        active_transaction,
                        observation_payload,
                        exc,
                    )
                    observation_context = (
                        "while processing a cached candidate result"
                        if cache_hit
                        else "after the MIN3P run completed"
                    )
                    raise TransactionRecoveryRequired(
                        f"Optimizer observation failed {observation_context}. "
                        "The pre-selection checkpoint will be restored so the campaign "
                        f"cannot retain a stale pending candidate: {type(exc).__name__}: {exc}"
                    ) from exc
                if cache_hit:
                    event.update({
                        "cache_hit": True,
                        "cache_source_candidate_id": str(cache_record.get("source_candidate_id", "")),
                        "cache_source_transaction": str(cache_record.get("source_transaction", "")),
                        "cache_source": str(cache_record.get("cache_source", "")),
                    })
                self.transaction_manager.mark_decision_observed(active_transaction, event)

                if valid:
                    try:
                        diag = write_candidate_diagnostics(
                            results_dir=self.paths.results_dir,
                            ranking_file=self.paths.ranking_file,
                            baseline_run_folder=baseline_run,
                            candidate_run_folder=str(run_dir),
                            baseline_total_score=baseline_score,
                            candidate_total_score=score,
                            parameter=str(active_row.get("parameter", "")),
                            group=str(active_row.get("group", "")),
                            direction=str(active_row.get("direction_label", active_row.get("direction", ""))),
                            step_fraction=_safe_float(
                                active_row.get("step_fraction_applied", active_row.get("step_fraction"))
                            ),
                            accepted=bool(event.get("accepted")),
                            decision=str(event.get("decision", "")),
                            rejection_reason=str(event.get("rejection_reason", "")),
                            config=self.config,
                            candidate_id=str(active_row.get("candidate_id", "")),
                        )
                        diagnostics.update(_json_safe(diag))
                        event.update(_json_safe(diag))
                    except Exception as exc:
                        log(self.paths, f"WARNING: V14 candidate diagnostics writer failed: {exc}")

                if event.get("accepted"):
                    # Write-ahead journal before the two-file accepted commit.
                    self.transaction_manager.mark_accepted_commit_pending(active_transaction, event)
                    try:
                        self.transaction_manager.commit_candidate_to_canonical(active_transaction)
                        self._save_v14_best(
                            total_score=float(score),
                            run_folder=str(run_dir),
                            stage=str(event.get("group", active_row.get("group", ""))),
                            diagnostics=event,
                            baseline_source="V14_accepted_transactional_candidate",
                        )
                        restored = self._verify_value(active_row["parameter"], active_row["new_value"])
                    except Exception as exc:
                        restored = False
                        event["accepted"] = False
                        event["decision"] = "accepted_candidate_snapshot_failed"
                        event["rejection_reason"] = f"could not commit accepted V14 transaction: {exc}"
                        # Do not silently rewrite state after a failed accepted commit.
                        raise TransactionRecoveryRequired(event["rejection_reason"])
                else:
                    # Rejected and invalid candidates never touched the canonical
                    # workbook.  Verify it remains byte-identical to the best.
                    hashes = self.transaction_manager.canonical_best_hashes()
                    restored = bool(hashes.get("match")) and self._verify_value(
                        active_row["parameter"], active_row["old_value"]
                    )
                    if not restored:
                        event["rejection_reason"] = (
                            str(event.get("rejection_reason", "")).strip("; ")
                            + "; canonical V14 best snapshot verification failed"
                        ).strip("; ")

                try:
                    event = self.optimizer_v14.finalize_decision(event, restored)
                except Exception as exc:
                    event["finalize_error"] = f"{type(exc).__name__}: {exc}"
                    event["restoration_verified"] = restored

                event["restoration_verified"] = restored
                event["iteration"] = iteration
                event["candidate_id"] = event.get("candidate_id", active_row.get("candidate_id", ""))

                self._audit_decision(
                    phase="candidate_evaluated",
                    suggestion=active_row,
                    event=event,
                    diagnostics=diagnostics,
                    restoration_verified=restored,
                    decision_source="objective_and_transaction_rules",
                )
                self._audit_interaction(
                    action="candidate_evaluated",
                    suggestion=active_row,
                    event=event,
                    reason=str(event.get("rejection_reason", "")),
                )
                self._audit_state_snapshot(action="candidate_evaluated", event=event)

                if valid and not cache_hit:
                    completed_valid_runs += 1

                self._request_gpt_advisory(
                    event=event,
                    diagnostics=diagnostics,
                    suggestion=active_row,
                    completed_valid_runs=completed_valid_runs,
                    disable_gpt=disable_gpt,
                )

                outcome = "accepted" if bool(event.get("accepted")) else ("rejected" if valid else "invalid")
                candidate_cycles_completed += 1
                if outcome == "accepted":
                    accepted_count += 1
                elif outcome == "rejected":
                    rejected_count += 1
                else:
                    invalid_count += 1
                self.transaction_manager.complete_transaction(
                    active_transaction,
                    outcome=outcome,
                    event=event,
                )
                active_transaction = None
                active_row = None

                log(
                    self.paths,
                    f"V14 {event.get('decision')} | {event.get('parameter', '')} | "
                    f"score={score} | restoration_verified={restored}",
                )

                if event.get("stop_auto") is True or event.get("should_stop") is True:
                    stop_reason = "optimizer_stop_requested"
                    stop_detail = str(event.get("convergence_reason", event.get("rejection_reason", "")) or "")
                    log(self.paths, "V14 optimiser requested a deterministic stop.")
                    break
                if self.transaction_manager.stop_requested():
                    stop_reason = "safe_stop_requested"
                    stop_detail = str(self.transaction_manager.stop_file)
                    log(
                        self.paths,
                        f"{V14_VERSION} safe-stop request honoured after the completed candidate transaction.",
                    )
                    break

        except CandidateRunCancelled as exc:
            stop_reason = "user_interrupt"
            stop_detail = f"candidate_cancelled:{exc.reason}"
            if active_transaction is not None and active_row is not None:
                try:
                    self._v14_cancelled_transaction_event(
                        transaction=active_transaction,
                        row=active_row,
                        reason=f"user_interrupt:{exc.reason}",
                        run_folder=str(exc.run_dir),
                    )
                except Exception as recovery_exc:
                    log(self.paths, f"CRITICAL: {V14_VERSION} cancellation recovery failed: {recovery_exc}")
            log(self.paths, f"{V14_VERSION} candidate cancelled safely; canonical best configuration was preserved.")

        except KeyboardInterrupt:
            stop_reason = "user_interrupt"
            stop_detail = "keyboard_interrupt"
            if active_transaction is not None and active_row is not None:
                try:
                    self._v14_cancelled_transaction_event(
                        transaction=active_transaction,
                        row=active_row,
                        reason="user_interrupt:keyboard_interrupt",
                    )
                except Exception as recovery_exc:
                    log(self.paths, f"CRITICAL: {V14_VERSION} interrupt recovery failed: {recovery_exc}")
            log(self.paths, f"{V14_VERSION} interrupted safely; no new candidate was committed.")

        except (TransactionRecoveryRequired, TransactionError) as exc:
            stop_reason = "transaction_guard_stop"
            stop_detail = f"{type(exc).__name__}:{exc}"
            log(self.paths, f"{V14_VERSION} transaction guard stopped the campaign: {exc}")
            if active_transaction is not None and active_row is not None:
                try:
                    self._v14_cancelled_transaction_event(
                        transaction=active_transaction,
                        row=active_row,
                        reason=f"transaction_guard:{type(exc).__name__}:{exc}",
                    )
                except Exception as recovery_exc:
                    log(self.paths, f"CRITICAL: {V14_VERSION} guarded recovery failed: {recovery_exc}")

        except Exception as exc:
            stop_reason = "unexpected_failure"
            stop_detail = f"{type(exc).__name__}:{exc}"
            log(self.paths, f"{V14_VERSION} unexpected candidate failure: {type(exc).__name__}: {exc}")
            if active_transaction is not None and active_row is not None:
                try:
                    self._v14_cancelled_transaction_event(
                        transaction=active_transaction,
                        row=active_row,
                        reason=f"unexpected_failure:{type(exc).__name__}:{exc}",
                    )
                except Exception as recovery_exc:
                    log(self.paths, f"CRITICAL: {V14_VERSION} unexpected-failure recovery failed: {recovery_exc}")

        finally:
            rank_runs(self.paths, self.config)
            self.write_v14_report()

            bound_skips_after = self._v14_optimizer_event_count("candidate_blocked_by_bound_noop")
            bound_skips = max(bound_skips_after - bound_skips_before, 0)
            invocation_best_after, _ = self._v14_current_best()
            optimizer_snapshot = self._v14_optimizer_status_snapshot()
            optimizer_status = str(optimizer_snapshot.get("convergence_status", "") or "running")
            optimizer_reason = str(optimizer_snapshot.get("convergence_reason", "") or "")
            if stop_reason == "unknown":
                if optimizer_status.strip().casefold() == "converged":
                    stop_reason = "true_convergence"
                    stop_detail = optimizer_reason
                else:
                    stop_reason = "completed_without_explicit_stop_reason"

            summary = {
                "last_invocation_started_at": invocation_started_at,
                "last_invocation_finished_at": _utc_now(),
                "last_invocation_stop_reason": stop_reason,
                "last_invocation_stop_detail": stop_detail,
                "last_invocation_candidate_cycles_selected": candidate_cycles_selected,
                "last_invocation_candidate_cycles_completed": candidate_cycles_completed,
                "last_invocation_physical_runs": physical_runs_started,
                "last_invocation_cache_hits": cache_hits,
                "last_invocation_bound_skips": bound_skips,
                "last_invocation_accepted": accepted_count,
                "last_invocation_rejected": rejected_count,
                "last_invocation_invalid": invalid_count,
                "last_invocation_best_objective_before": invocation_best_before,
                "last_invocation_best_objective_after": invocation_best_after,
                "last_invocation_requested_max_candidates": candidate_limit,
                "last_invocation_requested_max_physical_runs": max_physical_runs,
                "optimizer_status": optimizer_status,
                "optimizer_convergence_reason": optimizer_reason,
            }
            self._persist_v14_invocation_summary(summary)
            self._log_v14_invocation_summary(summary)
            log(self.paths, f"{V14_VERSION} transactional adaptive directional calibration finished")

    # ------------------------------------------------------------------
    # Preflight and report
    # ------------------------------------------------------------------

    def preflight(self, *, disable_gpt: bool = True) -> dict[str, Any]:
        """Validate paths and module availability without modifying a .dat file."""
        missing_paths = []
        for label, path in {
            "agent_config": self.paths.config_file,
            "results_dir": self.paths.results_dir,
            "reports_dir": self.paths.reports_dir,
        }.items():
            if label.endswith("_dir"):
                exists = Path(path).exists()
            else:
                exists = Path(path).exists()
            if not exists:
                missing_paths.append(f"{label}: {path}")

        missing_required_components = [
            key
            for key, status in self.v14_component_status.items()
            if status.get("required") and not status.get("available")
        ]
        state = self._read_v14_state()
        best_score, best_run = self._v14_current_best()

        transaction_hashes = self.transaction_manager.canonical_best_hashes()
        unfinished_transactions = self.transaction_manager.unfinished_transactions()
        local_futility_status: dict[str, Any] = {}
        status_method = getattr(self.optimizer_v14, "local_futility_status", None)
        if callable(status_method) and not self.using_v13_5_optimizer_fallback:
            try:
                local_futility_status = _json_safe(status_method())
            except Exception as exc:
                local_futility_status = {"available": False, "reason": f"{type(exc).__name__}: {exc}"}

        report = {
            "timestamp": _utc_now(),
            "optimizer_version": V14_VERSION,
            "mode": "preflight",
            "config_file": str(self.paths.config_file),
            "results_dir": str(self.paths.results_dir),
            "reports_dir": str(self.paths.reports_dir),
            "history_file_unchanged": str(self.paths.history_file),
            "ranking_file_unchanged": str(self.paths.ranking_file),
            "missing_paths": missing_paths,
            "v13_5_optimizer_fallback": self.using_v13_5_optimizer_fallback,
            "missing_required_v14_components": missing_required_components,
            "components": self.v14_component_status,
            "v14_campaign_state_exists": self.v14_campaign_state_file.exists(),
            "v14_best_exists": self.v14_best_file.exists(),
            "v14_current_best_score": best_score,
            "v14_current_best_run": best_run,
            "gpt_disabled_by_cli": disable_gpt,
            "v14_2_transactional_candidate_execution": {
                "canonical_best_hashes": transaction_hashes,
                "unfinished_transactions": unfinished_transactions,
                "safe_stop_file": str(self.transaction_manager.stop_file),
                "safe_stop_requested": self.transaction_manager.stop_requested(),
                "local_futility_gate": local_futility_status,
                "candidate_execution_mode": (
                    "MIN3P plus fast GBT/GBM objective processing; "
                    "full plots are separate"
                ),
            },
            "ready_for_full_v14_auto": (
                not missing_paths
                and not missing_required_components
                and not self.using_v13_5_optimizer_fallback
                and not unfinished_transactions
                and (not self.v14_best_file.exists() or bool(transaction_hashes.get("match")))
            ),
            "safe_next_step": (
                "Implement the missing V14 modules, rerun preflight, then use a "
                "two-run --disable-gpt smoke test."
                if missing_required_components or self.using_v13_5_optimizer_fallback
                else "V14 validation is complete. Review the final release audit; do not run auto calibration without an explicit campaign decision."
            ),
        }

        self._write_json_atomic(
            self.v14_results_dir / "V14_preflight.json",
            report,
        )
        self._audit_decision(
            phase="preflight",
            suggestion=None,
            event=None,
            diagnostics=None,
            restoration_verified=None,
            decision_source="preflight",
            notes=report["safe_next_step"],
        )

        print(json.dumps(_json_safe(report), indent=2, ensure_ascii=False))
        return report

    def write_v14_report(self) -> Path:
        """
        Write a minimal V14 campaign report now; later report_generator_V14.py can
        replace this with richer diagnostics without changing the pipeline.
        """
        writer = self.v14_components.get("report")
        if callable(writer):
            try:
                result = writer(
                    paths=self.paths,
                    campaign_state_file=self.v14_campaign_state_file,
                    state_history_file=self.v14_state_history_file,
                    interaction_history_file=self.v14_interaction_history_file,
                    decision_log_file=self.v14_decision_log_file,
                    gpt_log_file=self.v14_gpt_log_file,
                    output_file=self.v14_report_file,
                )
                return Path(result) if result else self.v14_report_file
            except Exception as exc:
                log(self.paths, f"WARNING: V14 report module failed; writing fallback report: {exc}")

        state = self._read_v14_state()
        best = self._read_v14_best_metadata()
        component_lines = []
        for key, status in self.v14_component_status.items():
            mark = "available" if status.get("available") else "missing"
            component_lines.append(f"- `{key}`: **{mark}** — {status.get('reason', '')}")

        lines = [
            "# V14 Calibration Report",
            "",
            f"**Generated:** {_utc_now()}",
            f"**Project:** `{self.paths.project_dir.name}`",
            f"**Pipeline:** `{V14_VERSION}`",
            "",
            "## V13.5 baseline and V14 campaign state",
            "",
            f"- Current V14 best TOTAL_SCORE: `{state.get('current_best_score', best.get('total_score', 'not established'))}`",
            f"- Current V14 best run: `{state.get('current_best_run_folder', best.get('run_folder', 'not established'))}`",
            f"- V14 best configuration: `{self.v14_best_file}`",
            f"- V13.5 fallback active: `{self.using_v13_5_optimizer_fallback}`",
            "",
            "## V14 module availability",
            "",
            *component_lines,
            "",
            "## Audit files",
            "",
            f"- Parameter state history: `{self.v14_state_history_file}`",
            f"- Interaction history: `{self.v14_interaction_history_file}`",
            f"- Calibration decision log: `{self.v14_decision_log_file}`",
            f"- GPT supervisor JSONL: `{self.v14_gpt_log_file}`",
            f"- V14 optimization history: `{self.v14_history_file}`",
            "",
            "## Safety guarantees retained from V13.5",
            "",
            "- Candidate selection remains inside the adaptive coordinate optimiser.",
            "- A pending direction pair must complete before another candidate can be selected.",
            "- Rejected/invalid candidates are restored from the V14 best snapshot.",
            "- User-controlled inactive and frozen parameters remain under deterministic optimiser control.",
            "- GPT is advisory only and cannot edit MIN3P input files or override safety rules.",
        ]
        self.v14_report_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.v14_report_file


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "MIN3P AI Pipeline V14 — adaptive directional calibration, "
            "interaction-ready governance, audit trail, and advisory GPT supervision."
        )
    )
    parser.add_argument(
        "--mode",
        choices=[
            "auto",
            "preflight",
            "report",
            "recover-interrupted",
            "request-safe-stop",
            "clear-safe-stop", "apply-local-futility",
            "postprocess-best",
        ],
        default="preflight",
    )
    parser.add_argument(
        "--max-runs",
        type=int,
        default=None,
        help=(
            "Backward-compatible alias for --max-candidates. Counts optimizer "
            "candidate cycles, including cache hits; it does not mean physical MIN3P runs."
        ),
    )
    parser.add_argument(
        "--max-candidates",
        type=int,
        default=None,
        help="Maximum optimizer candidate cycles for this invocation, including cache hits.",
    )
    parser.add_argument(
        "--max-physical-runs",
        type=int,
        default=None,
        help="Maximum new cache-miss MIN3P executions for this invocation.",
    )
    parser.add_argument(
        "--max-changes",
        type=int,
        default=1,
        help="Retained for compatibility; V14 directional search evaluates one candidate per iteration.",
    )
    parser.add_argument(
        "--disable-gpt",
        "--skip-gpt",
        dest="disable_gpt",
        action="store_true",
        help="Disable advisory GPT supervisor calls.",
    )
    parser.add_argument(
        "--use-gpt",
        dest="disable_gpt",
        action="store_false",
        help="Enable advisory GPT supervisor calls only after gpt_scientific_supervisor.py exists.",
    )
    parser.set_defaults(disable_gpt=True)
    parser.add_argument(
        "--reset-campaign-start",
        action="store_true",
        help="Pass through to the inherited V13 campaign marker setup.",
    )
    parser.add_argument(
        "--allow-v13-5-fallback",
        action="store_true",
        help=(
            "Allow auto mode to use the inherited V13.5 optimiser for a controlled "
            "regression test. This is not full V14 operation."
        ),
    )

    args = parser.parse_args()
    if args.max_runs is not None and args.max_candidates is not None:
        parser.error("Use only one of --max-runs or --max-candidates; --max-runs is the compatibility alias.")
    if args.mode == "auto" and args.max_runs is None and args.max_candidates is None and args.max_physical_runs is None:
        args.max_candidates = 10
    workflow = V14Workflow(reset_campaign_start=args.reset_campaign_start)

    if args.mode == "preflight":
        workflow.preflight(disable_gpt=args.disable_gpt)
    elif args.mode == "auto":
        workflow.auto_v14(
            max_runs=args.max_runs,
            max_candidates=args.max_candidates,
            max_physical_runs=args.max_physical_runs,
            max_changes=args.max_changes,
            disable_gpt=args.disable_gpt,
            allow_v13_5_fallback=args.allow_v13_5_fallback,
        )
    elif args.mode == "report":
        report = workflow.write_v14_report()
        print(f"V14 calibration report created: {report}")
    elif args.mode == "apply-local-futility":
        summary = workflow.apply_local_futility_v14()
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    elif args.mode == "recover-interrupted":
        summary = workflow.recover_interrupted_transactions_v14()
        print(json.dumps(summary, indent=2))
    elif args.mode == "request-safe-stop":
        flag = workflow.transaction_manager.request_safe_stop()
        print(f"{V14_VERSION} safe stop requested: {flag}")
    elif args.mode == "clear-safe-stop":
        removed = workflow.transaction_manager.clear_safe_stop()
        print(f"{V14_VERSION} safe stop flag removed: {removed}")
    elif args.mode == "postprocess-best":
        summary = workflow.postprocess_v14_best()
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
