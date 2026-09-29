from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from site_review.clock import FrozenClock
from site_review.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from site_review.jsonio import load_json
from site_review.service import SiteReviewService


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures" / "site_review"

POLYGON = {
    "type": "Polygon",
    "coordinates": [[[118.72, 27.94], [118.73, 27.94], [118.73, 27.93], [118.72, 27.93], [118.72, 27.94]]],
}
BOUNDARY = {
    "boundary_id": "zone-a",
    "label": "候选范围甲",
    "source_text": "勘界记录 2026-08",
    "geometry": POLYGON,
}


class SiteReviewServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 20, 8, 0, tzinfo=timezone.utc))
        self.service = SiteReviewService(self.connection, self.clock)
        for user_id, role in (
            ("coord", "coordinator"),
            ("forester", "forester"),
            ("ecologist", "ecologist"),
            ("engineer", "engineer"),
            ("engineer_b", "engineer"),
            ("auditor", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.protocols = {
            "forestry": load_json(FIXTURES / "forestry_protocol.json"),
            "ecology": load_json(FIXTURES / "ecology_protocol.json"),
            "engineering": load_json(FIXTURES / "engineering_protocol.json"),
        }
        self.service.register_protocol("forester", self.protocols["forestry"])
        self.service.register_protocol("ecologist", self.protocols["ecology"])
        self.service.register_protocol("engineer", self.protocols["engineering"])
        self.manifests = load_json(FIXTURES / "demo_conclusion.json")["manifests"]
        self.service.register_batch(
            "forester", "b-forest", "forest-understory-v1", 1, self.manifests["forestry"], "首轮"
        )
        self.service.register_batch(
            "ecologist", "b-eco", "bird-breeding-v1", 1, self.manifests["ecology"], "首轮"
        )
        self.service.register_batch(
            "engineer", "b-eng", "rain-drainage-v1", 1, self.manifests["engineering"], "首轮"
        )
        self.selection = [
            {"domain": "forestry", "batch_id": "b-forest"},
            {"domain": "ecology", "batch_id": "b-eco"},
            {"domain": "engineering", "batch_id": "b-eng"},
        ]

    def tearDown(self) -> None:
        self.connection.close()

    def _create(self, subject: str = "subject-a", version_title: str = "综合结论") -> dict:
        return self.service.create_conclusion_version(
            "coord", subject, version_title, BOUNDARY, self.selection
        )

    def _sign_all(self, version_no: int) -> None:
        self.service.sign("forester", "subject-a", version_no, "林业依据充分")
        self.service.sign("ecologist", "subject-a", version_no, "生态依据充分")
        self.service.sign("engineer", "subject-a", version_no, "工程依据充分")

    # --------------------------------------------------------------- 冻结

    def test_freeze_records_protocols_manifests_and_boundary(self) -> None:
        version = self._create()
        self.assertEqual(version["state"], "draft")
        self.assertEqual(len(version["freeze_sha256"]), 64)
        refs = {item["step"]: item for item in version["evidence_refs"]}
        self.assertEqual(set(refs), {"forestry", "ecology", "engineering"})
        self.assertEqual(refs["forestry"]["batch_revision"], 1)
        self.assertEqual(refs["forestry"]["protocol_id"], "forest-understory-v1")
        self.assertEqual(version["next_step"], "forestry")
        stored = self.connection.execute(
            "SELECT json_array_length(freeze_json) FROM conclusion_versions"
        ).fetchone()
        self.assertIsNotNone(stored)

    def test_only_coordinator_creates_version_and_domains_must_cover_all(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.create_conclusion_version(
                "forester", "subject-x", "标题", BOUNDARY, self.selection
            )
        with self.assertRaises(ValidationFailed):
            self.service.create_conclusion_version(
                "coord", "subject-x", "标题", BOUNDARY, self.selection[:2]
            )

    def test_protocol_and_batch_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.register_protocol("forester", self.protocols["ecology"])
        with self.assertRaises(Forbidden):
            self.service.register_batch(
                "ecologist", "b-other", "forest-understory-v1", 1, self.manifests["forestry"], "x"
            )

    def test_invalid_boundary_geometry_rejected(self) -> None:
        bad = dict(BOUNDARY)
        bad["geometry"] = {"type": "Point", "coordinates": [0, 0]}
        with self.assertRaises(ValidationFailed):
            self.service.create_conclusion_version("coord", "subject-b", "标题", bad, self.selection)

    def test_one_open_draft_per_subject(self) -> None:
        self._create()
        with self.assertRaises(Conflict):
            self._create()

    # --------------------------------------------------------------- 会签顺序

    def test_signing_must_follow_role_chain(self) -> None:
        self._create()
        with self.assertRaises(InvalidState):
            self.service.sign("ecologist", "subject-a", 1, "生态越序")
        self.service.sign("forester", "subject-a", 1, "林业依据")
        with self.assertRaises(Forbidden):
            self.service.sign("coord", "subject-a", 1, "协调人无签署职责")
        self.service.sign("ecologist", "subject-a", 1, "生态依据")
        self.service.sign("engineer", "subject-a", 1, "工程依据")
        version = self.service.get_version("subject-a", 1)
        self.assertIsNone(version["next_step"])
        orders = [item["sign_order"] for item in version["signatures"]]
        self.assertEqual(orders, [1, 2, 3])
        # 签名链：每一步都指向前一步签名摘要。
        self.assertIsNone(version["signatures"][0]["prev_signature_sha256"])
        self.assertEqual(
            version["signatures"][1]["prev_signature_sha256"],
            version["signatures"][0]["signature_sha256"],
        )

    def test_duplicate_signature_does_not_create_second_decision_record(self) -> None:
        self._create()
        self.service.sign("forester", "subject-a", 1, "林业依据")
        with self.assertRaises(Conflict):
            self.service.sign("forester", "subject-a", 1, "林业依据再次签署")
        count = self.connection.execute(
            "SELECT count(*) FROM signatures WHERE subject_id='subject-a' AND version_no=1"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_recused_person_cannot_sign(self) -> None:
        self._create()
        self.service.declare_recusal("engineer", "subject-a", 1, "engineer", "利益关联")
        with self.assertRaises(Forbidden):
            self.service.sign("engineer", "subject-a", 1, "试图签署")

    # --------------------------------------------------------------- 证据级联

    def test_evidence_supplement_blocks_draft_and_invalidates_signatures(self) -> None:
        self._create()
        self.service.sign("forester", "subject-a", 1, "林业依据")
        self.service.sign("ecologist", "subject-a", 1, "生态依据")
        result = self.service.revise_batch(
            "engineer", "b-eng", self.manifests["engineering"] * 2, "supplement", "新增断面"
        )
        self.assertEqual(result["revision"], 2)
        self.assertEqual(result["affected_versions"], [{"subject_id": "subject-a", "version_no": 1}])
        version = self.service.get_version("subject-a", 1)
        self.assertEqual(version["state"], "blocked")
        self.assertIn("新增断面", version["blocked_reason"])
        self.assertEqual(version["valid_signature_count"], 0)
        self.assertTrue(all(item["status"] == "invalidated" for item in version["signatures"]))
        with self.assertRaises(InvalidState):
            self.service.sign("engineer", "subject-a", 1, "阻断后签署")

    def test_revision_kinds_each_append_and_block(self) -> None:
        self._create()
        self.service.revise_batch("engineer", "b-eng", self.manifests["engineering"][:1], "invalidation", "一条失效")
        self.assertEqual(self.service.get_version("subject-a", 1)["state"], "blocked")
        # 撤回允许把清单替换为另一份非空清单；已阻断版本保持阻断并刷新原因。
        self.service.revise_batch(
            "engineer", "b-eng", self.manifests["engineering"][1:], "revocation", "撤回首条"
        )
        batch = self.service.get_batch("b-eng")
        self.assertEqual(batch["revision"], 3)
        self.assertEqual([item["revision"] for item in batch["revisions"]], [1, 2, 3])
        with self.assertRaises(ValidationFailed):
            self.service.revise_batch("engineer", "b-eng", [], "supplement", "空清单不允许")
        with self.assertRaises(ValidationFailed):
            self.service.revise_batch("engineer", "b-eng", self.manifests["engineering"], "unknown", "x")

    def test_evidence_change_after_publish_never_rewrites_archive(self) -> None:
        self._create()
        self._sign_all(1)
        publication = self.service.publish("coord", "subject-a", 1)
        freeze = self.service.get_version("subject-a", 1)["freeze_sha256"]
        self.service.revise_batch(
            "engineer_b", "b-eng", self.manifests["engineering"][:1], "revocation", "发布后撤回"
        )
        archived = self.service.get_version("subject-a", 1)
        self.assertEqual(archived["state"], "published")
        self.assertEqual(archived["freeze_sha256"], freeze)
        self.assertEqual(archived["valid_signature_count"], 3)
        self.assertEqual(archived["publication"]["decision_no"], publication["decision_no"])
        # 冻结引用仍指向 r1。
        self.assertEqual(
            archived["evidence_refs"][2]["batch_revision"], 1
        )

    # --------------------------------------------------------------- 回避与补签

    def test_recusal_after_signature_breaks_chain_and_replacement_can_sign(self) -> None:
        self._create()
        self._sign_all(1)
        result = self.service.declare_recusal(
            "engineer", "subject-a", 1, "engineer", "配偶在施工单位"
        )
        self.assertEqual(len(result["invalidated_signatures"]), 1)
        version = self.service.get_version("subject-a", 1)
        self.assertEqual(version["valid_signature_count"], 2)
        # 同版本由同角色的另一名工程负责人补签，无需新建版本。
        self.service.sign("engineer_b", "subject-a", 1, "独立复核后同意")
        self.assertEqual(self.service.get_version("subject-a", 1)["valid_signature_count"], 3)
        with self.assertRaises(Conflict):
            self.service.declare_recusal("coord", "subject-a", 1, "engineer", "重复申报")

    def test_recusal_of_earlier_step_invalidates_downstream_signatures(self) -> None:
        self._create()
        self._sign_all(1)
        result = self.service.declare_recusal("forester", "subject-a", 1, "forester", "林业回避")
        self.assertEqual(len(result["invalidated_signatures"]), 3)
        self.assertEqual(self.service.get_version("subject-a", 1)["valid_signature_count"], 0)

    # --------------------------------------------------------------- 原子发布

    def test_publish_requires_complete_chain_and_is_atomic(self) -> None:
        self._create()
        self.service.sign("forester", "subject-a", 1, "林业依据")
        with self.assertRaises(InvalidState):
            self.service.publish("coord", "subject-a", 1)
        self.assertIsNone(
            self.connection.execute(
                "SELECT * FROM publications WHERE subject_id='subject-a'"
            ).fetchone()
        )
        state = self.connection.execute(
            "SELECT state FROM conclusion_versions WHERE subject_id='subject-a' AND version_no=1"
        ).fetchone()[0]
        self.assertEqual(state, "draft")
        rejected = [
            row["event_type"]
            for row in self.connection.execute(
                "SELECT event_type FROM audit_events WHERE event_type='publication.rejected'"
            ).fetchall()
        ]
        self.assertEqual(rejected, ["publication.rejected"])

    def test_publish_rejects_tampered_signature_and_boundary(self) -> None:
        self._create()
        self._sign_all(1)
        self.connection.execute(
            "UPDATE signatures SET basis_summary='被改写的依据' WHERE subject_id='subject-a' "
            "AND version_no=1 AND step='ecology'"
        )
        with self.assertRaises(InvalidState):
            self.service.publish("coord", "subject-a", 1)
        self.connection.execute(
            "UPDATE signatures SET basis_summary='生态依据充分' WHERE subject_id='subject-a' "
            "AND version_no=1 AND step='ecology'"
        )
        self.connection.execute(
            "UPDATE conclusion_versions SET boundary_geometry_json=? "
            "WHERE subject_id='subject-a' AND version_no=1",
            (json.dumps(POLYGON).replace("27.94", "27.99", 1),),
        )
        with self.assertRaises(InvalidState):
            self.service.publish("coord", "subject-a", 1)

    def test_publish_is_idempotent_and_supersedes_previous(self) -> None:
        self._create()
        self._sign_all(1)
        first = self.service.publish("coord", "subject-a", 1)
        second = self.service.publish("coord", "subject-a", 1)
        self.assertEqual(second["decision_no"], first["decision_no"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM publications").fetchone()[0], 1
        )
        # 证据修订后创建 v2 并发布：v1 转为 superseded，当前结论指向 v2。
        self.service.revise_batch(
            "engineer", "b-eng", self.manifests["engineering"] * 2, "supplement", "新增断面"
        )
        self.service.create_conclusion_version("coord", "subject-a", "综合结论 v2", BOUNDARY, self.selection)
        self.service.sign("forester", "subject-a", 2, "林业依据")
        self.service.sign("ecologist", "subject-a", 2, "生态依据")
        self.service.sign("engineer", "subject-a", 2, "工程依据")
        v2 = self.service.publish("coord", "subject-a", 2)
        self.assertNotEqual(v2["decision_no"], first["decision_no"])
        self.assertEqual(self.service.get_version("subject-a", 1)["state"], "superseded")
        current = self.service.current_conclusion("auditor", "subject-a")
        self.assertEqual(current["decision_no"], v2["decision_no"])

    def test_only_coordinator_publishes(self) -> None:
        self._create()
        self._sign_all(1)
        with self.assertRaises(Forbidden):
            self.service.publish("auditor", "subject-a", 1)

    def test_current_conclusion_404_before_publish(self) -> None:
        self._create()
        with self.assertRaises(NotFound):
            self.service.current_conclusion("auditor", "subject-a")

    # --------------------------------------------------------------- 审计

    def test_audit_trail_permissions_and_content(self) -> None:
        self._create()
        self._sign_all(1)
        self.service.publish("coord", "subject-a", 1)
        with self.assertRaises(Forbidden):
            self.service.audit_trail("forester", "subject-a")
        trail = self.service.audit_trail("auditor", "subject-a")
        types = [event["event_type"] for event in trail["events"]]
        self.assertEqual(types[0], "conclusion.version_created")
        self.assertIn("conclusion.published", types)
        published = next(event for event in trail["events"] if event["event_type"] == "conclusion.published")
        self.assertIn("decision_no", published["payload"])
        batch_trail = self.service.batch_audit_trail("auditor", "b-eng")
        self.assertEqual(batch_trail["events"][0]["event_type"], "evidence_batch.registered")


if __name__ == "__main__":
    unittest.main()
