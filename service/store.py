"""SQLite 持久层。

所有状态落盘，服务重启后事件处置、升级流程、通知去重与清理进度均可恢复。
时间戳统一存为 UTC ISO-8601 文本，按字典序即可比较。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading

from . import clock

_SCHEMA = """
CREATE TABLE IF NOT EXISTS consent_versions (
    consent_id        TEXT NOT NULL,
    version           INTEGER NOT NULL,
    scope_type        TEXT NOT NULL,           -- family | child | guest
    device_id         TEXT NOT NULL,
    subject_id        TEXT NOT NULL,           -- 家庭成员 / 儿童 / 访客标识
    household_id      TEXT NOT NULL,
    guardian_id       TEXT,                    -- 儿童/访客授权的授予监护人
    granted_at        TEXT NOT NULL,
    effective_from    TEXT NOT NULL,
    expires_at        TEXT,                    -- 仅儿童授权可到期
    revoked_at        TEXT,                    -- 撤回时刻（接收时刻）
    transferred_at    TEXT,                    -- 设备转借时刻（接收时刻）
    allowed_actions   TEXT NOT NULL,           -- JSON 数组，家庭批准的安抚动作
    retention_mood    TEXT NOT NULL,           -- full | summary | minimal
    retention_risk    TEXT NOT NULL,
    voice_summary     INTEGER NOT NULL,        -- 是否允许留存语音情绪摘要
    raw_voice         INTEGER NOT NULL,        -- 原始语音（样例中本就不上平台）
    safety_minimum    INTEGER NOT NULL,        -- 安全审计最小事实是否保留
    document          TEXT NOT NULL,           -- 完整授权文档 JSON
    PRIMARY KEY (consent_id, version)
);
CREATE INDEX IF NOT EXISTS idx_consent_lookup
    ON consent_versions (device_id, scope_type, subject_id, effective_from);

CREATE TABLE IF NOT EXISTS events (
    event_id          TEXT PRIMARY KEY,
    device_id         TEXT NOT NULL,
    pairing_id        TEXT NOT NULL,
    sequence          INTEGER NOT NULL,
    captured_at       TEXT NOT NULL,           -- 事件发生时刻（设备时钟，归一成 UTC）
    received_at       TEXT NOT NULL,           -- 平台接收时刻
    clock_skew_sec    REAL,
    signals           TEXT NOT NULL,           -- 原始信号摘要 JSON
    classification    TEXT NOT NULL,           -- low | abnormal | danger
    resolution        TEXT NOT NULL,           -- handled | suppressed_no_consent | suppressed_transferred
    retention_level   TEXT NOT NULL,           -- full | summary | minimal | discard
    stored_record     TEXT,                    -- 留存记录 JSON（按留存粒度裁剪后）
    consent_id        TEXT,
    consent_version   INTEGER,
    rules_version     TEXT NOT NULL,
    decided_at        TEXT NOT NULL,
    UNIQUE (pairing_id, sequence)
);

CREATE TABLE IF NOT EXISTS event_actions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT NOT NULL,
    kind              TEXT NOT NULL,           -- comfort | care_alert | escalate
    detail            TEXT NOT NULL,           -- JSON
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS care_windows (
    -- 连续异常关怀提醒的去重窗口：按 captured_at 聚成的异常簇，每簇一行
    device_id          TEXT NOT NULL,
    first_event_id     TEXT NOT NULL,
    latest_event_id    TEXT NOT NULL,
    first_captured_at  TEXT NOT NULL,
    latest_captured_at TEXT NOT NULL,
    abnormal_count     INTEGER NOT NULL,
    notified           INTEGER NOT NULL,
    suppressed         INTEGER NOT NULL,
    closed             INTEGER NOT NULL,
    subject_id         TEXT,
    household_id       TEXT,
    PRIMARY KEY (device_id, first_event_id)
);

CREATE TABLE IF NOT EXISTS escalations (
    case_id           TEXT PRIMARY KEY,
    device_id         TEXT NOT NULL,
    trigger_event_id  TEXT NOT NULL,
    household_id      TEXT,
    subject_id        TEXT,
    status            TEXT NOT NULL,           -- open | confirmed | resolved | false_alarm
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    basis_event_ids   TEXT NOT NULL            -- JSON 数组（最小事实集合）
);

