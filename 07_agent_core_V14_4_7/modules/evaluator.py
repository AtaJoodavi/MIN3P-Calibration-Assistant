from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from .config import ProjectPaths
from .config_reader import ConfigReader
from .io_utils import fail_if_missing, log


def rmse(obs: np.ndarray, sim: np.ndarray) -> float:
    return float(np.sqrt(np.nanmean((obs - sim) ** 2)))


def mae(obs: np.ndarray, sim: np.ndarray) -> float:
    return float(np.nanmean(np.abs(obs - sim)))


def bias(obs: np.ndarray, sim: np.ndarray) -> float:
    return float(np.nanmean(sim - obs))


def normalize_observed_column_name(col: Any) -> str:
    name = str(col).strip().replace("−", "-").replace("²", "2").replace("³", "3")
    low = name.lower().strip()
    aliases = {
        "ph": "pH", "h+": "h+1", "h+1": "h+1",
        "so4": "so4-2", "so4-2": "so4-2", "sulfate": "so4-2", "sulfaatti (so4)": "so4-2",
        "ca": "ca+2", "ca2+": "ca+2", "ca+2": "ca+2",
        "mg": "mg+2", "mg2+": "mg+2", "mg+2": "mg+2",
        "fe2+": "fe+2", "fe+2": "fe+2", "fe3+": "fe+3", "fe+3": "fe+3",
        "al": "al+3", "al3+": "al+3", "al+3": "al+3",
        "cu": "cu+2", "cu2+": "cu+2", "cu+2": "cu+2",
        "cd": "cd+2", "cd2+": "cd+2", "cd+2": "cd+2",
        "pb": "pb+2", "pb2+": "pb+2", "pb+2": "pb+2",
        "zn": "zn+2", "zn2+": "zn+2", "zn+2": "zn+2",
        "k": "k+1", "k+": "k+1", "k+1": "k+1",
        "na": "na+1", "na+": "na+1", "na+1": "na+1",
        "day": "day", "time_days": "day", "date": "date", "parameter": "Parameter",
    }
    return aliases.get(low, name)


