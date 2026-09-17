"""事件处置引擎：分类、留存粒度、动作选择、关怀窗口、危险升级、幂等。"""

import json
import tempfile
import unittest

from service.consent import ConsentManager, CHILD, FAMILY
from service.engine import (Engine, CARE_GAP_SECONDS, RULES_VERSION)
from service.store import Store


def _evt(eid, seq, captured, signals, device="dev-1", pairing="pair-a"):
    return {
        "event_id": eid, "device_id": device, "pairing_id": pairing,
        "sequence": seq, "captured_at": captured, "signals": signals,
    }


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.cm = ConsentManager(self.store)
        self.engine = Engine(self.store, self.cm)
        self.cm.create(
            FAMILY, "dev-1", "fam-a", "home-a", consent_id="c-fam",
            allowed_actions=["hug_back", "soft_glow"],
            retention_mood="summary", retention_risk="minimal",
            voice_summary=True, effective_from="2026-09-12T00:00:00+00:00",
        )
        self.cm.create(
            CHILD, "dev-1", "kid-1", "home-a", guardian_id="g-1",
            consent_id="c-kid",
            allowed_actions=["hug_back"],
            retention_mood="full", retention_risk="summary",
            voice_summary=True, effective_from="2026-09-12T00:00:00+00:00",
        )

    def test_low_mood_uses_child_consent_and_approved_action(self):
        results = self.engine.ingest_batch(
            [_evt("e-1", 1, "2026-09-12T10:00:00+00:00",
                  {"mood": "low", "confidence": 0.72, "touch": "hold"})],
            received_at="2026-09-12T10:00:01+00:00",
        )
        r = results[0]
        self.assertEqual(r["classification"], "low")
        self.assertEqual(r["resolution"], "handled")
        self.assertEqual(r["action"], "hug_back")
        self.assertEqual(r["consent"], "c-kid@v1")
        self.assertEqual(r["rules_version"], RULES_VERSION)
        row = self.store.get_event("e-1")
        record = json.loads(row["stored_record"])
        self.assertEqual(record["subject_id"], "kid-1")

    def test_low_mood_retention_summary_strips_confidence(self):
        # 家庭授权（无更高优先授权时）retention_mood=summary
        self.cm.revoke("c-kid", at="2026-09-10T00:00:00+00:00")
        self.engine.ingest_batch(
            [_evt("e-1", 1, "2026-09-12T10:00:00+00:00",
                  {"mood": "low", "confidence": 0.9, "touch": "hold"})],
            received_at="2026-09-12T10:00:01+00:00",
        )
        row = self.store.get_event("e-1")
        kept = json.loads(row["signals"])
        self.assertNotIn("confidence", kept)
        self.assertEqual(kept["mood"], "low")

    def test_voice_summary_flag_removes_voice_fields(self):
        self.cm.append_version("c-kid", voice_summary=False,
                               effective_from="2026-09-12T12:00:00+00:00")
        self.engine.ingest_batch(
            [_evt("e-1", 1, clock_after("2026-09-12T12:00:01+00:00"),
                  {"mood": "low", "voice_tone": "trembling"})],
            received_at="2026-09-12T12:00:05+00:00",
        )
        row = self.store.get_event("e-1")
        kept = json.loads(row["signals"])
        self.assertNotIn("voice_tone", kept)

    def test_repeated_abnormal_emits_single_care_alert(self):
        events = [
            _evt("e-1", 1, "2026-09-12T10:00:00+00:00", {"risk": "needs_attention"}),
            _evt("e-2", 2, "2026-09-12T10:02:00+00:00", {"risk": "needs_attention"}),
            _evt("e-3", 3, "2026-09-12T10:04:00+00:00", {"risk": "needs_attention"}),
        ]
        results = self.engine.ingest_batch(
            events, received_at="2026-09-12T10:05:00+00:00")
        self.assertEqual([r["care_notification"] for r in results], [False, True, False])
        notes = [n for n in self.store.list_notifications() if n["ntype"] == "care_alert"]
        self.assertEqual(len(notes), 1)
        payload = json.loads(notes[0]["payload"])
        self.assertEqual(payload["abnormal_count"], 3)
        self.assertEqual(payload["information"], "minimal:attention_count_only")
        # 重传整批：不产生第二条通知，处置只发生一次
        again = self.engine.ingest_batch(events, received_at="2026-09-12T11:00:00+00:00")
        self.assertTrue(all(r["status"] == "duplicate" for r in again))
        notes = [n for n in self.store.list_notifications() if n["ntype"] == "care_alert"]
        self.assertEqual(len(notes), 1)

    def test_out_of_order_arrival_keeps_single_notification(self):
        # 先到第二个事件，再到第一个（乱序回传）
        self.engine.ingest_batch(
            [_evt("e-2", 2, "2026-09-12T10:02:00+00:00", {"risk": "needs_attention"})],
            received_at="2026-09-12T10:03:00+00:00")
        r = self.engine.ingest_batch(
            [_evt("e-1", 1, "2026-09-12T10:00:00+00:00", {"risk": "needs_attention"})],
            received_at="2026-09-12T10:04:00+00:00")
        self.assertTrue(r[0]["care_notification"])
        self.engine.ingest_batch(
            [_evt("e-3", 3, "2026-09-12T10:04:00+00:00", {"risk": "needs_attention"})],
            received_at="2026-09-12T10:05:00+00:00")
        notes = [n for n in self.store.list_notifications() if n["ntype"] == "care_alert"]
        self.assertEqual(len(notes), 1)

    def test_danger_opens_human_confirmation_even_without_consent(self):
        self.cm.revoke("c-fam", at="2026-09-10T00:00:00+00:00")
        self.cm.revoke("c-kid", at="2026-09-10T00:00:00+00:00")
        r = self.engine.ingest_batch(
            [_evt("e-9", 9, "2026-09-12T20:00:00+00:00",
                  {"risk": "danger", "confidence": 0.99, "speech_text": "x"})],
            received_at="2026-09-12T20:00:01+00:00",
        )[0]
        self.assertEqual(r["resolution"], "escalated")
        self.assertIsNotNone(r["escalation_case"])
        case = self.store.get_escalation(r["escalation_case"])
        self.assertEqual(case["status"], "open")
        # 最小事实：信号细节不落库
        row = self.store.get_event("e-9")
        self.assertEqual(row["retention_level"], "minimal")
        self.assertEqual(json.loads(row["signals"]), {})
        facts = self.store.list_facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0]["retain_reason"], "explicit_danger_human_confirmation")

    def test_danger_collision_with_revoke_uses_event_time_version(self):
        # 撤回发生在 11:00；危险事件发生于 10:59（撤回前），断网至 12:00 才到达
        self.cm.revoke("c-kid", at="2026-09-12T11:00:00+00:00")
        r = self.engine.ingest_batch(
            [_evt("e-late", 20, "2026-09-12T10:59:00+00:00", {"risk": "danger"})],
            received_at="2026-09-12T12:00:00+00:00",
        )[0]
        self.assertEqual(r["resolution"], "escalated")
        self.assertEqual(r["consent"], "c-kid@v1")  # 指向事件当刻授权版本
        row = self.store.get_event("e-late")
        self.assertGreaterEqual(abs(row["clock_skew_sec"]), 3600)

    def test_post_revoke_non_danger_is_suppressed(self):
        self.cm.revoke("c-fam", at="2026-09-12T11:00:00+00:00")
        self.cm.revoke("c-kid", at="2026-09-12T11:00:00+00:00")
        r = self.engine.ingest_batch(
            [_evt("e-1", 30, "2026-09-12T11:30:00+00:00", {"mood": "low"})],
            received_at="2026-09-12T11:30:01+00:00",
        )[0]
        self.assertEqual(r["resolution"], "suppressed_no_consent")
        self.assertIsNone(r["action"])
        self.assertEqual(json.loads(self.store.get_event("e-1")["signals"]), {})

    def test_transferred_device_suppresses_personalisation(self):
        self.cm.transfer_device("dev-1", at="2026-09-12T11:00:00+00:00")
        r = self.engine.ingest_batch(
            [_evt("e-1", 31, "2026-09-12T11:30:00+00:00", {"mood": "low", "touch": "hold"})],
            received_at="2026-09-12T11:30:01+00:00",
        )[0]
        self.assertEqual(r["resolution"], "suppressed_transferred")
        self.assertIsNone(r["action"])

    def test_pairing_sequence_duplicate_is_idempotent(self):
        e1 = _evt("e-a", 5, "2026-09-12T10:00:00+00:00", {"mood": "low"})
        e2 = _evt("e-b", 5, "2026-09-12T10:00:00+00:00", {"mood": "low"})
        self.engine.ingest_batch([e1])
        r = self.engine.ingest_batch([e2])[0]
        self.assertEqual(r["status"], "duplicate")
        self.assertEqual(r["conflict"], "pairing_sequence")
        self.assertEqual(r["original_event_id"], "e-a")

    def test_same_sequence_new_pairing_cycle_is_accepted(self):
        # 恢复出厂后 pairing_id 更换，sequence 重新从小开始是合法的
        self.engine.ingest_batch(
            [_evt("e-a", 1, "2026-09-12T10:00:00+00:00", {"mood": "low"})])
        r = self.engine.ingest_batch(
            [_evt("e-b", 1, "2026-09-12T15:00:00+00:00", {"mood": "low"},
                  pairing="pair-b")])[0]
        self.assertEqual(r["status"], "processed")

    def test_abnormal_outside_gap_starts_new_window_no_extra_notice(self):
        self.engine.ingest_batch([
            _evt("e-1", 1, "2026-09-12T10:00:00+00:00", {"risk": "needs_attention"}),
            _evt("e-2", 2, "2026-09-12T10:02:00+00:00", {"risk": "needs_attention"}),
        ])
        # 超过窗口间隔的新簇：单条不通知；再来一条才通知第二次
        base = self.engine
        base.ingest_batch([
            _evt("e-3", 3, f"2026-09-12T10:{20:02d}:00+00:00", {"risk": "needs_attention"}),
        ])
        care = [n for n in self.store.list_notifications() if n["ntype"] == "care_alert"]
        self.assertEqual(len(care), 1)
        # 10:22（与 10:20 间隔 2 分钟，与 10:02 间隔 20 分钟）
        base.ingest_batch([
            _evt("e-4", 4, "2026-09-12T10:22:00+00:00", {"risk": "needs_attention"}),
        ])
        care = [n for n in self.store.list_notifications() if n["ntype"] == "care_alert"]
        self.assertEqual(len(care), 2)

    def test_clock_skew_recorded(self):
        r = self.engine.ingest_batch(
            [_evt("e-1", 1, "2026-09-12T10:00:00+00:00", {"mood": "low"})],
            received_at="2026-09-12T10:05:00+00:00",
        )[0]
        self.assertAlmostEqual(r["clock_skew_seconds"], 300, places=1)

    def test_restart_recovers_state_from_disk(self):
        path = tempfile.mktemp(suffix=".db")
        store = Store(path)
        cm = ConsentManager(store)
        engine = Engine(store, cm)
        cm.create(FAMILY, "dev-1", "fam-a", "home-a", consent_id="c-fam",
                  allowed_actions=["soft_glow"],
                  effective_from="2026-09-12T00:00:00+00:00")
        engine.ingest_batch([
            _evt("e-1", 1, "2026-09-12T10:00:00+00:00", {"risk": "needs_attention"}),
            _evt("e-2", 2, "2026-09-12T10:02:00+00:00", {"risk": "needs_attention"}),
        ])
        store.close()
        # “服务重启”：重新打开同一数据库
        store2 = Store(path)
        engine2 = Engine(store2, ConsentManager(store2))
        self.assertEqual(engine2.store.get_event("e-1")["resolution"], "handled")
        r = engine2.ingest_batch([
            _evt("e-1", 1, "2026-09-12T10:00:00+00:00", {"risk": "needs_attention"})])[0]
        self.assertEqual(r["status"], "duplicate")
        care = [n for n in store2.list_notifications() if n["ntype"] == "care_alert"]
        self.assertEqual(len(care), 1)


def clock_after(ts):
    return ts


if __name__ == "__main__":
    unittest.main()
