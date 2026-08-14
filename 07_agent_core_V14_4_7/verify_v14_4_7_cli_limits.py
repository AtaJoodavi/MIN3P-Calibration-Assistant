from __future__ import annotations

import subprocess
import sys
from unittest.mock import MagicMock

from modules.adaptive_coordinate_optimizer_V14 import AdaptiveCoordinateOptimizerV14
from modules.step_size_controller import StepSizeController
from modules.v14_transaction_manager import V14TransactionManager


assert StepSizeController.VERSION == "V14.4.7"
assert AdaptiveCoordinateOptimizerV14.VERSION == "V14.4.7"
assert V14TransactionManager.VERSION == "V14.4.7"

help_text = subprocess.run(
    [sys.executable, "min3p_ai_pipeline_V14.py", "--help"],
    check=True,
    capture_output=True,
    text=True,
).stdout
for flag in ("--max-runs", "--max-candidates", "--max-physical-runs"):
    assert flag in help_text, flag

optimizer = AdaptiveCoordinateOptimizerV14.__new__(AdaptiveCoordinateOptimizerV14)
optimizer._read_state = MagicMock(return_value={"current_best_score": 8.4, "convergence_status": "running"})
written = {}
optimizer._write_state = MagicMock(side_effect=lambda state: written.update(state))
optimizer.record_invocation_summary({
    "last_invocation_stop_reason": "max_physical_runs_reached",
    "last_invocation_physical_runs": 20,
    "last_invocation_cache_hits": 5,
})
assert written["current_best_score"] == 8.4
assert written["convergence_status"] == "running"
assert written["last_invocation_physical_runs"] == 20
assert written["last_invocation_cache_hits"] == 5

print("PASS: V14.4.7 clear candidate/physical-run limits and invocation summary")
print("  --max-runs remains the backward-compatible candidate-cycle alias")
print("  --max-candidates explicitly counts optimizer decisions including cache hits")
print("  --max-physical-runs counts only new cache-miss MIN3P executions")
