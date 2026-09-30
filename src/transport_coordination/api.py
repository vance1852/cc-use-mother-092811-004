"""提供不依赖第三方框架的 HTTP/JSON 边界。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from .errors import DomainError, ValidationError
from .service import DomainService
from .storage import Database
from .timetable_service import TimetableService


def _receipt_response(receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def _route_timetable(service: TimetableService, method: str, segments: list[str],
                     query: dict[str, list[str]], body: dict[str, Any],
                     actor_id: str) -> tuple[int, dict[str, Any]]:
    """分派山区慢火车公共服务运行图接口。"""

    writes = {
        "commitments": service.register_commitment,
        "calendar-entries": service.register_calendar_entry,
        "calendar-retractions": service.retract_calendar_entry,
        "plans": service.create_plan,
        "plan-confirmations": service.confirm_plan,
        "plan-activations": service.activate_plan,
        "blockades": service.register_blockade,
        "restrictions": service.register_restriction,
        "subsidy-agreements": service.register_subsidy_agreement,
        "goods-acceptances": service.accept_goods,
        "changes": service.create_change,
        "ridership-observations": service.record_ridership,
        "coverage-freezes": service.freeze_coverage,
        "evaluation-rounds": service.create_evaluation_round,
    }
    if method == "POST" and len(segments) == 2 and segments[1] in writes:
        return _receipt_response(writes[segments[1]](actor_id=actor_id, **body))
    if method == "GET" and len(segments) == 2 and segments[1] == "adopted":
        site_id = query.get("site_id", [""])[0]
        date = query.get("date", [""])[0]
        if not site_id or not date:
            raise ValidationError("site_id 与 date 不能为空")
        return 200, service.timetable_on(site_id, date)
    if method == "GET" and len(segments) == 2 and segments[1] == "unmet":
        site_id = query.get("site_id", [""])[0]
        date = query.get("date", [""])[0]
        if not site_id or not date:
            raise ValidationError("site_id 与 date 不能为空")
        return 200, service.unmet_on(site_id, date)
    if method == "GET" and len(segments) == 2 and segments[1] == "changes":
        site_id = query.get("site_id", [""])[0]
        if not site_id:
            raise ValidationError("site_id 不能为空")
        date = query.get("date", [None])[0]
        return 200, {"items": service.list_changes(site_id, date)}
    if method == "GET" and len(segments) == 3 and segments[1] == "coverage-freezes":
        return 200, service.get_snapshot(segments[2])
    if method == "GET" and len(segments) == 4 and segments[1] == "coverage-freezes" \
            and segments[3] == "recompute":
        return 200, service.recompute_snapshot(segments[2])
    if method == "GET" and len(segments) == 3 and segments[1] == "evaluation-rounds":
        return 200, service.get_round(segments[2])
    return 404, {"error": "route_not_found", "message": "接口不存在"}


def route(service: DomainService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None,
          timetable: TimetableService | None = None) -> tuple[int, dict[str, Any]]:
    """把一个 HTTP 语义请求分派到领域服务。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    actor_id = headers.get("X-Actor-Id", "")
    try:
        segments = [segment for segment in parsed.path.split("/") if segment]
        if segments and segments[0] == "timetable" and timetable is not None:
            query = parse_qs(parsed.query)
            return _route_timetable(timetable, method, segments, query, body, actor_id)
        if method == "GET" and parsed.path == "/health":
            valid, count = service.verify_audit()
            return 200, {"status": "ok", "audit_valid": valid, "audit_events": count}
        if method == "POST" and parsed.path == "/organizations":
            receipt = service.register_organization(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/actors":
            receipt = service.register_actor(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/sites":
            receipt = service.register_site(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "POST" and parsed.path == "/domain-records":
            receipt = service.record_domain_data(actor_id=actor_id, **body)
            return 200 if receipt.replayed else 201, receipt.__dict__
        if method == "GET" and parsed.path == "/domain-records":
            query = parse_qs(parsed.query)
            site_id = query.get("site_id", [""])[0]
            if not site_id:
                raise ValidationError("site_id 不能为空")
            category = query.get("category", [None])[0]
            return 200, {"items": [item.__dict__ for item in service.list_domain_data(site_id, category)]}
        if method == "GET" and parsed.path == "/audit-events":
            query = parse_qs(parsed.query)
            after = int(query.get("after_sequence", ["0"])[0])
            return 200, {"items": service.audit_events(after)}
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为路由调用。"""

    service: DomainService
    timetable: TimetableService | None = None

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")},
                                timetable=self.timetable)
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动本地 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动技能赛训协作基础服务")
    parser.add_argument("--database", default="service.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = DomainService(database)
    Handler.timetable = TimetableService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
