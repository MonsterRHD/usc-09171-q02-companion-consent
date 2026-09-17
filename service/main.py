"""同意与响应中枢 HTTP 入口（标准库实现，无第三方依赖）。

路由概览：
  GET  /health
  POST /admin/consents                       创建家庭成员/儿童监护/临时访客授权
  POST /admin/consents/{id}/versions         追加授权版本（调整设置）
  POST /admin/consents/{id}/revoke           撤回授权（新个性化处理立即停止）
  POST /admin/devices/{id}/transfer          设备转借（终止原家庭全部授权）
  POST /events/batch                         设备批量事件回传（契约同 contracts/）
  GET  /events/{event_id}/explain            客服脱敏解释（X-Role: support）
  POST /escalations/{case_id}/decision       人工确认升级流程
  GET  /escalations                          升级案件列表（X-Role: safety_officer）
  POST /privacy/deletions                    删除可删资料，必留最小事实单列
  GET  /guardians/{guardian_id}/export       监护人导出权限内记录
  GET  /audit/facts                          安全审计最小事实清单
  GET  /notifications                        通知去重记录
"""

from __future__ import annotations

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .consent import ConsentError, ConsentManager
from .engine import Engine
from .privacy import PrivacyService
from .store import Store


class App:
    def __init__(self):
        self.store = Store(os.getenv("CONSENT_DB", ":memory:"))
        self.consents = ConsentManager(self.store)
        self.engine = Engine(self.store, self.consents)
        self.privacy = PrivacyService(self.store)


APP = App()


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_json(handler: BaseHTTPRequestHandler):
    length = int(handler.headers.get("Content-Length") or 0)
    if not length:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw.decode())


REQUIRED_EVENT_FIELDS = ("event_id", "device_id", "pairing_id", "sequence", "captured_at")


class Handler(BaseHTTPRequestHandler):
    # ---------- GET ----------

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if path == "/health":
                _json_response(self, 200, {"status": "ok", "service": "companion-consent"})
            elif path.startswith("/events/") and path.endswith("/explain"):
                self._support_explain(path.split("/")[2])
            elif path == "/escalations":
                self._require_role("safety_officer")
                status = query.get("status", [None])[0]
                _json_response(self, 200, [dict(r) for r in APP.store.list_escalations(status)])
            elif path == "/audit/facts":
                self._require_role("safety_officer")
                _json_response(self, 200, [dict(r) for r in APP.store.list_facts()])
            elif path == "/notifications":
                self._require_role("safety_officer")
                _json_response(self, 200, [dict(r) for r in APP.store.list_notifications()])
            elif re.match(r"^/guardians/[^/]+/export$", path):
                guardian_id = path.split("/")[2]
                self._require_role("guardian", guardian_id)
                household_id = query.get("household_id", [""])[0]
                _json_response(self, 200,
                               APP.privacy.guardian_export(guardian_id, household_id))
            else:
                _json_response(self, 404, {"error": "not_found"})
        except PermissionError as exc:
            _json_response(self, 403, {"error": "forbidden", "detail": str(exc)})
        except KeyError as exc:
            _json_response(self, 404, {"error": "not_found", "detail": str(exc)})
        except Exception as exc:  # noqa: BLE001 - 统一错误契约
            _json_response(self, 400, {"error": type(exc).__name__, "detail": str(exc)})

    # ---------- POST ----------

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/admin/consents":
                self._require_role("admin")
                _json_response(self, 201, self._create_consent(_read_json(self)))
            elif re.match(r"^/admin/consents/[^/]+/versions$", path):
                self._require_role("admin")
                consent_id = path.split("/")[3]
                _json_response(self, 201,
                               APP.consents.append_version(consent_id, **_read_json(self)))
            elif re.match(r"^/admin/consents/[^/]+/revoke$", path):
                self._require_role("admin")
                consent_id = path.split("/")[3]
                body = _read_json(self)
                APP.consents.revoke(consent_id, body.get("at"))
                _json_response(self, 200, {"consent_id": consent_id, "revoked": True})
            elif re.match(r"^/admin/devices/[^/]+/transfer$", path):
                self._require_role("admin")
                device_id = path.split("/")[3]
                body = _read_json(self)
                count = APP.consents.transfer_device(device_id, body.get("at"))
                _json_response(self, 200,
                               {"device_id": device_id, "terminated_consents": count})
            elif path == "/events/batch":
                payload = _read_json(self)
                events = payload["events"] if isinstance(payload, dict) else payload
                self._validate_events(events)
                results = APP.engine.ingest_batch(events)
                _json_response(self, 200, {"results": results})
            elif re.match(r"^/escalations/[^/]+/decision$", path):
                self._require_role("safety_officer")
                case_id = path.split("/")[2]
                body = _read_json(self)
                out = APP.engine.decide_escalation(
                    case_id, body["status"], body.get("by", "safety_officer"))
                _json_response(self, 200, out)
            elif path == "/privacy/deletions":
                body = _read_json(self)
                self._require_role("admin")
                result = APP.privacy.delete_subject_data(
                    body["scope_type"], body["subject_id"], body["requested_by"])
                _json_response(self, 200, result)
            else:
                _json_response(self, 404, {"error": "not_found"})
        except PermissionError as exc:
            _json_response(self, 403, {"error": "forbidden", "detail": str(exc)})
        except (ConsentError, ValueError, KeyError, TypeError) as exc:
            _json_response(self, 400, {"error": type(exc).__name__, "detail": str(exc)})
        except Exception as exc:  # noqa: BLE001
            _json_response(self, 500, {"error": type(exc).__name__, "detail": str(exc)})

    # ---------- helpers ----------

    def _support_explain(self, event_id: str) -> None:
        self._require_role("support")
        view = APP.privacy.support_explain_event(event_id)
        if view is None:
            raise KeyError(event_id)
        _json_response(self, 200, view)

    def _require_role(self, role: str, identity: str | None = None) -> None:
        if self.headers.get("X-Role") != role:
            raise PermissionError(f"requires role {role}")
        if identity and self.headers.get("X-Identity") != identity:
            raise PermissionError("cannot access another guardian's records")

    def _create_consent(self, body: dict) -> dict:
        return APP.consents.create(
            body["scope_type"],
            body["device_id"],
            body["subject_id"],
            body["household_id"],
            guardian_id=body.get("guardian_id"),
            allowed_actions=body.get("allowed_actions"),
            retention_mood=body.get("retention_mood", "summary"),
            retention_risk=body.get("retention_risk", "minimal"),
            voice_summary=body.get("voice_summary", True),
            safety_minimum=body.get("safety_minimum", True),
            expires_at=body.get("expires_at"),
            effective_from=body.get("effective_from"),
            granted_at=body.get("granted_at"),
            consent_id=body.get("consent_id"),
        )

    @staticmethod
    def _validate_events(events) -> None:
        if not isinstance(events, list) or not events:
            raise ValueError("events must be a non-empty list")
        seen = set()
        for e in events:
            for f in REQUIRED_EVENT_FIELDS:
                if f not in e:
                    raise ValueError(f"event missing field: {f}")
            key = (e["event_id"], e["pairing_id"], e["sequence"])
            if key in seen:
                raise ValueError(f"duplicate event inside batch: {e['event_id']}")
            seen.add(key)
            if not isinstance(e.get("signals", {}), dict):
                raise ValueError("signals must be an object")

    def log_message(self, _format, *_args):
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()
