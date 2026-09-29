from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from decimal import Decimal

from site_review.api import JsonApplication
from site_review.clock import FrozenClock
from site_review.jsonio import load_json
from site_review.service import SiteReviewService


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "site_review"


def dumps(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        default=lambda item: format(item, "f") if isinstance(item, Decimal) else item,
    )


class SiteReviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
        self.service = SiteReviewService(self.connection, self.clock)
        self.app = JsonApplication(self.service)
        for user_id, role in (
            ("coord", "coordinator"),
            ("forester", "forester"),
            ("ecologist", "ecologist"),
            ("engineer", "engineer"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.demo = load_json(FIXTURES / "demo_conclusion.json")
        protocols = {
            "forestry": load_json(FIXTURES / "forestry_protocol.json"),
            "ecology": load_json(FIXTURES / "ecology_protocol.json"),
            "engineering": load_json(FIXTURES / "engineering_protocol.json"),
        }
        self.app.handle("POST", "/evidence_protocols", {"X-Actor-Id": "forester"},
                        dumps(protocols["forestry"]).encode())
        self.app.handle("POST", "/evidence_protocols", {"X-Actor-Id": "ecologist"},
                        dumps(protocols["ecology"]).encode())
        self.app.handle("POST", "/evidence_protocols", {"X-Actor-Id": "engineer"},
                        dumps(protocols["engineering"]).encode())
        manifests = self.demo["manifests"]
        for actor, batch_id, protocol_id, manifest in (
            ("forester", "b-forest", "forest-understory-v1", manifests["forestry"]),
            ("ecologist", "b-eco", "bird-breeding-v1", manifests["ecology"]),
            ("engineer", "b-eng", "rain-drainage-v1", manifests["engineering"]),
        ):
            response = self.app.handle(
                "POST", "/evidence_batches", {"X-Actor-Id": actor},
                dumps({"batch_id": batch_id, "protocol_id": protocol_id, "protocol_version": 1,
                            "manifest": manifest, "note": "首轮"}).encode(),
            )
            self.assertEqual(response.status, 201, response.body)

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_full_publish_flow_over_http(self) -> None:
        payload = {
            "subject_id": "north-ridge",
            "title": "北脊平台",
            "boundary": self.demo["boundary"],
            "evidence_batches": [
                {"domain": "forestry", "batch_id": "b-forest"},
                {"domain": "ecology", "batch_id": "b-eco"},
                {"domain": "engineering", "batch_id": "b-eng"},
            ],
        }
        response = self.app.handle(
            "POST", "/conclusions", {"X-Actor-Id": "coord"},
            dumps(payload).encode(),
        )
        self.assertEqual(response.status, 201, response.body)
        self.assertEqual(response.body["version_no"], 1)

        for actor, basis in (
            ("forester", "林业可接受"), ("ecologist", "生态有条件同意"), ("engineer", "工程可行"),
        ):
            response = self.app.handle(
                "POST", "/subjects/north-ridge/versions/1/sign", {"X-Actor-Id": actor},
                dumps({"basis_summary": basis}).encode(),
            )
            self.assertEqual(response.status, 200, response.body)

        response = self.app.handle(
            "POST", "/subjects/north-ridge/versions/1/publish", {"X-Actor-Id": "coord"}, b"{}"
        )
        self.assertEqual(response.status, 200, response.body)
        self.assertTrue(response.body["decision_no"].startswith("D-north-ridge-v1-"))

        response = self.app.handle(
            "GET", "/subjects/north-ridge/current", {"X-Actor-Id": "auditor"}, b""
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["record"]["signatures"][0]["step"], "forestry")

        response = self.app.handle(
            "GET", "/subjects/north-ridge/versions", {"X-Actor-Id": "auditor"}, b""
        )
        self.assertEqual(response.status, 200)
        self.assertEqual([item["state"] for item in response.body["versions"]], ["published"])

    def test_missing_actor_header(self) -> None:
        response = self.app.handle("GET", "/subjects/north-ridge/versions", {}, b"")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_publish_rejection_is_audited_via_http(self) -> None:
        payload = {
            "subject_id": "north-ridge", "title": "北脊平台",
            "boundary": self.demo["boundary"],
            "evidence_batches": [
                {"domain": "forestry", "batch_id": "b-forest"},
                {"domain": "ecology", "batch_id": "b-eco"},
                {"domain": "engineering", "batch_id": "b-eng"},
            ],
        }
        self.app.handle("POST", "/conclusions", {"X-Actor-Id": "coord"},
                        dumps(payload).encode())
        response = self.app.handle(
            "POST", "/subjects/north-ridge/versions/1/publish", {"X-Actor-Id": "coord"}, b"{}"
        )
        self.assertEqual(response.status, 409)
        audit = self.app.handle("GET", "/subjects/north-ridge/audit", {"X-Actor-Id": "auditor"}, b"")
        self.assertEqual(audit.status, 200)
        self.assertEqual(audit.body["events"][-1]["event_type"], "publication.rejected")


if __name__ == "__main__":
    unittest.main()
