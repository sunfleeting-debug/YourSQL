"""固定大小的槽位页。

页面层只关心一件事：如何在 4096 字节中组织记录。缓冲池中的
``pin_count`` 和 ``dirty`` 仍作为兼容字段保留，但它们的生命周期由
``BufferPoolManager`` 管理。

页面布局（所有整数均为 little-endian）：

    0..15       页头
    16..        槽位目录（每项 4 字节：offset + length）
    ...         空闲区
    free_ptr..  记录区，记录从页尾向前生长

页头字段：

    0           page_type (uint8)
    1           flags (uint8)
    2..5        next_page_id (int32)，-1 表示没有下一页
    6..7        num_slots (uint16)
    8..9        free_pointer (uint16)
    10..15      保留

删除记录时只清空槽位项。记录区不会立即压缩，后续插入可以复用槽位，
但只能使用新的记录区空间。这是一个有意选择的简化策略。
"""

from __future__ import annotations

import struct

from database_system.utils.constants import INVALID_PAGE_ID, PAGE_SIZE, PageType
from database_system.utils.errors import StorageError

HEADER_SIZE = 16
SLOT_SIZE = 4
MAX_SLOTS = (PAGE_SIZE - HEADER_SIZE) // SLOT_SIZE
MAX_RECORD_SIZE = PAGE_SIZE - HEADER_SIZE - SLOT_SIZE


