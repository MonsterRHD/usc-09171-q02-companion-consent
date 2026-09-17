"""处置规则引擎。

规则以版本号标识（``RULES_VERSION``），每条处置都记录所使用的规则版本。
核心原则：

* 以 *事件发生时刻* (``captured_at``) 有效的同意版本决定留存粒度与可执行动作；
* 以 *接收时刻* 执行撤回 / 转借的“立即停止”闸门，非危险事件不再产生新的个性化处理；
* 明确危险信号即使无同意或授权已终止，仍以最小事实进入人工确认升级；
* 关怀提醒按 ``captured_at`` 重算连续窗口，乱序、重传、重启都只通知一次。
"""

from __future__ import annotations

import json
import threading

from . import clock
from .consent import ConsentManager, ConsentView
from .store import Store

RULES_VERSION = "rules-2026-09-v1"

# 连续异常：窗口间隔与触发阈值
CARE_GAP_SECONDS = 600
CARE_THRESHOLD = 2

# 家庭可配置动作的固定词表（顺序即普通低落时的默认偏好）
COMFORT_PREFERENCE = ("hug_back", "soft_glow", "soothing_sound", "breathing_prompt")
COMFORT_LABELS = {
    "hug_back": "轻抱回应",
    "soft_glow": "柔光安抚",
    "soothing_sound": "轻声安抚音",
    "breathing_prompt": "呼吸引导",
}

DANGER_RISK = {"danger", "emergency", "self_harm"}
ABNORMAL_RISK = {"needs_attention", "distress"}
LOW_MOOD = {"low", "sad", "anxious"}

# 语音派生字段：voice_summary=False 时一律不落盘
VOICE_KEY_PREFIXES = ("voice_", "speech_")

_LOW = "low"
_ABNORMAL = "abnormal"
_DANGER = "danger"
_NEUTRAL = "neutral"


def classify(signals: dict) -> str:
    risk = (signals.get("risk") or "").lower()
    if risk in DANGER_RISK:
        return _DANGER
    if risk in ABNORMAL_RISK:
        return _ABNORMAL
    mood = (signals.get("mood") or "").lower()
    if mood in LOW_MOOD:
        return _LOW
    return _NEUTRAL


def _is_voice_key(key: str) -> bool:
    return key.startswith(VOICE_KEY_PREFIXES)


def _reduce_signals(signals: dict, level: str, voice_summary: bool) -> dict:
    """按留存粒度裁剪信号摘要。原始语音本就不在信号中。"""
    if level == "minimal":
        return {}
    filtered = {k: v for k, v in signals.items() if voice_summary or not _is_voice_key(k)}
    if level == "full":
        return dict(filtered)
    # summary：只留类别（mood/risk/touch），不落置信度等量化细节
    return {k: filtered[k] for k in ("mood", "risk", "touch") if k in filtered}


def choose_comfort(signals: dict, allowed_actions) -> str | None:
    """普通低落只能从家庭批准的动作中选择；触摸 hold 优先轻抱回应。"""
    allowed = set(allowed_actions)
    preference = list(COMFORT_PREFERENCE)
    if signals.get("touch") == "hold":
        preference.remove("hug_back")
        preference.insert(0, "hug_back")
    for action in preference:
        if action in allowed:
            return action
    return None


