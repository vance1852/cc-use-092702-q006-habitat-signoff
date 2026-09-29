"""选址综合结论会签完整产品流程的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from .errors import Conflict, InvalidState
from .jsonio import load_json
from .service import SiteReviewService
from .storage import connect, inspect_schema


def _load_json(path: Path) -> dict:
    return load_json(path)


def run(workspace: Path) -> dict[str, object]:
    fixtures = workspace / "fixtures" / "site_review"
    forestry_protocol = _load_json(fixtures / "forestry_protocol.json")
    ecology_protocol = _load_json(fixtures / "ecology_protocol.json")
    engineering_protocol = _load_json(fixtures / "engineering_protocol.json")
    demo = _load_json(fixtures / "demo_conclusion.json")
    boundary = demo["boundary"]
    manifests = demo["manifests"]

    with tempfile.TemporaryDirectory(prefix="site-review-") as temporary:
        database = Path(temporary) / "site_review.sqlite3"
        connection = connect(database)
        try:
            service = SiteReviewService(connection)
            for user_id, name, role in (
                ("coordinator-1", "选址协调人", "coordinator"),
                ("forester-1", "林业调查负责人甲", "forester"),
                ("forester-2", "林业调查负责人乙", "forester"),
                ("ecologist-1", "鸟类生态负责人", "ecologist"),
                ("engineer-1", "排水工程负责人甲", "engineer"),
                ("engineer-2", "排水工程负责人乙", "engineer"),
                ("auditor-1", "审计人员", "auditor"),
            ):
                service.create_user(user_id, name, role)

            # 三个专业域分别冻结各自的调查协议与证据批次。
            service.register_protocol("forester-1", forestry_protocol)
            service.register_protocol("ecologist-1", ecology_protocol)
            service.register_protocol("engineer-1", engineering_protocol)
            service.register_batch(
                "forester-1", "batch-forestry", "forest-understory-v1", 1,
                manifests["forestry"], "雨季前景观林下植被首轮样方调查",
            )
            service.register_batch(
                "ecologist-1", "batch-ecology", "bird-breeding-v1", 1,
                manifests["ecology"], "繁殖季定点监听与巢位核查",
            )
            service.register_batch(
                "engineer-1", "batch-engineering", "rain-drainage-v1", 1,
                manifests["engineering"], "雨季初期断面与洪峰测算",
            )

            evidence_selection = [
                {"domain": "forestry", "batch_id": "batch-forestry"},
                {"domain": "ecology", "batch_id": "batch-ecology"},
                {"domain": "engineering", "batch_id": "batch-engineering"},
            ]

            # v1：林业、生态已签；工程证据随后补充，v1 必须被级联阻断。
            service.create_conclusion_version(
                "coordinator-1", "north-ridge-platform", "北脊观景平台选址综合结论",
                boundary, evidence_selection,
            )
            service.sign("forester-1", "north-ridge-platform", 1, "林下植被覆盖度处于可接受区间，样方 Q03 有一株保护植物需挂牌避让。")
            service.sign("ecologist-1", "north-ridge-platform", 1, "P1 点位存在两处活跃鸟巢，建议平台基础西移 25 米并限制繁殖季施工。")
            revision = service.revise_batch(
                "engineer-1", "batch-engineering", manifests["engineering"] + [{
                    "transect": "T3", "peak_runoff_lps": "560", "culvert_capacity_lps": "600",
                    "slope_percent": "10.5", "inundation_area_m2": "40",
                }], "supplement", "雨季新增 T3 断面监测记录",
            )
            blocked = service.get_version("north-ridge-platform", 1)
            if blocked["state"] != "blocked" or blocked["valid_signature_count"] != 0:
                raise RuntimeError("证据补充后草案应被阻断且全部未发布签署失效")
            if revision["affected_versions"] != [{"subject_id": "north-ridge-platform", "version_no": 1}]:
                raise RuntimeError("证据修订必须返回受影响版本清单")
            try:
                service.sign("engineer-1", "north-ridge-platform", 1, "阻断后不应能签署")
                raise RuntimeError("阻断版本不能继续会签")
            except InvalidState:
                pass

            # v2：基于三域最新批次修订重新冻结、重新会签。
            service.create_conclusion_version(
                "coordinator-1", "north-ridge-platform", "北脊观景平台选址综合结论（含 T3 断面）",
                boundary, evidence_selection,
            )
            v2 = service.get_version("north-ridge-platform", 2)
            if v2["evidence_refs"][2]["batch_revision"] != 2:
                raise RuntimeError("v2 必须冻结工程批次的第 2 修订")
            service.sign("forester-1", "north-ridge-platform", 2, "同 v1 结论，避让措施纳入设计条件。")
            try:
                service.sign("forester-1", "north-ridge-platform", 2, "重复签署")
                raise RuntimeError("重复签署必须被拒绝")
            except Conflict:
                pass
            try:
                service.sign("engineer-1", "north-ridge-platform", 2, "工程不能抢在生态之前签署")
                raise RuntimeError("越序签署必须被拒绝")
            except InvalidState:
                pass
            service.sign("ecologist-1", "north-ridge-platform", 2, "巢位核查结论不变，西移与施工窗口条件维持。")
            service.sign("engineer-1", "north-ridge-platform", 2, "T2 断面洪峰超涵管能力，需扩涵至 600 l/s；T3 可控。")

            # 工程负责人甲随后暴露利益关联：其签署失效，由负责人乙补签。
            recusal = service.declare_recusal(
                "engineer-1", "north-ridge-platform", 2, "engineer-1",
                "配偶任职于平台施工候选单位，主动申报回避",
            )
            if len(recusal["invalidated_signatures"]) != 1:
                raise RuntimeError("回避申报必须失效当事人的有效签署")
            try:
                service.publish("coordinator-1", "north-ridge-platform", 2)
                raise RuntimeError("会签不完整时发布必须被原子拒绝")
            except InvalidState:
                pass
            service.sign("engineer-2", "north-ridge-platform", 2, "复核 T2/T3 测算法与糙率取值，同意扩涵方案。")

            # 原子发布：会签链、回避关系与证据完整性在单事务内全部确认。
            publication = service.publish("coordinator-1", "north-ridge-platform", 2)
            decision_no = publication["decision_no"]
            replay = service.publish("coordinator-1", "north-ridge-platform", 2)
            if replay["decision_no"] != decision_no or replay["idempotent_replay"] is not True:
                raise RuntimeError("重复发布必须返回同一决定且不产生第二份决定")
            publication_rows = connection.execute("SELECT count(*) FROM publications").fetchone()[0]
            if publication_rows != 1:
                raise RuntimeError("重复发布不能产生第二份决定行")

            # 发布后工程证据再次撤回：已归档版本与其决定不能被改写。
            service.revise_batch(
                "engineer-2", "batch-engineering", manifests["engineering"],
                "revocation", "T3 断面仪器超期未检定，补充记录撤回，恢复首轮两份断面",
            )
            archived = service.get_version("north-ridge-platform", 2)
            if archived["state"] != "published" or archived["publication"]["decision_no"] != decision_no:
                raise RuntimeError("已发布历史版本不能被证据撤回改写")
            if archived["valid_signature_count"] != 3:
                raise RuntimeError("已归档版本的签署必须保持有效")
            current = service.current_conclusion("auditor-1", "north-ridge-platform")
            if current["decision_no"] != decision_no:
                raise RuntimeError("当前可执行结论必须指向最新发布决定")

            listing = service.list_versions("auditor-1", "north-ridge-platform")
            states = [(item["version_no"], item["state"]) for item in listing["versions"]]
            if states != [(1, "blocked"), (2, "published")]:
                raise RuntimeError(f"版本谱系异常: {states}")

            trail = service.audit_trail("auditor-1", "north-ridge-platform")
            event_types = [event["event_type"] for event in trail["events"]]
            for expected in (
                "conclusion.version_created", "conclusion.signed", "conclusion.blocked",
                "recusal.declared", "publication.rejected", "conclusion.published",
            ):
                if expected not in event_types:
                    raise RuntimeError(f"审计轨迹缺少 {expected}")
            batch_trail = service.batch_audit_trail("auditor-1", "batch-engineering")
            batch_events = [event["event_type"] for event in batch_trail["events"]]
            if batch_events != ["evidence_batch.registered", "evidence_batch.revision_appended",
                                "evidence_batch.revision_appended"]:
                raise RuntimeError(f"证据批次修订轨迹异常: {batch_events}")
            schema = inspect_schema(connection)
        finally:
            connection.close()

    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "subject": "north-ridge-platform",
        "versions": states,
        "decision_no": decision_no,
        "freeze_sha256": publication["freeze_sha256"],
        "evidence_integrity_sha256": publication["evidence_integrity_sha256"],
        "signers": [item["signer_id"] for item in publication["record"]["signatures"]],
        "audit_event_count": len(trail["events"]),
        "batch_revision_events": len(batch_trail["events"]),
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行选址综合结论会签平台的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
