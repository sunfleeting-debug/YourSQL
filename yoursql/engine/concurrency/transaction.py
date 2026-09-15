"""事务对象与事务管理器。

事务的原子性由**页级前像**支撑：事务第一次修改某个页时，把修改前的整页内容
留存下来（同时写入预写日志）。回滚时按相反顺序把前像写回，就能把堆表页、
索引页、目录页一视同仁地恢复原状——不需要为每种页面单独实现逆向操作。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum

from yoursql.common.errors import TransactionError

SERIALIZABLE = "serializable"
READ_COMMITTED = "read_committed"
ISOLATION_LEVELS = (SERIALIZABLE, READ_COMMITTED)


class TransactionState(str, Enum):
    """事务状态机：ACTIVE → COMMITTED / ABORTED，语句出错则先落到 FAILED。"""

    ACTIVE = "active"
    FAILED = "failed"
    COMMITTED = "committed"
    ABORTED = "aborted"


# HOW：FAILED 仍然算"活着"——它继续持有锁，只允许 ROLLBACK，
# 这样"半条语句"不会泄漏给并发事务。
LIVE_STATES = frozenset({TransactionState.ACTIVE, TransactionState.FAILED})


@dataclass
class PageImage:
    """一页在事务中被修改前的完整内容。"""

    page_id: int
    image: bytes
    lsn: int
    previous_lsn: int = 0


@dataclass
class Transaction:
    """一次显式事务的全部运行时状态。"""

    txn_id: int
    isolation: str = SERIALIZABLE
    state: TransactionState = TransactionState.ACTIVE
    # HOW：隐式事务是"自动提交"的内部实现，用户并未 BEGIN；
    # 一些不能回滚的操作（如 DROP TABLE）只禁止显式事务使用。
    implicit: bool = False
    started_at: float = field(default_factory=time.monotonic)
    statements: int = 0
    # HOW：page_images 按页去重保存首次前像；undo_order 记录捕获顺序，
    # 回滚时逆序写回，保证同一页只被恢复一次且顺序确定。
    page_images: dict[int, PageImage] = field(default_factory=dict)
    undo_order: list[int] = field(default_factory=list)
    allocated_pages: list[int] = field(default_factory=list)
    read_resources: set[str] = field(default_factory=set)
    write_resources: set[str] = field(default_factory=set)
    catalog_snapshot: dict[str, object] | None = None

    # ----- 前像记录 -----
    def record_page_image(
        self, page_id: int, image: bytes, previous_lsn: int, lsn: int
    ) -> None:
        """登记某页的首次前像；已被记录过的页不再覆盖。"""

        key = int(page_id)
        if key in self.page_images:
            return
        self.page_images[key] = PageImage(key, image, lsn, previous_lsn)
        self.undo_order.append(key)

    def page_lsn(self, page_id: int) -> int:
        """返回该页在当前事务中的日志序号；未记录过返回 0。"""

        image = self.page_images.get(int(page_id))
        return image.lsn if image is not None else 0

    def undo_plan(self) -> list[PageImage]:
        """按捕获的相反顺序返回回滚计划。"""

        return [self.page_images[key] for key in reversed(self.undo_order)]

    def note_allocated(self, page_id: int) -> None:
        self.allocated_pages.append(int(page_id))

    # ----- 状态与展示 -----
    @property
    def active(self) -> bool:
        """事务是否尚未结束（ACTIVE 与 FAILED 都仍持有锁）。"""

        return self.state in LIVE_STATES

    @property
    def failed(self) -> bool:
        return self.state is TransactionState.FAILED

    def touch(self) -> None:
        self.statements += 1

    def finish(self, state: TransactionState) -> None:
        if not self.active:
            raise TransactionError(
                f"事务 {self.txn_id} 已经结束（{self.state.value}）", txn_id=self.txn_id
            )
        if state not in {
            TransactionState.COMMITTED,
            TransactionState.ABORTED,
        }:
            raise TransactionError(f"非法的结束状态 {state.value}", txn_id=self.txn_id)
        self.state = state

    def stats(self) -> dict[str, object]:
        return {
            "txn_id": self.txn_id,
            "state": self.state.value,
            "isolation": self.isolation,
            "statements": self.statements,
            "pages_touched": len(self.page_images),
            "allocated_pages": len(self.allocated_pages),
            "read_locks": sorted(self.read_resources),
            "write_locks": sorted(self.write_resources),
            "duration_seconds": round(time.monotonic() - self.started_at, 6),
        }

class TransactionManager:
    """分配事务号并跟踪活跃事务。"""

    def __init__(self) -> None:
        self._next_id = 1
        self._active: dict[int, Transaction] = {}
        self._committed = 0
        self._aborted = 0

    def begin(self, isolation: str = SERIALIZABLE) -> Transaction:
        normalized = isolation.strip().lower()
        if normalized not in ISOLATION_LEVELS:
            raise TransactionError(f"不支持的隔离级别 {isolation!r}")
        txn = Transaction(self._next_id, normalized)
        self._next_id += 1
        self._active[txn.txn_id] = txn
        return txn

    def register(self, txn: Transaction) -> Transaction:
        """恢复或测试场景下直接登记一个已构造的事务。"""

        self._active[txn.txn_id] = txn
        self._next_id = max(self._next_id, txn.txn_id + 1)
        return txn

    def finish(self, txn: Transaction, state: TransactionState) -> None:
        self._active.pop(txn.txn_id, None)
        if state is TransactionState.COMMITTED:
            self._committed += 1
        else:
            self._aborted += 1

    def get(self, txn_id: int) -> Transaction | None:
        return self._active.get(int(txn_id))

    @property
    def active_count(self) -> int:
        return len(self._active)

    def active_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._active))

    def stats(self) -> dict[str, object]:
        return {
            "next_txn_id": self._next_id,
            "active": sorted(self._active),
            "committed": self._committed,
            "aborted": self._aborted,
        }

    def reset_counters(self) -> None:
        """仅供崩溃恢复后重新计数使用，不改变活跃事务集合。"""

        self._committed = 0
        self._aborted = 0
