"""选址综合结论版本化会签的领域用例。

核心不变量：
* 结论版本一旦创建，调查协议、证据批次修订与空间边界即被内容摘要冻结；
* 证据只增不改：补充、撤回、失效都追加批次修订，并级联阻断未发布草案；
* 已发布（含被取代）版本的冻结引用、签署与决定永不被后续事件改写；
* 会签严格按 林业→生态→工程 顺序进行，任一失效都打断签名链；
* 发布在单事务内原子确认全部会签、回避关系与证据完整性。
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Mapping

from .clock import SystemClock, isoformat
from .contracts import (
    ROLE_FOR_STEP,
    SIGNING_STEPS,
    STEP_FOR_ROLE,
    BoundarySpec,
    EvidenceSelection,
    identifier,
    required_text,
    validate_role,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ServiceError, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


CHANGE_KINDS = frozenset({"supplement", "revocation", "invalidation"})
CHANGE_LABELS = {
    "supplement": "证据补充",
    "revocation": "证据撤回",
    "invalidation": "证据失效",
}


class SiteReviewService:
    """在单个 SQLite 连接上提供选址会签的全部业务操作。"""

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

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        user_id = identifier(user_id, "user_id")
        role = validate_role(role)
        if not display_name.strip():
            raise ValidationFailed("display_name 不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id, display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id, "role": role}

    # ------------------------------------------------------------ 协议与批次

    def register_protocol(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        actor = self._user(actor_id)
        domain = required_text(raw.get("domain"), "protocol.domain", 32)
        if domain not in SIGNING_STEPS:
            raise ValidationFailed(f"protocol.domain 必须是 {', '.join(SIGNING_STEPS)} 之一")
        if actor["role"] != ROLE_FOR_STEP[domain]:
            raise Forbidden(f"只有 {ROLE_FOR_STEP[domain]} 角色可以登记 {domain} 域调查协议")
        protocol_id = identifier(raw.get("protocol_id"), "protocol.protocol_id")
        version = raw.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValidationFailed("protocol.version 必须是正整数")
        title = required_text(raw.get("title"), "protocol.title")
        spec = raw.get("spec")
        if spec is None:
            raise ValidationFailed("protocol.spec 不能为空")
        digest = content_digest([raw])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO evidence_protocols(protocol_id,version,domain,title,canonical_json,"
                    "content_sha256,registered_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (protocol_id, version, domain, title, canonical_json(raw), digest, actor_id, self._now()),
                )
                self._audit(
                    "evidence_protocol", f"{protocol_id}@{version}", "evidence_protocol.registered",
                    actor_id, {"domain": domain, "sha256": digest},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("协议版本已经存在") from exc
        return {"protocol_id": protocol_id, "version": version, "domain": domain, "sha256": digest}

    def _protocol(self, protocol_id: str, version: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM evidence_protocols WHERE protocol_id=? AND version=?", (protocol_id, version)
        ).fetchone()
        if row is None:
            raise NotFound(f"调查协议版本不存在: {protocol_id}@{version}")
        return row

    def register_batch(
        self, actor_id: str, batch_id: str, protocol_id: str, protocol_version: int,
        manifest: Any, note: str,
    ) -> dict[str, Any]:
        actor = self._user(actor_id)
        batch_id = identifier(batch_id, "batch_id")
        protocol = self._protocol(protocol_id, protocol_version)
        if actor["role"] != ROLE_FOR_STEP[protocol["domain"]]:
            raise Forbidden(f"证据批次 {protocol['domain']} 只能由 {ROLE_FOR_STEP[protocol['domain']]} 维护")
        manifest = _validate_manifest(manifest)
        note = required_text(note, "note", 1024)
        digest = content_digest([manifest])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO evidence_batches(batch_id,domain,protocol_id,protocol_version,protocol_sha256,"
                    "revision,registered_by,created_at) VALUES(?,?,?,?,?,1,?,?)",
                    (
                        batch_id, protocol["domain"], protocol_id, protocol_version, protocol["content_sha256"],
                        actor_id, self._now(),
                    ),
                )
                self.connection.execute(
                    "INSERT INTO evidence_batch_revisions(batch_id,revision,manifest_json,manifest_sha256,"
                    "change_note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (batch_id, 1, canonical_json(manifest), digest, note, actor_id, self._now()),
                )
                self._audit(
                    "evidence_batch", batch_id, "evidence_batch.registered", actor_id,
                    {"domain": protocol["domain"], "revision": 1, "manifest_sha256": digest,
                     "protocol": f"{protocol_id}@{protocol_version}"},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"证据批次已存在: {batch_id}") from exc
        return {"batch_id": batch_id, "domain": protocol["domain"], "revision": 1, "manifest_sha256": digest}

    def get_batch(self, batch_id: str) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM evidence_batches WHERE batch_id=?", (batch_id,)).fetchone()
        if row is None:
            raise NotFound("证据批次不存在")
        revisions = self.connection.execute(
            "SELECT revision,manifest_sha256,change_note,created_by,created_at "
            "FROM evidence_batch_revisions WHERE batch_id=? ORDER BY revision",
            (batch_id,),
        ).fetchall()
        return dict(row) | {"revisions": [dict(item) for item in revisions]}

    def revise_batch(
        self, actor_id: str, batch_id: str, manifest: Any, change_kind: str, note: str
    ) -> dict[str, Any]:
        """追加证据批次修订（补充/撤回/失效），并级联阻断未发布草案。"""

        actor = self._user(actor_id)
        if change_kind not in CHANGE_KINDS:
            raise ValidationFailed(f"change_kind 必须是 {', '.join(sorted(CHANGE_KINDS))} 之一")
        note = required_text(note, "note", 1024)
        batch = self.connection.execute(
            "SELECT * FROM evidence_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise NotFound("证据批次不存在")
        if actor["role"] != ROLE_FOR_STEP[batch["domain"]]:
            raise Forbidden(f"证据批次 {batch['domain']} 只能由 {ROLE_FOR_STEP[batch['domain']]} 维护")
        manifest = _validate_manifest(manifest)
        digest = content_digest([manifest])
        with transaction(self.connection, immediate=True):
            latest = self.connection.execute(
                "SELECT max(revision) AS revision FROM evidence_batch_revisions WHERE batch_id=?", (batch_id,)
            ).fetchone()["revision"]
            new_revision = latest + 1
            self.connection.execute(
                "INSERT INTO evidence_batch_revisions(batch_id,revision,manifest_json,manifest_sha256,"
                "change_note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (batch_id, new_revision, canonical_json(manifest), digest, note, actor_id, self._now()),
            )
            self.connection.execute(
                "UPDATE evidence_batches SET revision=? WHERE batch_id=?", (new_revision, batch_id)
            )
            affected = self.connection.execute(
                "SELECT DISTINCT c.subject_id,c.version_no,c.state FROM conclusion_evidence_refs r "
                "JOIN conclusion_versions c ON c.subject_id=r.subject_id AND c.version_no=r.version_no "
                "WHERE r.batch_id=? AND c.state IN ('draft','blocked') ORDER BY c.subject_id,c.version_no",
                (batch_id,),
            ).fetchall()
            for version in affected:
                self._invalidate_version_signatures(
                    version["subject_id"], version["version_no"],
                    reason=f"底层证据批次 {batch_id} 发生{CHANGE_LABELS[change_kind]}（新修订 r{new_revision}）",
                )
                self.connection.execute(
                    "UPDATE conclusion_versions SET state='blocked',blocked_reason=? "
                    "WHERE subject_id=? AND version_no=? AND state IN ('draft','blocked')",
                    (
                        f"证据批次 {batch_id} {CHANGE_LABELS[change_kind]}：{note}",
                        version["subject_id"], version["version_no"],
                    ),
                )
                self._audit(
                    "conclusion", f"{version['subject_id']}/v{version['version_no']}",
                    "conclusion.blocked", actor_id,
                    {"batch_id": batch_id, "new_revision": new_revision, "change_kind": change_kind,
                     "previous_state": version["state"], "note": note},
                )
            self._audit(
                "evidence_batch", batch_id, "evidence_batch.revision_appended", actor_id,
                {"revision": new_revision, "change_kind": change_kind, "manifest_sha256": digest,
                 "affected_versions": [
                     {"subject_id": item["subject_id"], "version_no": item["version_no"]} for item in affected
                 ], "note": note},
            )
        return {"batch_id": batch_id, "revision": new_revision, "manifest_sha256": digest,
                "affected_versions": [
                    {"subject_id": item["subject_id"], "version_no": item["version_no"]} for item in affected
                ]}

    # ----------------------------------------------------------- 结论版本草案

    def create_conclusion_version(
        self, actor_id: str, subject_id: str, title: str,
        boundary: Mapping[str, Any], evidence_batches: list[Mapping[str, Any]],
    ) -> dict[str, Any]:
        actor = self._user(actor_id)
        if actor["role"] != "coordinator":
            raise Forbidden("只有协调人可以创建结论草案")
        subject_id = identifier(subject_id, "subject_id")
        title = required_text(title, "title")
        spec = BoundarySpec.from_dict(boundary)
        selections = [EvidenceSelection.from_dict(raw, index) for index, raw in enumerate(evidence_batches)]
        domains = [item.domain for item in selections]
        if sorted(domains) != sorted(SIGNING_STEPS) or len(set(domains)) != len(domains):
            raise ValidationFailed("证据批次必须恰好覆盖林业、生态、工程三个专业域且各一个")

        boundary_object = {
            "boundary_id": spec.boundary_id,
            "label": spec.label,
            "geometry": spec.geometry,
            "source_text": spec.source_text,
        }
        boundary_digest = content_digest([boundary_object])

        frozen_evidence: list[dict[str, Any]] = []
        for step in SIGNING_STEPS:
            selection = next(item for item in selections if item.domain == step)
            batch = self.connection.execute(
                "SELECT * FROM evidence_batches WHERE batch_id=?", (selection.batch_id,)
            ).fetchone()
            if batch is None:
                raise NotFound(f"证据批次不存在: {selection.batch_id}")
            if batch["domain"] != step:
                raise ValidationFailed(f"证据批次 {selection.batch_id} 不属于 {step} 专业域")
            revision = self.connection.execute(
                "SELECT * FROM evidence_batch_revisions WHERE batch_id=? AND revision=?",
                (selection.batch_id, batch["revision"]),
            ).fetchone()
            protocol = self._protocol(batch["protocol_id"], batch["protocol_version"])
            if protocol["content_sha256"] != batch["protocol_sha256"]:
                raise InvalidState("证据批次登记的协议摘要与协议目录不一致")
            frozen_evidence.append({
                "step": step,
                "batch_id": selection.batch_id,
                "batch_revision": batch["revision"],
                "manifest_sha256": revision["manifest_sha256"],
                "protocol": {
                    "protocol_id": batch["protocol_id"],
                    "version": batch["protocol_version"],
                    "content_sha256": batch["protocol_sha256"],
                },
            })

        freeze_object = {
            "subject_id": subject_id,
            "title": title,
            "boundary": boundary_object,
            "boundary_sha256": boundary_digest,
            "evidence": frozen_evidence,
        }
        freeze_digest = content_digest([freeze_object])

        with transaction(self.connection, immediate=True):
            latest = self.connection.execute(
                "SELECT max(version_no) AS version_no, "
                "max(CASE WHEN state='draft' THEN 1 ELSE 0 END) AS has_open_draft "
                "FROM conclusion_versions WHERE subject_id=?",
                (subject_id,),
            ).fetchone()
            if latest["has_open_draft"]:
                raise Conflict("该评估对象已有可签署草案，请先完成或阻断当前版本")
            version_no = (latest["version_no"] or 0) + 1
            self.connection.execute(
                "INSERT INTO conclusion_versions(subject_id,version_no,title,state,boundary_id,boundary_label,"
                "boundary_geometry_json,boundary_source,boundary_sha256,freeze_json,freeze_sha256,"
                "created_by,created_at) VALUES(?,?,?, 'draft', ?,?,?,?,?,?,?,?,?)",
                (
                    subject_id, version_no, title, spec.boundary_id, spec.label,
                    canonical_json(spec.geometry), spec.source_text, boundary_digest,
                    canonical_json(freeze_object), freeze_digest, actor_id, self._now(),
                ),
            )
            for item in frozen_evidence:
                self.connection.execute(
                    "INSERT INTO conclusion_evidence_refs(subject_id,version_no,step,batch_id,batch_revision,"
                    "manifest_sha256,protocol_id,protocol_version,protocol_sha256) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        subject_id, version_no, item["step"], item["batch_id"], item["batch_revision"],
                        item["manifest_sha256"], item["protocol"]["protocol_id"],
                        item["protocol"]["version"], item["protocol"]["content_sha256"],
                    ),
                )
            self._audit(
                "conclusion", f"{subject_id}/v{version_no}", "conclusion.version_created", actor_id,
                {"freeze_sha256": freeze_digest, "boundary_sha256": boundary_digest,
                 "evidence": [
                     {"step": item["step"], "batch_id": item["batch_id"],
                      "batch_revision": item["batch_revision"], "manifest_sha256": item["manifest_sha256"]}
                     for item in frozen_evidence
                 ]},
            )
        return self.get_version(subject_id, version_no)

    def _version_row(self, subject_id: str, version_no: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM conclusion_versions WHERE subject_id=? AND version_no=?",
            (subject_id, version_no),
        ).fetchone()
        if row is None:
            raise NotFound(f"结论版本不存在: {subject_id}/v{version_no}")
        return row

    def get_version(self, subject_id: str, version_no: int) -> dict[str, Any]:
        version = self._version_row(subject_id, version_no)
        refs = sorted(
            self.connection.execute(
                "SELECT step,batch_id,batch_revision,manifest_sha256,protocol_id,protocol_version,protocol_sha256 "
                "FROM conclusion_evidence_refs WHERE subject_id=? AND version_no=?",
                (subject_id, version_no),
            ).fetchall(),
            key=lambda row: SIGNING_STEPS.index(row["step"]),
        )
        signatures = self.connection.execute(
            "SELECT signature_id,step,sign_order,signer_id,basis_summary,signature_sha256,"
            "prev_signature_sha256,status,invalidate_reason,signed_at,invalidated_at "
            "FROM signatures WHERE subject_id=? AND version_no=? ORDER BY signature_id",
            (subject_id, version_no),
        ).fetchall()
        recusals = self.connection.execute(
            "SELECT user_id,reason,declared_by,declared_at FROM recusals "
            "WHERE subject_id=? AND version_no=? ORDER BY recusal_id",
            (subject_id, version_no),
        ).fetchall()
        publication = self.connection.execute(
            "SELECT decision_no,evidence_integrity_sha256,published_by,published_at "
            "FROM publications WHERE subject_id=? AND version_no=?",
            (subject_id, version_no),
        ).fetchone()
        valid_signatures = [dict(row) for row in signatures if row["status"] == "valid"]
        return {
            "subject_id": version["subject_id"],
            "version_no": version["version_no"],
            "title": version["title"],
            "state": version["state"],
            "blocked_reason": version["blocked_reason"],
            "freeze_sha256": version["freeze_sha256"],
            "boundary_sha256": version["boundary_sha256"],
            "boundary": {
                "boundary_id": version["boundary_id"],
                "label": version["boundary_label"],
                "geometry": json.loads(version["boundary_geometry_json"]),
                "source_text": version["boundary_source"],
            },
            "evidence_refs": [dict(row) for row in refs],
            "signatures": [dict(row) for row in signatures],
            "valid_signature_count": len(valid_signatures),
            "next_step": None if len(valid_signatures) == len(SIGNING_STEPS)
            else SIGNING_STEPS[len(valid_signatures)],
            "recusals": [dict(row) for row in recusals],
            "publication": None if publication is None else dict(publication),
            "created_by": version["created_by"],
            "created_at": version["created_at"],
            "published_at": version["published_at"],
        }

    def list_versions(self, actor_id: str, subject_id: str) -> dict[str, Any]:
        self._user(actor_id)
        rows = self.connection.execute(
            "SELECT subject_id,version_no,title,state,blocked_reason,freeze_sha256,created_at,published_at "
            "FROM conclusion_versions WHERE subject_id=? ORDER BY version_no",
            (subject_id,),
        ).fetchall()
        if not rows:
            raise NotFound(f"评估对象不存在: {subject_id}")
        return {"subject_id": subject_id, "versions": [dict(row) for row in rows]}

    # --------------------------------------------------------------- 顺序会签

    @staticmethod
    def _signature_payload(
        freeze_digest: str, step: str, sign_order: int, signer_id: str,
        basis_summary: str, prev_signature_sha256: str | None,
    ) -> dict[str, Any]:
        return {
            "freeze_sha256": freeze_digest,
            "step": step,
            "sign_order": sign_order,
            "signer_id": signer_id,
            "basis_summary": basis_summary,
            "prev_signature_sha256": prev_signature_sha256,
        }

    def _invalidate_version_signatures(
        self, subject_id: str, version_no: int, *, reason: str, from_step: str | None = None
    ) -> int:
        """失效版本上的有效签署；from_step 给定时连其后步骤一并失效（签名链断裂）。"""

        query = (
            "SELECT signature_id,step FROM signatures "
            "WHERE subject_id=? AND version_no=? AND status='valid'"
        )
        parameters: list[Any] = [subject_id, version_no]
        if from_step is not None:
            minimum = SIGNING_STEPS.index(from_step)
            query += " AND sign_order >= ?"
            parameters.append(minimum + 1)
        rows = self.connection.execute(query, parameters).fetchall()
        for row in rows:
            self.connection.execute(
                "UPDATE signatures SET status='invalidated',invalidate_reason=?,invalidated_at=? "
                "WHERE signature_id=? AND status='valid'",
                (reason, self._now(), row["signature_id"]),
            )
        return len(rows)

    def sign(self, actor_id: str, subject_id: str, version_no: int, basis_summary: str) -> dict[str, Any]:
        actor = self._user(actor_id)
        step = STEP_FOR_ROLE.get(actor["role"])
        if step is None:
            raise Forbidden(f"角色 {actor['role']} 没有会签职责")
        basis_summary = required_text(basis_summary, "basis_summary", 2048)
        with transaction(self.connection, immediate=True):
            version = self._version_row(subject_id, version_no)
            if version["state"] == "published" or version["state"] == "superseded":
                raise InvalidState("版本已经归档发布，不能再追加签署")
            if version["state"] == "blocked":
                raise InvalidState("草案已被阻断，请基于最新证据创建新版本后重新会签")
            if self.connection.execute(
                "SELECT 1 FROM recusals WHERE subject_id=? AND version_no=? AND user_id=?",
                (subject_id, version_no, actor_id),
            ).fetchone() is not None:
                raise Forbidden("存在回避关系的人员不能在该版本上签署")
            if self.connection.execute(
                "SELECT 1 FROM signatures WHERE subject_id=? AND version_no=? AND step=? AND status='valid'",
                (subject_id, version_no, step),
            ).fetchone() is not None:
                raise Conflict("该职责步骤已存在有效签署，重复签署不会产生新记录")
            valid = self.connection.execute(
                "SELECT step,signer_id,signature_sha256 FROM signatures "
                "WHERE subject_id=? AND version_no=? AND status='valid' ORDER BY sign_order",
                (subject_id, version_no),
            ).fetchall()
            expected_order = len(valid) + 1
            required_step = SIGNING_STEPS[expected_order - 1]
            if step != required_step:
                raise InvalidState(
                    f"当前应完成第 {expected_order} 步 {required_step} 签署，不能由 {step} 提前或越序签署"
                )
            prev_digest = None if not valid else valid[-1]["signature_sha256"]
            payload = self._signature_payload(
                version["freeze_sha256"], step, expected_order, actor_id, basis_summary, prev_digest
            )
            signature_digest = content_digest([payload])
            try:
                cursor = self.connection.execute(
                    "INSERT INTO signatures(subject_id,version_no,step,sign_order,signer_id,basis_summary,"
                    "signature_sha256,prev_signature_sha256,signed_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        subject_id, version_no, step, expected_order, actor_id, basis_summary,
                        signature_digest, prev_digest, self._now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该职责步骤已存在有效签署，重复签署不会产生新记录") from exc
            self._audit(
                "conclusion", f"{subject_id}/v{version_no}", "conclusion.signed", actor_id,
                {"signature_id": cursor.lastrowid, "step": step, "sign_order": expected_order,
                 "signature_sha256": signature_digest, "prev_signature_sha256": prev_digest,
                 "basis_summary": basis_summary},
            )
        return self.get_version(subject_id, version_no)

    def declare_recusal(
        self, actor_id: str, subject_id: str, version_no: int, user_id: str, reason: str
    ) -> dict[str, Any]:
        actor = self._user(actor_id)
        target = self._user(user_id)
        if actor_id != user_id and actor["role"] != "coordinator":
            raise Forbidden("只有当事人本人或协调人可以申报回避关系")
        reason = required_text(reason, "reason", 1024)
        with transaction(self.connection, immediate=True):
            version = self._version_row(subject_id, version_no)
            if version["state"] in {"published", "superseded"}:
                raise InvalidState("版本已经归档发布，不能再变更回避关系")
            try:
                self.connection.execute(
                    "INSERT INTO recusals(subject_id,version_no,user_id,reason,declared_by,declared_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (subject_id, version_no, user_id, reason, actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该人员在本版本上的回避关系已经申报") from exc
            signed_step = self.connection.execute(
                "SELECT step FROM signatures WHERE subject_id=? AND version_no=? "
                "AND signer_id=? AND status='valid' ORDER BY sign_order",
                (subject_id, version_no, user_id),
            ).fetchone()
            invalidated: list[dict[str, Any]] = []
            if signed_step is not None:
                # 回避使当事人签署失效；签名链自该步起全部断裂，需按顺序重新会签。
                self._invalidate_version_signatures(
                    subject_id, version_no,
                    reason=f"签署人 {user_id} 存在回避关系：{reason}",
                    from_step=signed_step["step"],
                )
                invalidated = [
                    dict(row) for row in self.connection.execute(
                        "SELECT signature_id,step,sign_order,signer_id FROM signatures "
                        "WHERE subject_id=? AND version_no=? AND status='invalidated' "
                        "AND invalidate_reason=? ORDER BY sign_order",
                        (subject_id, version_no, f"签署人 {user_id} 存在回避关系：{reason}"),
                    ).fetchall()
                ]
            self._audit(
                "conclusion", f"{subject_id}/v{version_no}", "recusal.declared", actor_id,
                {"user_id": user_id, "role": target["role"], "reason": reason,
                 "invalidated_signatures": invalidated},
            )
        return {"subject_id": subject_id, "version_no": version_no, "user_id": user_id,
                "invalidated_signatures": invalidated}

    # ----------------------------------------------------------- 原子发布决定

    def _verify_freeze(self, version: sqlite3.Row) -> tuple[list[dict[str, Any]], list[sqlite3.Row]]:
        """重算并核对冻结的协议、证据清单与空间边界完整性。"""

        rows = self.connection.execute(
            "SELECT * FROM conclusion_evidence_refs WHERE subject_id=? AND version_no=?",
            (version["subject_id"], version["version_no"]),
        ).fetchall()
        refs = sorted(rows, key=lambda row: SIGNING_STEPS.index(row["step"]))
        if [row["step"] for row in refs] != list(SIGNING_STEPS):
            raise InvalidState("冻结证据未覆盖全部三个专业域")
        evidence: list[dict[str, Any]] = []
        for ref in refs:
            revision = self.connection.execute(
                "SELECT manifest_sha256 FROM evidence_batch_revisions WHERE batch_id=? AND revision=?",
                (ref["batch_id"], ref["batch_revision"]),
            ).fetchone()
            if revision is None:
                raise InvalidState(f"冻结的证据批次修订缺失: {ref['batch_id']}@r{ref['batch_revision']}")
            if revision["manifest_sha256"] != ref["manifest_sha256"]:
                raise InvalidState(f"证据批次 {ref['batch_id']} 清单摘要与冻结时不一致")
            protocol = self.connection.execute(
                "SELECT content_sha256,domain FROM evidence_protocols WHERE protocol_id=? AND version=?",
                (ref["protocol_id"], ref["protocol_version"]),
            ).fetchone()
            if protocol is None:
                raise InvalidState(
                    f"冻结的调查协议缺失: {ref['protocol_id']}@{ref['protocol_version']}"
                )
            if protocol["content_sha256"] != ref["protocol_sha256"]:
                raise InvalidState(f"调查协议 {ref['protocol_id']} 内容摘要与冻结时不一致")
            if protocol["domain"] != ref["step"]:
                raise InvalidState(f"证据批次 {ref['batch_id']} 的专业域与签署步骤不匹配")
            evidence.append({
                "step": ref["step"],
                "batch_id": ref["batch_id"],
                "batch_revision": ref["batch_revision"],
                "manifest_sha256": ref["manifest_sha256"],
                "protocol": {
                    "protocol_id": ref["protocol_id"],
                    "version": ref["protocol_version"],
                    "content_sha256": ref["protocol_sha256"],
                },
            })
        geometry = json.loads(version["boundary_geometry_json"])
        boundary_object = {
            "boundary_id": version["boundary_id"],
            "label": version["boundary_label"],
            "geometry": geometry,
            "source_text": version["boundary_source"],
        }
        if content_digest([boundary_object]) != version["boundary_sha256"]:
            raise InvalidState("空间边界内容摘要与冻结时不一致")
        freeze_object = {
            "subject_id": version["subject_id"],
            "title": version["title"],
            "boundary": boundary_object,
            "boundary_sha256": version["boundary_sha256"],
            "evidence": evidence,
        }
        stored_freeze = json.loads(version["freeze_json"])
        if freeze_object != stored_freeze:
            raise InvalidState("冻结内容与重算结果不一致")
        if content_digest([freeze_object]) != version["freeze_sha256"]:
            raise InvalidState("冻结摘要与重算结果不一致")
        return evidence, refs

    def publish(self, actor_id: str, subject_id: str, version_no: int) -> dict[str, Any]:
        actor = self._user(actor_id)
        if actor["role"] != "coordinator":
            raise Forbidden("只有协调人可以发布综合结论")
        failure: ServiceError | None = None
        failure_reason: dict[str, Any] | None = None
        with transaction(self.connection, immediate=True):
            version = self._version_row(subject_id, version_no)
            existing = self.connection.execute(
                "SELECT * FROM publications WHERE subject_id=? AND version_no=?",
                (subject_id, version_no),
            ).fetchone()
            if existing is not None:
                # 重复发布不产生第二份决定。
                return self._publication_view(existing, idempotent=True)

            # 校验放入保存点：失败时回滚校验过程，但拒绝事件随事务提交，供审计追溯。
            self.connection.execute("SAVEPOINT publish_checks")
            try:
                if version["state"] == "blocked":
                    failure_reason = {"reason": "blocked", "blocked_reason": version["blocked_reason"]}
                    raise InvalidState(f"草案处于阻断状态，不能发布：{version['blocked_reason']}")

                evidence, _ = self._verify_freeze(version)

                valid = self.connection.execute(
                    "SELECT * FROM signatures WHERE subject_id=? AND version_no=? AND status='valid' "
                    "ORDER BY sign_order",
                    (subject_id, version_no),
                ).fetchall()
                if len(valid) != len(SIGNING_STEPS):
                    failure_reason = {
                        "reason": "incomplete_countersign", "valid_signatures": len(valid),
                    }
                    raise InvalidState(f"会签不完整：{len(valid)}/{len(SIGNING_STEPS)}")
                prev_digest: str | None = None
                for index, row in enumerate(valid):
                    expected_step = SIGNING_STEPS[index]
                    if row["step"] != expected_step or row["sign_order"] != index + 1:
                        failure_reason = {
                            "reason": "signature_order", "expected_step": expected_step,
                            "actual_step": row["step"],
                        }
                        raise InvalidState("会签顺序与职责链不一致")
                    if row["prev_signature_sha256"] != prev_digest:
                        failure_reason = {"reason": "broken_signature_chain", "step": row["step"]}
                        raise InvalidState(f"{row['step']} 签署的前序签名链不连续")
                    signer = self.connection.execute(
                        "SELECT role FROM users WHERE user_id=?", (row["signer_id"],)
                    ).fetchone()
                    if signer is None or signer["role"] != ROLE_FOR_STEP[expected_step]:
                        failure_reason = {
                            "reason": "signer_role", "step": row["step"], "signer_id": row["signer_id"],
                        }
                        raise InvalidState(f"{row['step']} 签署人角色与职责不匹配")
                    payload = self._signature_payload(
                        version["freeze_sha256"], row["step"], row["sign_order"], row["signer_id"],
                        row["basis_summary"], row["prev_signature_sha256"],
                    )
                    if content_digest([payload]) != row["signature_sha256"]:
                        failure_reason = {"reason": "signature_digest", "step": row["step"]}
                        raise InvalidState(f"{row['step']} 签署摘要无法复核")
                    prev_digest = row["signature_sha256"]

                recused = {
                    row["user_id"]
                    for row in self.connection.execute(
                        "SELECT user_id FROM recusals WHERE subject_id=? AND version_no=?",
                        (subject_id, version_no),
                    ).fetchall()
                }
                signers = {row["signer_id"] for row in valid}
                conflict_users = sorted(recused & signers)
                if conflict_users:
                    failure_reason = {"reason": "recusal_conflict", "users": conflict_users}
                    raise Forbidden(f"签署人与回避关系冲突: {', '.join(conflict_users)}")
            except ServiceError as exc:
                self.connection.execute("ROLLBACK TO SAVEPOINT publish_checks")
                failure = exc
            finally:
                self.connection.execute("RELEASE SAVEPOINT publish_checks")

            if failure is not None:
                self._audit(
                    "conclusion", f"{subject_id}/v{version_no}", "publication.rejected", actor_id,
                    failure_reason or {"reason": "unknown"},
                )
                # 不在此处抛出：让事务正常提交拒绝事件，退出事务后再向调用方报错。

            if failure is None:
                integrity_material = {
                    "freeze_sha256": version["freeze_sha256"],
                    "manifests": [
                        {"step": item["step"], "manifest_sha256": item["manifest_sha256"]}
                        for item in evidence
                    ],
                    "signatures": [
                        {"step": row["step"], "signer_id": row["signer_id"],
                         "signature_sha256": row["signature_sha256"]}
                        for row in valid
                    ],
                }
                evidence_integrity_digest = content_digest([integrity_material])
                decision_no = f"D-{subject_id}-v{version_no}-{version['freeze_sha256'][:10]}"
                record = {
                    "decision_no": decision_no,
                    "subject_id": subject_id,
                    "version_no": version_no,
                    "title": version["title"],
                    "freeze_sha256": version["freeze_sha256"],
                    "boundary_sha256": version["boundary_sha256"],
                    "evidence": evidence,
                    "signatures": [
                        {"step": row["step"], "sign_order": row["sign_order"], "signer_id": row["signer_id"],
                         "basis_summary": row["basis_summary"], "signature_sha256": row["signature_sha256"],
                         "signed_at": row["signed_at"]}
                        for row in valid
                    ],
                    "recusals": sorted(recused),
                    "evidence_integrity_sha256": evidence_integrity_digest,
                }
                now = self._now()
                self.connection.execute(
                    "INSERT INTO publications(decision_no,subject_id,version_no,freeze_sha256,"
                    "evidence_integrity_sha256,record_json,published_by,published_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        decision_no, subject_id, version_no, version["freeze_sha256"],
                        evidence_integrity_digest, canonical_json(record), actor_id, now,
                    ),
                )
                self.connection.execute(
                    "UPDATE conclusion_versions SET state='published',published_at=? "
                    "WHERE subject_id=? AND version_no=?",
                    (now, subject_id, version_no),
                )
                # 同一评估对象的在案发布版本转为被取代；其冻结内容、签署与决定行均不改动。
                superseded = self.connection.execute(
                    "UPDATE conclusion_versions SET state='superseded' "
                    "WHERE subject_id=? AND state='published' AND version_no<>?",
                    (subject_id, version_no),
                ).rowcount
                self._audit(
                    "conclusion", f"{subject_id}/v{version_no}", "conclusion.published", actor_id,
                    {"decision_no": decision_no, "evidence_integrity_sha256": evidence_integrity_digest,
                     "superseded_versions": superseded},
                )
                publication = self.connection.execute(
                    "SELECT * FROM publications WHERE decision_no=?", (decision_no,)
                ).fetchone()

        if failure is not None:
            raise failure
        return self._publication_view(publication)

    @staticmethod
    def _publication_view(row: sqlite3.Row, *, idempotent: bool = False) -> dict[str, Any]:
        record = json.loads(row["record_json"])
        return {
            "decision_no": row["decision_no"],
            "subject_id": row["subject_id"],
            "version_no": row["version_no"],
            "freeze_sha256": row["freeze_sha256"],
            "evidence_integrity_sha256": row["evidence_integrity_sha256"],
            "published_by": row["published_by"],
            "published_at": row["published_at"],
            "record": record,
            "idempotent_replay": idempotent,
        }

    def current_conclusion(self, actor_id: str, subject_id: str) -> dict[str, Any]:
        """返回评估对象当前可执行结论（最新发布的决定）。"""

        self._user(actor_id)
        row = self.connection.execute(
            "SELECT * FROM publications WHERE subject_id=? ORDER BY version_no DESC LIMIT 1",
            (subject_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"评估对象 {subject_id} 尚无已发布结论")
        return self._publication_view(row)

    # ------------------------------------------------------------------ 审计

    def audit_trail(self, actor_id: str, subject_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        if user["role"] not in {"auditor", "coordinator"}:
            raise Forbidden("只有审计人员或协调人可以查阅完整审计轨迹")
        rows = self.connection.execute(
            "SELECT event_id,entity_type,entity_id,event_type,actor_id,payload_json,created_at "
            "FROM audit_events WHERE entity_type='conclusion' AND entity_id LIKE ? "
            "ORDER BY event_id",
            (f"{subject_id}/v%",),
        ).fetchall()
        if not rows:
            raise NotFound(f"评估对象没有审计记录: {subject_id}")
        return {
            "subject_id": subject_id,
            "events": [
                dict(row) | {"payload": json.loads(row["payload_json"])}
                for row in rows
            ],
        }

    def batch_audit_trail(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        if user["role"] not in {"auditor", "coordinator"}:
            raise Forbidden("只有审计人员或协调人可以查阅完整审计轨迹")
        exists = self.connection.execute(
            "SELECT 1 FROM evidence_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if exists is None:
            raise NotFound("证据批次不存在")
        rows = self.connection.execute(
            "SELECT event_id,event_type,actor_id,payload_json,created_at FROM audit_events "
            "WHERE entity_type='evidence_batch' AND entity_id=? ORDER BY event_id",
            (batch_id,),
        ).fetchall()
        return {
            "batch_id": batch_id,
            "events": [
                dict(row) | {"payload": json.loads(row["payload_json"])}
                for row in rows
            ],
        }


def _validate_manifest(manifest: Any) -> Any:
    """证据清单必须是非空数组或对象，元素为结构化描述。"""

    if isinstance(manifest, Mapping):
        if not manifest:
            raise ValidationFailed("证据清单不能是空对象")
        return manifest
    if isinstance(manifest, (list, tuple)):
        if not manifest:
            raise ValidationFailed("证据清单不能为空数组")
        for index, item in enumerate(manifest):
            if not isinstance(item, Mapping):
                raise ValidationFailed(f"证据清单[{index}] 必须是对象")
        return list(manifest)
    raise ValidationFailed("证据清单必须是非空数组或对象")
