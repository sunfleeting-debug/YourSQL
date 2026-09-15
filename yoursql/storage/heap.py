"""基于槽式页的变长记录堆表。"""
#将记录写入槽式页的空槽位，扫描更新删除记录
from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from yoursql.common.codec import PayloadCodec, PayloadCodecError, decode_payload
from yoursql.common.codec import payload_codec as get_payload_codec
from yoursql.common.errors import StorageError
from yoursql.common.types import PageId, RowId
from yoursql.storage.buffer import BufferPool
from yoursql.storage.page import (
    Page,
    PageType,
    SLOTTED_HEADER_SIZE,
    SLOT_ENTRY_SIZE,
    SlottedPage,
)


@dataclass(frozen=True)
class HeapRecord:
    """堆表扫描得到的行定位和行值。"""

    row_id: RowId
    row: tuple[object, ...]

    def __iter__(self):
        """兼容旧的 ``for row_id, row in heap.scan()`` 调用。"""

        yield self.row_id
        yield self.row


class TableHeap:
    """管理一张表的页链；页号列表由 Catalog 持久化。"""

    def __init__(
        self, buffer_pool: BufferPool, page_ids: list[int] | None = None
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        self.buffer_pool = buffer_pool
        self.page_ids: list[int] = [int(page_id) for page_id in (page_ids or [])]

    @property
    def page_size(self) -> int:
        """页大小跟随磁盘格式（打开已有库时可能是 512B–128KB 中的任意 2 的幂）。"""

        return self.buffer_pool.disk.page_size

    @staticmethod
    # HOW：字段元组交给当前数据库选择的 payload 编解码器，供槽式页保存。
    def _encode(
        row: tuple[object, ...], codec: PayloadCodec | str = "json"
    ) -> bytes:
        """将内部数据编码为存储字节串。"""
        selected = codec if isinstance(codec, PayloadCodec) else get_payload_codec(codec)
        return selected.encode(list(row))

    @staticmethod
    def _decode(
        raw: bytes, codec: PayloadCodec | str | None = None
    ) -> tuple[object, ...]:
        """将存储字节串解码为内部数据。"""
        try:
            value, _selected = decode_payload(raw, codec)
        except (PayloadCodecError, TypeError, ValueError) as exc:
            raise StorageError("记录 payload 损坏") from exc
        if not isinstance(value, list):
            raise StorageError("记录不是数组")
        return tuple(value)

    @property
    def payload_codec(self) -> PayloadCodec:
        """返回当前数据库文件选择的 payload 编解码器。"""

        return self.buffer_pool.disk.payload_codec

    def _read_slotted(self, page_id: int) -> SlottedPage:
        """读取页并解析为槽式页。"""
        page = self.buffer_pool.get_page(page_id, pin=True)
        try:
            return SlottedPage.from_page(page)
        finally:
            self.buffer_pool.unpin(page_id)

    # HOW: 修改后的页面放回缓冲池并标脏，标脏不等于已经落盘。
    def _write_slotted(self, slotted: SlottedPage) -> None:
        """将槽式页序列化后写回缓存。"""
        # WHY：SlottedPage 是页内操作的临时视图，必须重新生成 Page 并标记 dirty，
        # BufferPool 才能保留最新内容，并在淘汰或刷盘时写回 DiskManager。
        self.buffer_pool.put_page(slotted.to_page(), dirty=True)

    # HOW: 输入已校验的字段值，返回页号和槽号组成的 RowId，与用户 id 列不同。
    def insert(self, row: tuple[object, ...]) -> RowId:
        """向堆表写入一行并返回其 RowId。"""
        encoded = self._encode(row, self.payload_codec)
        # WHY：TableHeap 只负责选择页、写入记录并分配 RowId；索引需要表结构和索引元数据，
        # 因此由 runtime/commands.py 在此方法返回后统一建立索引入口。
        # HOW：新记录优先尝试尾页，避免大表插入时逐行扫描所有已满页。
        # WHY：TPC-H lineitem 这类批量导入会把 O(行数 × 页数) 放大到不可接受。
        for page_id in reversed(self.page_ids):
            slotted = self._read_slotted(page_id)
            try:
                slot_id = slotted.insert(encoded)
            except StorageError:
                # WHY：当前页无法完成这次页内插入时，继续尝试其他已有页；若所有页都失败，
                # 下面才扩展页链，由新页插入最终决定是否向上抛出异常。
                continue
            self._write_slotted(slotted)
            return RowId(PageId(page_id), slot_id)
        # WHY：只有所有已有页都无法容纳记录时才创建新页，避免无谓扩展页链和目录元数据。
        page = self.buffer_pool.new_page(PageType.HEAP)
        slotted = SlottedPage.from_page(page)
        slot_id = slotted.insert(encoded)
        self._write_slotted(slotted)
        # WHY：记录已经成功写入缓存后才登记新页，避免失败流程把未完成页暴露给扫描。
        self.page_ids.append(page.page_id)
        return RowId(PageId(page.page_id), slot_id)

    def append_batch(self, rows: Iterable[tuple[object, ...]]) -> list[RowId]:
        """批量追加记录：同一页写满才序列化一次。

        WHY：`insert` 每行都要整页解码 + 整页重编码（实测 60,175 行 51 s 的主因），
        同一页会被反复重写数百次；批量导入时改为累积记录、写满一页才落盘。
        返回值与行一一对应，调用方仍需按序维护索引。
        """

        row_ids: list[RowId] = []
        capacity = self.page_size - Page.HEADER_SIZE
        pending: list[bytes | None] = []
        used = SLOTTED_HEADER_SIZE
        page_id: int | None = None
        free_slots: list[int] = []
        if self.page_ids:
            # HOW：先尝试接着最后一页写，与 insert 的“优先尾页”行为一致。
            page_id = int(self.page_ids[-1])
            existing = list(self._read_slotted(page_id).slots)
            pending = list(existing)
            free_slots = [
                index for index, record in enumerate(existing) if record is None
            ]
            used = (
                SLOTTED_HEADER_SIZE
                + len(existing) * SLOT_ENTRY_SIZE
                + sum(len(record) for record in existing if record is not None)
            )

        def flush() -> None:
            """将暂存数据刷新到下一级存储。"""
            nonlocal pending, used, free_slots
            if page_id is None or not pending:
                return
            self._write_slotted(SlottedPage(page_id, self.page_size, list(pending)))
            pending = []
            free_slots = []
            used = SLOTTED_HEADER_SIZE

        for row in rows:
            encoded = self._encode(row, self.payload_codec)
            entry = len(encoded) + SLOT_ENTRY_SIZE
            if page_id is None or used + entry > capacity:
                flush()
                page = self.buffer_pool.new_page(PageType.HEAP)
                page_id = int(page.page_id)
                self.page_ids.append(page_id)
            if free_slots:
                slot_id = free_slots.pop(0)
                pending[slot_id] = encoded
            else:
                slot_id = len(pending)
                pending.append(encoded)
            used += entry
            row_ids.append(RowId(PageId(page_id), slot_id))
        flush()
        return row_ids

    def read(self, row_id: RowId) -> tuple[object, ...] | None:
        """读取数据并按调用方要求返回。"""
        page_id = int(row_id.page_id)
        if page_id not in self.page_ids:
            return None
        slotted = self._read_slotted(page_id)
        raw = slotted.get(row_id.slot_id)
        return None if raw is None else self._decode(raw, self.payload_codec)

    def update(self, row_id: RowId, row: tuple[object, ...]) -> None:
        """更新堆表中指定 RowId 的行。"""
        page_id = int(row_id.page_id)
        if page_id not in self.page_ids:
            raise StorageError("RowId 不属于当前堆表")
        # WHY：堆表只更新物理记录；若更新了索引列，调用方必须先删除旧索引入口，
        # 再按新行值插入入口，避免索引继续指向旧键或旧的覆盖索引 payload。
        slotted = self._read_slotted(page_id)
        slotted.update(row_id.slot_id, self._encode(row, self.payload_codec))
        self._write_slotted(slotted)

    # HOW: 按物理地址清除槽位，保留表及页列表；索引维护由上层负责。
    def delete(self, row_id: RowId) -> bool:
        """删除指定数据并维护相关状态。"""
        page_id = int(row_id.page_id)
        if page_id not in self.page_ids:
            return False
        slotted = self._read_slotted(page_id)
        if slotted.get(row_id.slot_id) is None:
            return False
        slotted.delete(row_id.slot_id)
        self._write_slotted(slotted)
        return True

    def reclaim_empty_pages(self, page_ids: Iterable[int]) -> tuple[int, ...]:
        """回收指定范围内已没有有效记录的 HEAP 页。"""

        known_page_ids = set(self.page_ids)
        candidates: list[int] = []
        for page_id in dict.fromkeys(int(value) for value in page_ids):
            if page_id not in known_page_ids:
                continue
            slotted = self._read_slotted(page_id)
            if not slotted.live_slots():
                candidates.append(page_id)
        if not candidates:
            return ()

        # WHY：先由 BufferPool 批量写入 FREE 页，再更新表页列表，避免表目录先丢失
        # 页面而磁盘回收失败时出现悬空或不可追踪的物理页。
        self.buffer_pool.delete_pages(candidates)
        reclaimed = set(candidates)
        self.page_ids[:] = [page_id for page_id in self.page_ids if page_id not in reclaimed]
        return tuple(candidates)

    # HOW: 按页号列表遍历有效槽位，逐条产生记录地址和解码后的字段值。
    def scan(self) -> Iterator[HeapRecord]:
        """按页和槽顺序扫描输入中的有效记录。"""
        for page_id in tuple(self.page_ids):
            slotted = self._read_slotted(page_id)
            for live_slot in slotted.live_slots():
                yield HeapRecord(
                    RowId(PageId(page_id), live_slot.slot_id),
                    self._decode(live_slot.raw, self.payload_codec),
                )

    def count(self) -> int:
        """统计活记录数；不逐行解码记录内容。

        WHY：``SELECT COUNT(*) FROM t`` 不需要任何列值，早先复用 ``scan()``
        会对每行做一次 JSON 解码与 Decimal 还原。按页槽目录统计后，60,175 行
        lineitem 的纯计数降到 50 ms 量级（端到端 ``SELECT COUNT(*)`` 实测
        571 ms → 66 ms，口径见 ``benchmarks/bench_scan_paths.py``）。
        """

        return sum(
            self._read_slotted(page_id).live_count()
            for page_id in tuple(self.page_ids)
        )

    def close(self) -> None:
        """关闭资源并释放关联状态。"""
        self.buffer_pool.flush_all()
