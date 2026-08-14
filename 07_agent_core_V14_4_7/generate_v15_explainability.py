from __future__ import annotations

import argparse
import json
from pathlib import Path

from modules.calibration_explainability_V15 import build_v15_candidate_explainability


def main() -> None:
    parser = argparse.ArgumentParser(
        description="V15.3.1 read-only candidate-selection explainability layer for V14/V15 reports."
    )
    parser.add_argument("--agent-core-dir", default=str(Path.cwd()), help="Path to 07_agent_core_V14; default is current directory.")
    parser.add_argument("--output-dir", default=None, help="Optional V15 report output directory. Default: ../05_reports/V15_scientific_report.")
    args = parser.parse_args()
    result = build_v15_candidate_explainability(args.agent_core_dir, output_dir=args.output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
