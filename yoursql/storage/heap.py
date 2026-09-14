"""基于槽式页的变长记录堆表。"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator

from ..common.errors import StorageError
from ..common.types import PageId, RowId
from .buffer import BufferPool
from .page import (
    Page,
    PageType,
    SLOTTED_HEADER_SIZE,
    SLOT_ENTRY_SIZE,
    SlottedPage,
)


class TableHeap:
    """管理一张表的页链；页号列表由 Catalog 持久化。"""

    def __init__(
        self, buffer_pool: BufferPool, page_ids: list[int] | None = None
    ) -> None:
        self.buffer_pool = buffer_pool
        self.page_ids: list[int] = [int(page_id) for page_id in (page_ids or [])]

    @property
    def page_size(self) -> int:
        """页大小跟随磁盘格式（打开已有库时可能是 512B–128KB 中的任意 2 的幂）。"""

        return self.buffer_pool.disk.page_size

    @staticmethod
    def _encode(row: tuple[object, ...]) -> bytes:
        return json.dumps(list(row), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )

    @staticmethod
    def _decode(raw: bytes) -> tuple[object, ...]:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise StorageError("记录 JSON 损坏") from exc
        if not isinstance(value, list):
            raise StorageError("记录不是数组")
        return tuple(value)

    def _read_slotted(self, page_id: int) -> SlottedPage:
        page = self.buffer_pool.get_page(page_id, pin=True)
        try:
            return SlottedPage.from_page(page)
        finally:
            self.buffer_pool.unpin(page_id)

    def _write_slotted(self, slotted: SlottedPage) -> None:
        self.buffer_pool.put_page(slotted.to_page(), dirty=True)

    def insert(self, row: tuple[object, ...]) -> RowId:
        encoded = self._encode(row)
        # HOW：新记录优先尝试尾页，避免大表插入时逐行扫描所有已满页。
        # WHY：TPC-H lineitem 这类批量导入会把 O(行数 × 页数) 放大到不可接受。
        for page_id in reversed(self.page_ids):
            slotted = self._read_slotted(page_id)
            try:
                slot_id = slotted.insert(encoded)
            except StorageError:
                continue
            self._write_slotted(slotted)
            return RowId(PageId(page_id), slot_id)
        page = self.buffer_pool.new_page(PageType.HEAP)
        slotted = SlottedPage.from_page(page)
        slot_id = slotted.insert(encoded)
        self._write_slotted(slotted)
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
            nonlocal pending, used, free_slots
            if page_id is None or not pending:
                return
            self._write_slotted(SlottedPage(page_id, self.page_size, list(pending)))
            pending = []
            free_slots = []
            used = SLOTTED_HEADER_SIZE

        for row in rows:
            encoded = self._encode(row)
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
        page_id = int(row_id.page_id)
        if page_id not in self.page_ids:
            return None
        slotted = self._read_slotted(page_id)
        raw = slotted.get(row_id.slot_id)
        return None if raw is None else self._decode(raw)

    def update(self, row_id: RowId, row: tuple[object, ...]) -> None:
        page_id = int(row_id.page_id)
        if page_id not in self.page_ids:
            raise StorageError("RowId 不属于当前堆表")
        slotted = self._read_slotted(page_id)
        slotted.update(row_id.slot_id, self._encode(row))
        self._write_slotted(slotted)

    def delete(self, row_id: RowId) -> bool:
        page_id = int(row_id.page_id)
        if page_id not in self.page_ids:
            return False
        slotted = self._read_slotted(page_id)
        if slotted.get(row_id.slot_id) is None:
            return False
        slotted.delete(row_id.slot_id)
        self._write_slotted(slotted)
        return True

    def scan(self) -> Iterator[tuple[RowId, tuple[object, ...]]]:
        for page_id in tuple(self.page_ids):
            slotted = self._read_slotted(page_id)
            for slot_id, raw in slotted.live_slots():
                yield RowId(PageId(page_id), slot_id), self._decode(raw)

    def count(self) -> int:
        return sum(1 for _row_id, _row in self.scan())

    def close(self) -> None:
        self.buffer_pool.flush_all()
