from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from modules.step_size_controller import StepSizeController
from modules.v14_utils import atomic_copy_file, write_excel_atomic, write_json_atomic


class DummyPaths:
    def __init__(self, root: Path):
        self.results_dir = root / "04_results"
        self.agent_core_dir = root / "07_agent_core_V14_4_6"
        self.results_dir.mkdir(parents=True, exist_ok=True)
        (self.agent_core_dir / "config").mkdir(parents=True, exist_ok=True)


class DummyConfig:
    def optimizer_v13(self):
        return {}


class TransientReplace:
    def __init__(self, real_replace, failures: int = 2):
        self.real_replace = real_replace
        self.failures = failures
        self.calls = 0

    def __call__(self, source, destination):
        self.calls += 1
        if self.calls <= self.failures:
            exc = PermissionError(13, "transient Windows sharing violation")
            # WinError 5 is the failure observed in the HCT3 campaign.
            exc.winerror = 5
            raise exc
        return self.real_replace(source, destination)


class TestV1446AtomicStateWrites(unittest.TestCase):
    def test_json_writer_retries_transient_permission_error(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "state.json"
            target.write_text('{"old": true}\n', encoding="utf-8")
            transient = TransientReplace(os.replace, failures=2)
            with patch("modules.v14_utils.os.replace", side_effect=transient):
                write_json_atomic(target, {"value": 42})
            self.assertGreaterEqual(transient.calls, 3)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8"))["value"], 42)
            self.assertEqual(list(root.glob("state.json.tmp-*")), [])

    def test_excel_writer_retries_and_cleans_unique_temp(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            target = root / "state.xlsx"
            pd.DataFrame([{"old": 1}]).to_excel(target, index=False)
            transient = TransientReplace(os.replace, failures=1)
            with patch("modules.v14_utils.os.replace", side_effect=transient):
                write_excel_atomic(target, pd.DataFrame([{"new": 2}]))
            self.assertEqual(int(pd.read_excel(target).iloc[0]["new"]), 2)
            self.assertEqual(list(root.glob("state.tmp-*.xlsx")), [])

    def test_atomic_copy_retries_transient_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.bin"
            target = root / "target.bin"
            source.write_bytes(b"new-best")
            target.write_bytes(b"old-best")
            transient = TransientReplace(os.replace, failures=1)
            with patch("modules.v14_utils.os.replace", side_effect=transient):
                atomic_copy_file(source, target)
            self.assertEqual(target.read_bytes(), b"new-best")
            self.assertEqual(list(root.glob("target.bin.tmp-*")), [])

    def test_step_controller_survives_transient_lock(self):
        with tempfile.TemporaryDirectory() as temp:
            paths = DummyPaths(Path(temp))
            controller = StepSizeController(paths, DummyConfig())
            transient = TransientReplace(os.replace, failures=2)
            with patch("modules.v14_utils.os.replace", side_effect=transient):
                controller._save({"parameters": {}, "metadata": {}})
            state = json.loads(controller.state_file.read_text(encoding="utf-8"))
            self.assertEqual(state["metadata"]["optimizer_version"], "V14.4.7")
            self.assertGreaterEqual(transient.calls, 3)

    def test_non_retryable_replace_error_is_not_hidden(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "state.json"
            with patch("modules.v14_utils.os.replace", side_effect=FileNotFoundError("bad path")):
                with self.assertRaises(FileNotFoundError):
                    write_json_atomic(target, {"x": 1})
            self.assertEqual(list(Path(temp).glob("state.json.tmp-*")), [])


if __name__ == "__main__":
    unittest.main()
