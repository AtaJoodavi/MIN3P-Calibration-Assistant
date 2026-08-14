from __future__ import annotations

"""V14.3.7 transactional candidate execution and interruption recovery.

Design contract
---------------
* The canonical ``01_input/agent_config.xlsx`` is the current accepted best
  configuration and must match ``best_parameters_V14.xlsx`` before any new
  candidate is prepared.
* A candidate is applied only to an isolated workbook inside a transaction
  directory.  MIN3P reads a DAT built from that isolated workbook.
* Optimizer state is checkpointed before ``next_suggestion()`` mutates its
  pending-candidate fields.  Interrupted candidates restore that checkpoint.
* A safe-stop flag is honoured only at candidate boundaries.  Ctrl+C is caught
  by the pipeline and converted into a recoverable cancelled transaction.

This module deliberately leaves V14 selection, objective evaluation, and
accept/reject rules inside the existing optimizer and pipeline.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import time
from typing import Any, Iterable
import uuid

import pandas as pd

from modules.config_reader import ConfigReader


class TransactionError(RuntimeError):
    """Base error for a V14 candidate transaction."""


class TransactionRecoveryRequired(TransactionError):
    """Raised when an unfinished transaction cannot be recovered safely."""


@dataclass(frozen=True)
class SelectionCheckpoint:
    """Optimizer files captured before selecting the next candidate."""

    root: Path
    snapshot_dir: Path
    manifest_file: Path
    manifest: dict[str, Any]


@dataclass(frozen=True)
class CandidateTransaction:
    """Immutable paths and metadata for one isolated V14 candidate."""

    root: Path
    manifest_file: Path
    candidate_config_file: Path
    candidate_dat_file: Path
    checkpoint: SelectionCheckpoint
    manifest: dict[str, Any]


class CandidateConfigReader(ConfigReader):
    """Read/write a transaction workbook while retaining canonical input paths.

    The inherited config reader resolves templates, observed data, and companion
    files against the normal project ``01_input`` folder.  Only workbook reads
    and the generated DAT destination are redirected into the transaction.
    """

    def __init__(self, paths, config_file: Path, generated_dat_file: Path):
        super().__init__(paths)
        self._transaction_config_file = Path(config_file)
        self._transaction_generated_dat_file = Path(generated_dat_file)

    @property
    def transaction_config_file(self) -> Path:
        return self._transaction_config_file

    def sheet(self, name: str) -> pd.DataFrame:
        if not self._transaction_config_file.exists():
            raise FileNotFoundError(self._transaction_config_file)
        return pd.read_excel(self._transaction_config_file, sheet_name=name)

    def all_sheets(self) -> dict[str, pd.DataFrame]:
        """Read all candidate workbook sheets without leaving a Windows file lock.

        The ExcelFile handle must close before the candidate workbook is atomically
        replaced by ``_write_workbook_atomic``.  Leaving it open can make
        ``os.replace`` fail with ``WinError 5`` on Windows.
        """
        if not self._transaction_config_file.exists():
            raise FileNotFoundError(self._transaction_config_file)
        with pd.ExcelFile(self._transaction_config_file, engine="openpyxl") as xls:
            return {
                sheet: pd.read_excel(xls, sheet_name=sheet)
                for sheet in xls.sheet_names
            }

    def write_all_sheets(self, sheets: dict[str, pd.DataFrame]) -> None:
        _write_workbook_atomic(self._transaction_config_file, sheets)

    def generated_dat_file(self) -> Path:
        return self._transaction_generated_dat_file


def _utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (str, bool, int, float)):
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
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


def _sha256(path: Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def _semantic_cell(value: Any) -> Any:
    """Normalize workbook values for a stable semantic candidate hash."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    return str(value)


def _semantic_workbook_sha256(path: Path) -> str:
    """Hash workbook content rather than XLSX container bytes.

    XLSX files can have different ZIP/document metadata even when every model
    setting is identical.  The cache therefore hashes sheet names, columns and
    cell values read through pandas.  All sheets are included so changing
    species weights, bounds, model_files, optimizer settings, or any other
    workbook content invalidates the cache safely.
    """
    path = Path(path)
    with pd.ExcelFile(path, engine="openpyxl") as xls:
        payload: dict[str, Any] = {}
        for sheet_name in xls.sheet_names:
            frame = pd.read_excel(xls, sheet_name=sheet_name)
            payload[str(sheet_name)] = {
                "columns": [str(column) for column in frame.columns],
                "rows": [
                    [_semantic_cell(value) for value in row]
                    for row in frame.itertuples(index=False, name=None)
                ],
            }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encoded).hexdigest().upper()


def _replace_with_retry(source: Path, destination: Path, *, attempts: int = 12) -> None:
    """Atomically replace destination, tolerating brief Windows handle-release delays."""
    last_error: PermissionError | None = None
    for attempt in range(max(int(attempts), 1)):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(0.05 * (attempt + 1))
    if last_error is not None:
        raise last_error


