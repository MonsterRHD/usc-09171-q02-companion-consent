import unittest
from datetime import datetime, timedelta, timezone

from service.hub import ConsentHub

BASE = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)


def make_event(event_id, sequence, captured_at, signals, device_id="orbit-08", pairing_id="pair-home-a"):
    return {
        "event_id": event_id,
        "device_id": device_id,
        "pairing_id": pairing_id,
        "sequence": sequence,
        "captured_at": captured_at,
        "signals": signals,
    }


def grant(hub, consent_id, subject_type, subject_id, permissions, **extra):
    payload = {
        "consent_id": consent_id,
        "device_id": "orbit-08",
        "pairing_id": "pair-home-a",
        "subject_type": subject_type,
        "subject_id": subject_id,
        "granted_by": "guardian-a",
        "permissions": permissions,
    }
    payload.update(extra)
    return hub.grant_consent(payload)


class ConsentLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.clock = self._clock()
        self.hub = ConsentHub(clock=self.clock)

    def test_grant_creates_version_and_supersedes_previous(self):
        v1 = grant(self.hub, "c-1", "family_member", "member-1",
                   {"retention": "full", "actions": ["play_music"], "personalization": True},
                   effective_from=BASE.isoformat())
        self.assertEqual(v1["version"], 1)
        v2 = grant(self.hub, "c-1", "family_member", "member-1",
                   {"retention": "summary", "actions": [], "personalization": False},
                   effective_from=(BASE + timedelta(hours=1)).isoformat())
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v1["superseded_at"], v2["effective_from"])
        self.assertEqual(len(self.hub.list_consents(device_id="orbit-08")), 1)

    def test_child_authorization_expiry_stops_personalization(self):
        grant(self.hub, "c-child", "child", "child-a",
              {"retention": "full", "actions": ["breathing_light"], "personalization": True},
              effective_from=(BASE - timedelta(days=1)).isoformat(),
              expires_at=(BASE + timedelta(days=1)).isoformat())
        before = make_event("evt-before", 1, BASE.isoformat(), {"mood": "low"})
        after = make_event("evt-after", 2, (BASE + timedelta(days=2)).isoformat(), {"mood": "low"})
        self.hub.ingest_batch([before, after])

        d_before = self.hub.get_disposition("evt-before")["disposition"]
        self.assertEqual(d_before["outcome"], "action_executed")
        d_after = self.hub.get_disposition("evt-after")["disposition"]
        self.assertEqual(d_after["outcome"], "personalization_blocked")
        self.assertEqual(d_after["retention"], "minimal")
        self.assertEqual(d_after["consent_versions"], [])

    def test_revocation_applies_immediately_but_not_retroactively(self):
        grant(self.hub, "c-family", "family_member", "member-1",
              {"retention": "full", "actions": ["play_music"], "personalization": True},
              effective_from=(BASE - timedelta(days=1)).isoformat())
        # 撤回发生在 BASE+1h；captured_at 在撤回前的事件仍按旧授权处置。
        self.clock.set(BASE + timedelta(hours=1))
        revoked = self.hub.revoke_consent("c-family")
        self.assertIsNotNone(revoked)
        self.assertEqual(revoked[0]["revoked_at"], (BASE + timedelta(hours=1)).isoformat())

        old_event = make_event("evt-old", 1, (BASE + timedelta(minutes=30)).isoformat(), {"mood": "low"})
        new_event = make_event("evt-new", 2, (BASE + timedelta(hours=2)).isoformat(), {"mood": "low"})
        self.hub.ingest_batch([old_event, new_event], received_at=BASE + timedelta(hours=3))

        d_old = self.hub.get_disposition("evt-old")["disposition"]
        self.assertEqual(d_old["outcome"], "action_executed")
        self.assertEqual(d_old["primary_consent"], {"consent_id": "c-family", "version": 1})
        d_new = self.hub.get_disposition("evt-new")["disposition"]
        self.assertEqual(d_new["outcome"], "personalization_blocked")

    def test_revoke_unknown_consent_returns_none(self):
        self.assertIsNone(self.hub.revoke_consent("c-missing"))

    def test_subject_priority_orders_child_over_family_over_visitor(self):
        grant(self.hub, "c-visitor", "visitor", "guest-1",
              {"retention": "minimal", "actions": [], "personalization": False},
              effective_from=(BASE - timedelta(days=1)).isoformat())
        grant(self.hub, "c-family", "family_member", "member-1",
              {"retention": "summary", "actions": [], "personalization": False},
              effective_from=(BASE - timedelta(days=1)).isoformat())
        grant(self.hub, "c-child", "child", "child-a",
              {"retention": "full", "actions": ["tell_story"], "personalization": True},
              effective_from=(BASE - timedelta(days=1)).isoformat())

        self.hub.ingest_batch([make_event("evt-1", 1, BASE.isoformat(), {"mood": "low"})])
        disposition = self.hub.get_disposition("evt-1")["disposition"]
        self.assertEqual(disposition["primary_consent"], {"consent_id": "c-child", "version": 1})
        self.assertEqual(disposition["action"], {"type": "tell_story"})
        # 三份授权分开管理，但都能在处置记录中追溯。
        self.assertEqual(
            [ref["consent_id"] for ref in disposition["consent_versions"]],
            ["c-child", "c-family", "c-visitor"],
        )

    def test_visitor_consent_governs_when_only_active(self):
        grant(self.hub, "c-visitor", "visitor", "guest-1",
              {"retention": "summary", "actions": ["play_music"], "personalization": True},
              effective_from=(BASE - timedelta(days=1)).isoformat())
        self.hub.ingest_batch([make_event("evt-1", 1, BASE.isoformat(), {"mood": "low"})])
        disposition = self.hub.get_disposition("evt-1")["disposition"]
        self.assertEqual(disposition["primary_consent"], {"consent_id": "c-visitor", "version": 1})
        self.assertEqual(disposition["retention"], "summary")

    def test_consent_scoped_to_pairing_period(self):
        grant(self.hub, "c-family", "family_member", "member-1",
              {"retention": "full", "actions": ["play_music"], "personalization": True},
              effective_from=(BASE - timedelta(days=1)).isoformat())
        other = make_event("evt-other", 1, BASE.isoformat(), {"mood": "low"}, pairing_id="pair-home-b")
        self.hub.ingest_batch([other])
        disposition = self.hub.get_disposition("evt-other")["disposition"]
        self.assertEqual(disposition["outcome"], "personalization_blocked")

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