CREATE TABLE IF NOT EXISTS notifications (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ntype             TEXT NOT NULL,           -- care_alert | escalation
    ref_id            TEXT NOT NULL,           -- care window 首事件 / case_id
    channel           TEXT NOT NULL,
    recipient         TEXT NOT NULL,
    payload           TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    UNIQUE (ntype, ref_id)                     -- 同一条关怀/升级只通知一次
);

CREATE TABLE IF NOT EXISTS retention_facts (
    -- 撤回 / 转借 / 删除后仍须保留的安全审计最小事实
    fact_id           TEXT PRIMARY KEY,
    device_id         TEXT NOT NULL,
    pairing_id        TEXT NOT NULL,
    event_id          TEXT NOT NULL,
    captured_at       TEXT NOT NULL,
    classification    TEXT NOT NULL,
    basis             TEXT NOT NULL,           -- consent_snapshot | safety_override
    consent_id        TEXT,
    consent_version   INTEGER,
    rules_version     TEXT NOT NULL,
    retained_at       TEXT NOT NULL,
    retain_reason     TEXT NOT NULL,
    case_id           TEXT
);

CREATE TABLE IF NOT EXISTS deletions (
    delete_id         TEXT PRIMARY KEY,
    scope_type        TEXT NOT NULL,
    subject_id        TEXT NOT NULL,
    requested_by      TEXT NOT NULL,
    requested_at      TEXT NOT NULL,
    status            TEXT NOT NULL,           -- completed
    events_deleted    INTEGER NOT NULL,
    actions_deleted   INTEGER NOT NULL,
    facts_retained    INTEGER NOT NULL,
    result_json       TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str | None = None):
        self.path = path or os.getenv("CONSENT_DB", ":memory:")
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            # 部分挂载文件系统不支持 WAL，回退到回滚日志
            self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self.conn.executescript(_SCHEMA)
            self.conn.commit()

    # ---------- consent ----------

    def insert_consent_version(self, v: dict) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO consent_versions
                   (consent_id, version, scope_type, device_id, subject_id, household_id,
                    guardian_id, granted_at, effective_from, expires_at, revoked_at,
                    transferred_at, allowed_actions, retention_mood, retention_risk,
                    voice_summary, raw_voice, safety_minimum, document)
                   VALUES (:consent_id,:version,:scope_type,:device_id,:subject_id,:household_id,
                    :guardian_id,:granted_at,:effective_from,:expires_at,:revoked_at,
                    :transferred_at,:allowed_actions,:retention_mood,:retention_risk,
                    :voice_summary,:raw_voice,:safety_minimum,:document)""",
                {
                    **v,
                    "allowed_actions": json.dumps(v["allowed_actions"], ensure_ascii=False),
                    "document": json.dumps(v["document"], ensure_ascii=False),
                },
            )
            self.conn.commit()

    def latest_consent_version(self, consent_id: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version),0) AS v FROM consent_versions WHERE consent_id=?",
                (consent_id,),
            ).fetchone()
            return row["v"]

    def consent_chain(self, consent_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM consent_versions WHERE consent_id=? ORDER BY version",
                    (consent_id,),
                )
            )

    def all_consent_rows(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM consent_versions ORDER BY consent_id, version"
                )
            )

    def consents_for_device(self, device_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM consent_versions WHERE device_id=? ORDER BY effective_from, version",
                    (device_id,),
                )
            )

    # ---------- events ----------

    def event_exists(self, event_id: str, pairing_id: str, sequence: int) -> str | None:
        """返回冲突类型：event_id / pairing_sequence / None。"""
        with self._lock:
            row = self.conn.execute(
                "SELECT pairing_id, sequence FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            if row:
                return "event_id"
            row = self.conn.execute(
                "SELECT event_id FROM events WHERE pairing_id=? AND sequence=?",
                (pairing_id, sequence),
            ).fetchone()
            if row:
                return "pairing_sequence"
        return None

    def get_event(self, event_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM events WHERE event_id=?", (event_id,)
            ).fetchone()

    def event_by_pairing_sequence(self, pairing_id: str, sequence: int) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM events WHERE pairing_id=? AND sequence=?",
                (pairing_id, sequence),
            ).fetchone()

    def insert_event(self, e: dict) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO events
                   (event_id, device_id, pairing_id, sequence, captured_at, received_at,
                    clock_skew_sec, signals, classification, resolution, retention_level,
                    stored_record, consent_id, consent_version, rules_version, decided_at)
                   VALUES (:event_id,:device_id,:pairing_id,:sequence,:captured_at,:received_at,
                    :clock_skew_sec,:signals,:classification,:resolution,:retention_level,
                    :stored_record,:consent_id,:consent_version,:rules_version,:decided_at)""",
                {
                    **e,
                    "signals": json.dumps(e["signals"], ensure_ascii=False),
                    "stored_record": (
                        json.dumps(e["stored_record"], ensure_ascii=False)
                        if e["stored_record"] is not None
                        else None
                    ),
                },
            )
            self.conn.commit()

    def add_action(self, event_id: str, kind: str, detail: dict) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO event_actions (event_id, kind, detail, created_at) VALUES (?,?,?,?)",
                (event_id, kind, json.dumps(detail, ensure_ascii=False), clock.now_iso()),
            )
            self.conn.commit()

    def event_actions(self, event_id: str) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self.conn.execute(
                    "SELECT * FROM event_actions WHERE event_id=? ORDER BY id", (event_id,)
                )
            )

    def list_events(self, device_id: str | None = None) -> list[sqlite3.Row]:
        with self._lock:
            if device_id:
                return list(
                    self.conn.execute(
                        "SELECT * FROM events WHERE device_id=? ORDER BY captured_at, sequence",
                        (device_id,),
                    )
                )
            return list(self.conn.execute("SELECT * FROM events ORDER BY captured_at, sequence"))

    # ---------- care windows ----------

    def upsert_care_window(self, w: dict) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO care_windows
                   (device_id, first_event_id, latest_event_id, first_captured_at,
                    latest_captured_at, abnormal_count, notified, suppressed, closed,
                    subject_id, household_id)
                   VALUES (:device_id,:first_event_id,:latest_event_id,:first_captured_at,
                    :latest_captured_at,:abnormal_count,0,0,:closed,:subject_id,:household_id)
                   ON CONFLICT(device_id, first_event_id) DO UPDATE SET
                     latest_event_id=excluded.latest_event_id,
                     latest_captured_at=excluded.latest_captured_at,
                     abnormal_count=excluded.abnormal_count,
                     closed=excluded.closed""",
                w,
            )
            self.conn.commit()

    def mark_care_notified(self, device_id: str, first_event_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE care_windows SET notified=1 WHERE device_id=? AND first_event_id=?",
                (device_id, first_event_id),
            )
            self.conn.commit()

    def mark_care_suppressed(self, device_id: str, first_event_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE care_windows SET suppressed=1 WHERE device_id=? AND first_event_id=?",
                (device_id, first_event_id),
            )
            self.conn.commit()

    def care_notification_exists(self, ref_id: str) -> bool:
        with self._lock:
            return self.conn.execute(
                "SELECT 1 FROM notifications WHERE ntype='care_alert' AND ref_id=?", (ref_id,)
            ).fetchone() is not None

    def find_notified_window(self, device_id: str, start: str, end: str) -> sqlite3.Row | None:
        """查找与给定时间区间重叠且已通知的窗口（乱序到达时用于合并去重）。"""
        with self._lock:
            return self.conn.execute(
                """SELECT * FROM care_windows
                   WHERE device_id=? AND notified=1
                     AND first_captured_at<=? AND latest_captured_at>=?""",
                (device_id, end, start),
            ).fetchone()

    def reconcile_care_windows(self, device_id: str, keep_first_ids: list[str]) -> None:
        """重算后，不再构成簇首的旧窗口关闭。"""
        with self._lock:
            if keep_first_ids:
                placeholders = ",".join("?" * len(keep_first_ids))
                self.conn.execute(
                    f"UPDATE care_windows SET closed=1 "
                    f"WHERE device_id=? AND closed=0 AND first_event_id NOT IN ({placeholders})",
                    [device_id, *keep_first_ids],
                )
            else:
                self.conn.execute(
                    "UPDATE care_windows SET closed=1 WHERE device_id=? AND closed=0",
                    (device_id,),
                )
            self.conn.commit()

    # ---------- escalations / notifications ----------

    def get_escalation(self, case_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM escalations WHERE case_id=?", (case_id,)
            ).fetchone()

    def open_escalation(self, c: dict) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO escalations
                   (case_id, device_id, trigger_event_id, household_id, subject_id,
                    status, created_at, updated_at, basis_event_ids)
                   VALUES (:case_id,:device_id,:trigger_event_id,:household_id,:subject_id,
                    'open',:created_at,:created_at,:basis_event_ids)""",
                {**c, "basis_event_ids": json.dumps(c["basis_event_ids"])},
            )
            self.conn.commit()

    def update_escalation_status(self, case_id: str, status: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE escalations SET status=?, updated_at=? WHERE case_id=?",
                (status, clock.now_iso(), case_id),
            )
            self.conn.commit()

    def list_escalations(self, status: str | None = None) -> list[sqlite3.Row]:
        with self._lock:
            if status:
                return list(
                    self.conn.execute(
                        "SELECT * FROM escalations WHERE status=? ORDER BY created_at", (status,)
                    )
                )
            return list(self.conn.execute("SELECT * FROM escalations ORDER BY created_at"))

    def add_notification(self, n: dict) -> bool:
        """插入去重通知；重复返回 False。"""
        with self._lock:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO notifications
                   (ntype, ref_id, channel, recipient, payload, created_at)
                   VALUES (?,?,?,?,?,?)""",
                (
                    n["ntype"], n["ref_id"], n["channel"], n["recipient"],
                    json.dumps(n["payload"], ensure_ascii=False), clock.now_iso(),
                ),
            )
            self.conn.commit()
            return cur.rowcount == 1

    def list_notifications(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self.conn.execute("SELECT * FROM notifications ORDER BY id"))

    # ---------- retention facts ----------

    def insert_fact(self, f: dict) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT OR IGNORE INTO retention_facts
                   (fact_id, device_id, pairing_id, event_id, captured_at, classification,
                    basis, consent_id, consent_version, rules_version, retained_at,
                    retain_reason, case_id)
                   VALUES (:fact_id,:device_id,:pairing_id,:event_id,:captured_at,:classification,
                    :basis,:consent_id,:consent_version,:rules_version,:retained_at,
                    :retain_reason,:case_id)""",
                f,
            )
            self.conn.commit()

    def list_facts(self, device_id: str | None = None) -> list[sqlite3.Row]:
        with self._lock:
            if device_id:
                return list(
                    self.conn.execute(
                        "SELECT * FROM retention_facts WHERE device_id=? ORDER BY captured_at",
                        (device_id,),
                    )
                )
            return list(self.conn.execute("SELECT * FROM retention_facts ORDER BY captured_at"))

    def fact_for_event(self, event_id: str) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM retention_facts WHERE event_id=?", (event_id,)
            ).fetchone()

    # ---------- deletions ----------

    def record_deletion(self, d: dict) -> None:
        with self._lock:
            self.conn.execute(
                """INSERT INTO deletions
                   (delete_id, scope_type, subject_id, requested_by, requested_at, status,
                    events_deleted, actions_deleted, facts_retained, result_json)
                   VALUES (?,?,?,?,?, 'completed',?,?,?,?)""",
                (
                    d["delete_id"], d["scope_type"], d["subject_id"], d["requested_by"],
                    d["requested_at"], d["events_deleted"], d["actions_deleted"],
                    d["facts_retained"], json.dumps(d["result"], ensure_ascii=False),
                ),
            )
            self.conn.commit()

    def list_deletions(self, subject_id: str | None = None) -> list[sqlite3.Row]:
        with self._lock:
            if subject_id:
                return list(
                    self.conn.execute(
                        "SELECT * FROM deletions WHERE subject_id=? ORDER BY requested_at",
                        (subject_id,),
                    )
                )
            return list(self.conn.execute("SELECT * FROM deletions ORDER BY requested_at"))

    def delete_events_by_id(self, event_ids: list[str]) -> tuple[int, int]:
        """删除事件及其处置动作，返回 (事件数, 动作数)。"""
        if not event_ids:
            return 0, 0
        placeholders = ",".join("?" * len(event_ids))
        with self._lock:
            actions = self.conn.execute(
                f"SELECT COUNT(*) AS c FROM event_actions WHERE event_id IN ({placeholders})",
                event_ids,
            ).fetchone()["c"]
            self.conn.execute(
                f"DELETE FROM event_actions WHERE event_id IN ({placeholders})", event_ids
            )
            cur = self.conn.execute(
                f"DELETE FROM events WHERE event_id IN ({placeholders})", event_ids
            )
            self.conn.commit()
            return cur.rowcount, actions

    def events_for_subject(self, subject_id: str) -> list[str]:
        with self._lock:
            return [
                r["event_id"]
                for r in self.conn.execute(
                    "SELECT event_id FROM events WHERE json_extract(stored_record,'$.subject_id')=?",
                    (subject_id,),
                )
            ]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
