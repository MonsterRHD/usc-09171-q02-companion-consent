"""设备同意与响应中枢的核心领域逻辑。

关键语义：
- 家庭成员、儿童监护、临时访客的授权分开记录，每次变更产生新的同意版本；
- 事件处置使用 captured_at（事件发生当刻）有效的同意版本，而不是接收时的设置；
- 同一设备同一配对周期内有多份有效授权时，按 儿童监护 > 家庭成员 > 临时访客 取主授权；
- 转借（配对周期关闭）与授权撤回立即作用于新事件；明确危险信号始终进入人工确认升级；
- 断网重传、时钟漂移、重复到达只产生一次处置；通知按去重键只发一次；
- 删除任务清除可删资料，保留安全审计与同意审计要求的最小事实。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from .rules import ANOMALY_STREAK_THRESHOLD, RULES_VERSION, classify, select_comfort_action
from .store import JsonStore

SUBJECT_TYPES = ("family_member", "child", "visitor")
SUBJECT_PRIORITY = {"child": 0, "family_member": 1, "visitor": 2}
RETENTIONS = ("minimal", "summary", "full")
SUMMARY_SIGNAL_KEYS = ("mood", "risk")
# 设备时钟与服务端可能存在的漂移容忍窗口，仅用于配对关闭边界的判定。
CLOCK_SKEW_TOLERANCE = timedelta(minutes=5)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _parse_ts(value, field="timestamp"):
    if not isinstance(value, str):
        raise ValueError(f"{field} 必须是 ISO 8601 字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} 不是合法的 ISO 8601 时间: {value!r}") from None
    if parsed.tzinfo is None:
        raise ValueError(f"{field} 必须携带时区偏移: {value!r}")
    return parsed.astimezone(timezone.utc)


def _payload_hash(raw) -> str:
    blob = json.dumps(raw, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class ConsentHub:
    def __init__(self, store_path=None, clock=None):
        self.store = JsonStore(store_path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    # 同意管理
    # ------------------------------------------------------------------

    def grant_consent(self, payload):
        """登记一份授权；同一主体再次登记时生成新的同意版本并取代旧版本。"""
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是对象")
        for field in ("device_id", "pairing_id", "subject_type", "subject_id", "granted_by"):
            if not payload.get(field):
                raise ValueError(f"缺少字段 {field}")
        if payload["subject_type"] not in SUBJECT_TYPES:
            raise ValueError(f"subject_type 必须是 {SUBJECT_TYPES} 之一")
        permissions = payload.get("permissions") or {}
        retention = permissions.get("retention", "minimal")
        if retention not in RETENTIONS:
            raise ValueError(f"retention 必须是 {RETENTIONS} 之一")
        actions = permissions.get("actions") or []
        if not isinstance(actions, list):
            raise ValueError("permissions.actions 必须是数组")
        personalization = bool(permissions.get("personalization", False))
        now = self.clock()
        effective_from = (
            _parse_ts(payload["effective_from"], "effective_from")
            if payload.get("effective_from")
            else now
        )
        expires_at = (
            _parse_ts(payload["expires_at"], "expires_at") if payload.get("expires_at") else None
        )

        with self.store.locked() as data:
            consent_id = payload.get("consent_id")
            chain = None
            if consent_id:
                chain = data["consents"].get(consent_id)
                if chain is None:
                    chain = data["consents"][consent_id] = []
                elif (
                    chain[0]["device_id"] != payload["device_id"]
                    or chain[0]["subject_id"] != payload["subject_id"]
                ):
                    raise ValueError(f"consent_id {consent_id} 已属于其他设备或主体")
            if chain is None:
                for existing_id, existing in data["consents"].items():
                    if (
                        existing
                        and existing[0]["device_id"] == payload["device_id"]
                        and existing[0]["pairing_id"] == payload["pairing_id"]
                        and existing[0]["subject_id"] == payload["subject_id"]
                    ):
                        consent_id, chain = existing_id, existing
                        break
            if chain is None:
                consent_id = f"c-{self._next(data, 'consent')}"
                chain = data["consents"][consent_id] = []

            for version in chain:
                if version["revoked_at"] is None and version["superseded_at"] is None:
                    version["superseded_at"] = _iso(effective_from)
            record = {
                "consent_id": consent_id,
                "version": len(chain) + 1,
                "device_id": payload["device_id"],
                "pairing_id": payload["pairing_id"],
                "subject_type": payload["subject_type"],
                "subject_id": payload["subject_id"],
                "granted_by": payload["granted_by"],
                "permissions": {
                    "retention": retention,
                    "actions": [str(action) for action in actions],
                    "personalization": personalization,
                },
                "effective_from": _iso(effective_from),
                "expires_at": _iso(expires_at) if expires_at else None,
                "revoked_at": None,
                "superseded_at": None,
                "created_at": _iso(now),
            }
            chain.append(record)
        self.store.save()
        return record

    def revoke_consent(self, consent_id):
        """撤回授权链，撤回即刻生效：captured_at 晚于撤回时刻的事件不再适用该授权。"""
        now = self.clock()
        with self.store.locked() as data:
            chain = data["consents"].get(consent_id)
            if not chain:
                return None
            for version in chain:
                if version["revoked_at"] is None:
                    version["revoked_at"] = _iso(now)
        self.store.save()
        return chain

    def list_consents(self, device_id=None, pairing_id=None):
        with self.store.locked() as data:
            chains = [
                chain
                for chain in data["consents"].values()
                if chain
                and (device_id is None or chain[0]["device_id"] == device_id)
                and (pairing_id is None or chain[0]["pairing_id"] == pairing_id)
            ]
        chains.sort(key=lambda chain: chain[0]["consent_id"])
        return chains

    # ------------------------------------------------------------------
    # 事件处置
    # ------------------------------------------------------------------

    def ingest_batch(self, events, received_at=None):
        """批量回传入口。重复到达、断网重传按 event_id 幂等，只产生一次处置。"""
        if not isinstance(events, list):
            raise ValueError("events 必须是数组")
        now = received_at or self.clock()
        with self.store.locked() as data:
            results = [self._ingest_one(data, raw, now) for raw in events]
        self.store.save()
        return {"results": results}

    def get_disposition(self, event_id):
        with self.store.locked() as data:
            disposition = data["dispositions"].get(event_id)
            if disposition is None:
                return None
            return {"disposition": disposition, "event": data["events"].get(event_id)}

    def list_dispositions(self, device_id=None, pairing_id=None):
        with self.store.locked() as data:
            result = [
                disposition
                for disposition in data["dispositions"].values()
                if (device_id is None or disposition["device_id"] == device_id)
                and (pairing_id is None or disposition["pairing_id"] == pairing_id)
            ]
        result.sort(key=lambda d: (_parse_ts(d["captured_at"]), d["event_id"]))
        return result

    def _ingest_one(self, data, raw, received_at):
        if not isinstance(raw, dict):
            return {"event_id": None, "status": "error", "error": "事件必须是对象"}
        try:
            event = self._validate_event(raw)
        except ValueError as exc:
            return {"event_id": raw.get("event_id"), "status": "error", "error": str(exc)}

        event_id = event["event_id"]
        existing = data["events"].get(event_id)
        if existing is not None:
            status = "duplicate" if existing.get("payload_hash") == _payload_hash(raw) else "conflict"
            return {
                "event_id": event_id,
                "status": status,
                "disposition_id": existing.get("disposition_id"),
            }

        device_id = event["device_id"]
        pairing_id = event["pairing_id"]
        pairing_key = f"{device_id}|{pairing_id}"
        pairing = data["pairings"].get(pairing_key)
        if pairing is None:
            pairing = {
                "device_id": device_id,
                "pairing_id": pairing_id,
                "closed_at": None,
                "trail": [],
                "care_episode": None,
            }
            data["pairings"][pairing_key] = pairing

        captured = event["captured_dt"]
        if (
            pairing["closed_at"]
            and captured > _parse_ts(pairing["closed_at"], "closed_at") + CLOCK_SKEW_TOLERANCE
        ):
            disposition = self._record_disposition(
                data, raw, event, received_at,
                classification="rejected",
                outcome="rejected_pairing_closed",
                retention="minimal",
                consent_versions=[],
                primary_consent=None,
                action=None,
                notification_ids=[],
                escalation_id=None,
            )
            return {
                "event_id": event_id,
                "status": "processed",
                "disposition_id": disposition["disposition_id"],
                "outcome": disposition["outcome"],
            }

        effective, refs, primary = self._consent_at(data, device_id, pairing_id, captured)
        classification = classify(event["signals"])
        pairing["trail"].append(
            {"sequence": event["sequence"], "classification": classification, "event_id": event_id}
        )

        action = None
        notification_ids = []
        escalation_id = None

        if classification == "danger":
            # 明确危险信号：无论授权状态如何都进入人工确认升级流程。
            escalation_id = f"esc-{event_id}"
            if escalation_id not in data["escalations"]:
                data["escalations"][escalation_id] = {
                    "escalation_id": escalation_id,
                    "event_id": event_id,
                    "device_id": device_id,
                    "pairing_id": pairing_id,
                    "captured_at": event["captured_at"],
                    "status": "pending",
                    "created_at": _iso(received_at),
                    "confirmed_at": None,
                    "confirmed_by": None,
                    "rule_version": RULES_VERSION,
                }
            notification_ids.append(self._notify(
                data,
                f"esc-notify:{event_id}",
                "escalation",
                device_id,
                pairing_id,
                f"设备 {device_id} 上报危险信号，已生成待人工确认的升级单 {escalation_id}。",
                received_at,
            ))
            outcome = "escalation_pending"
        elif classification == "anomaly":
            streak_start = self._update_care_episode(pairing)
            if streak_start is None:
                outcome = "recorded"
            elif effective["personalization"]:
                notification_ids.append(self._notify(
                    data,
                    f"care:{pairing_key}:{streak_start}",
                    "care_reminder",
                    device_id,
                    pairing_id,
                    f"设备 {device_id} 近期连续出现异常信号，请关注使用人状态。",
                    received_at,
                ))
                outcome = "care_reminder_sent"
            else:
                outcome = "personalization_blocked"
        elif classification == "comfort":
            if not effective["personalization"]:
                outcome = "personalization_blocked"
            else:
                chosen = select_comfort_action(effective["actions"])
                if chosen:
                    action = {"type": chosen}
                    outcome = "action_executed"
                else:
                    outcome = "no_approved_action"
        else:
            outcome = "recorded"

        disposition = self._record_disposition(
            data, raw, event, received_at,
            classification=classification,
            outcome=outcome,
            retention=effective["retention"],
            consent_versions=refs,
            primary_consent=primary,
            action=action,
            notification_ids=notification_ids,
            escalation_id=escalation_id,
        )
        return {
            "event_id": event_id,
            "status": "processed",
            "disposition_id": disposition["disposition_id"],
            "outcome": outcome,
        }

    def _record_disposition(
        self, data, raw, event, received_at, *, classification, outcome, retention,
        consent_versions, primary_consent, action, notification_ids, escalation_id,
    ):
        event_id = event["event_id"]
        record = {
            "event_id": event_id,
            "disposition_id": f"disp-{event_id}",
            "device_id": event["device_id"],
            "pairing_id": event["pairing_id"],
            "sequence": event["sequence"],
            "captured_at": event["captured_at"],
            "received_at": _iso(received_at),
            "payload_hash": _payload_hash(raw),
            "retention": retention,
        }
        if retention == "full":
            record["signals"] = event["signals"]
        elif retention == "summary":
            record["signals"] = {
                key: value for key, value in event["signals"].items() if key in SUMMARY_SIGNAL_KEYS
            }
        data["events"][event_id] = record

        disposition = {
            "disposition_id": record["disposition_id"],
            "event_id": event_id,
            "device_id": event["device_id"],
            "pairing_id": event["pairing_id"],
            "sequence": event["sequence"],
            "captured_at": event["captured_at"],
            "processed_at": _iso(received_at),
            "classification": classification,
            "outcome": outcome,
            "consent_versions": consent_versions,
            "primary_consent": primary_consent,
            "rule_version": RULES_VERSION,
            "action": action,
            "retention": retention,
            "notifications": notification_ids,
            "escalation_id": escalation_id,
        }
        disposition["explanation"] = self._explain(disposition)
        data["dispositions"][event_id] = disposition
        return disposition

    def _consent_at(self, data, device_id, pairing_id, moment):
        """解析事件发生当刻的有效授权：每条授权链取当刻最新版本，再按主体优先级取主授权。"""
        active = []
        for chain in data["consents"].values():
            chain_active = []
            for version in chain:
                if version["device_id"] != device_id or version["pairing_id"] != pairing_id:
                    continue
                if _parse_ts(version["effective_from"]) > moment:
                    continue
                if version.get("revoked_at") and _parse_ts(version["revoked_at"]) <= moment:
                    continue
                if version.get("expires_at") and _parse_ts(version["expires_at"]) <= moment:
                    continue
                if version.get("superseded_at") and _parse_ts(version["superseded_at"]) <= moment:
                    continue
                chain_active.append(version)
            if chain_active:
                active.append(max(chain_active, key=lambda v: (v["effective_from"], v["version"])))
        if not active:
            return {"retention": "minimal", "actions": [], "personalization": False}, [], None

        best_priority = min(SUBJECT_PRIORITY[v["subject_type"]] for v in active)
        primary = max(
            (v for v in active if SUBJECT_PRIORITY[v["subject_type"]] == best_priority),
            key=lambda v: (v["effective_from"], v["version"]),
        )
        refs = [
            {"consent_id": v["consent_id"], "version": v["version"], "subject_type": v["subject_type"]}
            for v in sorted(active, key=lambda v: (SUBJECT_PRIORITY[v["subject_type"]], v["consent_id"]))
        ]
        primary_ref = {"consent_id": primary["consent_id"], "version": primary["version"]}
        return dict(primary["permissions"]), refs, primary_ref

    def _update_care_episode(self, pairing):
        """按配对周期内的 sequence 重算连续异常；返回需要新通知的连续段起点，否则 None。"""
        trail = sorted(pairing["trail"], key=lambda entry: entry["sequence"])
        streak = 0
        for entry in reversed(trail):
            if entry["classification"] == "anomaly":
                streak += 1
            else:
                break
        episode = pairing.get("care_episode")
        if streak < ANOMALY_STREAK_THRESHOLD:
            if episode and streak == 0:
                pairing["care_episode"] = None
            return None
        top_sequence = trail[-1]["sequence"]
        streak_start = trail[len(trail) - streak]["sequence"]
        if episode:
            broken = any(
                entry["classification"] != "anomaly" and entry["sequence"] > episode["end"]
                for entry in trail
            )
            if not broken:
                episode["end"] = top_sequence
                return None
        pairing["care_episode"] = {"start": streak_start, "end": top_sequence}
        return streak_start

    def _notify(self, data, dedupe_key, kind, device_id, pairing_id, message, now):
        existing = data["notifications"].get(dedupe_key)
        if existing:
            return existing["notification_id"]
        notification_id = f"ntf-{self._next(data, 'notification')}"
        data["notifications"][dedupe_key] = {
            "notification_id": notification_id,
            "dedupe_key": dedupe_key,
            "kind": kind,
            "device_id": device_id,
            "pairing_id": pairing_id,
            "message": message,
            "minimal": True,
            "created_at": _iso(now),
        }
        return notification_id

    @staticmethod
    def _explain(disposition):
        refs = "、".join(
            f"{ref['consent_id']}@v{ref['version']}"
            for ref in disposition.get("consent_versions", [])
        ) or "无有效授权"
        basis = f"依据授权[{refs}]与规则{disposition.get('rule_version')}"
        outcome = disposition.get("outcome")
        action = disposition.get("action") or {}
        if outcome == "action_executed":
            return f"识别到低落情绪，{basis}，执行家庭批准动作 {action.get('type')}。"
        if outcome == "no_approved_action":
            return f"识别到低落情绪，{basis}，但家庭批准动作清单为空，仅记录不动作。"
        if outcome == "personalization_blocked":
            return f"{basis}，个性化处理未获授权（未授予、已过期或已撤回），事件按最小粒度留存。"
        if outcome == "care_reminder_sent":
            return f"连续异常信号达到阈值，{basis}，生成最少信息关怀提醒。"
        if outcome == "escalation_pending":
            return f"检测到明确危险信号，{basis}，生成待人工确认的升级单 {disposition.get('escalation_id')}。"
        if outcome == "rejected_pairing_closed":
            return "配对周期已关闭（设备转借），超出时钟漂移容忍的事件不再处置。"
        return f"{basis}，事件已记录。"

    # ------------------------------------------------------------------
    # 升级人工确认
    # ------------------------------------------------------------------

    def confirm_escalation(self, escalation_id, confirmed_by):
        if not confirmed_by:
            raise ValueError("confirmed_by 必填")
        now = self.clock()
        with self.store.locked() as data:
            escalation = data["escalations"].get(escalation_id)
            if escalation is None:
                return None
            if escalation["status"] != "confirmed":
                escalation["status"] = "confirmed"
                escalation["confirmed_by"] = confirmed_by
                escalation["confirmed_at"] = _iso(now)
        self.store.save()
        return escalation

    def list_escalations(self, device_id=None, status=None):
        with self.store.locked() as data:
            result = [
                escalation
                for escalation in data["escalations"].values()
                if (device_id is None or escalation["device_id"] == device_id)
                and (status is None or escalation["status"] == status)
            ]
        result.sort(key=lambda e: e["escalation_id"])
        return result

    # ------------------------------------------------------------------
    # 转借与删除
    # ------------------------------------------------------------------

    def transfer_device(self, device_id, new_pairing_id=None):
        """设备转借：关闭当前配对周期，旧配对的新事件（超出时钟漂移容忍）不再处置。"""
        now = self.clock()
        closed = []
        with self.store.locked() as data:
            for pairing in data["pairings"].values():
                if pairing["device_id"] == device_id and pairing["closed_at"] is None:
                    pairing["closed_at"] = _iso(now)
                    closed.append(pairing["pairing_id"])
        self.store.save()
        return {
            "device_id": device_id,
            "closed_pairing_ids": sorted(closed),
            "closed_at": _iso(now),
            "new_pairing_id": new_pairing_id,
        }

    def start_deletion(self, device_id, pairing_id, requested_by):
        """清理配对周期数据：可删资料立即清除，安全审计与同意审计的最小事实保留。"""
        if not requested_by:
            raise ValueError("requested_by 必填")
        now = self.clock()
        with self.store.locked() as data:
            job_id = f"del-{self._next(data, 'deletion')}"
            deleted = {"events": 0, "dispositions": 0, "signal_payloads": 0}
            retained = []
            event_ids = [
                event_id
                for event_id, record in data["events"].items()
                if record.get("device_id") == device_id
                and record.get("pairing_id") == pairing_id
                and not record.get("deleted")
            ]
            total = len(event_ids)
            for event_id in event_ids:
                record = data["events"][event_id]
                disposition = data["dispositions"].get(event_id)
                if "signals" in record:
                    deleted["signal_payloads"] += 1
                safety_relevant = bool(disposition) and (
                    disposition.get("classification") == "danger"
                    or disposition.get("escalation_id")
                    or disposition.get("notifications")
                )
                if safety_relevant:
                    data["dispositions"][event_id] = {
                        "disposition_id": disposition["disposition_id"],
                        "event_id": event_id,
                        "device_id": device_id,
                        "pairing_id": pairing_id,
                        "captured_at": disposition["captured_at"],
                        "classification": disposition["classification"],
                        "outcome": disposition["outcome"],
                        "consent_versions": disposition["consent_versions"],
                        "primary_consent": disposition.get("primary_consent"),
                        "rule_version": disposition["rule_version"],
                        "escalation_id": disposition.get("escalation_id"),
                        "notifications": disposition.get("notifications", []),
                        "retention": "minimal",
                        "retention_basis": "safety_audit",
                        "redacted": True,
                    }
                    retained.append({
                        "kind": "safety_fact",
                        "id": disposition["disposition_id"],
                        "basis": "safety_audit",
                    })
                else:
                    if disposition is not None:
                        del data["dispositions"][event_id]
                        deleted["dispositions"] += 1
                    deleted["events"] += 1
                # 事件本体只留墓碑（哈希与处置 id），重传不会复活个人数据。
                data["events"][event_id] = {
                    "event_id": event_id,
                    "disposition_id": record.get("disposition_id"),
                    "payload_hash": record.get("payload_hash"),
                    "deleted": True,
                    "deleted_by": job_id,
                }

            for consent_id, chain in data["consents"].items():
                if chain and chain[0]["device_id"] == device_id and chain[0]["pairing_id"] == pairing_id:
                    retained.append({"kind": "consent_record", "id": consent_id, "basis": "consent_audit"})
            for escalation_id, escalation in data["escalations"].items():
                if escalation["device_id"] == device_id and escalation["pairing_id"] == pairing_id:
                    retained.append({"kind": "escalation", "id": escalation_id, "basis": "safety_audit"})
            for notification in data["notifications"].values():
                if notification["device_id"] == device_id and notification["pairing_id"] == pairing_id:
                    retained.append({
                        "kind": "notification",
                        "id": notification["notification_id"],
                        "basis": "safety_audit",
                    })

            pairing = data["pairings"].get(f"{device_id}|{pairing_id}")
            if pairing:
                pairing["trail"] = [
                    entry for entry in pairing["trail"] if entry["event_id"] in data["dispositions"]
                ]

            job = {
                "job_id": job_id,
                "device_id": device_id,
                "pairing_id": pairing_id,
                "requested_by": requested_by,
                "status": "completed",
                "started_at": _iso(now),
                "finished_at": _iso(now),
                "progress": {"processed": total, "total": total},
                "deleted": deleted,
                "retained": retained,
            }
            data["deletion_jobs"][job_id] = job
        self.store.save()
        return job

    def get_deletion(self, job_id):
        with self.store.locked() as data:
            return data["deletion_jobs"].get(job_id)

    # ------------------------------------------------------------------
    # 视图：客服脱敏解释与监护人导出
    # ------------------------------------------------------------------

    def support_explanation(self, event_id):
        """客服视角：只给脱敏解释，不含主体身份与信号原文。"""
        with self.store.locked() as data:
            disposition = data["dispositions"].get(event_id)
            if disposition is None:
                record = data["events"].get(event_id)
                if record and record.get("deleted"):
                    return {
                        "event_id": event_id,
                        "purged": True,
                        "deletion_job": record.get("deleted_by"),
                        "explanation": "该事件的个人数据已按删除任务清除，无可提供的处置细节。",
                    }
                return None
            view = {
                "event_id": event_id,
                "device_id": disposition["device_id"],
                "pairing_id": disposition["pairing_id"],
                "captured_at": disposition["captured_at"],
                "classification": disposition["classification"],
                "outcome": disposition["outcome"],
                "action_type": (disposition.get("action") or {}).get("type"),
                "consent_versions": disposition["consent_versions"],
                "rule_version": disposition["rule_version"],
                "retention_basis": disposition.get("retention_basis"),
                "explanation": self._explain(disposition),
            }
            if disposition.get("redacted"):
                view["explanation"] = "个人数据已清除，仅保留安全审计最小事实。" + view["explanation"]
            return view

    def guardian_export(self, guardian_id):
        """监护人导出自己权限内的同意与处置记录。"""
        with self.store.locked() as data:
            chains = [
                chain
                for chain in data["consents"].values()
                if any(
                    version["granted_by"] == guardian_id or version["subject_id"] == guardian_id
                    for version in chain
                )
            ]
            scope = {(chain[0]["device_id"], chain[0]["pairing_id"]) for chain in chains}
            dispositions = [
                disposition
                for disposition in data["dispositions"].values()
                if (disposition["device_id"], disposition["pairing_id"]) in scope
            ]
            dispositions.sort(key=lambda d: (_parse_ts(d["captured_at"]), d["event_id"]))
            notifications = [
                notification
                for notification in data["notifications"].values()
                if (notification["device_id"], notification["pairing_id"]) in scope
            ]
            notifications.sort(key=lambda n: (n["created_at"], n["notification_id"]))
            deletion_jobs = [
                job
                for job in data["deletion_jobs"].values()
                if (job["device_id"], job["pairing_id"]) in scope
            ]
        return {
            "guardian_id": guardian_id,
            "consents": chains,
            "dispositions": dispositions,
            "notifications": notifications,
            "deletion_jobs": deletion_jobs,
        }

    def list_notifications(self, device_id=None, pairing_id=None):
        with self.store.locked() as data:
            result = [
                notification
                for notification in data["notifications"].values()
                if (device_id is None or notification["device_id"] == device_id)
                and (pairing_id is None or notification["pairing_id"] == pairing_id)
            ]
        result.sort(key=lambda n: (n["created_at"], n["notification_id"]))
        return result

    # ------------------------------------------------------------------

    @staticmethod
    def _next(data, name):
        data["counters"][name] = data["counters"].get(name, 0) + 1
        return data["counters"][name]

    @staticmethod
    def _validate_event(raw):
        for field in ("event_id", "device_id", "pairing_id", "captured_at"):
            if not raw.get(field):
                raise ValueError(f"事件缺少字段 {field}")
        sequence = raw.get("sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
            raise ValueError("sequence 必须是非负整数")
        captured_dt = _parse_ts(raw["captured_at"], "captured_at")
        signals = raw.get("signals")
        if not isinstance(signals, dict):
            raise ValueError("signals 必须是对象")
        return {
            "event_id": str(raw["event_id"]),
            "device_id": str(raw["device_id"]),
            "pairing_id": str(raw["pairing_id"]),
            "sequence": sequence,
            "captured_at": raw["captured_at"],
            "captured_dt": captured_dt,
            "signals": signals,
        }
