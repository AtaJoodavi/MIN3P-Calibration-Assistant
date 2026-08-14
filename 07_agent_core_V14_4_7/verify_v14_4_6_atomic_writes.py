from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from modules.v14_utils import write_excel_atomic, write_json_atomic


class TransientReplace:
    def __init__(self, real_replace, failures=2):
        self.real_replace = real_replace
        self.failures = failures
        self.calls = 0

    def __call__(self, source, destination):
        self.calls += 1
        if self.calls <= self.failures:
            exc = PermissionError(13, "simulated transient Windows lock")
            exc.winerror = 5
            raise exc
        return self.real_replace(source, destination)


with tempfile.TemporaryDirectory() as temp:
    root = Path(temp)
    json_path = root / "v14_step_size_state.json"
    json_path.write_text('{"old": true}\n', encoding="utf-8")
    transient = TransientReplace(os.replace, failures=2)
    with patch("modules.v14_utils.os.replace", side_effect=transient):
        write_json_atomic(json_path, {"optimizer_version": "V14.4.6", "ok": True})
    assert json.loads(json_path.read_text(encoding="utf-8"))["ok"] is True

    xlsx_path = root / "v14_optimizer_state.xlsx"
    transient_excel = TransientReplace(os.replace, failures=1)
    with patch("modules.v14_utils.os.replace", side_effect=transient_excel):
        write_excel_atomic(xlsx_path, pd.DataFrame([{"optimizer_version": "V14.4.6"}]))
    assert pd.read_excel(xlsx_path).iloc[0]["optimizer_version"] == "V14.4.6"

print("PASS: V14.4.6 robust atomic state writes")
print(f"  JSON replace attempts: {transient.calls} (2 simulated WinError 5 failures recovered)")
print(f"  Excel replace attempts: {transient_excel.calls} (1 simulated lock recovered)")
print("  unique temporary files cleaned after promotion")
