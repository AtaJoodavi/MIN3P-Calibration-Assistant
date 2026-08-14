from __future__ import annotations

import importlib
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from modules.v14_transaction_runner import (  # noqa: E402
    build_fast_candidate_wrapper,
    build_full_postprocess_wrapper,
)


FAKE_PLOTS = r'''
from pathlib import Path
import pandas as pd

EXECUTE_MIN3P = True
CREATE_PLOTS = True


def write_excel(sheets, path, *args, **kwargs):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("written", encoding="utf-8")


def make_time_series_plots_from_mvc(*args, **kwargs):
    Path(__file__).with_name("HEAVY_CALLED.txt").write_text("mvc", encoding="utf-8")
    return pd.DataFrame({"x": [1]})


def overlay_gsp_profiles_make_sheets(*args, **kwargs):
    Path(__file__).with_name("HEAVY_CALLED.txt").write_text("gsp", encoding="utf-8")
    return {"gsp": pd.DataFrame({"x": [1]})}


def build_outflow_timeseries_from_gsm(*args, **kwargs):
    Path(__file__).with_name("HEAVY_CALLED.txt").write_text("gsm", encoding="utf-8")
    return {"gsm": pd.DataFrame({"x": [1]})}


def process_concentration_type(*args, **kwargs):
    Path(__file__).with_name("HEAVY_CALLED.txt").write_text("concentration", encoding="utf-8")
    return {"concentration": pd.DataFrame({"x": [1]})}


def build_outflow_timeseries_from_conc(*args, **kwargs):
    Path(__file__).with_name("HEAVY_CALLED.txt").write_text("outflow", encoding="utf-8")
    return {"outflow": pd.DataFrame({"x": [1]})}


def process_gsd_gss_type(*args, **kwargs):
    Path(__file__).with_name("HEAVY_CALLED.txt").write_text("gsd", encoding="utf-8")
    return {"gsd": pd.DataFrame({"x": [1]})}


def make_mineral_plots_from_mms(*args, **kwargs):
    Path(__file__).with_name("HEAVY_CALLED.txt").write_text("mms", encoding="utf-8")
    return pd.DataFrame({"x": [1]})


def process_gbt_type(*args, **kwargs):
    Path(__file__).with_name("GBT_CALLED.txt").write_text("yes", encoding="utf-8")
    Path(__file__).with_name("GBT_PLOT_FLAG.txt").write_text(str(kwargs.get("do_plots")), encoding="utf-8")
    return {"gbt": pd.DataFrame({"time_days": [0.0], "x": [1.0]})}


def process_gbm_type(*args, **kwargs):
    Path(__file__).with_name("GBM_CALLED.txt").write_text("yes", encoding="utf-8")
    Path(__file__).with_name("GBM_PLOT_FLAG.txt").write_text(str(kwargs.get("do_plots")), encoding="utf-8")
    return {"gbm": pd.DataFrame({"time_days": [0.0], "pH": [7.0]})}


def main():
    workdir = Path(__file__).resolve().parent
    if EXECUTE_MIN3P:
        (workdir / "SOLVER_CALLED.txt").write_text("yes", encoding="utf-8")
    elif not (workdir / "RAW_OUTPUT_EXISTS.txt").exists():
        raise RuntimeError("full postprocess must not run the solver")

    results_root = workdir / "Results_000001"
    mvc_out = results_root / "mvc_plots"
    gsp_out = results_root / "gsp_plots"
    conc_root = results_root / "conc_plots"
    outflow_conc_dir = conc_root / "outflow_concentration"
    for item in (mvc_out, gsp_out, outflow_conc_dir):
        item.mkdir(parents=True, exist_ok=True)

    fls_units = {}
    time_unit = "days"
    obs_outflow_path = workdir / "obs.xlsx"
    mvc_files = [workdir / "x_o.mvc"]
    gsp_files = [workdir / "x.gsp"]
    gsm_files = [workdir / "x.gsm"]
    gst_files = [workdir / "x.gst"]
    gbt_files = [workdir / "x.gbt"]
    gbm_files = [workdir / "x.gbm"]
    mms_files = [workdir / "x.mms"]

    write_excel({"MVC time series": make_time_series_plots_from_mvc(mvc_files[0], mvc_out, fls_units, time_unit)}, mvc_out / "mvc_plots.xlsx")
    gsp_sheets = overlay_gsp_profiles_make_sheets(gsp_files, gsp_out, fls_units, time_unit)
    if gsp_sheets:
        write_excel(gsp_sheets, gsp_out / "gsp_plots.xlsx")
    gsm_outflow_sheets = build_outflow_timeseries_from_gsm(gsm_files, outflow_conc_dir, fls_units, obs_outflow_path, time_unit)
    if gsm_outflow_sheets:
        write_excel(gsm_outflow_sheets, outflow_conc_dir / "outflow_master_gsm.xlsx")
    sheets = process_concentration_type(gst_files, conc_root / "gst", fls_units, do_plots=CREATE_PLOTS, transform_hplus_to_ph=True, time_unit=time_unit)
    if sheets:
        write_excel(sheets, conc_root / "gst.xlsx")
    out_sheets = build_outflow_timeseries_from_conc(gst_files, outflow_conc_dir, fls_units, obs_outflow_path, do_plots=CREATE_PLOTS, time_unit=time_unit)
    if out_sheets:
        write_excel(out_sheets, outflow_conc_dir / "outflow.xlsx")
    gbt_sheets = process_gbt_type(gbt_files, conc_root / "gbt", fls_units, obs_outflow_path, do_plots=CREATE_PLOTS, transform_hplus_to_ph=False, time_unit=time_unit)
    if gbt_sheets:
        write_excel(gbt_sheets, conc_root / "gbt" / "concentration_plots_gbt.xlsx")
    gbm_sheets = process_gbm_type(gbm_files, conc_root / "gbm", fls_units, obs_outflow_path, do_plots=CREATE_PLOTS, time_unit=time_unit)
    if gbm_sheets:
        write_excel(gbm_sheets, conc_root / "gbm" / "master_variables_gbm.xlsx")
    write_excel({"Mineral system mass": make_mineral_plots_from_mms(mms_files[0], results_root / "minerals", fls_units, time_unit)}, results_root / "minerals" / "minerals_mms_timeseries.xlsx")
'''


