from __future__ import annotations

"""Read-only V14 interaction-pair inspection.

Run from the V14 project root:
    python .\interaction_pair_dry_run.py

This script invokes ParameterPairOptimizer.dry_run(), which never calls the
mutable interaction selector, does not activate/release interaction states, and
does not write v14_interaction_runtime_state.json.
"""

from pathlib import Path
import hashlib
import json
import sys

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from min3p_ai_pipeline_V14 import V14Workflow


def _digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    workflow = V14Workflow()
    optimizer = workflow.optimizer_v14
    pair_optimizer = optimizer.pair_optimizer
    if pair_optimizer is None or optimizer.state_manager is None:
        raise RuntimeError("V14 interaction services are unavailable.")

    state_path = Path(pair_optimizer.state_file)
    before = _digest(state_path)
    state = optimizer._read_state()
    report = pair_optimizer.dry_run(
        parameters=optimizer._params(),
        state_manager=optimizer.state_manager,
        evidence={
            **dict(state.get("last_diagnostics", {}) or {}),
            "last_event_accepted": bool(state.get("last_event_accepted", False)),
        },
        valid_run_count=int(state.get("valid_run_count", 0) or 0),
        iteration=int(state.get("iteration", 0) or 0),
    )
    after = _digest(state_path)
    if before != after:
        raise RuntimeError("Dry-run safety failure: interaction runtime state changed.")

    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    print("PASS: interaction dry-run did not modify v14_interaction_runtime_state.json.")


if __name__ == "__main__":
    main()
