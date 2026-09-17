import http.client
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone

from service import main
from service.hub import ConsentHub

BASE = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)


class ApiSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.hub = ConsentHub(
            store_path=f"{cls.tmp.name}/store.json", clock=lambda: BASE
        )
        main.HUB = cls.hub
        cls.server = main.ThreadingHTTPServer(("127.0.0.1", 0), main.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        main.HUB = None

    def request(self, method, path, payload=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"} if body else {}
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read())
        conn.close()
        return response.status, data

    def test_health(self):
        status, data = self.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_consent_to_disposition_flow(self):
        status, consent = self.request("POST", "/v1/consents", {
            "consent_id": "c-api",
            "device_id": "orbit-08",
            "pairing_id": "pair-api",
            "subject_type": "child",
            "subject_id": "child-api",
            "granted_by": "guardian-api",
            "permissions": {
                "retention": "full",
                "actions": ["breathing_light"],
                "personalization": True,
            },
            "effective_from": "2026-09-12T00:00:00+08:00",
        })
        self.assertEqual(status, 201)
        self.assertEqual(consent["version"], 1)

        status, result = self.request("POST", "/v1/events:batch", {
            "events": [{
                "event_id": "evt-api-1",
                "device_id": "orbit-08",
                "pairing_id": "pair-api",
                "sequence": 1,
                "captured_at": "2026-09-12T14:00:03+08:00",
                "signals": {"mood": "low", "confidence": 0.72},
            }]
        })
        self.assertEqual(status, 200)
        self.assertEqual(result["results"][0]["status"], "processed")

        status, found = self.request("GET", "/v1/dispositions/evt-api-1")
        self.assertEqual(status, 200)
        self.assertEqual(found["disposition"]["outcome"], "action_executed")

        status, view = self.request("GET", "/v1/support/dispositions/evt-api-1")
        self.assertEqual(status, 200)
        self.assertNotIn("child-api", json.dumps(view, ensure_ascii=False))

        status, export = self.request("GET", "/v1/guardians/guardian-api/export")
        self.assertEqual(status, 200)
        self.assertEqual(len(export["dispositions"]), 1)

        status, _ = self.request("GET", "/v1/notifications?device_id=orbit-08")
        self.assertEqual(status, 200)

    def test_unknown_routes_and_bad_body(self):
        status, _ = self.request("GET", "/v1/unknown")
        self.assertEqual(status, 404)
        status, _ = self.request("GET", "/v1/dispositions/evt-missing")
        self.assertEqual(status, 404)
        status, _ = self.request("POST", "/v1/consents", {"device_id": "x"})
        self.assertEqual(status, 400)

        conn = http.client.HTTPConnection("127.0.0.1", self.port)
        conn.request("POST", "/v1/events:batch", body="{not json",
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        self.assertEqual(response.status, 400)
        conn.close()


if __name__ == "__main__":
    unittest.main()
