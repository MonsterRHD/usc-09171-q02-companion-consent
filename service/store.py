"""JSON 文件持久化：原子写入，服务重启后状态完整恢复。"""

import json
import os
import threading
from contextlib import contextmanager

STORE_KEYS = (
    "consents",       # consent_id -> [版本记录, ...]
    "events",         # event_id -> 事件记录（含删除墓碑）
    "dispositions",   # event_id -> 处置记录
    "notifications",  # 去重键 -> 通知
    "escalations",    # escalation_id -> 升级单
    "pairings",       # "device|pairing" -> 配对周期状态
    "deletion_jobs",  # job_id -> 删除任务
    "counters",       # 各类自增序号
)


class JsonStore:
    def __init__(self, path=None):
        self.path = path
        self._lock = threading.RLock()
        self._data = {key: {} for key in STORE_KEYS}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            for key, value in loaded.items():
                self._data[key] = value

    @contextmanager
    def locked(self):
        with self._lock:
            yield self._data

    def save(self):
        if not self.path:
            return
        with self._lock:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp_path = f"{self.path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp_path, self.path)
