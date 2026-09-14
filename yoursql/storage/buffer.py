"""支持 LRU/FIFO 的固定容量页缓存。"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Iterator

from yoursql.common.errors import StorageError
from yoursql.common.trace import current_trace
from yoursql.storage.disk import DiskManager
from yoursql.storage.page import Page, PageType


@dataclass
class BufferFrame:
    page: Page
    pin_count: int = 0
    dirty: bool = False
    loaded_order: int = 0
    last_used: int = 0


@dataclass(frozen=True)
class BufferPoolStats:
    """BufferPool 的累计命中统计。"""

    capacity: int
    size: int
    hits: int
    misses: int
    evictions: int
    hit_rate: float

    def __getitem__(self, key: str) -> int | float:
        """兼容旧的调试调用方；新代码优先使用属性。"""

        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        """返回对象的迭代器。"""
        return iter(("capacity", "size", "hits", "misses", "evictions", "hit_rate"))

    def to_dict(self) -> dict[str, int | float]:
        """将对象转换为可序列化的字典。"""
        return {
            "capacity": self.capacity,
            "size": self.size,
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "hit_rate": self.hit_rate,
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

    def to_dict(self) -> dict[str, int | str | bool]:
        """将对象转换为可序列化的字典。"""
        return {
            "page_id": self.page_id,
            "type": self.page_type,
            "pin_count": self.pin_count,
            "dirty": self.dirty,
            "loaded_order": self.loaded_order,
            "last_used": self.last_used,
        }


@dataclass(frozen=True)
class BufferPoolSnapshot:
    """【前端特供】BufferPool 检查快照；不包含页 payload。"""

    stats: BufferPoolStats
    policy: str
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
    """缓存磁盘页并记录命中、缺页和淘汰统计。"""

    def __init__(
        self, disk: DiskManager, capacity: int = 64, replacement_policy: str = "lru"
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        if capacity < 1:
            raise ValueError("缓存容量必须为正数")
        policy = replacement_policy.lower()
        if policy not in {"lru", "fifo"}:
            raise ValueError("replacement_policy 只能是 lru 或 fifo")
        self.disk = disk
        self.capacity = capacity
        self.replacement_policy = policy
        # HOW：`_frames` 自身按淘汰优先级排序（队首最先淘汰），使淘汰 O(1) 摊还。
        self._frames: OrderedDict[int, BufferFrame] = OrderedDict()
        self._clock = 0
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._revision = 0
        self._change_log: deque[tuple[int, int]] = deque(maxlen=4096)
        self._event_log: deque[dict[str, object]] = deque(maxlen=4096)
        self._lock = RLock()

    @property
    def size(self) -> int:
        """返回对象占用或包含的大小。"""
        return len(self._frames)

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
        )

    statistics = stats

    def set_replacement_policy(self, replacement_policy: str) -> bool:
        """切换后续淘汰使用的策略，返回策略是否发生变化。"""

        policy = replacement_policy.lower()
        if policy not in {"lru", "fifo"}:
            raise ValueError("replacement_policy 只能是 lru 或 fifo")
        with self._lock:
            changed = self.replacement_policy != policy
            self.replacement_policy = policy
            return changed

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
            for _ in range(required):
                self._evict_one(reason="resize")
            self.capacity = capacity
            return required

    def _touch(self, frame: BufferFrame) -> None:
        """更新缓存页的访问顺序和相关统计。"""
        self._clock += 1
        frame.last_used = self._clock

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

        candidates = [
            (page_id, frame)
            for page_id, frame in self._frames.items()
            if frame.pin_count == 0
        ]
        if self.replacement_policy == "fifo":
            candidates.sort(key=lambda item: (item[1].loaded_order, item[0]))
        else:
            candidates.sort(key=lambda item: (item[1].last_used, item[0]))
        return [page_id for page_id, _ in candidates]

    def _evict_one(self, *, reason: str = "capacity") -> None:
        """淘汰队首第一个未 pin 的页。

        WHY：原实现每次淘汰都构建候选列表并取 min，复杂度 O(容量)；全表扫描时几乎每页
        都触发一次淘汰，缓冲池越大反而越慢。现在按维护好的优先级顺序取首项，
        选中的页与原来一致（时钟单调递增，不会出现同优先级）。
        """

        victim: tuple[int, BufferFrame] | None = None
        for page_id, frame in self._frames.items():
            if not frame.pin_count:
                victim = (page_id, frame)
                break
        if victim is None:
            raise StorageError("缓存已满且所有页都被 pin")
        page_id, frame = victim
        writeback = frame.dirty
        if frame.dirty:
            self.disk.write(frame.page)
        del self._frames[page_id]
        self._evictions += 1
        self._record_event(
            "evict",
            page_id,
            dirty=writeback,
            writeback=writeback,
            policy=self.replacement_policy,
            reason=reason,
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
                self._touch(frame)
                # LRU 命中要把该页移到队尾（最后淘汰）；FIFO 保持装载顺序不变。
                if self.replacement_policy == "lru":
                    self._frames.move_to_end(normalized)
                if pin:
                    frame.pin_count += 1
                return frame.page
            self._misses += 1
            if len(self._frames) >= self.capacity:
                self._evict_one()
            self._clock += 1
            page = self.disk.read(normalized)
            self._frames[normalized] = BufferFrame(
                page, 1 if pin else 0, False, self._clock, self._clock
            )
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
            else:
                frame.page = page
                frame.dirty = frame.dirty or dirty
                self._touch(frame)
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
        with self._lock:
            frame = self._frames.get(int(page_id))
            if frame is not None and frame.pin_count:
                raise StorageError(f"页 {page_id} 仍被 pin")
            self._frames.pop(int(page_id), None)
            self.disk.free(int(page_id))
            self._mark_changed(int(page_id))

    def close(self) -> None:
        """关闭资源并释放关联状态。"""
        self.flush_all()
        self._frames.clear()

    def snapshot(self, offset: int = 0, limit: int = 100) -> BufferPoolSnapshot:
        """【前端特供】只复制有限帧头，不 pin、不淘汰、不刷新或推进替换时钟。"""
        from itertools import islice

        with self._lock:
            return BufferPoolSnapshot(
                stats=self.stats(),
                policy=self.replacement_policy,
                frames=tuple(
                    BufferFrameSnapshot(
                        page_id=page_id,
                        page_type=frame.page.page_type.value,
                        pin_count=frame.pin_count,
                        dirty=frame.dirty,
                        loaded_order=frame.loaded_order,
                        last_used=frame.last_used,
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
