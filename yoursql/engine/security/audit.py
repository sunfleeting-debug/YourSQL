"""结构化 JSON Lines 审计日志。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from threading import RLock
from typing import Mapping


class AuditLog:
    """内存保留事件，同时可追加写入审计文件。"""

    def __init__(self, path: str | Path | None = None) -> None:
        """初始化实例所需的状态和依赖。"""
        self.path = Path(path) if path is not None else None
        self._events: list[dict[str, object]] = []
        self._lock = RLock()

    def record(
        self,
        action: str,
        *,
        user: str | None = None,
        success: bool = True,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """向审计日志追加一条事件记录。"""
        event: dict[str, object] = {
            "timestamp": time.time(),
            "action": action,
            "user": user,
            "success": success,
            "details": dict(details or {}),
        }
        with self._lock:
            self._events.append(event)
            if self.path is not None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
                    )

    def events(self) -> tuple[dict[str, object], ...]:
        """返回审计日志中的事件快照。"""
        with self._lock:
            return tuple(dict(event) for event in self._events)
