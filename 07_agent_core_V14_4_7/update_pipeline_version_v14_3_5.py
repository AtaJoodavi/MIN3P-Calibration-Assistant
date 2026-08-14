from __future__ import annotations

"""Update the V14 pipeline release label from V14.3.4 to V14.3.5.

Run from project root after replacing parameter_pair_optimizer.py:
    python .\update_pipeline_version_v14_3_5.py
"""

from pathlib import Path
import shutil

pipeline = Path("min3p_ai_pipeline_V14.py")
backup = Path("min3p_ai_pipeline_V14.pre_V14_3_5.py")
old = 'V14_VERSION = "V14.3.4"'
new = 'V14_VERSION = "V14.3.5"'
text = pipeline.read_text(encoding="utf-8")
count = text.count(old)
if count != 1:
    raise RuntimeError(f"Expected exactly one {old!r} in {pipeline}; found {count}. No file changed.")
if not backup.exists():
    shutil.copy2(pipeline, backup)
pipeline.write_text(text.replace(old, new, 1), encoding="utf-8")
print(f"PASS: updated {pipeline} to V14.3.5; backup: {backup}")
