import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .hub import ConsentHub

HUB = None


def get_hub():
    global HUB
    if HUB is None:
        HUB = ConsentHub(store_path=os.getenv("STORE_PATH", "data/store.json"))
    return HUB


def _query(query, key):
    values = query.get(key)
    return values[0] if values else None


def _health(hub, match, query, body):
    return 200, {"status": "ok", "service": "companion-consent"}


def _grant_consent(hub, match, query, body):
    return 201, hub.grant_consent(body or {})


def _revoke_consent(hub, match, query, body):
    chain = hub.revoke_consent(match.group("consent_id"))
    if chain is None:
        return 404, {"error": {"code": "not_found", "message": "授权不存在"}}
    return 200, {"consent_id": match.group("consent_id"), "versions": chain}


def _list_consents(hub, match, query, body):
    return 200, {
        "consents": hub.list_consents(
            device_id=_query(query, "device_id"),
            pairing_id=_query(query, "pairing_id"),
        )
    }


def _ingest_events(hub, match, query, body):
    events = body.get("events") if isinstance(body, dict) else body
    if not isinstance(events, list):
        raise ValueError("请求体必须是事件数组或包含 events 数组的对象")
    return 200, hub.ingest_batch(events)


def _get_disposition(hub, match, query, body):
    found = hub.get_disposition(match.group("event_id"))
    if found is None:
        return 404, {"error": {"code": "not_found", "message": "处置记录不存在"}}
    return 200, found


def _support_explanation(hub, match, query, body):
    view = hub.support_explanation(match.group("event_id"))
    if view is None:
        return 404, {"error": {"code": "not_found", "message": "处置记录不存在"}}
    return 200, view


def _confirm_escalation(hub, match, query, body):
    escalation = hub.confirm_escalation(
        match.group("escalation_id"), (body or {}).get("confirmed_by")
    )
    if escalation is None:
        return 404, {"error": {"code": "not_found", "message": "升级单不存在"}}
    return 200, escalation


def _list_escalations(hub, match, query, body):
    return 200, {
        "escalations": hub.list_escalations(
            device_id=_query(query, "device_id"), status=_query(query, "status")
        )
    }


def _transfer_device(hub, match, query, body):
    return 200, hub.transfer_device(
        match.group("device_id"), new_pairing_id=(body or {}).get("new_pairing_id")
    )


def _start_deletion(hub, match, query, body):
    body = body or {}
    return 201, hub.start_deletion(
        match.group("device_id"), body.get("pairing_id"), body.get("requested_by")
    )


def _get_deletion(hub, match, query, body):
    job = hub.get_deletion(match.group("job_id"))
    if job is None:
        return 404, {"error": {"code": "not_found", "message": "删除任务不存在"}}
    return 200, job


def _guardian_export(hub, match, query, body):
    return 200, hub.guardian_export(match.group("guardian_id"))


def _list_notifications(hub, match, query, body):
    return 200, {
        "notifications": hub.list_notifications(
            device_id=_query(query, "device_id"),
            pairing_id=_query(query, "pairing_id"),
        )
    }


ROUTES = [
    ("GET", re.compile(r"/health"), _health),
    ("POST", re.compile(r"/v1/consents"), _grant_consent),
    ("POST", re.compile(r"/v1/consents/(?P<consent_id>[^/]+)/revoke"), _revoke_consent),
    ("GET", re.compile(r"/v1/consents"), _list_consents),
    ("POST", re.compile(r"/v1/events:batch"), _ingest_events),
    ("GET", re.compile(r"/v1/dispositions/(?P<event_id>[^/]+)"), _get_disposition),
    ("GET", re.compile(r"/v1/support/dispositions/(?P<event_id>[^/]+)"), _support_explanation),
    ("POST", re.compile(r"/v1/escalations/(?P<escalation_id>[^/]+)/confirm"), _confirm_escalation),
    ("GET", re.compile(r"/v1/escalations"), _list_escalations),
    ("POST", re.compile(r"/v1/devices/(?P<device_id>[^/]+)/transfer"), _transfer_device),
    ("POST", re.compile(r"/v1/devices/(?P<device_id>[^/]+)/deletions"), _start_deletion),
    ("GET", re.compile(r"/v1/deletions/(?P<job_id>[^/]+)"), _get_deletion),
    ("GET", re.compile(r"/v1/guardians/(?P<guardian_id>[^/]+)/export"), _guardian_export),
    ("GET", re.compile(r"/v1/notifications"), _list_notifications),
]


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        body = None
        if method == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw)
                except json.JSONDecodeError:
                    return self._reply(400, {"error": {"code": "bad_json", "message": "请求体不是合法 JSON"}})
        hub = get_hub()
        for route_method, pattern, handler in ROUTES:
            if route_method != method:
                continue
            match = pattern.fullmatch(parsed.path)
            if not match:
                continue
            try:
                status, payload = handler(hub, match, parse_qs(parsed.query), body)
            except ValueError as exc:
                status, payload = 400, {"error": {"code": "invalid_request", "message": str(exc)}}
            return self._reply(status, payload)
        self._reply(404, {"error": {"code": "not_found", "message": "路径不存在"}})

    def _reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", int(os.getenv("PORT", "8080"))), Handler).serve_forever()
