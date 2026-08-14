from __future__ import annotations

import datetime as dt
import math
import shutil
from pathlib import Path
from typing import Any, Dict

import pandas as pd

from .config import ProjectPaths


def safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        if isinstance(value, str):
            value = value.strip().replace("D", "E").replace("d", "e")
            if value == "" or value.lower() == "nan":
                return None
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return None
        return out
    except Exception:
        return None


def timestamp() -> str:
    return dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def run_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def log(paths: ProjectPaths, message: str) -> None:
    paths.ensure_dirs()
    line = f"[{timestamp()}] {message}"
    print(line)
    with open(paths.workflow_log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def fail_if_missing(path: Path, description: str) -> None:
    if not path.exists():
        raise FileNotFoundError(f"{description} not found: {path}")


def append_row_excel(path: Path, row: Dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = pd.DataFrame([row])
    if path.exists():
        try:
            old = pd.read_excel(path)
            if old.empty or old.dropna(how="all").empty:
                out = new
            else:
                out = pd.concat([old.dropna(how="all"), new], ignore_index=True)
        except Exception:
            out = new
    else:
        out = new
    out.to_excel(path, index=False)
    return path


def append_df_excel(path: Path, df: pd.DataFrame) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if df is None or df.empty:
        if not path.exists():
            pd.DataFrame().to_excel(path, index=False)
        return path
    if path.exists():
        try:
            old = pd.read_excel(path)
            if old.empty or old.dropna(how="all").empty:
                out = df.copy()
            else:
                out = pd.concat([old.dropna(how="all"), df], ignore_index=True)
        except Exception:
            out = df.copy()
    else:
        out = df.copy()
    out.to_excel(path, index=False)
    return path


def safe_read_text(path: Path | None, max_chars: int = 120_000) -> str:
    if path is None or not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="ignore")
    if len(text) > max_chars:
        return text[:max_chars] + "\n...[truncated]..."
    return text


def copy_tree_overwrite(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
