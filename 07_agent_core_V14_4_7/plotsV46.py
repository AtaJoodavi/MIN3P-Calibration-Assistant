#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MIN3P post-processing utility

MODIFICATIONS INCLUDED:
1) HARDENED time unit detection (`detect_time_unit`) to prevent false-positives (like accidentally mapping "a" to years).
2) Support for GBM (Master Variables breakthrough curves) with observation overlays.
3) MMS/MVC files robustly parse variable names ensuring commas inside parentheses like "sio2(a,pt)" don't break columns.
4) GBT files are properly supported as time-series breakthrough curves, now with observation data overlays.
5) Handled NaN time sorting bug for _0.* output files so they properly sort to time=0.0 instead of the end of the sheet.
6) Transform H+1 to pH = -log10([H+]) for GST & GSC files.
7) ALWAYS copy and open the latest *.log file.
8) Auto-detects Primary Spatial Axis (z, x, or y) to support non-vertical 1D models for plots.
"""

import re
import math
import os
import platform
import subprocess
import shutil
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple, List, Optional, Union

import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.ticker import ScalarFormatter

# ================= Config =================
EXECUTE_MIN3P = True       # Set to False to skip running MIN3P and ONLY post-process results
SHOW_MIN3P_WINDOW = True
FORCE_RUN = True

# Output control
# CREATE_PLOTS = False -> do NOT create PNG image files
CREATE_PLOTS = False


# CREATE_EXCEL = True  -> write Excel result files
CREATE_EXCEL = True

# Time unit control
# "auto"    = read from .fls / .dat / output headers
# "seconds" = force model time as seconds
# "days"    = force model time as days
# "years"   = force model time as years
TIME_UNIT_OVERRIDE = "seconds"

DEFAULT_OMP_THREADS = 14   # 14 is optimum

# Plot configuration
PLOT_MOL_L = False  # Set to False to disable mol/L plots
PLOT_MG_L = True    # Keep mg/L plots
PLOT_PPB = False    # Keep ppb plots

# Time unit conversion factors (to seconds)
TIME_UNIT_TO_SECONDS = {
    "second": 1.0, "seconds": 1.0, "sec": 1.0, "s": 1.0,
    "minute": 60.0, "minutes": 60.0, "min": 60.0,
    "hour": 3600.0, "hours": 3600.0, "hr": 3600.0, "h": 3600.0,
    "day": 86400.0, "days": 86400.0, "d": 86400.0,
    "week": 604800.0, "weeks": 604800.0, "wk": 604800.0, "w": 604800.0,
    "year": 31536000.0, "years": 31536000.0, "yr": 31536000.0, "y": 31536000.0, "a": 31536000.0
}

# Canonical time-unit aliases. Keep all time conversion in ONE place.
# Important: "m" is not used as minute because MIN3P files often use m for meters.
TIME_UNIT_ALIASES = {
    "second": "seconds", "seconds": "seconds", "sec": "seconds", "secs": "seconds", "s": "seconds",
    "minute": "minutes", "minutes": "minutes", "min": "minutes", "mins": "minutes",
    "hour": "hours", "hours": "hours", "hr": "hours", "hrs": "hours", "h": "hours",
    "day": "days", "days": "days", "d": "days",
    "week": "weeks", "weeks": "weeks", "wk": "weeks", "wks": "weeks", "w": "weeks",
    "year": "years", "years": "years", "yr": "years", "yrs": "years", "y": "years", "a": "years",
}

SECONDS_PER_UNIT = {
    "seconds": 1.0,
    "minutes": 60.0,
    "hours": 3600.0,
    "days": 86400.0,
    "weeks": 604800.0,
    "years": 365.25 * 86400.0,
}

def canonical_time_unit(unit: str) -> str:
    """Return a safe canonical time unit: seconds/minutes/hours/days/weeks/years."""
    if unit is None:
        return "seconds"
    u = str(unit).strip().lower().strip("'\"")
    u = re.sub(r"[^a-z]", "", u)  # removes brackets, commas, etc.
    return TIME_UNIT_ALIASES.get(u, "seconds")

def add_time_columns(df: pd.DataFrame, source_col: str = "time", time_unit: str = "seconds") -> pd.DataFrame:
    """
    Add consistent time_s and time_days columns.

    If the MIN3P output time unit is days, a value of 14 becomes:
      time_s    = 14 * 86400
      time_days = 14
    """
    out = df.copy()
    unit = canonical_time_unit(time_unit)
    factor = SECONDS_PER_UNIT[unit]
    out[source_col] = pd.to_numeric(out[source_col], errors="coerce")
    out = out.dropna(subset=[source_col]).reset_index(drop=True)
    out["time_s"] = out[source_col] * factor
    out["time_days"] = out["time_s"] / 86400.0
    return out

def find_time_column(df: pd.DataFrame) -> Optional[str]:
    """Find the most likely time column after normalized MIN3P column parsing."""
    for c in df.columns:
        cl = str(c).lower()
        if cl == "time" or cl.startswith("time_") or cl.startswith("time"):
            return c
    return None

# Optional: per-species molar masses (g/mol) to enable mg/L & ppb conversions.
MOLAR_MASS: Dict[str, float] = {
    "co3-2": 61.02,
    "cl-1": 35.45,
    "so4-2": 96.06,
    "h3aso4": 74.92,
    "k+1": 39.1,
    "ca+2": 40.08,
    "co+2": 58.93,
    "mg+2": 24.31,
    "na+1": 22.99,
    "ni+2": 58.69,
    "h4sio4": 28.09,
    "fe+2": 55.85,
    "al+3": 26.98,
    "zn+2": 65.38,
    "cd+2": 112.41,
    "cu+2" : 63.55,
    "pb+2": 207.2,
    "fe+3": 55.85,
}

# Outflow selection tolerance
OUTFLOW_Z_TOL = 1e-6 
OBS_OUTFLOW_FILENAME = "observed data for min3p.xlsx"

# ---------- Spatial Axis Helper ----------
def get_primary_spatial_axis(df: pd.DataFrame) -> str:
    """
    Detect whether the model is vertical (z) or horizontal (x/y).
    Checks which coordinate axis has the largest spatial variation.
    """
    best_axis = "z"
    max_range = -1.0
    for col in ["z", "x", "y"]:
        if col in df.columns:
            try:
                c_min = pd.to_numeric(df[col], errors='coerce').min()
                c_max = pd.to_numeric(df[col], errors='coerce').max()
                if pd.notna(c_min) and pd.notna(c_max):
                    rng = float(c_max - c_min)
                    if rng > max_range and rng > 1e-6:
                        max_range = rng
                        best_axis = col
            except:
                pass
    if max_range <= 1e-6:
        for col in ["z", "x", "y"]:
            if col in df.columns: return col
    return best_axis


# ---------- Time unit detection from FLS ----------
def detect_time_unit_from_fls(workdir: Path, fls_units: Dict[str, Tuple[str, str]]) -> Optional[str]:
    """
    Detect the global MIN3P output time unit from the .fls file.

    MIN3P .gbm/.gbt files often contain only numeric time values.
    The .fls file describes the unit of the 'time' column, e.g.:

        column   entry   unit
        1        time    days

    Therefore this function is the safest source for GBM/GBT conversion.
    """
    # First use the already parsed FLS mapping.
    for key, (entry, unit) in fls_units.items():
        if normalize_name(entry) == "time" or key == "time":
            u = canonical_time_unit(str(unit).split()[0])
            if u in SECONDS_PER_UNIT:
                return u

    # Fallback: scan raw .fls text directly.
    time_line_re = re.compile(
        r"^\s*\d+\s+time\s+(seconds?|secs?|s|minutes?|mins?|hours?|hrs?|days?|d|weeks?|wks?|years?|yrs?|y|a)\b",
        re.IGNORECASE
    )
    for fls_path in sorted(workdir.glob("*.fls")):
        try:
            for line in fls_path.read_text(encoding="utf-8", errors="ignore").splitlines():
                m = time_line_re.match(line)
                if m:
                    u = canonical_time_unit(m.group(1))
                    if u in SECONDS_PER_UNIT:
                        return u
        except Exception:
            continue

    return None


def detect_time_unit(workdir: Path, fls_units: Dict[str, Tuple[str, str]]) -> str:
    """
    Detect global model time unit.

    Priority:
    1) .fls file: best for GBM/GBT because these files usually have no unit in their own header.
    2) .dat file: model input definition.
    3) output headers such as .gsp/.gst/.gsc/.gsm if they explicitly print "time = value unit".
    4) fail safely instead of silently assuming seconds.
    """
    # 1. FLS file: safest for post-processing output tables.
    u = detect_time_unit_from_fls(workdir, fls_units)
    if u:
        return u

    # 2. Check .dat file.
    try:
        dat_path = find_dat_file(workdir)
        text = dat_path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip().lower().strip("'\"")
            if not stripped or stripped.startswith("!"):
                continue

            if re.match(r"^time\s+units?$", stripped) or re.match(r"^time\s+units?\s*[=:]", stripped):
                # Inline: time unit = days
                m = re.search(
                    r"time\s+units?\s*[=:]\s*['\"]?([a-zA-Z]+)['\"]?",
                    line,
                    re.IGNORECASE
                )
                if m:
                    u = canonical_time_unit(m.group(1))
                    if u in SECONDS_PER_UNIT:
                        return u

                # Next non-comment line: 'days'
                for j in range(i + 1, min(i + 10, len(lines))):
                    nxt = lines[j].strip().lower().strip("'\"")
                    if not nxt or nxt.startswith("!"):
                        continue
                    u = canonical_time_unit(nxt.split()[0])
                    if u in SECONDS_PER_UNIT:
                        return u
                    break
    except Exception:
        pass

    # 3. Check output headers that explicitly contain unit.
    for ext in ["*.gsp", "*.gst", "*.gsc", "*.gsm"]:
        for fp in workdir.glob(ext):
            try:
                text = fp.read_text(encoding="utf-8", errors="ignore")
                m = re.search(r"time\s*=\s*[0-9.+Ee-]+\s+([a-zA-Z]+)", text, re.IGNORECASE)
                if m:
                    u = canonical_time_unit(m.group(1))
                    if u in SECONDS_PER_UNIT:
                        return u
            except Exception:
                continue

    raise ValueError(
        "Could not detect MIN3P time unit from .fls, .dat, or output headers. "
        "Set TIME_UNIT_OVERRIDE = 'seconds', 'days', or 'years'."
    )


def get_time_conversion_factor(time_unit: str) -> float:
    return SECONDS_PER_UNIT[canonical_time_unit(time_unit)]

def convert_time_to_seconds(time_value: Union[float, int], time_unit: str) -> float:
    factor = get_time_conversion_factor(time_unit)
    return float(time_value) * factor

def convert_time_to_days(time_value: Union[float, int], time_unit: str) -> float:
    seconds = convert_time_to_seconds(time_value, time_unit)
    return seconds / 86400.0


# ---------- FS & run ----------
def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p

def get_script_workdir() -> Path:
    try:
        return Path(__file__).resolve().parent
    except NameError:
        return Path.cwd().resolve()

def find_dat_file(workdir: Path) -> Path:
    c = list(workdir.glob("*.dat"))
    if not c:
        raise FileNotFoundError(f"No .dat in {workdir}")
    return c[0] if len(c) == 1 else max(c, key=lambda p: p.stat().st_mtime)

def find_min3p_exe(workdir: Path) -> Path:
    p = sorted(workdir.glob("MIN3P-HPC-V*.exe")) or \
        sorted([x for x in workdir.glob("*.exe") if "min3p" in x.name.lower()])
    if not p:
        raise FileNotFoundError(f"No MIN3P exe in {workdir}")
    return p[0]

def run_min3p(exe: Path, dat: Path, workdir: Path, force_run=True, show_window=True, omp_threads: int = DEFAULT_OMP_THREADS):
    if not force_run:
        outs = list(workdir.glob("*.gsp")) + list(workdir.glob("*_o.mvc"))
        if outs and max(fp.stat().st_mtime for fp in outs) >= dat.stat().st_mtime:
            print("[INFO] Outputs newer than .dat; skipping run.")
            return
    cmd = [str(exe), dat.stem]
    print(f"[INFO] Running: {' '.join(cmd)}")
    env = os.environ.copy()
    env["OMP_NUM_THREADS"] = str(int(omp_threads))
    env["OMP_PROC_BIND"] = "spread"
    env["OMP_PLACES"] = "cores"

    if show_window and platform.system().lower() == "windows":
        proc = subprocess.Popen(cmd, cwd=workdir, env=env, creationflags=subprocess.CREATE_NEW_CONSOLE)
        code = proc.wait()
        if code: raise RuntimeError(f"MIN3P exit {code}")
    else:
        logs = ensure_dir(workdir / "logs")
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log = logs / f"min3p_run_{ts}.log"
        r = subprocess.run(cmd, cwd=workdir, env=env, input=dat.stem + "\n", capture_output=True, text=True)
        log.write_text(f"=== STDOUT ===\n{r.stdout}\n=== STDERR ===\n{r.stderr}", encoding="utf-8", errors="ignore")
        if r.returncode: raise RuntimeError(f"MIN3P exit {r.returncode}; see {log}")

def copy_and_open_latest_log(workdir: Path, results_root: Path):
    log_files = list(workdir.glob("*.log"))
    logs_dir = workdir / "logs"
    if logs_dir.exists():
        log_files.extend(logs_dir.glob("*.log"))
    if not log_files: return
    latest_log = max(log_files, key=lambda p: p.stat().st_mtime)
    target = results_root / latest_log.name
    try:
        shutil.copy2(latest_log, target)
    except Exception:
        target = latest_log 
    try:
        if platform.system().lower() == "windows": os.startfile(target)
        elif platform.system().lower() == "darwin": subprocess.run(["open", str(target)])
        else: subprocess.run(["xdg-open", str(target)])
    except Exception as e:
        print(f"[WARN] Could not open log file: {e}")

# ---------- Helpers ----------
def normalize_name(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")

def safe_sheet_name(name: str) -> str:
    name = re.sub(r'[\\/*?:\[\]]', "_", name)
    return name[:31] if len(name) > 31 else name

def parse_fls_units(fls_path: Path) -> Dict[str, Tuple[str, str]]:
    mapping = {}
    try: text = fls_path.read_text(encoding="utf-8", errors="ignore")
    except Exception: return mapping
    line_re = re.compile(r"^\s*\d+\s+(.+?)\s+([\-A-Za-z0-9^\/\%\.\* ]+)\s*$")
    for line in text.splitlines():
        if "column" in line.lower() and "entry" in line.lower(): continue
        m = line_re.match(line.strip())
        if not m: continue
        entry = m.group(1).strip()
        unit = m.group(2).strip()
        mapping[normalize_name(entry)] = (entry, unit)
    return mapping

def default_labels_units() -> Dict[str, Tuple[str, str]]:
    return {
        "x": ("x", "m"), "y": ("y", "m"), "z": ("z", "m"),
        "tr": ("Total aqueous concentration, tr", "mol/L H2O"),
        "freshwater_head": ("freshwater head", "m"),
        "fluid_pressure": ("fluid pressure", "Pa"),
        "pressure_head": ("pressure head", "m"),
        "density": ("density", "kg/m^3"),
        "water_saturation": ("water saturation", "-"),
        "water_content": ("water content", "-"),
        "air_saturation": ("air saturation", "-"),
        "air_content": ("air content", "-"),
        "total_root_water_uptake": ("total root water uptake", "m^3/d"),
        "inflow": ("inflow", "m^3/day"),
        "outflow": ("outflow", "m^3/day"),
        "change_in_storage": ("change in storage", "m^3/day"),
    }

def _extract_time_from_header(text: str, time_unit: str = "seconds") -> float:
    patterns = [
        r'[Tt]\s*=\s*"[^"]*[Tt]\s*=\s*([0-9\.\+Ee\-]+)\s+(\w+)"',
        r'[Tt]\s*=\s*([0-9\.\+Ee\-]+)\s+(\w+)',
        r'time\s*=\s*([0-9\.\+Ee\-]+)\s+(\w+)',
        r'solution\s+time\s*=\s*([0-9\.\+Ee\-]+)\s+(\w+)',
        r'[Tt]\s*=\s*([0-9\.\+Ee\-]+)',
        r'"([Tt]\s*=\s*[0-9\.\+Ee\-]+\s+\w+)"',
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            try:
                if len(m.groups()) >= 1:
                    time_value = float(m.group(1))
                    if len(m.groups()) > 1 and m.group(2):
                        header_unit = m.group(2).strip().lower()
                        unit_mapping = {
                            'second': 'seconds', 'sec': 'seconds', 's': 'seconds',
                            'minute': 'minutes', 'min': 'minutes', 'm': 'minutes',
                            'hour': 'hours', 'hr': 'hours', 'h': 'hours',
                            'day': 'days', 'd': 'days',
                            'week': 'weeks', 'wk': 'weeks', 'w': 'weeks',
                            'year': 'years', 'yr': 'years', 'y': 'years',
                        }
                        header_unit = unit_mapping.get(header_unit, header_unit)
                        return convert_time_to_seconds(time_value, header_unit)
                    else:
                        return convert_time_to_seconds(time_value, time_unit)
            except (ValueError, IndexError):
                continue
    return math.nan

def _extract_time_seconds_from_header(text: str, time_unit: str = "seconds") -> float:
    return _extract_time_from_header(text, time_unit)

GSP_FLS_ALIAS_RAW = {
    "fh_w": "freshwater_head", "p_w": "fluid_pressure", "ph_w": "pressure_head",
    "rho_w": "density", "s_a": "water_saturation", "theta_a": "water_content",
    "s_g": "air_saturation", "theta_g": "air_content", "s_n": "air_saturation",
    "theta_n": "air_content", "temperature": "temperature",
}


# ---------- Readers ----------
def read_mvc(mvc_path: Path, time_unit: str = "seconds") -> tuple[pd.DataFrame, Dict[str, str]]:
    text = mvc_path.read_text(encoding="utf-8", errors="ignore")
    varline = re.search(r"variables\s*=\s*(.+?)\n", text, flags=re.IGNORECASE)
    pretty_map: Dict[str, str] = {}
    norm_vars: List[str] = []
    
    if varline:
        vars_text = varline.group(1)
        quoted_vars = re.findall(r'"([^"]*)"', vars_text)
        raw_vars = [v.strip() for v in quoted_vars] if quoted_vars else [v.strip().strip('"').strip("'") for v in vars_text.split(",")]
        norm_vars = [normalize_name(v) for v in raw_vars]
        pretty_map = {n: rv for n, rv in zip(norm_vars, raw_vars)}
        
    lines = text.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        if re.search(r"\bzone\b.*f\s*=\s*point", line, flags=re.IGNORECASE):
            start_idx = i + 1; break
    if start_idx is None:
        for i, line in enumerate(lines):
            if re.match(r"^\s*[+\-0-9]", line):
                start_idx = i; break

    df = pd.read_csv(mvc_path, sep=r"\s+", header=None, engine="python", skiprows=start_idx if start_idx else 0)
    
    if norm_vars and len(norm_vars) <= df.shape[1]:
        df = df.iloc[:, :len(norm_vars)]
        df.columns = norm_vars
    else:
        cols = ["time"] + [f"col{i}" for i in range(2, df.shape[1] + 1)]
        df.columns = [normalize_name(c) for c in cols[: df.shape[1]]]

    time_col = next((c for c in df.columns if c == "time" or c.startswith("time_") or c.startswith("time")), None)
    if time_col:
        df.rename(columns={time_col: "time_s"}, inplace=True)
    else:
        df.rename(columns={df.columns[0]: "time_s"}, inplace=True)

    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")
        
    df = df.dropna(subset=["time_s"]).reset_index(drop=True)
    df["time_s"] = df["time_s"].apply(lambda x: convert_time_to_seconds(x, time_unit))
    df["time (days)"] = df["time_s"] / 86400.0
    return df, pretty_map

def read_mms(mms_path: Path, time_unit: str = "seconds") -> tuple[pd.DataFrame, Dict[str, str]]:
    text = mms_path.read_text(encoding="utf-8", errors="ignore")
    varline = re.search(r"variables\s*=\s*(.+?)\n", text, flags=re.IGNORECASE)
    pretty_map: Dict[str, str] = {}
    norm_vars: List[str] = []
    
    if varline:
        vars_text = varline.group(1)
        quoted_vars = re.findall(r'"([^"]*)"', vars_text)
        raw_vars = [v.strip() for v in quoted_vars] if quoted_vars else [v.strip().strip('"').strip("'") for v in vars_text.split(",")]
        norm_vars = [normalize_name(v) for v in raw_vars]
        pretty_map = {n: rv for n, rv in zip(norm_vars, raw_vars)}

    lines = text.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        if re.search(r"\bzone\b.*f\s*=\s*point", line, flags=re.IGNORECASE):
            start_idx = i + 1; break
    if start_idx is None:
        for i, line in enumerate(lines):
            if re.match(r"^\s*[+\-0-9]", line):
                start_idx = i; break

    df = pd.read_csv(mms_path, sep=r"\s+", header=None, engine="python", skiprows=start_idx if start_idx else 0)

    if norm_vars and len(norm_vars) <= df.shape[1]:
        df = df.iloc[:, :len(norm_vars)]
        df.columns = norm_vars
    else:
        cols = [f"col{i+1}" for i in range(df.shape[1])]
        df.columns = [normalize_name(c) for c in cols]

    for c in df.columns:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    time_col = next((c for c in df.columns if c == "time" or c.startswith("time_") or c.startswith("time")), None)
    
    if time_col:
        df.rename(columns={time_col: "time_s"}, inplace=True)
        df = df.dropna(subset=["time_s"]).reset_index(drop=True)
        df["time_s"] = df["time_s"].apply(lambda x: convert_time_to_seconds(x, time_unit))
    else:
        raise ValueError(f"'time' column not found in {mms_path.name}. Found columns: {list(df.columns)}")

    return df, pretty_map

def read_gsp(gsp_path: Path, time_unit: str = "seconds"):
    header = gsp_path.read_text(encoding="utf-8", errors="ignore")
    tsec = _extract_time_seconds_from_header(header, time_unit)
    if math.isnan(tsec) and "_0." in gsp_path.name: 
        tsec = 0.0
    
    varline = re.search(r"variables\s*=\s*(.+?)\n", header, flags=re.IGNORECASE)
    colmap = {}
    if varline:
        vars_text = varline.group(1)
        quoted_vars = re.findall(r'"([^"]*)"', vars_text)
        raw_vars = [v.strip() for v in quoted_vars] if quoted_vars else [v.strip().strip('"').strip("'") for v in vars_text.split(",")]
        norm_vars = [normalize_name(v) for v in raw_vars]
    else:
        raw_vars = []; norm_vars = []
        
    lines = header.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        if re.search(r"\bzone\b.*f\s*=\s*point", line, re.IGNORECASE):
            start_idx = i + 1; break
    if start_idx is None:
        for i, line in enumerate(lines):
            if re.match(r"^\s*[+\-0-9]", line):
                start_idx = i; break
                
    df = pd.read_csv(gsp_path, sep=r"\s+", header=None, comment="#", skiprows=start_idx if start_idx else 0, engine="python")
    if norm_vars and df.shape[1] >= len(norm_vars):
        df = df.iloc[:, -len(norm_vars):]
        df.columns = norm_vars
        colmap = {n: r for n, r in zip(norm_vars, raw_vars)}
    else:
        df.columns = [normalize_name(c) for c in df.columns]
        colmap = {c: c for c in df.columns}
    return df, tsec, colmap

def read_conc_file(path: Path, time_unit: str = "seconds"):
    header = path.read_text(encoding="utf-8", errors="ignore")
    tsec = _extract_time_from_header(header, time_unit)
    if math.isnan(tsec) and "_0." in path.name: 
        tsec = 0.0

    varline = re.search(r"variables\s*=\s*(.+?)\n", header, flags=re.IGNORECASE)
    raw_map = {}
    norm_vars = []
    
    if varline:
        vars_text = varline.group(1)
        quoted_vars = re.findall(r'"([^"]*)"', vars_text)
        raw_vars = [v.strip() for v in quoted_vars] if quoted_vars else [v.strip().strip('"').strip("'") for v in vars_text.split(",")]
        norm_vars = [normalize_name(v) for v in raw_vars]
        raw_map = {normalize_name(rv): rv for rv in raw_vars}
        
    lines = header.splitlines()
    start_idx = None
    for i, line in enumerate(lines):
        if re.search(r"\bzone\b.*f\s*=\s*point", line, re.IGNORECASE):
            start_idx = i + 1; break
    if start_idx is None:
        for i, line in enumerate(lines):
            if re.match(r"^\s*[+\-0-9]", line):
                start_idx = i; break
                
    df = pd.read_csv(path, sep=r"\s+", header=None, comment="#", skiprows=start_idx if start_idx else 0, engine="python")
    
    if norm_vars and df.shape[1] >= len(norm_vars):
        df = df.iloc[:, :len(norm_vars)]
        df.columns = norm_vars
    else:
        base = ["x", "y", "z"]
        df.columns = base + [f"sp_{i}" for i in range(1, df.shape[1] - 2)] if df.shape[1] >= 4 else [f"col{i+1}" for i in range(df.shape[1])]
        
    df.columns = [normalize_name(c) for c in df.columns]
    return df, tsec, raw_map

_STEP_RE = re.compile(r"_(\d+)\.(gsd|gss)$", re.IGNORECASE)
def extract_step_from_filename(fp: Path) -> float:
    m = _STEP_RE.search(fp.name)
    return float(m.group(1)) if m else math.nan


# ---------- Plot Processors ----------
def _plot_profile_over_files(records, plots_dir: Path, var_pretty: str, unit_text: str, suffix: str, axis_name: str = "z"):
    labels = []; seen = {}
    for tsec, step, fname, *_ in records:
        if not math.isnan(tsec): lab = f"{tsec / 86400.0:.2f} d"
        elif not math.isnan(step): lab = f"step {int(step)}"
        else: lab = fname
        seen[lab] = seen.get(lab, 0) + 1
        labels.append(lab if seen[lab] == 1 else f"{lab} ({seen[lab]})")

    plt.figure()
    for (tsec, step, fname, ax_vals, v), lbl in zip(records, labels):
        v = pd.to_numeric(v, errors="coerce")
        ax_vals = pd.to_numeric(ax_vals, errors="coerce")
        plt.plot(v.values, ax_vals.values, label=lbl)

    plt.xlabel(f"{var_pretty} ({unit_text})" if unit_text else var_pretty)
    plt.ylabel(f"{axis_name} (m)")
    plt.title(f"{var_pretty} profiles over outputs")
    plt.grid(True, linestyle=":", alpha=0.6)
    plt.legend(title="Output", loc="best")
    _apply_no_offset(plt.gca())
    plt.tight_layout()
    plt.savefig(plots_dir / f"{normalize_name(var_pretty)}_{suffix}.png", dpi=300)
    plt.close()

def process_gsd_gss_type(files: List[Path], outdir: Path, kind: str, do_plots: bool = True, time_unit: str = "seconds") -> Dict[str, pd.DataFrame]:
    if not files: return {}
    rec = []
    for fp in sorted(files):
        try: df, tsec, raw_map = read_conc_file(fp, time_unit)
        except: continue
        step = extract_step_from_filename(fp)
        rec.append((tsec, step, fp.name, df, raw_map))
    if not rec: return {}

    minerals = set()
    raw_by_norm = {}
    for _, _, _, df, raw_map in rec:
        for col in df.columns:
            if col in ("x", "y", "z") or col.startswith("time"): continue
            minerals.add(col)
            if col not in raw_by_norm: raw_by_norm[col] = raw_map.get(col, col)

    minerals = sorted(minerals)
    sheets: Dict[str, pd.DataFrame] = {}

    unit_text = "rate (model units)" if kind.lower() == "gsd" else "SI (-)"
    plot_suffix = "gsd_rate" if kind.lower() == "gsd" else "gss_si"

    primary_axis = get_primary_spatial_axis(rec[0][3])

    for m_norm in minerals:
        m_raw = raw_by_norm.get(m_norm, m_norm)
        rows, plot_series = [], []

        for (tsec, step, fname, df, _) in rec:
            if m_norm not in df.columns or primary_axis not in df.columns: continue
            ax_vals = pd.to_numeric(df[primary_axis], errors="coerce")
            v = pd.to_numeric(df[m_norm], errors="coerce") 

            rows.append(pd.DataFrame({
                "file": fname, "time_s": tsec, "time_days": (tsec / 86400.0) if not math.isnan(tsec) else math.nan,
                "step": step, f"{primary_axis} (m)": ax_vals.values, f"{m_raw} ({unit_text})": v.values,
            }))
            plot_series.append((tsec, step, fname, ax_vals, v))

        if rows:
            if do_plots: _plot_profile_over_files(plot_series, outdir, m_raw, unit_text, plot_suffix, primary_axis)
            sheets[f"{m_raw} ({unit_text})"] = pd.concat(rows, ignore_index=True)

    return sheets

def defaults_lookup(key: str) -> Tuple[str, str]:
    return default_labels_units().get(key, (key.replace("_", " "), ""))

def get_label_unit(var_norm: str, var_raw: str, fls_units: Dict[str, Tuple[str, str]]) -> Tuple[str, str, str]:
    for key in [GSP_FLS_ALIAS_RAW.get(var_raw), normalize_name(var_raw), GSP_FLS_ALIAS_RAW.get(var_norm), var_norm]:
        if key and key in fls_units: return fls_units[key][0], fls_units[key][1], key
    raw_norm = normalize_name(var_raw)
    pretty, unit = defaults_lookup(GSP_FLS_ALIAS_RAW.get(var_raw) or raw_norm or var_norm)
    return pretty, unit, GSP_FLS_ALIAS_RAW.get(var_raw) or raw_norm or var_norm

def write_excel(sheets: Dict[str, pd.DataFrame], path: Path):
    if not CREATE_EXCEL:
        return
    if not sheets: return
    with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
        used = set()
        for name, df in sheets.items():
            if df is None or df.empty: continue
            nm = safe_sheet_name(name); base = nm; i = 2
            while nm in used:
                nm = safe_sheet_name(f"{base}_{i}"); i += 1
            used.add(nm)
            df.to_excel(writer, sheet_name=nm, index=False)
    print(f"[INFO] Wrote Excel: {path}")

def disable_png_output_if_requested():
    """Prevent creation of PNG/PDF/SVG/etc. files when CREATE_PLOTS is False.

    Some legacy functions still build matplotlib figures internally. This guard makes
    sure that even if a plotting block is accidentally reached, plt.savefig() does
    not write an image file. Excel export is not affected.
    """
    if CREATE_PLOTS:
        return

    def _skip_savefig(*args, **kwargs):
        return None

    plt.savefig = _skip_savefig

def _apply_no_offset(ax):
    sf = ScalarFormatter(useMathText=True)
    sf.set_useOffset(False)
    sf.set_powerlimits((-3, 3))
    ax.xaxis.set_major_formatter(sf)

def make_time_series_plots_from_mvc(mvc_path: Path, outdir: Path, fls_units: Dict[str, Tuple[str, str]], time_unit: str = "seconds") -> pd.DataFrame:
    df, pretty_map = read_mvc(mvc_path, time_unit)
    wide = pd.DataFrame({"time_s": df["time_s"], "time (days)": df["time (days)"]})

    for var in [c for c in df.columns if c not in ("time_s", "time (days)")]:
        key = normalize_name(pretty_map.get(var, var))
        pretty, unit = fls_units.get(key, defaults_lookup(var))
        if pretty == var.replace("_", " "): pretty = pretty_map.get(var, pretty)
        colname = pretty + (f" ({unit})" if unit else "")
        wide[colname] = pd.to_numeric(df[var], errors="coerce")

        plt.figure()
        plt.plot(df["time (days)"], df[var])
        plt.xlabel("Time (days)")
        plt.ylabel(colname)
        plt.title(f"{pretty} vs Time (days)")
        plt.grid(True, linestyle=":", alpha=0.6)
        plt.tight_layout()
        plt.savefig(outdir / f"mvc_{normalize_name(pretty)}.png", dpi=300)
        plt.close()
    return wide

def make_mineral_plots_from_mms(mms_path: Path, outdir: Path, fls_units: Dict[str, Tuple[str, str]], time_unit: str = "seconds") -> pd.DataFrame:
    df, pretty_map = read_mms(mms_path, time_unit)
    wide = pd.DataFrame({"time_s": df["time_s"], "time_days": df["time_s"] / 86400.0})

    for var in [c for c in df.columns if c != "time_s"]:
        pretty = pretty_map.get(var, var)
        colname = f"{pretty} (mol)"
        wide[colname] = pd.to_numeric(df[var], errors="coerce")

        plt.figure()
        plt.plot(wide["time_days"], wide[colname])
        plt.xlabel("Time (days)")
        plt.ylabel(colname)
        plt.title(f"{pretty} vs Time")
        plt.grid(True, linestyle=":", alpha=0.6)
        plt.tight_layout()
        plt.savefig(outdir / f"mineral_{normalize_name(pretty)}.png", dpi=300)
        plt.close()
    return wide

def overlay_gsp_profiles_make_sheets(gsp_files: List[Path], plots_dir: Path, fls_units: Dict[str, Tuple[str, str]], time_unit: str = "seconds") -> Dict[str, pd.DataFrame]:
    if not gsp_files: return {}
    records = []
    for fp in sorted(gsp_files):
        try: df, tsec, colmap = read_gsp(fp, time_unit)
        except: continue
        records.append((tsec, fp.name, df, colmap))
    if not records: return {}

    union_vars = set(); raw_by_norm = {}
    for _, _, df, colmap in records:
        for n in df.columns:
            if n in ("x", "y", "z"): continue
            union_vars.add(n); raw_by_norm[n] = raw_by_norm.get(n, colmap.get(n, n))
            
    records.sort(key=lambda r: (float("inf") if math.isnan(r[0]) else r[0], r[1]))
    labels = []; seen = {}
    for tsec, fname, _, _ in records:
        lab = fname if math.isnan(tsec) else f"{tsec / 86400.0:.2f} d"
        seen[lab] = seen.get(lab, 0) + 1
        labels.append(lab if seen[lab] == 1 else f"{lab} ({seen[lab]})")

    primary_axis = get_primary_spatial_axis(records[0][2])
    param_sheets: Dict[str, pd.DataFrame] = {}
    
    for var_norm in sorted(list(union_vars)):
        var_raw = raw_by_norm.get(var_norm, var_norm)
        pretty, unit, _ = get_label_unit(var_norm, var_raw, fls_units)
        xlab = pretty + (f" ({unit})" if unit else "")

        plt.figure()
        for (tsec, fname, df, _), lbl in zip(records, labels):
            if var_norm not in df.columns or primary_axis not in df.columns: continue
            plt.plot(pd.to_numeric(df[var_norm], errors="coerce").values, pd.to_numeric(df[primary_axis], errors="coerce").values, label=lbl)
        plt.xlabel(xlab)
        plt.ylabel(f"{primary_axis} (m)")
        plt.title(f"{pretty} profiles over time")
        plt.grid(True, linestyle=":", alpha=0.6)
        plt.legend(title="Time", loc="best")
        plt.tight_layout()
        plt.savefig(plots_dir / f"profile_{normalize_name(pretty)}.png", dpi=300)
        plt.close()

        rows = []
        for (tsec, fname, df, _) in records:
            if var_norm not in df.columns or primary_axis not in df.columns: continue
            rows.append(pd.DataFrame({
                "file": fname, "time_s": tsec, "time_days": 0.0 if math.isnan(tsec) else tsec / 86400.0,
                f"{primary_axis} (m)": pd.to_numeric(df[primary_axis], errors="coerce").values,
                xlab: pd.to_numeric(df[var_norm], errors="coerce").values,
            }))
        if rows: param_sheets[xlab] = pd.concat(rows, ignore_index=True)
        
    return param_sheets

def _plot_conc(records, plots_dir: Path, species_pretty: str, unit_text: str, suffix: str, axis_name: str = "z"):
    labels = []; seen = {}
    for tsec, fname, *_ in records:
        lab = fname if math.isnan(tsec) else f"{tsec / 86400.0:.2f} d"
        seen[lab] = seen.get(lab, 0) + 1
        labels.append(lab if seen[lab] == 1 else f"{lab} ({seen[lab]})")

    plt.figure()
    for (tsec, fname, ax_vals, c), lbl in zip(records, labels):
        plt.plot(pd.to_numeric(c, errors="coerce").values, pd.to_numeric(ax_vals, errors="coerce").values, label=lbl)
    plt.xlabel(f"{species_pretty} ({unit_text})")
    plt.ylabel(f"{axis_name} (m)")
    plt.title(f"{species_pretty} profiles over time")
    plt.grid(True, linestyle=":", alpha=0.6)
    plt.legend(title="Time", loc="best")
    _apply_no_offset(plt.gca())
    plt.tight_layout()
    plt.savefig(plots_dir / f"{normalize_name(species_pretty)}_{suffix}.png", dpi=300)
    plt.close()

def process_concentration_type(conc_files: List[Path], outdir: Path, fls_units: Dict[str, Tuple[str, str]], do_plots: bool = True, transform_hplus_to_ph: bool = False, time_unit: str = "seconds") -> Dict[str, pd.DataFrame]:
    if not conc_files: return {}
    rec = []
    for fp in sorted(conc_files):
        try: df, tsec, raw_map = read_conc_file(fp, time_unit)
        except: continue
        rec.append((tsec, fp.name, df, raw_map))
    if not rec: return {}

    species = set(); raw_by_norm = {}
    for _, _, df, raw_map in rec:
        for col in df.columns:
            if col in ("x", "y", "z") or col.startswith("time"): continue
            species.add(col); raw_by_norm[col] = raw_by_norm.get(col, raw_map.get(col, col))

    primary_axis = get_primary_spatial_axis(rec[0][2])
    sheets: Dict[str, pd.DataFrame] = {}

    for sp_norm in sorted(species):
        sp_raw = raw_by_norm.get(sp_norm, sp_norm)
        key = normalize_name(sp_raw)
        is_hplus = transform_hplus_to_ph and (key == "h_1" or sp_raw.strip().lower() == "h+1")
        sp_pretty = "pH" if is_hplus else (fls_units.get(key, (sp_raw, "mol/L H2O"))[0])

        rows, plot_series = [], []
        for (tsec, fname, df, _) in rec:
            if sp_norm not in df.columns or primary_axis not in df.columns: continue
            ax_vals = pd.to_numeric(df[primary_axis], errors="coerce")
            c = pd.to_numeric(df[sp_norm], errors="coerce").clip(lower=0)
            tdays = 0.0 if math.isnan(tsec) else tsec / 86400.0

            if is_hplus:
                ph = -np.log10(c.clip(lower=1e-30))
                rows.append(pd.DataFrame({"file": fname, "time_s": tsec, "time_days": tdays, f"{primary_axis} (m)": ax_vals.values, "pH (-log10[H+])": ph.values}))
                plot_series.append((tsec, fname, ax_vals, ph))
            else:
                rows.append(pd.DataFrame({"file": fname, "time_s": tsec, "time_days": tdays, f"{primary_axis} (m)": ax_vals.values, f"{sp_pretty} (mol/L H2O)": c.values}))
                plot_series.append((tsec, fname, ax_vals, c))

        if not rows: continue
        base_df = pd.concat(rows, ignore_index=True)

        if do_plots:
            if is_hplus: _plot_conc(plot_series, outdir, "pH", "-log10[H+]", "pH", primary_axis)
            else:
                _plot_conc(plot_series, outdir, sp_pretty, "mol/L H2O", "molL", primary_axis)
                mw = MOLAR_MASS.get(sp_raw) or MOLAR_MASS.get(key)
                if mw:
                    _plot_conc([(t, f, z, c * mw * 1000.0) for t, f, z, c in plot_series], outdir, sp_pretty, "mg/L", "mgL", primary_axis)
                    _plot_conc([(t, f, z, c * mw * 1000000.0) for t, f, z, c in plot_series], outdir, sp_pretty, "ppb", "ppb", primary_axis)

        if is_hplus: sheets["pH (-log10[H+])"] = base_df
        else:
            sheets[f"{sp_pretty} (mol/L H2O)"] = base_df
            mw = MOLAR_MASS.get(sp_raw) or MOLAR_MASS.get(key)
            if mw:
                sheets[f"{sp_pretty} (mg/L)"] = base_df.assign(**{f"{sp_pretty} (mg/L)": base_df[f"{sp_pretty} (mol/L H2O)"] * mw * 1000.0}).drop(columns=[f"{sp_pretty} (mol/L H2O)"])
                sheets[f"{sp_pretty} (ppb)"] = base_df.assign(**{f"{sp_pretty} (ppb)": base_df[f"{sp_pretty} (mol/L H2O)"] * mw * 1000000.0}).drop(columns=[f"{sp_pretty} (mol/L H2O)"])

    return sheets

def process_gbt_type(gbt_files: List[Path], outdir: Path, fls_units: Dict[str, Tuple[str, str]], obs_outflow_path: Optional[Path] = None, do_plots: bool = True, transform_hplus_to_ph: bool = False, time_unit: str = "seconds") -> Dict[str, pd.DataFrame]:
    if not gbt_files: return {}
    rec = []
    
    obs_df = read_observed_data(obs_outflow_path)
    
    for fp in sorted(gbt_files):
        try:
            df, _, raw_map = read_conc_file(fp, time_unit)
            time_col = find_time_column(df)
            if time_col:
                if time_col != "time":
                    df.rename(columns={time_col: "time"}, inplace=True)
            else:
                df.rename(columns={df.columns[0]: "time"}, inplace=True)

            df = add_time_columns(df, source_col="time", time_unit=time_unit)
            
            rec.append((fp.name, df, raw_map))
                        
            
            
            
        except Exception as e:
            print(f"[WARN] Failed to read GBT file {fp.name}: {e}")
            continue

    if not rec: return {}
    
    species = set()
    raw_by_norm = {}
    for _, df, raw_map in rec:
        for col in df.columns:
            if col in ("time", "time_s", "time_days", "x", "y", "z"): continue
            species.add(col)
            if col not in raw_by_norm: raw_by_norm[col] = raw_map.get(col, col)
            
    sheets = {}
    for sp_norm in sorted(species):
        sp_raw = raw_by_norm.get(sp_norm, sp_norm)
        key = normalize_name(sp_raw)
        is_hplus = transform_hplus_to_ph and (key == "h_1" or sp_raw.strip().lower() == "h+1")
        sp_pretty = "pH" if is_hplus else fls_units.get(key, (sp_raw, "mol/L H2O"))[0]
        
        rows = []
        plot_series = []
        for fname, df, _ in rec:
            if sp_norm not in df.columns: continue
            c = pd.to_numeric(df[sp_norm], errors="coerce").clip(lower=0)
            
            if is_hplus:
                ph = -np.log10(c.clip(lower=1e-30))
                rows.append(pd.DataFrame({"file": fname, "time_s": df["time_s"], "time_days": df["time_days"], "pH (-log10[H+])": ph.values}))
                plot_series.append((fname, df["time_days"], ph))
            else:
                rows.append(pd.DataFrame({"file": fname, "time_s": df["time_s"], "time_days": df["time_days"], f"{sp_pretty} (mol/L H2O)": c.values}))
                plot_series.append((fname, df["time_days"], c))
                
        if not rows: continue
        base_df = pd.concat(rows, ignore_index=True)
        
        if is_hplus: sheets["pH (-log10[H+])"] = base_df
        else:
            sheets[f"{sp_pretty} (mol/L H2O)"] = base_df
            mw = MOLAR_MASS.get(sp_raw) or MOLAR_MASS.get(key)
            if mw:
                sheets[f"{sp_pretty} (mg/L)"] = base_df.assign(**{f"{sp_pretty} (mg/L)": base_df[f"{sp_pretty} (mol/L H2O)"] * mw * 1000.0}).drop(columns=[f"{sp_pretty} (mol/L H2O)"])
                sheets[f"{sp_pretty} (ppb)"] = base_df.assign(**{f"{sp_pretty} (ppb)": base_df[f"{sp_pretty} (mol/L H2O)"] * mw * 1000000.0}).drop(columns=[f"{sp_pretty} (mol/L H2O)"])
                
        if do_plots:
            obs_mol, obs_mg = None, None
            if obs_df is not None and not is_hplus:
                if sp_raw in obs_df.columns:
                    obs_mol = obs_df[["time_days", sp_raw]].dropna()
                if f"{sp_pretty} (mg/L)" in obs_df.columns:
                    obs_mg = obs_df[["time_days", f"{sp_pretty} (mg/L)"]].dropna()
                    
            plt.figure()
            for fname, t_days, vals in plot_series:
                plt.plot(t_days, vals, label=fname)
            
            if obs_mol is not None and not obs_mol.empty:
                plt.scatter(obs_mol["time_days"], obs_mol.iloc[:, 1], marker='o', facecolors='none', edgecolors='r', label="Observed", s=50, zorder=5)

            unit_lbl = "-log10[H+]" if is_hplus else "mol/L H2O"
            plt.xlabel("Time (days)")
            plt.ylabel(f"{sp_pretty} ({unit_lbl})")
            plt.title(f"{sp_pretty} breakthrough over time")
            plt.grid(True, linestyle=":", alpha=0.6)
            plt.legend(title="Observation Point", loc="best")
            _apply_no_offset(plt.gca())
            plt.tight_layout()
            plt.savefig(outdir / f"{normalize_name(sp_pretty)}_gbt_{'pH' if is_hplus else 'molL'}.png", dpi=300)
            plt.close()
            
            if not is_hplus and mw and PLOT_MG_L:
                plt.figure()
                for fname, t_days, vals in plot_series:
                    plt.plot(t_days, vals * mw * 1000.0, label=fname)
                    
                if obs_mg is not None and not obs_mg.empty:
                    plt.scatter(obs_mg["time_days"], obs_mg.iloc[:, 1], marker='o', facecolors='none', edgecolors='r', label="Observed", s=50, zorder=5)
                elif obs_mol is not None and not obs_mol.empty:
                    plt.scatter(obs_mol["time_days"], obs_mol.iloc[:, 1] * mw * 1000.0, marker='o', facecolors='none', edgecolors='r', label="Observed (converted from mol/L)", s=50, zorder=5)

                plt.xlabel("Time (days)")
                plt.ylabel(f"{sp_pretty} (mg/L)")
                plt.title(f"{sp_pretty} breakthrough over time")
                plt.grid(True, linestyle=":", alpha=0.6)
                plt.legend(title="Observation Point", loc="best")
                _apply_no_offset(plt.gca())
                plt.tight_layout()
                plt.savefig(outdir / f"{normalize_name(sp_pretty)}_gbt_mgL.png", dpi=300)
                plt.close()
                
            if not is_hplus and mw and PLOT_PPB:
                plt.figure()
                for fname, t_days, vals in plot_series:
                    plt.plot(t_days, vals * mw * 1000000.0, label=fname)
                plt.xlabel("Time (days)")
                plt.ylabel(f"{sp_pretty} (ppb)")
                plt.title(f"{sp_pretty} breakthrough over time")
                plt.grid(True, linestyle=":", alpha=0.6)
                plt.legend(title="Observation Point", loc="best")
                _apply_no_offset(plt.gca())
                plt.tight_layout()
                plt.savefig(outdir / f"{normalize_name(sp_pretty)}_gbt_ppb.png", dpi=300)
                plt.close()
                
    return sheets

def process_gbm_type(gbm_files: List[Path], outdir: Path, fls_units: Dict[str, Tuple[str, str]], obs_outflow_path: Optional[Path] = None, do_plots: bool = True, time_unit: str = "seconds") -> Dict[str, pd.DataFrame]:
    """ Dedicated function for Master Variables (.gbm) breakthrough curves to avoid invalid molar mass multiplications """
    if not gbm_files: return {}
    rec = []
    obs_df = read_observed_data(obs_outflow_path)
    
    for fp in sorted(gbm_files):
        try:
            df, _, raw_map = read_conc_file(fp, time_unit)
            time_col = find_time_column(df)
            if time_col:
                if time_col != "time":
                    df.rename(columns={time_col: "time"}, inplace=True)
            else:
                df.rename(columns={df.columns[0]: "time"}, inplace=True)

            df = add_time_columns(df, source_col="time", time_unit=time_unit)
            
            rec.append((fp.name, df, raw_map))
            
            
            
        except Exception as e:
            print(f"[WARN] Failed to read GBM file {fp.name}: {e}")
            continue

    if not rec: return {}
    
    variables = set()
    raw_by_norm = {}
    for _, df, raw_map in rec:
        for col in df.columns:
            if col in ("time", "time_s", "time_days", "x", "y", "z"): continue
            variables.add(col)
            if col not in raw_by_norm: raw_by_norm[col] = raw_map.get(col, col)
            
    sheets = {}
    for var_norm in sorted(variables):
        var_raw = raw_by_norm.get(var_norm, var_norm)
        
        rows = []
        plot_series = []
        for fname, df, _ in rec:
            if var_norm not in df.columns: continue
            v = pd.to_numeric(df[var_norm], errors="coerce")
            rows.append(pd.DataFrame({
                "file": fname, "time_s": df["time_s"], "time_days": df["time_days"], var_raw: v.values
            }))
            plot_series.append((fname, df["time_days"], v))
            
        if not rows: continue
        sheets[var_raw] = pd.concat(rows, ignore_index=True)
        
        if do_plots:
            obs_data = None
            if obs_df is not None:
                if any(term in var_raw.lower() for term in ['alk', 'alkalinity', 'alk_mg_l_caco_3']) and 'eq/l' not in var_raw.lower():
                    for col in obs_df.columns:
                        if 'alk' in str(col).lower():
                            obs_data = obs_df[['time_days', col]].dropna()
                            break
                else:
                    if var_raw in obs_df.columns: 
                        obs_data = obs_df[['time_days', var_raw]].dropna()
                    else:
                        for col in obs_df.columns:
                            if normalize_name(str(col)) == normalize_name(var_raw):
                                obs_data = obs_df[['time_days', col]].dropna(); break
                                
            plt.figure(figsize=(10, 6))
            for fname, t_days, vals in plot_series:
                plt.plot(t_days, vals, label=fname)
                
            if obs_data is not None and not obs_data.empty:
                plt.scatter(obs_data["time_days"], obs_data.iloc[:, 1], marker='o', facecolors='none', edgecolors='r', label="Observed", s=60, zorder=5)

            plt.xlabel("Time (days)")
            plt.ylabel(var_raw)
            plt.title(f"{var_raw} breakthrough over time")
            plt.grid(True, linestyle=":", alpha=0.6)
            plt.legend(title="Observation Point", loc="best")
            _apply_no_offset(plt.gca())
            plt.tight_layout()
            plt.savefig(outdir / f"{normalize_name(var_raw)}_gbm.png", dpi=300)
            plt.close()
            
    return sheets

def normalize_observed_column_name(col) -> str:
    """Normalize observed-data headers to MIN3P species names."""
    name = str(col).strip()
    name = name.replace("−", "-").replace("²", "2").replace("³", "3")
    low = name.lower().strip()
    aliases = {
        "parameter": "Parameter", "date": "date", "day": "day",
        "time_days": "day", "s": "s", "time_s": "time_s",
        "ph": "pH", "h+": "h+1", "h+1": "h+1",
        "so4": "so4-2", "so4-2": "so4-2", "sulfaatti (so4)": "so4-2",
        "ca": "ca+2", "ca2+": "ca+2", "ca+2": "ca+2",
        "mg": "mg+2", "mg2+": "mg+2", "mg+2": "mg+2",
        "ni": "ni+2", "ni2+": "ni+2", "ni+2": "ni+2",
        "fe2+": "fe+2", "fe+2": "fe+2", "rauta (fe), liukoinen": "fe+2",
        "al": "al+3", "al3+": "al+3", "al+3": "al+3",
        "cu": "cu+2", "cu2+": "cu+2", "cu+2": "cu+2", "kupari (cu), liukoinen": "cu+2",
        "cd": "cd+2", "cd2+": "cd+2", "cd+2": "cd+2",
        "k": "k+1", "k+": "k+1", "k+1": "k+1",
        "h4sio4": "h4sio4",
        "na": "na+1", "na+": "na+1", "na+1": "na+1",
        "pb": "pb+2", "pb2+": "pb+2", "pb+2": "pb+2", "lyijy (pb), liukoinen": "pb+2",
        "zn": "zn+2", "zn2+": "zn+2", "zn+2": "zn+2", "sinkki (zn), liukoinen": "zn+2",
        "fe3+": "fe+3", "fe+3": "fe+3",
    }
    return aliases.get(low, name)


def read_observed_data(obs_outflow_path: Optional[Path]) -> Optional[pd.DataFrame]:
    """Read observed drainage-water data with a single header row.

    Old versions used header=1 because the observation file had two header rows.
    The updated file has only one header row, so use header=0.
    """
    if not obs_outflow_path or not obs_outflow_path.exists():
        return None
    try:
        if obs_outflow_path.suffix.lower() in [".xlsx", ".xls"]:
            obs_df = pd.read_excel(obs_outflow_path, header=0)
        else:
            obs_df = pd.read_csv(obs_outflow_path, header=0)

        obs_df.columns = [normalize_observed_column_name(col) for col in obs_df.columns]

        if "Parameter" in obs_df.columns:
            obs_df = obs_df[obs_df["Parameter"].astype(str).str.lower().str.strip() != "parameter"].copy()

        if 's' in obs_df.columns:
            obs_df['time_s'] = pd.to_numeric(obs_df['s'], errors='coerce')
            obs_df['time_days'] = obs_df['time_s'] / 86400.0
        elif 'day' in obs_df.columns:
            obs_df['time_days'] = pd.to_numeric(obs_df['day'], errors='coerce')
            obs_df['time_s'] = obs_df['time_days'] * 86400.0
        elif 'time_s' in obs_df.columns:
            obs_df['time_s'] = pd.to_numeric(obs_df['time_s'], errors='coerce')
            obs_df['time_days'] = obs_df['time_s'] / 86400.0
        elif 'time_days' in obs_df.columns:
            obs_df['time_days'] = pd.to_numeric(obs_df['time_days'], errors='coerce')
            obs_df['time_s'] = obs_df['time_days'] * 86400.0
        else:
            return None

        for col in obs_df.columns:
            if col not in ["Parameter", "date"]:
                obs_df[col] = pd.to_numeric(obs_df[col], errors="coerce")

        return obs_df.dropna(subset=['time_s', 'time_days'])
    except Exception:
        return None


def build_outflow_timeseries_from_conc(conc_files: List[Path], outflow_dir: Path, fls_units: Dict[str, Tuple[str, str]], obs_outflow_path: Optional[Path] = None, do_plots: bool = True, time_unit: str = "seconds") -> Dict[str, pd.DataFrame]:
    if not conc_files: return {}
    obs_df = read_observed_data(obs_outflow_path)
    
    rec = []
    for fp in sorted(conc_files):
        try: df, tsec, raw_map = read_conc_file(fp, time_unit)
        except: continue
        rec.append((tsec, fp.name, df, raw_map))
    if not rec: return {}
    rec.sort(key=lambda r: (float("inf") if math.isnan(r[0]) else r[0], r[1]))

    species = set(); raw_by_norm = {}
    for _, _, df, raw_map in rec:
        for col in df.columns:
            if col in ("x", "y", "z") or col.startswith("time"): continue
            species.add(col); raw_by_norm[col] = raw_by_norm.get(col, raw_map.get(col, col))

    if not species: return {}
    primary_axis = get_primary_spatial_axis(rec[0][2])
    data = {"time_s": [], "time_days": {}}; data.update({sp: [] for sp in sorted(species)})
    data = {"time_s": [], "time_days": []}
    for sp in sorted(species): data[sp] = []

    for tsec, fname, df, _ in rec:
        if math.isnan(tsec): tsec = 0.0 # Force 0.0 to prevent bad sorting at the bottom
        if primary_axis not in df.columns: continue
        df = df.copy()
        df[primary_axis] = pd.to_numeric(df[primary_axis], errors="coerce")
        target_val = df[primary_axis].min() if primary_axis == "z" else df[primary_axis].max()
        if pd.isna(target_val): continue
        dfz = df[df[primary_axis] == target_val]

        data["time_s"].append(tsec); data["time_days"].append(tsec / 86400.0)
        for sp in sorted(species):
            data[sp].append(float(pd.to_numeric(dfz[sp], errors="coerce").clip(lower=0).mean()) if sp in dfz.columns and not dfz.empty else math.nan)

    ts_model = pd.DataFrame(data); wide = ts_model.copy()

    for sp_norm in sorted(species):
        sp_raw = raw_by_norm.get(sp_norm, sp_norm)
        key = normalize_name(sp_raw)
        sp_pretty = fls_units[key][0] if key in fls_units else sp_raw

        mol_col = f"{sp_pretty} (mol/L)"
        wide[mol_col] = wide.pop(sp_norm)
        mw = MOLAR_MASS.get(sp_raw) or MOLAR_MASS.get(key)
        
        if mw:
            wide[f"{sp_pretty} (mg/L)"] = wide[mol_col] * mw * 1000.0
            wide[f"{sp_pretty} (ppb)"] = wide[mol_col] * mw * 1_000_000.0

        obs_mol, obs_mg = None, None
        if obs_df is not None:
            if sp_raw in obs_df.columns:
                obs_mol = obs_df[["time_days", sp_raw]].dropna().rename(columns={sp_raw: f"{sp_pretty} (mol/L) observed"})
            if f"{sp_pretty} (mg/L)" in obs_df.columns:
                obs_mg = obs_df[["time_days", f"{sp_pretty} (mg/L)"]].dropna().rename(columns={f"{sp_pretty} (mg/L)": f"{sp_pretty} (mg/L) observed"})

        if do_plots and mw and PLOT_MG_L:
            plt.figure()
            plt.plot(wide["time_days"], wide[f"{sp_pretty} (mg/L)"], 'b-', label="Modelled")
            if obs_mg is not None and not obs_mg.empty:
                plt.scatter(obs_mg["time_days"], obs_mg.iloc[:, 1], marker='o', facecolors='none', edgecolors='r', label="Observed", s=50, linewidths=1.5)
            elif obs_mol is not None and not obs_mol.empty:
                plt.scatter(obs_mol["time_days"], obs_mol.iloc[:, 1] * mw * 1000.0, marker='o', facecolors='none', edgecolors='r', label="Observed (converted from mol/L)", s=50, linewidths=1.5)
            
            plt.xlabel("Time (days)")
            plt.ylabel(f"{sp_pretty} (mg/L)")
            plt.title(f"{sp_pretty} at model outflow ({primary_axis}={target_val})")
            plt.grid(True, linestyle=":", alpha=0.6); plt.legend(loc="best"); plt.tight_layout()
            plt.savefig(outflow_dir / f"outflow_{normalize_name(sp_pretty)}_mgL.png", dpi=300); plt.close()

    return {"Outflow concentrations": wide}

def build_outflow_timeseries_from_gsm(gsm_files: List[Path], outflow_dir: Path, fls_units: Dict[str, Tuple[str, str]], obs_outflow_path: Optional[Path] = None, time_unit: str = "seconds") -> Dict[str, pd.DataFrame]:
    if not gsm_files: return {}
    obs_df = read_observed_data(obs_outflow_path)
    
    rec = []
    for fp in sorted(gsm_files):
        try: df, tsec, colmap = read_gsp(fp, time_unit)
        except: continue
        rec.append((tsec, fp.name, df, colmap))
    if not rec: return {}
    rec.sort(key=lambda r: (float("inf") if math.isnan(r[0]) else r[0], r[1]))

    vars_set = set(); raw_by_norm = {}
    for _, _, df, colmap in rec:
        for col in df.columns:
            if col in ("x", "y", "z") or col.startswith("time"): continue
            vars_set.add(col); raw_by_norm[col] = raw_by_norm.get(col, colmap.get(col, col))
    
    if not vars_set: return {}
    primary_axis = get_primary_spatial_axis(rec[0][2])

    data = {"time_s": [], "time_days": []}
    for v in sorted(vars_set): data[v] = []

    for tsec, fname, df, _ in rec:
        if math.isnan(tsec): tsec = 0.0 # Prevent NaN from pushing time=0 row to the very bottom
        if primary_axis not in df.columns: continue
        df = df.copy()
        df[primary_axis] = pd.to_numeric(df[primary_axis], errors="coerce")
        target_val = df[primary_axis].min() if primary_axis == "z" else df[primary_axis].max()
        if pd.isna(target_val): continue

        dfz = df[df[primary_axis] == target_val]
        data["time_s"].append(tsec); data["time_days"].append(tsec / 86400.0)

        for v in sorted(vars_set):
            data[v].append(float(pd.to_numeric(dfz[v], errors="coerce").mean()) if v in dfz.columns and not dfz.empty else math.nan)

    if not data["time_s"]: return {}
    ts_model = pd.DataFrame(data)

    all_time_s = sorted(set(ts_model["time_s"].tolist()) | (set(obs_df["time_s"].tolist()) if obs_df is not None else set()))
    wide = pd.DataFrame({"time_s": all_time_s}); wide["time_days"] = wide["time_s"] / 86400.0

    for var_norm in sorted(vars_set):
        var_raw = raw_by_norm.get(var_norm, var_norm)
        pretty, unit = fls_units.get(normalize_name(var_raw), (var_raw, ""))
        model_col = pretty + (f" ({unit})" if unit else "")

        s_df = pd.merge(wide[["time_s"]], ts_model[["time_s", var_norm]], on="time_s", how="left").rename(columns={var_norm: model_col})
        wide[model_col] = pd.to_numeric(s_df[model_col], errors="coerce")
        
        if 'alk_eq_l' in var_raw.lower() or 'alk_eq_l' in pretty.lower(): continue
        
        obs_data = None
        if obs_df is not None:
            if any(term in var_raw.lower() for term in ['alk', 'alkalinity', 'alk_mg_l_caco_3']) and 'eq/l' not in var_raw.lower():
                for col in obs_df.columns:
                    if 'alk' in str(col).lower():
                        obs_data = obs_df[['time_days', col]].dropna().rename(columns={col: "Alkalinity observed"})
                        break
            else:
                if var_raw in obs_df.columns: obs_data = obs_df[['time_days', var_raw]].dropna().rename(columns={var_raw: f"{pretty} observed"})
                else:
                    for col in obs_df.columns:
                        if normalize_name(str(col)) == normalize_name(var_raw):
                            obs_data = obs_df[['time_days', col]].dropna().rename(columns={col: f"{pretty} observed"}); break

        plt.figure(figsize=(10, 6))
        model_nonan = wide.dropna(subset=[model_col])
        if not model_nonan.empty: plt.plot(model_nonan["time_days"], model_nonan[model_col], 'b-', label="Modelled", linewidth=2)
        if obs_data is not None and not obs_data.empty:
            plt.scatter(obs_data["time_days"], obs_data.iloc[:, 1], marker='o', facecolors='none', edgecolors='r', label="Observed", s=80, linewidths=2, zorder=5)

        plt.xlabel("Time (days)", fontsize=12); plt.ylabel(model_col, fontsize=12)
        plt.title(f"{pretty} at model outflow ({primary_axis}={target_val})", fontsize=14)
        plt.grid(True, linestyle=":", alpha=0.6); _apply_no_offset(plt.gca()); plt.legend(loc="best", fontsize=10); plt.tight_layout()
        plt.savefig(outflow_dir / f"outflow_master_{normalize_name(pretty)}.png", dpi=300); plt.close()

    return {"Outflow master variables": wide}


# ---------- Main ----------
def main():
    workdir = get_script_workdir()
    print(f"[INFO] Working directory: {workdir}")
    disable_png_output_if_requested()
    if CREATE_PLOTS:
        print("[INFO] PNG plot creation: ON")
    else:
        print("[INFO] PNG plot creation: OFF (Excel files will still be created)")
    if CREATE_EXCEL:
        print("[INFO] Excel export: ON")
    else:
        print("[INFO] Excel export: OFF")

    fls_files = list(workdir.glob("*.fls"))
    fls_units = parse_fls_units(fls_files[0]) if fls_files else {}
    if TIME_UNIT_OVERRIDE.lower() != "auto":
        time_unit = canonical_time_unit(TIME_UNIT_OVERRIDE)
    else:
        time_unit = detect_time_unit(workdir, fls_units)
    print(f"[INFO] Detected time unit: {time_unit} (1 {time_unit} = {get_time_conversion_factor(time_unit)} seconds)")

    if EXECUTE_MIN3P:
        try:
            exe = find_min3p_exe(workdir)
            dat = find_dat_file(workdir)
            run_min3p(exe, dat, workdir, force_run=FORCE_RUN, show_window=SHOW_MIN3P_WINDOW)
        except Exception as e:
            print(f"[FATAL] MIN3P Execution failed: {e}")
            return
    else:
        print("[INFO] EXECUTE_MIN3P is False. Skipping model execution.")

    try: dat = find_dat_file(workdir)
    except Exception: dat = None

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_root = ensure_dir(workdir / f"Results_{ts}")
    if dat: shutil.copy2(dat, results_root / dat.name)
    #copy_and_open_latest_log(workdir, results_root)

    mvc_out = ensure_dir(results_root / "mvc_plots")
    gsp_out = ensure_dir(results_root / "gsp_plots")
    conc_root = ensure_dir(results_root / "conc_plots")
    outflow_conc_dir = ensure_dir(conc_root / "outflow_concentration")
    
    obs_outflow_path = workdir / OBS_OUTFLOW_FILENAME

    mvc_files = list(workdir.glob("*_o.mvc"))
    if mvc_files:
        print(f"[INFO] Plotting MVC time series -> {mvc_out}")
        write_excel({"MVC time series": make_time_series_plots_from_mvc(mvc_files[0], mvc_out, fls_units, time_unit)}, mvc_out / "mvc_plots.xlsx")

    gsp_files = sorted(workdir.glob("*.gsp"))
    if gsp_files:
        print(f"[INFO] Plotting GSP profiles -> {gsp_out}")
        gsp_sheets = overlay_gsp_profiles_make_sheets(gsp_files, gsp_out, fls_units, time_unit)
        if gsp_sheets: write_excel(gsp_sheets, gsp_out / "gsp_plots.xlsx")

    gsm_files = sorted(workdir.glob("*.gsm"))
    if gsm_files:
        print(f"[INFO] Plotting GSM outflow time series -> {outflow_conc_dir}")
        gsm_outflow_sheets = build_outflow_timeseries_from_gsm(gsm_files, outflow_conc_dir, fls_units, obs_outflow_path, time_unit)
        if gsm_outflow_sheets: write_excel(gsm_outflow_sheets, outflow_conc_dir / "outflow_master_gsm.xlsx")

    for f_type, transform_ph in [("gst", True), ("gsc", True)]:
        files = sorted(workdir.glob(f"*.{f_type}"))
        if not files: continue
        d_out = ensure_dir(conc_root / f_type)
        print(f"[INFO] Processing {f_type.upper()} vertical profiles -> {d_out}")
        sheets = process_concentration_type(files, d_out, fls_units, do_plots=CREATE_PLOTS, transform_hplus_to_ph=transform_ph, time_unit=time_unit)
        if sheets: write_excel(sheets, d_out / f"concentration_plots_{f_type}.xlsx")

        if f_type in ("gst", "gsc"):
            out_sheets = build_outflow_timeseries_from_conc(files, outflow_conc_dir, fls_units, obs_outflow_path, do_plots=CREATE_PLOTS and (f_type=="gst"), time_unit=time_unit)
            if out_sheets: write_excel(out_sheets, outflow_conc_dir / f"outflow_concentration_{f_type}.xlsx")
            
    # Process GBT files explicitly as time-series breakthrough curves
    gbt_files = sorted(workdir.glob("*.gbt"))
    if gbt_files:
        d_out = ensure_dir(conc_root / "gbt")
        print(f"[INFO] Processing GBT breakthrough curves -> {d_out}")
        sheets = process_gbt_type(gbt_files, d_out, fls_units, obs_outflow_path, do_plots=CREATE_PLOTS, transform_hplus_to_ph=False, time_unit=time_unit)
        if sheets: write_excel(sheets, d_out / "concentration_plots_gbt.xlsx")

    # Process GBM files explicitly as time-series master variables
    gbm_files = sorted(workdir.glob("*.gbm"))
    if gbm_files:
        d_out = ensure_dir(conc_root / "gbm")
        print(f"[INFO] Processing GBM master variables -> {d_out}")
        sheets = process_gbm_type(gbm_files, d_out, fls_units, obs_outflow_path, do_plots=CREATE_PLOTS, time_unit=time_unit)
        if sheets: write_excel(sheets, d_out / "master_variables_gbm.xlsx")

    for kind in ["gsd", "gss"]:
        files = sorted(workdir.glob(f"*.{kind}"))
        if not files: continue
        d_out = ensure_dir(conc_root / kind)
        print(f"[INFO] Processing {kind.upper()} files -> {d_out}")
        sheets = process_gsd_gss_type(files, d_out, kind=kind, do_plots=CREATE_PLOTS, time_unit=time_unit)
        if sheets: write_excel(sheets, d_out / f"{kind}_plots.xlsx")

    mms_files = sorted(workdir.glob("*.mms"))
    if mms_files:
        minerals_out = ensure_dir(results_root / "minerals")
        print(f"[INFO] Plotting mineral masses -> {minerals_out}")
        write_excel({"Mineral system mass": make_mineral_plots_from_mms(mms_files[0], minerals_out, fls_units, time_unit)}, minerals_out / "minerals_mms_timeseries.xlsx")

    print("[DONE]")

if __name__ == "__main__":
    main()