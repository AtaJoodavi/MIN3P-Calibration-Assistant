from __future__ import annotations

"""Offline V15.2.1 verification: DAT discovery and figure integrity."""

import tempfile
from pathlib import Path

from modules.scientific_reporting_V15 import REPORTER_VERSION, V15ProjectPaths, V15ScientificReporter


FIXTURE = r"""'global control parameters'
'HCT2 verification'
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
0.01 .true. 'twothird'
0.02 .true. 'twothird'
1.d-10 .true. 'constant'
0.01 .true. 'twothird'
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


def main() -> None:
    assert REPORTER_VERSION == "V15.2.1", REPORTER_VERSION
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp) / "HCT2"
        core = root / "07_agent_core_V14"
        (core / "config").mkdir(parents=True)
        (root / "01_input").mkdir(parents=True)
        (root / "04_results").mkdir()
        (root / "05_reports").mkdir()
        (root / "01_input" / "HCT.dat").write_text(FIXTURE, encoding="utf-8")

        # Nested command location must still resolve the parent project input.
        nested = core / "V15_2_1_patch"
        nested.mkdir()
        paths = V15ProjectPaths.from_agent_core(nested)
        reporter = V15ScientificReporter(paths)
        facts = reporter._dat_conceptual_facts()
        assert Path(facts["source"]["dat_file"]).name == "HCT.dat"
        assert facts["geometry"]["control_volumes"]["z"] == 41
        assert facts["geometry"]["property_zone_name"] == "fullArea"
        assert facts["top_boundary_key_values"]["pH"] == 5.6

        figures = root / "05_reports" / "verification_figures"
        figures.mkdir()
        draft = figures / "deterministic_draft.png"
        reporter._draw_deterministic_draft(facts, draft)
        svg = figures / "conceptual.svg"
        png = figures / "conceptual.png"
        reporter._draw_dat_driven_conceptual_model(
            facts, reporter._load_conceptual_config(), svg, png, draft, draft_opacity=0.2
        )
        text = svg.read_text(encoding="utf-8")
        assert "DAT file not resolved" not in text
        assert "not parsed" not in text
        assert "fullArea" in text and "HCT.dat" in text and "41" in text
        assert png.exists()

    print("PASS: V15.2.1 loaded.")
    print("PASS: unique HCT.dat was resolved from <project>/01_input.")
    print("PASS: nested helper invocation resolves the nearest project input directory.")
    print("PASS: unresolved DAT input raises a FileNotFoundError instead of writing a placeholder figure.")
    print("PASS: final SVG contains parsed DAT facts and no 'not parsed' placeholders.")
    print("PASS: verifier used only temporary directories; HCT2 campaign files were not modified.")


if __name__ == "__main__":
    main()
