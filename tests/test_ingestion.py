import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from service.hub import ConsentHub
from service.rules import RULES_VERSION

BASE = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)
CONTRACT_SAMPLE = Path(__file__).resolve().parent.parent / "contracts" / "device-events.json"


def make_event(event_id, sequence, captured_at, signals, device_id="orbit-08", pairing_id="pair-home-a"):
    return {
        "event_id": event_id,
        "device_id": device_id,
        "pairing_id": pairing_id,
        "sequence": sequence,
        "captured_at": captured_at,
        "signals": signals,
    }


class IngestionTest(unittest.TestCase):
    def setUp(self):
        self.clock = self._clock()
        self.hub = ConsentHub(clock=self.clock)
        self.hub.grant_consent({
            "consent_id": "consent-child-a",
            "device_id": "orbit-08",
            "pairing_id": "pair-home-a",
            "subject_type": "child",
            "subject_id": "child-a",
            "granted_by": "guardian-a",
            "permissions": {
                "retention": "full",
                "actions": ["breathing_light", "play_music"],
                "personalization": True,
            },
            "effective_from": (BASE - timedelta(days=1)).isoformat(),
        })

    def test_contract_sample_uses_event_time_consent(self):
        sample = json.loads(CONTRACT_SAMPLE.read_text(encoding="utf-8"))
        result = self.hub.ingest_batch(sample, received_at=BASE + timedelta(minutes=5))
        self.assertEqual([r["status"] for r in result["results"]], ["processed", "processed"])

        d71 = self.hub.get_disposition("evt-71")["disposition"]
        self.assertEqual(d71["classification"], "comfort")
        self.assertEqual(d71["outcome"], "action_executed")
        self.assertEqual(d71["action"], {"type": "breathing_light"})
        self.assertEqual(
            d71["consent_versions"],
            [{"consent_id": "consent-child-a", "version": 1, "subject_type": "child"}],
        )
        self.assertEqual(d71["rule_version"], RULES_VERSION)

        d72 = self.hub.get_disposition("evt-72")["disposition"]
        self.assertEqual(d72["classification"], "anomaly")
        self.assertEqual(d72["outcome"], "recorded")

    def test_duplicate_arrival_processed_once(self):
        event = make_event("evt-1", 1, BASE.isoformat(), {"mood": "low"})
        first = self.hub.ingest_batch([event])
        second = self.hub.ingest_batch([event])
        self.assertEqual(second["results"][0]["status"], "duplicate")
        self.assertEqual(
            first["results"][0]["disposition_id"], second["results"][0]["disposition_id"]
        )
        self.assertEqual(len(self.hub.list_dispositions()), 1)

    def test_conflicting_resend_does_not_overwrite(self):
        event = make_event("evt-1", 1, BASE.isoformat(), {"mood": "low"})
        self.hub.ingest_batch([event])
        tampered = dict(event, signals={"mood": "low", "confidence": 0.99})
        result = self.hub.ingest_batch([tampered])
        self.assertEqual(result["results"][0]["status"], "conflict")
        self.assertEqual(len(self.hub.list_dispositions()), 1)
        self.assertNotIn("confidence", self.hub.get_disposition("evt-1")["event"]["signals"])

    def test_invalid_entries_do_not_block_batch(self):
        batch = [
            make_event("evt-ok", 1, BASE.isoformat(), {"mood": "low"}),
            {"event_id": "evt-bad"},
            "not-an-object",
        ]
        result = self.hub.ingest_batch(batch)
        self.assertEqual(
            [r["status"] for r in result["results"]], ["processed", "error", "error"]
        )
        self.assertIsNotNone(self.hub.get_disposition("evt-ok"))

    def test_summary_retention_drops_signal_details(self):
        self.hub.grant_consent({
            "consent_id": "consent-visitor-x",
            "device_id": "orbit-09",
            "pairing_id": "pair-visit",
            "subject_type": "visitor",
            "subject_id": "guest-1",
            "granted_by": "guardian-a",
            "permissions": {"retention": "summary", "actions": [], "personalization": False},
            "effective_from": (BASE - timedelta(days=1)).isoformat(),
        })
        event = make_event(
            "evt-v1", 1, BASE.isoformat(),
            {"mood": "low", "confidence": 0.9, "touch": "hold"},
            device_id="orbit-09", pairing_id="pair-visit",
        )
        self.hub.ingest_batch([event])
        record = self.hub.get_disposition("evt-v1")["event"]
        self.assertEqual(record["retention"], "summary")
        self.assertEqual(record["signals"], {"mood": "low"})

    def test_event_time_consent_not_receipt_time(self):
        # v2 在 BASE+1h 生效，只批准 play_music。
        self.hub.grant_consent({
            "consent_id": "consent-child-a",
            "device_id": "orbit-08",
            "pairing_id": "pair-home-a",
            "subject_type": "child",
            "subject_id": "child-a",
            "granted_by": "guardian-a",
            "permissions": {"retention": "full", "actions": ["play_music"], "personalization": True},
            "effective_from": (BASE + timedelta(hours=1)).isoformat(),
        })
        # 两个事件都在 v2 生效之后才到达，但 captured_at 决定各自适用的版本。
        early = make_event("evt-early", 10, (BASE + timedelta(minutes=30)).isoformat(), {"mood": "low"})
        late = make_event("evt-late", 11, (BASE + timedelta(hours=2)).isoformat(), {"mood": "low"})
        self.hub.ingest_batch([late, early], received_at=BASE + timedelta(hours=3))

        d_early = self.hub.get_disposition("evt-early")["disposition"]
        self.assertEqual(d_early["action"], {"type": "breathing_light"})
        self.assertEqual(d_early["primary_consent"], {"consent_id": "consent-child-a", "version": 1})

        d_late = self.hub.get_disposition("evt-late")["disposition"]
        self.assertEqual(d_late["action"], {"type": "play_music"})
        self.assertEqual(d_late["primary_consent"], {"consent_id": "consent-child-a", "version": 2})

    def test_clock_drift_across_timezones(self):
        # 授权时间用 +08:00 表示，事件 captured_at 用 UTC 表示，应正确对齐。
        self.hub.grant_consent({
            "consent_id": "consent-tz",
            "device_id": "orbit-10",
            "pairing_id": "pair-tz",
            "subject_type": "family_member",
            "subject_id": "member-1",
            "granted_by": "guardian-a",
            "permissions": {"retention": "full", "actions": ["play_music"], "personalization": True},
            "effective_from": "2026-09-12T14:00:00+08:00",
        })
        event = make_event(
            "evt-tz", 1, "2026-09-12T06:00:01+00:00", {"mood": "low"},
            device_id="orbit-10", pairing_id="pair-tz",
        )
        self.hub.ingest_batch([event])
        disposition = self.hub.get_disposition("evt-tz")["disposition"]
        self.assertEqual(disposition["outcome"], "action_executed")

    @staticmethod
    def _clock():
        class Clock:
            def __init__(self):
                self.now = BASE
            def __call__(self):
                return self.now
            def set(self, value):
                self.now = value
        return Clock()


if __name__ == "__main__":
    unittest.main()
