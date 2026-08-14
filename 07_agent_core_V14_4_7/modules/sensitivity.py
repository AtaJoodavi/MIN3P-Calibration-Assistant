from __future__ import annotations

import shutil
from typing import Any, Dict, List, Tuple

import pandas as pd

from .config import ProjectPaths
from .config_reader import ConfigReader
from .io_utils import log, safe_float, timestamp


class SensitivityAnalyzer:
    def __init__(
        self,
        paths: ProjectPaths,
        config: ConfigReader,
        run_single_cycle_callable,
    ):
        self.paths = paths
        self.config = config
        self.run_single_cycle = run_single_cycle_callable

    def _metric_dict_from_latest_history(self) -> Dict[str, Any]:
        if not self.paths.history_file.exists():
            return {}

        df = pd.read_excel(self.paths.history_file)

        if df.empty:
            return {}

        row = df.iloc[-1]
        out: Dict[str, Any] = {}

        keep_keys = [
            "run_status",
            "error_type",
            "run_health_score",
            "run_folder",
            "results_folder",
        ]

        for k, v in row.items():
            if str(k).startswith(
                (
                    "RMSE_",
                    "MAE_",
                    "Bias_",
                    "Mean_obs_",
                    "Mean_model_",
                )
            ) or k in keep_keys:
                out[k] = v

        return out

    def _test_values(
        self,
        row: pd.Series,
        default_multipliers: List[float],
    ) -> List[Tuple[float, str, bool]]:
        """
        Build sensitivity test values for one parameter.

        Supported modes in agent_config.xlsx / parameters sheet:

        sensitivity_mode = minmax
            Test parameter at min and max.

        sensitivity_mode = multiplier
            Test parameter using either:
            - sensitivity_multiplier column, e.g. 0.2 gives 0.8 and 1.2
            - command-line multipliers, e.g. --sensitivity-multipliers 0.8,1.2

        Returns:
            list of tuples:
            (test_value, sensitivity_case, clipped_to_bounds)
        """

        base = safe_float(row.get("value"))

        if base is None:
            return []

        mn = safe_float(row.get("min"))
        mx = safe_float(row.get("max"))

        mode = str(row.get("sensitivity_mode", "multiplier")).strip().lower()

        tests: List[Tuple[float, str, bool]] = []

        # --------------------------------------------------
        # Mode 1: test using explicit min and max bounds
        # --------------------------------------------------
        if mode == "minmax":
            if mn is not None and mn != base:
                tests.append((mn, "min", False))

            if mx is not None and mx != base:
                tests.append((mx, "max", False))

            return tests

        # --------------------------------------------------
        # Mode 2: test using multiplier around current value
        # --------------------------------------------------
        if mode == "multiplier":
            custom = safe_float(row.get("sensitivity_multiplier"))

            if custom is not None:
                multipliers = [1.0 - custom, 1.0 + custom]
            else:
                multipliers = default_multipliers

            for mult in multipliers:
                value = base * mult
                clipped = False

                if mn is not None and value < mn:
                    value = mn
                    clipped = True

                if mx is not None and value > mx:
                    value = mx
                    clipped = True

                if value != base:
                    tests.append((value, f"multiplier_{mult:g}", clipped))

            return tests

        # --------------------------------------------------
        # Fallback: use command-line multipliers
        # --------------------------------------------------
        for mult in default_multipliers:
            value = base * mult
            clipped = False

            if mn is not None and value < mn:
                value = mn
                clipped = True

            if mx is not None and value > mx:
                value = mx
                clipped = True

            if value != base:
                tests.append((value, f"multiplier_{mult:g}", clipped))

        return tests

    def run(
        self,
        multipliers: List[float] | None = None,
        max_parameters: int | None = None,
        include_baseline: bool = True,
    ) -> tuple[pd.DataFrame, pd.DataFrame]:

        log(
            self.paths,
            "V9 STEP SENS - Deterministic one-at-a-time sensitivity analysis",
        )

        multipliers = multipliers or [0.8, 1.2]

        backup = (
            self.paths.input_dir
            / f"agent_config_before_sensitivity_V9_0_{timestamp().replace(':', '').replace(' ', '_')}.xlsx"
        )

        shutil.copy2(self.paths.config_file, backup)

        original_sheets = self.config.all_sheets()
        params = self.config.active_parameters().copy()

        if max_parameters:
            params = params.head(max_parameters).copy()

        rows: List[Dict[str, Any]] = []

        try:
            if include_baseline:
                self.run_single_cycle(skip_gpt=True)

                rows.append(
                    {
                        "sensitivity_role": "baseline",
                        "parameter": "BASELINE",
                        "sensitivity_case": "baseline",
                        "multiplier": 1.0,
                        **self._metric_dict_from_latest_history(),
                    }
                )

            for _, prow in params.iterrows():
                parameter = str(prow.get("parameter", "")).strip()
                base = safe_float(prow.get("value"))

                if not parameter or base is None:
                    continue

                test_values = self._test_values(
                    row=prow,
                    default_multipliers=multipliers,
                )

                if not test_values:
                    log(
                        self.paths,
                        f"Sensitivity skipped for {parameter}: no valid test values",
                    )
                    continue

                for test, sensitivity_case, clipped in test_values:
                    self.config.write_all_sheets(original_sheets)

                    if test is None or test == base:
                        continue

                    relative_change = (test - base) / base if base else None

                    log(
                        self.paths,
                        (
                            f"Sensitivity: {parameter} "
                            f"{base:.6e} -> {test:.6e} "
                            f"case={sensitivity_case}"
                        ),
                    )

                    self.config.set_parameter_value(parameter, test)
                    self.run_single_cycle(skip_gpt=True)

                    rows.append(
                        {
                            "sensitivity_role": "perturbation",
                            "parameter": parameter,
                            "sensitivity_case": sensitivity_case,
                            "multiplier": sensitivity_case,
                            "base_value": base,
                            "test_value": test,
                            "relative_parameter_change": relative_change,
                            "clipped_to_bounds": clipped,
                            **self._metric_dict_from_latest_history(),
                        }
                    )

        finally:
            self.config.write_all_sheets(original_sheets)
            log(
                self.paths,
                f"Restored agent_config.xlsx after sensitivity; backup: {backup}",
            )

        results = pd.DataFrame(rows)
        results.to_excel(self.paths.sensitivity_results_file, index=False)

        coeff = self.analyze(results)

        return results, coeff

    def analyze(
        self,
        results: pd.DataFrame | None = None,
    ) -> pd.DataFrame:

        if results is None:
            if not self.paths.sensitivity_results_file.exists():
                return pd.DataFrame()

            results = pd.read_excel(self.paths.sensitivity_results_file)

        if results.empty or "sensitivity_role" not in results.columns:
            return pd.DataFrame()

        base_rows = results[
            results["sensitivity_role"].astype(str) == "baseline"
        ]

        if base_rows.empty:
            return pd.DataFrame()

        base = base_rows.iloc[-1]

        metric_cols = [
            c
            for c in results.columns
            if str(c).startswith(("RMSE_", "MAE_", "Bias_"))
        ]

        rows: List[Dict[str, Any]] = []

        perturbations = results[
            results["sensitivity_role"].astype(str) == "perturbation"
        ]

        for _, r in perturbations.iterrows():
            rel = safe_float(r.get("relative_parameter_change"))

            if rel in [None, 0]:
                continue

            for metric in metric_cols:
                b = safe_float(base.get(metric))
                t = safe_float(r.get(metric))

                if b is None or t is None:
                    continue

                delta = t - b

                norm = (delta / abs(b)) / rel if abs(b) > 1e-30 else None

                rows.append(
                    {
                        "parameter": r.get("parameter"),
                        "metric": metric,
                        "sensitivity_case": r.get("sensitivity_case"),
                        "multiplier": r.get("multiplier"),
                        "base_value": r.get("base_value"),
                        "test_value": r.get("test_value"),
                        "relative_parameter_change": rel,
                        "base_metric": b,
                        "test_metric": t,
                        "delta_metric": delta,
                        "normalized_sensitivity": norm,
                        "abs_normalized_sensitivity": (
                            abs(norm) if norm is not None else None
                        ),
                        "run_status": r.get("run_status"),
                    }
                )

        out = pd.DataFrame(rows)

        if not out.empty:
            out = out.sort_values(
                "abs_normalized_sensitivity",
                ascending=False,
            )

        out.to_excel(
            self.paths.sensitivity_coefficients_file,
            index=False,
        )

        self.build_influence_matrix(out)

        return out

    def build_influence_matrix(
        self,
        coeff: pd.DataFrame | None = None,
    ) -> pd.DataFrame:

        if coeff is None:
            if not self.paths.sensitivity_coefficients_file.exists():
                return pd.DataFrame()

            coeff = pd.read_excel(self.paths.sensitivity_coefficients_file)

        if coeff.empty:
            return pd.DataFrame()

        out = coeff.copy()

        out["species"] = (
            out["metric"]
            .astype(str)
            .str.replace(r"^(RMSE_|MAE_|Bias_)", "", regex=True)
        )

        out["direction"] = out["normalized_sensitivity"].apply(
            lambda x: (
                "positive"
                if safe_float(x) is not None and safe_float(x) > 0
                else "negative"
            )
        )

        out.to_excel(
            self.paths.parameter_influence_matrix_file,
            index=False,
        )

        return out