"""删除清理、最小事实保留、客服脱敏视图与监护人导出测试。"""

import json
import unittest

from service.consent import ConsentManager, CHILD, FAMILY
from service.engine import Engine
from service.privacy import PrivacyService
from service.store import Store


def _evt(eid, seq, captured, signals, device="dev-1", pairing="pair-a"):
    return {
        "event_id": eid, "device_id": device, "pairing_id": pairing,
        "sequence": seq, "captured_at": captured, "signals": signals,
    }


class PrivacyTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.cm = ConsentManager(self.store)
        self.engine = Engine(self.store, self.cm)
        self.pv = PrivacyService(self.store)
        self.cm.create(FAMILY, "dev-1", "fam-a", "home-a", consent_id="c-fam",
                       allowed_actions=["hug_back"],
                       effective_from="2026-09-12T00:00:00+00:00")
        self.cm.create(CHILD, "dev-1", "kid-1", "home-a", guardian_id="g-1",
                       consent_id="c-kid", allowed_actions=["hug_back"],
                       effective_from="2026-09-12T00:00:00+00:00")

    def test_deletion_removes_details_but_keeps_danger_facts(self):
        self.engine.ingest_batch([
            _evt("e-low", 1, "2026-09-12T10:00:00+00:00",
                 {"mood": "low", "confidence": 0.8, "touch": "hold"}),
            _evt("e-danger", 2, "2026-09-12T10:05:00+00:00",
                 {"risk": "danger", "confidence": 0.97}),
        ], received_at="2026-09-12T10:05:01+00:00")

        result = self.pv.delete_subject_data(CHILD, "kid-1", "g-1")
        self.assertEqual(result["deleted"]["events"], 1)
        self.assertIn("e-low", result["deleted"]["event_ids"])
        # 普通事件明细与处置动作已删除
        self.assertIsNone(self.store.get_event("e-low"))
        # 危险事件的最小事实仍保留，且保留依据清楚
        self.assertEqual(len(result["retained_minimum_facts"]), 1)
        fact = result["retained_minimum_facts"][0]
        self.assertEqual(fact["event_id"], "e-danger")
        self.assertEqual(fact["retain_reason"], "explicit_danger_human_confirmation")
        self.assertEqual(fact["classification"], "danger")
        # 删除进度有记录可查
        deletions = self.store.list_deletions("kid-1")
        self.assertEqual(len(deletions), 1)
        self.assertEqual(deletions[0]["facts_retained"], 1)

    def test_support_explain_is_masked_and_traceable(self):
        self.engine.ingest_batch([
            _evt("e-1", 1, "2026-09-12T10:00:00+00:00",
                 {"mood": "low", "confidence": 0.72, "touch": "hold"}),
        ], received_at="2026-09-12T10:00:01+00:00")
        view = self.pv.support_explain_event("e-1")
        # 脱敏
        self.assertNotIn("kid-1", json.dumps(view, ensure_ascii=False))
        self.assertNotIn("0.72", json.dumps(view))
        self.assertIn("**", view["device_ref"])
        # 可追溯：授权版本 + 规则版本 + 动作解释
        self.assertEqual(view["consent"]["version"], 1)
        self.assertEqual(view["consent"]["scope"], "儿童监护")
        self.assertEqual(view["rules_version"], "rules-2026-09-v1")
        self.assertEqual(view["responses"][0]["response"], "家庭批准动作：轻抱回应")

    def test_guardian_export_scoped_to_own_permissions(self):
        # 另一个家庭/监护人的事件不得出现在导出中
        self.cm.create(CHILD, "dev-2", "kid-2", "home-b", guardian_id="g-2",
                       consent_id="c-kid-b",
                       effective_from="2026-09-12T00:00:00+00:00")
        self.engine.ingest_batch([
            _evt("e-1", 1, "2026-09-12T10:00:00+00:00", {"mood": "low"}, device="dev-1"),
        ])
        self.engine.ingest_batch([
            _evt("e-2", 1, "2026-09-12T10:00:00+00:00", {"mood": "low"}, device="dev-2"),
        ])
        out = self.pv.guardian_export("g-1", "home-a")
        consent_ids = {c["consent_id"] for c in out["consents"]}
        self.assertEqual(consent_ids, {"c-fam", "c-kid"})
        event_ids = {d["event_id"] for d in out["dispositions"]}
        self.assertEqual(event_ids, {"e-1"})
        # 处置记录能指回授权与规则版本
        d = out["dispositions"][0]
        self.assertEqual(d["consent_id"], "c-kid")
        self.assertEqual(d["consent_version"], 1)
        self.assertTrue(d["rules_version"])

    def test_export_includes_consent_version_history(self):
        self.cm.append_version("c-kid", allowed_actions=["soft_glow"],
                               effective_from="2026-09-12T12:00:00+00:00")
        out = self.pv.guardian_export("g-1", "home-a")
        kid_versions = [c["version"] for c in out["consents"] if c["consent_id"] == "c-kid"]
        self.assertEqual(kid_versions, [1, 2])


if __name__ == "__main__":
    unittest.main()
