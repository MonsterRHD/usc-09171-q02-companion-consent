import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from service.hub import ConsentHub

BASE = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)


def make_event(event_id, sequence, signals, captured_at, pairing_id="pair-home-a"):
    return {
        "event_id": event_id,
        "device_id": "orbit-08",
        "pairing_id": pairing_id,
        "sequence": sequence,
        "captured_at": captured_at.isoformat(),
        "signals": signals,
    }


class TransferAndDeletionTest(unittest.TestCase):
    def setUp(self):
        self.clock = self._clock()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store_path = os.path.join(self.tmp.name, "store.json")
        self.hub = ConsentHub(store_path=self.store_path, clock=self.clock)
        self.hub.grant_consent({
            "consent_id": "c-child",
            "device_id": "orbit-08",
            "pairing_id": "pair-home-a",
            "subject_type": "child",
            "subject_id": "child-a",
            "granted_by": "guardian-a",
            "permissions": {
                "retention": "full",
                "actions": ["breathing_light"],
                "personalization": True,
            },
            "effective_from": (BASE - timedelta(days=1)).isoformat(),
        })

    def test_transfer_closes_pairing_and_late_events_follow_event_time(self):
        self.hub.ingest_batch([make_event("evt-1", 1, {"mood": "low"}, BASE)])
        self.clock.set(BASE + timedelta(hours=2))
        result = self.hub.transfer_device("orbit-08", new_pairing_id="pair-home-b")
        self.assertEqual(result["closed_pairing_ids"], ["pair-home-a"])

        # 断网重传的旧事件：captured_at 在关闭前，仍按发生当刻授权处置。
        late = make_event("evt-late", 2, {"mood": "low"}, BASE + timedelta(hours=1))
        self.hub.ingest_batch([late])
        self.assertEqual(
            self.hub.get_disposition("evt-late")["disposition"]["outcome"], "action_executed"
        )

        # 时钟漂移容忍内的事件仍然受理。
        drifted = make_event("evt-drift", 3, {"mood": "low"}, BASE + timedelta(hours=2, minutes=2))
        self.hub.ingest_batch([drifted])
        self.assertEqual(
            self.hub.get_disposition("evt-drift")["disposition"]["outcome"], "action_executed"
        )

        # 超出容忍窗口的旧配对事件被拒绝。
        stale = make_event("evt-stale", 4, {"mood": "low"}, BASE + timedelta(hours=3))
        self.hub.ingest_batch([stale])
        disposition = self.hub.get_disposition("evt-stale")["disposition"]
        self.assertEqual(disposition["outcome"], "rejected_pairing_closed")
        self.assertEqual(disposition["retention"], "minimal")

    def test_deletion_separates_purgeable_data_from_audit_facts(self):
        self.hub.ingest_batch([
            make_event("evt-comfort", 1, {"mood": "low", "touch": "hold"}, BASE),
            make_event("evt-anomaly", 2, {"risk": "needs_attention"}, BASE + timedelta(minutes=1)),
            make_event("evt-anomaly-2", 3, {"risk": "needs_attention"}, BASE + timedelta(minutes=2)),
            make_event("evt-danger", 4, {"risk": "danger"}, BASE + timedelta(minutes=3)),
        ])
        job = self.hub.start_deletion("orbit-08", "pair-home-a", requested_by="guardian-a")

        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["progress"], {"processed": 4, "total": 4})
        # 普通安抚事件与未触发通知的异常事件都属于可删资料，被完全清除。
        self.assertEqual(job["deleted"]["events"], 2)
        self.assertEqual(job["deleted"]["dispositions"], 2)
        self.assertEqual(job["deleted"]["signal_payloads"], 4)

        # 保留依据清楚可查。
        retained = {(item["kind"], item["basis"]) for item in job["retained"]}
        self.assertIn(("consent_record", "consent_audit"), retained)
        self.assertIn(("safety_fact", "safety_audit"), retained)
        self.assertIn(("escalation", "safety_audit"), retained)
        self.assertIn(("notification", "safety_audit"), retained)

        # 普通事件与未触发通知的异常事件：处置记录与信号载荷都被清除，只剩墓碑。
        self.assertIsNone(self.hub.get_disposition("evt-comfort"))
        self.assertIsNone(self.hub.get_disposition("evt-anomaly"))
        tombstone = self.hub.support_explanation("evt-comfort")
        self.assertTrue(tombstone["purged"])

        # 触发关怀提醒的事件：保留安全审计最小事实。
        reminder_fact = self.hub.get_disposition("evt-anomaly-2")["disposition"]
        self.assertEqual(reminder_fact["retention_basis"], "safety_audit")

        # 危险事件：只留最小事实。
        fact = self.hub.get_disposition("evt-danger")["disposition"]
        self.assertEqual(fact["retention_basis"], "safety_audit")
        self.assertTrue(fact["redacted"])
        self.assertNotIn("signals", self.hub.get_disposition("evt-danger")["event"])

        # 删除后重传不会复活数据。
        replay = make_event("evt-comfort", 1, {"mood": "low", "touch": "hold"}, BASE)
        result = self.hub.ingest_batch([replay])
        self.assertEqual(result["results"][0]["status"], "duplicate")
        self.assertIsNone(self.hub.get_disposition("evt-comfort"))

        # 删除进度可查询。
        fetched = self.hub.get_deletion(job["job_id"])
        self.assertEqual(fetched["job_id"], job["job_id"])
        self.assertEqual(fetched["requested_by"], "guardian-a")

    def test_restart_preserves_state_and_idempotency(self):
        self.hub.ingest_batch([
            make_event("evt-1", 1, {"risk": "needs_attention"}, BASE),
            make_event("evt-2", 2, {"risk": "needs_attention"}, BASE + timedelta(minutes=1)),
            make_event("evt-3", 3, {"risk": "danger"}, BASE + timedelta(minutes=2)),
        ])
        notifications_before = self.hub.list_notifications(device_id="orbit-08")
        self.assertEqual(len(notifications_before), 2)

        # 服务重启：从同一存储恢复。
        restarted = ConsentHub(store_path=self.store_path, clock=self.clock)
        replay = restarted.ingest_batch([
            make_event("evt-1", 1, {"risk": "needs_attention"}, BASE),
            make_event("evt-2", 2, {"risk": "needs_attention"}, BASE + timedelta(minutes=1)),
            make_event("evt-3", 3, {"risk": "danger"}, BASE + timedelta(minutes=2)),
        ])
        self.assertEqual([r["status"] for r in replay["results"]], ["duplicate"] * 3)
        self.assertEqual(len(restarted.list_notifications(device_id="orbit-08")), 2)
        self.assertEqual(len(restarted.list_escalations(status="pending")), 1)

        confirmed = restarted.confirm_escalation("esc-evt-3", confirmed_by="staff-01")
        self.assertEqual(confirmed["status"], "confirmed")
        restarted2 = ConsentHub(store_path=self.store_path, clock=self.clock)
        self.assertEqual(
            restarted2.list_escalations(status="confirmed")[0]["confirmed_by"], "staff-01"
        )

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
