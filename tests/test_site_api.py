from __future__ import annotations

import json
import sqlite3
import unittest

from site_review.api import JsonApplication
from site_review.service import SiteReviewService


POLYGON = {"type": "Polygon",
           "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]]}


class SiteReviewApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(SiteReviewService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, payload: dict, actor: str | None = None,
              headers: dict | None = None) -> tuple[int, dict]:
        merged_headers = {"Content-Type": "application/json"}
        if actor:
            merged_headers["X-Actor-Id"] = actor
        if headers:
            merged_headers.update(headers)
        response = self.app.handle(
            "POST", path, merged_headers, json.dumps(payload).encode("utf-8"))
        return response.status, response.body

    def _get(self, path: str, actor: str) -> tuple[int, dict]:
        response = self.app.handle("GET", path, {"X-Actor-Id": actor})
        return response.status, response.body

    def _build(self) -> str:
        for user_id, role in (
            ("f", "forester"), ("e", "ecologist"), ("g", "engineer"),
            ("p", "publisher"), ("a", "auditor"),
        ):
            self._post("/users", {"user_id": user_id, "display_name": user_id, "role": role})
        self._post("/survey_protocols", {
            "protocol_id": "pr", "version": 1, "discipline": "ecologist", "title": "协议"}, "e")
        self._post("/spatial_boundaries", {
            "boundary_id": "bd", "discipline": "ecologist", "geometry": POLYGON,
            "crs": "EPSG:32649"}, "e")
        self._post("/assessment_subjects", {
            "subject_id": "s1", "code": "S-1", "title": "鸟类", "discipline_scope": "ecologist"}, "e")
        self._post("/evidence_batches", {
            "batch_id": "b1", "discipline": "ecologist", "protocol_id": "pr",
            "protocol_version": 1, "collected_by": "组", "collected_at": "2026-05-01T00:00:00Z"}, "e")
        status, body = self._post("/evidence_batches/b1/items", {
            "evidence_items": [
                {"source_ref": "P1", "evidence_type": "point_count", "payload": {"contacts": 2}}]},
            "e", {"Idempotency-Key": "k1"})
        self.assertEqual(status, 200)
        item_id = body["item_ids"][0]
        chain = [
            {"sequence": 1, "role": "forester", "title": "林业会签"},
            {"sequence": 2, "role": "ecologist", "title": "生态会签"},
            {"sequence": 3, "role": "engineer", "title": "工程会签"},
        ]
        status, body = self._post("/conclusion_versions", {
            "subject_id": "s1", "discipline": "ecologist", "protocol_id": "pr",
            "protocol_version": 1, "boundary_id": "bd",
            "evidence_refs": [{"batch_id": "b1", "item_id": item_id}],
            "signoff_chain": chain, "summary": {"v": "conditional"}}, "e")
        self.assertEqual(status, 201)
        return body["version_id"]

    def test_health_and_errors(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")
        response = self.app.handle("GET", "/nope")
        self.assertEqual(response.status, 404)

    def test_full_http_flow(self) -> None:
        version_id = self._build()
        for actor in ("f", "e", "g"):
            status, body = self._post(f"/conclusion_versions/{version_id}/sign",
                                      {"note": "同意"}, actor)
            self.assertEqual(status, 200, body)
        status, body = self._post(f"/conclusion_versions/{version_id}/publish", {}, "p")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "published")

        status, body = self._get(f"/conclusion_versions/{version_id}", "a")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["signoffs"]), 3)

        status, body = self._get("/assessment_subjects/s1/current", "p")
        self.assertEqual(status, 200)
        self.assertEqual(body["executable"]["version_id"], version_id)

        status, body = self._get("/assessment_subjects/s1/versions", "a")
        self.assertEqual(status, 200)
        self.assertEqual(len(body["versions"]), 1)

        status, body = self._get(f"/audit_events?version_id={version_id}", "a")
        self.assertEqual(status, 200)
        kinds = [event["event_type"] for event in body["events"]]
        self.assertIn("conclusion.published", kinds)

    def test_http_blocks_on_supplement(self) -> None:
        version_id = self._build()
        self._post(f"/conclusion_versions/{version_id}/sign", {"note": "林业同意"}, "f")
        status, body = self._post("/evidence_batches/b1/items", {
            "evidence_items": [
                {"source_ref": "P2", "evidence_type": "nest", "payload": {"distance_m": 50}}]},
            "e", {"Idempotency-Key": "k2"})
        self.assertEqual(status, 200)
        self.assertEqual(body["blocked_versions"], [version_id])
        status, body = self._get(f"/conclusion_versions/{version_id}", "a")
        self.assertEqual(body["state"], "blocked")

    def test_actor_header_required(self) -> None:
        status, body = self._get("/assessment_subjects/s1/versions", "a")
        # 用户尚未创建，应返回 404 而不是崩溃。
        self.assertEqual(status, 404)
        response = self.app.handle("GET", "/assessment_subjects/s1/current")
        self.assertEqual(response.status, 422)


if __name__ == "__main__":
    unittest.main()
