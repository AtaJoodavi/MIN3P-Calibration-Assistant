from __future__ import annotations

import unittest

from gpt_advisory_dry_run import VALID_ADVICE, pipeline_advisory_checks, selector_precedence_checks
from modules.gpt_response_validator import validate_gpt_response
from modules.gpt_scientific_supervisor import GPTScientificSupervisorV14


class V1436GPTAdvisoryTests(unittest.TestCase):
    def test_supervisor_release_version(self):
        self.assertEqual(GPTScientificSupervisorV14.VERSION, "V14.3.6")

    def test_valid_mock_advisory_and_disabled_yaml_path(self):
        result = pipeline_advisory_checks()
        self.assertTrue(result["mock_valid_advisory_logged"])
        self.assertTrue(result["disabled_yaml_configuration_logged_without_api_call"])

    def test_deterministic_rules_override_illegal_gpt_advice(self):
        result = selector_precedence_checks()
        self.assertTrue(result["frozen_suggestion_overridden"])
        self.assertTrue(result["smaller_step_suggestion_overridden"])
        self.assertTrue(result["unfinished_direction_pair_overridden"])

    def test_boolean_confidence_is_rejected(self):
        payload = dict(VALID_ADVICE)
        payload["confidence"] = True
        with self.assertRaises(ValueError):
            validate_gpt_response(payload)


if __name__ == "__main__":
    unittest.main()
