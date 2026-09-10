"""阶段 5：缓冲区管理（页缓存 + 替换策略）。

对外接口（计划书要求）：
    get_page(page_id)  -> 命中缓存或磁盘，返回被 pin 住的页
    unpin_page(page_id, is_dirty)
    new_page()         -> 分配新页（已 pin）
    flush_page(page_id) / flush_all()
统计：命中率、磁盘读写次数、页面替换次数
日志：每次替换输出一条记录（替换了谁、为什么）
策略：LRU（最近最少使用）与 FIFO（先进先出）可切换
"""

from __future__ import annotations

from collections import OrderedDict

from database_system.storage.page import Page
from database_system.utils.constants import INVALID_PAGE_ID, PAGE_SIZE, PageType, ReplacePolicy
from database_system.utils.errors import StorageError


class BoundedLog(list):
    """兼容 list API 的有界日志，避免长期运行时无界增长。"""

    def __init__(self, maxlen: int = 500):
        super().__init__()
        self.maxlen = maxlen

    def append(self, item) -> None:
        super().append(item)
        if len(self) > self.maxlen:
            del self[: len(self) - self.maxlen]


# ============================ 替换器 ============================


class Replacer:
    """替换器基类：给出「淘汰候选页」的有序列表。

    候选列表只是顺序建议，真正的淘汰由 BufferPoolManager 执行，
    它会跳过 pin_count > 0 的页。
    """

    def pin(self, page_id: int) -> None:
        """页被 pin 住时的通知。"""

    def unpin(self, page_id: int) -> None:
        """页变为可替换（pin_count == 0）时的通知。"""
        raise NotImplementedError

    def candidates(self) -> list:
        """按「优先淘汰」顺序返回候选页号。"""
        raise NotImplementedError

    def remove(self, page_id: int) -> None:
        """页离开缓冲区。"""
        raise NotImplementedError


class LRUReplacer(Replacer):
    """LRU：每次变为可替换时放到队尾，队首即最久未使用的页。"""

    def __init__(self):
        self._order: "OrderedDict[int, None]" = OrderedDict()

    def pin(self, page_id: int) -> None:
        self._order.pop(page_id, None)

    def unpin(self, page_id: int) -> None:
        # 先删后插 == move_to_end
        self._order.pop(page_id, None)
        self._order[page_id] = None

    def candidates(self) -> list:
        return list(self._order)

    def remove(self, page_id: int) -> None:
        self._order.pop(page_id, None)

    def __str__(self) -> str:
        return f"LRU[{list(self._order)}]"


class FIFOReplacer(Replacer):
    """FIFO：按「最早进入缓冲区」淘汰；命中（重新 pin）不改变先后顺序。"""

    def __init__(self):
        self._seq: dict = {}
        self._counter = 0

    def pin(self, page_id: int) -> None:
        pass  # 命中不影响 FIFO 顺序

    def unpin(self, page_id: int) -> None:
        if page_id not in self._seq:
            self._seq[page_id] = self._counter
            self._counter += 1

    def candidates(self) -> list:
        return sorted(self._seq, key=lambda p: self._seq[p])

    def remove(self, page_id: int) -> None:
        self._seq.pop(page_id, None)

    def __str__(self) -> str:
        return f"FIFO[{self.candidates()}]"


# ============================ 缓冲池 ============================


class BufferStats:
    def __init__(self):
        self.hits = 0
        self.misses = 0
        self.evictions = 0
        self.disk_reads = 0
        self.disk_writes = 0

    @property
    def accesses(self) -> int:
        return self.hits + self.misses

    @property
    def hit_rate(self) -> float:
        return self.hits / self.accesses if self.accesses else 0.0

    def to_dict(self) -> dict:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "accesses": self.accesses,
            "hit_rate": round(self.hit_rate, 4),
            "evictions": self.evictions,
            "disk_reads": self.disk_reads,
            "disk_writes": self.disk_writes,
        }

    def __str__(self) -> str:
        d = self.to_dict()
        return (
            f"hits={d['hits']} misses={d['misses']} hit_rate={d['hit_rate']:.2%} "
            f"evictions={d['evictions']} reads={d['disk_reads']} writes={d['disk_writes']}"
        )


