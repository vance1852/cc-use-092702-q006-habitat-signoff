"""选址综合结论会签服务的无第三方依赖 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

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
    ) -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}

            if method == "POST" and path == "/users":
                result = self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]
                )
                return Response(201, result)

            if method == "POST" and path == "/evidence_protocols":
                return Response(201, self.service.register_protocol(self._actor(normalized_headers), payload))

            if method == "POST" and path == "/evidence_batches":
                result = self.service.register_batch(
                    self._actor(normalized_headers), payload["batch_id"], payload["protocol_id"],
                    int(payload["protocol_version"]), payload["manifest"], payload.get("note", ""),
                )
                return Response(201, result)
            if (
                method == "POST" and len(parts) == 3
                and parts[0] == "evidence_batches" and parts[2] == "revisions"
            ):
                result = self.service.revise_batch(
                    self._actor(normalized_headers), parts[1], payload["manifest"],
                    payload["change_kind"], payload["note"],
                )
                return Response(201, result)
            if method == "GET" and len(parts) == 2 and parts[0] == "evidence_batches":
                return Response(200, self.service.get_batch(parts[1]))
            if (
                method == "GET" and len(parts) == 3
                and parts[0] == "evidence_batches" and parts[2] == "audit"
            ):
                return Response(200, self.service.batch_audit_trail(self._actor(normalized_headers), parts[1]))

            if method == "POST" and path == "/conclusions":
                result = self.service.create_conclusion_version(
                    self._actor(normalized_headers), payload["subject_id"], payload["title"],
                    payload["boundary"], payload["evidence_batches"],
                )
                return Response(201, result)
            if method == "GET" and len(parts) == 3 and parts[0] == "subjects" and parts[2] == "versions":
                return Response(200, self.service.list_versions(self._actor(normalized_headers), parts[1]))
            if (
                method == "GET" and len(parts) == 4
                and parts[0] == "subjects" and parts[2] == "versions"
            ):
                return Response(200, self.service.get_version(parts[1], int(parts[3])))
            if (
                method == "POST" and len(parts) == 5
                and parts[0] == "subjects" and parts[2] == "versions" and parts[4] == "sign"
            ):
                result = self.service.sign(
                    self._actor(normalized_headers), parts[1], int(parts[3]), payload["basis_summary"]
                )
                return Response(200, result)
            if (
                method == "POST" and len(parts) == 5
                and parts[0] == "subjects" and parts[2] == "versions" and parts[4] == "recusals"
            ):
                result = self.service.declare_recusal(
                    self._actor(normalized_headers), parts[1], int(parts[3]),
                    payload["user_id"], payload["reason"],
                )
                return Response(201, result)
            if (
                method == "POST" and len(parts) == 5
                and parts[0] == "subjects" and parts[2] == "versions" and parts[4] == "publish"
            ):
                result = self.service.publish(self._actor(normalized_headers), parts[1], int(parts[3]))
                return Response(200, result)
            if (
                method == "GET" and len(parts) == 3
                and parts[0] == "subjects" and parts[2] == "current"
            ):
                return Response(200, self.service.current_conclusion(self._actor(normalized_headers), parts[1]))
            if (
                method == "GET" and len(parts) == 3
                and parts[0] == "subjects" and parts[2] == "audit"
            ):
                return Response(200, self.service.audit_trail(self._actor(normalized_headers), parts[1]))
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
    parser = argparse.ArgumentParser(description="启动选址综合结论会签 HTTP 服务")
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
