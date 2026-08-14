from __future__ import annotations

"""Read-only command-line entry point for V15.4.2 scientific reporting."""

import argparse
import json
from pathlib import Path

from modules.scientific_reporting_V15 import generate_v15_scientific_report


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "V15.4.2 DAT-driven scientific reporting, explainability, and campaign review for a frozen V14 campaign. "
            "This command does not run MIN3P or modify V14 calibration state."
        )
    )
    parser.add_argument(
        "--agent-core-dir",
        default=str(Path.cwd()),
        help="Path to 07_agent_core_V14; default is the current directory.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Optional report directory; default is ../05_reports/V15_scientific_report.",
    )
    parser.add_argument(
        "--conceptual-config",
        default=None,
        help="Optional JSON conceptual-model configuration file.",
    )
    parser.add_argument(
        "--dat-file",
        default=None,
        help="Optional MIN3P DAT file. If omitted, the reporter selects the main non-template *.dat file from ../01_input.",
    )
    parser.add_argument(
        "--conceptual-image-mode",
        choices=["deterministic", "gpt", "auto"],
        default="deterministic",
        help="Use deterministic draft (default), GPT draft, or auto GPT fallback. GPT requires enabled config and OPENAI_API_KEY.",
    )

    parser.add_argument(
        "--conceptual-detail",
        choices=["paper", "technical"],
        default="paper",
        help="Render the conceptual model in paper mode (process-focused) or technical mode (DAT-inventory-focused).",
    )
    parser.add_argument(
        "--campaign-review-mode",
        choices=["auto", "gpt", "deterministic", "off"],
        default="auto",
        help="Generate the V15.4.2 campaign review with GPT, deterministic fallback, or disable it.",
    )
    parser.add_argument(
        "--campaign-review-config",
        default=None,
        help="Optional YAML config. Default: config/gpt_campaign_review_V15_4.yaml.",
    )
    args = parser.parse_args()
    result = generate_v15_scientific_report(
        args.agent_core_dir,
        output_dir=args.output_dir,
        conceptual_config_file=args.conceptual_config,
        dat_file=args.dat_file,
        conceptual_image_mode=args.conceptual_image_mode,
        conceptual_detail=args.conceptual_detail,
        campaign_review_mode=args.campaign_review_mode,
        campaign_review_config=args.campaign_review_config,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
