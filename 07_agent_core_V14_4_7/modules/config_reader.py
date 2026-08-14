from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pandas as pd

from .config import ProjectPaths
from .io_utils import fail_if_missing


class ConfigReader:
    def __init__(self, paths: ProjectPaths):
        self.paths = paths

    def sheet(self, name: str) -> pd.DataFrame:
        fail_if_missing(self.paths.config_file, "agent_config.xlsx")
        return pd.read_excel(self.paths.config_file, sheet_name=name)

    def all_sheets(self) -> Dict[str, pd.DataFrame]:
        fail_if_missing(self.paths.config_file, "agent_config.xlsx")
        xls = pd.ExcelFile(self.paths.config_file)
        return {s: pd.read_excel(self.paths.config_file, sheet_name=s) for s in xls.sheet_names}

    def write_all_sheets(self, sheets: Dict[str, pd.DataFrame]) -> None:
        with pd.ExcelWriter(self.paths.config_file, engine="openpyxl") as writer:
            for name, df in sheets.items():
                df.to_excel(writer, sheet_name=name, index=False)

    def parameters(self) -> pd.DataFrame:
        df = self.sheet("parameters")
        if "parameter" not in df.columns or "value" not in df.columns:
            raise ValueError("agent_config.xlsx / parameters sheet must contain 'parameter' and 'value'.")
        if "status" in df.columns:
            df["status"] = df["status"].astype(str).str.lower().str.strip()
        return df

    def active_parameters(self) -> pd.DataFrame:
        df = self.parameters()
        if "status" not in df.columns:
            return df.copy()
        return df[df["status"].isin(["active", "yes", "true", "1"])].copy()

    def species(self) -> pd.DataFrame:
        df = self.sheet("species")
        if "active" in df.columns:
            active = df["active"].astype(str).str.lower().str.strip()
            df = df[active.isin(["active", "yes", "true", "1"])]
        if "species" not in df.columns:
            raise ValueError("agent_config.xlsx / species sheet must contain 'species'.")
        return df.copy()

    def optimizer_v13(self) -> Dict[str, Any]:
        """Read optional optimizer_v13 sheet as a setting/value mapping."""
        try:
            df = self.sheet("optimizer_v13")
            if {"setting", "value"}.issubset(df.columns):
                return {
                    str(row["setting"]).strip(): row["value"]
                    for _, row in df.iterrows()
                    if str(row.get("setting", "")).strip()
                }
        except Exception:
            pass
        return {}


    def model_files(self) -> Dict[str, Any]:
        try:
            df = self.sheet("model_files")
            if "key" in df.columns and "value" in df.columns:
                return dict(zip(df["key"], df["value"]))
        except Exception:
            pass
        return {}

    def _resolve(self, value: Any, default_folder: Path) -> Path:
        p = Path(str(value).strip())
        return p if p.is_absolute() else default_folder / p

    def template_file(self) -> Path:
        return self._resolve(self.model_files().get("template_file", "Orijarvi_template.dat"), self.paths.input_dir)

    def generated_dat_file(self) -> Path:
        return self._resolve(self.model_files().get("input_file", "Orijarvi.dat"), self.paths.input_dir)

    def observed_file(self) -> Path:
        name = self.model_files().get("observed_file", "observed data for min3p.xlsx")
        candidates = [
            self._resolve(name, self.paths.input_dir),
            self.paths.input_dir / "observed_data.xlsx",
            self.paths.input_dir / "observed data for min3p.xlsx",
            self.paths.project_dir / str(name),
        ]
        for c in candidates:
            if c.exists():
                return c
        return candidates[0]

    def set_parameter_value(self, parameter: str, value: float) -> None:
        sheets = self.all_sheets()
        params = sheets["parameters"].copy()
        mask = params["parameter"].astype(str).str.strip() == parameter
        if not mask.any():
            raise ValueError(f"Parameter not found in agent_config.xlsx: {parameter}")
        params.loc[mask, "value"] = value
        sheets["parameters"] = params
        self.write_all_sheets(sheets)
