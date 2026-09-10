"""阶段 6：存储引擎。

在页式存储之上提供「表堆（TableHeap）」抽象：
  * 一张表 = 一条页链表（首页号记录在 Catalog 中，页头 next_page_id 串起来）
  * 记录序列化后写入槽位，删除时清空槽目录项（逻辑删除，槽可复用）
  * 空间不足自动申请新页并挂到链表尾部（表扩展）
  * drop_all() 归还全部页（表回收）
"""

from __future__ import annotations

from typing import Iterator, Optional

from database_system.engine.record import decode_row, encode_row
from database_system.storage.buffer import BufferPoolManager
from database_system.utils.constants import INVALID_PAGE_ID, PageType
from database_system.utils.errors import ExecutionError


class RowId:
    """记录的物理地址：(页号, 槽号)。"""

    __slots__ = ("page_id", "slot_id")

    def __init__(self, page_id: int = INVALID_PAGE_ID, slot_id: int = 0):
        self.page_id = page_id
        self.slot_id = slot_id

    def __iter__(self):
        yield self.page_id
        yield self.slot_id

    def __eq__(self, other) -> bool:
        return (isinstance(other, RowId)
                and self.page_id == other.page_id
                and self.slot_id == other.slot_id)

    def __hash__(self) -> int:
        return hash((self.page_id, self.slot_id))

    def __repr__(self) -> str:
        return f"RowId(page={self.page_id}, slot={self.slot_id})"


class TableHeap:
    """一张表的全部数据页。"""

    def __init__(self, buffer: BufferPoolManager, first_page_id: int):
        if first_page_id == INVALID_PAGE_ID:
            raise ExecutionError("table has no data page allocated")
        self.buffer = buffer
        self.first_page_id = first_page_id

    @staticmethod
    def _visit_page(page_id: int, visited: set[int]) -> None:
        """检测页链环路，避免损坏链表导致扫描无限循环。"""
        if page_id in visited:
            raise ExecutionError(f"cycle detected in table page chain at page {page_id}")
        visited.add(page_id)

    # ------------------------------ 写入 ------------------------------

    def insert_row(self, values: list) -> RowId:
        """写入一行，必要时扩展新页。"""
        data = encode_row(values)
        page_id = self.first_page_id
        visited = set()
        while True:
            self._visit_page(page_id, visited)
            page = self.buffer.fetch_page(page_id)
            pinned = True
            try:
                if page.can_insert(len(data)):
                    slot = page.insert_record(data)
                    self.buffer.unpin_page(page_id, True)
                    pinned = False
                    return RowId(page_id, slot)
                nxt = page.next_page_id
                if nxt == INVALID_PAGE_ID:
                    # 表扩展：申请新页并挂到链表尾部（new_page_unpinned 保证 pin 引用平衡）
                    new_id = self.buffer.new_page_unpinned(PageType.DATA)
                    page.next_page_id = new_id
                    self.buffer.unpin_page(page_id, True)
                    pinned = False
                    page_id = new_id
                    continue
                self.buffer.unpin_page(page_id, False)
                pinned = False
                page_id = nxt
            finally:
                # 任何异常都不能把当前页面永久留在 pinned 状态。
                if pinned:
                    self.buffer.unpin_page(page_id, True)

    # ------------------------------ 读取 ------------------------------

    def iter_rows(self) -> Iterator:
        """迭代访问表的所有数据页（SeqScan 的物理基础）。"""
        page_id = self.first_page_id
        visited = set()
        while page_id != INVALID_PAGE_ID:
            self._visit_page(page_id, visited)
            page = self.buffer.fetch_page(page_id)
            try:
                next_id = page.next_page_id
                slot_count = page.num_slots
                for slot in range(slot_count):
                    record = page.get_record(slot)
                    if record is None:
                        continue
                    yield RowId(page_id, slot), decode_row(record)
            finally:
                self.buffer.unpin_page(page_id, False)
            page_id = next_id

    def get_row(self, rid: RowId) -> Optional[list]:
        page = self.buffer.fetch_page(rid.page_id)
        try:
            record = page.get_record(rid.slot_id)
        finally:
            self.buffer.unpin_page(rid.page_id, False)
        return None if record is None else decode_row(record)

    # ------------------------------ 删除 ------------------------------

    def delete_row(self, rid: RowId) -> bool:
        page = self.buffer.fetch_page(rid.page_id)
        try:
            ok = page.delete_record(rid.slot_id)
        finally:
            self.buffer.unpin_page(rid.page_id, True)
        return ok

    # ------------------------------ 页管理 ------------------------------

    def page_ids(self) -> list:
        ids = []
        page_id = self.first_page_id
        visited = set()
        while page_id != INVALID_PAGE_ID:
            self._visit_page(page_id, visited)
            ids.append(page_id)
            page = self.buffer.fetch_page(page_id)
            try:
                next_id = page.next_page_id
            finally:
                self.buffer.unpin_page(page_id, False)
            page_id = next_id
        return ids

    def page_count(self) -> int:
        return len(self.page_ids())

    def row_count(self) -> int:
        return sum(1 for _ in self.iter_rows())

    def drop_all(self) -> int:
        """释放表占用的全部页，返回释放页数。"""
        freed = 0
        page_id = self.first_page_id
        visited = set()
        while page_id != INVALID_PAGE_ID:
            self._visit_page(page_id, visited)
            page = self.buffer.fetch_page(page_id)
            try:
                next_id = page.next_page_id
            finally:
                self.buffer.unpin_page(page_id, False)
            self.buffer.delete_page(page_id)
            freed += 1
            page_id = next_id
        self.first_page_id = INVALID_PAGE_ID
        return freed

    def __str__(self) -> str:
        return f"TableHeap(first_page={self.first_page_id})"
