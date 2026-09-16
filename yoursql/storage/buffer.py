"""支持 LRU/FIFO/2Q 的固定容量页缓存。

页面按类型同时登记到普通队列和受保护队列；superblock、catalog、directory、index
属于受保护页，其它页面属于普通页。只有开启 ``protect_page_types`` 时，淘汰才会
优先从普通队列选择，并限制受保护页占用的缓存预算。使用 2Q 时，两类队列还会
分别拆成冷队列和热队列。

名称带 ``_locked`` 的内部辅助函数约定由已经持有 ``self._lock`` 的路径调用，主要
负责维护或读取上述分类队列，避免在内部重复加锁。"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Callable, Iterator

from yoursql.common.errors import StorageError
from yoursql.common.trace import current_trace
from yoursql.storage.disk import DiskManager
from yoursql.storage.page import Page, PageType
from yoursql.storage.wal import WriteAheadLog


_REPLACEMENT_POLICIES = frozenset({"lru", "fifo", "2q"})
# HOW：2Q 只把总容量作为硬上限，同时给冷队列保留固定比例，避免热队列长期挤占冷队列。
TWO_Q_COLD_QUEUE_RATIO: float = 0.25
# HOW：实验默认只为 INDEX/CATALOG 等保护页预留一半缓存，便于四策略都产生可见差异。
PROTECTED_PAGE_RATIO: float = 0.5
_PROTECTED_PAGE_TYPES = frozenset(
    {PageType.SUPERBLOCK, PageType.CATALOG, PageType.DIRECTORY, PageType.INDEX}
)


@dataclass
class BufferFrame:
    page: Page
    pin_count: int = 0
    dirty: bool = False
    loaded_order: int = 0
    last_used: int = 0
    queue: str = "main"
    access_count: int = 1


@dataclass(frozen=True)
class BufferPoolStats:
    """BufferPool 的累计命中统计。"""

    capacity: int
    size: int
    hits: int
    misses: int
    evictions: int
    hit_rate: float
    promotions: int = 0
    writebacks: int = 0
    type_protection_skips: int = 0
    cold_hits: int = 0
    hot_hits: int = 0

    def __getitem__(self, key: str) -> int | float:
        # === 兼容旧调试接口的映射式读取 ===
        """兼容旧的调试调用方；新代码优先使用属性。"""

        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        # === 兼容旧调试接口的映射式遍历 ===
        """返回对象的迭代器。"""
        return iter(
            (
                "capacity",
                "size",
                "hits",
                "misses",
                "evictions",
                "hit_rate",
                "promotions",
                "writebacks",
                "type_protection_skips",
                "cold_hits",
                "hot_hits",
            )
        )

    def to_dict(self) -> dict[str, int | float]:
        """将对象转换为可序列化的字典。"""
        return {
            "capacity": self.capacity,
            "size": self.size,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_rate": self.hit_rate,
            "promotions": self.promotions,
            "writebacks": self.writebacks,
            "type_protection_skips": self.type_protection_skips,
            "cold_hits": self.cold_hits,
            "hot_hits": self.hot_hits,
        }


@dataclass(frozen=True)
class BufferFrameSnapshot:
    """【前端特供】缓存帧的只读检查摘要。"""

    page_id: int
    page_type: str
    pin_count: int
    dirty: bool
    loaded_order: int
    last_used: int
    queue: str
    access_count: int

    def to_dict(self) -> dict[str, int | str | bool]:
        """将对象转换为可序列化的字典。"""
        return {
            "page_id": self.page_id,
            "type": self.page_type,
            "pin_count": self.pin_count,
            "dirty": self.dirty,
            "loaded_order": self.loaded_order,
            "last_used": self.last_used,
            "queue": self.queue,
            "access_count": self.access_count,
        }


@dataclass(frozen=True)
class BufferPoolSnapshot:
    """【前端特供】BufferPool 检查快照；不包含页 payload。"""

    stats: BufferPoolStats
    policy: str
    protect_page_types: bool
    protected_page_limit: int
    frames: tuple[BufferFrameSnapshot, ...]
    eviction_order: tuple[int, ...]
    total: int
    offset: int
    limit: int
    revision: int

    def __getitem__(self, key: str) -> object:
        # === 兼容 HTTP 适配层旧的映射式读取 ===
        """兼容 HTTP 适配层迁移期间的映射式读取。"""

        return self.to_dict()[key]

    def to_dict(self) -> dict[str, object]:
        """将对象转换为可序列化的字典。"""
        return {
            "stats": self.stats.to_dict(),
            "policy": self.policy,
            "protect_page_types": self.protect_page_types,
            "protected_page_limit": self.protected_page_limit,
            "frames": [frame.to_dict() for frame in self.frames],
            "eviction_order": list(self.eviction_order),
            "total": self.total,
            "offset": self.offset,
            "limit": self.limit,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class ChangeSet:
    """【前端特供】BufferPool 页内容变更游标的结果。"""

    revision: int
    changed_page_ids: tuple[int, ...]
    truncated: bool

    def __getitem__(self, key: str) -> object:
        # === 兼容旧变更游标适配层的映射式读取 ===
        """按键或下标读取对象中的元素。"""
        return self.to_dict()[key]

    def to_dict(self) -> dict[str, object]:
        """将对象转换为可序列化的字典。"""
        return {
            "revision": self.revision,
            "changed_page_ids": list(self.changed_page_ids),
            "truncated": self.truncated,
        }


class BufferPool:
    """缓存磁盘页并记录命中、缺页和淘汰统计。

    ``2q`` 是一个专注于教学演示的两队列策略：首次访问进入有配额的冷队列，
    第二次访问晋升热队列。它能避免一次性顺序扫描污染热点页。

    接入日志后额外承担两条职责：

    * **写前日志规则**：脏页在写回磁盘前，必须先把它对应的日志刷到稳定存储
      （见 ``_write_back``），否则日志无法重放/撤销这次修改。
    * **前像捕获**：页第一次被当前事务修改时，把修改前的内容交给前像回调，
    供事务回滚与崩溃恢复使用。
    """

    def __init__(
        self,
        disk: DiskManager,
        capacity: int = 64,
        replacement_policy: str = "lru",
        *,
        protect_page_types: bool = False,
        wal: WriteAheadLog | None = None,
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        if capacity < 1:
            raise ValueError("缓存容量必须为正数")
        policy = replacement_policy.lower()
        if policy not in _REPLACEMENT_POLICIES:
            raise ValueError("replacement_policy 只能是 lru、fifo 或 2q")
        self.disk = disk
        self.capacity = capacity
        self.replacement_policy = policy
        self.protect_page_types = bool(protect_page_types)
        self.wal = wal
        # HOW：前像回调由运行时注入，内部会查询"当前线程所属事务"，
        # 因此缓冲池本身不需要知道事务对象。返回值是该页被赋予的日志序号，
        # 缓冲池把它写回页头，从而让"日志先于数据页落盘"可以被校验。
        self.image_sink: Callable[[int, bytes, int], int] | None = None
        # HOW：`_frames` 自身按淘汰优先级排序（队首最先淘汰），使淘汰 O(1) 摊还。
        self._frames: OrderedDict[int, BufferFrame] = OrderedDict()
        # HOW：2Q 的冷队列是 FIFO，热队列是 LRU；`_frames` 仍保留统一页号索引。
        self._cold_queue: OrderedDict[int, None] = OrderedDict()
        self._hot_queue: OrderedDict[int, None] = OrderedDict()
        # HOW：类型保护使用同一策略的平行队列，避免每次淘汰线性跳过 INDEX 页。
        self._regular_queue: OrderedDict[int, None] = OrderedDict()
        self._protected_queue: OrderedDict[int, None] = OrderedDict()
        self._regular_cold_queue: OrderedDict[int, None] = OrderedDict()
        self._regular_hot_queue: OrderedDict[int, None] = OrderedDict()
        self._protected_cold_queue: OrderedDict[int, None] = OrderedDict()
        self._protected_hot_queue: OrderedDict[int, None] = OrderedDict()
        self._protected_page_count = 0
        self._clock = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._promotions = 0
        self._writebacks = 0
        self._type_protection_skips = 0
        self._cold_hits = 0
        self._hot_hits = 0
        self._revision = 0
        self._change_log: deque[tuple[int, int]] = deque(maxlen=4096)
        self._event_log: deque[dict[str, object]] = deque(maxlen=4096)
        self._lock = RLock()

    @property
    def size(self) -> int:
        """返回对象占用或包含的大小。"""
        return len(self._frames)

    def _protected_page_limit(self, capacity: int | None = None) -> int:
        """返回受保护页预算，避免 INDEX 页无限挤占普通页。"""

        target_capacity = self.capacity if capacity is None else capacity
        ratio = PROTECTED_PAGE_RATIO
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not 0 < ratio <= 1
        ):
            raise ValueError("PROTECTED_PAGE_RATIO 必须位于 (0, 1] 区间")
        return min(target_capacity, max(1, int(target_capacity * ratio)))

    def _two_q_cold_limit(self) -> int:
        """返回当前容量下 2Q 冷队列的页数上限。"""

        ratio = TWO_Q_COLD_QUEUE_RATIO
        if (
            isinstance(ratio, bool)
            or not isinstance(ratio, (int, float))
            or not 0 < ratio <= 1
        ):
            raise ValueError("TWO_Q_COLD_QUEUE_RATIO 必须位于 (0, 1] 区间")
        if self.capacity == 1:
            return 1
        return min(self.capacity - 1, max(1, int(self.capacity * ratio)))

    def _two_q_hot_limit(self) -> int:
        """返回当前容量下 2Q 热队列的页数上限。"""

        return self.capacity - self._two_q_cold_limit()

    def stats(self) -> BufferPoolStats:
        """返回对象的统计信息。"""
        total = self._hits + self._misses
        return BufferPoolStats(
            capacity=self.capacity,
            size=len(self._frames),
            hits=self._hits,
            misses=self._misses,
            evictions=self._evictions,
            hit_rate=self._hits / total if total else 0.0,
            promotions=self._promotions,
            writebacks=self._writebacks,
            type_protection_skips=self._type_protection_skips,
            cold_hits=self._cold_hits,
            hot_hits=self._hot_hits,
        )

    statistics = stats

    def set_replacement_policy(self, replacement_policy: str) -> bool:
        """切换后续淘汰使用的策略，返回策略是否发生变化。"""

        policy = replacement_policy.lower()
        if policy not in _REPLACEMENT_POLICIES:
            raise ValueError("replacement_policy 只能是 lru、fifo 或 2q")
        with self._lock:
            changed = self.replacement_policy != policy
            if changed:
                self.replacement_policy = policy
                self._reset_policy_queues_locked(policy)
            else:
                self.replacement_policy = policy
            return changed

    def set_protect_page_types(self, enabled: bool) -> bool:
        """在线切换系统页和索引页保护，返回开关是否发生变化。"""

        if not isinstance(enabled, bool):
            raise ValueError("protect_page_types 必须是布尔值")
        with self._lock:
            changed = self.protect_page_types != enabled
            self.protect_page_types = enabled
            if changed and enabled:
                while self._protected_page_count > self._protected_page_limit():
                    if self._find_protected_victim_locked() is None:
                        break
                    self._evict_one(reason="protection-budget")
            return changed

    def reset_runtime(self) -> None:
        """清空缓存帧和累计运行态，保留磁盘数据与当前策略。"""

        with self._lock:
            self.flush_all()
            self._frames.clear()
            self._cold_queue.clear()
            self._hot_queue.clear()
            self._regular_queue.clear()
            self._protected_queue.clear()
            self._regular_cold_queue.clear()
            self._regular_hot_queue.clear()
            self._protected_cold_queue.clear()
            self._protected_hot_queue.clear()
            self._protected_page_count = 0
            self._clock = 0
            self._hits = 0
            self._misses = 0
            self._evictions = 0
            self._promotions = 0
            self._writebacks = 0
            self._type_protection_skips = 0
            self._cold_hits = 0
            self._hot_hits = 0
            self._event_log.clear()

    def _reset_policy_queues_locked(self, policy: str) -> None:
        """切换策略时重建队列，不清空已有缓存页。"""

        self._cold_queue.clear()
        self._hot_queue.clear()
        self._regular_queue.clear()
        self._protected_queue.clear()
        self._regular_cold_queue.clear()
        self._regular_hot_queue.clear()
        self._protected_cold_queue.clear()
        self._protected_hot_queue.clear()
        self._protected_page_count = 0
        for page_id, frame in self._frames.items():
            if policy == "2q":
                frame.queue = "a1in"
                self._cold_queue[page_id] = None
            else:
                frame.queue = "main"
            self._add_type_queue_locked(page_id, frame)

    def _clear_type_queues_locked(self) -> None:
        """清空按页面类型拆分的策略队列。"""

        self._regular_queue.clear()
        self._protected_queue.clear()
        self._regular_cold_queue.clear()
        self._regular_hot_queue.clear()
        self._protected_cold_queue.clear()
        self._protected_hot_queue.clear()
        self._protected_page_count = 0

    def _remove_type_queue_locked(self, page_id: int, frame: BufferFrame) -> None:
        """从所有类型队列移除一个缓存帧。"""

        queues = (
            self._regular_queue,
            self._protected_queue,
            self._regular_cold_queue,
            self._regular_hot_queue,
            self._protected_cold_queue,
            self._protected_hot_queue,
        )
        for queue in queues:
            queue.pop(page_id, None)
        if self._is_protected_page(frame):
            self._protected_page_count -= 1

    def _add_type_queue_locked(self, page_id: int, frame: BufferFrame) -> None:
        """把缓存帧加入当前策略对应的页面类型队列。"""

        protected = self._is_protected_page(frame)
        if self.replacement_policy == "2q":
            if frame.queue == "a1in":
                queue = (
                    self._protected_cold_queue
                    if protected
                    else self._regular_cold_queue
                )
            else:
                queue = (
                    self._protected_hot_queue
                    if protected
                    else self._regular_hot_queue
                )
        else:
            queue = self._protected_queue if protected else self._regular_queue
        queue[page_id] = None
        if protected:
            self._protected_page_count += 1

    def _move_type_queue_to_end_locked(self, page_id: int, frame: BufferFrame) -> None:
        """按 LRU 访问顺序移动页面类型队列中的帧。"""

        protected = self._is_protected_page(frame)
        queue = self._protected_queue if protected else self._regular_queue
        queue.move_to_end(page_id)

    def resize(self, capacity: int) -> int:
        """在线调整缓存容量，并返回因缩容淘汰的页数。

        WHY：扩容只改变上限并保留当前热页；缩容必须先确认有足够的未 pin 页，
        再按当前淘汰策略回收，避免热更新把正在使用的页强行移出缓存。
        """

        if isinstance(capacity, bool) or not isinstance(capacity, int):
            raise ValueError("缓存容量必须是整数")
        if capacity < 1:
            raise ValueError("缓存容量必须为正数")
        with self._lock:
            required = max(0, len(self._frames) - capacity)
            available = sum(frame.pin_count == 0 for frame in self._frames.values())
            if required > available:
                raise StorageError(
                    "目标缓存容量小于当前 pin 页数量", reason="pinned"
                )
            if required == 0:
                self.capacity = capacity
                return 0
            self.capacity = capacity
            for _ in range(required):
                self._evict_one(reason="resize")
            return required

    def _touch(self, frame: BufferFrame) -> None:
        """更新缓存页的访问顺序和相关统计。"""
        self._clock += 1
        frame.last_used = self._clock

    def _touch_page(self, page_id: int, frame: BufferFrame) -> None:
        """按当前策略处理一次命中或写入访问。"""

        self._touch(frame)
        frame.access_count += 1
        if self.replacement_policy == "lru":
            self._frames.move_to_end(page_id)
            self._move_type_queue_to_end_locked(page_id, frame)
            return
        if self.replacement_policy != "2q":
            return
        if frame.queue == "a1in":
            # HOW：晋升不能突破热队列配额；淘汰时排除当前页，避免把正在晋升的页淘汰。
            if self._two_q_hot_limit() <= 0:
                return
            if len(self._hot_queue) >= self._two_q_hot_limit():
                try:
                    self._evict_one(
                        reason="2q-promotion",
                        preferred_queue="hot",
                        strict_queue=True,
                        exclude_page_id=page_id,
                    )
                except StorageError:
                    # HOW：热队列全是 pin 页时保留当前页在冷队列，等待后续访问再晋升。
                    return
            # 冷队列是 FIFO，命中后晋升到热队列。
            self._cold_queue.pop(page_id, None)
            self._remove_type_queue_locked(page_id, frame)
            self._hot_queue[page_id] = None
            frame.queue = "am"
            self._add_type_queue_locked(page_id, frame)
            self._promotions += 1
        else:
            # am简单用LRU实现
            self._hot_queue.move_to_end(page_id)
            queue = (
                self._protected_hot_queue
                if self._is_protected_page(frame)
                else self._regular_hot_queue
            )
            queue.move_to_end(page_id)

    def _queue_page_locked(self, page_id: int, frame: BufferFrame) -> None:
        """把新页加入当前策略的队列。"""

        if self.replacement_policy == "2q":
            frame.queue = "a1in"
            self._cold_queue[page_id] = None
        else:
            frame.queue = "main"
        self._add_type_queue_locked(page_id, frame)

    def _new_page_eviction_queue_locked(self) -> str | None:
        """返回缓存已满时加入新冷页前应优先腾挪的 2Q 队列。"""

        if self.replacement_policy != "2q":
            return None
        if len(self._cold_queue) >= self._two_q_cold_limit():
            return "cold"
        return "hot"

    def _two_q_queue_order_locked(self) -> tuple[str, str]:
        """返回下一次普通淘汰的 2Q 队列顺序。"""

        if len(self._cold_queue) < self._two_q_cold_limit():
            return ("hot", "cold")
        return ("cold", "hot")

    def _two_q_queue_ids_locked(
        self, queue_name: str, protected: bool | None = None
    ) -> Iterator[int]:
        """按队列和页面类型产出 2Q 页号。"""

        if queue_name == "cold":
            all_queue = self._cold_queue
            regular_queue = self._regular_cold_queue
            protected_queue = self._protected_cold_queue
        else:
            all_queue = self._hot_queue
            regular_queue = self._regular_hot_queue
            protected_queue = self._protected_hot_queue

        if protected is True:
            yield from protected_queue
        elif protected is False:
            yield from regular_queue
        elif self.protect_page_types:
            yield from regular_queue
            yield from protected_queue
        else:
            yield from all_queue

    def _find_2q_victim_locked(
        self,
        queue_order: tuple[str, ...],
        protected: bool | None = None,
        exclude_page_id: int | None = None,
    ) -> tuple[int, BufferFrame] | None:
        """按指定 2Q 队列顺序查找一个可淘汰页。"""

        for queue_name in queue_order:
            for page_id in self._two_q_queue_ids_locked(queue_name, protected):
                if page_id == exclude_page_id:
                    continue
                frame = self._frames[page_id]
                if not frame.pin_count:
                    return page_id, frame
        return None

    def _ordered_page_ids_locked(self) -> Iterator[int]:
        """按当前策略产出从老到新的页号，不创建中间候选列表。"""

        if self.replacement_policy == "2q":
            for queue_name in self._two_q_queue_order_locked():
                yield from self._two_q_queue_ids_locked(queue_name)
            return
        yield from self._frames

    def _regular_page_ids_locked(self) -> Iterator[int]:
        """按当前策略产出普通页，供页面类型保护的快速淘汰路径使用。"""

        if self.replacement_policy == "2q":
            for queue_name in self._two_q_queue_order_locked():
                yield from self._two_q_queue_ids_locked(queue_name, False)
            return
        yield from self._regular_queue

    def _protected_page_ids_locked(self) -> Iterator[int]:
        """按当前策略产出受保护页，供检查接口展示完整淘汰顺序。"""

        if self.replacement_policy == "2q":
            for queue_name in self._two_q_queue_order_locked():
                yield from self._two_q_queue_ids_locked(queue_name, True)
            return
        yield from self._protected_queue

    def _find_protected_victim_locked(self) -> tuple[int, BufferFrame] | None:
        """按当前替换策略选择一个未 pin 的受保护页。"""

        for page_id in self._protected_page_ids_locked():
            frame = self._frames[page_id]
            if frame.pin_count:
                continue
            return page_id, frame
        return None

    @staticmethod
    def _is_protected_page(frame: BufferFrame) -> bool:
        """判断页类型保护开关是否应保护当前页。"""

        return frame.page.page_type in _PROTECTED_PAGE_TYPES

    def _mark_changed(self, page_id: int) -> None:
        """记录页内容/生命周期变化，供只读检查页级增量刷新。"""

        self._revision += 1
        self._change_log.append((self._revision, int(page_id)))

    def _record_event(self, action: str, page_id: int, **details: object) -> None:
        """记录可供查询诊断查看的缓存事件。"""
        event = {
            "at": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "page_id": int(page_id),
            **details,
        }
        self._event_log.append(event)
        trace = current_trace.get()
        if trace is not None:
            trace.event(action, int(page_id), **details)

    def _eviction_order_locked(self) -> list[int]:
        """返回当前可淘汰页的优先级；列表首项下一次最先被淘汰。"""

        if self.protect_page_types:
            # HOW：先保持 LRU/FIFO/2Q 各自的队列顺序，再把普通页排在受保护页之前，
            # 确保检查接口展示的顺序与真正的淘汰路径一致。
            ordered_page_ids = (
                *self._regular_page_ids_locked(),
                *self._protected_page_ids_locked(),
            )
        else:
            ordered_page_ids = tuple(self._ordered_page_ids_locked())
        return [
            page_id
            for page_id in ordered_page_ids
            if self._frames[page_id].pin_count == 0
        ]

    def _write_back(self, frame: BufferFrame) -> None:
        """按写前日志规则把脏页写回磁盘。

        WHY：日志必须先于数据页落盘，否则崩溃后既无法用日志补齐这次修改，
        也无法把它撤销——磁盘上会出现“无日志可解释的改动”。
        """

        if self.wal is not None and self.wal.enabled:
            self.wal.ensure_persisted(frame.page.lsn)
        self.disk.write(frame.page)
        frame.dirty = False

    def _capture_image(self, page_id: int, previous: Page | None) -> int:
        """把修改前的页内容交给当前事务，返回该页对应的日志序号（无事务时 0）。"""

        sink = self.image_sink
        if sink is None:
            return 0
        snapshot = previous
        if snapshot is None:
            # HOW：页不在缓存中时（极少见，通常是已被淘汰后又被直接改写），
            # 只能从磁盘取当前内容作为前像。
            try:
                snapshot = self.disk.peek(page_id)
            except StorageError:
                return 0
        return sink(page_id, snapshot.to_bytes(), snapshot.lsn)

    def _evict_one(
        self,
        *,
        reason: str = "capacity",
        preferred_queue: str | None = None,
        strict_queue: bool = False,
        exclude_page_id: int | None = None,
    ) -> None:
        """淘汰队首第一个未 pin 的页。

        WHY：原实现每次淘汰都构建候选列表并取 min，复杂度 O(容量)；全表扫描时几乎每页
        都触发一次淘汰，缓冲池越大反而越慢。现在按维护好的优先级顺序取首项，
        选中的页与原来一致（时钟单调递增，不会出现同优先级）。
        """

        victim: tuple[int, BufferFrame] | None = None
        protected_skips = 0

        if preferred_queue not in {None, "cold", "hot"}:
            raise ValueError("preferred_queue 只能是 cold 或 hot")

        # 2Q 需要在新增冷页和冷页晋升时分别偏向热/冷队列。
        if self.replacement_policy == "2q":
            queue_order = (
                (preferred_queue,)
                if strict_queue and preferred_queue is not None
                else (
                    (preferred_queue, "cold" if preferred_queue == "hot" else "hot")
                    if preferred_queue is not None
                    else self._two_q_queue_order_locked()
                )
            )
            if self.protect_page_types:
                over_protected_limit = (
                    self._protected_page_count > self._protected_page_limit()
                )
                if over_protected_limit:
                    victim = self._find_2q_victim_locked(
                        queue_order, True, exclude_page_id
                    )
                if victim is None:
                    victim = self._find_2q_victim_locked(
                        queue_order, False, exclude_page_id
                    )
                    if victim is not None:
                        protected_skips = self._protected_page_count
                if victim is None:
                    victim = self._find_2q_victim_locked(
                        queue_order, True, exclude_page_id
                    )
            else:
                victim = self._find_2q_victim_locked(
                    queue_order, None, exclude_page_id
                )
        # LRU/FIFO 仍沿用原有页面类型保护路径。
        elif self.protect_page_types:
            over_protected_limit = (
                self._protected_page_count > self._protected_page_limit()
            )
            if over_protected_limit:
                victim = self._find_protected_victim_locked()
            if victim is None:
                for page_id in self._regular_page_ids_locked():
                    frame = self._frames[page_id]
                    if not frame.pin_count:
                        victim = (page_id, frame)
                        protected_skips = self._protected_page_count
                        break
            if victim is None:
                for page_id in self._ordered_page_ids_locked():
                    frame = self._frames[page_id]
                    if not frame.pin_count:
                        victim = (page_id, frame)
                        break
        else:
            for page_id in self._ordered_page_ids_locked():
                frame = self._frames[page_id]
                if not frame.pin_count:
                    victim = (page_id, frame)
                    break

        # 正常淘汰
        if victim is None:
            raise StorageError("缓存已满且所有页都被 pin")
        page_id, frame = victim
        writeback = frame.dirty
        if frame.dirty:
            self._write_back(frame)
        self._cold_queue.pop(page_id, None)
        self._hot_queue.pop(page_id, None)
        self._remove_type_queue_locked(page_id, frame)
        del self._frames[page_id]
        self._evictions += 1
        self._type_protection_skips += protected_skips
        self._record_event(
            "evict",
            page_id,
            dirty=writeback,
            writeback=writeback,
            policy=self.replacement_policy,
            reason=reason,
            queue=frame.queue,
            page_type=frame.page.page_type.value,
            protected=protected_skips > 0,
            protected_skips=protected_skips,
        )

    def get_page(self, page_id: int, pin: bool = True) -> Page:
        """按页号读取缓存页；``pin`` 为真时固定该页，避免其被淘汰。"""
        normalized = int(page_id)
        with self._lock:
            frame = self._frames.get(normalized)
            trace = current_trace.get()
            if trace is not None:
                trace.event("get_page", normalized, cache_hit=frame is not None)

            # 处理缓存命中
            if frame is not None:
                self._hits += 1
                if self.replacement_policy == "2q":
                    if frame.queue == "a1in":
                        self._cold_hits += 1
                    elif frame.queue == "am":
                        self._hot_hits += 1
                self._touch_page(normalized, frame)
                if pin:
                    frame.pin_count += 1
                return frame.page

            # 处理没获取得到
            self._misses += 1
            page = self.disk.read(normalized)
            if len(self._frames) >= self.capacity:
                self._evict_one(
                    preferred_queue=self._new_page_eviction_queue_locked()
                )
            self._clock += 1
            frame = BufferFrame(
                page, 1 if pin else 0, False, self._clock, self._clock
            )
            self._frames[normalized] = frame
            self._queue_page_locked(normalized, frame)
            return page

    def put_page(self, page: Page, *, dirty: bool = True) -> None:
        """将页写入缓存并按需标记为脏页。"""
        with self._lock:
            trace = current_trace.get()
            if trace is not None:
                trace.event("put_page", page.page_id, dirty=dirty)
            frame = self._frames.get(page.page_id)
            if frame is not None:
                # HOW：前像必须在覆盖帧内容之前取，因此先算 LSN 再赋值。
                if dirty:
                    lsn = self._capture_image(page.page_id, frame.page)
                    if lsn:
                        page.lsn = lsn
                frame.page = page
                frame.dirty = frame.dirty or dirty
                self._touch(frame)
            else:
                if dirty:
                    # HOW：页不在缓存里，磁盘内容才是"修改前"的样子。
                    lsn = self._capture_image(page.page_id, None)
                    if lsn:
                        page.lsn = lsn
                if len(self._frames) >= self.capacity:
                    self._evict_one(
                        preferred_queue=self._new_page_eviction_queue_locked()
                    )
                self._clock += 1
                frame = BufferFrame(page, 0, dirty, self._clock, self._clock)
                self._frames[page.page_id] = frame
                self._queue_page_locked(page.page_id, frame)
            self._mark_changed(page.page_id)

    def pin_page(self, page_id: int) -> Page:
        """固定指定页，避免其在使用期间被淘汰。"""
        return self.get_page(page_id, pin=True)

    def unpin(self, page_id: int, dirty: bool = False) -> None:
        """解除指定页的固定状态。"""
        with self._lock:
            frame = self._frames.get(int(page_id))
            if frame is None:
                raise StorageError(f"页 {page_id} 不在缓存中")
            if frame.pin_count > 0:
                frame.pin_count -= 1
            frame.dirty = frame.dirty or dirty

    def mark_dirty(self, page_id: int) -> None:
        """标记页已修改，等待后续刷新。"""
        with self._lock:
            frame = self._frames.get(int(page_id))
            if frame is None:
                raise StorageError(f"页 {page_id} 不在缓存中")
            frame.dirty = True

    def flush_page(self, page_id: int) -> None:
        """将指定脏页刷新到磁盘。"""
        with self._lock:
            frame = self._frames.get(int(page_id))
            if frame is None:
                return
            if frame.dirty:
                self._write_back(frame)

    def flush_all(self) -> None:
        """将全部脏页刷新到磁盘。"""
        with self._lock:
            for page_id in tuple(self._frames):
                self.flush_page(page_id)
            self.disk.sync()

    def restore_page(self, page_id: int, raw: bytes) -> None:
        """用日志前像把页恢复成旧内容，并同步缓存与磁盘。

        HOW：先直接写盘（跳过脏页标记），再让缓存帧失效后按需重新装载，
        避免缓存里残留回滚前的新内容。
        """

        with self._lock:
            page = Page.from_bytes(raw, page_size=self.disk.page_size)
            self.disk.write(page)
            self._frames.pop(int(page_id), None)
            self._mark_changed(int(page_id))

    def new_page(
        self, page_type: PageType = PageType.FREE, payload: bytes = b""
    ) -> Page:
        """分配并缓存一个新页。"""
        page = self.disk.allocate(page_type, payload)
        self.put_page(page, dirty=False)
        return page

    def delete_page(self, page_id: int) -> None:
        """删除缓存中的指定页。"""
        self.delete_pages((page_id,))

    def delete_pages(self, page_ids: Iterable[int]) -> None:
        """批量删除缓存页并一次提交磁盘 free-list。"""

        with self._lock:
            normalized_ids = tuple(dict.fromkeys(int(page_id) for page_id in page_ids))
            for page_id in normalized_ids:
                frame = self._frames.get(page_id)
                if frame is not None and frame.pin_count:
                    raise StorageError(f"页 {page_id} 仍被 pin")
            # WHY：先由磁盘层完成完整校验，再移除缓存帧，避免批量删除失败时
            # 出现“缓存已删、磁盘未删”的半完成状态。
            self.disk.free_many(normalized_ids)
            for page_id in normalized_ids:
                frame = self._frames.get(page_id)
                if frame is not None:
                    self._remove_type_queue_locked(page_id, frame)
                self._frames.pop(page_id, None)
                self._cold_queue.pop(page_id, None)
                self._hot_queue.pop(page_id, None)
                self._mark_changed(page_id)

    def close(self) -> None:
        """关闭资源并释放关联状态。"""
        self.flush_all()
        self._frames.clear()
        self._cold_queue.clear()
        self._hot_queue.clear()
        self._clear_type_queues_locked()

    def snapshot(self, offset: int = 0, limit: int = 100) -> BufferPoolSnapshot:
        """【前端特供】只复制有限帧头，不 pin、不淘汰、不刷新或推进替换时钟。"""
        from itertools import islice

        with self._lock:
            return BufferPoolSnapshot(
                stats=self.stats(),
                policy=self.replacement_policy,
                protect_page_types=self.protect_page_types,
                protected_page_limit=self._protected_page_limit(),
                frames=tuple(
                    BufferFrameSnapshot(
                        page_id=page_id,
                        page_type=frame.page.page_type.value,
                        pin_count=frame.pin_count,
                        dirty=frame.dirty,
                        loaded_order=frame.loaded_order,
                        last_used=frame.last_used,
                        queue=frame.queue,
                        access_count=frame.access_count,
                    )
                    for page_id, frame in islice(
                        self._frames.items(), offset, offset + limit
                    )
                ),
                eviction_order=tuple(self._eviction_order_locked()),
                total=len(self._frames),
                offset=offset,
                limit=limit,
                revision=self._revision,
            )

    def events(self, limit: int = 100) -> list[dict[str, object]]:
        """【前端特供】返回最近缓存淘汰事件，不读取磁盘也不改变缓存状态。"""
        if limit < 1:
            return []
        with self._lock:
            return [dict(event) for event in list(self._event_log)[-limit:]]

    def changes_since(self, revision: int) -> ChangeSet:
        """【前端特供】返回指定游标之后变化的页号，不读取磁盘也不触碰替换状态。"""

        with self._lock:
            if revision >= self._revision:
                return ChangeSet(self._revision, (), False)
            first_revision = (
                self._change_log[0][0] if self._change_log else self._revision + 1
            )
            truncated = revision < first_revision - 1
            page_ids = list(
                dict.fromkeys(
                    page_id
                    for item_revision, page_id in self._change_log
                    if item_revision > revision
                )
            )
            return ChangeSet(self._revision, tuple(page_ids), truncated)

    def peek_page(self, page_id: int) -> Page:
        """【调试用】复制缓存最新页或只读磁盘页，保持缓存与 I/O 指标不变。"""
        with self._lock:
            frame = self._frames.get(page_id)
            if frame is not None:
                page = frame.page
                return Page(
                    page.page_id, page.page_size, page.page_type, page.payload, page.lsn
                )
            return self.disk.peek(page_id)

    def __contains__(self, page_id: object) -> bool:
        """判断指定元素是否存在于对象中。"""
        return isinstance(page_id, int) and page_id in self._frames
