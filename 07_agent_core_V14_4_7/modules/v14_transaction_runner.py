from __future__ import annotations

"""V14.2.2 fast, cancellation-safe transactional MIN3P runner.

Candidate calibration runs need the solver output plus the two compact tables
used by the objective evaluator (GBT and GBM).  They do not need the complete
plotsV46.py report, which also generates many profiles, figures, and Excel
workbooks.  This runner preserves the established MIN3P-launch logic inside
plotsV46.py, but executes it through a generated wrapper that:

* exposes exactly one ``.dat`` file to MIN3P: the transaction candidate;
* runs MIN3P using the project's known-good plotsV46.py execution block;
* disables figures and skips heavyweight MVC/GSP/GSM/GST/GSC/GSD/GSS/MMS work;
* keeps only GBT and GBM processing required by ``ResultEvaluator``;
* sends child stdout/stderr directly to files, avoiding a pipe-buffer deadlock;
* terminates the Python + MIN3P process tree on Ctrl+C.

Full plots remain available through ``run_full_postprocess_existing`` and are
never required for an ordinary candidate evaluation.
"""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Optional, Tuple

from modules.io_utils import copy_tree_overwrite, fail_if_missing, log
from modules.runner import Min3pRunner


FAST_WRAPPER_NAME = "v14_fast_candidate_runner.py"
FULL_POSTPROCESS_WRAPPER_NAME = "v14_full_postprocess_runner.py"


class CandidateRunCancelled(KeyboardInterrupt):
    """Raised after the candidate process tree was terminated safely."""

    def __init__(self, run_dir: Path, reason: str = "keyboard_interrupt"):
        super().__init__(reason)
        self.run_dir = Path(run_dir)
        self.reason = str(reason)


def _terminate_process_tree(process: subprocess.Popen) -> None:
    """Terminate a wrapper and all of its children on Windows/POSIX."""
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=20,
            )
        else:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def build_fast_candidate_wrapper(post_script_name: str) -> str:
    """Return a wrapper that retains only solver + objective-essential parsing.

    Importing ``plotsV46.py`` retains the exact local MIN3P invocation logic.
    The wrapper replaces only expensive plotting/profile functions before
    calling that script's normal ``main()`` function.
    """
    return f'''from __future__ import annotations

import importlib.util
from pathlib import Path
import pandas as pd

WORKDIR = Path(__file__).resolve().parent
POST_SCRIPT = WORKDIR / {post_script_name!r}

spec = importlib.util.spec_from_file_location("v14_plots_module", POST_SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Cannot load plots script: {{POST_SCRIPT}}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

# Candidate evaluation needs only the GBT and GBM workbooks used by
# ResultEvaluator.  Do not create figures or full profile/plot reports.
if hasattr(module, "CREATE_PLOTS"):
    module.CREATE_PLOTS = False


def _empty_frame(*_args, **_kwargs):
    return pd.DataFrame()


def _empty_mapping(*_args, **_kwargs):
    return {{}}

# The normal script still runs MIN3P.  These functions are called only after
# the solver stage and are not needed for the calibration objective.
for _name in (
    "make_time_series_plots_from_mvc",
    "make_mineral_plots_from_mms",
):
    if hasattr(module, _name):
        setattr(module, _name, _empty_frame)

for _name in (
    "overlay_gsp_profiles_make_sheets",
    "build_outflow_timeseries_from_gsm",
    "process_concentration_type",
    "build_outflow_timeseries_from_conc",
    "process_gsd_gss_type",
):
    if hasattr(module, _name):
        setattr(module, _name, _empty_mapping)

_original_write_excel = getattr(module, "write_excel", None)
if callable(_original_write_excel):
    def _write_excel_without_empty_workbooks(sheets, path, *args, **kwargs):
        values = sheets.values() if isinstance(sheets, dict) else []
        if values and all(isinstance(value, pd.DataFrame) and value.empty for value in values):
            print(f"[V14.2.2] Skipping empty non-objective workbook: {{path}}", flush=True)
            return None
        return _original_write_excel(sheets, path, *args, **kwargs)
    module.write_excel = _write_excel_without_empty_workbooks

print("[V14.2.2] Fast candidate mode: MIN3P + GBT/GBM objective outputs only.", flush=True)
module.main()
'''


