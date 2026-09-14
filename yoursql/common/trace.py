"""按请求采集真实执行路径；普通 SQL 调用不启用采集。"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Event
from time import monotonic

from .errors import YourSQLError


@dataclass
class ExecutionTrace:
    deadline: float
    cancelled: Event
    interruptible: bool = True
    max_steps: int | None = 250_000
    steps: int = 0
    events: list[dict[str, object]] = field(default_factory=list)
    scans: list[dict[str, object]] = field(default_factory=list)
    dropped_events: int = 0

    def check(self) -> None:
        # WHY：写操作只能在语句边界取消，避免在更新索引期间留下半完成状态。
        if not self.interruptible:
            return
        if self.cancelled.is_set():
            raise YourSQLError("查询已取消", "CANCELLED")
        if monotonic() > self.deadline:
            raise YourSQLError("查询超过执行期限", "TIMEOUT")
        if self.max_steps is not None and self.steps > self.max_steps:
            raise YourSQLError(
                f"查询超过 {self.max_steps} 次扫描/连接/投影步骤，请缩小查询范围",
                "RESOURCE_LIMIT",
            )

    def step(self) -> None:
        self.steps += 1
        self.check()

    def event(self, action: str, page_id: int, **details: object) -> None:
        if len(self.events) < 256:
            self.events.append({"action": action, "page_id": page_id, **details})
        else:
            self.dropped_events += 1


current_trace: ContextVar[ExecutionTrace | None] = ContextVar(
    "yoursql_execution_trace", default=None
)
