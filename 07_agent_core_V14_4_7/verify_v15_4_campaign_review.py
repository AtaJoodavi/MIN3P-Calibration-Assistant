from __future__ import annotations

"""Offline smoke verifier for the V15.4 campaign reviewer."""

import unittest

from tests.test_v15_4_campaign_review import V154CampaignReviewTests


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(V154CampaignReviewTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print("PASS: V15.4 deterministic analysis, GPT fallback, schema, and artifacts verified.")
