"""支持 LRU/FIFO/2Q 的固定容量页缓存。"""

from __future__ import annotations

from collections import OrderedDict, deque
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Iterator

from yoursql.common.errors import StorageError
from yoursql.common.trace import current_trace
from yoursql.storage.disk import DiskManager
from yoursql.storage.page import Page, PageType


_REPLACEMENT_POLICIES = frozenset({"lru", "fifo", "2q"})
_PROTECTED_PAGE_TYPES = frozenset(
    {PageType.SUPERBLOCK, PageType.CATALOG, PageType.INDEX}
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

    def __getitem__(self, key: str) -> int | float:
        """兼容旧的调试调用方；新代码优先使用属性。"""

        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
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

    ``2q`` 是一个专注于教学演示的两队列策略：首次访问进入冷队列，
    第二次访问晋升热队列。它能避免一次性顺序扫描污染热点页。
    """

    def __init__(
        self,
        disk: DiskManager,
        capacity: int = 64,
        replacement_policy: str = "lru",
        *,
        protect_page_types: bool = False,
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
        return min(target_capacity, max(1, target_capacity // 2))

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
            self._cold_queue.pop(page_id, None)
            self._remove_type_queue_locked(page_id, frame)
            self._hot_queue[page_id] = None
            frame.queue = "am"
            self._add_type_queue_locked(page_id, frame)
            self._promotions += 1
        else:
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

    def _ordered_page_ids_locked(self) -> Iterator[int]:
        """按当前策略产出从老到新的页号，不创建中间候选列表。"""

        if self.replacement_policy == "2q":
            yield from self._cold_queue
            yield from self._hot_queue
            return
        yield from self._frames

    def _regular_page_ids_locked(self) -> Iterator[int]:
        """按当前策略产出普通页，供页面类型保护的快速淘汰路径使用。"""

        if self.replacement_policy == "2q":
            yield from self._regular_cold_queue
            yield from self._regular_hot_queue
            return
        yield from self._regular_queue

    def _protected_page_ids_locked(self) -> Iterator[int]:
        """按当前策略产出受保护页，供检查接口展示完整淘汰顺序。"""

        if self.replacement_policy == "2q":
            yield from self._protected_cold_queue
            yield from self._protected_hot_queue
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

    def _evict_one(
        self,
        *,
        reason: str = "capacity",
    ) -> None:
        """淘汰队首第一个未 pin 的页。

        WHY：原实现每次淘汰都构建候选列表并取 min，复杂度 O(容量)；全表扫描时几乎每页
        都触发一次淘汰，缓冲池越大反而越慢。现在按维护好的优先级顺序取首项，
        选中的页与原来一致（时钟单调递增，不会出现同优先级）。
        """

        victim: tuple[int, BufferFrame] | None = None
        protected_skips = 0
        if self.protect_page_types:
            over_protected_limit = self._protected_page_count > self._protected_page_limit()
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
        if victim is None:
            raise StorageError("缓存已满且所有页都被 pin")
        page_id, frame = victim
        writeback = frame.dirty
        if frame.dirty:
            self.disk.write(frame.page)
            self._writebacks += 1
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
        """按页号读取缓存页。"""
        normalized = int(page_id)
        with self._lock:
            frame = self._frames.get(normalized)
            trace = current_trace.get()
            if trace is not None:
                trace.event("get_page", normalized, cache_hit=frame is not None)
            if frame is not None:
                self._hits += 1
                self._touch_page(normalized, frame)
                if pin:
                    frame.pin_count += 1
                return frame.page
            self._misses += 1
            page = self.disk.read(normalized)
            if len(self._frames) >= self.capacity:
                self._evict_one()
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
            if frame is None:
                if len(self._frames) >= self.capacity:
                    self._evict_one()
                self._clock += 1
                frame = BufferFrame(page, 0, dirty, self._clock, self._clock)
                self._frames[page.page_id] = frame
                self._queue_page_locked(page.page_id, frame)
            else:
                frame.page = page
                frame.dirty = frame.dirty or dirty
                self._touch_page(page.page_id, frame)
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
                self.disk.write(frame.page)
                frame.dirty = False

    def flush_all(self) -> None:
        """将全部脏页刷新到磁盘。"""
        with self._lock:
            for page_id in tuple(self._frames):
                self.flush_page(page_id)
            self.disk.sync()

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
        """复制缓存最新页或只读磁盘页，保持缓存与 I/O 指标不变。"""
        with self._lock:
            frame = self._frames.get(page_id)
            if frame is not None:
                page = frame.page
                return Page(page.page_id, page.page_size, page.page_type, page.payload)
            return self.disk.peek(page_id)

    def __contains__(self, page_id: object) -> bool:
        """判断指定元素是否存在于对象中。"""
        return isinstance(page_id, int) and page_id in self._frames
