from __future__ import annotations

"""CLI for deterministic V15.4 calibration-campaign analysis."""

import argparse
import json
from pathlib import Path

from modules.campaign_review_V15_4 import CampaignAnalysisV15_4, CampaignReviewPaths


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate deterministic V15.4 campaign-analysis JSON and Excel files.")
    parser.add_argument("--agent-core-dir", default=str(Path.cwd()), help="Path to 07_agent_core_V14.")
    parser.add_argument("--output-dir", default=None, help="Default: ../05_reports/V15_scientific_report.")
    args = parser.parse_args()
    paths = CampaignReviewPaths.from_agent_core(args.agent_core_dir, output_dir=args.output_dir)
    analyzer = CampaignAnalysisV15_4(paths)
    analysis = analyzer.build()
    result = {"analysis": analysis, "artifacts": analyzer.write(analysis)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
