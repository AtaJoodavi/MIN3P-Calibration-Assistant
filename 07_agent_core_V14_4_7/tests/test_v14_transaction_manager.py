from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import shutil
import tempfile
import unittest

import pandas as pd

from modules.v14_transaction_manager import V14TransactionManager


class V14TransactionManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v14_txn_test_"))
        self.input_dir = self.tmp / "01_input"
        self.results_dir = self.tmp / "04_results"
        self.input_dir.mkdir(parents=True)
        self.results_dir.mkdir(parents=True)
        self.config = self.input_dir / "agent_config.xlsx"
        self.best = self.results_dir / "best_parameters_V14.xlsx"
        sheets = {
            "parameters": pd.DataFrame(
                [{"parameter": "feoh_s_mass", "value": 0.2, "min": 0.0, "max": None, "status": "active"}]
            ),
            "model_files": pd.DataFrame(
                [{"key": "template_file", "value": "template.dat"}, {"key": "input_file", "value": "model.dat"}]
            ),
        }
        with pd.ExcelWriter(self.config, engine="openpyxl") as writer:
            for name, frame in sheets.items():
                frame.to_excel(writer, sheet_name=name, index=False)
        shutil.copy2(self.config, self.best)
        pd.DataFrame([{"parameter": "feoh_s_mass", "pending": False, "direction_queue": "increase|decrease"}]).to_excel(
            self.results_dir / "v14_optimizer_parameter_state.xlsx", index=False
        )
        pd.DataFrame([{"pending_candidate_id": "", "last_action": "ready"}]).to_excel(
            self.results_dir / "v14_optimizer_state.xlsx", index=False
        )
        self.paths = SimpleNamespace(
            results_dir=self.results_dir,
            config_file=self.config,
            input_dir=self.input_dir,
            project_dir=self.tmp,
        )
        self.manager = V14TransactionManager(self.paths, best_config_file=self.best)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_candidate_workbook_isolated_and_cancel_restores_state(self):
        checkpoint = self.manager.create_selection_checkpoint()
        # Simulate next_suggestion() creating a pending optimizer state.
        pd.DataFrame([{"parameter": "feoh_s_mass", "pending": True, "direction_queue": "decrease"}]).to_excel(
            self.results_dir / "v14_optimizer_parameter_state.xlsx", index=False
        )
        pd.DataFrame([{"pending_candidate_id": "abc", "last_action": "candidate_selected"}]).to_excel(
            self.results_dir / "v14_optimizer_state.xlsx", index=False
        )
        suggestion = pd.DataFrame(
            [{"candidate_id": "abc", "parameter": "feoh_s_mass", "old_value": 0.2, "new_value": 0.21}]
        )
        transaction = self.manager.begin_transaction(
            checkpoint,
            suggestion=suggestion.iloc[0].to_dict(),
            selection_reason="unit_test",
        )
        self.manager.prepare_candidate(transaction)
        # Read all sheets before rewriting; Windows must release this reader handle.
        loaded = self.manager.candidate_config_reader(transaction).all_sheets()
        self.assertIn("parameters", loaded)
        self.manager.apply_suggestion_to_candidate(transaction, suggestion)

        self.assertTrue(self.manager.canonical_best_hashes()["match"])
        candidate = pd.read_excel(transaction.candidate_config_file, sheet_name="parameters")
        self.assertAlmostEqual(float(candidate.loc[0, "value"]), 0.21)
        canonical = pd.read_excel(self.config, sheet_name="parameters")
        self.assertAlmostEqual(float(canonical.loc[0, "value"]), 0.2)

        self.manager.cancel_and_recover(transaction, reason="unit_test_interrupt")
        memory = pd.read_excel(self.results_dir / "v14_optimizer_parameter_state.xlsx")
        state = pd.read_excel(self.results_dir / "v14_optimizer_state.xlsx")
        self.assertFalse(bool(memory.loc[0, "pending"]))
        self.assertEqual(str(memory.loc[0, "direction_queue"]), "increase|decrease")
        self.assertEqual(str(state.loc[0, "pending_candidate_id"]), "nan")
        self.assertTrue(self.manager.canonical_best_hashes()["match"])


if __name__ == "__main__":
    unittest.main()
