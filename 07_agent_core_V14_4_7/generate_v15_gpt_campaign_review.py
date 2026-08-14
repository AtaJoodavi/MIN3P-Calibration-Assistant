from __future__ import annotations

"""CLI for V15.4 GPT/deterministic calibration-campaign review."""

import argparse
import json
from pathlib import Path

from modules.campaign_review_V15_4 import (
    CampaignAnalysisV15_4,
    CampaignReviewPaths,
    GPTCampaignReviewerV15_4,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the V15.4 scientific campaign review.")
    parser.add_argument("--agent-core-dir", default=str(Path.cwd()), help="Path to 07_agent_core_V14.")
    parser.add_argument("--output-dir", default=None, help="Default: ../05_reports/V15_scientific_report.")
    parser.add_argument("--analysis-json", default=None, help="Optional pre-generated campaign_analysis_V15_4.json.")
    parser.add_argument("--config", default=None, help="Optional GPT campaign-review YAML configuration.")
    parser.add_argument("--mode", choices=["auto", "gpt", "deterministic", "off"], default="auto")
    args = parser.parse_args()

    paths = CampaignReviewPaths.from_agent_core(args.agent_core_dir, output_dir=args.output_dir, config_file=args.config)
    if args.analysis_json:
        analysis = json.loads(Path(args.analysis_json).read_text(encoding="utf-8"))
    else:
        analyzer = CampaignAnalysisV15_4(paths)
        analysis = analyzer.build()
        analyzer.write(analysis)
    reviewer = GPTCampaignReviewerV15_4(paths)
    review, metadata = reviewer.review(analysis, mode=args.mode)
    result = {"review_metadata": metadata, "review": review, "artifacts": reviewer.write(review, metadata)}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
