from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .config_reader import ConfigReader
from .io_utils import safe_float, safe_read_text


def find_min3p_text_file(run_dir: Path, suffix: str, config: ConfigReader) -> Optional[Path]:
    suffix = suffix.lower().lstrip(".")
    dat_stem = config.generated_dat_file().stem.lower()
    candidates = list(run_dir.glob(f"*.{suffix}")) + list(run_dir.rglob(f"*.{suffix}"))
    candidates = [p for p in candidates if p.name.lower() != "agent_run_log.txt"]
    if not candidates:
        return None
    exact = [p for p in candidates if dat_stem in p.stem.lower()]
    return max(exact or candidates, key=lambda p: p.stat().st_mtime)


def classify_failure_from_text(text: str, return_code: int | None = None) -> Tuple[str, str]:
    low = text.lower()
    patterns = [
        ("sorption", ["total sorbed mass", "exceeds total sites", "sorbed mass"]),
        ("convergence", ["maximum newton", "failed to converge", "no convergence", "newton iteration"]),
        ("speciation", ["aqueous speciation", "could not solve chemistry", "chemical equilibrium"]),
        ("transport", ["negative concentration", "negative saturation", "negative pressure"]),
        ("database", ["not found in database", "unknown species", "unknown mineral", "could not open"]),
        ("timestep", ["time step too small", "minimum time step", "failed time step"]),
        ("mass_balance", ["mass balance", "charge balance"]),
    ]
    for etype, keys in patterns:
        for key in keys:
            if key in low:
                line = next((ln.strip() for ln in text.splitlines() if key in ln.lower()), key)
                return etype, line[:500]
    if return_code not in (None, 0):
        return "python_or_postprocessing", f"plotsV46.py return code = {return_code}"
    if "simulation terminated" in low:
        return "simulation_terminated", "SIMULATION TERMINATED"
    return "unknown", "No known MIN3P failure pattern detected"


def _regex_number(label: str, text: str, integer: bool = False) -> Any:
    pat = re.escape(label) + r"\s*=\s*([-+]?\d*\.?\d+(?:[EeDd][-+]?\d+)?)"
    m = re.search(pat, text, flags=re.IGNORECASE)
    if not m:
        return None
    v = safe_float(m.group(1))
    return int(v) if integer and v is not None else v


def parse_log(log_file: Optional[Path], return_code: int | None = None) -> Dict[str, Any]:
    out = {"log_file": str(log_file) if log_file else "", "log_found": bool(log_file and log_file.exists())}
    if not log_file or not log_file.exists():
        out.update({"log_normal_exit": False, "log_error_type": "missing_log", "log_error_message": "MIN3P .log not found"})
        return out
    text = safe_read_text(log_file)
    low = text.lower()
    normal = "normal exit" in low
    terminated = "simulation terminated" in low
    out["log_normal_exit"] = normal
    out["log_simulation_terminated"] = terminated
    if normal and not terminated and return_code == 0:
        out.update({"log_error_type": "none", "log_error_message": ""})
    else:
        etype, emsg = classify_failure_from_text(text, return_code)
        out.update({"log_error_type": etype, "log_error_message": emsg})
    return out


def parse_gen(gen_file: Optional[Path]) -> Dict[str, Any]:
    out = {"gen_file": str(gen_file) if gen_file else "", "gen_found": bool(gen_file and gen_file.exists())}
    if not gen_file or not gen_file.exists():
        out.update({"gen_normal_exit": False})
        return out
    text = safe_read_text(gen_file, max_chars=250_000)
    out["gen_normal_exit"] = "normal exit" in text.lower()
    out["failed_time_steps"] = _regex_number("number of failed time steps", text, integer=True)
    ts = re.findall(r"total number of time steps\s*=\s*(\d+)", text, flags=re.IGNORECASE)
    out["total_time_steps"] = int(ts[-1]) if ts else None
    out["total_coupling_iterations"] = _regex_number("total number of coupling iterations", text, integer=True)
    cpu = re.search(r"cputime\s*=\s*([-+]?\d*\.?\d+(?:[EeDd][-+]?\d+)?)\s*sec", text, flags=re.IGNORECASE)
    out["cpu_time_sec"] = safe_float(cpu.group(1)) if cpu else None
    for key, label in {
        "porosity": "porosity",
        "Kzz": "saturated hydraulic conductivity K_zz",
        "aq_diff": "free diffusion coefficient in aqueous phase",
        "gas_diff": "free diffusion coefficient in gaseous phase",
        "dispersivity": "longitudinal dispersivity",
    }.items():
        out[key] = _regex_number(label, text)
    ph = re.findall(r"computed pH of solution:\s*pH\s*=\s*([-+]?\d*\.?\d+(?:[EeDd][-+]?\d+)?)", text, flags=re.IGNORECASE)
    cb = re.findall(r"charge balance error:\s*([-+]?\d*\.?\d+(?:[EeDd][-+]?\d+)?)\s*%", text, flags=re.IGNORECASE)
    out["initial_pH"] = safe_float(ph[0]) if ph else None
    out["initial_charge_balance_error_percent"] = safe_float(cb[0]) if cb else None
    vals = [safe_float(v) for v in cb]
    vals = [v for v in vals if v is not None]
    out["max_charge_balance_error_percent"] = max(vals) if vals else None
    return out


def compute_run_health(d: Dict[str, Any]) -> Tuple[float, str]:
    score = 0.0
    warnings = []
    if d.get("run_status") not in ["success", "success_with_retries", "partial_success"]:
        score += 1000
        warnings.append("run_not_successful")
    failed = safe_float(d.get("failed_time_steps"))
    total = safe_float(d.get("total_time_steps"))
    if failed and failed > 0:
        frac = failed / total if total else None
        d["failed_step_fraction"] = frac
        score += min(40.0, failed * 0.5)
        warnings.append("failed_time_steps")
        if frac is not None and frac > 0.25:
            score += 50.0
            warnings.append("high_failed_step_fraction")
    cb = safe_float(d.get("initial_charge_balance_error_percent"))
    if cb is not None and cb > 5:
        score += cb
        warnings.append("charge_balance_warning")
    coupling = safe_float(d.get("total_coupling_iterations"))
    if coupling is not None and total:
        d["coupling_iterations_per_step"] = coupling / total
    return score, "; ".join(warnings)


def analyze_run(run_dir: Path, results_dir: Path | None, return_code: int, config: ConfigReader) -> Dict[str, Any]:
    log_file = find_min3p_text_file(run_dir, "log", config)
    gen_file = find_min3p_text_file(run_dir, "gen", config)
    d: Dict[str, Any] = {"run_folder": str(run_dir), "results_folder": str(results_dir) if results_dir else "", "plots_return_code": return_code}
    d.update(parse_log(log_file, return_code))
    d.update(parse_gen(gen_file))
    normal = bool(d.get("log_normal_exit") or d.get("gen_normal_exit"))
    has_results = bool(results_dir and results_dir.exists())
    failed = safe_float(d.get("failed_time_steps"))
    if normal and return_code == 0 and has_results:
        d["run_status"] = "success_with_retries" if failed and failed > 0 else "success"
        d["error_type"] = "convergence_warning" if failed and failed > 0 else "none"
        d["error_message"] = f"MIN3P completed with {int(failed)} failed/retried timesteps" if failed and failed > 0 else ""
    else:
        d["run_status"] = "failed"
        d["error_type"] = d.get("log_error_type") or "no_normal_exit"
        d["error_message"] = d.get("log_error_message") or "No normal exit detected"
    health, warnings = compute_run_health(d)
    d["run_health_score"] = health
    d["warnings"] = warnings
    return d