class Page:
    """内存中的一个完整页面。

    ``data`` 始终是一个长度为 ``PAGE_SIZE`` 的 bytearray。页面对象提供
    结构化属性和槽位操作，``encode`` / ``decode`` 负责页面边界校验。
    """

    __slots__ = ("page_id", "data", "pin_count", "dirty")

    def __init__(self, page_id: int, data: bytearray | bytes | None = None):
        self.page_id = page_id
        if data is None:
            self.data = bytearray(PAGE_SIZE)
        else:
            if len(data) != PAGE_SIZE:
                raise StorageError(
                    f"page data must be exactly {PAGE_SIZE} bytes"
                )
            self.data = bytearray(data)
        # 这两个字段属于缓冲池状态，保留在这里是为了兼容现有 engine 调用。
        self.pin_count = 0
        self.dirty = False

    # ------------------------------ 编码 / 校验 ------------------------------

    def encode(self) -> bytes:
        """把页面编码为恰好一个磁盘页，并在写盘前校验布局。"""
        self.validate()
        return bytes(self.data)

    @classmethod
    def decode(cls, data: bytes, page_id: int = -1) -> "Page":
        """从一个完整磁盘页解码，并拒绝越界的页头或槽位。"""
        page = cls(page_id, data)
        page.validate()
        return page

    def validate(self) -> None:
        """验证页头、槽位目录和记录边界。"""
        if len(self.data) != PAGE_SIZE:
            raise StorageError(f"page data must be exactly {PAGE_SIZE} bytes")
        if self.page_type not in (PageType.DATA, PageType.CATALOG):
            raise StorageError(f"invalid page type {self.page_type}")
        if self.next_page_id < INVALID_PAGE_ID or self.next_page_id == 0:
            raise StorageError(f"invalid next page id {self.next_page_id}")
        if self.num_slots > MAX_SLOTS:
            raise StorageError(f"too many slots: {self.num_slots}")

        directory_end = HEADER_SIZE + SLOT_SIZE * self.num_slots
        if not directory_end <= self.free_pointer <= PAGE_SIZE:
            raise StorageError(
                f"invalid free pointer {self.free_pointer} for {self.num_slots} slots"
            )

        for slot_id in range(self.num_slots):
            offset, length = self.get_slot(slot_id)
            if length == 0 and offset == 0:
                continue
            if offset < self.free_pointer or offset + length > PAGE_SIZE:
                raise StorageError(f"slot {slot_id} points outside page")

    # ------------------------------ 页头 ------------------------------

    @property
    def page_type(self) -> int:
        return self.data[0]

    @page_type.setter
    def page_type(self, value: int) -> None:
        if value < 0 or value > 255:
            raise StorageError(f"invalid page type {value}")
        self.data[0] = value

    @property
    def next_page_id(self) -> int:
        return struct.unpack_from("<i", self.data, 2)[0]

    @next_page_id.setter
    def next_page_id(self, value: int) -> None:
        if value < INVALID_PAGE_ID or value == 0:
            raise StorageError(f"invalid next page id {value}")
        struct.pack_into("<i", self.data, 2, value)

    @property
    def num_slots(self) -> int:
        return struct.unpack_from("<H", self.data, 6)[0]

    @num_slots.setter
    def num_slots(self, value: int) -> None:
        if value < 0 or value > MAX_SLOTS:
            raise StorageError(f"invalid slot count {value}")
        struct.pack_into("<H", self.data, 6, value)

    @property
    def free_pointer(self) -> int:
        return struct.unpack_from("<H", self.data, 8)[0]

    @free_pointer.setter
    def free_pointer(self, value: int) -> None:
        if value < 0 or value > PAGE_SIZE:
            raise StorageError(f"invalid free pointer {value}")
        struct.pack_into("<H", self.data, 8, value)

    def init(
        self,
        page_type: int = PageType.DATA,
        next_page_id: int = INVALID_PAGE_ID,
    ) -> None:
        """初始化为空页。"""
        if page_type not in (PageType.DATA, PageType.CATALOG):
            raise StorageError(f"invalid page type {page_type}")
        self.data[:] = bytes(PAGE_SIZE)
        self.page_type = page_type
        self.next_page_id = next_page_id
        self.num_slots = 0
        self.free_pointer = PAGE_SIZE

    # ------------------------------ 槽位目录 ------------------------------

    def _slot_at(self, slot_id: int) -> int:
        if slot_id < 0 or slot_id >= self.num_slots:
            raise StorageError(f"invalid slot id {slot_id}")
        return HEADER_SIZE + SLOT_SIZE * slot_id

    def get_slot(self, slot_id: int) -> tuple[int, int]:
        offset = self._slot_at(slot_id)
        return struct.unpack_from("<HH", self.data, offset)

    def set_slot(self, slot_id: int, offset: int, length: int) -> None:
        slot_offset = HEADER_SIZE + SLOT_SIZE * slot_id
        if slot_id < 0 or slot_id >= MAX_SLOTS:
            raise StorageError(f"invalid slot id {slot_id}")
        if not 0 <= offset <= PAGE_SIZE or not 0 <= length <= PAGE_SIZE:
            raise StorageError("invalid slot offset or length")
        struct.pack_into("<HH", self.data, slot_offset, offset, length)

    def is_slot_deleted(self, slot_id: int) -> bool:
        offset, length = self.get_slot(slot_id)
        return offset == 0 and length == 0

    def _first_free_slot(self) -> int | None:
        for slot_id in range(self.num_slots):
            if self.is_slot_deleted(slot_id):
                return slot_id
        return None

    # ------------------------------ 空间管理 ------------------------------

    def free_space(self) -> int:
        """返回当前可插入的记录空间，包含新增槽位的开销。"""
        directory_end = HEADER_SIZE + SLOT_SIZE * self.num_slots
        extra_slot = 0 if self._first_free_slot() is not None else SLOT_SIZE
        return self.free_pointer - directory_end - extra_slot

    def can_insert(self, size: int) -> bool:
        return isinstance(size, int) and size >= 0 and size <= self.free_space()

    # ------------------------------ 记录读写 ------------------------------

    def insert_record(self, data: bytes) -> int:
        """插入一条记录并返回槽号。"""
        payload = bytes(data)
        size = len(payload)
        if size > MAX_RECORD_SIZE:
            raise StorageError(f"record too large: {size} bytes")

        slot_id = self._first_free_slot()
        if slot_id is None:
            if self.num_slots >= MAX_SLOTS or not self.can_insert(size):
                raise StorageError("page is full")
            slot_id = self.num_slots
            self.num_slots += 1
        elif self.free_pointer - (
            HEADER_SIZE + SLOT_SIZE * self.num_slots
        ) < size:
            raise StorageError("page is full")

        new_pointer = self.free_pointer - size
        self.data[new_pointer : new_pointer + size] = payload
        self.set_slot(slot_id, new_pointer, size)
        self.free_pointer = new_pointer
        return slot_id

    def get_record(self, slot_id: int) -> bytes | None:
        """读取槽位记录；无效或已删除槽位返回 None。"""
        if slot_id < 0 or slot_id >= self.num_slots:
            return None
        if self.is_slot_deleted(slot_id):
            return None
        offset, length = self.get_slot(slot_id)
        if offset < self.free_pointer or offset + length > PAGE_SIZE:
            raise StorageError(f"slot {slot_id} points outside page")
        return bytes(self.data[offset : offset + length])

    def update_record(self, slot_id: int, data: bytes) -> bool:
        """原地更新记录；新记录不能比旧记录更长。"""
        if slot_id < 0 or slot_id >= self.num_slots:
            return False
        if self.is_slot_deleted(slot_id):
            return False
        offset, length = self.get_slot(slot_id)
        payload = bytes(data)
        if len(payload) > length:
            return False
        self.data[offset : offset + len(payload)] = payload
        if len(payload) < length:
            self.data[offset + len(payload) : offset + length] = bytes(
                length - len(payload)
            )
        return True

    def delete_record(self, slot_id: int) -> bool:
        """逻辑删除记录，清空槽位使其可以复用。"""
        if slot_id < 0 or slot_id >= self.num_slots:
            return False
        if self.is_slot_deleted(slot_id):
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