def build_full_postprocess_wrapper(post_script_name: str) -> str:
    """Return a wrapper that fully post-processes existing raw MIN3P output.

    The wrapper explicitly disables solver execution, so this mode never reruns
    MIN3P.  It is intended for a selected best run or manual report generation.
    """
    return f'''from __future__ import annotations

import importlib.util
from pathlib import Path

WORKDIR = Path(__file__).resolve().parent
POST_SCRIPT = WORKDIR / {post_script_name!r}

spec = importlib.util.spec_from_file_location("v14_plots_module", POST_SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Cannot load plots script: {{POST_SCRIPT}}")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

if hasattr(module, "EXECUTE_MIN3P"):
    module.EXECUTE_MIN3P = False
if hasattr(module, "CREATE_PLOTS"):
    module.CREATE_PLOTS = True

print("[V14.2.2] Full post-processing existing MIN3P outputs; solver execution is disabled.", flush=True)
module.main()
'''


def _tail_text(path: Path, *, limit: int = 24_000) -> str:
    """Read only the end of an execution log for the compact agent log."""
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return text[-limit:]
    except Exception as exc:
        return f"<could not read {path.name}: {type(exc).__name__}: {exc}>"


class V14TransactionalMin3pRunner(Min3pRunner):
    """Run V14 transactional candidates without full plotting overhead."""

    execution_mode = "min3p_plus_fast_gbt_gbm_objective_postprocess"

    def _copy_candidate_inputs(self, run_dir: Path, dat_file: Path) -> None:
        """Stage companion files and exactly one candidate DAT in the run folder.

        The base ``Min3pRunner.copy_required_input_files`` copies every ``*.dat``
        from ``01_input``.  That is unsafe for a transaction because the normal
        baseline DAT/template could be selected instead of the candidate DAT.

        TP3 has a boundary-condition sidecar (usually ``*.bcvs``) but may not
        have a ``*.tem`` file.  Some local MIN3P/plotsV46 workflows are also
        sensitive to the original model basename.  Therefore, if an original
        boundary sidecar exists, the candidate DAT contents are staged using
        that original basename, for example:

            tp3_v11_3.dat
            tp3_v11_3.bcvs

        The transaction candidate content is still used; only the filename is
        changed inside the isolated run folder.
        """
        run_dir = Path(run_dir)
        dat_file = Path(dat_file)
        run_dir.mkdir(parents=True, exist_ok=True)

        for stale in run_dir.glob("*.dat"):
            stale.unlink(missing_ok=True)

        copied: list[str] = []

        def _copy_once(source: Path, destination: Path) -> None:
            source = Path(source)
            destination = Path(destination)
            if not source.exists() or not source.is_file():
                return
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                if source.resolve() == destination.resolve():
                    return
            except Exception:
                pass
            shutil.copy2(source, destination)
            copied.append(destination.name)

        # Search both the original input directory and the transaction DAT
        # directory.  The latter is useful if a transaction builder has already
        # created candidate_input sidecars.
        sidecar_suffixes = (".bcv", ".bcvs", ".tem")
        search_dirs: list[Path] = []
        for folder in (self.paths.input_dir, dat_file.parent):
            folder = Path(folder)
            if folder.exists() and folder.is_dir() and folder not in search_dirs:
                search_dirs.append(folder)

        sidecar_sources: list[Path] = []
        for folder in search_dirs:
            for source in folder.iterdir():
                if source.is_file() and source.suffix.lower() in sidecar_suffixes:
                    sidecar_sources.append(source)

        # Use the original boundary-condition basename when available.  This
        # avoids second-run failures caused by candidate_input.dat while the
        # local runtime expects tp3_v11_3.*.
        boundary_sources = [
            source for source in sidecar_sources
            if source.suffix.lower() in (".bcv", ".bcvs")
        ]
        if boundary_sources:
            preferred_boundary = sorted(
                boundary_sources,
                key=lambda item: (
                    item.suffix.lower() != ".bcvs",
                    item.name.lower(),
                ),
            )[0]
            active_stem = preferred_boundary.stem
        else:
            active_stem = dat_file.stem

        # Copy the candidate DAT content using the active model basename.
        destination_dat = run_dir / f"{active_stem}.dat"
        _copy_once(dat_file, destination_dat)

        # Copy all sidecars using their original names.  Also create active-stem
        # aliases so MIN3P can find boundary/temperature files if it infers the
        # names from the DAT basename.
        copied_sidecar_suffixes: set[str] = set()
        for source in sorted(set(sidecar_sources), key=lambda item: item.name.lower()):
            suffix = source.suffix.lower()
            copied_sidecar_suffixes.add(suffix)

            _copy_once(source, run_dir / source.name)
            _copy_once(source, run_dir / f"{active_stem}{suffix}")

            # Compatibility alias for older patches/scripts that still look for
            # candidate_input.* in the run folder.  This is harmless because it
            # does not create another DAT file.
            _copy_once(source, run_dir / f"{dat_file.stem}{suffix}")

        observed = self.config.observed_file()
        if observed.exists() and observed.is_file():
            _copy_once(observed, run_dir / observed.name)

        visible_dat_files = sorted(run_dir.glob("*.dat"))
        if visible_dat_files != [destination_dat]:
            raise RuntimeError(
                "Transactional candidate run folder must contain exactly one .dat file; "
                f"found: {[item.name for item in visible_dat_files]}"
            )

        # Boundary sidecar is required for TP3.  TEM is optional because this
        # project does not have a temperature sidecar file.
        has_boundary = any(suffix in copied_sidecar_suffixes for suffix in (".bcv", ".bcvs"))
        if boundary_sources and not has_boundary:
            raise FileNotFoundError(
                "Transactional candidate input staging is incomplete. "
                "A boundary sidecar was detected but was not copied. "
                f"Searched folders: {[str(folder) for folder in search_dirs]}"
            )

        log(
            self.paths,
            "V14.2.2 copied isolated candidate inputs using active model basename "
            f"{active_stem}: " + ", ".join(sorted(set(copied))),
        )

    def _stage_solver_runtime(self, run_dir: Path, dat_file: Path) -> Path:
        """Copy all runtime files required by plotsV46.py and MIN3P.

        Required in every candidate run folder:
          - exactly one active DAT file;
          - boundary sidecar if present in the project input folder;
          - observed Excel file if configured;
          - plotsV46.py;
          - MIN3P executable;
          - database folder, if available.
        """
        fail_if_missing(dat_file, "Transactional generated DAT file")

        run_dir = Path(run_dir)
        dat_file = Path(dat_file)
        run_dir.mkdir(parents=True, exist_ok=True)

        self._copy_candidate_inputs(run_dir, dat_file)

        def _unique_existing(paths: list[Path]) -> list[Path]:
            out: list[Path] = []
            seen: set[str] = set()
            for item in paths:
                try:
                    item = Path(item)
                    key = str(item.resolve()).casefold()
                except Exception:
                    key = str(item).casefold()
                if key in seen:
                    continue
                seen.add(key)
                if item.exists() and item.is_file():
                    out.append(item)
            return out

        def _candidate_search_dirs() -> list[Path]:
            dirs: list[Path] = []

            for attr in (
                "project_dir",
                "input_dir",
                "agent_dir",
                "core_dir",
                "scripts_dir",
                "results_dir",
                "runs_dir",
            ):
                value = getattr(self.paths, attr, None)
                if value is not None:
                    dirs.append(Path(value))

            configured_post_script = getattr(self.paths, "post_script", None)
            if configured_post_script is not None:
                dirs.append(Path(configured_post_script).parent)

            configured_database = getattr(self.paths, "database_dir", None)
            if configured_database is not None:
                dirs.append(Path(configured_database).parent)

            dirs.extend([
                Path.cwd(),
                Path(__file__).resolve().parent,
                Path(__file__).resolve().parent.parent,
                dat_file.parent,
            ])

            clean: list[Path] = []
            seen: set[str] = set()
            for folder in dirs:
                try:
                    folder = Path(folder)
                    key = str(folder.resolve()).casefold()
                except Exception:
                    key = str(folder).casefold()
                if key in seen:
                    continue
                seen.add(key)
                if folder.exists() and folder.is_dir():
                    clean.append(folder)

            return clean

        search_dirs = _candidate_search_dirs()

        # ------------------------------------------------------------------
        # 1. Find and copy plotsV46.py
        # ------------------------------------------------------------------
        plots_candidates: list[Path] = []

        configured_post_script = getattr(self.paths, "post_script", None)
        if configured_post_script is not None:
            plots_candidates.append(Path(configured_post_script))

        for folder in search_dirs:
            plots_candidates.extend(folder.glob("plotsV46.py"))
            plots_candidates.extend(folder.glob("plotsv46.py"))

        project_dir = getattr(self.paths, "project_dir", None)
        if project_dir is not None and Path(project_dir).exists():
            try:
                plots_candidates.extend(Path(project_dir).rglob("plotsV46.py"))
                plots_candidates.extend(Path(project_dir).rglob("plotsv46.py"))
            except Exception as exc:
                log(self.paths, f"WARNING: recursive plotsV46.py search failed: {exc}")

        plots_candidates = _unique_existing(plots_candidates)

        if not plots_candidates:
            raise FileNotFoundError(
                "plotsV46.py was not found. Put plotsV46.py in one of these folders:\n"
                + "\n".join(f"  - {folder}" for folder in search_dirs)
            )

        post_source = sorted(
            plots_candidates,
            key=lambda item: (
                item.name.casefold() != "plotsv46.py",
                len(str(item)),
                item.name.casefold(),
            ),
        )[0]

        post_script = run_dir / post_source.name
        shutil.copy2(post_source, post_script)

        if not post_script.exists():
            raise FileNotFoundError(f"plotsV46.py copy failed: {post_script}")

        # ------------------------------------------------------------------
        # 2. Find and copy MIN3P executable
        # ------------------------------------------------------------------
        exe_candidates: list[Path] = []

        try:
            found_by_base_runner = self.find_min3p_exe()
            if found_by_base_runner is not None:
                exe_candidates.append(Path(found_by_base_runner))
        except Exception as exc:
            log(self.paths, f"WARNING: find_min3p_exe() failed: {type(exc).__name__}: {exc}")

        for folder in search_dirs:
            exe_candidates.extend(folder.glob("MIN3P*.exe"))
            exe_candidates.extend(folder.glob("min3p*.exe"))
            exe_candidates.extend(folder.glob("*MIN3P*.exe"))
            exe_candidates.extend(folder.glob("*min3p*.exe"))

        if project_dir is not None and Path(project_dir).exists():
            try:
                exe_candidates.extend(Path(project_dir).rglob("MIN3P*.exe"))
                exe_candidates.extend(Path(project_dir).rglob("min3p*.exe"))
                exe_candidates.extend(Path(project_dir).rglob("*MIN3P*.exe"))
                exe_candidates.extend(Path(project_dir).rglob("*min3p*.exe"))
            except Exception as exc:
                log(self.paths, f"WARNING: recursive MIN3P executable search failed: {exc}")

        exe_candidates = _unique_existing(exe_candidates)

        if not exe_candidates:
            raise FileNotFoundError(
                "MIN3P executable was not found. Expected a file like "
                "MIN3P-HPC-V2.6.4.903.exe. Put it in one of these folders:\n"
                + "\n".join(f"  - {folder}" for folder in search_dirs)
            )

        executable = sorted(
            exe_candidates,
            key=lambda item: (
                "min3p" not in item.name.casefold(),
                len(str(item)),
                item.name.casefold(),
            ),
        )[0]

        destination_exe = run_dir / executable.name
        shutil.copy2(executable, destination_exe)

        if not destination_exe.exists():
            raise FileNotFoundError(f"MIN3P executable copy failed: {destination_exe}")

        # ------------------------------------------------------------------
        # 3. Copy database folder
        # ------------------------------------------------------------------
        database_copied = False
        database_source = getattr(self.paths, "database_dir", None)
        if database_source is not None:
            database_source = Path(database_source)
            if database_source.exists() and database_source.is_dir():
                copy_tree_overwrite(database_source, run_dir / "database")
                database_copied = True

        if not database_copied:
            for folder in search_dirs:
                candidate_db = folder / "database"
                if candidate_db.exists() and candidate_db.is_dir():
                    copy_tree_overwrite(candidate_db, run_dir / "database")
                    database_copied = True
                    break

        if not database_copied and project_dir is not None and Path(project_dir).exists():
            try:
                for candidate_db in Path(project_dir).rglob("database"):
                    if candidate_db.exists() and candidate_db.is_dir():
                        copy_tree_overwrite(candidate_db, run_dir / "database")
                        database_copied = True
                        break
            except Exception as exc:
                log(self.paths, f"WARNING: recursive database search failed: {exc}")

        # ------------------------------------------------------------------
        # 4. Final hard verification
        # ------------------------------------------------------------------
        missing: list[str] = []

        if not post_script.exists():
            missing.append(post_script.name)

        if not destination_exe.exists():
            missing.append(destination_exe.name)

        if not list(run_dir.glob("*.dat")):
            missing.append("*.dat")

        # If the project has boundary sidecars, the run folder must have them.
        project_has_boundary = (
            bool(list(Path(self.paths.input_dir).glob("*.bcv")))
            or bool(list(Path(self.paths.input_dir).glob("*.bcvs")))
        )
        run_has_boundary = bool(list(run_dir.glob("*.bcv"))) or bool(list(run_dir.glob("*.bcvs")))
        if project_has_boundary and not run_has_boundary:
            missing.append("*.bcv or *.bcvs")

        if missing:
            raise FileNotFoundError(
                "Candidate run folder is missing required runtime file(s): "
                + ", ".join(missing)
                + f"\nRun folder: {run_dir}"
            )

        log(
            self.paths,
            "V14.2.2 staged solver runtime: "
            f"post_script={post_script.name}; "
            f"exe={destination_exe.name}; "
            f"database_exists={(run_dir / 'database').exists()}; "
            f"run_dir={run_dir}"
        )

        return post_script

    def _run_wrapper(self, run_dir: Path, wrapper: Path, *, label: str) -> int:
        """Execute a wrapper without PIPE buffering, with Ctrl+C tree cleanup."""
        stdout_file = run_dir / f"{label}_stdout.log"
        stderr_file = run_dir / f"{label}_stderr.log"
        agent_log = run_dir / "agent_run_log.txt"
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"

        popen_kwargs: dict[str, object] = {
            "cwd": str(run_dir),
            "stdout": None,
            "stderr": None,
            "text": True,
            "env": env,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            popen_kwargs["start_new_session"] = True

        process: subprocess.Popen | None = None
        try:
            with stdout_file.open("w", encoding="utf-8", newline="") as stdout_handle, \
                 stderr_file.open("w", encoding="utf-8", newline="") as stderr_handle:
                popen_kwargs["stdout"] = stdout_handle
                popen_kwargs["stderr"] = stderr_handle
                process = subprocess.Popen(
                    [sys.executable, "-u", str(wrapper)],
                    **popen_kwargs,
                )
                while process.poll() is None:
                    time.sleep(0.25)
                return_code = int(process.wait())
        except KeyboardInterrupt:
            if process is not None:
                _terminate_process_tree(process)
                try:
                    process.wait(timeout=15)
                except Exception:
                    pass
            agent_log.write_text(
                "=== CANCELLATION ===\n"
                "User interrupt received. Process-tree termination was requested.\n"
                f"Execution mode: {{label}}\n"
                f"Stdout log: {{stdout_file.name}}\n"
                f"Stderr log: {{stderr_file.name}}\n"
                "=== STDOUT TAIL ===\n" + _tail_text(stdout_file)
                + "\n=== STDERR TAIL ===\n" + _tail_text(stderr_file),
                encoding="utf-8",
            )
            raise CandidateRunCancelled(run_dir, reason="keyboard_interrupt")

        agent_log.write_text(
            f"=== EXECUTION MODE ===\n{{label}}\n"
            f"=== RETURN CODE ===\n{{return_code}}\n"
            f"=== STDOUT FILE ===\n{{stdout_file.name}}\n"
            f"=== STDERR FILE ===\n{{stderr_file.name}}\n"
            "=== STDOUT TAIL ===\n" + _tail_text(stdout_file)
            + "\n=== STDERR TAIL ===\n" + _tail_text(stderr_file),
            encoding="utf-8",
        )
        return return_code

    @staticmethod
    def _latest_results_dir(run_dir: Path) -> Optional[Path]:
        results_dirs = sorted(run_dir.glob("Results_*"), key=lambda item: item.stat().st_mtime)
        return results_dirs[-1] if results_dirs else None

    def run(self, dat_file: Path) -> Tuple[Path, Optional[Path], int]:
        """Run MIN3P and only the compact GBT/GBM objective post-processing."""
        log(
            self.paths,
            "V14.2.2 STEP 2 - Run isolated candidate: MIN3P + fast GBT/GBM objective extraction",
        )
        run_dir = self.create_run_folder()
        post_script = self._stage_solver_runtime(run_dir, dat_file)
        wrapper = run_dir / FAST_WRAPPER_NAME
        wrapper.write_text(build_fast_candidate_wrapper(post_script.name), encoding="utf-8", newline="\n")

        return_code = self._run_wrapper(run_dir, wrapper, label="v14_fast_candidate")
        results_dir = self._latest_results_dir(run_dir)
        log(self.paths, f"Run folder: {run_dir}")
        log(self.paths, f"V14.2.2 fast candidate return code: {return_code}")
        log(self.paths, f"Latest fast objective results: {results_dir}")
        return run_dir, results_dir, return_code

    def run_full_postprocess_existing(self, run_dir: Path) -> Tuple[Optional[Path], int]:
        """Create the full plots/report bundle for an existing solver run only."""
        run_dir = Path(run_dir)
        fail_if_missing(run_dir, "Existing MIN3P run folder")
        post_script = run_dir / self.paths.post_script.name
        fail_if_missing(post_script, "Copied plotsV46.py in existing run folder")
        wrapper = run_dir / FULL_POSTPROCESS_WRAPPER_NAME
        wrapper.write_text(build_full_postprocess_wrapper(post_script.name), encoding="utf-8", newline="\n")
        return_code = self._run_wrapper(run_dir, wrapper, label="v14_full_postprocess")
        results_dir = self._latest_results_dir(run_dir)
        log(self.paths, f"V14.2.2 full post-processing return code: {return_code}")
        log(self.paths, f"Latest full results: {results_dir}")
        return results_dir, return_code


__all__ = [
    "CandidateRunCancelled",
    "V14TransactionalMin3pRunner",
    "build_fast_candidate_wrapper",
    "build_full_postprocess_wrapper",
]
