from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import shutil
import tempfile
import unittest

import pandas as pd

from modules.v14_transaction_manager import V14TransactionManager


class V1437ObservationRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="v14_3_7_recovery_"))
        self.input_dir = self.tmp / "01_input"
        self.results_dir = self.tmp / "04_results"
        self.input_dir.mkdir(parents=True)
        self.results_dir.mkdir(parents=True)
        self.config = self.input_dir / "agent_config.xlsx"
        self.best = self.results_dir / "best_parameters_V14.xlsx"
        pd.DataFrame([{"parameter": "x", "value": 1.0}]).to_excel(self.config, index=False)
        shutil.copy2(self.config, self.best)
        pd.DataFrame([{"parameter": "x", "pending": False, "pending_candidate_id": ""}]).to_excel(
            self.results_dir / "v14_optimizer_parameter_state.xlsx", index=False
        )
        pd.DataFrame([{"pending_candidate_id": "", "iteration": 10}]).to_excel(
            self.results_dir / "v14_optimizer_state.xlsx", index=False
        )
        self.paths = SimpleNamespace(
            results_dir=self.results_dir,
            config_file=self.config,
            input_dir=self.input_dir,
            project_dir=self.tmp,
        )
        self.manager = V14TransactionManager(self.paths, best_config_file=self.best)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_observation_failure_restores_preselection_state(self) -> None:
        checkpoint = self.manager.create_selection_checkpoint()
        pd.DataFrame([{"parameter": "x", "pending": True, "pending_candidate_id": "abc"}]).to_excel(
            self.results_dir / "v14_optimizer_parameter_state.xlsx", index=False
        )
        pd.DataFrame([{"pending_candidate_id": "abc", "iteration": 10}]).to_excel(
            self.results_dir / "v14_optimizer_state.xlsx", index=False
        )
        transaction = self.manager.begin_transaction(
            checkpoint,
            suggestion={"candidate_id": "abc", "parameter": "x", "old_value": 1.0, "new_value": 1.05},
            selection_reason="unit_test",
        )
        payload = {
            "candidate_id": "abc",
            "candidate_TOTAL_SCORE": 9.0,
            "run_folder": str(self.tmp / "run_001"),
        }
        self.manager.mark_observation_pending(transaction, payload)
        self.manager.mark_observation_failed(transaction, payload, PermissionError("locked workbook"))

        summary = self.manager.recover_interrupted_transactions(reason="unit_test")
        self.assertEqual(len(summary["recovered"]), 1)
        self.assertEqual(self.manager.unfinished_transactions(), [])

        memory = pd.read_excel(self.results_dir / "v14_optimizer_parameter_state.xlsx")
        state = pd.read_excel(self.results_dir / "v14_optimizer_state.xlsx")
        self.assertFalse(bool(memory.loc[0, "pending"]))
        self.assertTrue(pd.isna(state.loc[0, "pending_candidate_id"]))
        self.assertEqual(int(state.loc[0, "iteration"]), 10)
        self.assertEqual(self.manager._read_transaction(transaction)["status"], "recovered")


if __name__ == "__main__":
    unittest.main()