def _copy_atomic(source: Path, destination: Path) -> None:
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f"{destination.name}.tmp-{uuid.uuid4().hex}"
    )
    try:
        shutil.copy2(source, temporary)
        _replace_with_retry(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        temporary.write_text(
            json.dumps(_json_safe(payload), indent=2, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _replace_with_retry(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def _read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not Path(path).exists():
        return dict(default or {})
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else dict(default or {})
    except Exception:
        return dict(default or {})


def _write_workbook_atomic(path: Path, sheets: dict[str, pd.DataFrame]) -> None:
    path = Path(path)
    temporary = path.with_name(f"{path.stem}.tmp-{uuid.uuid4().hex}{path.suffix}")
    try:
        with pd.ExcelWriter(temporary, engine="openpyxl") as writer:
            for name, frame in sheets.items():
                frame.to_excel(writer, sheet_name=name, index=False)
        _replace_with_retry(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


class V14TransactionManager:
    """Persist and recover isolated candidate transactions.

    The manager owns only transaction files.  It never selects a parameter or
    evaluates an objective.  Its recovery action restores optimizer files to
    their *pre-selection* checkpoint, which prevents a cancelled candidate from
    leaving a pending direction family or a changed canonical configuration.
    """

    VERSION = "V14.4.7"
    ACTIVE_STATUSES = {
        "checkpoint_created",
        "selected",
        "candidate_prepared",
        "candidate_config_applied",
        "running",
        "run_completed",
        "observation_pending",
        "observation_failed",
        "decision_observed",
        "accepted_commit_pending",
    }

    # Core state files whose contents can be mutated by next_suggestion(),
    # step control, pair management, or runtime parameter state.  In addition,
    # dynamic v14_*state*/v14_*memory* files are captured automatically.
    CORE_STATE_FILES = {
        "v14_optimizer_parameter_state.xlsx",
        "v14_optimizer_state.xlsx",
        "v14_step_size_state.json",
        "v14_campaign_state.json",
        "v14_pair_optimizer_state.json",
        "v14_parameter_pair_state.json",
        "v14_parameter_runtime_state.xlsx",
        "v14_parameter_state_manager.json",
    }

    def __init__(self, paths, *, best_config_file: Path):
        self.paths = paths
        self.results_dir = Path(paths.results_dir)
        self.canonical_config = Path(paths.config_file)
        self.best_config_file = Path(best_config_file)
        self.root = self.results_dir / "v14_transactions"
        self.checkpoint_root = self.root / "_selection_checkpoints"
        self.audit_file = self.results_dir / "v14_transaction_audit.jsonl"
        self.cache_file = self.results_dir / "v14_candidate_cache.jsonl"
        self.stop_file = self.results_dir / "v14_stop_requested.flag"
        self.root.mkdir(parents=True, exist_ok=True)
        self.checkpoint_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Canonical best configuration integrity
    # ------------------------------------------------------------------

    def canonical_best_hashes(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "canonical_config": str(self.canonical_config),
            "best_config": str(self.best_config_file),
            "canonical_exists": self.canonical_config.exists(),
            "best_exists": self.best_config_file.exists(),
            "canonical_sha256": None,
            "best_sha256": None,
            "match": False,
        }
        if result["canonical_exists"]:
            result["canonical_sha256"] = _sha256(self.canonical_config)
        if result["best_exists"]:
            result["best_sha256"] = _sha256(self.best_config_file)
        result["match"] = bool(
            result["canonical_sha256"]
            and result["canonical_sha256"] == result["best_sha256"]
        )
        return result

    def assert_canonical_matches_best(self) -> None:
        hashes = self.canonical_best_hashes()
        if not hashes["best_exists"]:
            raise TransactionError(
                f"V14 best snapshot does not exist: {self.best_config_file}"
            )
        if not hashes["canonical_exists"]:
            raise TransactionError(
                f"Active configuration does not exist: {self.canonical_config}"
            )
        if not hashes["match"]:
            raise TransactionRecoveryRequired(
                "agent_config.xlsx does not match best_parameters_V14.xlsx. "
                "Run --mode recover-interrupted before selecting another candidate."
            )

    def restore_canonical_best(self, *, backup: bool = True, reason: str = "recovery") -> dict[str, Any]:
        """Restore canonical config from the V14 best snapshot atomically."""
        if not self.best_config_file.exists():
            raise TransactionError(f"Missing V14 best snapshot: {self.best_config_file}")
        previous = self.canonical_best_hashes()
        backup_file = ""
        if backup and self.canonical_config.exists() and not previous.get("match"):
            backup_dir = self.results_dir / "v14_transaction_recovery_backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = backup_dir / f"agent_config_before_{reason}_{stamp}.xlsx"
            shutil.copy2(self.canonical_config, backup_path)
            backup_file = str(backup_path)
        _copy_atomic(self.best_config_file, self.canonical_config)
        after = self.canonical_best_hashes()
        if not after["match"]:
            raise TransactionError("Canonical best restoration did not produce matching hashes.")
        return {"before": previous, "after": after, "backup_file": backup_file}

    # ------------------------------------------------------------------
    # Safe-stop control
    # ------------------------------------------------------------------

    def stop_requested(self) -> bool:
        return self.stop_file.exists()

    def request_safe_stop(self) -> Path:
        self.stop_file.parent.mkdir(parents=True, exist_ok=True)
        self.stop_file.write_text(
            "V14 safe stop requested. The pipeline will exit after the current candidate transaction.\n",
            encoding="utf-8",
        )
        return self.stop_file

    def clear_safe_stop(self) -> bool:
        existed = self.stop_file.exists()
        self.stop_file.unlink(missing_ok=True)
        return existed

    # ------------------------------------------------------------------
    # Checkpoint lifecycle
    # ------------------------------------------------------------------

    def _state_files_to_snapshot(self) -> list[Path]:
        files: dict[str, Path] = {}
        for name in self.CORE_STATE_FILES:
            path = self.results_dir / name
            if path.exists() and path.is_file():
                files[path.name] = path
        for path in self.results_dir.glob("v14_*"):
            if not path.is_file():
                continue
            lowered = path.name.casefold()
            if any(token in lowered for token in ("state", "memory", "pair")):
                files[path.name] = path
        return [files[name] for name in sorted(files)]

    def create_selection_checkpoint(self) -> SelectionCheckpoint:
        """Capture mutable optimizer state before calling ``next_suggestion``."""
        self.assert_canonical_matches_best()
        checkpoint_id = uuid.uuid4().hex
        root = self.checkpoint_root / f"checkpoint_{checkpoint_id}"
        snapshot_dir = root / "optimizer_state_before_selection"
        snapshot_dir.mkdir(parents=True, exist_ok=False)

        files: list[dict[str, Any]] = []
        for source in self._state_files_to_snapshot():
            target = snapshot_dir / source.name
            shutil.copy2(source, target)
            files.append(
                {
                    "name": source.name,
                    "source": str(source),
                    "snapshot": str(target),
                    "sha256": _sha256(source),
                    "existed_before_selection": True,
                }
            )

        manifest = {
            "schema_version": 1,
            "optimizer_version": self.VERSION,
            "checkpoint_id": checkpoint_id,
            "created_at": _utc_now(),
            "status": "checkpoint_created",
            "canonical_hashes": self.canonical_best_hashes(),
            "state_files": files,
        }
        manifest_file = root / "checkpoint.json"
        _write_json_atomic(manifest_file, manifest)
        return SelectionCheckpoint(root, snapshot_dir, manifest_file, manifest)

    def discard_checkpoint(self, checkpoint: SelectionCheckpoint) -> None:
        shutil.rmtree(checkpoint.root, ignore_errors=True)

    def begin_transaction(
        self,
        checkpoint: SelectionCheckpoint,
        *,
        suggestion: dict[str, Any],
        selection_reason: str,
    ) -> CandidateTransaction:
        """Promote the pre-selection checkpoint to a durable candidate journal."""
        candidate_id = str(suggestion.get("candidate_id") or uuid.uuid4().hex)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        root = self.root / f"txn_{stamp}_{candidate_id}"
        if root.exists():
            root = self.root / f"txn_{stamp}_{candidate_id}_{uuid.uuid4().hex[:8]}"
        root.mkdir(parents=True, exist_ok=False)

        checkpoint_destination = root / "optimizer_state_before_selection"
        shutil.move(str(checkpoint.snapshot_dir), str(checkpoint_destination))
        checkpoint_manifest = checkpoint.manifest.copy()
        checkpoint_manifest["promoted_to_transaction_at"] = _utc_now()
        checkpoint_manifest["transaction_root"] = str(root)
        _write_json_atomic(root / "checkpoint.json", checkpoint_manifest)
        shutil.rmtree(checkpoint.root, ignore_errors=True)

        candidate_config = root / "candidate_config.xlsx"
        candidate_dat = root / "candidate_input.dat"
        manifest = {
            "schema_version": 1,
            "optimizer_version": self.VERSION,
            "transaction_id": root.name,
            "candidate_id": candidate_id,
            "status": "selected",
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "selection_reason": str(selection_reason),
            "suggestion": _json_safe(suggestion),
            "canonical_hashes_at_selection": self.canonical_best_hashes(),
            "best_config_file": str(self.best_config_file),
            "candidate_config_file": str(candidate_config),
            "candidate_dat_file": str(candidate_dat),
            "checkpoint_file": str(root / "checkpoint.json"),
            "run_folder": "",
            "run_status": "",
            "event": {},
            "recovery": {},
        }
        manifest_file = root / "transaction.json"
        _write_json_atomic(manifest_file, manifest)
        transaction = CandidateTransaction(
            root=root,
            manifest_file=manifest_file,
            candidate_config_file=candidate_config,
            candidate_dat_file=candidate_dat,
            checkpoint=SelectionCheckpoint(
                root=root,
                snapshot_dir=checkpoint_destination,
                manifest_file=root / "checkpoint.json",
                manifest=checkpoint_manifest,
            ),
            manifest=manifest,
        )
        self._append_audit({"action": "transaction_selected", **manifest})
        return transaction

    def _read_transaction(self, transaction: CandidateTransaction | Path) -> dict[str, Any]:
        path = transaction.manifest_file if isinstance(transaction, CandidateTransaction) else Path(transaction) / "transaction.json"
        result = _read_json(path, default={})
        if not result:
            raise TransactionError(f"Could not read transaction manifest: {path}")
        return result

    def update_transaction(
        self,
        transaction: CandidateTransaction,
        *,
        status: str | None = None,
        **updates: Any,
    ) -> dict[str, Any]:
        manifest = self._read_transaction(transaction)
        if status is not None:
            manifest["status"] = str(status)
        manifest.update(_json_safe(updates))
        manifest["updated_at"] = _utc_now()
        _write_json_atomic(transaction.manifest_file, manifest)
        return manifest

    def prepare_candidate(self, transaction: CandidateTransaction) -> CandidateConfigReader:
        """Copy the accepted best workbook into an isolated candidate workspace."""
        self.assert_canonical_matches_best()
        _copy_atomic(self.best_config_file, transaction.candidate_config_file)
        manifest = self.update_transaction(
            transaction,
            status="candidate_prepared",
            candidate_config_sha256=_sha256(transaction.candidate_config_file),
            candidate_semantic_sha256=_semantic_workbook_sha256(transaction.candidate_config_file),
        )
        self._append_audit({"action": "candidate_prepared", **manifest})
        return CandidateConfigReader(
            self.paths,
            transaction.candidate_config_file,
            transaction.candidate_dat_file,
        )

    def apply_suggestion_to_candidate(
        self,
        transaction: CandidateTransaction,
        suggestion: pd.DataFrame,
    ) -> None:
        """Apply suggestions to the isolated workbook, never agent_config.xlsx."""
        if suggestion is None or suggestion.empty:
            raise TransactionError("Cannot apply an empty candidate suggestion.")
        if not transaction.candidate_config_file.exists():
            raise TransactionError("Candidate workbook is missing; call prepare_candidate first.")

        sheets = CandidateConfigReader(
            self.paths,
            transaction.candidate_config_file,
            transaction.candidate_dat_file,
        ).all_sheets()
        if "parameters" not in sheets:
            raise TransactionError("Candidate workbook has no 'parameters' sheet.")
        parameters = sheets["parameters"].copy()
        if "parameter" not in parameters.columns or "value" not in parameters.columns:
            raise TransactionError("Candidate workbook parameters sheet needs parameter and value columns.")

        applied: list[dict[str, Any]] = []
        for _, row in suggestion.iterrows():
            parameter = str(row.get("parameter", "")).strip()
            if not parameter:
                raise TransactionError("Candidate suggestion has an empty parameter name.")
            mask = parameters["parameter"].astype(str).str.strip().eq(parameter)
            if int(mask.sum()) != 1:
                raise TransactionError(
                    f"Expected exactly one parameter row for {parameter!r}; found {int(mask.sum())}."
                )
            value = row.get("new_value")
            numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
            if pd.isna(numeric):
                raise TransactionError(f"Candidate suggestion has invalid new_value for {parameter!r}: {value!r}")
            parameters.loc[mask, "value"] = float(numeric)
            applied.append(
                {
                    "parameter": parameter,
                    "old_value": _json_safe(row.get("old_value")),
                    "new_value": float(numeric),
                }
            )

        sheets["parameters"] = parameters
        _write_workbook_atomic(transaction.candidate_config_file, sheets)
        manifest = self.update_transaction(
            transaction,
            status="candidate_config_applied",
            applied_parameters=applied,
            candidate_config_sha256=_sha256(transaction.candidate_config_file),
            candidate_semantic_sha256=_semantic_workbook_sha256(transaction.candidate_config_file),
        )
        self._append_audit({"action": "candidate_config_applied", **manifest})

    def evaluation_context_sha256(self, config_file: Path) -> str:
        """Hash the model-evaluation context used by the candidate cache.

        The complete workbook is hashed semantically.  Key external inputs that
        can change the objective for the same parameter vector are also hashed
        when present.  This keeps cache reuse local to an equivalent model and
        observation context.
        """
        config_file = Path(config_file)
        reader = CandidateConfigReader(
            self.paths, config_file, self.paths.input_dir / "__v14_cache_probe__.dat"
        )
        files: dict[str, Any] = {
            "candidate_workbook_semantic_sha256": _semantic_workbook_sha256(config_file),
        }
        external = {
            "template": reader.template_file(),
            "observed": reader.observed_file(),
            "post_script": getattr(self.paths, "post_script", None),
            "objective_reference": self.results_dir / "objective_reference_V13.xlsx",
        }
        for label, path in external.items():
            if path is None:
                files[label] = None
                continue
            path = Path(path)
            files[label] = _sha256(path) if path.exists() and path.is_file() else None
        encoded = json.dumps(
            files, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return sha256(encoded).hexdigest().upper()

    def _cache_entries(self) -> list[dict[str, Any]]:
        if not self.cache_file.exists():
            return []
        entries: list[dict[str, Any]] = []
        for line in self.cache_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except Exception:
                continue
            if isinstance(value, dict):
                entries.append(value)
        return entries

    def register_evaluated_configuration(
        self,
        *,
        config_file: Path,
        total_score: float | None,
        run_folder: Path | str,
        run_status: str,
        source: str,
        source_candidate_id: str = "",
        source_transaction: str = "",
        valid: bool = True,
    ) -> dict[str, Any] | None:
        """Persist a reusable valid evaluation, including the campaign baseline."""
        if not valid or total_score is None:
            return None
        try:
            score = float(total_score)
            if pd.isna(score):
                return None
        except Exception:
            return None
        config_file = Path(config_file)
        if not config_file.exists():
            return None
        record = {
            "timestamp": _utc_now(),
            "optimizer_version": self.VERSION,
            "evaluation_context_sha256": self.evaluation_context_sha256(config_file),
            "candidate_semantic_sha256": _semantic_workbook_sha256(config_file),
            "TOTAL_SCORE": score,
            "run_folder": str(run_folder),
            "run_status": str(run_status or "success"),
            "source": str(source),
            "source_candidate_id": str(source_candidate_id),
            "source_transaction": str(source_transaction),
        }
        # Avoid repeatedly appending the same context/score/run entry at each
        # auto-mode startup.
        for prior in reversed(self._cache_entries()):
            if (
                prior.get("evaluation_context_sha256") == record["evaluation_context_sha256"]
                and str(prior.get("run_folder", "")) == record["run_folder"]
                and float(prior.get("TOTAL_SCORE", float("nan"))) == score
            ):
                return prior
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        with self.cache_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(_json_safe(record), ensure_ascii=False) + "\n")
        self._append_audit({"action": "candidate_cache_registered", **record})
        return record

    def _find_v14_history_cache(self, config_file: Path) -> dict[str, Any] | None:
        """Bootstrap cache reuse from run_ranking for runs referenced by V14 audit.

        This migration path matters when upgrading an already-started V14.4
        campaign: the original baseline may predate ``v14_candidate_cache.jsonl``.
        A historical row is eligible only when (1) its run folder is referenced
        by the V14 decision log and (2) every parameter value matches the
        candidate workbook.
        """
        decision_file = self.results_dir / "calibration_decision_log.xlsx"
        ranking_file = getattr(self.paths, "ranking_file", self.results_dir / "run_ranking.xlsx")
        ranking_file = Path(ranking_file)
        if not decision_file.exists() or not ranking_file.exists():
            return None
        try:
            decisions = pd.read_excel(decision_file)
            ranking = pd.read_excel(ranking_file)
            parameters = pd.read_excel(config_file, sheet_name="parameters")
        except Exception:
            return None
        if decisions.empty or ranking.empty or parameters.empty:
            return None
        if "run_folder" not in ranking.columns or "TOTAL_SCORE" not in ranking.columns:
            return None

        referenced_runs: set[str] = set()
        for column in ("current_best_run_folder",):
            if column in decisions.columns:
                referenced_runs.update(
                    str(value).strip()
                    for value in decisions[column].dropna().tolist()
                    if str(value).strip() and str(value).lower() != "nan"
                )
        for column in ("event_json", "diagnostics_json"):
            if column not in decisions.columns:
                continue
            for raw in decisions[column].dropna().tolist():
                try:
                    payload = json.loads(str(raw))
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                for key in (
                    "run_folder",
                    "parent_best_run_folder",
                    "baseline_run_folder",
                    "candidate_run_folder",
                ):
                    value = str(payload.get(key, "")).strip()
                    if value and value.lower() != "nan":
                        referenced_runs.add(value)
                nested = payload.get("v14_residual_diagnostics", {})
                if isinstance(nested, dict):
                    for key in ("baseline_run_folder", "candidate_run_folder"):
                        value = str(nested.get(key, "")).strip()
                        if value and value.lower() != "nan":
                            referenced_runs.add(value)
        if not referenced_runs:
            return None

        candidates = ranking[ranking["run_folder"].astype(str).isin(referenced_runs)].copy()
        if "run_status" in candidates.columns:
            candidates = candidates[
                candidates["run_status"].astype(str).str.lower().isin(
                    ["success", "success_with_retries", "partial_success"]
                )
            ]
        if candidates.empty:
            return None

        param_rows = []
        for _, row in parameters.iterrows():
            name = str(row.get("parameter", "")).strip()
            if not name or name.lower() == "nan":
                continue
            if name not in candidates.columns:
                return None
            param_rows.append((name, row.get("value")))
        if not param_rows:
            return None

        mask = pd.Series(True, index=candidates.index)
        for name, target in param_rows:
            target_num = pd.to_numeric(pd.Series([target]), errors="coerce").iloc[0]
            series_num = pd.to_numeric(candidates[name], errors="coerce")
            if pd.notna(target_num):
                tolerance = max(abs(float(target_num)) * 1e-12, 1e-15)
                mask &= (series_num - float(target_num)).abs() <= tolerance
            else:
                mask &= candidates[name].astype(str).eq(str(target))
        matches = candidates[mask].copy()
        if matches.empty:
            return None
        matches["_score"] = pd.to_numeric(matches["TOTAL_SCORE"], errors="coerce")
        matches = matches[matches["_score"].notna()]
        if matches.empty:
            return None
        row = matches.iloc[-1]
        return {
            "cache_hit": True,
            "cache_source": "v14_history_bootstrap",
            "source_transaction": "",
            "source_candidate_id": "",
            "run_folder": str(row.get("run_folder", "")),
            "run_status": str(row.get("run_status", "success")),
            "TOTAL_SCORE": float(row["_score"]),
        }

    def find_cached_candidate(self, transaction: CandidateTransaction) -> dict[str, Any] | None:
        """Return a previous valid result for the exact evaluation context."""
        current = self._read_transaction(transaction)
        context_hash = self.evaluation_context_sha256(transaction.candidate_config_file)
        semantic_hash = _semantic_workbook_sha256(transaction.candidate_config_file)
        self.update_transaction(
            transaction,
            candidate_semantic_sha256=semantic_hash,
            evaluation_context_sha256=context_hash,
        )

        for entry in reversed(self._cache_entries()):
            if str(entry.get("evaluation_context_sha256", "")) != context_hash:
                continue
            try:
                score = float(entry.get("TOTAL_SCORE"))
                if pd.isna(score):
                    continue
            except Exception:
                continue
            return {
                "cache_hit": True,
                "evaluation_context_sha256": context_hash,
                "candidate_semantic_sha256": semantic_hash,
                "source_transaction": str(entry.get("source_transaction", "")),
                "source_candidate_id": str(entry.get("source_candidate_id", "")),
                "cache_source": str(entry.get("source", "")),
                "run_folder": str(entry.get("run_folder", "")),
                "run_status": str(entry.get("run_status", "success")),
                "TOTAL_SCORE": score,
            }

        historical = self._find_v14_history_cache(transaction.candidate_config_file)
        if historical is not None:
            historical["evaluation_context_sha256"] = context_hash
            historical["candidate_semantic_sha256"] = semantic_hash
            # Promote the migration hit into the persistent V14.4.7 cache.
            self.register_evaluated_configuration(
                config_file=transaction.candidate_config_file,
                total_score=historical.get("TOTAL_SCORE"),
                run_folder=historical.get("run_folder", ""),
                run_status=historical.get("run_status", "success"),
                source="v14_history_bootstrap",
                valid=True,
            )
            return historical
        return None

    def record_cache_hit(
        self, transaction: CandidateTransaction, cache_record: dict[str, Any]
    ) -> None:
        """Audit a reused candidate result without pretending MIN3P ran again."""
        manifest = self.update_transaction(
            transaction,
            cache_hit=True,
            cache_source=str(cache_record.get("cache_source", "")),
            cache_source_transaction=str(cache_record.get("source_transaction", "")),
            cache_source_candidate_id=str(cache_record.get("source_candidate_id", "")),
            cached_run_folder=str(cache_record.get("run_folder", "")),
            cached_TOTAL_SCORE=cache_record.get("TOTAL_SCORE"),
        )
        self._append_audit({"action": "candidate_cache_hit", **manifest})

    def candidate_config_reader(self, transaction: CandidateTransaction) -> CandidateConfigReader:
        if not transaction.candidate_config_file.exists():
            raise TransactionError("Candidate workbook is missing.")
        return CandidateConfigReader(
            self.paths,
            transaction.candidate_config_file,
            transaction.candidate_dat_file,
        )

    def mark_running(self, transaction: CandidateTransaction) -> None:
        manifest = self.update_transaction(transaction, status="running")
        self._append_audit({"action": "candidate_run_started", **manifest})

    def attach_run_artifacts(
        self,
        transaction: CandidateTransaction,
        *,
        run_folder: Path | str,
        run_status: str,
        return_code: int | None,
    ) -> None:
        run_folder = Path(run_folder) if run_folder else Path()
        copied: list[str] = []
        if str(run_folder) and run_folder.exists() and run_folder.is_dir():
            for source, name in (
                (transaction.candidate_config_file, "candidate_config.xlsx"),
                (transaction.candidate_dat_file, "candidate_input.dat"),
            ):
                if source.exists():
                    _copy_atomic(source, run_folder / name)
                    copied.append(str(run_folder / name))
            _copy_atomic(transaction.manifest_file, run_folder / "v14_transaction.json")
            copied.append(str(run_folder / "v14_transaction.json"))
        manifest = self.update_transaction(
            transaction,
            status="run_completed",
            run_folder=str(run_folder) if str(run_folder) else "",
            run_status=str(run_status or ""),
            return_code=return_code,
            run_artifacts=copied,
        )
        self._append_audit({"action": "candidate_run_completed", **manifest})

    def mark_observation_pending(
        self,
        transaction: CandidateTransaction,
        payload: dict[str, Any],
    ) -> None:
        """Persist enough evidence to diagnose/recover an interrupted observation."""
        manifest = self.update_transaction(
            transaction,
            status="observation_pending",
            observation_payload=_json_safe(payload),
        )
        self._append_audit({"action": "candidate_observation_pending", **manifest})

    def mark_observation_failed(
        self,
        transaction: CandidateTransaction,
        payload: dict[str, Any],
        error: Exception,
    ) -> None:
        """Journal an observation failure before checkpoint restoration."""
        manifest = self.update_transaction(
            transaction,
            status="observation_failed",
            observation_payload=_json_safe(payload),
            observation_error={
                "type": type(error).__name__,
                "message": str(error),
            },
        )
        self._append_audit({"action": "candidate_observation_failed", **manifest})

    def mark_decision_observed(self, transaction: CandidateTransaction, event: dict[str, Any]) -> None:
        manifest = self.update_transaction(
            transaction,
            status="decision_observed",
            event=_json_safe(event),
        )
        self._append_audit({"action": "candidate_decision_observed", **manifest})

    def mark_accepted_commit_pending(self, transaction: CandidateTransaction, event: dict[str, Any]) -> None:
        manifest = self.update_transaction(
            transaction,
            status="accepted_commit_pending",
            event=_json_safe(event),
        )
        self._append_audit({"action": "candidate_accept_commit_pending", **manifest})

    def commit_candidate_to_canonical(self, transaction: CandidateTransaction) -> None:
        """Atomically replace the canonical config only for an accepted candidate."""
        if not transaction.candidate_config_file.exists():
            raise TransactionError("Accepted candidate workbook is missing.")
        _copy_atomic(transaction.candidate_config_file, self.canonical_config)
        manifest = self.update_transaction(
            transaction,
            status="canonical_config_committed",
            canonical_hash_after_commit=_sha256(self.canonical_config),
        )
        self._append_audit({"action": "canonical_config_committed", **manifest})

    def complete_transaction(
        self,
        transaction: CandidateTransaction,
        *,
        outcome: str,
        event: dict[str, Any] | None = None,
    ) -> None:
        if outcome not in {"accepted", "rejected", "invalid", "bounded"}:
            raise TransactionError(f"Unsupported completed transaction outcome: {outcome}")
        manifest = self.update_transaction(
            transaction,
            status=outcome,
            event=_json_safe(event or self._read_transaction(transaction).get("event", {})),
            completed_at=_utc_now(),
        )
        self._append_audit({"action": f"transaction_{outcome}", **manifest})

    # ------------------------------------------------------------------
    # Interrupted transaction recovery
    # ------------------------------------------------------------------

    def _checkpoint_manifest(self, transaction_root: Path) -> dict[str, Any]:
        return _read_json(transaction_root / "checkpoint.json", default={})

    def _restore_optimizer_checkpoint(self, transaction_root: Path) -> list[str]:
        checkpoint = self._checkpoint_manifest(transaction_root)
        snapshot_dir = transaction_root / "optimizer_state_before_selection"
        restored: list[str] = []
        captured_names: set[str] = set()
        for item in checkpoint.get("state_files", []) or []:
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            captured_names.add(name)
            source = snapshot_dir / name
            destination = self.results_dir / name
            if source.exists():
                _copy_atomic(source, destination)
                restored.append(str(destination))

        # Selection can create a state/memory/pair file that did not exist at
        # checkpoint time.  It is part of the unfinished transaction and must
        # not survive recovery.  Audit/history files are deliberately excluded.
        for current in self._state_files_to_snapshot():
            if current.name not in captured_names:
                current.unlink(missing_ok=True)
                restored.append(f"removed:{current}")
        return restored

    def _find_transactions(self) -> Iterable[Path]:
        if not self.root.exists():
            return []
        return sorted(
            [p for p in self.root.glob("txn_*") if p.is_dir() and (p / "transaction.json").exists()],
            key=lambda p: p.stat().st_mtime,
        )

    def _restore_preselection_checkpoint(
        self,
        transaction_root: Path,
        *,
        reason: str,
        restore_canonical_if_needed: bool = True,
    ) -> dict[str, Any]:
        manifest_file = transaction_root / "transaction.json"
        manifest = _read_json(manifest_file, default={})
        if not manifest:
            raise TransactionRecoveryRequired(f"Missing transaction manifest: {manifest_file}")

        before_hashes = self.canonical_best_hashes()
        canonical_recovery: dict[str, Any] | None = None
        if restore_canonical_if_needed and not before_hashes.get("match"):
            canonical_recovery = self.restore_canonical_best(
                backup=True,
                reason="interrupted_candidate",
            )

        restored_files = self._restore_optimizer_checkpoint(transaction_root)
        manifest.update(
            {
                "status": "recovered",
                "updated_at": _utc_now(),
                "recovery": {
                    "reason": str(reason),
                    "restored_optimizer_files": restored_files,
                    "canonical_hashes_before": before_hashes,
                    "canonical_recovery": canonical_recovery or {},
                    "canonical_hashes_after": self.canonical_best_hashes(),
                },
            }
        )
        _write_json_atomic(manifest_file, manifest)
        self._append_audit({"action": "transaction_recovered", **manifest})
        return manifest

    def cancel_and_recover(
        self,
        transaction: CandidateTransaction,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Recover a candidate interrupted before a committed decision.

        A candidate with a durable observed decision is intentionally not
        rewritten here.  It is marked for manual recovery instead, because the
        optimizer may already have advanced state while a cross-file accepted
        commit was incomplete.
        """
        manifest = self._read_transaction(transaction)
        status = str(manifest.get("status", ""))
        if status in {"selected", "candidate_prepared", "candidate_config_applied", "running", "run_completed", "observation_pending", "observation_failed"}:
            return self._restore_preselection_checkpoint(transaction.root, reason=reason)
        if status == "decision_observed":
            manifest["status"] = "manual_recovery_required"
            manifest["updated_at"] = _utc_now()
            manifest["recovery"] = {
                "reason": reason,
                "message": (
                    "A decision was already observed. The optimizer state may have advanced; "
                    "do not restore the pre-selection checkpoint automatically."
                ),
            }
            _write_json_atomic(transaction.manifest_file, manifest)
            self._append_audit({"action": "transaction_manual_recovery_required", **manifest})
            raise TransactionRecoveryRequired(
                "Interrupted transaction already has an observed decision. "
                f"Review {transaction.manifest_file} before recovery."
            )
        if status == "accepted_commit_pending":
            manifest["status"] = "manual_recovery_required"
            manifest["updated_at"] = _utc_now()
            manifest["recovery"] = {
                "reason": reason,
                "message": "Accepted commit was pending; manual reconciliation is required.",
            }
            _write_json_atomic(transaction.manifest_file, manifest)
            self._append_audit({"action": "transaction_manual_recovery_required", **manifest})
            raise TransactionRecoveryRequired(
                "Accepted transaction commit was interrupted. Manual reconciliation is required."
            )
        return manifest

    def recover_interrupted_transactions(
        self,
        *,
        reason: str = "recover_interrupted_command",
    ) -> dict[str, Any]:
        """Recover all unambiguous cancelled/interrupted transactions.

        Returns a structured summary.  Transactions with a durable observed
        decision are never silently rewritten; they are reported as requiring
        manual reconciliation.
        """
        summary: dict[str, Any] = {
            "timestamp": _utc_now(),
            "optimizer_version": self.VERSION,
            "recovered": [],
            "manual_recovery_required": [],
            "skipped": [],
        }
        for root in self._find_transactions():
            manifest = _read_json(root / "transaction.json", default={})
            status = str(manifest.get("status", ""))
            if status not in self.ACTIVE_STATUSES:
                summary["skipped"].append({"transaction": str(root), "status": status})
                continue
            try:
                if status in {"selected", "candidate_prepared", "candidate_config_applied", "running", "run_completed", "observation_pending", "observation_failed"}:
                    recovered = self._restore_preselection_checkpoint(
                        root,
                        reason=str(reason),
                    )
                    summary["recovered"].append(
                        {"transaction": str(root), "candidate_id": recovered.get("candidate_id", "")}
                    )
                else:
                    summary["manual_recovery_required"].append(
                        {
                            "transaction": str(root),
                            "candidate_id": manifest.get("candidate_id", ""),
                            "status": status,
                            "reason": "decision already observed or accepted commit pending",
                        }
                    )
            except Exception as exc:
                summary["manual_recovery_required"].append(
                    {
                        "transaction": str(root),
                        "candidate_id": manifest.get("candidate_id", ""),
                        "status": status,
                        "reason": f"{type(exc).__name__}: {exc}",
                    }
                )
        self._append_audit({"action": "recover_interrupted_transactions", **summary})
        return summary

    def unfinished_transactions(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for root in self._find_transactions():
            manifest = _read_json(root / "transaction.json", default={})
            status = str(manifest.get("status", ""))
            if status in self.ACTIVE_STATUSES or status == "manual_recovery_required":
                items.append(
                    {
                        "transaction": str(root),
                        "candidate_id": manifest.get("candidate_id", ""),
                        "status": status,
                        "parameter": (manifest.get("suggestion", {}) or {}).get("parameter", ""),
                    }
                )
        return items

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------

    def _append_audit(self, record: dict[str, Any]) -> None:
        self.audit_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"timestamp": _utc_now(), "optimizer_version": self.VERSION, **_json_safe(record)}
        with self.audit_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


__all__ = [
    "CandidateConfigReader",
    "CandidateTransaction",
    "SelectionCheckpoint",
    "TransactionError",
    "TransactionRecoveryRequired",
    "V14TransactionManager",
]
