from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

from .config import ProjectPaths
from .config_reader import ConfigReader
from .io_utils import copy_tree_overwrite, fail_if_missing, log, run_stamp


class Min3pRunner:
    def __init__(self, paths: ProjectPaths, config: ConfigReader):
        self.paths = paths
        self.config = config

    def find_min3p_exe(self) -> Path:
        patterns = ["MIN3P-HPC-V*.exe", "MIN3P*.exe", "min3p*.exe"]
        for folder in [self.paths.project_dir, self.paths.input_dir, self.paths.database_dir, self.paths.agent_core_dir]:
            if not folder.exists():
                continue
            for pattern in patterns:
                matches = sorted(folder.glob(pattern))
                if matches:
                    return matches[0]
        raise FileNotFoundError("MIN3P executable not found in project root, 01_input, database, or 07_agent_core.")

    def create_run_folder(self) -> Path:
        run_dir = self.paths.runs_dir / f"run_{run_stamp()}"
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def copy_required_input_files(self, run_dir: Path, dat_file: Path) -> None:
        """
        Copy only the required MIN3P input/companion files into the run folder.

        Copied from 01_input:
        - *.bcvs
        - *.tem
        - *.dat
        - observed data for min3p.xlsx, using the observed-file path from config

        The generated DAT is copied again after this helper in run(), so it remains
        the authoritative DAT if an older/template DAT with the same name exists.
        """
        patterns = ["*.bcvs", "*.tem", "*.dat"]
        copied = []

        for pattern in patterns:
            for src in sorted(self.paths.input_dir.glob(pattern)):
                if not src.is_file():
                    continue
                dst = run_dir / src.name
                shutil.copy2(src, dst)
                copied.append(src.name)

        obs = self.config.observed_file()
        if obs.exists() and obs.is_file():
            shutil.copy2(obs, run_dir / obs.name)
            copied.append(obs.name)

        # Ensure the generated/calibrated DAT is present and not overwritten by
        # any template/input DAT copied above.
        shutil.copy2(dat_file, run_dir / dat_file.name)
        copied.append(dat_file.name)

        unique_copied = sorted(set(copied))
        log(
            self.paths,
            "Copied required input files to run folder: "
            + (", ".join(unique_copied) if unique_copied else "none found"),
        )

    def run(self, dat_file: Path) -> Tuple[Path, Optional[Path], int]:
        log(self.paths, "V9 STEP 2 - Run MIN3P/post-processing")
        fail_if_missing(dat_file, "Generated DAT file")
        fail_if_missing(self.paths.post_script, "plotsV46.py")
        run_dir = self.create_run_folder()
        self.copy_required_input_files(run_dir, dat_file)
        shutil.copy2(self.paths.post_script, run_dir / self.paths.post_script.name)
        exe = self.find_min3p_exe()
        shutil.copy2(exe, run_dir / exe.name)
        if self.paths.database_dir.exists():
            copy_tree_overwrite(self.paths.database_dir, run_dir / "database")
        result = subprocess.run(
            [sys.executable, str(run_dir / self.paths.post_script.name)],
            cwd=str(run_dir), capture_output=True, text=True,
        )
        (run_dir / "agent_run_log.txt").write_text(
            "=== STDOUT ===\n" + result.stdout + "\n=== STDERR ===\n" + result.stderr,
            encoding="utf-8",
        )
        results_dirs = sorted(run_dir.glob("Results_*"), key=lambda p: p.stat().st_mtime)
        latest_results = results_dirs[-1] if results_dirs else None
        log(self.paths, f"Run folder: {run_dir}")
        log(self.paths, f"plotsV46.py return code: {result.returncode}")
        log(self.paths, f"Latest results: {latest_results}")
        return run_dir, latest_results, result.returncode
