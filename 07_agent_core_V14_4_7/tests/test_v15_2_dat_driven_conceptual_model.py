from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from modules.scientific_reporting_V15 import V15ProjectPaths, V15ScientificReporter, _parse_min3p_dat


class V152DatDrivenConceptualModelTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()) / "HCT2"
        self.core = self.root / "07_agent_core_V14"
        self.input = self.root / "01_input"
        self.results = self.root / "04_results"
        self.core.mkdir(parents=True)
        self.input.mkdir(parents=True)
        self.results.mkdir(parents=True)
        fixture = Path(__file__).with_name("HCT_one_layer_fixture.dat")
        shutil.copy2(fixture, self.input / "HCT.dat")

    def tearDown(self):
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def test_parser_detects_single_property_zone_and_vertical_grid(self):
        facts = _parse_min3p_dat(self.input / "HCT.dat")
        self.assertEqual(facts["geometry"]["property_zone_count"], 1)
        self.assertTrue(facts["geometry"]["one_reactive_property_zone"])
        self.assertEqual(facts["geometry"]["control_volumes"]["z"], 41)
        self.assertEqual(facts["geometry"]["numerical_dimension"], "1D vertical")
        self.assertEqual(facts["top_boundary_key_values"]["pH"], 5.6)
        self.assertIn("pyrite-par", facts["geochemistry"]["active_phase_groups"]["sulfides"])

    def test_report_uses_dat_and_writes_audit_without_gpt(self):
        paths = V15ProjectPaths.from_agent_core(self.core)
        reporter = V15ScientificReporter(paths, conceptual_image_mode="deterministic")
        result = reporter.generate()
        audit = Path(result.conceptual_facts)
        self.assertTrue(audit.exists())
        facts = json.loads(audit.read_text(encoding="utf-8"))
        self.assertEqual(Path(facts["source"]["dat_file"]).name, "HCT.dat")
        self.assertTrue(Path(result.conceptual_png).exists())
        self.assertTrue(Path(result.conceptual_svg).exists())
        generation = json.loads(Path(result.conceptual_generation_manifest).read_text(encoding="utf-8"))
        self.assertFalse(generation["gpt_used"])
        self.assertEqual(generation["generation_status"], "deterministic_requested")


if __name__ == "__main__":
    unittest.main()