class V14FastCandidateRunnerTests(unittest.TestCase):
    def test_fast_wrapper_runs_solver_and_objective_outputs_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v14_fast_runner_") as raw:
            root = Path(raw)
            (root / "plotsV46.py").write_text(FAKE_PLOTS, encoding="utf-8")
            wrapper = root / "v14_fast_candidate_runner.py"
            wrapper.write_text(build_fast_candidate_wrapper("plotsV46.py"), encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(wrapper)],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "SOLVER_CALLED.txt").exists())
            self.assertTrue((root / "GBT_CALLED.txt").exists())
            self.assertTrue((root / "GBM_CALLED.txt").exists())
            self.assertEqual((root / "GBT_PLOT_FLAG.txt").read_text(encoding="utf-8"), "False")
            self.assertEqual((root / "GBM_PLOT_FLAG.txt").read_text(encoding="utf-8"), "False")
            self.assertFalse((root / "HEAVY_CALLED.txt").exists())
            self.assertTrue((root / "Results_000001" / "conc_plots" / "gbt" / "concentration_plots_gbt.xlsx").exists())
            self.assertTrue((root / "Results_000001" / "conc_plots" / "gbm" / "master_variables_gbm.xlsx").exists())

    def test_full_wrapper_disables_solver_execution(self) -> None:
        with tempfile.TemporaryDirectory(prefix="v14_full_postprocess_") as raw:
            root = Path(raw)
            (root / "plotsV46.py").write_text(FAKE_PLOTS, encoding="utf-8")
            (root / "RAW_OUTPUT_EXISTS.txt").write_text("yes", encoding="utf-8")
            wrapper = root / "v14_full_postprocess_runner.py"
            wrapper.write_text(build_full_postprocess_wrapper("plotsV46.py"), encoding="utf-8")

            result = subprocess.run(
                [sys.executable, str(wrapper)],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / "SOLVER_CALLED.txt").exists())
            self.assertTrue((root / "GBT_CALLED.txt").exists())
            self.assertTrue((root / "GBM_CALLED.txt").exists())
            self.assertEqual((root / "GBT_PLOT_FLAG.txt").read_text(encoding="utf-8"), "True")
            self.assertEqual((root / "GBM_PLOT_FLAG.txt").read_text(encoding="utf-8"), "True")


if __name__ == "__main__":
    unittest.main()
