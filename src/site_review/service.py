"""选址评估版本化结论草案与会签的领域用例。"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable, Mapping

from .clock import SystemClock, isoformat
from .contracts import SignoffStep, SpatialBoundary, SurveyProtocol, ValidationError
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


DISCIPLINE_ROLES = ("forester", "ecologist", "engineer")
ROLE_LABELS = {
    "forester": "林业人员",
    "ecologist": "生态人员",
    "engineer": "工程人员",
    "publisher": "发布人员",
    "auditor": "审计人员",
}

ROLE_PERMISSIONS = {
    "forester": {
        "protocol.publish", "boundary.write", "subject.create", "evidence.register",
        "draft.write", "signoff.write", "conflict.declare", "conclusion.read",
    },
    "ecologist": {
        "protocol.publish", "boundary.write", "subject.create", "evidence.register",
        "draft.write", "signoff.write", "conflict.declare", "conclusion.read",
    },
    "engineer": {
        "protocol.publish", "boundary.write", "subject.create", "evidence.register",
        "draft.write", "signoff.write", "conflict.declare", "conclusion.read",
    },
    "publisher": {"subject.create", "conclusion.read", "conclusion.publish"},
    "auditor": {"audit.read", "conclusion.read"},
}


class SiteReviewService:
    """在单个 SQLite 连接上提供全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    def deactivate_user(self, actor_id: str, user_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE users SET active=0 WHERE user_id=? AND active=1", (user_id,)
            )
            if cursor.rowcount != 1:
                raise InvalidState("用户不存在或已停用")
            self._audit("user", user_id, "user.deactivated", actor_id, {})
        return {"user_id": user_id, "active": False}

    # ----------------------------------------------------------- 协议与边界

    def publish_survey_protocol(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        user = self._require(actor_id, "protocol.publish")
        try:
            protocol = SurveyProtocol.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        if protocol.discipline != user["role"]:
            raise Forbidden("只能发布本专业的调查协议")
        text = canonical_json(raw)
        digest = content_digest([raw])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO survey_protocols(protocol_id,version,discipline,title,canonical_json,"
                    "content_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        protocol.protocol_id, protocol.version, protocol.discipline, protocol.title,
                        text, digest, actor_id, self._now(),
                    ),
                )
                self._audit(
                    "survey_protocol",
                    f"{protocol.protocol_id}@{protocol.version}",
                    "protocol.published",
                    actor_id,
                    {"discipline": protocol.discipline, "sha256": digest},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("协议版本或内容摘要已经存在") from exc
        return {"protocol_id": protocol.protocol_id, "version": protocol.version, "sha256": digest}

    def register_spatial_boundary(
        self, actor_id: str, boundary_id: str, discipline: str, geometry: Mapping[str, Any], crs: str
    ) -> dict[str, Any]:
        self._require(actor_id, "boundary.write")
        if discipline not in DISCIPLINE_ROLES:
            raise ValidationFailed("空间边界专业必须是 forester/ecologist/engineer")
        try:
            boundary = SpatialBoundary.from_dict({"geometry": geometry, "crs": crs})
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        frozen = {"geometry": boundary.geometry, "crs": boundary.crs}
        digest = content_digest([frozen])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO spatial_boundaries(boundary_id,discipline,geometry_json,crs,content_sha256,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (boundary_id, discipline, canonical_json(boundary.geometry), boundary.crs,
                     digest, actor_id, self._now()),
                )
                self._audit(
                    "spatial_boundary", boundary_id, "boundary.registered", actor_id,
                    {"discipline": discipline, "sha256": digest},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("边界编号或内容摘要冲突") from exc
        return {"boundary_id": boundary_id, "sha256": digest}

    def create_assessment_subject(
        self, actor_id: str, subject_id: str, code: str, title: str, discipline_scope: str
    ) -> dict[str, Any]:
        self._require(actor_id, "subject.create")
        if not subject_id.strip() or not code.strip() or not title.strip():
            raise ValidationFailed("评估对象编号、代码和标题不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO assessment_subjects(subject_id,code,title,discipline_scope,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (subject_id.strip(), code.strip(), title.strip(), discipline_scope, actor_id, self._now()),
                )
                self._audit(
                    "assessment_subject", subject_id.strip(), "subject.created", actor_id,
                    {"code": code.strip(), "title": title.strip()},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("评估对象编号或代码冲突") from exc
        return {"subject_id": subject_id.strip(), "code": code.strip(), "title": title.strip()}

    # --------------------------------------------------------------- 证据

    def register_evidence_batch(
        self,
        actor_id: str,
        batch_id: str,
        discipline: str,
        protocol_id: str,
        protocol_version: int,
        collected_by: str,
        collected_at: str,
        note: str | None = None,
    ) -> dict[str, Any]:
        user = self._require(actor_id, "evidence.register")
        if discipline != user["role"]:
            raise Forbidden("各专业只能维护本专业的证据批次")
        if discipline not in DISCIPLINE_ROLES:
            raise ValidationFailed("证据专业必须是 forester/ecologist/engineer")
        protocol = self.connection.execute(
            "SELECT discipline FROM survey_protocols WHERE protocol_id=? AND version=?",
            (protocol_id, protocol_version),
        ).fetchone()
        if protocol is None:
            raise NotFound("调查协议版本不存在")
        if protocol["discipline"] != discipline:
            raise ValidationFailed("证据批次专业与调查协议专业不一致")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO evidence_batches(batch_id,discipline,protocol_id,protocol_version,state,"
                    "collected_by,collected_at,note,created_by,created_at) VALUES(?,?,?,?, 'active', ?,?,?,?,?)",
                    (batch_id, discipline, protocol_id, protocol_version, collected_by,
                     collected_at, note, actor_id, self._now()),
                )
                self._audit(
                    "evidence_batch", batch_id, "evidence_batch.registered", actor_id,
                    {"discipline": discipline, "protocol": f"{protocol_id}@{protocol_version}"},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"证据批次已存在: {batch_id}") from exc
        return {"batch_id": batch_id, "state": "active"}

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM idempotency_keys WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def add_evidence_items(
        self,
        actor_id: str,
        batch_id: str,
        idempotency_key: str,
        items: Iterable[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """导入或补充证据条目；补充会使引用该批次的未发布草案失效。"""

        self._require(actor_id, "evidence.register")
        rows = tuple(items)
        if not rows:
            raise ValidationFailed("证据条目数组不能为空")
        request_digest = content_digest(list(rows))
        scope = f"evidence_items:{batch_id}"
        existing = self._idempotent_response(scope, idempotency_key, request_digest)
        if existing is not None:
            return existing
        batch = self.connection.execute(
            "SELECT * FROM evidence_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise NotFound("证据批次不存在")
        if batch["discipline"] != self._user(actor_id)["role"]:
            raise Forbidden("不能向其他专业的证据批次补充证据")
        if batch["state"] != "active":
            raise InvalidState("证据批次已撤回，不能再补充证据")
        parsed: list[dict[str, Any]] = []
        for index, raw in enumerate(rows):
            if not isinstance(raw, Mapping):
                raise ValidationFailed(f"evidence_items[{index}] 必须是对象")
            source_ref = str(raw.get("source_ref") or "").strip()
            evidence_type = str(raw.get("evidence_type") or "").strip()
            payload = raw.get("payload")
            if not source_ref or not evidence_type:
                raise ValidationFailed(f"evidence_items[{index}] 缺少 source_ref 或 evidence_type")
            if not isinstance(payload, Mapping):
                raise ValidationFailed(f"evidence_items[{index}].payload 必须是对象")
            parsed.append({"source_ref": source_ref, "evidence_type": evidence_type, "payload": dict(payload)})
        response = {"batch_id": batch_id, "inserted": len(parsed), "request_sha256": request_digest}
        blocked: list[str] = []
        inserted_ids: list[int] = []
        try:
            with transaction(self.connection, immediate=True):
                for item in parsed:
                    frozen = {
                        "source_ref": item["source_ref"],
                        "evidence_type": item["evidence_type"],
                        "payload": item["payload"],
                    }
                    cursor = self.connection.execute(
                        "INSERT INTO evidence_items(batch_id,source_ref,evidence_type,payload_json,"
                        "content_sha256,state,imported_by,imported_at) VALUES(?,?,?,?,?,'active',?,?)",
                        (
                            batch_id, item["source_ref"], item["evidence_type"],
                            canonical_json(item["payload"]), content_digest([frozen]),
                            actor_id, self._now(),
                        ),
                    )
                    inserted_ids.append(cursor.lastrowid)
                self._audit(
                    "evidence_batch", batch_id, "evidence_items.imported", actor_id, response,
                )
                # 补充证据：冻结清单不再覆盖批次现状，未发布会签全部失效。
                blocked = self._block_referencing_drafts(
                    batch_id=batch_id,
                    reason=f"证据批次 {batch_id} 补充了证据，冻结清单不再完整",
                    actor_id=actor_id,
                )
                response["blocked_versions"] = blocked
                response["item_ids"] = inserted_ids
                self.connection.execute(
                    "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (scope, idempotency_key, request_digest, canonical_json(response), self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("来源标识重复或幂等键并发冲突") from exc
        return response

    def withdraw_evidence_batch(self, actor_id: str, batch_id: str, reason: str) -> dict[str, Any]:
        user = self._require(actor_id, "evidence.register")
        batch = self.connection.execute(
            "SELECT discipline,state FROM evidence_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise NotFound("证据批次不存在")
        if batch["discipline"] != user["role"]:
            raise Forbidden("只能撤回本专业的证据批次")
        if batch["state"] != "active":
            raise InvalidState("证据批次不是生效状态")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE evidence_batches SET state='withdrawn',withdrawn_at=?,withdraw_reason=? "
                "WHERE batch_id=? AND state='active'",
                (self._now(), reason, batch_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("证据批次状态已变化")
            self.connection.execute(
                "UPDATE evidence_items SET state='withdrawn',invalidated_at=?,invalidate_reason=? "
                "WHERE batch_id=? AND state='active'",
                (self._now(), f"所属批次撤回：{reason}", batch_id),
            )
            self._audit(
                "evidence_batch", batch_id, "evidence_batch.withdrawn", actor_id, {"reason": reason},
            )
            blocked = self._block_referencing_drafts(
                batch_id=batch_id,
                reason=f"证据批次 {batch_id} 已撤回：{reason}",
                actor_id=actor_id,
            )
        return {"batch_id": batch_id, "state": "withdrawn", "blocked_versions": blocked}

    def invalidate_evidence_item(self, actor_id: str, item_id: int, reason: str) -> dict[str, Any]:
        user = self._require(actor_id, "evidence.register")
        item = self.connection.execute(
            "SELECT i.state,b.discipline AS discipline FROM evidence_items i "
            "JOIN evidence_batches b ON b.batch_id=i.batch_id WHERE i.item_id=?",
            (item_id,),
        ).fetchone()
        if item is None:
            raise NotFound("证据条目不存在")
        if item["discipline"] != user["role"]:
            raise Forbidden("只能失效本专业的证据条目")
        if item["state"] != "active":
            raise InvalidState("证据条目不是生效状态")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE evidence_items SET state='invalidated',invalidated_at=?,invalidate_reason=? "
                "WHERE item_id=? AND state='active'",
                (self._now(), reason, item_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("证据条目状态已变化")
            self._audit(
                "evidence_item", str(item_id), "evidence_item.invalidated", actor_id, {"reason": reason},
            )
            blocked = self._block_referencing_drafts(
                item_id=item_id,
                reason=f"证据条目 {item_id} 已失效：{reason}",
                actor_id=actor_id,
            )
        return {"item_id": item_id, "state": "invalidated", "blocked_versions": blocked}

    # ----------------------------------------------------- 草案、阻断、会签

    def _block_referencing_drafts(
        self,
        *,
        reason: str,
        actor_id: str,
        batch_id: str | None = None,
        item_id: int | None = None,
        version_id: str | None = None,
    ) -> list[str]:
        """在同一事务内阻断尚未发布的草案并作废其签署。已发布版本绝不改写。"""

        if version_id is not None:
            version_ids = [version_id]
        elif item_id is not None:
            version_ids = [
                row[0]
                for row in self.connection.execute(
                    "SELECT DISTINCT v.version_id FROM conclusion_versions v "
                    "JOIN conclusion_manifest_items m ON m.version_id=v.version_id "
                    "WHERE v.state='signing' AND m.item_id=?",
                    (item_id,),
                ).fetchall()
            ]
        else:
            version_ids = [
                row[0]
                for row in self.connection.execute(
                    "SELECT DISTINCT v.version_id FROM conclusion_versions v "
                    "JOIN conclusion_manifest_items m ON m.version_id=v.version_id "
                    "WHERE v.state='signing' AND m.batch_id=?",
                    (batch_id,),
                ).fetchall()
            ]
        now = self._now()
        for blocked_id in version_ids:
            signoff_rows = self.connection.execute(
                "SELECT signoff_id,signer_id,sequence FROM signoffs WHERE version_id=? AND status='valid'",
                (blocked_id,),
            ).fetchall()
            self.connection.execute(
                "UPDATE conclusion_versions SET state='blocked',blocked_reason=?,blocked_at=? "
                "WHERE version_id=? AND state='signing'",
                (reason, now, blocked_id),
            )
            self.connection.execute(
                "UPDATE signoffs SET status='invalidated',invalidated_at=?,invalidate_reason=? "
                "WHERE version_id=? AND status='valid'",
                (now, reason, blocked_id),
            )
            invalidated_ids = [row["signoff_id"] for row in signoff_rows]
            self._audit(
                "conclusion", blocked_id, "conclusion.blocked", actor_id,
                {"reason": reason, "invalidated_signoff_ids": invalidated_ids},
            )
            for row in signoff_rows:
                self._audit(
                    "conclusion", blocked_id, "signoff.invalidated", actor_id,
                    {"signoff_id": row["signoff_id"], "signer_id": row["signer_id"],
                     "sequence": row["sequence"], "reason": reason},
                )
        return version_ids

    def create_conclusion_draft(
        self,
        actor_id: str,
        subject_id: str,
        discipline: str,
        protocol_id: str,
        protocol_version: int,
        boundary_id: str,
        evidence_refs: Iterable[Mapping[str, Any]],
        signoff_chain: Iterable[Mapping[str, Any]],
        summary: Mapping[str, Any],
    ) -> dict[str, Any]:
        user = self._require(actor_id, "draft.write")
        if discipline not in DISCIPLINE_ROLES:
            raise ValidationFailed("结论专业必须是 forester/ecologist/engineer")
        if discipline != user["role"]:
            raise Forbidden("只能为本专业评估对象建立结论草案")
        if not isinstance(summary, Mapping):
            raise ValidationFailed("summary 必须是对象")
        subject = self.connection.execute(
            "SELECT * FROM assessment_subjects WHERE subject_id=?", (subject_id,)
        ).fetchone()
        if subject is None:
            raise NotFound("评估对象不存在")
        protocol = self.connection.execute(
            "SELECT content_sha256 FROM survey_protocols WHERE protocol_id=? AND version=? AND discipline=?",
            (protocol_id, protocol_version, discipline),
        ).fetchone()
        if protocol is None:
            raise NotFound("调查协议版本不存在或专业不匹配")
        boundary = self.connection.execute(
            "SELECT content_sha256,discipline FROM spatial_boundaries WHERE boundary_id=?", (boundary_id,)
        ).fetchone()
        if boundary is None:
            raise NotFound("空间边界不存在")
        if boundary["discipline"] != discipline:
            raise ValidationFailed("空间边界专业与结论专业不一致")

        refs = tuple(evidence_refs)
        if not refs:
            raise ValidationFailed("证据引用不能为空")
        frozen_items: list[dict[str, Any]] = []
        seen_pairs: set[tuple[str, int]] = set()
        for index, ref in enumerate(refs):
            if not isinstance(ref, Mapping):
                raise ValidationFailed(f"evidence_refs[{index}] 必须是对象")
            ref_batch = str(ref.get("batch_id") or "").strip()
            ref_item = ref.get("item_id")
            if not ref_batch or isinstance(ref_item, bool) or not isinstance(ref_item, int):
                raise ValidationFailed(f"evidence_refs[{index}] 必须包含 batch_id 和整数 item_id")
            if (ref_batch, ref_item) in seen_pairs:
                raise ValidationFailed(f"证据引用重复: {ref_batch}/{ref_item}")
            seen_pairs.add((ref_batch, ref_item))
            row = self.connection.execute(
                "SELECT i.item_id,i.content_sha256,i.state,b.state AS batch_state,"
                "b.protocol_id AS protocol_id,b.protocol_version AS protocol_version,b.discipline AS discipline "
                "FROM evidence_items i JOIN evidence_batches b ON b.batch_id=i.batch_id "
                "WHERE i.batch_id=? AND i.item_id=?",
                (ref_batch, ref_item),
            ).fetchone()
            if row is None:
                raise NotFound(f"证据条目不存在: {ref_batch}/{ref_item}")
            if row["discipline"] != discipline:
                raise ValidationFailed(f"证据 {ref_batch}/{ref_item} 不属于结论专业 {discipline}")
            if row["batch_state"] != "active" or row["state"] != "active":
                raise InvalidState(f"证据 {ref_batch}/{ref_item} 已撤回或失效，不能冻结进新草案")
            if row["protocol_id"] != protocol_id or row["protocol_version"] != protocol_version:
                raise ValidationFailed(f"证据 {ref_batch}/{ref_item} 所用协议版本与草案冻结协议不一致")
            frozen_items.append({
                "batch_id": ref_batch, "item_id": ref_item, "item_sha256": row["content_sha256"],
            })

        steps = sorted(
            (SignoffStep.from_dict(raw, index) for index, raw in enumerate(signoff_chain)),
            key=lambda step: step.sequence,
        )
        if not steps:
            raise ValidationFailed("会签链条不能为空")
        if [step.sequence for step in steps] != list(range(1, len(steps) + 1)):
            raise ValidationFailed("会签环节 sequence 必须是从 1 开始的连续不重复整数")
        if len({step.role for step in steps}) != len(steps):
            raise ValidationFailed("会签链条的专业角色不能重复")
        if any(step.role not in DISCIPLINE_ROLES for step in steps):
            raise ValidationFailed("会签角色必须是 forester/ecologist/engineer")

        frozen_items.sort(key=lambda item: (item["batch_id"], item["item_id"]))
        manifest = {
            "subject_id": subject_id,
            "discipline": discipline,
            "protocol": {"protocol_id": protocol_id, "version": protocol_version,
                         "sha256": protocol["content_sha256"]},
            "boundary": {"boundary_id": boundary_id, "sha256": boundary["content_sha256"]},
            "items": frozen_items,
        }
        manifest_sha256 = content_digest([manifest])
        basis_hash = content_digest([manifest_sha256, protocol["content_sha256"], boundary["content_sha256"]])

        with transaction(self.connection, immediate=True):
            last = self.connection.execute(
                "SELECT MAX(version_no) AS last_no,version_id FROM conclusion_versions WHERE subject_id=?",
                (subject_id,),
            ).fetchone()
            version_no = (last["last_no"] or 0) + 1
            version_id = f"{subject_id}:v{version_no}"
            try:
                self.connection.execute(
                    "INSERT INTO conclusion_versions(version_id,subject_id,version_no,discipline,state,"
                    "protocol_id,protocol_version,protocol_sha256,boundary_id,boundary_sha256,"
                    "manifest_json,manifest_sha256,basis_hash,summary_json,supersedes_version,"
                    "created_by,created_at) VALUES(?,?,?,?,'signing',?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        version_id, subject_id, version_no, discipline,
                        protocol_id, protocol_version, protocol["content_sha256"],
                        boundary_id, boundary["content_sha256"],
                        canonical_json(manifest), manifest_sha256, basis_hash,
                        canonical_json(summary), last["version_id"], actor_id, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该评估对象已有一份会签中的草案，请先完成或等待其阻断") from exc
            for item in frozen_items:
                self.connection.execute(
                    "INSERT INTO conclusion_manifest_items(version_id,batch_id,item_id,item_sha256,frozen_state) "
                    "VALUES(?,?,?,?,'active')",
                    (version_id, item["batch_id"], item["item_id"], item["item_sha256"]),
                )
            for step in steps:
                self.connection.execute(
                    "INSERT INTO signoff_steps(version_id,sequence,role,title) VALUES(?,?,?,?)",
                    (version_id, step.sequence, step.role, step.title),
                )
            self._audit(
                "conclusion", version_id, "conclusion.drafted", actor_id,
                {"subject_id": subject_id, "version_no": version_no,
                 "supersedes": last["version_id"], "manifest_sha256": manifest_sha256,
                 "basis_hash": basis_hash, "evidence_item_count": len(frozen_items),
                 "chain": [{"sequence": s.sequence, "role": s.role} for s in steps]},
            )
        return self.get_version(actor_id, version_id)

    def declare_conflict(self, actor_id: str, version_id: str, user_id: str, reason: str) -> dict[str, Any]:
        """登记回避关系；若当事人已签署，立即阻断草案并作废其签署。"""

        self._require(actor_id, "conflict.declare")
        version = self.connection.execute(
            "SELECT state FROM conclusion_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if version is None:
            raise NotFound("结论版本不存在")
        if version["state"] == "published":
            raise InvalidState("已发布版本不可变更回避关系")
        target = self._user(user_id)
        if not reason.strip():
            raise ValidationFailed("回避理由不能为空")
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO conflicts_of_interest(version_id,user_id,reason,declared_by,declared_at) "
                    "VALUES(?,?,?,?,?)",
                    (version_id, user_id, reason.strip(), actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该用户在此版本上已登记回避关系") from exc
            self._audit(
                "conclusion", version_id, "conflict.declared", actor_id,
                {"user_id": user_id, "role": target["role"], "reason": reason.strip()},
            )
            prior = self.connection.execute(
                "SELECT signoff_id FROM signoffs WHERE version_id=? AND signer_id=? AND status='valid'",
                (version_id, user_id),
            ).fetchone()
            blocked = []
            if prior is not None and version["state"] == "signing":
                blocked = self._block_referencing_drafts(
                    reason=f"签署人 {user_id} 存在回避关系：{reason.strip()}",
                    actor_id=actor_id,
                    version_id=version_id,
                )
        return {"version_id": version_id, "user_id": user_id, "blocked": bool(blocked)}

    def _integrity_issues(self, version_id: str) -> list[str]:
        """复核冻结协议、边界与证据清单的完整性。"""

        version = self.connection.execute(
            "SELECT * FROM conclusion_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        issues: list[str] = []
        protocol = self.connection.execute(
            "SELECT content_sha256 FROM survey_protocols WHERE protocol_id=? AND version=?",
            (version["protocol_id"], version["protocol_version"]),
        ).fetchone()
        if protocol is None:
            issues.append("冻结的调查协议版本不存在")
        elif protocol["content_sha256"] != version["protocol_sha256"]:
            issues.append("冻结的调查协议内容摘要发生变化")
        boundary = self.connection.execute(
            "SELECT content_sha256 FROM spatial_boundaries WHERE boundary_id=?",
            (version["boundary_id"],),
        ).fetchone()
        if boundary is None:
            issues.append("冻结的空间边界不存在")
        elif boundary["content_sha256"] != version["boundary_sha256"]:
            issues.append("冻结的空间边界内容摘要发生变化")
        manifest_rows = self.connection.execute(
            "SELECT batch_id,item_id,item_sha256 FROM conclusion_manifest_items WHERE version_id=? "
            "ORDER BY batch_id,item_id",
            (version_id,),
        ).fetchall()
        frozen_by_batch: dict[str, dict[int, str]] = {}
        for row in manifest_rows:
            frozen_by_batch.setdefault(row["batch_id"], {})[row["item_id"]] = row["item_sha256"]
        for batch_id, frozen in frozen_by_batch.items():
            batch = self.connection.execute(
                "SELECT state,protocol_id,protocol_version FROM evidence_batches WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                issues.append(f"证据批次 {batch_id} 不存在")
                continue
            if batch["state"] != "active":
                issues.append(f"证据批次 {batch_id} 已撤回")
            if batch["protocol_id"] != version["protocol_id"] or batch["protocol_version"] != version["protocol_version"]:
                issues.append(f"证据批次 {batch_id} 的协议版本与冻结协议不一致")
            active_now = {
                row["item_id"]: row["content_sha256"]
                for row in self.connection.execute(
                    "SELECT item_id,content_sha256 FROM evidence_items WHERE batch_id=? AND state='active'",
                    (batch_id,),
                ).fetchall()
            }
            for item_id, frozen_sha in frozen.items():
                current = self.connection.execute(
                    "SELECT state,content_sha256 FROM evidence_items WHERE item_id=?", (item_id,)
                ).fetchone()
                if current is None:
                    issues.append(f"证据条目 {item_id} 不存在")
                elif current["state"] != "active":
                    issues.append(f"证据条目 {item_id} 已撤回或失效")
                elif current["content_sha256"] != frozen_sha:
                    issues.append(f"证据条目 {item_id} 内容摘要与冻结清单不一致")
            supplemented = sorted(set(active_now) - set(frozen))
            if supplemented:
                issues.append(f"证据批次 {batch_id} 出现未冻结的补充证据: {supplemented}")
        return issues

    def sign(self, actor_id: str, version_id: str, note: str = "") -> dict[str, Any]:
        user = self._require(actor_id, "signoff.write")
        version = self.connection.execute(
            "SELECT state,discipline FROM conclusion_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if version is None:
            raise NotFound("结论版本不存在")
        integrity_blocked: list[str] | None = None
        with transaction(self.connection, immediate=True):
            # 重复签署不产生第二份决定：直接返回既有签署（含发布后的重放）。
            replay = self.connection.execute(
                "SELECT signoff_id,sequence,status FROM signoffs WHERE version_id=? AND signer_id=?",
                (version_id, actor_id),
            ).fetchone()
            if replay is not None:
                return {
                    "version_id": version_id, "signoff_id": replay["signoff_id"],
                    "sequence": replay["sequence"], "status": replay["status"], "replayed": True,
                }
            if version["state"] != "signing":
                raise InvalidState(f"版本处于 {version['state']} 状态，不能签署")
            steps = self.connection.execute(
                "SELECT * FROM signoff_steps WHERE version_id=? ORDER BY sequence", (version_id,)
            ).fetchall()
            signed = {
                row["sequence"]: row
                for row in self.connection.execute(
                    "SELECT * FROM signoffs WHERE version_id=? AND status='valid'", (version_id,)
                ).fetchall()
            }
            pending = next((step for step in steps if step["sequence"] not in signed), None)
            if pending is None:
                raise InvalidState("会签链条已经全部签署")
            if user["role"] != pending["role"]:
                raise Forbidden(
                    f"当前等待 {pending['role']}（{pending['title']}）签署，{user['role']} 不能越权签署"
                )
            for sequence in range(1, pending["sequence"]):
                if sequence not in signed:
                    raise InvalidState("必须按职责顺序依次会签")
            if self.connection.execute(
                "SELECT 1 FROM conflicts_of_interest WHERE version_id=? AND user_id=?",
                (version_id, actor_id),
            ).fetchone() is not None:
                raise Forbidden("存在登记在案的回避关系，不能签署本版本")
            issues = self._integrity_issues(version_id)
            if issues:
                # 阻断必须落库后再向调用方报错，否则异常会回滚整个事务。
                self._block_referencing_drafts(
                    reason="；".join(issues), actor_id=actor_id, version_id=version_id,
                )
                integrity_blocked = issues
            else:
                now = self._now()
                basis_hash = self.connection.execute(
                    "SELECT basis_hash FROM conclusion_versions WHERE version_id=?", (version_id,)
                ).fetchone()["basis_hash"]
                fingerprint = content_digest([
                    basis_hash, pending["sequence"], actor_id, user["role"], note, now,
                ])
                cursor = self.connection.execute(
                    "INSERT INTO signoffs(step_id,version_id,signer_id,signer_role,sequence,"
                    "signed_fingerprint,note,status,signed_at) VALUES(?,?,?,?,?,?,?,'valid',?)",
                    (
                        pending["step_id"], version_id, actor_id, user["role"], pending["sequence"],
                        fingerprint, note, now,
                    ),
                )
                signoff_id = cursor.lastrowid
                self._audit(
                    "conclusion", version_id, "conclusion.signed", actor_id,
                    {"signoff_id": signoff_id, "sequence": pending["sequence"], "role": user["role"],
                     "title": pending["title"], "note": note, "fingerprint": fingerprint,
                     "basis_hash": basis_hash},
                )
        if integrity_blocked is not None:
            raise InvalidState(f"证据完整性已破坏，版本被阻断: {integrity_blocked}")
        return self.get_version(actor_id, version_id)

    def publish(self, actor_id: str, version_id: str) -> dict[str, Any]:
        """原子确认全部会签、回避关系与证据完整性后发布归档。"""

        self._require(actor_id, "conclusion.publish")
        with transaction(self.connection, immediate=True):
            version = self.connection.execute(
                "SELECT * FROM conclusion_versions WHERE version_id=?", (version_id,)
            ).fetchone()
            if version is None:
                raise NotFound("结论版本不存在")
            if version["state"] == "published":
                raise InvalidState("版本已经发布，发布决定不可重复产生")
            if version["state"] != "signing":
                raise InvalidState(f"版本处于 {version['state']} 状态，不能发布")
            steps = self.connection.execute(
                "SELECT * FROM signoff_steps WHERE version_id=? ORDER BY sequence", (version_id,)
            ).fetchall()
            signoffs = self.connection.execute(
                "SELECT * FROM signoffs WHERE version_id=? AND status='valid' ORDER BY sequence",
                (version_id,),
            ).fetchall()
            if len(signoffs) != len(steps):
                raise InvalidState("会签未完成，不能发布")
            signer_ids: list[str] = []
            chain_basis: list[dict[str, Any]] = []
            for step, signoff in zip(steps, signoffs):
                if signoff["sequence"] != step["sequence"]:
                    raise InvalidState("会签顺序与职责链条不一致")
                if signoff["signer_role"] != step["role"]:
                    raise InvalidState(f"第 {step['sequence']} 环节签署角色不匹配")
                signer = self.connection.execute(
                    "SELECT active FROM users WHERE user_id=?", (signoff["signer_id"],)
                ).fetchone()
                if signer is None or not signer["active"]:
                    raise InvalidState(f"签署人 {signoff['signer_id']} 已不存在或停用")
                signer_ids.append(signoff["signer_id"])
                chain_basis.append({
                    "sequence": signoff["sequence"],
                    "signer_id": signoff["signer_id"],
                    "fingerprint": signoff["signed_fingerprint"],
                })
            if len(set(signer_ids)) != len(signer_ids):
                raise InvalidState("同一用户不能承担多个会签环节")
            conflict_rows = self.connection.execute(
                "SELECT user_id FROM conflicts_of_interest WHERE version_id=?", (version_id,)
            ).fetchall()
            conflicts = [row["user_id"] for row in conflict_rows]
            hit = sorted(set(conflicts) & set(signer_ids))
            if hit:
                raise InvalidState(f"签署人存在未解除的回避关系: {hit}")
            issues = self._integrity_issues(version_id)
            if issues:
                raise InvalidState(f"证据完整性校验失败，拒绝发布: {issues}")
            signoff_fingerprint = content_digest(chain_basis)
            now = self._now()
            self.connection.execute(
                "UPDATE conclusion_versions SET state='published',published_at=?,published_by=? "
                "WHERE version_id=? AND state='signing'",
                (now, actor_id, version_id),
            )
            self.connection.execute(
                "INSERT INTO conclusion_publications(version_id,manifest_sha256,basis_hash,"
                "signoff_fingerprint,published_by,published_at) VALUES(?,?,?,?,?,?)",
                (version_id, version["manifest_sha256"], version["basis_hash"],
                 signoff_fingerprint, actor_id, now),
            )
            self._audit(
                "conclusion", version_id, "conclusion.published", actor_id,
                {"manifest_sha256": version["manifest_sha256"], "basis_hash": version["basis_hash"],
                 "signoff_fingerprint": signoff_fingerprint, "signers": signer_ids},
            )
            publication = self.connection.execute(
                "SELECT * FROM conclusion_publications WHERE version_id=?", (version_id,)
            ).fetchone()
        return self.get_version(actor_id, version_id)

    # ---------------------------------------------------------------- 读取

    def _version_row(self, version_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM conclusion_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFound("结论版本不存在")
        return row

    def get_version(self, actor_id: str, version_id: str) -> dict[str, Any]:
        self._require(actor_id, "conclusion.read")
        row = self._version_row(version_id)
        steps = self.connection.execute(
            "SELECT * FROM signoff_steps WHERE version_id=? ORDER BY sequence", (version_id,)
        ).fetchall()
        signoffs = self.connection.execute(
            "SELECT * FROM signoffs WHERE version_id=? ORDER BY signoff_id", (version_id,)
        ).fetchall()
        manifest_items = self.connection.execute(
            "SELECT batch_id,item_id,item_sha256,frozen_state FROM conclusion_manifest_items "
            "WHERE version_id=? ORDER BY batch_id,item_id",
            (version_id,),
        ).fetchall()
        conflicts = self.connection.execute(
            "SELECT user_id,reason,declared_by,declared_at FROM conflicts_of_interest WHERE version_id=? "
            "ORDER BY conflict_id",
            (version_id,),
        ).fetchall()
        publication = self.connection.execute(
            "SELECT * FROM conclusion_publications WHERE version_id=?", (version_id,)
        ).fetchone()
        issues = self._integrity_issues(version_id) if row["state"] != "published" else []
        return {
            "version_id": version_id,
            "subject_id": row["subject_id"],
            "version_no": row["version_no"],
            "discipline": row["discipline"],
            "state": row["state"],
            "protocol": {
                "protocol_id": row["protocol_id"],
                "version": row["protocol_version"],
                "sha256": row["protocol_sha256"],
            },
            "boundary": {"boundary_id": row["boundary_id"], "sha256": row["boundary_sha256"]},
            "manifest_sha256": row["manifest_sha256"],
            "basis_hash": row["basis_hash"],
            "summary": json.loads(row["summary_json"]),
            "supersedes_version": row["supersedes_version"],
            "blocked_reason": row["blocked_reason"],
            "blocked_at": row["blocked_at"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "published_at": row["published_at"],
            "published_by": row["published_by"],
            "manifest": [dict(item) for item in manifest_items],
            "steps": [{"sequence": s["sequence"], "role": s["role"], "title": s["title"]} for s in steps],
            "signoffs": [
                {
                    "signoff_id": s["signoff_id"], "sequence": s["sequence"], "signer_id": s["signer_id"],
                    "signer_role": s["signer_role"], "status": s["status"], "note": s["note"],
                    "signed_at": s["signed_at"], "fingerprint": s["signed_fingerprint"],
                    "invalidated_at": s["invalidated_at"], "invalidate_reason": s["invalidate_reason"],
                }
                for s in signoffs
            ],
            "conflicts": [dict(row) for row in conflicts],
            "integrity": {"ok": not issues, "issues": issues},
            "publication": None if publication is None else {
                "publication_id": publication["publication_id"],
                "manifest_sha256": publication["manifest_sha256"],
                "basis_hash": publication["basis_hash"],
                "signoff_fingerprint": publication["signoff_fingerprint"],
                "published_by": publication["published_by"],
                "published_at": publication["published_at"],
            },
        }

    def list_versions(self, actor_id: str, subject_id: str) -> dict[str, Any]:
        self._require(actor_id, "conclusion.read")
        rows = self.connection.execute(
            "SELECT version_id,version_no,state,discipline,blocked_reason,created_at,published_at,published_by "
            "FROM conclusion_versions WHERE subject_id=? ORDER BY version_no",
            (subject_id,),
        ).fetchall()
        return {"subject_id": subject_id, "versions": [dict(row) for row in rows]}

    def current_conclusion(self, actor_id: str, subject_id: str) -> dict[str, Any]:
        """返回当前可执行结论（最近归档版本）与在途草案状态。"""

        self._require(actor_id, "conclusion.read")
        published = self.connection.execute(
            "SELECT version_id FROM conclusion_versions WHERE subject_id=? AND state='published' "
            "ORDER BY version_no DESC LIMIT 1",
            (subject_id,),
        ).fetchone()
        open_row = self.connection.execute(
            "SELECT version_id,version_no,state,blocked_reason FROM conclusion_versions "
            "WHERE subject_id=? AND state='signing' ORDER BY version_no DESC LIMIT 1",
            (subject_id,),
        ).fetchone()
        return {
            "subject_id": subject_id,
            "executable": None if published is None else self.get_version(actor_id, published["version_id"]),
            "open_version": None if open_row is None else dict(open_row),
        }

    def audit_trail(self, actor_id: str, *, subject_id: str | None = None, version_id: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        if version_id is not None:
            self._version_row(version_id)
            rows = self.connection.execute(
                "SELECT event_id,entity_type,entity_id,event_type,actor_id,payload_json,created_at "
                "FROM audit_events WHERE entity_type='conclusion' AND entity_id=? ORDER BY event_id",
                (version_id,),
            ).fetchall()
            scope = {"version_id": version_id}
        elif subject_id is not None:
            scope_rows = self.connection.execute(
                "SELECT version_id FROM conclusion_versions WHERE subject_id=? ORDER BY version_no",
                (subject_id,),
            ).fetchall()
            version_ids = [row["version_id"] for row in scope_rows]
            rows = self._subject_audit_rows(subject_id, version_ids)
            scope = {"subject_id": subject_id}
        else:
            raise ValidationFailed("必须提供 subject_id 或 version_id")
        events = [
            {
                "event_id": row["event_id"], "entity_type": row["entity_type"], "entity_id": row["entity_id"],
                "event_type": row["event_type"], "actor_id": row["actor_id"], "created_at": row["created_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]
        return {"scope": scope, "events": events}

    def _subject_audit_rows(self, subject_id: str, version_ids: list[str]) -> list[sqlite3.Row]:
        # 纳入被各版本冻结清单引用过的证据批次与条目，使补充/撤回/失效源头可追溯。
        batch_rows = self.connection.execute(
            "SELECT DISTINCT batch_id FROM conclusion_manifest_items WHERE version_id IN "
            "(SELECT value FROM json_each(?))",
            (canonical_json(version_ids),),
        ).fetchall() if version_ids else []
        batches = [row["batch_id"] for row in batch_rows]
        item_rows: list[sqlite3.Row] = []
        if batches:
            placeholders = ",".join("?" for _ in batches)
            item_rows = self.connection.execute(
                f"SELECT DISTINCT CAST(item_id AS TEXT) AS item_id FROM conclusion_manifest_items "
                f"WHERE item_id IS NOT NULL AND batch_id IN ({placeholders})",
                batches,
            ).fetchall()
        items = [row["item_id"] for row in item_rows]
        clauses = ["(entity_type='assessment_subject' AND entity_id=?)"]
        params: list[Any] = [subject_id]
        if version_ids:
            clauses.append(
                f"entity_id IN ({','.join('?' for _ in version_ids)}) "
                "AND entity_type IN ('conclusion','conclusion_version')"
            )
            params.extend(version_ids)
        if batches:
            clauses.append(
                f"entity_type='evidence_batch' AND entity_id IN ({','.join('?' for _ in batches)})"
            )
            params.extend(batches)
        if items:
            clauses.append(
                f"entity_type='evidence_item' AND entity_id IN ({','.join('?' for _ in items)})"
            )
            params.extend(items)
        return list(self.connection.execute(
            "SELECT event_id,entity_type,entity_id,event_type,actor_id,payload_json,created_at "
            f"FROM audit_events WHERE {' OR '.join(clauses)} ORDER BY event_id",
            params,
        ).fetchall())
