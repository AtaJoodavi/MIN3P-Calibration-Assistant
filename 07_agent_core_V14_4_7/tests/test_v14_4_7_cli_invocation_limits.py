from __future__ import annotations

import sys
import unittest
from unittest.mock import MagicMock, patch

import min3p_ai_pipeline_V14 as pipeline
from modules.adaptive_coordinate_optimizer_V14 import AdaptiveCoordinateOptimizerV14


class TestV1447CliInvocationLimits(unittest.TestCase):
    def _run_main(self, argv: list[str]):
        workflow = MagicMock()
        with patch.object(sys, "argv", ["min3p_ai_pipeline_V14.py", *argv]), patch.object(
            pipeline, "V14Workflow", return_value=workflow
        ):
            pipeline.main()
        return workflow

    def test_max_runs_remains_candidate_alias(self):
        workflow = self._run_main(["--mode", "auto", "--max-runs", "20"])
        kwargs = workflow.auto_v14.call_args.kwargs
        self.assertEqual(kwargs["max_runs"], 20)
        self.assertIsNone(kwargs["max_candidates"])
        self.assertIsNone(kwargs["max_physical_runs"])

    def test_max_candidates_is_explicit_candidate_limit(self):
        workflow = self._run_main(["--mode", "auto", "--max-candidates", "20"])
        kwargs = workflow.auto_v14.call_args.kwargs
        self.assertIsNone(kwargs["max_runs"])
        self.assertEqual(kwargs["max_candidates"], 20)
        self.assertIsNone(kwargs["max_physical_runs"])

    def test_max_physical_runs_does_not_inject_candidate_limit(self):
        workflow = self._run_main(["--mode", "auto", "--max-physical-runs", "20"])
        kwargs = workflow.auto_v14.call_args.kwargs
        self.assertIsNone(kwargs["max_runs"])
        self.assertIsNone(kwargs["max_candidates"])
        self.assertEqual(kwargs["max_physical_runs"], 20)

    def test_auto_default_retains_ten_candidate_cycles(self):
        workflow = self._run_main(["--mode", "auto"])
        kwargs = workflow.auto_v14.call_args.kwargs
        self.assertIsNone(kwargs["max_runs"])
        self.assertEqual(kwargs["max_candidates"], 10)
        self.assertIsNone(kwargs["max_physical_runs"])

    def test_alias_and_explicit_candidate_limit_are_rejected(self):
        with patch.object(
            sys,
            "argv",
            [
                "min3p_ai_pipeline_V14.py",
                "--mode",
                "auto",
                "--max-runs",
                "20",
                "--max-candidates",
                "20",
            ],
        ), patch.object(pipeline, "V14Workflow"):
            with self.assertRaises(SystemExit) as caught:
                pipeline.main()
        self.assertEqual(caught.exception.code, 2)

    def test_optimizer_invocation_summary_preserves_search_state(self):
        optimizer = AdaptiveCoordinateOptimizerV14.__new__(AdaptiveCoordinateOptimizerV14)
        existing = {"current_best_score": 8.4, "convergence_status": "running"}
        written = {}
        optimizer._read_state = MagicMock(return_value=existing.copy())
        optimizer._write_state = MagicMock(side_effect=lambda state: written.update(state))

        result = optimizer.record_invocation_summary(
            {
                "last_invocation_stop_reason": "max_physical_runs_reached",
                "last_invocation_physical_runs": 20,
                "last_invocation_cache_hits": 5,
            }
        )

        self.assertEqual(result["current_best_score"], 8.4)
        self.assertEqual(result["convergence_status"], "running")
        self.assertEqual(result["last_invocation_stop_reason"], "max_physical_runs_reached")
        self.assertEqual(written["last_invocation_physical_runs"], 20)
        self.assertEqual(written["last_invocation_cache_hits"], 5)


if __name__ == "__main__":
    unittest.main()