class Engine:
    def __init__(self, store: Store, consents: ConsentManager | None = None):
        self.store = store
        self.consents = consents or ConsentManager(store)
        self._decision_lock = threading.RLock()

    # ---------- 批量入口 ----------

    def ingest_batch(self, events: list[dict], received_at: str | None = None) -> list[dict]:
        results = []
        with self._decision_lock:
            received_at = received_at or clock.now_iso()
            touched_devices: set[str] = set()
            for event in events:
                r = self._process_one(event, received_at)
                results.append(r)
                if r["status"] == "processed" and r["classification"] == _ABNORMAL \
                        and r["resolution"] == "handled":
                    touched_devices.add(event["device_id"])
            # 整批落库后统一按 captured_at 重算关怀窗口，保证计数准确、通知只发一次
            new_care = []
            for device_id in touched_devices:
                new_care.extend(self._recompute_care_windows(device_id, received_at))
            by_event = {r["event_id"]: r for r in results}
            for ref_id, member_ids in new_care:
                # 标记达到阈值的触发事件；若其属于更早批次（乱序到达），标到本批最早成员
                trigger = member_ids[min(CARE_THRESHOLD, len(member_ids)) - 1]
                if trigger not in by_event:
                    trigger = next((eid for eid in member_ids if eid in by_event), None)
                if trigger:
                    by_event[trigger]["care_notification"] = True
            return results

    # ---------- 单事件处置（幂等） ----------

    def _process_one(self, event: dict, received_at: str) -> dict:
        event_id = event["event_id"]
        pairing_id = event["pairing_id"]
        sequence = int(event["sequence"])
        device_id = event["device_id"]
        signals = event.get("signals") or {}
        captured_at = clock.iso(clock.parse(event["captured_at"]))

        conflict = self.store.event_exists(event_id, pairing_id, sequence)
        if conflict:
            row = self.store.get_event(event_id)
            if row is None:
                # event_id 不同但 (pairing_id, sequence) 冲突：取回已处置的那条记录
                row = self.store.event_by_pairing_sequence(pairing_id, sequence)
            return {
                "event_id": event_id,
                "status": "duplicate",
                "conflict": conflict,
                "original_event_id": row["event_id"],
                "classification": row["classification"],
                "resolution": row["resolution"],
            }

        skew = (clock.parse(received_at) - clock.parse(captured_at)).total_seconds()
        kind = classify(signals)

        view, reason = self.consents.resolve(device_id, captured_at)
        if kind == _DANGER:
            # 明确危险：即使无授权 / 已撤回 / 已转借，仍以最小事实升级人工确认
            resolution = "escalated"
        elif view is None and reason == "transferred":
            resolution = "suppressed_transferred"
        elif view is None:
            resolution = "suppressed_no_consent"
        else:
            # 事件时刻授权有效即按该版本处置；撤回前发生、延迟到达的事件也只处置这一次
            resolution = "handled"

        # ---------- 留存与动作 ----------
        stored = None
        action = None
        case_id = None

        if kind == _DANGER:
            # 危险：无论同意状态如何，只保留最小事实并进入人工确认
            stored = self._minimal_record(event, captured_at, received_at, kind, view)
            self._persist_event(event, captured_at, received_at, skew, signals, kind,
                                resolution, "minimal", stored, view, redact_signals=True)
            case_id = self._open_escalation(event, captured_at, view)
            self.store.add_action(event_id, "escalate", {
                "case_id": case_id, "requires": "human_confirmation",
                "information": "minimal_facts_only",
            })
            self.store.add_notification({
                "ntype": "escalation", "ref_id": case_id,
                "channel": "safety_review", "recipient": "safety_officer",
                "payload": {
                    "case_id": case_id, "device_id": device_id,
                    "captured_at": captured_at, "reason": "explicit_danger_signal",
                },
            })
        elif resolution.startswith("suppressed"):
            # 无同意 / 已转借 / 已撤回：停止个性化处理，仅留操作级最小记录，不含信号
            stored = self._minimal_record(event, captured_at, received_at, kind, view)
            if view is None:
                # 撤回/到期后的抑制事件归属最近终止授权的主体，便于删除归集
                owner = self.consents.former_owner(device_id, received_at)
                if owner:
                    stored["subject_id"] = owner
            self._persist_event(event, captured_at, received_at, skew, {}, kind,
                                resolution, "minimal", stored, view, redact_signals=True)
        else:
            level = view.retention_mood if kind == _LOW else view.retention_risk
            kept = _reduce_signals(signals, level, view.voice_summary)
            if kind == _LOW:
                action = choose_comfort(signals, view.allowed_actions)
                if action:
                    self.store.add_action(event_id, "comfort", {
                        "action": action,
                        "label": COMFORT_LABELS.get(action, action),
                        "source": "family_approved",
                    })
            stored = self._full_record(event, captured_at, received_at, kind, view, kept, action)
            self._persist_event(event, captured_at, received_at, skew, kept, kind,
                                resolution, level, stored, view)

        return {
            "event_id": event_id,
            "status": "processed",
            "classification": kind,
            "resolution": resolution,
            "retention_level": self.store.get_event(event_id)["retention_level"],
            "consent": view.label if view else None,
            "rules_version": RULES_VERSION,
            "action": action,
            "escalation_case": case_id,
            "care_notification": False,
            "clock_skew_seconds": round(skew, 3),
        }

    # ---------- 记录组装 ----------

    @staticmethod
    def _minimal_record(event, captured_at, received_at, kind, view: ConsentView | None) -> dict:
        rec = {
            "event_id": event["event_id"],
            "device_id": event["device_id"],
            "pairing_id": event["pairing_id"],
            "sequence": event["sequence"],
            "captured_at": captured_at,
            "classification": kind,
            "consent_id": view.consent_id if view else None,
            "consent_version": view.version if view else None,
        }
        # 主体标识仅用于删除归集；信号与情绪细节不在最小记录内
        if view is not None:
            rec["subject_id"] = view.subject_id
            rec["household_id"] = view.household_id
        return rec

    @staticmethod
    def _full_record(event, captured_at, received_at, kind, view: ConsentView,
                     signals_kept: dict, action) -> dict:
        return {
            "event_id": event["event_id"],
            "device_id": event["device_id"],
            "pairing_id": event["pairing_id"],
            "sequence": event["sequence"],
            "captured_at": captured_at,
            "received_at": received_at,
            "classification": kind,
            "signals": signals_kept,
            "action": action,
            "subject_id": view.subject_id,
            "household_id": view.household_id,
            "guardian_id": view.guardian_id,
            "consent_id": view.consent_id,
            "consent_version": view.version,
        }

    def _persist_event(self, event, captured_at, received_at, skew, signals_kept,
                       kind, resolution, level, stored, view: ConsentView | None,
                       redact_signals: bool = False) -> None:
        self.store.insert_event({
            "event_id": event["event_id"],
            "device_id": event["device_id"],
            "pairing_id": event["pairing_id"],
            "sequence": int(event["sequence"]),
            "captured_at": captured_at,
            "received_at": received_at,
            "clock_skew_sec": round(skew, 3),
            "signals": signals_kept if not redact_signals else {},
            "classification": kind,
            "resolution": resolution,
            "retention_level": level,
            "stored_record": stored,
            "consent_id": view.consent_id if view else None,
            "consent_version": view.version if view else None,
            "rules_version": RULES_VERSION,
            "decided_at": clock.now_iso(),
        })

    # ---------- 危险升级 ----------

    def _open_escalation(self, event, captured_at: str, view: ConsentView | None) -> str:
        case_id = f"case-{event['event_id']}"
        if self.store.get_escalation(case_id):
            return case_id
        # 依据事实：危险事件本身 + 紧邻的连续异常（最小集合）
        basis = [event["event_id"]]
        for row in self.store.list_events(event["device_id"]):
            if row["classification"] != _ABNORMAL:
                continue
            gap = abs(
                (clock.parse(captured_at) - clock.parse(row["captured_at"])).total_seconds()
            )
            if gap <= CARE_GAP_SECONDS:
                basis.append(row["event_id"])
        self.store.open_escalation({
            "case_id": case_id,
            "device_id": event["device_id"],
            "trigger_event_id": event["event_id"],
            "household_id": view.household_id if view else None,
            "subject_id": view.subject_id if view else None,
            "created_at": clock.now_iso(),
            "basis_event_ids": basis,
        })
        self.store.insert_fact({
            "fact_id": f"fact-{event['event_id']}",
            "device_id": event["device_id"],
            "pairing_id": event["pairing_id"],
            "event_id": event["event_id"],
            "captured_at": captured_at,
            "classification": _DANGER,
            "basis": "consent_snapshot" if view else "safety_override",
            "consent_id": view.consent_id if view else None,
            "consent_version": view.version if view else None,
            "rules_version": RULES_VERSION,
            "retained_at": clock.now_iso(),
            "retain_reason": "explicit_danger_human_confirmation",
            "case_id": case_id,
        })
        return case_id

    # 人工确认升级流程的合法流转
    ESCALATION_TRANSITIONS = {
        "open": {"confirmed", "false_alarm", "resolved"},
        "confirmed": {"resolved"},
        "resolved": set(),
        "false_alarm": set(),
    }

    def decide_escalation(self, case_id: str, status: str, by: str) -> dict:
        if status not in ("confirmed", "false_alarm", "resolved"):
            raise ValueError("bad status")
        case = self.store.get_escalation(case_id)
        if not case:
            raise KeyError(case_id)
        current = case["status"]
        if status not in self.ESCALATION_TRANSITIONS.get(current, set()):
            return {"case_id": case_id, "status": current, "changed": False}
        self.store.update_escalation_status(case_id, status)
        return {"case_id": case_id, "status": status, "changed": True, "by": by}

    # ---------- 关怀窗口 ----------

    def _recompute_care_windows(self, device_id: str, received_at: str) -> list[tuple]:
        """按 captured_at 重算异常簇；返回本次新发出通知的 (ref_id, 簇成员事件) 列表。"""
        rows = [
            r for r in self.store.list_events(device_id)
            if r["classification"] == _ABNORMAL and r["resolution"] == "handled"
        ]
        rows.sort(key=lambda r: (r["captured_at"], r["sequence"]))

        clusters: list[list] = []
        for r in rows:
            if clusters and (
                clock.parse(r["captured_at"])
                - clock.parse(clusters[-1][-1]["captured_at"])
            ).total_seconds() <= CARE_GAP_SECONDS:
                clusters[-1].append(r)
            else:
                clusters.append([r])

        if not rows:
            self.store.reconcile_care_windows(device_id, [])
            return []
        newest_event_id = rows[-1]["event_id"]
        notified: list[tuple] = []
        keep_open: list[str] = []
        for cluster in clusters:
            first, latest = cluster[0], cluster[-1]
            record = json.loads(first["stored_record"]) if first["stored_record"] else {}
            is_open = latest["event_id"] == newest_event_id
            if is_open:
                keep_open.append(first["event_id"])
            self.store.upsert_care_window({
                "device_id": device_id,
                "first_event_id": first["event_id"],
                "latest_event_id": latest["event_id"],
                "first_captured_at": first["captured_at"],
                "latest_captured_at": latest["captured_at"],
                "abnormal_count": len(cluster),
                "closed": 0 if is_open else 1,
                "subject_id": record.get("subject_id"),
                "household_id": record.get("household_id"),
            })
            if len(cluster) < CARE_THRESHOLD:
                continue
            ref_id = f"care:{device_id}:{first['event_id']}"
            if self.store.care_notification_exists(ref_id):
                continue
            # 乱序：新算出的簇与已通知窗口在时间上重叠时，视为同一次关怀，不重复通知
            overlap = self.store.find_notified_window(
                device_id, first["captured_at"], latest["captured_at"]
            )
            if overlap:
                self.store.mark_care_notified(device_id, first["event_id"])
                continue
            # 终止闸门按簇主体自身的授权判断（转借家庭的孩子与新访客互不影响）
            gated = bool(
                first["consent_id"]
                and self.consents.is_terminated(first["consent_id"], received_at)
            )
            if gated:
                # 授权已终止：不再发出个性化关怀提醒，仅留抑制痕迹
                self.store.mark_care_suppressed(device_id, first["event_id"])
                continue
            chain = self.store.consent_chain(first["consent_id"])
            view_row = chain[-1]
            created = self.store.add_notification({
                "ntype": "care_alert", "ref_id": ref_id,
                "channel": "guardian_app",
                "recipient": (view_row["guardian_id"]
                              or f"household:{view_row['household_id']}"),
                "payload": {
                    "device_id": device_id,
                    "window_first": first["captured_at"],
                    "window_last": latest["captured_at"],
                    "abnormal_count": len(cluster),
                    "information": "minimal:attention_count_only",
                },
            })
            if created:
                self.store.mark_care_notified(device_id, first["event_id"])
                notified.append((ref_id, [r["event_id"] for r in cluster]))
        self.store.reconcile_care_windows(device_id, keep_open)
        return notified
