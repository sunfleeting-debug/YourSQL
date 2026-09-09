"""阶段 5：页结构（固定大小 + 槽位目录）。

单页布局（PAGE_SIZE = 4096 字节）：

    ┌──────────────────────────────────────────────────────────┐
    │ Header (16B) │ 槽目录 (4B × n) │  空闲区  │  记录区(向下生长) │
    └──────────────────────────────────────────────────────────┘
    0           16            16+4n        free_ptr         4096

Header：
    0      : page_type  (uint8)
    1      : flags      (uint8)
    2..5   : next_page_id (int32)   —— 同表的下一页，构成页链表
    6..7   : num_slots  (uint16)
    8..9   : free_ptr   (uint16)   —— 记录区起始偏移（记录从页尾向前生长）
    10..15 : 保留

槽目录项：offset(uint16) + length(uint16)；length == 0 表示该槽已删除（可复用）。
"""

from __future__ import annotations

import struct

from database_system.utils.constants import INVALID_PAGE_ID, PAGE_SIZE, PageType
from database_system.utils.errors import StorageError

HEADER_SIZE = 16
SLOT_SIZE = 4
MAX_SLOTS = (PAGE_SIZE - HEADER_SIZE) // SLOT_SIZE


class Page:
    """一个内存中的页，持有 bytearray 数据；由 BufferPool 负责落盘。"""

    __slots__ = ("page_id", "data", "pin_count", "dirty")

    def __init__(self, page_id: int, data: bytearray | bytes | None = None):
        self.page_id = page_id
        self.data = bytearray(data) if data is not None else bytearray(PAGE_SIZE)
        self.pin_count = 0
        self.dirty = False

    # ------------------------------ 页头 ------------------------------

    @property
    def page_type(self) -> int:
        return self.data[0]

    @page_type.setter
    def page_type(self, value: int) -> None:
        self.data[0] = value & 0xFF

    @property
    def next_page_id(self) -> int:
        return struct.unpack_from("<i", self.data, 2)[0]

    @next_page_id.setter
    def next_page_id(self, value: int) -> None:
        struct.pack_into("<i", self.data, 2, value)

    @property
    def num_slots(self) -> int:
        return struct.unpack_from("<H", self.data, 6)[0]

    @num_slots.setter
    def num_slots(self, value: int) -> None:
        struct.pack_into("<H", self.data, 6, value)

    @property
    def free_pointer(self) -> int:
        return struct.unpack_from("<H", self.data, 8)[0]

    @free_pointer.setter
    def free_pointer(self, value: int) -> None:
        struct.pack_into("<H", self.data, 8, value)

    def init(self, page_type: int = PageType.DATA,
             next_page_id: int = INVALID_PAGE_ID) -> None:
        """初始化为一张空页。"""
        self.data[:] = bytearray(PAGE_SIZE)
        self.page_type = page_type
        self.next_page_id = next_page_id
        self.num_slots = 0
        self.free_pointer = PAGE_SIZE

    # ------------------------------ 槽目录 ------------------------------

    def _slot_at(self, slot_id: int) -> int:
        return HEADER_SIZE + SLOT_SIZE * slot_id

    def get_slot(self, slot_id: int):
        off, length = struct.unpack_from("<HH", self.data, self._slot_at(slot_id))
        return off, length

    def set_slot(self, slot_id: int, offset: int, length: int) -> None:
        struct.pack_into("<HH", self.data, self._slot_at(slot_id), offset, length)

    def is_slot_deleted(self, slot_id: int) -> bool:
        off, length = self.get_slot(slot_id)
        return length == 0 and off == 0

    def _first_free_slot(self):
        for i in range(self.num_slots):
            if self.is_slot_deleted(i):
                return i
        return None

    # ------------------------------ 空间管理 ------------------------------

    def free_space(self) -> int:
        """当前可用字节数（已为「可能新增一个槽」预留空间）。"""
        used_end = HEADER_SIZE + SLOT_SIZE * self.num_slots
        extra = 0 if self._first_free_slot() is not None else SLOT_SIZE
        return self.free_pointer - used_end - extra

    def can_insert(self, size: int) -> bool:
        return size <= self.free_space()

    # ------------------------------ 记录读写 ------------------------------

    def insert_record(self, data: bytes) -> int:
        """写入一条记录，返回槽号；空间不足抛 StorageError。"""
        size = len(data)
        if size > PAGE_SIZE - HEADER_SIZE - SLOT_SIZE:
            raise StorageError(f"record too large: {size} bytes")
        slot = self._first_free_slot()
        if slot is None:
            if not self.can_insert(size):
                raise StorageError("page is full")
            slot = self.num_slots
            self.num_slots = slot + 1
        else:
            if self.free_pointer - (HEADER_SIZE + SLOT_SIZE * self.num_slots) < size:
                raise StorageError("page is full")

        new_ptr = self.free_pointer - size
        self.data[new_ptr : new_ptr + size] = data
        self.set_slot(slot, new_ptr, size)
        self.free_pointer = new_ptr
        return slot

    def get_record(self, slot_id: int):
        """读取槽中的记录；槽为空（已删除）返回 None。"""
        if slot_id < 0 or slot_id >= self.num_slots:
            return None
        if self.is_slot_deleted(slot_id):
            return None
        off, length = self.get_slot(slot_id)
        return bytes(self.data[off : off + length])

    def update_record(self, slot_id: int, data: bytes) -> bool:
        """原地更新（新记录不比旧记录长时可行）。"""
        if slot_id < 0 or slot_id >= self.num_slots or self.is_slot_deleted(slot_id):
            return False
        off, length = self.get_slot(slot_id)
        if len(data) > length:
            return False
        self.data[off : off + len(data)] = data
        if len(data) < length:
            # 用 0 填充剩余部分，避免脏数据
            self.data[off + len(data) : off + length] = bytes(length - len(data))
        return True

    def delete_record(self, slot_id: int) -> bool:
        """逻辑删除：清空槽目录项（空间不立即回收，标记可复用）。"""
        if slot_id < 0 or slot_id >= self.num_slots or self.is_slot_deleted(slot_id):
            return False
        self.set_slot(slot_id, 0, 0)
        return True

    # ------------------------------ 调试 ------------------------------

    def __str__(self) -> str:
        return (
            f"Page(id={self.page_id}, type={self.page_type}, "
            f"next={self.next_page_id}, slots={self.num_slots}, "
            f"free={self.free_space()}, pin={self.pin_count}, dirty={self.dirty})"
        )