class BufferPoolManager:
    """页缓存：固定帧数、可切换替换策略。"""

    def __init__(self, disk_manager, pool_size: int = 16,
                 policy: str = ReplacePolicy.LRU, verbose: bool = False):
        if pool_size < 1:
            raise StorageError("pool_size must be >= 1")
        self.disk = disk_manager
        self.pool_size = pool_size
        self.policy = (policy or ReplacePolicy.LRU).upper()
        self.frames: dict = {}
        self.stats = BufferStats()
        self.log: BoundedLog = BoundedLog()
        self.verbose = verbose
        self.replacer = LRUReplacer() if self.policy == ReplacePolicy.LRU else FIFOReplacer()
        if self.policy not in (ReplacePolicy.LRU, ReplacePolicy.FIFO):
            raise StorageError(f"unknown replace policy: {policy}")

    # ------------------------------ 核心接口 ------------------------------

    def fetch_page(self, page_id: int) -> Page:
        """取页（pin_count += 1）。"""
        page = self.frames.get(page_id)
        if page is not None:
            self.stats.hits += 1
            page.pin_count += 1
            self.replacer.pin(page_id)  # LRU 会把它移出候选集，unpin 时再回到队尾
            self._emit(f"HIT  page {page_id} (pin={page.pin_count})")
            return page

        self.stats.misses += 1
        # 先读取并校验目标页，再淘汰已有帧。这样无效 page_id 不会
        # 破坏当前缓存内容，也不会产生虚假的淘汰记录。
        data = self.disk.read_page(page_id)
        self.stats.disk_reads += 1
        page = Page.decode(data, page_id=page_id)
        if len(self.frames) >= self.pool_size:
            self._evict()
        page.pin_count = 1
        self.frames[page_id] = page
        self.replacer.pin(page_id)
        self._emit(f"MISS page {page_id} -> load from disk, pin=1")
        return page

    def set_policy(self, policy: str) -> None:
        """运行时切换替换策略（LRU <-> FIFO）。"""
        policy = (policy or ReplacePolicy.LRU).upper()
        if policy not in (ReplacePolicy.LRU, ReplacePolicy.FIFO):
            raise StorageError(f"unknown replace policy: {policy}")
        self.policy = policy
        self.replacer = LRUReplacer() if policy == ReplacePolicy.LRU else FIFOReplacer()
        # 当前所有 pin_count == 0 的页重新进入候选集合
        for pid, page in self.frames.items():
            if page.pin_count == 0:
                self.replacer.unpin(pid)
        self._emit(f"POLICY -> {policy}")

    # 计划书中要求的 get_page 命名
    get_page = fetch_page

    def unpin_page(self, page_id: int, is_dirty: bool = False) -> None:
        page = self.frames.get(page_id)
        if page is None:
            raise StorageError(f"page {page_id} is not in buffer pool")
        if page.pin_count <= 0:
            raise StorageError(f"page {page_id} is already unpinned")
        page.pin_count -= 1
        if is_dirty:
            page.dirty = True
        if page.pin_count == 0:
            self.replacer.unpin(page_id)
        self._emit(f"UNPIN page {page_id} (pin={page.pin_count}, dirty={page.dirty})")

    def new_page(self, page_type: int = PageType.DATA) -> int:
        """分配并返回一个新页号。

        注意：返回的页已在缓冲区中且 pin_count = 1（与 BusTub 一致）。
        调用方若只是想「占个页号」，应随后再 unpin 一次以归还引用。
        """
        if len(self.frames) >= self.pool_size:
            self._evict()
        page_id = self.disk.allocate_page()
        page = Page(page_id)
        page.init(page_type, INVALID_PAGE_ID)
        page.pin_count = 1
        page.dirty = True
        self.frames[page_id] = page
        self.replacer.pin(page_id)
        self._emit(f"NEW  page {page_id} (pin=1)")
        return page_id

    def new_page_unpinned(self, page_type: int = PageType.DATA) -> int:
        """分配新页并立即归还 pin 引用。

        正常情况下新页会进入缓冲池。若缓冲池容量为 1，且当前唯一页面仍
        pinned（典型场景是表页扩展），无法先把新页放入缓存；此时直接把已
        初始化的新页写入磁盘，调用方稍后通过 fetch_page 再加载它。这样不
        破坏 pin 语义，也让单帧缓冲池能够支持多页表。
        """
        if len(self.frames) < self.pool_size:
            page_id = self.new_page(page_type)
            self.unpin_page(page_id, True)
            return page_id

        try:
            self._evict()
        except StorageError:
            if not self.frames or not all(page.pin_count > 0 for page in self.frames.values()):
                raise
            page_id = self.disk.allocate_page()
            page = Page(page_id)
            page.init(page_type, INVALID_PAGE_ID)
            self.disk.write_page(page_id, page.encode())
            self.stats.disk_writes += 1
            self._emit(f"NEW  page {page_id} (direct-to-disk, pool is pinned)")
            return page_id

        page_id = self.new_page(page_type)
        self.unpin_page(page_id, True)
        return page_id

    def flush_page(self, page_id: int) -> bool:
        page = self.frames.get(page_id)
        if page is None:
            return False
        if page.dirty:
            self._write_back(page)
            self._emit(f"FLUSH page {page_id} -> disk")
        return True

    def flush_all(self) -> int:
        count = 0
        for page_id in list(self.frames):
            if self.frames[page_id].dirty:
                self.flush_page(page_id)
                count += 1
        return count

    def delete_page(self, page_id: int) -> None:
        """从缓冲区移除并归还给磁盘空间管理。"""
        # 先检查所有前置条件，再修改 frames/replacer，避免失败操作破坏状态。
        self.disk.validate_page(page_id)
        page = self.frames.get(page_id)
        if page is not None and page.pin_count > 0:
            raise StorageError(f"cannot delete pinned page {page_id}")

        # 先完成磁盘回收，再修改内存索引。若磁盘操作失败，缓冲池仍保持可用。
        self.disk.deallocate_page(page_id)
        if page is not None:
            self.frames.pop(page_id)
        self.replacer.remove(page_id)
        self._emit(f"FREE page {page_id} -> returned to free list")

    # ------------------------------ 内部 ------------------------------

    def _evict(self) -> None:
        victim = None
        for candidate in self.replacer.candidates():
            page = self.frames.get(candidate)
            if page is not None and page.pin_count == 0:
                victim = candidate
                break
        if victim is None:
            raise StorageError("buffer pool is full: all frames are pinned")
        page = self.frames[victim]
        reason = "dirty, write back" if page.dirty else "clean, discard"
        if page.dirty:
            # 写回失败时保留 frame 和 replacer 状态，下一次仍可重试。
            self._write_back(page)
        self.replacer.remove(victim)
        self.frames.pop(victim)
        self.stats.evictions += 1
        self._emit(f"EVICT page {victim} ({self.policy}, {reason})")

    def _write_back(self, page: Page) -> None:
        """统一执行页校验、写盘、dirty 清除和写盘统计。"""
        self.disk.write_page(page.page_id, page.encode())
        page.dirty = False
        self.stats.disk_writes += 1

    def _emit(self, message: str) -> None:
        self.log.append(message)
        if self.verbose:
            print(f"[buffer] {message}")

    # ------------------------------ 状态 ------------------------------

    def pinned_pages(self) -> list:
        return [pid for pid, p in self.frames.items() if p.pin_count > 0]

    def __len__(self) -> int:
        return len(self.frames)

    def __str__(self) -> str:
        return (
            f"BufferPool(size={len(self.frames)}/{self.pool_size}, "
            f"policy={self.policy}, stats={self.stats})"
        )
