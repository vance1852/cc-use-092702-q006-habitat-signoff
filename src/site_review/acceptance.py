"""选址版本化结论会签平台的完整离线验收流程。

情景覆盖：
1. 三个专业分别登记协议、空间边界、证据批次与证据；
2. 为鸟类繁殖地评估对象建立 v1 草案并完成林业→生态→工程顺序会签后发布；
3. 对排水评估对象的未发布草案补充证据，草案被阻断、既有签署失效；
4. 阻断后另立 v2 草案完成会签并发布，v1 以 blocked 形态完整保留；
5. 已发布的鸟类结论在其证据失效后保持归档不变；
6. 审计追踪记录全部修订、阻断与签署依据，当前可执行结论可查询。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .errors import Forbidden, InvalidState
from .jsonio import load_json
from .service import DISCIPLINE_ROLES, ROLE_LABELS, SiteReviewService
from .storage import connect, inspect_schema


ACTORS = {
    "forester": "forester-1",
    "ecologist": "ecologist-1",
    "engineer": "engineer-1",
    "publisher": "publisher-1",
    "auditor": "auditor-1",
}


def _build_foundation(service: SiteReviewService, demo: dict) -> dict[str, dict[str, int]]:
    """登记用户、协议、边界、评估对象、批次和证据，返回每条证据的 item_id。"""

    for role in (*DISCIPLINE_ROLES, "publisher", "auditor"):
        service.create_user(ACTORS[role], ROLE_LABELS[role], role)

    for protocol in demo["protocols"]:
        service.publish_survey_protocol(ACTORS[protocol["discipline"]], protocol)

    for boundary in demo["boundaries"]:
        service.register_spatial_boundary(
            ACTORS[boundary["discipline"]], boundary["boundary_id"], boundary["discipline"],
            boundary["geometry"], boundary["crs"],
        )

    for subject in demo["subjects"]:
        service.create_assessment_subject(
            ACTORS[subject["discipline_scope"]], subject["subject_id"], subject["code"],
            subject["title"], subject["discipline_scope"],
        )

    item_ids: dict[str, dict[str, int]] = {}
    for batch_id, batch in demo["evidence"].items():
        discipline = batch["discipline"]
        service.register_evidence_batch(
            ACTORS[discipline], batch_id, discipline, batch["protocol_id"],
            batch["protocol_version"], batch["collected_by"], batch["collected_at"],
        )
        imported = service.add_evidence_items(
            ACTORS[discipline], batch_id, f"import-{batch_id}", batch["items"],
        )
        item_ids[batch_id] = {
            item["source_ref"]: item_id
            for item, item_id in zip(batch["items"], imported["item_ids"])
        }
    return item_ids


def _refs(batch_id: str, item_ids: dict[str, int]) -> list[dict]:
    return [{"batch_id": batch_id, "item_id": item_id} for item_id in item_ids.values()]


def _create_and_sign(service: SiteReviewService, subject_id: str, discipline: str,
                     protocol_id: str, boundary_id: str, batch_id: str,
                     item_ids: dict[str, int], chain: list[dict], summary: dict,
                     notes: dict[str, str]) -> str:
    draft = service.create_conclusion_draft(
        ACTORS[discipline], subject_id, discipline, protocol_id, 1, boundary_id,
        _refs(batch_id, item_ids), chain, summary,
    )
    version_id = draft["version_id"]
    for role in DISCIPLINE_ROLES:
        service.sign(ACTORS[role], version_id, notes.get(role, ""))
    return version_id


def run(workspace: Path) -> dict[str, object]:
    demo = load_json(workspace / "fixtures" / "site_review_demo.json")
    chain = demo["signoff_chain"]
    with tempfile.TemporaryDirectory(prefix="site-review-") as temporary:
        database = Path(temporary) / "site_review.sqlite3"
        connection = connect(database)
        try:
            service = SiteReviewService(connection)
            item_ids = _build_foundation(service, demo)

            # ---- 情景 A：鸟类繁殖地结论完整会签并发布 ---------------------
            bird_version = _create_and_sign(
                service, "site-bird", "ecologist", "bird-breeding", "bird-zone-a",
                "bird-batch-1", item_ids["bird-batch-1"], chain,
                {"recommendation": "conditional", "buffer_m": 200,
                 "construction_window": "2026-08 至 2027-02"},
                {"forester": "林地边界无异议", "ecologist": "保留 200 米缓冲带",
                 "engineer": "施工窗口可行"},
            )
            bird_published = service.publish(ACTORS["publisher"], bird_version)
            if bird_published["state"] != "published":
                raise RuntimeError("鸟类结论未成功发布")
            # 重复发布不产生第二份决定。
            try:
                service.publish(ACTORS["publisher"], bird_version)
                raise RuntimeError("重复发布未被拒绝")
            except InvalidState:
                pass
            # 重复签署不产生第二份决定。
            replay = service.sign(ACTORS["ecologist"], bird_version, "再次签署")
            if not replay.get("replayed"):
                raise RuntimeError("重复签署产生了第二份决定")
            signoff_count = connection.execute(
                "SELECT count(*) FROM signoffs WHERE version_id=?", (bird_version,)
            ).fetchone()[0]
            if signoff_count != 3:
                raise RuntimeError("已发布版本签署行数异常")

            # ---- 情景 B：排水草案签署中，证据被补充，草案阻断 ---------------
            drain_draft = service.create_conclusion_draft(
                ACTORS["engineer"], "site-drain", "engineer", "rain-drainage", 1,
                "drain-line-a", _refs("drain-batch-1", item_ids["drain-batch-1"]),
                chain, {"recommendation": "pass", "capacity_margin": 0.27},
            )
            drain_v1 = drain_draft["version_id"]
            service.sign(ACTORS["forester"], drain_v1, "坡面植被恢复措施已安排")
            service.sign(ACTORS["ecologist"], drain_v1, "与繁殖地缓冲带不冲突")
            # 工程方尚未签署时，水文组补充了一批新证据。
            supplemented = service.add_evidence_items(
                ACTORS["engineer"], "drain-batch-1", "import-drain-batch-1-supplement",
                demo["supplement"]["drain-batch-1"],
            )
            new_item_ids = {
                item["source_ref"]: item_id
                for item, item_id in zip(
                    demo["supplement"]["drain-batch-1"], supplemented["item_ids"]
                )
            }
            if drain_v1 not in supplemented["blocked_versions"]:
                raise RuntimeError("补充证据未阻断在途草案")
            blocked_view = service.get_version(ACTORS["auditor"], drain_v1)
            if blocked_view["state"] != "blocked":
                raise RuntimeError("排水 v1 未进入 blocked 状态")
            if any(signoff["status"] != "invalidated" for signoff in blocked_view["signoffs"]):
                raise RuntimeError("阻断后仍有有效签署")
            # 阻断版本既不能继续签署也不能发布。
            try:
                service.sign(ACTORS["engineer"], drain_v1, "试图补签")
                raise RuntimeError("阻断版本仍可签署")
            except InvalidState:
                pass
            try:
                service.publish(ACTORS["publisher"], drain_v1)
                raise RuntimeError("阻断版本仍可发布")
            except InvalidState:
                pass

            # ---- 情景 C：基于完整证据另立 v2 并发布 ------------------------
            merged_ids = {**item_ids["drain-batch-1"], **new_item_ids}
            drain_v2 = _create_and_sign(
                service, "site-drain", "engineer", "rain-drainage", "drain-line-a",
                "drain-batch-1", merged_ids, chain,
                {"recommendation": "conditional", "capacity_margin": -0.01,
                 "measure": "增设一道横向截水沟"},
                {"forester": "截水沟配套植被恢复", "ecologist": "避开繁殖期施工",
                 "engineer": "按 20 年一遇校核通过"},
            )
            service.publish(ACTORS["publisher"], drain_v2)

            # ---- 情景 D：已发布鸟类结论的证据事后失效，归档不可变 -----------
            nest_item = item_ids["bird-batch-1"]["P-02"]
            invalidated = service.invalidate_evidence_item(
                ACTORS["ecologist"], nest_item, "巢位记录复核发现坐标错位"
            )
            if invalidated["blocked_versions"]:
                raise RuntimeError("证据失效不应阻断已发布版本")
            bird_after = service.get_version(ACTORS["auditor"], bird_version)
            if bird_after["state"] != "published" or bird_after["publication"] is None:
                raise RuntimeError("已发布版本被证据失效改写")
            if any(s["status"] != "valid" for s in bird_after["signoffs"]):
                raise RuntimeError("已发布版本的签署被改写")
            publication = connection.execute(
                "SELECT signoff_fingerprint FROM conclusion_publications WHERE version_id=?",
                (bird_version,),
            ).fetchone()
            if publication is None or len(publication["signoff_fingerprint"]) != 64:
                raise RuntimeError("发布归档指纹缺失")

            # ---- 情景 E：顺序、回避与越权约束 -------------------------------
            veg_draft = service.create_conclusion_draft(
                ACTORS["forester"], "site-veg", "forester", "understory-veg", 1,
                "veg-plot-a", _refs("veg-batch-1", item_ids["veg-batch-1"]),
                chain, {"recommendation": "pass", "native_ratio_min": 0.81},
            )
            veg_v1 = veg_draft["version_id"]
            try:  # 生态角色不能抢在林业之前签署。
                service.sign(ACTORS["ecologist"], veg_v1, "越权签署")
                raise RuntimeError("未按职责顺序签署未被拒绝")
            except Forbidden:
                pass
            service.sign(ACTORS["forester"], veg_v1, "林下植被指标达标")
            service.declare_conflict(
                ACTORS["ecologist"], veg_v1, ACTORS["ecologist"],
                "其配偶参与该样方外业，自行声明回避",
            )
            try:  # 已登记回避的签署人不能签署。
                service.sign(ACTORS["ecologist"], veg_v1, "试图签署")
                raise RuntimeError("回避关系未阻止签署")
            except Forbidden:
                pass

            # ---- 情景 F：当前可执行结论与审计追踪 ---------------------------
            bird_current = service.current_conclusion(ACTORS["auditor"], "site-bird")
            if bird_current["executable"]["version_id"] != bird_version:
                raise RuntimeError("当前可执行鸟类结论查询错误")
            drain_current = service.current_conclusion(ACTORS["auditor"], "site-drain")
            if drain_current["executable"]["version_id"] != drain_v2:
                raise RuntimeError("当前可执行排水结论不是最新归档版本")
            drain_versions = service.list_versions(ACTORS["auditor"], "site-drain")
            if [v["state"] for v in drain_versions["versions"]] != ["blocked", "published"]:
                raise RuntimeError("排水版本历史状态序列错误")
            bird_trail = service.audit_trail(ACTORS["auditor"], version_id=bird_version)
            event_types = [event["event_type"] for event in bird_trail["events"]]
            for expected in ("conclusion.drafted", "conclusion.signed", "conclusion.published"):
                if expected not in event_types:
                    raise RuntimeError(f"审计缺少事件: {expected}")
            drain_trail = service.audit_trail(ACTORS["auditor"], subject_id="site-drain")
            if "conclusion.blocked" not in [e["event_type"] for e in drain_trail["events"]]:
                raise RuntimeError("审计无法追溯阻断依据")
            if "signoff.invalidated" not in [e["event_type"] for e in drain_trail["events"]]:
                raise RuntimeError("审计无法追溯签署失效依据")
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "published": {"bird": bird_version, "drain": drain_v2},
        "blocked_versions": [v for v in supplemented["blocked_versions"]],
        "bird_events": len(bird_trail["events"]),
        "drain_events": len(drain_trail["events"]),
        "audit_event_total": len(bird_trail["events"]) + len(drain_trail["events"]),
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行选址会签平台的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
