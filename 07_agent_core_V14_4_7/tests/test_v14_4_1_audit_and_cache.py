from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from min3p_ai_pipeline_V14 import V14Workflow
from modules.adaptive_coordinate_optimizer_V14 import AdaptiveCoordinateOptimizerV14
from modules.history_manager import V14HistoryManager
from modules.v14_transaction_manager import V14TransactionManager


class DummyConfig:
    def __init__(self, parameters: pd.DataFrame):
        self._parameters = parameters.copy()

    def parameters(self):
        return self._parameters.copy()

    def optimizer_v13(self):
        return {}

    def sheet(self, name: str):
        if name == "v13_parameter_groups":
            return pd.DataFrame([
                {"parameter": "Kz", "group": "hydraulic", "enabled": "yes", "priority": 1}
            ])
        raise ValueError(name)


class V1441Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v1441_"))
        self.input_dir = self.tmp / "01_input"
        self.results_dir = self.tmp / "04_results"
        self.agent_core = self.tmp / "07_agent_core_V14_4_1"
        self.input_dir.mkdir(parents=True)
        self.results_dir.mkdir(parents=True)
        (self.agent_core / "config").mkdir(parents=True)
        (self.agent_core / "config" / "calibration_rules.yaml").write_text(
            "initial_step_fraction: 0.05\nminimum_step_fraction: 0.005\nmaximum_step_fraction: 0.20\n",
            encoding="utf-8",
        )
        self.config_file = self.input_dir / "agent_config.xlsx"
        self.best_file = self.results_dir / "best_parameters_V14.xlsx"
        self._write_config(0.2)
        shutil.copy2(self.config_file, self.best_file)
        self.paths = SimpleNamespace(
            results_dir=self.results_dir,
            config_file=self.config_file,
            input_dir=self.input_dir,
            project_dir=self.tmp,
            agent_core_dir=self.agent_core,
            post_script=self.agent_core / "plotsV46.py",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_config(self, value: float):
        sheets = {
            "parameters": pd.DataFrame([
                {"parameter": "Kz", "value": value, "min": 0.1, "max": 0.3, "status": "active"}
            ]),
            "model_files": pd.DataFrame([
                {"key": "template_file", "value": "template.dat"},
                {"key": "input_file", "value": "model.dat"},
                {"key": "observed_file", "value": "observed.xlsx"},
            ]),
        }
        with pd.ExcelWriter(self.config_file, engine="openpyxl") as writer:
            for name, frame in sheets.items():
                frame.to_excel(writer, sheet_name=name, index=False)

    def _state_files(self):
        pd.DataFrame([{"parameter": "Kz", "pending": False, "direction_queue": "increase|decrease"}]).to_excel(
            self.results_dir / "v14_optimizer_parameter_state.xlsx", index=False
        )
        pd.DataFrame([{"pending_candidate_id": "", "last_action": "ready"}]).to_excel(
            self.results_dir / "v14_optimizer_state.xlsx", index=False
        )

    def test_exact_baseline_configuration_is_reused(self):
        self._state_files()
        manager = V14TransactionManager(self.paths, best_config_file=self.best_file)
        manager.register_evaluated_configuration(
            config_file=self.best_file,
            total_score=10.0,
            run_folder=self.tmp / "03_runs" / "baseline",
            run_status="success",
            source="accepted_baseline",
        )

        # Current accepted best moves to 0.21, but the next candidate returns to
        # the previously evaluated baseline value 0.20.
        self._write_config(0.21)
        shutil.copy2(self.config_file, self.best_file)
        checkpoint = manager.create_selection_checkpoint()
        suggestion = pd.DataFrame([
            {"candidate_id": "return_to_baseline", "parameter": "Kz", "old_value": 0.21, "new_value": 0.20}
        ])
        transaction = manager.begin_transaction(
            checkpoint,
            suggestion=suggestion.iloc[0].to_dict(),
            selection_reason="unit_test",
        )
        manager.prepare_candidate(transaction)
        manager.apply_suggestion_to_candidate(transaction, suggestion)
        hit = manager.find_cached_candidate(transaction)
        self.assertIsNotNone(hit)
        self.assertTrue(hit["cache_hit"])
        self.assertEqual(hit["cache_source"], "accepted_baseline")
        self.assertAlmostEqual(hit["TOTAL_SCORE"], 10.0)


    def test_history_bootstrap_reuses_pre_v1441_baseline(self):
        self._state_files()
        self.paths.ranking_file = self.results_dir / "run_ranking.xlsx"
        baseline_run = str(self.tmp / "03_runs" / "baseline_pre_v1441")
        # Simulate the V14.4 decision log that referenced the old baseline before
        # V14.4.1 introduced the persistent cache file.
        pd.DataFrame([
            {
                "phase": "candidate_selected",
                "parameter": "Kz",
                "old_value": 0.2,
                "new_value": 0.21,
                "current_best_objective": 10.0,
                "current_best_run_folder": baseline_run,
                "event_json": "{}",
                "diagnostics_json": "{}",
            }
        ]).to_excel(self.results_dir / "calibration_decision_log.xlsx", index=False)
        pd.DataFrame([
            {
                "run_folder": baseline_run,
                "run_status": "success",
                "TOTAL_SCORE": 10.0,
                "Kz": 0.2,
            }
        ]).to_excel(self.paths.ranking_file, index=False)

        # The currently accepted best is 0.21.  The next opposite-direction
        # candidate returns to the old 0.20 baseline.
        self._write_config(0.21)
        shutil.copy2(self.config_file, self.best_file)
        manager = V14TransactionManager(self.paths, best_config_file=self.best_file)
        checkpoint = manager.create_selection_checkpoint()
        suggestion = pd.DataFrame([
            {"candidate_id": "return_old_baseline", "parameter": "Kz", "old_value": 0.21, "new_value": 0.20}
        ])
        transaction = manager.begin_transaction(
            checkpoint, suggestion=suggestion.iloc[0].to_dict(), selection_reason="unit_test"
        )
        manager.prepare_candidate(transaction)
        manager.apply_suggestion_to_candidate(transaction, suggestion)
        hit = manager.find_cached_candidate(transaction)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["cache_source"], "v14_history_bootstrap")
        self.assertEqual(hit["run_folder"], baseline_run)
        self.assertAlmostEqual(hit["TOTAL_SCORE"], 10.0)

    def test_changed_workbook_context_does_not_hit_cache(self):
        self._state_files()
        manager = V14TransactionManager(self.paths, best_config_file=self.best_file)
        manager.register_evaluated_configuration(
            config_file=self.best_file,
            total_score=10.0,
            run_folder="baseline",
            run_status="success",
            source="accepted_baseline",
        )
        # Add a species sheet: same Kz is no longer the same evaluation context.
        with pd.ExcelWriter(self.config_file, engine="openpyxl") as writer:
            pd.DataFrame([{"parameter": "Kz", "value": 0.2, "min": 0.1, "max": 0.3, "status": "active"}]).to_excel(writer, sheet_name="parameters", index=False)
            pd.DataFrame([{"species": "SO4", "active": "yes", "weight": 2.0}]).to_excel(writer, sheet_name="species", index=False)
            pd.DataFrame([{"key": "template_file", "value": "template.dat"}]).to_excel(writer, sheet_name="model_files", index=False)
        shutil.copy2(self.config_file, self.best_file)
        checkpoint = manager.create_selection_checkpoint()
        suggestion = pd.DataFrame([
            {"candidate_id": "same_value_new_context", "parameter": "Kz", "old_value": 0.2, "new_value": 0.2}
        ])
        transaction = manager.begin_transaction(checkpoint, suggestion=suggestion.iloc[0].to_dict(), selection_reason="unit_test")
        manager.prepare_candidate(transaction)
        manager.apply_suggestion_to_candidate(transaction, suggestion)
        self.assertIsNone(manager.find_cached_candidate(transaction))

    def test_optimizer_event_is_written_once_even_with_history_service(self):
        config = DummyConfig(pd.DataFrame([
            {"parameter": "Kz", "value": 0.2, "min": 0.1, "max": 0.3, "status": "active"}
        ]))
        optimizer = AdaptiveCoordinateOptimizerV14(self.paths, config)
        history = V14HistoryManager(self.paths, config)
        optimizer.attach_services(history_manager=history)
        optimizer._event({"action": "unit_test_event", "parameter": "Kz"})
        frame = pd.read_excel(self.results_dir / "v14_optimizer_events.xlsx")
        rows = frame[frame["action"].astype(str).eq("unit_test_event")]
        self.assertEqual(len(rows), 1)

    def test_decision_log_contains_step_metadata_in_columns_and_event_json(self):
        workflow = V14Workflow.__new__(V14Workflow)
        workflow._latest_gpt_advice = None
        workflow.v14_decision_log_file = self.results_dir / "calibration_decision_log.xlsx"
        workflow.v14_history_file = self.results_dir / "optimization_history_V14.xlsx"
        workflow._v14_current_best = lambda: (9.9, "best_run")
        captured = []
        workflow._append_excel_safe = lambda path, rows, label="": captured.extend(rows)

        suggestion = pd.Series({
            "candidate_id": "c1",
            "parameter": "Kz",
            "group": "hydraulic",
            "direction": "increase",
            "old_value": 0.2,
            "new_value": 0.204,
            "step_fraction_applied": 0.05,
            "move_space": "logarithmic",
            "step_basis": "configured_log_range",
            "range_normalized": True,
            "configured_log_range_decades": 0.176091,
            "factor_applied": 1.02048,
        })
        workflow._audit_decision(
            phase="candidate_evaluated",
            suggestion=suggestion,
            event={"accepted": False, "decision": "reject"},
            diagnostics={},
            restoration_verified=True,
            decision_source="unit_test",
        )
        row = captured[0]
        self.assertEqual(row["step_basis"], "configured_log_range")
        self.assertTrue(bool(row["range_normalized"]))
        payload = json.loads(row["event_json"])
        self.assertEqual(payload["step_basis"], "configured_log_range")
        self.assertTrue(payload["range_normalized"])


if __name__ == "__main__":
    unittest.main()
