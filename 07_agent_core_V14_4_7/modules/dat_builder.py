from __future__ import annotations

import re
from pathlib import Path
from typing import List, Tuple

import pandas as pd

from .config import ProjectPaths
from .config_reader import ConfigReader
from .io_utils import fail_if_missing, log


def replace_all_parameters(text: str, parameters_df: pd.DataFrame) -> Tuple[str, List[Tuple[str, str]], List[str]]:
    replaced: List[Tuple[str, str]] = []
    missing: List[str] = []
    for _, row in parameters_df.iterrows():
        name = str(row.get("parameter", "")).strip()
        value = str(row.get("value", "")).strip()
        if not name or name.lower() == "nan":
            continue
        placeholder = "{{" + name + "}}"
        if placeholder in text:
            text = text.replace(placeholder, value)
            replaced.append((placeholder, value))
        else:
            missing.append(placeholder)
    return text, replaced, missing


def find_unreplaced_placeholders(text: str) -> List[Tuple[int, str]]:
    rows = []
    for i, line in enumerate(text.splitlines(), start=1):
        if line.strip().startswith("!"):
            continue
        if "{{" in line or "}}" in line:
            rows.append((i, line.strip()))
    return rows


class DatBuilder:
    def __init__(self, paths: ProjectPaths, config: ConfigReader):
        self.paths = paths
        self.config = config

    def build(self) -> Path:
        log(self.paths, "V9 STEP 1 - Build MIN3P DAT from template")
        template = self.config.template_file()
        output = self.config.generated_dat_file()
        fail_if_missing(template, "Template DAT file")
        text = template.read_text(encoding="utf-8", errors="ignore")
        text, replaced, missing = replace_all_parameters(text, self.config.parameters())
        unreplaced = find_unreplaced_placeholders(text)
        if unreplaced:
            details = "\n".join(f"Line {n}: {s}" for n, s in unreplaced[:20])
            raise ValueError("Unreplaced placeholders remain in generated DAT:\n" + details)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8", newline="")
        log(self.paths, f"Generated DAT: {output}")
        log(self.paths, f"Replaced placeholders: {len(replaced)}; config-only placeholders: {len(missing)}")
        return output


def extract_placeholders(template_file: Path) -> List[str]:
    if not template_file.exists():
        return []
    text = template_file.read_text(encoding="utf-8", errors="ignore")
    out = []
    for line in text.splitlines():
        if line.strip().startswith("!"):
            continue
        out.extend(re.findall(r"\{\{(.*?)\}\}", line))
    return sorted(set(out))
