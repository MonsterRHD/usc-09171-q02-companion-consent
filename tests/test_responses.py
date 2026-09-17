import unittest
from datetime import datetime, timedelta, timezone

from service.hub import ConsentHub

BASE = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)


def make_event(event_id, sequence, signals, captured_at=None, pairing_id="pair-home-a"):
    return {
        "event_id": event_id,
        "device_id": "orbit-08",
        "pairing_id": pairing_id,
        "sequence": sequence,
        "captured_at": (captured_at or BASE + timedelta(minutes=sequence)).isoformat(),
        "signals": signals,
    }


class ResponsePolicyTest(unittest.TestCase):
    def setUp(self):
        self.clock = self._clock()
        self.hub = ConsentHub(clock=self.clock)
        self.hub.grant_consent({
            "consent_id": "c-child",
            "device_id": "orbit-08",
            "pairing_id": "pair-home-a",
            "subject_type": "child",
            "subject_id": "child-a",
            "granted_by": "guardian-a",
            "permissions": {"retention": "full", "actions": ["play_music"], "personalization": True},
            "effective_from": (BASE - timedelta(days=1)).isoformat(),
        })

    def test_comfort_action_only_from_family_approved_list(self):
        self.hub.ingest_batch([make_event("evt-1", 1, {"mood": "low"})])
        disposition = self.hub.get_disposition("evt-1")["disposition"]
        # 规则优先 breathing_light，但家庭只批准了 play_music。
        self.assertEqual(disposition["outcome"], "action_executed")
        self.assertEqual(disposition["action"], {"type": "play_music"})

    def test_comfort_without_approved_actions_records_only(self):
        self.hub.grant_consent({
            "consent_id": "c-child",
            "device_id": "orbit-08",
            "pairing_id": "pair-home-a",
            "subject_type": "child",
            "subject_id": "child-a",
            "granted_by": "guardian-a",
            "permissions": {"retention": "full", "actions": [], "personalization": True},
            "effective_from": (BASE - timedelta(hours=1)).isoformat(),
        })
        self.hub.ingest_batch([make_event("evt-1", 1, {"mood": "low"})])
        disposition = self.hub.get_disposition("evt-1")["disposition"]
        self.assertEqual(disposition["outcome"], "no_approved_action")
        self.assertIsNone(disposition["action"])

    def test_continuous_anomalies_raise_single_minimal_care_reminder(self):
        events = [
            make_event("evt-1", 1, {"risk": "needs_attention"}),
            make_event("evt-2", 2, {"risk": "needs_attention"}),
            make_event("evt-3", 3, {"risk": "needs_attention"}),
        ]
        results = self.hub.ingest_batch(events)
        outcomes = [r["outcome"] for r in results["results"]]
        self.assertEqual(outcomes, ["recorded", "care_reminder_sent", "recorded"])

        notifications = self.hub.list_notifications(device_id="orbit-08")
        self.assertEqual(len(notifications), 1)
        reminder = notifications[0]
        self.assertEqual(reminder["kind"], "care_reminder")
        self.assertTrue(reminder["minimal"])
        # 最少信息：不含信号类别、置信度等细节。
        self.assertNotIn("needs_attention", reminder["message"])
        self.assertNotIn("mood", reminder["message"])

    def test_broken_streak_starts_new_episode(self):
        self.hub.ingest_batch([
            make_event("evt-1", 1, {"risk": "needs_attention"}),
            make_event("evt-2", 2, {"risk": "needs_attention"}),
        ])
        self.hub.ingest_batch([make_event("evt-3", 3, {"touch": "hold"})])
        self.hub.ingest_batch([
            make_event("evt-4", 4, {"risk": "needs_attention"}),
            make_event("evt-5", 5, {"risk": "needs_attention"}),
        ])
        reminders = [n for n in self.hub.list_notifications(device_id="orbit-08")
                     if n["kind"] == "care_reminder"]
        self.assertEqual(len(reminders), 2)

    def test_out_of_order_backhaul_notifies_once(self):
        # 断网后乱序回传：seq 3 先到，seq 1、2 后补。
        self.hub.ingest_batch([make_event("evt-3", 3, {"risk": "needs_attention"})])
        self.hub.ingest_batch([make_event("evt-1", 1, {"risk": "needs_attention"})])
        self.hub.ingest_batch([make_event("evt-2", 2, {"risk": "needs_attention"})])
        reminders = [n for n in self.hub.list_notifications(device_id="orbit-08")
                     if n["kind"] == "care_reminder"]
        self.assertEqual(len(reminders), 1)

    def test_replayed_batch_does_not_duplicate_notifications(self):
        events = [
            make_event("evt-1", 1, {"risk": "needs_attention"}),
            make_event("evt-2", 2, {"risk": "needs_attention"}),
        ]
        self.hub.ingest_batch(events)
        replay = self.hub.ingest_batch(events)
        self.assertEqual([r["status"] for r in replay["results"]], ["duplicate", "duplicate"])
        self.assertEqual(len(self.hub.list_notifications(device_id="orbit-08")), 1)

    def test_danger_enters_human_confirmed_escalation(self):
        self.hub.ingest_batch([make_event("evt-1", 1, {"risk": "danger"})])
        disposition = self.hub.get_disposition("evt-1")["disposition"]
        self.assertEqual(disposition["outcome"], "escalation_pending")
        escalation_id = disposition["escalation_id"]

        escalations = self.hub.list_escalations(device_id="orbit-08", status="pending")
        self.assertEqual([e["escalation_id"] for e in escalations], [escalation_id])
        notifications = self.hub.list_notifications(device_id="orbit-08")
        self.assertEqual([n["kind"] for n in notifications], ["escalation"])

        confirmed = self.hub.confirm_escalation(escalation_id, confirmed_by="staff-01")
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertEqual(confirmed["confirmed_by"], "staff-01")
        # 重复确认幂等。
        confirmed_again = self.hub.confirm_escalation(escalation_id, confirmed_by="staff-02")
        self.assertEqual(confirmed_again["confirmed_by"], "staff-01")

    def test_danger_without_any_consent_still_escalates(self):
        # 撤回与危险事件相撞：无有效授权时个性化停止，但危险信号仍升级。
        event = make_event("evt-x", 1, {"risk": "danger"}, pairing_id="pair-none")
        self.hub.ingest_batch([event])
        disposition = self.hub.get_disposition("evt-x")["disposition"]
        self.assertEqual(disposition["outcome"], "escalation_pending")
        self.assertEqual(disposition["consent_versions"], [])
        self.assertEqual(disposition["retention"], "minimal")
        self.assertEqual(len(self.hub.list_escalations(status="pending")), 1)

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
