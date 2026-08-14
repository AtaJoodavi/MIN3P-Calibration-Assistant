from __future__ import annotations

"""Small, dependency-light utilities shared by the V14 modules."""

from datetime import datetime, timezone
import json
import os
import shutil
import time
import uuid
import warnings
from pathlib import Path
from typing import Any

import pandas as pd


def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def as_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or pd.isna(value):
            return default
        parsed = float(value)
        return parsed if pd.notna(parsed) else default
    except Exception:
        return default


def as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "enabled", "active"}


def json_safe(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return None if pd.isna(value) else value
    if hasattr(value, "item"):
        try:
            return json_safe(value.item())
        except Exception:
            pass
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v) for v in value]
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return str(value)


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        return loaded
    except Exception:
        return default


def _retryable_replace_error(exc: OSError) -> bool:
    """Return True for transient Windows/permission replacement failures."""
    if isinstance(exc, PermissionError):
        return True
    return getattr(exc, "winerror", None) in {5, 32, 33}


def replace_with_retry(
    source: Path,
    destination: Path,
    *,
    attempts: int = 12,
    base_delay_seconds: float = 0.05,
) -> Path:
    """Atomically replace *destination* and tolerate brief Windows file locks.

    Excel, antivirus/indexing, and delayed handle release can transiently make
    ``os.replace`` raise WinError 5/32/33 even after our own file handle has
    closed.  A long calibration campaign should not stop for such a brief lock.
    Non-transient errors are raised immediately.
    """
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    attempts = max(int(attempts), 1)
    last_error: OSError | None = None
    for attempt in range(attempts):
        try:
            os.replace(source, destination)
            return destination
        except OSError as exc:
            if not _retryable_replace_error(exc):
                raise
            last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(float(base_delay_seconds) * (attempt + 1), 0.50))
    if last_error is not None:
        raise last_error
    return destination


def _unique_temp_path(path: Path, *, suffix: str | None = None) -> Path:
    path = Path(path)
    if suffix is None:
        return path.with_name(f"{path.name}.tmp-{uuid.uuid4().hex}")
    return path.with_name(f"{path.stem}.tmp-{uuid.uuid4().hex}{suffix}")


def write_json_atomic(path: Path, payload: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_temp_path(path)
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(json_safe(payload), indent=2, ensure_ascii=False))
            handle.write("\n")
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        replace_with_retry(tmp, path)
        return path
    finally:
        tmp.unlink(missing_ok=True)


def atomic_copy_file(source: Path, destination: Path) -> Path:
    """Copy a file through a unique temporary path then atomically promote it."""
    source = Path(source)
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = _unique_temp_path(destination)
    try:
        shutil.copy2(source, tmp)
        replace_with_retry(tmp, destination)
        return destination
    finally:
        tmp.unlink(missing_ok=True)


def read_excel_safe(path: Path) -> pd.DataFrame:
    try:
        return pd.read_excel(path) if path.exists() else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def write_excel_atomic(path: Path, frame: pd.DataFrame) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = path.suffix or ".xlsx"
    tmp = _unique_temp_path(path, suffix=suffix)
    try:
        frame.to_excel(tmp, index=False)
        replace_with_retry(tmp, path)
        return path
    finally:
        tmp.unlink(missing_ok=True)


def append_excel_atomic(path: Path, rows: list[dict[str, Any]] | pd.DataFrame) -> Path:
    new = rows.copy() if isinstance(rows, pd.DataFrame) else pd.DataFrame(rows)
    if new.empty:
        return path
    new = new.map(json_safe)
    old = read_excel_safe(path)
    if old.empty:
        out = new
    else:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="The behavior of DataFrame concatenation with empty or all-NA entries is deprecated.*",
                category=FutureWarning,
            )
            out = pd.concat([old, new], ignore_index=True, sort=False)
    return write_excel_atomic(path, out)


def normalise_parameter(value: Any) -> str:
    return str(value or "").strip().casefold()


def read_simple_yaml(path: Path) -> dict[str, Any]:
    """Read a flat YAML mapping without making PyYAML mandatory.

    V14 configuration templates intentionally use simple scalar keys.  If
    PyYAML is installed, it is used.  Otherwise this parser handles the V14
    scalar template safely and ignores comments/unsupported nesting.
    """
    if not path.exists():
        return {}
    try:
        import yaml  # type: ignore
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        result: dict[str, Any] = {}
        for raw_line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line or line.startswith(("-", " ")):
                continue
            key, value = [part.strip() for part in line.split(":", 1)]
            if not key:
                continue
            lower = value.casefold()
            if lower in {"true", "yes", "on"}:
                result[key] = True
            elif lower in {"false", "no", "off"}:
                result[key] = False
            elif lower in {"null", "none", ""}:
                result[key] = None
            else:
                try:
                    result[key] = float(value) if any(ch in value for ch in ".eE") else int(value)
                except Exception:
                    result[key] = value.strip('"\'')
        return result
