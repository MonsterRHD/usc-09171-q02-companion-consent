"""授权三层分离、版本化与事件时刻裁决测试。"""

import unittest

from service.consent import ConsentError, ConsentManager, CHILD, FAMILY, GUEST
from service.store import Store


class ConsentTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.cm = ConsentManager(self.store)

    def test_three_scopes_are_separate_and_prioritised(self):
        self.cm.create(FAMILY, "dev-1", "fam-a", "home-a",
                       consent_id="c-fam",
                       allowed_actions=["soft_glow"],
                       effective_from="2026-09-12T00:00:00+00:00")
        self.cm.create(CHILD, "dev-1", "kid-1", "home-a", guardian_id="g-1",
                       consent_id="c-kid",
                       allowed_actions=["hug_back", "soft_glow"],
                       effective_from="2026-09-12T00:00:00+00:00")
        self.cm.create(GUEST, "dev-1", "visitor-1", "home-a", guardian_id="g-1",
                       consent_id="c-guest",
                       allowed_actions=["breathing_prompt"],
                       effective_from="2026-09-12T00:00:00+00:00")
        view, _ = self.cm.resolve("dev-1", "2026-09-12T10:00:00+00:00")
        self.assertEqual(view.scope_type, GUEST)

    def test_guest_expires_falls_back_to_child_then_family(self):
        base = "2026-09-12T00:00:00+00:00"
        self.cm.create(FAMILY, "dev-1", "fam-a", "home-a", consent_id="c-fam",
                       effective_from=base)
        self.cm.create(CHILD, "dev-1", "kid-1", "home-a", guardian_id="g-1",
                       consent_id="c-kid",
                       expires_at="2026-09-12T12:00:00+00:00",
                       effective_from=base)
        self.cm.create(GUEST, "dev-1", "visitor-1", "home-a", guardian_id="g-1",
                       consent_id="c-guest",
                       expires_at="2026-09-12T10:00:00+00:00",
                       effective_from=base)
        v, _ = self.cm.resolve("dev-1", "2026-09-12T09:59:59+00:00")
        self.assertEqual(v.scope_type, GUEST)
        v, _ = self.cm.resolve("dev-1", "2026-09-12T10:30:00+00:00")
        self.assertEqual(v.scope_type, CHILD)
        v, _ = self.cm.resolve("dev-1", "2026-09-12T13:00:00+00:00")
        self.assertEqual(v.scope_type, FAMILY)

    def test_new_version_does_not_apply_to_past_events(self):
        self.cm.create(FAMILY, "dev-1", "fam-a", "home-a", consent_id="c-fam",
                       allowed_actions=["soft_glow"],
                       effective_from="2026-09-12T08:00:00+00:00")
        self.cm.append_version("c-fam", allowed_actions=["hug_back"],
                               effective_from="2026-09-12T12:00:00+00:00")
        v, _ = self.cm.resolve("dev-1", "2026-09-12T09:00:00+00:00")
        self.assertEqual(v.version, 1)
        self.assertEqual(v.allowed_actions, ("soft_glow",))
        v, _ = self.cm.resolve("dev-1", "2026-09-12T13:00:00+00:00")
        self.assertEqual(v.version, 2)

    def test_revoke_leaves_past_snapshot_valid_but_blocks_future(self):
        self.cm.create(FAMILY, "dev-1", "fam-a", "home-a", consent_id="c-fam",
                       effective_from="2026-09-12T08:00:00+00:00")
        self.cm.revoke("c-fam", at="2026-09-12T12:00:00+00:00")
        v, _ = self.cm.resolve("dev-1", "2026-09-12T11:59:59+00:00")
        self.assertIsNotNone(v)
        view, reason = self.cm.resolve("dev-1", "2026-09-12T12:00:01+00:00")
        self.assertIsNone(view)
        self.assertEqual(reason, "no_consent")

    def test_transfer_terminates_original_family_and_is_distinguishable(self):
        base = "2026-09-12T00:00:00+00:00"
        self.cm.create(FAMILY, "dev-1", "fam-a", "home-a", consent_id="c-fam",
                       effective_from=base)
        self.cm.create(CHILD, "dev-1", "kid-1", "home-a", guardian_id="g-1",
                       consent_id="c-kid", effective_from=base)
        count = self.cm.transfer_device("dev-1", at="2026-09-13T00:00:00+00:00")
        self.assertEqual(count, 2)
        view, reason = self.cm.resolve("dev-1", "2026-09-13T01:00:00+00:00")
        self.assertIsNone(view)
        self.assertEqual(reason, "transferred")

    def test_raw_voice_is_rejected(self):
        with self.assertRaises(ConsentError):
            self.cm.create(FAMILY, "dev-1", "fam-a", "home-a", raw_voice=True)

    def test_child_and_guest_require_guardian(self):
        with self.assertRaises(ConsentError):
            self.cm.create(CHILD, "dev-1", "kid-1", "home-a")


if __name__ == "__main__":
    unittest.main()
