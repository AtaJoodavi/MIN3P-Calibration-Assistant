from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from modules.scientific_reporting_V15 import V15ProjectPaths, V15ScientificReporter


FIXTURE = r"""'global control parameters'
'HCT2 test'
.true.
'done'
'spatial discretization'
1 ;number of discretization intervals in x
1 ;number of control volumes in x
0 1 ;xmin,xmax
1 ;number of discretization intervals in y
1 ;number of control volumes in y
0 1 ;ymin,ymax
1 ;number of discretization intervals in z
41 ;number of control volumes in z
0 0.1 ;zmin,zmax
'done'
'geochemical system'
'components'
2
'o2(aq)'
'so4-2'
'non-aqueous components'
2
'=feoh(s)' 'surface'
'=feoh(w)' 'surface'
'gases'
2
'o2(g)'
'co2(g)'
'minerals'
4
'pyrite-par'
'calcite-ch'
'jarositek'
'magnesite'
'define sorption type'
'sorbed species'
1
'=feozn+(s)'
'redox couples'
1
'so4-2' 'hs-1'
'done'
'physical parameters - porous medium'
1 ;number of property zones
'number and name of zone'
1
'fullArea'
0.4 ;porosity
'extent of zone'
0 1 0 1 0 0.1
'end of zone'
'done'
'initial condition - reactive transport'
'mineral input'
0.01 .true. 'twothird' ;phi, min_equ, type - pyrite-par
0.02 .true. 'twothird' ;phi, min_equ, type - calcite-ch
1.d-10 .true. 'constant' ;phi, min_equ, type - jarositek
0.01 .true. 'twothird' ;phi, min_equ, type - magnesite
'extent of zone'
'done'
'boundary conditions - variably saturated flow'
'number and name of zone'
1
'Top'
'boundary type'
'second' 1e-07
'end of zone'
'number and name of zone'
2
'Bottom'
'boundary type'
'first' -0.28875
'end of zone'
'done'
'boundary conditions - reactive transport'
'number and name of zone'
1
'Top'
'boundary type'
'mixed'
'concentration input'
0.21 'po2'
5.6 'ph'
0.00039 'pco2'
'guess for ph'
7.0
'end of zone'
'number and name of zone'
2
'Bottom'
'boundary type'
'second'
'end of zone'
'done'
"""


class V1521DatResolutionAndFigureIntegrityTests(unittest.TestCase):
    def make_project(self, *, nested: bool = False, include_dat: bool = True) -> tuple[Path, Path]:
        root = Path(self.tmp.name) / "HCT2"
        core = root / "07_agent_core_V14"
        (core / "modules").mkdir(parents=True, exist_ok=True)
        (core / "config").mkdir(parents=True, exist_ok=True)
        (root / "04_results").mkdir(parents=True, exist_ok=True)
        (root / "05_reports").mkdir(parents=True, exist_ok=True)
        input_dir = root / "01_input"
        input_dir.mkdir(parents=True, exist_ok=True)
        if include_dat:
            (input_dir / "HCT.dat").write_text(FIXTURE, encoding="utf-8")
        selected = core / "V15_2_1_patch" if nested else core
        selected.mkdir(parents=True, exist_ok=True)
        return root, selected

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_resolves_unique_dat_from_project_input(self) -> None:
        root, core = self.make_project()
        paths = V15ProjectPaths.from_agent_core(core)
        reporter = V15ScientificReporter(paths)
        facts = reporter._dat_conceptual_facts()
        self.assertEqual(Path(facts["source"]["dat_file"]).name, "HCT.dat")
        self.assertTrue(facts["geometry"]["one_reactive_property_zone"])
        self.assertEqual(facts["geometry"]["control_volumes"]["z"], 41)
        self.assertEqual(facts["top_boundary_key_values"]["pH"], 5.6)

    def test_resolves_when_command_runs_from_nested_patch_directory(self) -> None:
        root, nested = self.make_project(nested=True)
        paths = V15ProjectPaths.from_agent_core(nested)
        reporter = V15ScientificReporter(paths)
        self.assertEqual(reporter._discover_dat_file().name, "HCT.dat")
        self.assertEqual(paths.project_dir, root)

    def test_no_dat_fails_before_any_placeholder_figure_can_be_created(self) -> None:
        _, core = self.make_project(include_dat=False)
        paths = V15ProjectPaths.from_agent_core(core)
        reporter = V15ScientificReporter(paths)
        with self.assertRaises(FileNotFoundError):
            reporter._dat_conceptual_facts()

    def test_final_svg_contains_parsed_facts_and_no_placeholder_text(self) -> None:
        root, core = self.make_project()
        paths = V15ProjectPaths.from_agent_core(core)
        reporter = V15ScientificReporter(paths)
        facts = reporter._dat_conceptual_facts()
        figures = root / "05_reports" / "test_figures"
        figures.mkdir(parents=True)
        draft = figures / "deterministic_draft.png"
        reporter._draw_deterministic_draft(facts, draft)
        svg = figures / "conceptual.svg"
        png = figures / "conceptual.png"
        reporter._draw_dat_driven_conceptual_model(
            facts, reporter._load_conceptual_config(), svg, png, draft, draft_opacity=0.2
        )
        payload = svg.read_text(encoding="utf-8")
        self.assertIn("fullArea", payload)
        self.assertIn("HCT.dat", payload)
        self.assertIn("41", payload)
        self.assertNotIn("DAT file not resolved", payload)
        self.assertNotIn("not parsed", payload)
        self.assertTrue(png.exists())


if __name__ == "__main__":
    unittest.main()