class ResultEvaluator:
    def __init__(self, paths: ProjectPaths, config: ConfigReader):
        self.paths = paths
        self.config = config

    def read_observed(self) -> pd.DataFrame:
        obs_file = self.config.observed_file()
        fail_if_missing(obs_file, "Observed data file")
        obs = pd.read_excel(obs_file, header=0)
        obs.columns = [normalize_observed_column_name(c) for c in obs.columns]
        if "Parameter" in obs.columns:
            obs = obs[obs["Parameter"].astype(str).str.lower().str.strip() != "parameter"].copy()
        if "day" not in obs.columns:
            raise ValueError(f"Observed data file must contain day column. Columns: {list(obs.columns)}")
        obs["day"] = pd.to_numeric(obs["day"], errors="coerce")
        for c in obs.columns:
            if c not in ["Parameter", "date"]:
                obs[c] = pd.to_numeric(obs[c], errors="coerce")
        return obs.dropna(subset=["day"])

    def find_gbm_file(self, results_dir: Path) -> Path:
        c = list(results_dir.rglob("master_variables_gbm.xlsx"))
        if not c:
            raise FileNotFoundError("master_variables_gbm.xlsx not found")
        return c[0]

    def find_gbt_file(self, results_dir: Path) -> Path:
        c = list(results_dir.rglob("concentration_plots_gbt.xlsx"))
        if not c:
            raise FileNotFoundError("concentration_plots_gbt.xlsx not found")
        return c[0]

    def read_model_ph(self, gbm_file: Path) -> pd.DataFrame:
        df = pd.read_excel(gbm_file, sheet_name="pH")
        df["time_days"] = pd.to_numeric(df["time_days"], errors="coerce")
        df["pH"] = pd.to_numeric(df["pH"], errors="coerce")
        return df[["time_days", "pH"]].dropna()

    def read_model_species(self, gbt_file: Path, species: str) -> pd.DataFrame:
        sheet = f"{species} (mol_L H2O)"
        xls = pd.ExcelFile(gbt_file)
        if sheet not in xls.sheet_names:
            raise ValueError(f"Sheet not found: {sheet}")
        df = pd.read_excel(gbt_file, sheet_name=sheet)
        value_cols = [c for c in df.columns if species in str(c)]
        if not value_cols:
            raise ValueError(f"No value column found for {species}")
        value_col = value_cols[-1]
        df["time_days"] = pd.to_numeric(df["time_days"], errors="coerce")
        df[value_col] = pd.to_numeric(df[value_col], errors="coerce")
        return df[["time_days", value_col]].dropna().rename(columns={value_col: species})

    def evaluate_variable(self, name: str, obs_df: pd.DataFrame, model_df: pd.DataFrame, unit: str) -> Tuple[Dict[str, Any] | None, pd.DataFrame | None]:
        if name not in obs_df.columns:
            log(self.paths, f"Observed column not found: {name}")
            return None, None
        valid = obs_df[["day", name]].dropna()
        if valid.empty:
            return None, None
        model_df = model_df.sort_values("time_days")
        obs_days = valid["day"].values
        obs_values = valid[name].values
        sim_values = np.interp(obs_days, model_df["time_days"].values, model_df[name].values)
        residuals = sim_values - obs_values
        metrics = {
            "variable": name, "unit": unit, "n_points": len(valid),
            "RMSE": rmse(obs_values, sim_values), "MAE": mae(obs_values, sim_values),
            "Bias_model_minus_obs": bias(obs_values, sim_values),
            "mean_observed": float(np.nanmean(obs_values)),
            "mean_model_at_obs_time": float(np.nanmean(sim_values)),
        }
        ts = pd.DataFrame({
            "day": obs_days, "variable": name, "unit": unit,
            "observed": obs_values, "model_interpolated": sim_values,
            "residual_model_minus_obs": residuals, "absolute_error": np.abs(residuals),
        })
        return metrics, ts

    def compare(self, results_dir: Path) -> Tuple[Path, Path]:
        log(self.paths, "V9 STEP 3 - Compare model outputs with observations")
        gbm = self.find_gbm_file(results_dir)
        gbt = self.find_gbt_file(results_dir)
        obs = self.read_observed()
        active_species = self.config.species()["species"].astype(str).str.strip().tolist()
        rows: List[Dict[str, Any]] = []
        tables: List[pd.DataFrame] = []
        for sp in active_species:
            try:
                if sp.lower() == "ph":
                    r, ts = self.evaluate_variable("pH", obs, self.read_model_ph(gbm), "-")
                else:
                    r, ts = self.evaluate_variable(sp, obs, self.read_model_species(gbt, sp), "mol/L")
            except Exception as exc:
                log(self.paths, f"WARNING: could not evaluate {sp}: {exc}")
                continue
            if r is not None and ts is not None:
                rows.append(r)
                tables.append(ts)
        metrics = pd.DataFrame(rows)
        timeseries = pd.concat(tables, ignore_index=True) if tables else pd.DataFrame()
        metrics_file = results_dir / "model_vs_observed_metrics.xlsx"
        timeseries_file = results_dir / "model_vs_observed_timeseries.xlsx"
        metrics.to_excel(metrics_file, index=False)
        timeseries.to_excel(timeseries_file, index=False)
        return metrics_file, timeseries_file


def metrics_to_history_dict(results_dir: Path | None) -> Dict[str, Any]:
    if results_dir is None:
        return {}
    path = results_dir / "model_vs_observed_metrics.xlsx"
    if not path.exists():
        return {}
    df = pd.read_excel(path)
    out: Dict[str, Any] = {}
    for _, row in df.iterrows():
        var = str(row.get("variable", "")).strip()
        if not var:
            continue
        out[f"RMSE_{var}"] = row.get("RMSE")
        out[f"MAE_{var}"] = row.get("MAE")
        out[f"Bias_{var}"] = row.get("Bias_model_minus_obs")
        out[f"Mean_obs_{var}"] = row.get("mean_observed")
        out[f"Mean_model_{var}"] = row.get("mean_model_at_obs_time")
    return out
