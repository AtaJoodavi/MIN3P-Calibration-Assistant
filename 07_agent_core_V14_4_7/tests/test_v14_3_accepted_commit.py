from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import shutil
import tempfile
import unittest

import pandas as pd

from modules.v14_transaction_manager import V14TransactionManager


class V143AcceptedCommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="v14_3_commit_"))
        self.input_dir = self.tmp / "01_input"
        self.results_dir = self.tmp / "04_results"
        self.input_dir.mkdir(parents=True)
        self.results_dir.mkdir(parents=True)
        self.config = self.input_dir / "agent_config.xlsx"
        self.best = self.results_dir / "best_parameters_V14.xlsx"
        with pd.ExcelWriter(self.config, engine="openpyxl") as writer:
            pd.DataFrame([{"parameter": "feoh_s_mass", "value": 0.2, "status": "active"}]).to_excel(writer, sheet_name="parameters", index=False)
            pd.DataFrame([{"key": "template_file", "value": "template.dat"}]).to_excel(writer, sheet_name="model_files", index=False)
        shutil.copy2(self.config, self.best)
        pd.DataFrame([{"pending_candidate_id": ""}]).to_excel(self.results_dir / "v14_optimizer_state.xlsx", index=False)
        pd.DataFrame([{"parameter": "feoh_s_mass", "pending": False}]).to_excel(self.results_dir / "v14_optimizer_parameter_state.xlsx", index=False)
        self.paths = SimpleNamespace(results_dir=self.results_dir, config_file=self.config, input_dir=self.input_dir, project_dir=self.tmp)
        self.manager = V14TransactionManager(self.paths, best_config_file=self.best)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_accepted_candidate_commit_requires_and_reaches_new_best_snapshot(self):
        checkpoint = self.manager.create_selection_checkpoint()
        suggestion = pd.DataFrame([{"candidate_id": "accept1", "parameter": "feoh_s_mass", "old_value": 0.2, "new_value": 0.21}])
        transaction = self.manager.begin_transaction(checkpoint, suggestion=suggestion.iloc[0].to_dict(), selection_reason="unit_test")
        self.manager.prepare_candidate(transaction)
        self.manager.apply_suggestion_to_candidate(transaction, suggestion)
        self.manager.mark_accepted_commit_pending(transaction, {"accepted": True, "decision": "accept"})
        self.manager.commit_candidate_to_canonical(transaction)
        # The pipeline's accepted path then snapshots canonical -> V14 best.
        shutil.copy2(self.config, self.best)
        self.manager.complete_transaction(transaction, outcome="accepted", event={"accepted": True, "decision": "accept"})
        self.assertTrue(self.manager.canonical_best_hashes()["match"])
        canonical = pd.read_excel(self.config, sheet_name="parameters")
        self.assertAlmostEqual(float(canonical.loc[0, "value"]), 0.21)
        manifest = __import__("json").loads(transaction.manifest_file.read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "accepted")


if __name__ == "__main__":
    unittest.main()
