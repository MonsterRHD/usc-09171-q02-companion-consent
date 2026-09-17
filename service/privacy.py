"""数据清理、最小事实保留、客服脱敏视图与监护人导出。

* 可删除资料：删除事件明细与处置动作；
* 安全审计最小事实：危险升级相关事实独立留存，删除请求不影响，并逐条给出保留依据；
* 客服只能看到脱敏解释（不含主体身份、置信度、信号细节）；
* 监护人只能导出本人权限内的授权版本与处置记录。
"""

from __future__ import annotations

import json
import uuid

from . import clock
from .store import Store

SCOPE_LABELS = {"family": "家庭成员", "child": "儿童监护", "guest": "临时访客"}
RESUTION_LABELS = {
    "handled": "已按当时授权处置",
    "escalated": "明确危险信号，已进入人工确认",
    "suppressed_no_consent": "事件时刻无有效授权，未做个性化处理",
    "suppressed_transferred": "设备已转借，原家庭授权终止，未做个性化处理",
    "suppressed_revoked": "授权已撤回，未做新的个性化处理",
}


def _mask(value: str | None, left: int = 3) -> str | None:
    if not value:
        return value
    if len(value) <= left:
        return value[:1] + "**"
    return value[:left] + "**"


class PrivacyService:
    def __init__(self, store: Store):
        self.store = store

    # ---------- 删除：可删资料与必留最小事实分开 ----------

    def delete_subject_data(self, scope_type: str, subject_id: str,
                            requested_by: str) -> dict:
        event_ids = self.store.events_for_subject(subject_id)
        retained_facts = []
        deletable = []
        for eid in event_ids:
            fact = self.store.fact_for_event(eid)
            if fact:
                retained_facts.append({
                    "fact_id": fact["fact_id"],
                    "event_id": fact["event_id"],
                    "device_id": fact["device_id"],
                    "captured_at": fact["captured_at"],
                    "classification": fact["classification"],
                    "retain_reason": fact["retain_reason"],
                    "basis": fact["basis"],
                    "case_id": fact["case_id"],
                    "rules_version": fact["rules_version"],
                })
            else:
                deletable.append(eid)

        deleted_events, deleted_actions = self.store.delete_events_by_id(deletable)
        result = {
            "scope_type": scope_type,
            "subject_id": subject_id,
            "requested_by": requested_by,
            "completed_at": clock.now_iso(),
            "deleted": {
                "events": deleted_events,
                "actions": deleted_actions,
                "event_ids": deletable,
            },
            "retained_minimum_facts": retained_facts,
            "retention_policy": (
                "危险升级与安全审计所需最小事实依法/安全义务保留，"
                "不含情绪摘要、语音信息与个性化处置内容"
            ),
        }
        self.store.record_deletion({
            "delete_id": f"del-{uuid.uuid4().hex[:10]}",
            "scope_type": scope_type,
            "subject_id": subject_id,
            "requested_by": requested_by,
            "requested_at": clock.now_iso(),
            "events_deleted": deleted_events,
            "actions_deleted": deleted_actions,
            "facts_retained": len(retained_facts),
            "result": result,
        })
        return result

    # ---------- 客服脱敏解释 ----------

    def support_explain_event(self, event_id: str) -> dict | None:
        """客服视图：回答“谁同意了什么、设备为什么这样回应”，全程脱敏。"""
        row = self.store.get_event(event_id)
        if not row:
            return None
        record = json.loads(row["stored_record"]) if row["stored_record"] else {}
        consent_explain = None
        if row["consent_id"]:
            chain = self.store.consent_chain(row["consent_id"])
            v = next((c for c in chain if c["version"] == row["consent_version"]), None)
            if v:
                consent_explain = {
                    "scope": SCOPE_LABELS.get(v["scope_type"], v["scope_type"]),
                    "guardian_granted": bool(v["guardian_id"]),
                    "version": v["version"],
                    "effective_from": v["effective_from"],
                    "expires_at": v["expires_at"],
                    "revoked_at": v["revoked_at"],
                    "transferred_at": v["transferred_at"],
                    "approved_actions_count": len(json.loads(v["allowed_actions"])),
                }
        actions = []
        for a in self.store.event_actions(event_id):
            detail = json.loads(a["detail"])
            item = {"kind": a["kind"]}
            if a["kind"] == "comfort":
                item["response"] = f"家庭批准动作：{detail.get('label')}"
            elif a["kind"] == "escalate":
                item["response"] = "已转人工确认"
                item["case_id"] = detail.get("case_id")
            else:
                item["response"] = "系统动作"
            actions.append(item)
        return {
            "event_id": event_id,
            "device_ref": _mask(row["device_id"], left=4),
            "captured_at": row["captured_at"],
            "classification": row["classification"],
            "why": RESUTION_LABELS.get(row["resolution"], row["resolution"]),
            "retention": row["retention_level"],
            "consent": consent_explain,
            "rules_version": row["rules_version"],
            "responses": actions,
            "note": "本视图已脱敏：不含儿童/访客身份、语音与情绪量化细节",
        }

    # ---------- 监护人导出（权限范围内） ----------

    def guardian_export(self, guardian_id: str, household_id: str) -> dict:
        consents: list[dict] = []
        subject_ids: set[str] = set()
        for row in self.store.all_consent_rows():
            in_scope = (
                row["scope_type"] == "family" and row["household_id"] == household_id
            ) or (
                row["scope_type"] in ("child", "guest")
                and row["guardian_id"] == guardian_id
                and row["household_id"] == household_id
            )
            if not in_scope:
                continue
            subject_ids.add(row["subject_id"])
            consents.append({
                "consent_id": row["consent_id"],
                "version": row["version"],
                "scope_type": row["scope_type"],
                "subject_id": row["subject_id"],
                "granted_at": row["granted_at"],
                "effective_from": row["effective_from"],
                "expires_at": row["expires_at"],
                "revoked_at": row["revoked_at"],
                "transferred_at": row["transferred_at"],
                "allowed_actions": json.loads(row["allowed_actions"]),
                "retention_mood": row["retention_mood"],
                "retention_risk": row["retention_risk"],
                "voice_summary": bool(row["voice_summary"]),
                "document": json.loads(row["document"]),
            })

        dispositions = []
        for row in self.store.list_events():
            record = json.loads(row["stored_record"]) if row["stored_record"] else {}
            if record.get("subject_id") not in subject_ids:
                continue
            dispositions.append({
                "event_id": row["event_id"],
                "device_id": row["device_id"],
                "captured_at": row["captured_at"],
                "classification": row["classification"],
                "resolution": row["resolution"],
                "retention_level": row["retention_level"],
                "consent_id": row["consent_id"],
                "consent_version": row["consent_version"],
                "rules_version": row["rules_version"],
                "actions": [
                    {"kind": a["kind"], "detail": json.loads(a["detail"])}
                    for a in self.store.event_actions(row["event_id"])
                ],
            })

        return {
            "exported_by": guardian_id,
            "household_id": household_id,
            "exported_at": clock.now_iso(),
            "consents": consents,
            "dispositions": dispositions,
        }
