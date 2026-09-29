from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone

from site_review.clock import FrozenClock
from site_review.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from site_review.service import SiteReviewService


POLYGON = {
    "type": "Polygon",
    "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
}

CHAIN = [
    {"sequence": 1, "role": "forester", "title": "林业会签"},
    {"sequence": 2, "role": "ecologist", "title": "生态会签"},
    {"sequence": 3, "role": "engineer", "title": "工程会签"},
]


class SiteReviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc))
        self.service = SiteReviewService(self.connection, self.clock)
        for user_id, role in (
            ("forester", "forester"),
            ("ecologist", "ecologist"),
            ("engineer", "engineer"),
            ("publisher", "publisher"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self._foundation()

    def tearDown(self) -> None:
        self.connection.close()

    def _foundation(self) -> None:
        self.service.publish_survey_protocol("ecologist", {
            "protocol_id": "bird-v1", "version": 1, "discipline": "ecologist",
            "title": "鸟类繁殖地调查",
        })
        self.service.publish_survey_protocol("engineer", {
            "protocol_id": "drain-v1", "version": 1, "discipline": "engineer",
            "title": "排水测算",
        })
        self.service.register_spatial_boundary(
            "ecologist", "bird-b", "ecologist", POLYGON, "EPSG:32649")
        self.service.register_spatial_boundary(
            "engineer", "drain-b", "engineer", POLYGON, "EPSG:32649")
        self.service.create_assessment_subject(
            "ecologist", "subject-bird", "BIRD-1", "鸟类繁殖地", "ecologist")
        self.service.create_assessment_subject(
            "engineer", "subject-drain", "DRAIN-1", "雨季排水", "engineer")
        self.service.register_evidence_batch(
            "ecologist", "bird-batch", "ecologist", "bird-v1", 1,
            "监测组", "2026-05-01T00:00:00Z")
        self.service.register_evidence_batch(
            "engineer", "drain-batch", "engineer", "drain-v1", 1,
            "水文组", "2026-06-01T00:00:00Z")
        imported = self.service.add_evidence_items("ecologist", "bird-batch", "k-bird-1", [
            {"source_ref": "P1", "evidence_type": "point_count", "payload": {"contacts": 2}},
            {"source_ref": "P2", "evidence_type": "nest", "payload": {"distance_m": 120}},
        ])
        self.bird_items = dict(zip(["P1", "P2"], imported["item_ids"]))
        imported = self.service.add_evidence_items("engineer", "drain-batch", "k-drain-1", [
            {"source_ref": "H1", "evidence_type": "runoff", "payload": {"runoff_mm": 90}},
        ])
        self.drain_items = dict(zip(["H1"], imported["item_ids"]))

    def _bird_draft(self) -> str:
        draft = self.service.create_conclusion_draft(
            "ecologist", "subject-bird", "ecologist", "bird-v1", 1, "bird-b",
            [{"batch_id": "bird-batch", "item_id": item_id} for item_id in self.bird_items.values()],
            CHAIN, {"verdict": "conditional"},
        )
        return draft["version_id"]

    def _drain_draft(self) -> str:
        draft = self.service.create_conclusion_draft(
            "engineer", "subject-drain", "engineer", "drain-v1", 1, "drain-b",
            [{"batch_id": "drain-batch", "item_id": item_id} for item_id in self.drain_items.values()],
            CHAIN, {"verdict": "pass"},
        )
        return draft["version_id"]

    def _sign_all(self, version_id: str) -> None:
        for role in ("forester", "ecologist", "engineer"):
            self.service.sign(role, version_id, f"{role} 意见")

    # ------------------------------------------------------------ 正常流程

    def test_full_workflow_and_publish(self) -> None:
        version_id = self._bird_draft()
        self._sign_all(version_id)
        published = self.service.publish("publisher", version_id)
        self.assertEqual(published["state"], "published")
        self.assertIsNotNone(published["publication"])
        self.assertEqual(len(published["signoffs"]), 3)
        self.assertTrue(published["integrity"]["ok"])
        current = self.service.current_conclusion("auditor", "subject-bird")
        self.assertEqual(current["executable"]["version_id"], version_id)
        self.assertIsNone(current["open_version"])

    def test_sequential_and_role_enforcement(self) -> None:
        version_id = self._bird_draft()
        with self.assertRaises(Forbidden):
            self.service.sign("ecologist", version_id)
        with self.assertRaises(Forbidden):
            self.service.sign("engineer", version_id)
        self.service.sign("forester", version_id)
        with self.assertRaises(Forbidden):
            self.service.sign("engineer", version_id)
        self.service.sign("ecologist", version_id)
        self.service.sign("engineer", version_id)
        self.service.publish("publisher", version_id)

    def test_repeated_sign_is_replay_and_publish_is_once(self) -> None:
        version_id = self._bird_draft()
        self._sign_all(version_id)
        replay = self.service.sign("ecologist", version_id, "再来一次")
        self.assertTrue(replay["replayed"])
        count = self.connection.execute(
            "SELECT count(*) FROM signoffs WHERE version_id=?", (version_id,)).fetchone()[0]
        self.assertEqual(count, 3)
        self.service.publish("publisher", version_id)
        with self.assertRaises(InvalidState):
            self.service.publish("publisher", version_id)
        replay_after = self.service.sign("forester", version_id, "发布后重放")
        self.assertTrue(replay_after["replayed"])
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM signoffs WHERE version_id=?",
                                    (version_id,)).fetchone()[0],
            3,
        )

    def test_publish_requires_all_signoffs_and_publisher_role(self) -> None:
        version_id = self._bird_draft()
        self.service.sign("forester", version_id)
        with self.assertRaises(InvalidState):
            self.service.publish("publisher", version_id)
        with self.assertRaises(Forbidden):
            self.service.publish("auditor", version_id)

    # ------------------------------------------------------- 冻结与完整性

    def test_draft_freezes_protocol_boundary_and_manifest(self) -> None:
        version_id = self._bird_draft()
        view = self.service.get_version("auditor", version_id)
        self.assertEqual(len(view["manifest"]), 2)
        self.assertEqual(len(view["manifest_sha256"]), 64)
        self.assertEqual(len(view["basis_hash"]), 64)
        self.assertEqual(view["protocol"], {"protocol_id": "bird-v1", "version": 1,
                                            "sha256": view["protocol"]["sha256"]})

    def test_cannot_freeze_other_discipline_evidence_or_boundary(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.create_conclusion_draft(
                "ecologist", "subject-bird", "ecologist", "bird-v1", 1, "bird-b",
                [{"batch_id": "drain-batch", "item_id": next(iter(self.drain_items.values()))}],
                CHAIN, {},
            )
        with self.assertRaises(ValidationFailed):
            self.service.create_conclusion_draft(
                "engineer", "subject-drain", "engineer", "drain-v1", 1, "bird-b",
                [{"batch_id": "drain-batch", "item_id": next(iter(self.drain_items.values()))}],
                CHAIN, {},
            )

    def test_evidence_protocol_mismatch_rejected(self) -> None:
        self.service.publish_survey_protocol("engineer", {
            "protocol_id": "drain-v1", "version": 2, "discipline": "engineer", "title": "排水测算修订",
        })
        with self.assertRaises(ValidationFailed):
            self.service.create_conclusion_draft(
                "engineer", "subject-drain", "engineer", "drain-v1", 2, "drain-b",
                [{"batch_id": "drain-batch", "item_id": next(iter(self.drain_items.values()))}],
                CHAIN, {},
            )

    # --------------------------------------------------- 补充/撤回/失效联动

    def test_supplement_blocks_signing_version(self) -> None:
        version_id = self._drain_draft()
        self.service.sign("forester", version_id)
        result = self.service.add_evidence_items("engineer", "drain-batch", "k-drain-2", [
            {"source_ref": "H2", "evidence_type": "runoff", "payload": {"runoff_mm": 130}},
        ])
        self.assertEqual(result["blocked_versions"], [version_id])
        view = self.service.get_version("auditor", version_id)
        self.assertEqual(view["state"], "blocked")
        self.assertIn("补充了证据", view["blocked_reason"])
        self.assertEqual({s["status"] for s in view["signoffs"]}, {"invalidated"})
        self.assertFalse(view["integrity"]["ok"])
        with self.assertRaises(InvalidState):
            self.service.sign("engineer", version_id)
        with self.assertRaises(InvalidState):
            self.service.publish("publisher", version_id)

    def test_idempotent_supplement_replay_keeps_blocking_result(self) -> None:
        version_id = self._drain_draft()
        payload = [{"source_ref": "H2", "evidence_type": "runoff", "payload": {"runoff_mm": 130}}]
        first = self.service.add_evidence_items("engineer", "drain-batch", "k-drain-2", payload)
        second = self.service.add_evidence_items("engineer", "drain-batch", "k-drain-2", payload)
        self.assertEqual(first, second)
        with self.assertRaises(Conflict):
            self.service.add_evidence_items(
                "engineer", "drain-batch", "k-drain-2",
                [{"source_ref": "H2", "evidence_type": "runoff", "payload": {"runoff_mm": 1}}],
            )

    def test_withdraw_batch_blocks_and_cascades_items(self) -> None:
        version_id = self._drain_draft()
        self.service.sign("forester", version_id)
        result = self.service.withdraw_evidence_batch("engineer", "drain-batch", "批次仪器校准过期")
        self.assertEqual(result["blocked_versions"], [version_id])
        self.assertEqual(result["state"], "withdrawn")
        with self.assertRaises(InvalidState):
            self.service.add_evidence_items("engineer", "drain-batch", "k-x", [
                {"source_ref": "H9", "evidence_type": "runoff", "payload": {}}])
        with self.assertRaises(InvalidState):
            self.service.withdraw_evidence_batch("engineer", "drain-batch", "再次撤回")

    def test_invalidate_item_blocks_unpublished_only(self) -> None:
        bird = self._bird_draft()
        self._sign_all(bird)
        self.service.publish("publisher", bird)
        drain = self._drain_draft()
        result = self.service.invalidate_evidence_item(
            "ecologist", self.bird_items["P2"], "巢位坐标错误")
        self.assertEqual(result["blocked_versions"], [])
        published = self.service.get_version("auditor", bird)
        self.assertEqual(published["state"], "published")
        self.assertTrue(all(s["status"] == "valid" for s in published["signoffs"]))
        # 失效生态专业证据不能由其他专业操作。
        with self.assertRaises(Forbidden):
            self.service.invalidate_evidence_item(
                "engineer", self.bird_items["P1"], "越权失效")
        with self.assertRaises(InvalidState):
            self.service.invalidate_evidence_item(
                "ecologist", self.bird_items["P2"], "重复失效")
        self.assertIsNotNone(drain)

    def test_blocked_version_remains_immutable_history_and_new_version_takes_over(self) -> None:
        v1 = self._drain_draft()
        self.service.sign("forester", v1)
        self.service.add_evidence_items("engineer", "drain-batch", "k-drain-2", [
            {"source_ref": "H2", "evidence_type": "runoff", "payload": {"runoff_mm": 130}},
        ])
        # 阻断后允许另立新草案；同一对象仍只允许一份在签草案。
        all_refs = [
            {"batch_id": "drain-batch", "item_id": row[0]}
            for row in self.connection.execute(
                "SELECT item_id FROM evidence_items WHERE batch_id='drain-batch' ORDER BY item_id")
        ]
        v2 = self.service.create_conclusion_draft(
            "engineer", "subject-drain", "engineer", "drain-v1", 1, "drain-b",
            all_refs, CHAIN, {"verdict": "conditional"},
        )["version_id"]
        self.assertEqual(v2, "subject-drain:v2")
        with self.assertRaises(Conflict):
            self.service.create_conclusion_draft(
                "engineer", "subject-drain", "engineer", "drain-v1", 1, "drain-b",
                all_refs, CHAIN, {},
            )
        self._sign_all(v2)
        self.service.publish("publisher", v2)
        versions = self.service.list_versions("auditor", "subject-drain")["versions"]
        self.assertEqual([(v["version_no"], v["state"]) for v in versions],
                         [(1, "blocked"), (2, "published")])
        trail = self.service.audit_trail("auditor", version_id=v1)
        kinds = {event["event_type"] for event in trail["events"]}
        self.assertIn("conclusion.blocked", kinds)
        self.assertIn("signoff.invalidated", kinds)
        self.assertIn("conclusion.drafted", kinds)

    def test_integrity_failure_at_sign_time_blocks(self) -> None:
        version_id = self._drain_draft()
        self.service.sign("forester", version_id)
        # 工程方签署时证据已被同专业其他人失效：签署动作原子阻断并失败。
        self.service.invalidate_evidence_item(
            "engineer", next(iter(self.drain_items.values())), "数据异常")
        with self.assertRaises(InvalidState):
            self.service.sign("engineer", version_id)
        self.assertEqual(self.service.get_version("auditor", version_id)["state"], "blocked")

    # ------------------------------------------------------------- 回避关系

    def test_conflict_before_sign_blocks_signer(self) -> None:
        version_id = self._bird_draft()
        self.service.sign("forester", version_id)
        self.service.declare_conflict(
            "forester", version_id, "ecologist", "生态人员参与过该地块外业")
        with self.assertRaises(Forbidden):
            self.service.sign("ecologist", version_id)

    def test_conflict_after_sign_blocks_version_and_invalidates_signoff(self) -> None:
        version_id = self._bird_draft()
        self._sign_all(version_id)
        result = self.service.declare_conflict(
            "ecologist", version_id, "ecologist", "事后发现利害关系")
        self.assertTrue(result["blocked"])
        view = self.service.get_version("auditor", version_id)
        self.assertEqual(view["state"], "blocked")
        self.assertTrue(any(s["signer_id"] == "ecologist" and s["status"] == "invalidated"
                            for s in view["signoffs"]))

    def test_publish_rejects_when_signer_has_conflict(self) -> None:
        version_id = self._drain_draft()
        self._sign_all(version_id)
        self.connection.execute(
            "INSERT INTO conflicts_of_interest(version_id,user_id,reason,declared_by,declared_at) "
            "VALUES(?,?,?,?,?)",
            (version_id, "engineer", "旁路登记", "engineer", "2026-09-29T09:00:00Z"),
        )
        with self.assertRaises(InvalidState):
            self.service.publish("publisher", version_id)

    # ----------------------------------------------------------------- 其他

    def test_discipline_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.publish_survey_protocol("forester", {
                "protocol_id": "x", "version": 1, "discipline": "ecologist", "title": "x"})
        with self.assertRaises(Forbidden):
            self.service.add_evidence_items("forester", "bird-batch", "k", [
                {"source_ref": "X", "evidence_type": "t", "payload": {}}])
        with self.assertRaises(Forbidden):
            self.service.audit_trail("publisher", subject_id="subject-bird")

    def test_missing_entities_raise_not_found(self) -> None:
        with self.assertRaises(NotFound):
            self.service.get_version("auditor", "nope:v1")
        with self.assertRaises(NotFound):
            self.service.register_evidence_batch(
                "engineer", "b-x", "engineer", "missing", 1, "g", "2026-01-01T00:00:00Z")

    def test_subject_audit_includes_evidence_change_sources(self) -> None:
        v1 = self._drain_draft()
        self.service.sign("forester", v1)
        self.service.add_evidence_items("engineer", "drain-batch", "k-drain-2", [
            {"source_ref": "H2", "evidence_type": "runoff", "payload": {"runoff_mm": 130}},
        ])
        trail = self.service.audit_trail("auditor", subject_id="subject-drain")
        kinds = [event["event_type"] for event in trail["events"]]
        self.assertIn("evidence_items.imported", kinds)
        self.assertIn("conclusion.blocked", kinds)
        self.assertIn("signoff.invalidated", kinds)

    def test_chain_validation(self) -> None:
        refs = [{"batch_id": "bird-batch", "item_id": next(iter(self.bird_items.values()))}]
        bad_chain = [{"sequence": 1, "role": "forester", "title": "林业"},
                     {"sequence": 3, "role": "engineer", "title": "工程"}]
        with self.assertRaises(ValidationFailed):
            self.service.create_conclusion_draft(
                "ecologist", "subject-bird", "ecologist", "bird-v1", 1, "bird-b",
                refs, bad_chain, {})
        dup_chain = [{"sequence": 1, "role": "forester", "title": "林业甲"},
                     {"sequence": 2, "role": "forester", "title": "林业乙"},
                     {"sequence": 3, "role": "engineer", "title": "工程"}]
        with self.assertRaises(ValidationFailed):
            self.service.create_conclusion_draft(
                "ecologist", "subject-bird", "ecologist", "bird-v1", 1, "bird-b",
                refs, dup_chain, {})


if __name__ == "__main__":
    unittest.main()
