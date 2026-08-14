from __future__ import annotations

"""Optional advisory GPT supervisor using the Responses API and Structured Outputs.

No GPT call can modify model files. The pipeline logs failed or unavailable
advisory calls and continues deterministic operation.
"""

import json
import os
from pathlib import Path
from typing import Any

from modules.gpt_prompt_builder import build_system_prompt, build_user_input
from modules.gpt_response_validator import supervisor_schema, validate_gpt_response
from modules.v14_utils import as_bool, read_simple_yaml


class GPTScientificSupervisorV14:
    VERSION = "V14.3.6"

    def __init__(self, paths, config):
        self.paths = paths
        self.config = config
        self.config_file = Path(paths.agent_core_dir) / "config" / "gpt_supervisor_config.yaml"
        self.settings = self._settings()

    @staticmethod
    def _bool_setting(data: dict[str, Any], key: str, default: bool) -> bool:
        value = data.get(key)
        return default if value is None else bool(as_bool(value))

    @staticmethod
    def _int_setting(data: dict[str, Any], key: str, default: int, minimum: int = 1) -> int:
        try:
            value = int(data.get(key, default) or default)
        except (TypeError, ValueError):
            value = default
        return max(value, minimum)

    def _settings(self) -> dict[str, Any]:
        data = read_simple_yaml(self.config_file)
        return {
            "enabled": self._bool_setting(data, "enabled", False),
            "model": str(data.get("model", "gpt-5.5")),
            "reasoning_effort": str(data.get("reasoning_effort", "low")),
            "max_output_tokens": self._int_setting(data, "max_output_tokens", 1200),
            # These were present in YAML but were not previously propagated
            # into the runtime supervisor settings.
            "valid_run_interval": self._int_setting(data, "valid_run_interval", 4),
            "call_on_new_best": self._bool_setting(data, "call_on_new_best", True),
            "call_on_repeated_failures": self._bool_setting(data, "call_on_repeated_failures", True),
            "call_on_group_transition": self._bool_setting(data, "call_on_group_transition", True),
            "call_on_numerical_warning": self._bool_setting(data, "call_on_numerical_warning", True),
            "call_on_possible_stop": self._bool_setting(data, "call_on_possible_stop", True),
        }

    def review_evidence(self, *, evidence: dict[str, Any]) -> dict[str, Any]:
        if not self.settings["enabled"]:
            raise RuntimeError("GPT Scientific Supervisor is disabled in config/gpt_supervisor_config.yaml")
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is not set; no GPT advisory call was made.")
        try:
            from openai import OpenAI
        except Exception as exc:
            raise RuntimeError("The OpenAI Python package is required for V14 GPT supervision.") from exc

        client = OpenAI()
        response = client.responses.create(
            model=self.settings["model"],
            instructions=build_system_prompt(),
            input=build_user_input(evidence),
            store=False,
            reasoning={"effort": self.settings["reasoning_effort"]},
            max_output_tokens=self.settings["max_output_tokens"],
            text={
                "format": {
                    "type": "json_schema",
                    "name": "min3p_v14_scientific_supervisor",
                    "strict": True,
                    "schema": supervisor_schema(),
                },
                "verbosity": "low",
            },
        )
        raw = getattr(response, "output_text", "")
        if not raw:
            raise RuntimeError("Responses API returned no output_text.")
        return validate_gpt_response(json.loads(raw))

    supervise = review_evidence
