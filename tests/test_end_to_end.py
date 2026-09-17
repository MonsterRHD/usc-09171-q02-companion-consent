"""端到端流程：两个家庭转借、儿童授权到期、乱序回传、撤回与危险事件相撞、
服务重启连成一条流程，验证每个响应都能指向对应授权和规则版本。"""

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from service.hub import ConsentHub
from service.rules import RULES_VERSION

BASE = datetime(2026, 9, 12, 6, 0, 0, tzinfo=timezone.utc)  # 即 2026-09-12T14:00:00+08:00
CONTRACT_SAMPLE = Path(__file__).resolve().parent.parent / "contracts" / "device-events.json"


def make_event(event_id, sequence, signals, captured_at, pairing_id):
    return {
        "event_id": event_id,
        "device_id": "orbit-08",
        "pairing_id": pairing_id,
        "sequence": sequence,
        "captured_at": captured_at.isoformat(),
        "signals": signals,
    }


class EndToEndFlowTest(unittest.TestCase):
    def setUp(self):
        self.clock = self._clock()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store_path = os.path.join(self.tmp.name, "store.json")
        self.hub = ConsentHub(store_path=self.store_path, clock=self.clock)

    def test_full_family_trial_flow(self):
        hub = self.hub

        # ---- 家庭 A 试用：儿童监护授权 + 家庭成员授权分开管理 ----
        hub.grant_consent({
            "consent_id": "c-a-01",
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
            "expires_at": (BASE + timedelta(days=2)).isoformat(),
        })
        hub.grant_consent({
            "consent_id": "c-a-02",
            "device_id": "orbit-08",
            "pairing_id": "pair-home-a",
            "subject_type": "family_member",
            "subject_id": "member-a",
            "granted_by": "guardian-a",
            "permissions": {"retention": "summary", "actions": [], "personalization": False},
            "effective_from": (BASE - timedelta(days=1)).isoformat(),
        })

        # ---- 仓库样例批量事件：沿用 pairing_id / sequence / captured_at / signals 语义 ----
        sample = json.loads(CONTRACT_SAMPLE.read_text(encoding="utf-8"))
        result = hub.ingest_batch(sample, received_at=BASE + timedelta(minutes=5))
        self.assertEqual([r["status"] for r in result["results"]], ["processed", "processed"])
        d71 = hub.get_disposition("evt-71")["disposition"]
        self.assertEqual(d71["outcome"], "action_executed")
        self.assertEqual(d71["action"], {"type": "breathing_light"})
        self.assertEqual(d71["rule_version"], RULES_VERSION)
        self.assertEqual(
            d71["primary_consent"], {"consent_id": "c-a-01", "version": 1}
        )

        # 断网重传同一批：只产生一次处置。
        replay = hub.ingest_batch(sample, received_at=BASE + timedelta(minutes=6))
        self.assertEqual([r["status"] for r in replay["results"]], ["duplicate", "duplicate"])

        # ---- 乱序回传：seq 74 先于 seq 73 到达，连续异常只提醒一次 ----
        late_batch = [
            make_event("evt-74", 74, {"risk": "needs_attention"}, BASE + timedelta(seconds=40), "pair-home-a"),
            make_event("evt-73", 73, {"touch": "hold"}, BASE + timedelta(seconds=30), "pair-home-a"),
        ]
        hub.ingest_batch([late_batch[0]])
        reminders = [n for n in hub.list_notifications(device_id="orbit-08")
                     if n["kind"] == "care_reminder"]
        self.assertEqual(len(reminders), 1)  # evt-72 与 evt-74 在序列上连续异常
        hub.ingest_batch([late_batch[1]])
        self.assertEqual(
            len([n for n in hub.list_notifications(device_id="orbit-08")
                 if n["kind"] == "care_reminder"]),
            1,
        )

        # ---- 儿童授权到期：个性化立即停止 ----
        expired = make_event("evt-75", 75, {"mood": "low"}, BASE + timedelta(days=3), "pair-home-a")
        hub.ingest_batch([expired])
        d75 = hub.get_disposition("evt-75")["disposition"]
        self.assertEqual(d75["outcome"], "personalization_blocked")
        self.assertEqual(d75["primary_consent"], {"consent_id": "c-a-02", "version": 1})

        # ---- 设备转借给家庭 B：旧配对关闭，新配对走新家庭授权 ----
        self.clock.set(BASE + timedelta(days=3, hours=1))
        transfer = hub.transfer_device("orbit-08", new_pairing_id="pair-home-b")
        self.assertEqual(transfer["closed_pairing_ids"], ["pair-home-a"])

        # 转借后才送达的旧配对事件：captured_at 在关闭前，仍按发生当刻的家庭 A 授权处置。
        old_late = make_event("evt-70", 70, {"mood": "low"}, BASE - timedelta(seconds=1), "pair-home-a")
        hub.ingest_batch([old_late])
        d70 = hub.get_disposition("evt-70")["disposition"]
        self.assertEqual(d70["outcome"], "action_executed")
        self.assertEqual(d70["primary_consent"], {"consent_id": "c-a-01", "version": 1})

        # 关闭之后（超出时钟漂移容忍）的旧配对事件被拒绝。
        stale = make_event("evt-99", 99, {"mood": "low"}, BASE + timedelta(days=4), "pair-home-a")
        hub.ingest_batch([stale])
        self.assertEqual(
            hub.get_disposition("evt-99")["disposition"]["outcome"], "rejected_pairing_closed"
        )

        hub.grant_consent({
            "consent_id": "c-b-01",
            "device_id": "orbit-08",
            "pairing_id": "pair-home-b",
            "subject_type": "child",
            "subject_id": "child-b",
            "granted_by": "guardian-b",
            "permissions": {"retention": "full", "actions": ["tell_story"], "personalization": True},
            "effective_from": (BASE + timedelta(days=3, hours=2)).isoformat(),
        })
        self.clock.set(BASE + timedelta(days=3, hours=3))
        new_home = make_event("evt-201", 1, {"mood": "low"}, BASE + timedelta(days=3, hours=3), "pair-home-b")
        hub.ingest_batch([new_home])
        d201 = hub.get_disposition("evt-201")["disposition"]
        self.assertEqual(d201["outcome"], "action_executed")
        self.assertEqual(d201["action"], {"type": "tell_story"})
        self.assertEqual(d201["primary_consent"], {"consent_id": "c-b-01", "version": 1})

        # ---- 撤回与危险事件相撞：个性化停止，危险信号仍进入人工确认 ----
        self.clock.set(BASE + timedelta(days=4))
        hub.revoke_consent("c-b-01")
        danger = make_event("evt-202", 2, {"risk": "danger"}, BASE + timedelta(days=4, minutes=1), "pair-home-b")
        hub.ingest_batch([danger])
        d202 = hub.get_disposition("evt-202")["disposition"]
        self.assertEqual(d202["outcome"], "escalation_pending")
        self.assertEqual(d202["consent_versions"], [])
        self.assertEqual(d202["retention"], "minimal")
        confirmed = hub.confirm_escalation("esc-evt-202", confirmed_by="staff-01")
        self.assertEqual(confirmed["status"], "confirmed")

        # ---- 服务重启：状态恢复，重传不重复处置、不重复通知 ----
        notifications_before = len(hub.list_notifications(device_id="orbit-08"))
        restarted = ConsentHub(store_path=self.store_path, clock=self.clock)
        replay = restarted.ingest_batch([new_home, danger])
        self.assertEqual([r["status"] for r in replay["results"]], ["duplicate", "duplicate"])
        self.assertEqual(
            len(restarted.list_notifications(device_id="orbit-08")), notifications_before
        )
        self.assertEqual(
            restarted.list_escalations(status="confirmed")[0]["confirmed_by"], "staff-01"
        )

        # ---- 删除：可删资料清除，安全审计与同意审计最小事实保留 ----
        job = restarted.start_deletion("orbit-08", "pair-home-a", requested_by="guardian-a")
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["progress"]["processed"], job["progress"]["total"])
        self.assertGreaterEqual(job["deleted"]["dispositions"], 5)
        retained_consents = {
            item["id"] for item in job["retained"] if item["kind"] == "consent_record"
        }
        self.assertEqual(retained_consents, {"c-a-01", "c-a-02"})
        self.assertTrue(all(item["basis"] for item in job["retained"]))

        # 普通事件已清除；触发关怀提醒的事件保留最小事实。
        self.assertIsNone(restarted.get_disposition("evt-71"))
        fact = restarted.get_disposition("evt-74")["disposition"]
        self.assertEqual(fact["retention_basis"], "safety_audit")
        self.assertEqual(fact["rule_version"], RULES_VERSION)

        # 删除后重传不复活。
        again = restarted.ingest_batch(sample)
        self.assertEqual([r["status"] for r in again["results"]], ["duplicate", "duplicate"])
        self.assertIsNone(restarted.get_disposition("evt-71"))

        # ---- 客服只能看脱敏解释 ----
        view = restarted.support_explanation("evt-74")
        self.assertEqual(view["rule_version"], RULES_VERSION)
        view_text = json.dumps(view, ensure_ascii=False)
        for sensitive in ("child-a", "guardian-a", "member-a", "signals", "needs_attention"):
            self.assertNotIn(sensitive, view_text)
        purged_view = restarted.support_explanation("evt-71")
        self.assertTrue(purged_view["purged"])

        # ---- 监护人各自导出权限内的记录 ----
        export_a = restarted.guardian_export("guardian-a")
        consent_ids_a = {chain[0]["consent_id"] for chain in export_a["consents"]}
        self.assertEqual(consent_ids_a, {"c-a-01", "c-a-02"})
        self.assertTrue(export_a["dispositions"])
        self.assertTrue(
            all(d["pairing_id"] == "pair-home-a" for d in export_a["dispositions"])
        )
        self.assertEqual([job["job_id"] for job in export_a["deletion_jobs"]], [job["job_id"]])

        export_b = restarted.guardian_export("guardian-b")
        self.assertEqual(
            {chain[0]["consent_id"] for chain in export_b["consents"]}, {"c-b-01"}
        )
        self.assertTrue(
            all(d["pairing_id"] == "pair-home-b" for d in export_b["dispositions"])
        )
        self.assertNotIn(
            "c-a-01",
            json.dumps(export_b, ensure_ascii=False),
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
