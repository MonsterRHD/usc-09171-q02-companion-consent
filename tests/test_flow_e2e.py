"""端到端流程：两家庭转借、儿童授权到期、乱序回传、撤回与危险相撞、服务重启。

对应验收主线：每个响应都能指向授权与规则版本；通知不重复；
删除进度与保留依据可查；转借/撤回后新个性化处理立即停止。
"""

import json
import os
import tempfile
import unittest

from service.consent import ConsentManager, CHILD, FAMILY, GUEST
from service.engine import Engine
from service.privacy import PrivacyService
from service.store import Store

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _evt(eid, seq, captured, signals, device="orbit-08", pairing="pair-home-a"):
    return {
        "event_id": eid, "device_id": device, "pairing_id": pairing,
        "sequence": seq, "captured_at": captured, "signals": signals,
    }


class FullFlowTest(unittest.TestCase):
    def test_two_family_lifecycle_flow(self):
        db = tempfile.mktemp(suffix=".db")

        # ---- 家庭 A 试用：家庭成员 + 儿童监护两层授权 ----
        store = Store(db)
        cm = ConsentManager(store)
        engine = Engine(store, cm)
        privacy = PrivacyService(store)

        cm.create(FAMILY, "orbit-08", "fam-A", "home-A", consent_id="c-fam-A",
                  allowed_actions=["soft_glow", "soothing_sound"],
                  retention_mood="summary",
                  effective_from="2026-09-10T00:00:00+08:00")
        cm.create(CHILD, "orbit-08", "kid-A", "home-A", guardian_id="g-A",
                  consent_id="c-kid-A",
                  allowed_actions=["hug_back", "soft_glow"],
                  expires_at="2026-09-13T08:00:00+08:00",
                  effective_from="2026-09-10T00:00:00+08:00")

        # ---- 普通低落：只从家庭批准动作中选，触摸 hold 优先轻抱 ----
        r = engine.ingest_batch([
            _evt("evt-71", 71, "2026-09-12T14:00:03+08:00",
                 {"mood": "low", "confidence": 0.72, "touch": "hold"}),
        ], received_at="2026-09-12T14:00:05+08:00")[0]
        self.assertEqual(r["action"], "hug_back")
        self.assertEqual(r["consent"], "c-kid-A@v1")
        self.assertTrue(r["rules_version"])

        # ---- 连续异常：最少信息关怀提醒，仅一次 ----
        engine.ingest_batch([
            _evt("evt-72", 72, "2026-09-12T14:00:08+08:00",
                 {"risk": "needs_attention", "confidence": 0.81}),
            _evt("evt-73", 73, "2026-09-12T14:00:40+08:00",
                 {"risk": "needs_attention", "confidence": 0.77}),
        ], received_at="2026-09-12T14:00:42+08:00")
        care = [n for n in store.list_notifications() if n["ntype"] == "care_alert"]
        self.assertEqual(len(care), 1)
        self.assertNotIn("confidence", care[0]["payload"])

        # ---- 乱序回传：更早的 evt-70 延迟到达，处置只一次、通知不重复 ----
        engine.ingest_batch([
            _evt("evt-70", 70, "2026-09-12T13:59:50+08:00",
                 {"risk": "needs_attention", "confidence": 0.6}),
        ], received_at="2026-09-12T14:05:00+08:00")
        self.assertEqual(
            len([n for n in store.list_notifications() if n["ntype"] == "care_alert"]), 1)

        # ---- 断网重传：同一 event_id / 配对序号重复到达 ----
        dup = engine.ingest_batch([
            _evt("evt-72", 72, "2026-09-12T14:00:08+08:00",
                 {"risk": "needs_attention", "confidence": 0.81}),
        ], received_at="2026-09-12T15:00:00+08:00")[0]
        self.assertEqual(dup["status"], "duplicate")
        self.assertEqual(len(store.list_events("orbit-08")), 4)

        # ---- 儿童授权到期：回落至家庭授权，动作词表随之变化 ----
        r = engine.ingest_batch([
            _evt("evt-80", 80, "2026-09-13T09:00:00+08:00",
                 {"mood": "low", "touch": "hold"}),
        ], received_at="2026-09-13T09:00:02+08:00")[0]
        self.assertEqual(r["consent"], "c-fam-A@v1")
        self.assertEqual(r["action"], "soft_glow")  # hug_back 未在家庭批准词表中

        # ---- 设备转借家庭 B：A 的授权立即终止 ----
        cm.transfer_device("orbit-08", at="2026-09-14T10:00:00+08:00")
        r = engine.ingest_batch([
            _evt("evt-90", 90, "2026-09-14T10:05:00+08:00",
                 {"mood": "low", "touch": "hold"}),
        ], received_at="2026-09-14T10:05:01+08:00")[0]
        self.assertEqual(r["resolution"], "suppressed_transferred")
        self.assertIsNone(r["action"])

        # B 家庭以访客授权试用后转正：新个性化处理按 B 的授权进行
        cm.create(GUEST, "orbit-08", "kid-B", "home-B", guardian_id="g-B",
                  consent_id="c-guest-B",
                  allowed_actions=["breathing_prompt"],
                  effective_from="2026-09-14T11:00:00+08:00")
        # B 家庭授权生效前的旧时间戳事件（断网积压）：仍按事件当刻 A 家庭授权处置
        r = engine.ingest_batch([
            _evt("evt-91", 91, "2026-09-12T10:00:00+08:00",
                 {"mood": "low", "touch": "hold"}),
        ], received_at="2026-09-14T11:05:00+08:00")[0]
        self.assertEqual(r["resolution"], "handled")
        self.assertEqual(r["consent"], "c-kid-A@v1")
        r = engine.ingest_batch([
            _evt("evt-92", 92, "2026-09-14T11:10:00+08:00",
                 {"mood": "low"}),
        ], received_at="2026-09-14T11:10:02+08:00")[0]
        self.assertEqual(r["resolution"], "handled")
        self.assertEqual(r["consent"], "c-guest-B@v1")
        self.assertEqual(r["action"], "breathing_prompt")

        # ---- 撤回与危险事件相撞：危险发生于撤回前，断网延迟到撤回后到达 ----
        cm.create(CHILD, "orbit-08", "kid-B", "home-B", guardian_id="g-B",
                  consent_id="c-kid-B",
                  allowed_actions=["hug_back"],
                  effective_from="2026-09-14T12:00:00+08:00")
        cm.revoke("c-guest-B", at="2026-09-14T13:00:00+08:00")
        cm.revoke("c-kid-B", at="2026-09-14T13:00:00+08:00")
        danger = engine.ingest_batch([
            _evt("evt-100", 100, "2026-09-14T12:59:00+08:00",
                 {"risk": "danger", "confidence": 0.98, "speech_text": "secret"}),
        ], received_at="2026-09-14T13:30:00+08:00")[0]
        self.assertEqual(danger["resolution"], "escalated")
        # 12:59 访客授权尚未撤回且优先级最高，指向事件当刻的访客版本
        self.assertEqual(danger["consent"], "c-guest-B@v1")
        case_id = danger["escalation_case"]

        # 撤回后的非危险事件：立即停止个性化处理
        after = engine.ingest_batch([
            _evt("evt-101", 101, "2026-09-14T13:31:00+08:00", {"mood": "low"}),
        ], received_at="2026-09-14T13:31:02+08:00")[0]
        self.assertEqual(after["resolution"], "suppressed_no_consent")

        # 危险最小事实不含语音/置信度，保留依据清楚
        fact = store.fact_for_event("evt-100")
        self.assertEqual(fact["retain_reason"], "explicit_danger_human_confirmation")
        self.assertEqual(json.loads(store.get_event("evt-100")["signals"]), {})

        # ---- 人工确认升级流程 ----
        decision = engine.decide_escalation(case_id, "confirmed", "officer-liu")
        self.assertTrue(decision["changed"])
        again = engine.decide_escalation(case_id, "resolved", "officer-liu")
        self.assertTrue(again["changed"])

        # ---- 客服只能看脱敏解释，且能回答“为什么这样回应” ----
        explain = privacy.support_explain_event("evt-71")
        text = json.dumps(explain, ensure_ascii=False)
        self.assertNotIn("kid-A", text)
        self.assertNotIn("0.72", text)
        self.assertEqual(explain["consent"]["scope"], "儿童监护")
        self.assertEqual(explain["responses"][0]["response"], "家庭批准动作：轻抱回应")

        # ---- 删除：可删资料给清理结果，最小事实单列保留依据 ----
        cleanup = privacy.delete_subject_data(CHILD, "kid-B", "g-B")
        self.assertIn("evt-101", cleanup["deleted"]["event_ids"])
        self.assertIsNone(store.get_event("evt-101"))
        retained = {f["event_id"] for f in cleanup["retained_minimum_facts"]}
        self.assertEqual(retained, {"evt-100"})
        self.assertEqual(store.list_deletions("kid-B")[0]["status"], "completed")

        # ---- 监护人导出权限内记录：只含本家庭、且每条可追溯 ----
        export_a = privacy.guardian_export("g-A", "home-A")
        ids_a = {d["event_id"] for d in export_a["dispositions"]}
        self.assertIn("evt-71", ids_a)
        self.assertNotIn("evt-92", ids_a)
        for d in export_a["dispositions"]:
            self.assertIsNotNone(d["consent_id"])
            self.assertIsNotNone(d["consent_version"])
            self.assertTrue(d["rules_version"])

        store.close()

        # ---- 服务重启：状态恢复；重传仍只处置一次，通知不重复 ----
        store2 = Store(db)
        engine2 = Engine(store2, ConsentManager(store2))
        dup_after_restart = engine2.ingest_batch([
            _evt("evt-73", 73, "2026-09-12T14:00:40+08:00",
                 {"risk": "needs_attention", "confidence": 0.77}),
        ], received_at="2026-09-15T00:00:00+08:00")[0]
        self.assertEqual(dup_after_restart["status"], "duplicate")
        care = [n for n in store2.list_notifications() if n["ntype"] == "care_alert"]
        self.assertEqual(len(care), 1)
        self.assertEqual(store2.get_escalation(case_id)["status"], "resolved")
        store2.close()

    def test_contract_sample_is_ingestible(self):
        """contracts/device-events.json 批量样例：沿用 pairing/sequence/信号含义。"""
        with open(os.path.join(ROOT, "contracts", "device-events.json"), encoding="utf-8") as f:
            sample = json.load(f)
        store = Store(":memory:")
        cm = ConsentManager(store)
        engine = Engine(store, cm)
        cm.create(FAMILY, "orbit-08", "fam-A", "home-A", consent_id="c-fam",
                  allowed_actions=["hug_back", "soft_glow"],
                  effective_from="2026-09-12T00:00:00+08:00")
        results = engine.ingest_batch(sample,
                                      received_at="2026-09-12T14:00:10+08:00")
        self.assertEqual([r["status"] for r in results], ["processed", "processed"])
        self.assertEqual(results[0]["classification"], "low")
        self.assertEqual(results[1]["classification"], "abnormal")
        self.assertEqual(results[0]["action"], "hug_back")
        # 配对周期与序号如实落库
        row = store.get_event("evt-72")
        self.assertEqual(row["pairing_id"], "pair-home-a")
        self.assertEqual(row["sequence"], 72)


if __name__ == "__main__":
    unittest.main()
