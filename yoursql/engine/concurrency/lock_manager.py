"""表级共享/排他锁与严格两阶段封锁。

封锁协议
--------
采用**严格两阶段封锁（Strict 2PL）**：事务一旦持有锁就保持到事务结束，
因此不会出现级联回滚，且天然满足可串行化。

* 读操作申请 **S 锁**（共享），写操作申请 **X 锁**（排他）；
* S/S 相容，S/X 与 X/X 互斥；
* 允许 **锁升级**（S→X），但只有在当前事务是唯一持有者时才立即成功；
* 等待超过 ``timeout`` 秒抛出 ``ConcurrencyError``；
* 等待期间用**等待图（wait-for graph）**检测死锁，选出环中最年轻的事务
  （事务号最大者）作为牺牲者并将其标记为待回滚。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from threading import Condition, RLock

from yoursql.common.errors import ConcurrencyError


class LockMode(str, Enum):
    """锁模式；值直接用于日志与 EXPLAIN 展示。"""

    SHARED = "S"#用途：读取资源，共享锁
    EXCLUSIVE = "X"#用途：修改资源，排他锁


@dataclass
class LockEntry:
    """某个事务在某个资源上持有的一种锁及其重入计数。"""

    txn_id: int
    mode: LockMode
    count: int = 1


@dataclass
class LockStats:
    """锁管理器的可观测指标。"""

    granted: int = 0
    waited: int = 0
    timeouts: int = 0
    deadlocks: int = 0
    upgraded: int = 0
    released: int = 0
    resources: dict[str, int] = field(default_factory=dict)


class LockManager:
    """管理事务对命名资源（当前为表）的封锁请求。"""

    def __init__(self, *, timeout: float = 5.0, enabled: bool = True) -> None:
        self.enabled = bool(enabled)
        self.timeout = float(timeout)
        self._condition = Condition(RLock())
        self._table: dict[str, list[LockEntry]] = {}
        self._held: dict[int, dict[str, LockEntry]] = {}
        self._waiting: dict[int, str] = {}
        self._aborted: dict[int, str] = {}
        self.stats = LockStats()

    # ----- 查询接口 -----
    def holders(self, resource: str) -> tuple[LockEntry, ...]:
        with self._condition:
            return tuple(self._table.get(resource.lower(), ()))

    def waiters(self, resource: str) -> tuple[int, ...]:
        resource = resource.lower()
        with self._condition:
            return tuple(
                txn_id for txn_id, waiting in self._waiting.items() if waiting == resource
            )

    def held_by(self, txn_id: int) -> dict[str, LockMode]:
        with self._condition:
            return {
                resource: entry.mode
                for resource, entry in self._held.get(txn_id, {}).items()
            }

    def snapshot(self) -> dict[str, object]:
        """返回当前锁表快照，供工作台与验收演示查看封锁状态。"""

        with self._condition:
            return {
                "enabled": self.enabled,
                "timeout": self.timeout,
                "resources": {
                    resource: [
                        {"txn": entry.txn_id, "mode": entry.mode.value, "count": entry.count}
                        for entry in entries
                    ]
                    for resource, entries in sorted(self._table.items())
                },
                "waiting": {str(key): value for key, value in self._waiting.items()},
                "stats": {
                    "granted": self.stats.granted,
                    "waited": self.stats.waited,
                    "timeouts": self.stats.timeouts,
                    "deadlocks": self.stats.deadlocks,
                    "upgraded": self.stats.upgraded,
                    "released": self.stats.released,
                },
            }

    # ----- 加锁 -----
    def acquire(self, txn_id: int, resource: str, mode: LockMode) -> bool:#申请锁
        """申请锁；成功返回 ``True``，等待超时或无解的死锁抛 ``ConcurrencyError``。"""
        #可以把申请分为四个阶段
        if not self.enabled:
            return True
        resource = resource.strip().lower()
        deadline = time.monotonic() + self.timeout#第一阶段：统一资源名，设置等待期限
        with self._condition:
            self._raise_if_aborted(txn_id)
            while True:
                mine = self._held.get(txn_id, {}).get(resource)#第二阶段：检查是否已持有锁
                if mine is not None:
                    if mine.mode is LockMode.EXCLUSIVE or mode is LockMode.SHARED:
                        mine.count += 1
                        return True
                    if self._can_upgrade(txn_id, resource):#第三阶段：处理锁升级
                        mine.mode = LockMode.EXCLUSIVE
                        mine.count += 1
                        self.stats.upgraded += 1
                        return True
                elif self._compatible(resource, txn_id, mode):#第四阶段：新申请能通过就登记，否则等待
                    self._grant(txn_id, resource, mode)
                    return True

                self._waiting[txn_id] = resource
                self.stats.waited += 1
                self._detect_deadlock(txn_id)#检测死锁
                #处理超时情况:等待时间太长就退出
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._waiting.pop(txn_id, None)
                    self.stats.timeouts += 1
                    self._condition.notify_all()
                    raise ConcurrencyError(
                        f"等待 {resource!r} 的 {mode.value} 锁超时",
                        resource=resource,
                        txn_id=txn_id,
                        timeout=self.timeout,
                    )
                self._condition.wait(remaining)
                self._raise_if_aborted(txn_id)

    def grant_after_abort(self, txn_id: int) -> None:
        """放弃等待（事务被判为死锁牺牲者或主动退出）后清理等待状态。"""

        with self._condition:
            self._waiting.pop(txn_id, None)
            self._condition.notify_all()

    def _raise_if_aborted(self, txn_id: int) -> None:
        reason = self._aborted.pop(txn_id, None)
        if reason is not None:
            raise ConcurrencyError(reason, txn_id=txn_id)

    def mark_aborted(self, txn_id: int, reason: str) -> None:
        """由死锁检测选出牺牲者；被选中的事务在下次检查时抛出异常。"""

        self._aborted[txn_id] = reason
        self._waiting.pop(txn_id, None)
        self._condition.notify_all()

    # ----- 释放 -----，释放事务持有的全部锁
    def release_all(self, txn_id: int) -> int:
        """释放事务持有的全部锁，返回释放的资源数。"""

        with self._condition:
            held = self._held.pop(txn_id, {})
            for resource in held:
                entries = self._table.get(resource)
                if entries is None:
                    continue
                remaining = [entry for entry in entries if entry.txn_id != txn_id]
                if remaining:
                    self._table[resource] = remaining
                else:
                    del self._table[resource]
            self._waiting.pop(txn_id, None)
            self._aborted.pop(txn_id, None)
            self.stats.released += len(held)
            self._condition.notify_all()
            return len(held)

    def release_shared(self, txn_id: int) -> int:
        """释放事务持有的全部 S 锁（读已提交隔离级别在语句结束时调用）。"""

        with self._condition:
            held = self._held.get(txn_id)
            if not held:
                return 0
            released = 0
            for resource in list(held):
                entry = held[resource]
                if entry.mode is not LockMode.SHARED:
                    continue
                del held[resource]
                entries = self._table.get(resource, [])
                remaining = [item for item in entries if item.txn_id != txn_id]
                if remaining:
                    self._table[resource] = remaining
                else:
                    self._table.pop(resource, None)
                released += 1
            if not held:
                self._held.pop(txn_id, None)
            self.stats.released += released
            self._condition.notify_all()
            return released

    # ----- 内部判定 -----
    def _grant(self, txn_id: int, resource: str, mode: LockMode) -> None:
        entry = LockEntry(txn_id, mode)
        self._table.setdefault(resource, []).append(entry)
        self._held.setdefault(txn_id, {})[resource] = entry
        self._waiting.pop(txn_id, None)
        self.stats.granted += 1
    #S所和X锁怎样判断冲突，只要已有锁或者新申请的锁有一个是X，就不兼容
    def _compatible(self, resource: str, txn_id: int, mode: LockMode) -> bool:
        for entry in self._table.get(resource, ()):
            if entry.txn_id == txn_id:
                continue
            if entry.mode is LockMode.EXCLUSIVE or mode is LockMode.EXCLUSIVE:
                return False
        return True

    def _can_upgrade(self, txn_id: int, resource: str) -> bool:
        return all(
            entry.txn_id == txn_id for entry in self._table.get(resource, ())
        )

    def _detect_deadlock(self, txn_id: int) -> None:
        """在等待图中寻找经过 ``txn_id`` 的环；命中则选定牺牲者。"""

        graph = self._wait_for_graph()
        cycle = _find_cycle_through(graph, txn_id)
        if cycle is None:
            return
        self.stats.deadlocks += 1
        victim = max(cycle)#选取牺牲者，这一步我选的是事务号中最大的事务，也就是较晚开始的事务
        reason = f"检测到死锁（事务 {sorted(cycle)}），事务 {victim} 已被回滚"
        if victim == txn_id:
            self._waiting.pop(txn_id, None)
            raise ConcurrencyError(reason, txn_id=txn_id, cycle=sorted(cycle))
        self.mark_aborted(victim, reason)

    def _wait_for_graph(self) -> dict[int, set[int]]:
        """构造等待图：等待者 → 它所需资源的持有者。"""

        graph: dict[int, set[int]] = {}
        for waiting_txn, resource in self._waiting.items():
            edges = graph.setdefault(waiting_txn, set())
            held = self._held.get(waiting_txn, {})
            desired = held.get(resource)
            for entry in self._table.get(resource, ()):
                if entry.txn_id == waiting_txn:
                    continue
                # HOW：已持 S 锁的事务在等同一资源的 X 锁时属于升级等待，
                # 只与其它持有者构成等待边。
                if desired is not None and desired.mode is LockMode.EXCLUSIVE:
                    edges.add(entry.txn_id)
                elif entry.mode is LockMode.EXCLUSIVE:
                    edges.add(entry.txn_id)
        return graph


def _find_cycle_through(
    graph: dict[int, set[int]], start: int
) -> list[int] | None:
    """返回从 ``start`` 出发能回到自身的环（含起点），没有则返回 ``None``。"""

    stack: list[tuple[int, list[int]]] = [(start, [start])]
    while stack:
        node, path = stack.pop()
        for neighbour in graph.get(node, ()):  # 深度优先，路径即等待链
            if neighbour == start:
                return path
            if neighbour in path:
                continue
            stack.append((neighbour, [*path, neighbour]))
    return None
