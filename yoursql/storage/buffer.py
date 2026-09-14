"""支持 LRU/FIFO 的固定容量页缓存。"""

from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass
from threading import RLock

from ..common.errors import StorageError
from ..common.trace import current_trace
from .disk import DiskManager
from .page import Page, PageType


@dataclass
class BufferFrame:
    page: Page
    pin_count: int = 0
    dirty: bool = False
    loaded_order: int = 0
    last_used: int = 0


class BufferPool:
    """缓存磁盘页并记录命中、缺页和淘汰统计。"""

    def __init__(
        self, disk: DiskManager, capacity: int = 64, replacement_policy: str = "lru"
    ) -> None:
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
        self._lock = RLock()

    @property
    def size(self) -> int:
        return len(self._frames)

    def stats(self) -> dict[str, int | float]:
        total = self._hits + self._misses
        return {
            "capacity": self.capacity,
            "size": len(self._frames),
            "hits": self._hits,
            "misses": self._misses,
            "evictions": self._evictions,
            "hit_rate": self._hits / total if total else 0.0,
        }

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

    def _touch(self, frame: BufferFrame) -> None:
        self._clock += 1
        frame.last_used = self._clock

    def _mark_changed(self, page_id: int) -> None:
        """记录页内容/生命周期变化，供只读检查页级增量刷新。"""

        self._revision += 1
        self._change_log.append((self._revision, int(page_id)))

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

    def _evict_one(self) -> None:
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
        if frame.dirty:
            self.disk.write(frame.page)
        del self._frames[page_id]
        self._evictions += 1

    def get_page(self, page_id: int, pin: bool = True) -> Page:
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
        return self.get_page(page_id, pin=True)

    def unpin(self, page_id: int, dirty: bool = False) -> None:
        with self._lock:
            frame = self._frames.get(int(page_id))
            if frame is None:
                raise StorageError(f"页 {page_id} 不在缓存中")
            if frame.pin_count > 0:
                frame.pin_count -= 1
            frame.dirty = frame.dirty or dirty

    def mark_dirty(self, page_id: int) -> None:
        with self._lock:
            frame = self._frames.get(int(page_id))
            if frame is None:
                raise StorageError(f"页 {page_id} 不在缓存中")
            frame.dirty = True

    def flush_page(self, page_id: int) -> None:
        with self._lock:
            frame = self._frames.get(int(page_id))
            if frame is None:
                return
            if frame.dirty:
                self.disk.write(frame.page)
                frame.dirty = False

    def flush_all(self) -> None:
        with self._lock:
            for page_id in tuple(self._frames):
                self.flush_page(page_id)
            self.disk.sync()

    def new_page(
        self, page_type: PageType = PageType.FREE, payload: bytes = b""
    ) -> Page:
        page = self.disk.allocate(page_type, payload)
        self.put_page(page, dirty=False)
        return page

    def delete_page(self, page_id: int) -> None:
        with self._lock:
            frame = self._frames.get(int(page_id))
            if frame is not None and frame.pin_count:
                raise StorageError(f"页 {page_id} 仍被 pin")
            self._frames.pop(int(page_id), None)
            self.disk.free(int(page_id))
            self._mark_changed(int(page_id))

    def close(self) -> None:
        self.flush_all()
        self._frames.clear()

    def snapshot(self, offset: int = 0, limit: int = 100) -> dict[str, object]:
        """只复制有限帧头，不 pin、不淘汰、不刷新或推进替换时钟。"""
        from itertools import islice

        with self._lock:
            return {
                "stats": self.stats(),
                "policy": self.replacement_policy,
                "frames": [
                    {
                        "page_id": page_id,
                        "type": frame.page.page_type.value,
                        "pin_count": frame.pin_count,
                        "dirty": frame.dirty,
                        "loaded_order": frame.loaded_order,
                        "last_used": frame.last_used,
                    }
                    for page_id, frame in islice(
                        self._frames.items(), offset, offset + limit
                    )
                ],
                "eviction_order": self._eviction_order_locked(),
                "total": len(self._frames),
                "offset": offset,
                "limit": limit,
                "revision": self._revision,
            }

    def changes_since(self, revision: int) -> dict[str, object]:
        """返回指定游标之后发生变化的页号，不读取磁盘也不触碰替换状态。"""

        with self._lock:
            if revision >= self._revision:
                return {
                    "revision": self._revision,
                    "changed_page_ids": [],
                    "truncated": False,
                }
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
            return {
                "revision": self._revision,
                "changed_page_ids": page_ids,
                "truncated": truncated,
            }

    def peek_page(self, page_id: int) -> Page:
        """复制缓存最新页或只读磁盘页，保持缓存与 I/O 指标不变。"""
        with self._lock:
            frame = self._frames.get(page_id)
            if frame is not None:
                page = frame.page
                return Page(page.page_id, page.page_size, page.page_type, page.payload)
            return self.disk.peek(page_id)

    def __contains__(self, page_id: object) -> bool:
        return isinstance(page_id, int) and page_id in self._frames
