"""固定大小页和页内槽目录。"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from enum import Enum

from ..common.errors import StorageError


class PageType(str, Enum):
    """磁盘页的用途。"""

    FREE = "free"
    SUPERBLOCK = "superblock"
    CATALOG = "catalog"
    HEAP = "heap"
    INDEX = "index"


PAGE_MAGIC = b"MDBP"
PAGE_VERSION = 2
PAGE_HEADER = struct.Struct("<4sIBBQI I4s")
# 4 magic + 4 version + 1 type + 1 reserved + 8 page id + 4 payload length + 4 crc + 4 对齐保留
HEADER_SIZE = PAGE_HEADER.size

# HEAP 页在外层 Page Header 之后使用独立的双向槽式布局。槽目录从
# payload 起点向后增长，记录从 payload 尾部向前增长，中间保留空闲区。
SLOTTED_MAGIC = b"MSP2"
SLOTTED_VERSION = 1
SLOTTED_HEADER = struct.Struct("<4sBBHHH")
SLOTTED_HEADER_SIZE = SLOTTED_HEADER.size
SLOT_ENTRY = struct.Struct("<HHBB")
SLOT_ENTRY_SIZE = SLOT_ENTRY.size
SLOT_DELETED = 1


def _page_type_code(page_type: PageType) -> int:
    values = tuple(PageType)
    return values.index(page_type) + 1


def _page_type_from_code(code: int) -> PageType:
    values = tuple(PageType)
    if code < 1 or code > len(values):
        raise StorageError(f"未知页类型编码 {code}")
    return values[code - 1]


@dataclass
class Page:
    """带 CRC 校验的固定大小页。"""

    page_id: int
    page_size: int = 4096
    page_type: PageType = PageType.FREE
    payload: bytes = b""

    def __post_init__(self) -> None:
        self.page_id = int(self.page_id)
        if self.page_id < 0:
            raise StorageError("page_id 不能为负数")
        if self.page_size < HEADER_SIZE:
            raise StorageError("page_size 小于页头")
        if not isinstance(self.page_type, PageType):
            self.page_type = PageType(str(self.page_type))
        if not isinstance(self.payload, bytes):
            self.payload = bytes(self.payload)
        if len(self.payload) > self.page_size - HEADER_SIZE:
            raise StorageError(f"页 {self.page_id} 负载超过页容量")

    @property
    def free_space(self) -> int:
        return self.page_size - HEADER_SIZE - len(self.payload)

    def to_bytes(self) -> bytes:
        checksum = zlib.crc32(self.payload) & 0xFFFFFFFF
        header = PAGE_HEADER.pack(
            PAGE_MAGIC,
            PAGE_VERSION,
            _page_type_code(self.page_type),
            0,
            self.page_id,
            len(self.payload),
            checksum,
            b"\x00" * 4,
        )
        return header + self.payload + bytes(self.page_size - len(header) - len(self.payload))

    @classmethod
    def from_bytes(cls, raw: bytes, *, page_size: int | None = None) -> "Page":
        if page_size is None:
            page_size = len(raw)
        if len(raw) != page_size:
            raise StorageError(f"页长度错误，期望 {page_size}，实际 {len(raw)}")
        if len(raw) < HEADER_SIZE:
            raise StorageError("页内容不足以包含页头")
        magic, version, type_code, _reserved, page_id, payload_length, checksum, _alignment_reserved = PAGE_HEADER.unpack(raw[:HEADER_SIZE])
        if magic != PAGE_MAGIC:
            raise StorageError(f"页 {page_id} 魔数错误")
        if version != PAGE_VERSION:
            raise StorageError(f"不支持的页版本 {version}")
        end = HEADER_SIZE + payload_length
        if end > page_size:
            raise StorageError(f"页 {page_id} 负载长度非法")
        payload = raw[HEADER_SIZE:end]
        if zlib.crc32(payload) & 0xFFFFFFFF != checksum:
            raise StorageError(f"页 {page_id} CRC 校验失败")
        return cls(page_id, page_size, _page_type_from_code(type_code), payload)

    @classmethod
    def empty(cls, page_id: int, page_size: int = 4096, page_type: PageType = PageType.FREE) -> "Page":
        return cls(page_id=page_id, page_size=page_size, page_type=page_type)


class SlottedPage:
    """双向增长的槽式页；写入优先保留现有记录的物理 offset。"""

    def __init__(
        self,
        page_id: int,
        page_size: int = 4096,
        slots: list[bytes | None] | None = None,
        *,
        entries: list[tuple[int, int, bool]] | None = None,
        _payload: bytes | None = None,
    ) -> None:
        self.page_id = int(page_id)
        self.page_size = int(page_size)
        self.slots: list[bytes | None] = list(slots or [])
        self._entries: list[tuple[int, int, bool]] = list(entries or [])
        self._payload: bytearray | None = None if _payload is None else bytearray(_payload)
        if self._payload is not None and len(self._payload) != self._payload_capacity:
            raise StorageError("双向槽式页负载长度错误")

    @property
    def storage_format(self) -> str:
        """返回当前页内编码名称。"""

        return "double_ended_v2"

    @property
    def _payload_capacity(self) -> int:
        return self.page_size - HEADER_SIZE

    @classmethod
    def from_page(cls, page: Page) -> "SlottedPage":
        if page.page_type is not PageType.HEAP:
            raise StorageError(f"页 {page.page_id} 不是 HEAP 页")
        if not page.payload:
            return cls(page.page_id, page.page_size)
        if page.payload.startswith(SLOTTED_MAGIC):
            return cls._from_binary(page)
        raise StorageError(f"HEAP 页 {page.page_id} 不是双向槽式格式")

    @classmethod
    def _from_binary(cls, page: Page) -> "SlottedPage":
        capacity = page.page_size - HEADER_SIZE
        if len(page.payload) != capacity:
            raise StorageError(f"HEAP 页 {page.page_id} 双向槽式负载长度错误")
        if capacity > 0xFFFF:
            raise StorageError("页容量超过双向槽式格式的偏移上限")
        try:
            magic, version, _flags, slot_count, free_start, free_end = SLOTTED_HEADER.unpack(
                page.payload[:SLOTTED_HEADER_SIZE])
        except struct.error as exc:
            raise StorageError(f"HEAP 页 {page.page_id} 双向槽式页头损坏") from exc
        if magic != SLOTTED_MAGIC or version != SLOTTED_VERSION:
            raise StorageError(f"HEAP 页 {page.page_id} 双向槽式版本不支持")
        directory_end = SLOTTED_HEADER_SIZE + slot_count * SLOT_ENTRY_SIZE
        if directory_end > capacity or free_start != directory_end or free_start > free_end or free_end > capacity:
            raise StorageError(f"HEAP 页 {page.page_id} 空闲区边界非法")

        slots: list[bytes | None] = []
        entries: list[tuple[int, int, bool]] = []
        ranges: list[tuple[int, int]] = []
        for slot_id in range(slot_count):
            entry_offset = SLOTTED_HEADER_SIZE + slot_id * SLOT_ENTRY_SIZE
            try:
                record_offset, record_length, flags, _reserved = SLOT_ENTRY.unpack(
                    page.payload[entry_offset:entry_offset + SLOT_ENTRY_SIZE])
            except struct.error as exc:
                raise StorageError(f"HEAP 页 {page.page_id} 槽目录损坏") from exc
            deleted = bool(flags & SLOT_DELETED)
            if deleted:
                if record_length and (record_offset < directory_end or record_offset + record_length > capacity):
                    raise StorageError(f"HEAP 页 {page.page_id} 删除槽记录范围非法")
                slots.append(None)
                entries.append((record_offset, record_length, True))
                continue
            if record_length <= 0 or record_offset < free_end or record_offset + record_length > capacity:
                raise StorageError(f"HEAP 页 {page.page_id} 槽 {slot_id} 记录范围非法")
            ranges.append((record_offset, record_offset + record_length))
            slots.append(bytes(page.payload[record_offset:record_offset + record_length]))
            entries.append((record_offset, record_length, False))
        for left, right in zip(sorted(ranges), sorted(ranges)[1:]):
            if left[1] > right[0]:
                raise StorageError(f"HEAP 页 {page.page_id} 记录范围重叠")
        return cls(page.page_id, page.page_size, slots, entries=entries, _payload=page.payload)

    def _compact_binary(self, slots: list[bytes | None] | None = None) -> tuple[bytes, list[tuple[int, int, bool]], dict[str, int]]:
        """在无法增量分配时压缩记录，并生成新的槽目录。"""

        selected = self.slots if slots is None else slots
        capacity = self._payload_capacity
        if capacity <= SLOTTED_HEADER_SIZE:
            raise StorageError("页容量不足以保存双向槽式页头")
        if len(selected) > 0xFFFF:
            raise StorageError("槽位数量超过双向槽式格式上限")
        directory_end = SLOTTED_HEADER_SIZE + len(selected) * SLOT_ENTRY_SIZE
        if directory_end > capacity:
            raise StorageError("槽目录超过页容量")

        cursor = capacity
        entries: list[tuple[int, int, bool]] = [(0, 0, True) for _ in selected]
        # 逆向填充让较小的 slot_id 位于较低地址；仅在压缩时重排旧记录。
        for slot_id in range(len(selected) - 1, -1, -1):
            record = selected[slot_id]
            if record is None:
                continue
            if not isinstance(record, bytes):
                raise TypeError("record 必须是 bytes")
            if len(record) > 0xFFFF:
                raise StorageError("记录长度超过双向槽式格式上限")
            cursor -= len(record)
            if cursor < directory_end:
                raise StorageError("槽式页没有足够空间")
            entries[slot_id] = (cursor, len(record), False)

        payload = bytearray(capacity)
        SLOTTED_HEADER.pack_into(payload, 0, SLOTTED_MAGIC, SLOTTED_VERSION, 0,
                                  len(selected), directory_end, cursor)
        for slot_id, (record_offset, record_length, deleted) in enumerate(entries):
            entry_offset = SLOTTED_HEADER_SIZE + slot_id * SLOT_ENTRY_SIZE
            SLOT_ENTRY.pack_into(payload, entry_offset, record_offset, record_length,
                                 SLOT_DELETED if deleted else 0, 0)
        for (record_offset, _record_length, deleted), record in zip(entries, selected):
            if not deleted and record is not None:
                payload[record_offset:record_offset + len(record)] = record
        return bytes(payload), entries, {
            "directory_start": SLOTTED_HEADER_SIZE,
            "directory_end": directory_end,
            "free_start": directory_end,
            "free_end": cursor,
            "record_start": cursor,
            "record_end": capacity,
        }

    def _active_ranges(self, entries: list[tuple[int, int, bool]], directory_end: int) -> list[tuple[int, int]]:
        ranges: list[tuple[int, int]] = []
        for record_offset, record_length, deleted in entries:
            if deleted or record_length == 0:
                continue
            record_end = record_offset + record_length
            if record_offset < directory_end or record_end > self._payload_capacity:
                raise StorageError("槽式页记录范围非法")
            ranges.append((record_offset, record_end))
        ranges.sort()
        for left, right in zip(ranges, ranges[1:]):
            if left[1] > right[0]:
                raise StorageError("槽式页记录范围重叠")
        return ranges

    def _free_extents(self, entries: list[tuple[int, int, bool]], directory_end: int) -> list[tuple[int, int]]:
        """返回目录之后所有可用空闲片段，供插入和更新做 first-fit。"""

        if directory_end > self._payload_capacity:
            raise StorageError("槽目录超过页容量")
        ranges = self._active_ranges(entries, directory_end)
        extents: list[tuple[int, int]] = []
        cursor = directory_end
        for start, end in ranges:
            if cursor < start:
                extents.append((cursor, start))
            cursor = max(cursor, end)
        if cursor < self._payload_capacity:
            extents.append((cursor, self._payload_capacity))
        return extents

    def _current_binary(self) -> tuple[bytes, list[tuple[int, int, bool]], dict[str, int]]:
        """序列化当前物理布局，不主动压缩已有记录。"""

        if self._payload is None or len(self._entries) != len(self.slots):
            return self._compact_binary()
        capacity = self._payload_capacity
        directory_end = SLOTTED_HEADER_SIZE + len(self.slots) * SLOT_ENTRY_SIZE
        if directory_end > capacity:
            raise StorageError("槽目录超过页容量")
        entries = list(self._entries)
        ranges: list[tuple[int, int]] = []
        for slot_id, (record_offset, record_length, deleted) in enumerate(entries):
            if record_length:
                if record_offset < directory_end or record_offset + record_length > capacity:
                    raise StorageError(f"槽 {slot_id} 记录范围非法")
                ranges.append((record_offset, record_offset + record_length))
            if not deleted:
                record = self.slots[slot_id]
                if record is None or len(record) != record_length:
                    raise StorageError(f"槽 {slot_id} 记录长度与槽目录不一致")
        for left, right in zip(sorted(ranges), sorted(ranges)[1:]):
            if left[1] > right[0]:
                raise StorageError("槽式页记录范围重叠")

        payload = bytearray(self._payload)
        live_starts: list[int] = []
        for slot_id, (record_offset, record_length, deleted) in enumerate(entries):
            if not deleted:
                record = self.slots[slot_id]
                if record is None:
                    raise StorageError(f"槽 {slot_id} 缺少记录")
                payload[record_offset:record_offset + record_length] = record
                live_starts.append(record_offset)
            entry_offset = SLOTTED_HEADER_SIZE + slot_id * SLOT_ENTRY_SIZE
            SLOT_ENTRY.pack_into(payload, entry_offset, record_offset, record_length,
                                 SLOT_DELETED if deleted else 0, 0)
        free_end = min(live_starts, default=capacity)
        SLOTTED_HEADER.pack_into(payload, 0, SLOTTED_MAGIC, SLOTTED_VERSION, 0,
                                  len(entries), directory_end, free_end)
        return bytes(payload), entries, {
            "directory_start": SLOTTED_HEADER_SIZE,
            "directory_end": directory_end,
            "free_start": directory_end,
            "free_end": free_end,
            "record_start": free_end,
            "record_end": capacity,
        }

    def _build_binary(self, slots: list[bytes | None] | None = None) -> tuple[bytes, list[tuple[int, int, bool]], dict[str, int]]:
        # 带候选 slots 的调用按候选布局试算，不改变当前页的物理记录位置。
        if slots is not None:
            return self._compact_binary(slots)
        return self._current_binary()

    def _set_compact(self, slots: list[bytes | None] | None = None) -> None:
        selected = list(self.slots if slots is None else slots)
        payload, entries, _layout = self._compact_binary(selected)
        self.slots = selected
        self._payload = bytearray(payload)
        self._entries = entries

    def _ensure_binary(self) -> None:
        if self._payload is None or len(self._entries) != len(self.slots):
            self._set_compact()

    def _allocate_record(self, record_length: int, directory_end: int,
                         entries: list[tuple[int, int, bool]]) -> int | None:
        if record_length <= 0 or record_length > 0xFFFF:
            raise StorageError("记录长度非法")
        for start, end in reversed(self._free_extents(entries, directory_end)):
            if end - start >= record_length:
                return end - record_length
        return None

    @staticmethod
    def _clear_deleted_overlaps(entries: list[tuple[int, int, bool]],
                                 record_offset: int, record_length: int) -> None:
        """清理被新记录覆盖的删除槽旧范围，避免目录范围与新记录重叠。"""

        record_end = record_offset + record_length
        for slot_id, (old_offset, old_length, deleted) in enumerate(entries):
            if not deleted or old_length == 0:
                continue
            old_end = old_offset + old_length
            if old_offset < record_end and record_offset < old_end:
                entries[slot_id] = (0, 0, True)

    def _place_record(self, slot_id: int, record: bytes, *, append_slot: bool = False) -> int:
        self._ensure_binary()
        if not isinstance(record, bytes):
            raise TypeError("record 必须是 bytes")
        old_entries = list(self._entries)
        if append_slot:
            old_entries.append((0, 0, False))
        directory_end = SLOTTED_HEADER_SIZE + len(old_entries) * SLOT_ENTRY_SIZE
        try:
            offset = self._allocate_record(len(record), directory_end, old_entries)
        except StorageError:
            # 新槽目录可能侵入旧记录的起始位置，此时只能压缩后再分配。
            offset = None
        candidate = list(self.slots)
        if append_slot:
            candidate.append(record)
        else:
            candidate[slot_id] = record
        if offset is None:
            # WHY：只有没有可用连续片段时才压缩，避免一次插入移动整页旧记录。
            self._set_compact(candidate)
            return slot_id

        if self._payload is None:
            raise StorageError("槽式页负载尚未初始化")
        self._clear_deleted_overlaps(old_entries, offset, len(record))
        if not append_slot:
            old_offset, old_length, old_deleted = old_entries[slot_id]
            if old_deleted and old_length:
                self._payload[old_offset:old_offset + old_length] = b"\x00" * old_length
        self.slots = candidate
        self._entries = old_entries
        self._entries[slot_id] = (offset, len(record), False)
        self._payload[offset:offset + len(record)] = record
        return slot_id

    @property
    def free_space(self) -> int:
        self._ensure_binary()
        directory_end = SLOTTED_HEADER_SIZE + len(self._entries) * SLOT_ENTRY_SIZE
        return sum(end - start for start, end in self._free_extents(self._entries, directory_end))

    def insert(self, record: bytes) -> int:
        if not isinstance(record, bytes):
            raise TypeError("record 必须是 bytes")
        for index, value in enumerate(self.slots):
            if value is None:
                return self._place_record(index, record)
        return self._place_record(len(self.slots), record, append_slot=True)

    def get(self, slot_id: int) -> bytes | None:
        if slot_id < 0 or slot_id >= len(self.slots):
            raise StorageError(f"槽号 {slot_id} 越界")
        return self.slots[slot_id]

    def update(self, slot_id: int, record: bytes) -> None:
        if slot_id < 0 or slot_id >= len(self.slots) or self.slots[slot_id] is None:
            raise StorageError(f"槽号 {slot_id} 不存在")
        if not isinstance(record, bytes):
            raise TypeError("record 必须是 bytes")
        self._ensure_binary()
        old_offset, old_length, _deleted = self._entries[slot_id]
        if len(record) <= old_length:
            if self._payload is None:
                raise StorageError("槽式页负载尚未初始化")
            self._payload[old_offset:old_offset + old_length] = record + b"\x00" * (old_length - len(record))
            self.slots[slot_id] = record
            self._entries[slot_id] = (old_offset, len(record), False)
            return

        entries_for_space = list(self._entries)
        entries_for_space[slot_id] = (old_offset, old_length, True)
        offset = self._allocate_record(len(record), SLOTTED_HEADER_SIZE + len(self._entries) * SLOT_ENTRY_SIZE, entries_for_space)
        candidate = list(self.slots)
        candidate[slot_id] = record
        if offset is None:
            self._set_compact(candidate)
            return
        if self._payload is None:
            raise StorageError("槽式页负载尚未初始化")
        self._clear_deleted_overlaps(self._entries, offset, len(record))
        self._payload[old_offset:old_offset + old_length] = b"\x00" * old_length
        self._payload[offset:offset + len(record)] = record
        self.slots[slot_id] = record
        self._entries[slot_id] = (offset, len(record), False)

    def delete(self, slot_id: int) -> None:
        if slot_id < 0 or slot_id >= len(self.slots):
            raise StorageError(f"槽号 {slot_id} 越界")
        if self.slots[slot_id] is None:
            return
        self._ensure_binary()
        record_offset, record_length, _deleted = self._entries[slot_id]
        if self._payload is not None and record_length:
            self._payload[record_offset:record_offset + record_length] = b"\x00" * record_length
        self.slots[slot_id] = None
        self._entries[slot_id] = (record_offset, record_length, True)

    def slot_layout(self) -> list[dict[str, int | bool]]:
        """返回每个槽的物理范围，offset 相对于完整数据库页。"""

        _payload, entries, _layout = self._build_binary()
        return [{"slot_id": slot_id,
                 "offset": 0 if deleted else HEADER_SIZE + record_offset,
                 "length": 0 if deleted else record_length,
                 "deleted": deleted}
                for slot_id, (record_offset, record_length, deleted) in enumerate(entries)]

    def layout_metadata(self) -> dict[str, object]:
        """返回工作台绘制双向页布局所需的有界结构化信息。"""

        _payload, entries, layout = self._build_binary()
        page_offset = HEADER_SIZE
        slot_directory_start = page_offset + layout["directory_start"]
        slot_directory_end = page_offset + layout["directory_end"]
        free_regions = [{"start": page_offset + start, "end": page_offset + end,
                         "size": end - start, "direction": "free"}
                        for start, end in self._free_extents(entries, layout["directory_end"])
                        if end > start]
        free_start = page_offset + layout["free_start"]
        free_end = page_offset + layout["free_end"]
        record_start = page_offset + layout["record_start"]
        record_end = page_offset + layout["record_end"]
        return {
            "format": self.storage_format, "physical": True,
            "payload_offset": page_offset, "payload_capacity": self._payload_capacity,
            "inner_header_size": SLOTTED_HEADER_SIZE, "slot_entry_size": SLOT_ENTRY_SIZE,
            "slot_count": len(entries),
            "slot_directory": {"start": slot_directory_start, "end": slot_directory_end,
                                "size": slot_directory_end - slot_directory_start,
                                "direction": "forward"},
            "free_region": {"start": free_start, "end": free_end,
                             "size": max(0, free_end - free_start),
                             "direction": "free"},
            "free_regions": free_regions,
            "record_region": {"start": record_start, "end": record_end,
                               "size": max(0, record_end - record_start),
                               "direction": "backward"},
            "slots": [{"slot_id": slot_id,
                       "offset": 0 if deleted else HEADER_SIZE + record_offset,
                       "length": 0 if deleted else record_length,
                       "deleted": deleted,
                       "directory_offset": slot_directory_start + slot_id * SLOT_ENTRY_SIZE,
                       "directory_length": SLOT_ENTRY_SIZE}
                      for slot_id, (record_offset, record_length, deleted) in enumerate(entries)],
        }

    def to_page(self) -> Page:
        payload, entries, _layout = self._build_binary()
        self._payload = bytearray(payload)
        self._entries = entries
        return Page(self.page_id, self.page_size, PageType.HEAP, payload)

    def live_slots(self) -> tuple[tuple[int, bytes], ...]:
        return tuple((index, value) for index, value in enumerate(self.slots) if value is not None)


# 让测试和调用者可以从 Page 访问常量，避免重复导入内部结构。
Page.HEADER_SIZE = HEADER_SIZE  # type: ignore[attr-defined]
