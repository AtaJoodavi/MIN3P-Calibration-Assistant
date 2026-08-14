from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from modules.scientific_reporting_V15 import REPORTER_VERSION, V15ProjectPaths, V15ScientificReporter, _parse_min3p_dat


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print("PASS:", message)


def main() -> None:
    require(REPORTER_VERSION == "V15.2", "V15.2 DAT-driven reporter loaded.")
    root = Path(tempfile.mkdtemp()) / "HCT2"
    try:
        core = root / "07_agent_core_V14"
        input_dir = root / "01_input"
        results = root / "04_results"
        core.mkdir(parents=True)
        input_dir.mkdir(parents=True)
        results.mkdir(parents=True)
        fixture = Path(__file__).parent / "tests" / "HCT_one_layer_fixture.dat"
        shutil.copy2(fixture, input_dir / "HCT.dat")
        facts = _parse_min3p_dat(input_dir / "HCT.dat")
        require(facts["geometry"]["one_reactive_property_zone"], "parser detected one reactive property zone from DAT.")
        require(facts["geometry"]["control_volumes"]["z"] == 41, "parser detected 41 vertical control volumes from DAT.")
        report = V15ScientificReporter(V15ProjectPaths.from_agent_core(core), conceptual_image_mode="deterministic").generate()
        manifest = json.loads(Path(report.conceptual_generation_manifest).read_text(encoding="utf-8"))
        require(manifest["dat_file"].endswith("HCT.dat"), "report discovers HCT.dat under 01_input.")
        require(not manifest["gpt_used"], "deterministic verification made no GPT image API call.")
        require(Path(report.conceptual_png).exists() and Path(report.conceptual_svg).exists(), "final PNG and SVG with Python overlay were written.")
        require(Path(report.conceptual_facts).exists() and Path(report.conceptual_prompt).exists(), "facts JSON and GPT prompt audit files were written.")
        require(report.protected_hashes_unchanged, "protected V14 campaign files remained unchanged.")
    finally:
        shutil.rmtree(root.parent, ignore_errors=True)


if __name__ == "__main__":
    main()
