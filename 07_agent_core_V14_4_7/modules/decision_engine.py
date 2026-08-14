from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

import pandas as pd

from .config import ProjectPaths
from .config_reader import ConfigReader
from .diagnostics import geochemical_diagnosis, relative_bias, runtime_state
from .io_utils import append_df_excel, append_row_excel, log, safe_float, timestamp


@dataclass
class Suggestion:
    parameter: str
    old_value: float
    new_value: float
    factor_requested: float
    factor_applied: float
    priority: int
    reason: str
    evidence: str


class DeterministicCalibrationEngine:
    """
    Deterministic calibration engine.

    V11 behavior:
    ----------------
    1. First tries to read 04_results/calibration_strategy_V11.xlsx, then falls back to calibration_strategy_V10.xlsx.
    2. If rows with next_cycle_change = yes exist, suggestions are generated
       from the V11/V10 strategy.
    3. If no V11/V10 strategy exists, or no valid V10 suggestion can be created,
       the engine falls back to the older V9.3 hydrogeochemical rules.

    This makes:
        suggest-only
        apply-suggestions
        auto

    follow the V11/V11/V10 strategy file whenever it is available.
    """

    def __init__(self, paths: ProjectPaths, config: ConfigReader):
        self.paths = paths
        self.config = config

    # =========================================================================
    # Common utilities
    # =========================================================================

    def _history(self) -> pd.DataFrame:
        if not self.paths.history_file.exists():
            return pd.DataFrame()
        return pd.read_excel(self.paths.history_file)

    def _latest_success(self) -> pd.Series | None:
        hist = self._history()

        if hist.empty:
            return None

        if "run_status" in hist.columns:
            good = hist[
                hist["run_status"]
                .astype(str)
                .str.lower()
                .isin(["success", "success_with_retries", "partial_success"])
            ]
            return good.iloc[-1] if not good.empty else hist.iloc[-1]

        return hist.iloc[-1]

    def _param_row(self, parameter: str) -> pd.Series | None:
        params = self.config.parameters()

        if "parameter" not in params.columns:
            return None

        sub = params[
            params["parameter"].astype(str).str.strip()
            == str(parameter).strip()
        ]

        return None if sub.empty else sub.iloc[0]

    def _parameter_change_ratio_from_history(self, parameter: str) -> float | None:
        """
        Returns current_value / initial_value using optimization_history.xlsx.

        This is used by the old fallback rules to detect when a parameter has
        already been pushed far from its original value.
        """
        hist = self._history()

        if hist.empty or parameter not in hist.columns:
            return None

        values = [
            safe_float(v)
            for v in hist[parameter].tolist()
            if safe_float(v) is not None
        ]

        if len(values) < 2:
            return None

        initial_value = values[0]
        current_value = values[-1]

        if initial_value in [None, 0]:
            return None

        return current_value / initial_value

    def _add(
        self,
        suggestions: List[Suggestion],
        parameter: str,
        factor: float,
        priority: int,
        reason: str,
        evidence: str,
        limits: Dict[str, float],
    ) -> None:
        r = self._param_row(parameter)

        if r is None:
            return

        status = str(r.get("status", "active")).lower().strip()

        if status not in ["active", "yes", "true", "1"]:
            return

        old = safe_float(r.get("value"))

        if old is None:
            return

        cf_min = safe_float(r.get("change_factor_min")) or 0.7
        cf_max = safe_float(r.get("change_factor_max")) or 1.3

        f = max(cf_min, min(cf_max, factor))

        if f > 1:
            f = min(f, limits.get("max_up", 1.12))
        else:
            f = max(f, limits.get("max_down", 0.88))

        new = old * f

        mn = safe_float(r.get("min"))
        mx = safe_float(r.get("max"))

        if mn is not None:
            new = max(mn, new)

        if mx is not None:
            new = min(mx, new)

        if new == old:
            return

        suggestions.append(
            Suggestion(
                parameter=parameter,
                old_value=old,
                new_value=new,
                factor_requested=factor,
                factor_applied=new / old if old else 1.0,
                priority=priority,
                reason=reason,
                evidence=evidence,
            )
        )

    def _trace_metal_allowed(
        self,
        pH_bias: float | None,
        so4_rel: float | None,
    ) -> bool:
        """
        Strict gate to avoid premature Zn/Pb/Cd/Cu optimization.
        """

        if pH_bias is not None and abs(pH_bias) > 0.20:
            log(
                self.paths,
                (
                    f"Trace-metal calibration blocked: "
                    f"abs(Bias_pH)={abs(pH_bias):.3g} > 0.20"
                ),
            )
            return False

        if so4_rel is not None and abs(so4_rel) > 0.20:
            log(
                self.paths,
                (
                    f"Trace-metal calibration blocked: "
                    f"abs(SO4_relative_bias)={abs(so4_rel):.3g} > 0.20"
                ),
            )
            return False

        return True

    # =========================================================================
    # V11/V10 strategy-driven suggestion layer
    # =========================================================================

    def _strategy_file(self):
        """Prefer the V11 automatically updated strategy; fall back to V10.9 baseline."""
        v11 = self.paths.results_dir / "calibration_strategy_V11.xlsx"
        if v11.exists():
            return v11
        return self.paths.results_dir / "calibration_strategy_V10.xlsx"

    def _read_v11_or_v10_strategy(self) -> pd.DataFrame:
        """
        Read V11/V10 strategy rows.

        V11 behavior inherited from V10.9:
        -------------
        Do not read only next_cycle_change = yes. Read all active strategy rows,
        then sort them with next_cycle_change=yes first and total_score second.

        Reason: after the selected parameter reaches a bound, e.g. top_flux at
        its minimum, the engine must be able to skip that saturated parameter
        and try the next ranked active strategy row instead of falling back to
        old V9 rules.
        """
        path = self._strategy_file()

        if not path.exists():
            return pd.DataFrame()

        try:
            df = pd.read_excel(path, sheet_name="calibration_strategy")
        except Exception as exc:
            log(self.paths, f"Could not read V11/V10 calibration strategy: {exc}")
            return pd.DataFrame()

        try:
            df.to_excel(
                self.paths.results_dir / "v11_strategy_raw_decision_debug.xlsx",
                index=False,
            )
        except Exception as exc:
            log(self.paths, f"Could not write V11 raw strategy debug file: {exc}")

        if df.empty:
            return pd.DataFrame()

        if "parameter" not in df.columns:
            log(
                self.paths,
                "calibration_strategy_V11/V10.xlsx found, but required column parameter was not found.",
            )
            return pd.DataFrame()

        df = df.copy()

        if "next_cycle_change" in df.columns:
            df["_next"] = (
                df["next_cycle_change"]
                .astype(str)
                .str.lower()
                .str.strip()
                .isin(["yes", "true", "1", "y"])
            )
        else:
            df["_next"] = False

        if "config_status" in df.columns:
            df = df[
                df["config_status"]
                .astype(str)
                .str.lower()
                .str.strip()
                .isin(["active", "yes", "true", "1"])
            ].copy()

        if "strategy_status" in df.columns:
            df = df[
                ~df["strategy_status"]
                .astype(str)
                .str.lower()
                .str.strip()
                .isin([
                    "inactive",
                    "frozen",
                    "do_not_touch",
                    "blocked",
                    "manual_review",
                    "temporarily_exhausted",
                    "boundary_limited",
                    "cooldown",
                    "watch",
                ])
            ].copy()

        if df.empty:
            return pd.DataFrame()

        if "total_score" in df.columns:
            df["total_score"] = pd.to_numeric(df["total_score"], errors="coerce").fillna(0.0)
        else:
            df["total_score"] = 0.0

        if "v11_auto_score" in df.columns:
            df["_sort_score"] = pd.to_numeric(df["v11_auto_score"], errors="coerce").fillna(df["total_score"])
        else:
            df["_sort_score"] = df["total_score"]

        # Keep explicit next-cycle rows first, but allow fall-through to the
        # next-ranked active rows when the explicit row is saturated at bounds.
        df = df.sort_values(["_next", "_sort_score"], ascending=[False, False])

        try:
            df.to_excel(
                self.paths.results_dir / "v11_strategy_candidate_decision_debug.xlsx",
                index=False,
            )
        except Exception as exc:
            log(self.paths, f"Could not write V11 candidate strategy debug file: {exc}")

        return df

    def _strategy_factor(
        self,
        parameter: str,
        strategy_row: pd.Series,
        latest_row: pd.Series | None,
    ) -> tuple[float, str]:
        """
        Convert V11/V10 strategy direction into a concrete factor.

        The strategy file often says "test_both_directions". Automatic mode can
        only apply one change at a time, so this function chooses a safe direction
        from the current geochemical bias.

        The step is intentionally conservative. Runtime controller limits and
        parameter min/max bounds are still applied by _add().
        """
        p = str(parameter).strip()
        p_lower = p.lower()

        direction = str(strategy_row.get("recommended_direction", "test_both_directions")).lower().strip()

        pH_bias = safe_float(latest_row.get("Bias_pH")) if latest_row is not None else None
        so4_rel = relative_bias(latest_row, "so4-2") if latest_row is not None else None

        # Default step.
        prow = self._param_row(p)
        step = 0.10

        if prow is not None:
            sm = safe_float(prow.get("sensitivity_multiplier"))
            if sm is not None and sm > 0:
                # In auto mode keep the step conservative even if sensitivity used 25%.
                step = min(sm, 0.10)

        up = 1.0 + step
        down = 1.0 - step

        # Explicit direction from strategy file.
        if direction in ["increase", "increase_value", "up", "positive"]:
            return up, f"V11/V10 strategy direction={direction}"

        if direction in ["decrease", "decrease_value", "down", "negative"]:
            return down, f"V11/V10 strategy direction={direction}"

        # Direction inference for common MIN3P calibration controls.
        if p_lower in ["top_flux", "bc_top_flux", "infiltration", "top_infiltration"]:
            if so4_rel is not None and so4_rel < -0.10:
                return down, (
                    "V11 strategy selected top_flux; SO4 is underpredicted, so reduce "
                    "top infiltration/flux to increase residence/concentration effect."
                )
            if so4_rel is not None and so4_rel > 0.10:
                return up, (
                    "V11 strategy selected top_flux; SO4 is overpredicted, so increase "
                    "top infiltration/flux to dilute/flush concentrations."
                )
            return down, "V11 strategy selected top_flux; default conservative direction is decrease."

        if "calcite" in p_lower or "dolomite" in p_lower or "magnesite" in p_lower:
            if pH_bias is not None and pH_bias < -0.20:
                return up, "pH is underpredicted; increase carbonate buffering."
            if pH_bias is not None and pH_bias > 0.20:
                return down, "pH is overpredicted; decrease carbonate buffering."
            return up, "carbonate buffering selected by V11/V10 strategy; default direction increase."

        if "pyrite" in p_lower or "pyrrhot" in p_lower:
            # pH and SO4 can conflict. In acidic models, avoid increasing acid generation
            # when pH is strongly underpredicted.
            if pH_bias is not None and pH_bias < -0.20:
                return down, (
                    "pH is underpredicted; reduce sulfide oxidation/acid-generation control."
                )
            if so4_rel is not None and so4_rel < -0.20:
                return up, "SO4 is underpredicted; increase sulfide oxidation/sulfate source."
            if so4_rel is not None and so4_rel > 0.20:
                return down, "SO4 is overpredicted; reduce sulfide oxidation/sulfate source."
            return up, "sulfide parameter selected by V11/V10 strategy; default direction increase."

        if "sphalerite" in p_lower or "galena" in p_lower or "chalcopyr" in p_lower:
            # Trace-metal changes should normally be selected only after pH/SO4 are acceptable.
            return up, "metal-source parameter selected by V11/V10 strategy; default direction increase."

        return up, "V11/V10 strategy direction=test_both_directions; default direction increase."

    def _suggest_from_v11_or_v10_strategy(
        self,
        max_changes: int,
        latest_row: pd.Series | None,
        runtime_limits: Dict[str, float],
    ) -> pd.DataFrame:
        strategy = self._read_v11_or_v10_strategy()

        if strategy.empty:
            return pd.DataFrame()

        log(
            self.paths,
            (
                "V11 STEP 5 - Strategy-driven parameter decision "
                f"from {self._strategy_file()}"
            ),
        )

        suggestions: List[Suggestion] = []

        for idx, (_, srow) in enumerate(strategy.iterrows(), start=1):
            parameter = str(srow.get("parameter", "")).strip()
            if not parameter:
                continue

            factor, direction_reason = self._strategy_factor(
                parameter=parameter,
                strategy_row=srow,
                latest_row=latest_row,
            )

            reason = (
                f"V11/V10 calibration_strategy selected this parameter/candidate. "
                f"{direction_reason}"
            )

            evidence = (
                f"total_score={srow.get('total_score', '')}; "
                f"sensitivity_score={srow.get('sensitivity_score', '')}; "
                f"importance_score={srow.get('importance_score', '')}; "
                f"priority={srow.get('priority', '')}; "
                f"process_group={srow.get('process_group', '')}"
            )

            before_count = len(suggestions)

            self._add(
                suggestions=suggestions,
                parameter=parameter,
                factor=factor,
                priority=idx,
                reason=reason,
                evidence=evidence,
                limits=runtime_limits,
            )

            if len(suggestions) == before_count:
                log(
                    self.paths,
                    (
                        f"V11/V10 strategy candidate {parameter} produced no valid bounded change "
                        f"(likely already at min/max or inactive). Trying next ranked candidate."
                    ),
                )

            if len(suggestions) >= max_changes:
                break

        if not suggestions:
            log(
                self.paths,
                "V11/V10 strategy was found, but no valid bounded suggestion could be created.",
            )
            return pd.DataFrame()

        out = pd.DataFrame(
            [
                {
                    **s.__dict__,
                    "timestamp": timestamp(),
                    "decision_source": "V11_calibration_strategy",
                    "strategy_file": str(self._strategy_file()),
                }
                for s in suggestions
            ]
        )

        # V11: this DataFrame is a candidate pool requested for V10.9 memory/fallback
        # filtering. Do not write it to parameter_suggestions_V11.xlsx, because that
        # file should contain only the selected/applied suggestion(s). Keep the full
        # candidate pool in a separate audit file.
        candidate_file = self.paths.results_dir / "candidate_suggestions_V11.xlsx"
        out = out.copy()
        out["candidate_pool_status"] = "candidate_before_v10_9_filters"
        append_df_excel(candidate_file, out)
        return out

    # =========================================================================
    # Public API
    # =========================================================================

    def suggest(self, max_changes: int = 2) -> pd.DataFrame:
        row = self._latest_success()

        if row is None:
            return pd.DataFrame()

        hist = self._history()
        rt = runtime_state(row, hist)

        append_row_excel(
            self.paths.deterministic_decision_file,
            {
                "timestamp": timestamp(),
                **rt,
            },
        )

        if rt.get("stop_auto"):
            log(self.paths, "Runtime controller stopped automatic suggestions.")
            return pd.DataFrame()

        if rt.get("force_single_change"):
            max_changes = min(max_changes, 1)

        limits = {
            "max_up": rt.get("max_up", 1.12),
            "max_down": rt.get("max_down", 0.88),
        }

        # First use V11/V10 strategy if it exists.
        strategy_path = self._strategy_file()

        if strategy_path.exists():
            strategy_out = self._suggest_from_v11_or_v10_strategy(
                max_changes=max_changes,
                latest_row=row,
                runtime_limits=limits,
            )

            if not strategy_out.empty:
                return strategy_out

            # Critical V10.5 safeguard:
            # If a V11/V11/V10 strategy file exists, do NOT silently fall back to V9.3.
            # Silent fallback was the reason auto could still change keff_calcite.
            log(
                self.paths,
                (
                    "V11/V11/V10 strategy file exists but no valid V10 suggestion was created. "
                    "Stopping suggestion generation instead of falling back to V9.3 rules."
                ),
            )
            return pd.DataFrame()

        # Fallback to V9.3 rules only if no V11/V11/V10 strategy file exists.
        return self._suggest_v9_rules(
            max_changes=max_changes,
            row=row,
            hist=hist,
            rt=rt,
            limits=limits,
        )

    # =========================================================================
    # V9.3 fallback rules
    # =========================================================================

    def _suggest_v9_rules(
        self,
        max_changes: int,
        row: pd.Series,
        hist: pd.DataFrame,
        rt: Dict[str, float],
        limits: Dict[str, float],
    ) -> pd.DataFrame:
        log(self.paths, "V9.3 STEP 5 - Deterministic parameter decision fallback")

        diagnosis = geochemical_diagnosis(row)

        suggestions: List[Suggestion] = []

        pH_bias = safe_float(row.get("Bias_pH"))
        so4_rel = relative_bias(row, "so4-2")

        zn_bias = safe_float(row.get("Bias_zn+2"))
        cu_bias = safe_float(row.get("Bias_cu+2"))
        pb_bias = safe_float(row.get("Bias_pb+2"))
        cd_bias = safe_float(row.get("Bias_cd+2"))
        mg_bias = safe_float(row.get("Bias_mg+2"))

        allow_trace_metals = self._trace_metal_allowed(
            pH_bias=pH_bias,
            so4_rel=so4_rel,
        )

        # --------------------------------------------------
        # Calcite saturation rule
        # --------------------------------------------------

        calcite_growth = self._parameter_change_ratio_from_history("keff_calcite")
        calcite_saturated = False

        if calcite_growth is not None and calcite_growth > 2.0:
            calcite_saturated = True
            log(
                self.paths,
                (
                    f"keff_calcite already increased {calcite_growth:.2f}x "
                    "from its initial value. Switching pH correction toward pyrite controls."
                ),
            )

        # --------------------------------------------------
        # Priority 1: pH correction
        # --------------------------------------------------

        if pH_bias is not None and pH_bias > 0.20:
            self._add(
                suggestions,
                "keff_calcite",
                0.90,
                1,
                "pH is overpredicted; reduce carbonate buffering.",
                f"Bias_pH={pH_bias}",
                limits,
            )

            self._add(
                suggestions,
                "phi_calcite",
                0.95,
                2,
                "pH is overpredicted; reduce calcite inventory slightly.",
                f"Bias_pH={pH_bias}",
                limits,
            )

            self._add(
                suggestions,
                "keff_pyrite",
                1.08,
                2,
                "pH is overpredicted; increase pyrite acid generation.",
                f"Bias_pH={pH_bias}",
                limits,
            )

            self._add(
                suggestions,
                "usr_pyrite",
                1.05,
                2,
                "pH is overpredicted; increase pyrite-related acid source.",
                f"Bias_pH={pH_bias}",
                limits,
            )

        elif pH_bias is not None and pH_bias < -0.20:
            if not calcite_saturated:
                self._add(
                    suggestions,
                    "keff_calcite",
                    1.10,
                    1,
                    "pH is underpredicted; increase carbonate buffering.",
                    f"Bias_pH={pH_bias}",
                    limits,
                )

                self._add(
                    suggestions,
                    "phi_calcite",
                    1.05,
                    2,
                    "pH is underpredicted; increase calcite inventory slightly.",
                    f"Bias_pH={pH_bias}",
                    limits,
                )

            else:
                self._add(
                    suggestions,
                    "keff_pyrite",
                    0.92,
                    1,
                    "pH is underpredicted, but calcite has already increased >2x; reduce pyrite acid generation.",
                    f"Bias_pH={pH_bias}; keff_calcite_growth={calcite_growth}",
                    limits,
                )

                self._add(
                    suggestions,
                    "usr_pyrite",
                    0.95,
                    2,
                    "pH is underpredicted, but calcite has already increased >2x; reduce pyrite-related acid source.",
                    f"Bias_pH={pH_bias}; keff_calcite_growth={calcite_growth}",
                    limits,
                )

                self._add(
                    suggestions,
                    "imr_pyrite",
                    0.95,
                    2,
                    "pH is underpredicted, but calcite has already increased >2x; reduce pyrite-related acid source.",
                    f"Bias_pH={pH_bias}; keff_calcite_growth={calcite_growth}",
                    limits,
                )

            self._add(
                suggestions,
                "keff_pyrite",
                0.92,
                2,
                "pH is underpredicted; reduce pyrite acid generation.",
                f"Bias_pH={pH_bias}",
                limits,
            )

            self._add(
                suggestions,
                "usr_pyrite",
                0.95,
                2,
                "pH is underpredicted; reduce pyrite-related acid source.",
                f"Bias_pH={pH_bias}",
                limits,
            )

        # --------------------------------------------------
        # Priority 2: sulfate correction
        # --------------------------------------------------

        if so4_rel is not None and so4_rel < -0.20:
            self._add(
                suggestions,
                "usr_pyrite",
                1.08,
                1,
                "SO4 is underpredicted; increase pyrite-related sulfate source.",
                f"SO4_relative_bias={so4_rel}",
                limits,
            )

            self._add(
                suggestions,
                "imr_pyrite",
                1.08,
                1,
                "SO4 is underpredicted; increase pyrite-related sulfate source.",
                f"SO4_relative_bias={so4_rel}",
                limits,
            )

            self._add(
                suggestions,
                "keff_pyrite",
                1.08,
                2,
                "SO4 is underpredicted; increase pyrite oxidation scale.",
                f"SO4_relative_bias={so4_rel}",
                limits,
            )

            self._add(
                suggestions,
                "top_flux",
                0.95,
                3,
                "SO4 is underpredicted; reduce flushing slightly to increase concentration/residence effect.",
                f"SO4_relative_bias={so4_rel}",
                limits,
            )

        elif so4_rel is not None and so4_rel > 0.20:
            self._add(
                suggestions,
                "usr_pyrite",
                0.92,
                1,
                "SO4 is overpredicted; reduce pyrite-related sulfate source.",
                f"SO4_relative_bias={so4_rel}",
                limits,
            )

            self._add(
                suggestions,
                "imr_pyrite",
                0.92,
                1,
                "SO4 is overpredicted; reduce pyrite-related sulfate source.",
                f"SO4_relative_bias={so4_rel}",
                limits,
            )

            self._add(
                suggestions,
                "keff_pyrite",
                0.92,
                2,
                "SO4 is overpredicted; reduce pyrite oxidation scale.",
                f"SO4_relative_bias={so4_rel}",
                limits,
            )

        # --------------------------------------------------
        # Priority 3: trace metals only after pH/SO4 are acceptable
        # --------------------------------------------------

        if allow_trace_metals:
            metal_rules = [
                ("zn+2", zn_bias, "keff_sphalerite", 0.90, 1.10, "sphalerite"),
                ("cd+2", cd_bias, "keff_sphalerite", 0.90, 1.10, "sphalerite/Cd"),
                ("pb+2", pb_bias, "keff_galena", 0.90, 1.10, "galena"),
                ("cu+2", cu_bias, "keff_chalcopyr", 0.90, 1.10, "chalcopyrite"),
            ]

            for sp, b, p, down, up, mineral in metal_rules:
                if b is None:
                    continue

                if b > 0:
                    self._add(
                        suggestions,
                        p,
                        down,
                        4,
                        f"{sp} overpredicted; reduce {mineral} source.",
                        f"Bias_{sp}={b}",
                        limits,
                    )

                elif b < 0:
                    self._add(
                        suggestions,
                        p,
                        up,
                        4,
                        f"{sp} underpredicted; increase {mineral} source.",
                        f"Bias_{sp}={b}",
                        limits,
                    )

        else:
            log(
                self.paths,
                "V9.3 hierarchy: trace-metal calibration postponed until pH and SO4 are acceptable.",
            )

        # --------------------------------------------------
        # Secondary Mg / silicate tuning
        # --------------------------------------------------

        pH_ok_for_secondary = pH_bias is None or abs(pH_bias) < 0.20

        if pH_ok_for_secondary and mg_bias is not None:
            if mg_bias < 0:
                self._add(
                    suggestions,
                    "s_chlorite",
                    1.08,
                    5,
                    "Mg underpredicted; increase chlorite surface area.",
                    f"Bias_mg+2={mg_bias}",
                    limits,
                )

            elif mg_bias > 0:
                self._add(
                    suggestions,
                    "s_chlorite",
                    0.92,
                    5,
                    "Mg overpredicted; reduce chlorite surface area.",
                    f"Bias_mg+2={mg_bias}",
                    limits,
                )

        # --------------------------------------------------
        # Deduplicate by parameter and preserve priority order
        # --------------------------------------------------

        suggestions = sorted(suggestions, key=lambda s: s.priority)

        unique: List[Suggestion] = []
        seen = set()

        for s in suggestions:
            if s.parameter in seen:
                continue

            unique.append(s)
            seen.add(s.parameter)

            if len(unique) >= max_changes:
                break

        out = pd.DataFrame(
            [
                {
                    **s.__dict__,
                    "timestamp": timestamp(),
                    "runtime_risk": rt.get("runtime_risk"),
                    "pH_bias": pH_bias,
                    "so4_relative_bias": so4_rel,
                    "trace_metals_allowed": allow_trace_metals,
                    "keff_calcite_growth": calcite_growth,
                    "keff_calcite_saturated": calcite_saturated,
                    "decision_source": "V9_3_fallback_rules",
                }
                for s in unique
            ]
        )

        append_df_excel(self.paths.suggestions_file, out)

        diagnosis.to_excel(
            self.paths.results_dir / "geochemical_diagnosis_V9_3.xlsx",
            index=False,
        )

        return out

    # =========================================================================
    # Apply suggestions
    # =========================================================================

    def apply_suggestions(self, suggestions: pd.DataFrame) -> None:
        if suggestions is None or suggestions.empty:
            return

        import shutil

        backup = (
            self.paths.input_dir
            / f"agent_config_backup_V10_5_{timestamp().replace(':', '').replace(' ', '_')}.xlsx"
        )

        shutil.copy2(self.paths.config_file, backup)

        xls = pd.ExcelFile(backup)

        sheets = {
            s: pd.read_excel(backup, sheet_name=s)
            for s in xls.sheet_names
        }

        params = sheets["parameters"].copy()

        for _, row in suggestions.iterrows():
            mask = (
                params["parameter"].astype(str).str.strip()
                == str(row["parameter"]).strip()
            )
            params.loc[mask, "value"] = row["new_value"]

        sheets["parameters"] = params

        with pd.ExcelWriter(self.paths.config_file, engine="openpyxl") as writer:
            for name, df in sheets.items():
                df.to_excel(writer, sheet_name=name, index=False)

        log(self.paths, f"Updated agent_config.xlsx; backup: {backup}")
