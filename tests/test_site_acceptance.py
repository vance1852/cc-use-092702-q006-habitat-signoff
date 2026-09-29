from __future__ import annotations

import unittest
from pathlib import Path

from site_review.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class SiteReviewAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["published"]["bird"], "site-bird:v1")
        self.assertEqual(result["published"]["drain"], "site-drain:v2")
        self.assertEqual(result["blocked_versions"], ["site-drain:v1"])
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertEqual(result["schema"]["schema_version"], "1")
        self.assertGreaterEqual(result["audit_event_total"], 10)


if __name__ == "__main__":
    unittest.main()
