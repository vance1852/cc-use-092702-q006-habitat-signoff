"""无第三方依赖的 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse, parse_qs

from .errors import ServiceError, ValidationFailed
from .service import SiteReviewService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

    def __init__(self, service: SiteReviewService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ):
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        parts = [part for part in path.split("/") if part]
        service = self.service
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = lambda: self._actor(normalized_headers)

            if method == "POST" and path == "/users":
                return Response(201, service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]))
            if method == "POST" and path == "/survey_protocols":
                return Response(201, service.publish_survey_protocol(actor(), payload))
            if method == "POST" and path == "/spatial_boundaries":
                return Response(201, service.register_spatial_boundary(
                    actor(), payload["boundary_id"], payload["discipline"],
                    payload["geometry"], payload["crs"]))
            if method == "POST" and path == "/assessment_subjects":
                return Response(201, service.create_assessment_subject(
                    actor(), payload["subject_id"], payload["code"], payload["title"],
                    payload.get("discipline_scope", "")))
            if method == "POST" and path == "/evidence_batches":
                return Response(201, service.register_evidence_batch(
                    actor(), payload["batch_id"], payload["discipline"], payload["protocol_id"],
                    int(payload["protocol_version"]), payload["collected_by"],
                    payload["collected_at"], payload.get("note")))
            if method == "POST" and len(parts) == 3 and parts[0] == "evidence_batches" and parts[2] == "items":
                key = normalized_headers.get("idempotency-key", "").strip()
                if not key:
                    raise ValidationFailed("缺少 Idempotency-Key")
                return Response(200, service.add_evidence_items(
                    actor(), parts[1], key, payload.get("evidence_items", [])))
            if method == "POST" and len(parts) == 3 and parts[0] == "evidence_batches" and parts[2] == "withdraw":
                return Response(200, service.withdraw_evidence_batch(
                    actor(), parts[1], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "evidence_items" and parts[2] == "invalidate":
                return Response(200, service.invalidate_evidence_item(
                    actor(), int(parts[1]), payload["reason"]))
            if method == "POST" and path == "/conclusion_versions":
                return Response(201, service.create_conclusion_draft(
                    actor(), payload["subject_id"], payload["discipline"], payload["protocol_id"],
                    int(payload["protocol_version"]), payload["boundary_id"],
                    payload.get("evidence_refs", []), payload.get("signoff_chain", []),
                    payload.get("summary", {})))
            if method == "POST" and len(parts) == 3 and parts[0] == "conclusion_versions" and parts[2] == "conflicts":
                return Response(201, service.declare_conflict(
                    actor(), parts[1], payload["user_id"], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "conclusion_versions" and parts[2] == "sign":
                return Response(200, service.sign(actor(), parts[1], payload.get("note", "")))
            if method == "POST" and len(parts) == 3 and parts[0] == "conclusion_versions" and parts[2] == "publish":
                return Response(200, service.publish(actor(), parts[1]))
            if method == "GET" and len(parts) == 2 and parts[0] == "conclusion_versions":
                return Response(200, service.get_version(actor(), parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "assessment_subjects" and parts[2] == "versions":
                return Response(200, service.list_versions(actor(), parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "assessment_subjects" and parts[2] == "current":
                return Response(200, service.current_conclusion(actor(), parts[1]))
            if method == "GET" and path == "/audit_events":
                subject_id = query.get("subject_id", [None])[0]
                version_id = query.get("version_id", [None])[0]
                return Response(200, service.audit_trail(
                    actor(), subject_id=subject_id, version_id=version_id))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SiteReview/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动选址评估版本化会签 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("site_review.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    application = JsonApplication(SiteReviewService(connection))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
