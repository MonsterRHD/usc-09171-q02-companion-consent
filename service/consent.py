"""家庭成员、儿童监护、临时访客三类授权的分开管理与版本化。

授权版本一旦创建不可修改；调整设置会追加新版本（``effective_from`` 为设置生效的
接收时刻）。撤回、儿童到期、设备转借通过状态时刻表达，事件裁决时按 *事件发生时刻*
判断当时是否有效，从而与断网延迟到达的事件区分开。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from . import clock
from .store import Store

FAMILY = "family"
CHILD = "child"
GUEST = "guest"
SCOPES = (FAMILY, CHILD, GUEST)

# 裁决优先级：临时访客 > 儿童监护 > 家庭
SCOPE_PRIORITY = {GUEST: 0, CHILD: 1, FAMILY: 2}


class ConsentError(ValueError):
    pass


@dataclass(frozen=True)
class ConsentView:
    """事件发生当刻有效的一份授权快照。"""

    consent_id: str
    version: int
    scope_type: str
    device_id: str
    subject_id: str
    household_id: str
    guardian_id: str | None
    allowed_actions: tuple[str, ...]
    retention_mood: str
    retention_risk: str
    voice_summary: bool
    raw_voice: bool
    safety_minimum: bool
    effective_from: str
    expires_at: str | None
    revoked_at: str | None
    transferred_at: str | None

    @property
    def label(self) -> str:
        return f"{self.consent_id}@v{self.version}"


class ConsentManager:
    def __init__(self, store: Store):
        self.store = store

    # ---------- 创建 / 版本追加 ----------

    def create(
        self,
        scope_type: str,
        device_id: str,
        subject_id: str,
        household_id: str,
        *,
        guardian_id: str | None = None,
        allowed_actions: list[str] | None = None,
        retention_mood: str = "summary",
        retention_risk: str = "minimal",
        voice_summary: bool = True,
        raw_voice: bool = False,
        safety_minimum: bool = True,
        expires_at: str | None = None,
        effective_from: str | None = None,
        granted_at: str | None = None,
        consent_id: str | None = None,
    ) -> dict:
        if scope_type not in SCOPES:
            raise ConsentError(f"unknown scope_type: {scope_type}")
        if scope_type in (CHILD, GUEST) and not guardian_id:
            raise ConsentError("child/guest consent must be granted by a guardian")
        if retention_mood not in ("full", "summary", "minimal"):
            raise ConsentError("bad retention_mood")
        if retention_risk not in ("full", "summary", "minimal"):
            raise ConsentError("bad retention_risk")
        if raw_voice:
            # 契约：原始语音不进入平台
            raise ConsentError("raw voice never leaves the device")

        consent_id = consent_id or f"consent-{uuid.uuid4().hex[:10]}"
        version = self.store.latest_consent_version(consent_id) + 1
        # 所有时刻统一归一为 UTC 文本，保证字典序比较与时区无关
        granted_ts = clock.iso(clock.parse(granted_at)) if granted_at else clock.now_iso()
        effective_ts = clock.iso(clock.parse(effective_from)) if effective_from else granted_ts
        expires_ts = clock.iso(clock.parse(expires_at)) if expires_at else None
        ts = clock.now_iso()
        doc = {
            "consent_id": consent_id,
            "version": version,
            "scope_type": scope_type,
            "device_id": device_id,
            "subject_id": subject_id,
            "household_id": household_id,
            "guardian_id": guardian_id,
            "allowed_actions": list(allowed_actions or []),
            "retention": {"mood": retention_mood, "risk": retention_risk},
            "voice_summary": voice_summary,
            "raw_voice": False,
            "safety_minimum": safety_minimum,
            "expires_at": expires_ts,
            "effective_from": effective_ts,
            "granted_at": granted_ts,
        }
        row = {
            "consent_id": consent_id,
            "version": version,
            "scope_type": scope_type,
            "device_id": device_id,
            "subject_id": subject_id,
            "household_id": household_id,
            "guardian_id": guardian_id,
            "granted_at": granted_ts,
            "effective_from": effective_ts,
            "expires_at": expires_ts,
            "revoked_at": None,
            "transferred_at": None,
            "allowed_actions": doc["allowed_actions"],
            "retention_mood": retention_mood,
            "retention_risk": retention_risk,
            "voice_summary": int(voice_summary),
            "raw_voice": 0,
            "safety_minimum": int(safety_minimum),
            "document": doc,
        }
        self.store.insert_consent_version(row)
        return doc

    def append_version(
        self,
        consent_id: str,
        *,
        allowed_actions: list[str] | None = None,
        retention_mood: str | None = None,
        retention_risk: str | None = None,
        voice_summary: bool | None = None,
        effective_from: str | None = None,
    ) -> dict:
        """家庭/监护人调整设置：沿用身份字段，追加新版本。"""
        chain = self.store.consent_chain(consent_id)
        if not chain:
            raise ConsentError(f"unknown consent: {consent_id}")
        prev = chain[-1]
        return self.create(
            prev["scope_type"],
            prev["device_id"],
            prev["subject_id"],
            prev["household_id"],
            guardian_id=prev["guardian_id"],
            allowed_actions=(allowed_actions if allowed_actions is not None
                             else _json(prev["allowed_actions"])),
            retention_mood=retention_mood or prev["retention_mood"],
            retention_risk=retention_risk or prev["retention_risk"],
            voice_summary=voice_summary if voice_summary is not None
                          else bool(prev["voice_summary"]),
            safety_minimum=bool(prev["safety_minimum"]),
            expires_at=prev["expires_at"],
            effective_from=effective_from,
            consent_id=consent_id,
        )

    # ---------- 撤回 / 到期 / 转借 ----------

    def revoke(self, consent_id: str, at: str | None = None) -> None:
        """撤回授权：自接收时刻起，新的个性化处理立即停止。"""
        chain = self.store.consent_chain(consent_id)
        if not chain:
            raise ConsentError(f"unknown consent: {consent_id}")
        ts = clock.iso(clock.parse(at)) if at else clock.now_iso()
        with self.store.conn:
            cur = self.store.conn.execute(
                "UPDATE consent_versions SET revoked_at=? "
                "WHERE consent_id=? AND revoked_at IS NULL",
                (ts, consent_id),
            )
            if cur.rowcount == 0:
                raise ConsentError(f"consent already terminated: {consent_id}")

    def transfer_device(self, device_id: str, at: str | None = None) -> int:
        """设备转借：终止该设备上原家庭的全部授权，返回受影响授权数。"""
        ts = clock.iso(clock.parse(at)) if at else clock.now_iso()
        with self.store.conn:
            rows = self.store.conn.execute(
                "SELECT DISTINCT consent_id FROM consent_versions "
                "WHERE device_id=? AND transferred_at IS NULL AND revoked_at IS NULL",
                (device_id,),
            ).fetchall()
            for r in rows:
                self.store.conn.execute(
                    "UPDATE consent_versions SET transferred_at=? "
                    "WHERE consent_id=? AND transferred_at IS NULL",
                    (ts, r["consent_id"]),
                )
        return len(rows)

    # ---------- 事件时刻裁决 ----------

    def resolve(self, device_id: str, at: str) -> tuple[ConsentView | None, str | None]:
        """返回事件发生当刻有效的授权快照；无有效授权时返回 (None, 原因)。"""
        candidates: list[ConsentView] = []
        for row in self.store.consents_for_device(device_id):
            if row["effective_from"] > at:
                continue  # 该版本在事件发生后才生效，不能套用
            view = _view(row)
            # 同一条授权链取事件时刻前最近的版本（行已按 version 排序，循环覆盖）
            candidates = [c for c in candidates if c.consent_id != view.consent_id]
            candidates.append(view)

        valid: list[ConsentView] = []
        for c in candidates:
            if c.expires_at and at >= c.expires_at:
                continue
            if c.revoked_at and at >= c.revoked_at:
                continue
            if c.transferred_at and at >= c.transferred_at:
                continue
            valid.append(c)

        if not valid:
            # 转借原因：存在因转借终止的候选，且没有转借时刻之后建立的新授权
            transfer_times = [
                c.transferred_at for c in candidates
                if c.transferred_at and at >= c.transferred_at
            ]
            if transfer_times:
                latest_transfer = max(transfer_times)
                has_new_consent = any(
                    c.effective_from >= latest_transfer for c in candidates
                )
                if not has_new_consent:
                    return None, "transferred"
            return None, "no_consent"

        valid.sort(key=lambda c: (SCOPE_PRIORITY[c.scope_type], c.effective_from))
        return valid[0], None

    # ---------- 状态查询 ----------

    def is_terminated(self, consent_id: str, at: str) -> bool:
        """接收时刻该授权是否已撤回 / 到期 / 随设备转借终止。"""
        chain = self.store.consent_chain(consent_id)
        if not chain:
            return False
        latest = chain[-1]
        if latest["revoked_at"] and at >= latest["revoked_at"]:
            return True
        if latest["transferred_at"] and at >= latest["transferred_at"]:
            return True
        if latest["expires_at"] and at >= latest["expires_at"]:
            return True
        return False

    def former_owner(self, device_id: str, at: str) -> str | None:
        """无有效授权时，供删除归集的“最近终止授权”主体。

        同家庭内撤回/到期终止的授权仍可归属该主体（设备仍在该家庭）；
        随设备转借终止的授权不归属（设备已离开原家庭，进入转借真空期）。
        """
        best: ConsentView | None = None
        best_ended = ""
        for row in self.store.consents_for_device(device_id):
            if row["effective_from"] > at:
                continue
            view = _view(row)
            if view.transferred_at and at >= view.transferred_at:
                continue  # 转借真空期：不归属原家庭
            ended_at = None
            if view.revoked_at and at >= view.revoked_at:
                ended_at = view.revoked_at
            elif view.expires_at and at >= view.expires_at:
                ended_at = view.expires_at
            if ended_at and ended_at > best_ended:
                best, best_ended = view, ended_at
        return best.subject_id if best else None


def _json(value: str):
    import json
    return json.loads(value)


def _view(row) -> ConsentView:
    import json
    return ConsentView(
        consent_id=row["consent_id"],
        version=row["version"],
        scope_type=row["scope_type"],
        device_id=row["device_id"],
        subject_id=row["subject_id"],
        household_id=row["household_id"],
        guardian_id=row["guardian_id"],
        allowed_actions=tuple(json.loads(row["allowed_actions"])),
        retention_mood=row["retention_mood"],
        retention_risk=row["retention_risk"],
        voice_summary=bool(row["voice_summary"]),
        raw_voice=bool(row["raw_voice"]),
        safety_minimum=bool(row["safety_minimum"]),
        effective_from=row["effective_from"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        transferred_at=row["transferred_at"],
    )
