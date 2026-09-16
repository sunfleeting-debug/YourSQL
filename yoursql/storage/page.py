"""固定大小页和页内槽目录。"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from yoursql.common.errors import StorageError


class PageType(str, Enum):
    """磁盘页的用途；值会写入页头，不能随意改名。"""

    FREE = "free"  # 已释放、可重新分配的空白页
    SUPERBLOCK = "superblock"  # 第 0 页：格式版本、页大小、空闲页与命名页
    CATALOG = "catalog"  # 目录链：表/视图/索引/权限元数据的 JSON
    HEAP = "heap"  # 表数据：`MSP2` 双向槽式记录页
    INDEX = "index"  # B+Tree 节点：叶子 key/RowId、内部分隔键与叶子链
    # === 兼容旧页类型编码 ===
    # DIRECTORY 只能追加到枚举末尾，不能插入已有成员中间，否则旧页的 type code 会改变。
    DIRECTORY = "directory"  # 内部命名页目录


PAGE_MAGIC = b"MDBP"
PAGE_VERSION = 2
# === 兼容旧页头布局 ===
# HOW：末尾 4 字节原本是对齐填充（旧文件恒为 0），现复用为页 LSN（日志序号），
# 因此结构体尺寸与磁盘布局均未变化，旧数据库文件可以直接打开。
PAGE_HEADER = struct.Struct("<4sIBBQIII")
# 4 magic + 4 version + 1 type + 1 reserved + 8 page id + 4 payload length + 4 crc + 4 lsn
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

# FREE 页不再是完全空白页，而是保存 free-list 的后继页号。
# 采用固定二进制布局，避免把空闲页号重新编码成会膨胀的 JSON。
FREE_PAGE_MAGIC = b"MFR1"
FREE_PAGE_HEADER = struct.Struct("<4sQ")
FREE_PAGE_HEADER_SIZE = FREE_PAGE_HEADER.size


@dataclass(frozen=True)
class SlotEntry:
    """槽目录中的一项；offset/length 均相对于 HEAP 页 payload。"""

    offset: int
    length: int
    deleted: bool


@dataclass(frozen=True)
class SlottedPageBinaryLayout:
    """双向槽式页序列化后各区域的 payload 相对范围。"""

    directory_start: int
    directory_end: int
    free_start: int
    free_end: int
    record_start: int
    record_end: int


@dataclass(frozen=True)
class SlottedPageBinary:
    """槽式页构建结果，避免在 payload、目录和布局之间靠位置解包。"""

    payload: bytes
    entries: tuple[SlotEntry, ...]
    layout: SlottedPageBinaryLayout


@dataclass(frozen=True)
class SlotLocation:
    """【前端特供】槽在完整数据库页中的物理位置展示结果。"""

    slot_id: int
    offset: int
    length: int
    deleted: bool
    directory_offset: int | None = None
    directory_length: int = SLOT_ENTRY_SIZE

    def __getitem__(self, key: str) -> int | bool | None:
        # === 兼容旧检查接口的下标访问 ===
        """保留旧检查代码的下标访问，同时提供带名字段访问。"""

        return getattr(self, key)

    def to_dict(self) -> dict[str, int | bool | None]:
        """【前端特供】转换为工作台 JSON 使用的映射。"""

        return {
            "slot_id": self.slot_id,
            "offset": self.offset,
            "length": self.length,
            "deleted": self.deleted,
            "directory_offset": self.directory_offset,
            "directory_length": self.directory_length,
        }


@dataclass(frozen=True)
class LiveSlot:
    """槽式页中仍存活的槽号与原始记录字节。"""

    slot_id: int
    raw: bytes

    def __iter__(self):
        # === 兼容旧槽位二元组解包 ===
        """兼容旧的槽号/字节二元组解包。"""

        yield self.slot_id
        yield self.raw


@dataclass(frozen=True)
class PageRegion:
    """【前端特供】页布局中的一个连续区域展示结果。"""

    start: int
    end: int
    size: int
    direction: str

    def to_dict(self) -> dict[str, int | str]:
        """【前端特供】转换为工作台使用的区域描述字典。"""
        return {
            "start": self.start,
            "end": self.end,
            "size": self.size,
            "direction": self.direction,
        }


@dataclass(frozen=True)
class SlottedPageLayoutInfo:
    """【前端特供】工作台绘制双向槽式页所需的带名布局结果。"""

    format: str
    physical: bool
    payload_offset: int
    payload_capacity: int
    inner_header_size: int
    slot_entry_size: int
    slot_count: int
    slot_directory: PageRegion
    free_region: PageRegion
    free_regions: tuple[PageRegion, ...]
    record_region: PageRegion
    slots: tuple[SlotLocation, ...]

    def __getitem__(self, key: str) -> object:
        # === 兼容旧工作台布局接口的映射式读取 ===
        """按字段名读取布局信息。"""
        return self.to_dict()[key]

    def get(self, key: str, default: object = None) -> object:
        # === 兼容旧工作台布局接口的 get 访问 ===
        """按字段名读取布局信息，缺失时返回默认值。"""
        return self.to_dict().get(key, default)

    def to_dict(self) -> dict[str, object]:
        """【前端特供】转换为工作台使用的完整布局字典。"""
        return {
            "format": self.format,
            "physical": self.physical,
            "payload_offset": self.payload_offset,
            "payload_capacity": self.payload_capacity,
            "inner_header_size": self.inner_header_size,
            "slot_entry_size": self.slot_entry_size,
            "slot_count": self.slot_count,
            "slot_directory": self.slot_directory.to_dict(),
            "free_region": self.free_region.to_dict(),
            "free_regions": [region.to_dict() for region in self.free_regions],
            "record_region": self.record_region.to_dict(),
            "slots": [slot.to_dict() for slot in self.slots],
        }


def _page_type_code(page_type: PageType) -> int:
    """将页类型转换为页头使用的数值编码。"""
    values = tuple(PageType)
    return values.index(page_type) + 1


def _page_type_from_code(code: int) -> PageType:
    """将页头编码恢复为页类型。"""
    values = tuple(PageType)
    if code < 1 or code > len(values):
        raise StorageError(f"未知页类型编码 {code}")
    return values[code - 1]


def encode_free_page_payload(next_page_id: int | None) -> bytes:
    """编码 FREE 页的后继页号；0 表示链尾。"""

    normalized = 0 if next_page_id is None else int(next_page_id)
    if normalized < 0:
        raise StorageError("FREE 页后继页号不能为负数")
    return FREE_PAGE_HEADER.pack(FREE_PAGE_MAGIC, normalized)


def decode_free_page_next(payload: bytes) -> int | None:
    """解析 FREE 页的后继页号；兼容旧版空 FREE 页。"""

    # === 兼容旧版空 FREE 页 ===
    # 旧版释放页没有 payload，按“链尾”解释；新版才要求固定的 MFR1 结构。
    if not payload:
        return None
    if len(payload) != FREE_PAGE_HEADER_SIZE:
        raise StorageError("FREE 页 payload 长度非法")
    magic, next_page_id = FREE_PAGE_HEADER.unpack(payload)
    if magic != FREE_PAGE_MAGIC:
        raise StorageError("FREE 页 magic 非法")
    return None if next_page_id == 0 else int(next_page_id)


@dataclass
class Page:
    """带 CRC 校验的固定大小页。

    ``lsn`` 是该页最后一次被日志记录覆盖的日志序号（Log Sequence Number）。
    未参与任何事务的页保持 0；写入路径上它用于实现"日志先于数据页落盘"的
    预写日志规则。
    """

    HEADER_SIZE: ClassVar[int] = HEADER_SIZE
    page_id: int
    page_size: int = 4096
    page_type: PageType = PageType.FREE
    payload: bytes = b""
    lsn: int = 0

    def __post_init__(self) -> None:
        """完成数据类初始化后的派生状态设置。"""
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
        self.lsn = int(self.lsn)
        if not 0 <= self.lsn <= 0xFFFFFFFF:
            raise StorageError("页 LSN 超出 4 字节范围")

    @property
    def free_space(self) -> int:
        """返回当前页可用于写入的空间大小。"""
        return self.page_size - HEADER_SIZE - len(self.payload)

    def to_bytes(self) -> bytes:
        """将对象序列化为字节串。"""
        checksum = zlib.crc32(self.payload) & 0xFFFFFFFF
        header = PAGE_HEADER.pack(
            PAGE_MAGIC,
            PAGE_VERSION,
            _page_type_code(self.page_type),
            0,
            self.page_id,
            len(self.payload),
            checksum,
            self.lsn,
        )
        return (
            header
            + self.payload
            + bytes(self.page_size - len(header) - len(self.payload))
        )

    @classmethod
    def from_bytes(cls, raw: bytes, *, page_size: int | None = None) -> "Page":
        """从字节串恢复对象实例。"""
        if page_size is None:
            page_size = len(raw)
        if len(raw) != page_size:
            raise StorageError(f"页长度错误，期望 {page_size}，实际 {len(raw)}")
        if len(raw) < HEADER_SIZE:
            raise StorageError("页内容不足以包含页头")
        (
            magic,
            version,
            type_code,
            _reserved,
            page_id,
            payload_length,
            checksum,
            lsn,
        ) = PAGE_HEADER.unpack(raw[:HEADER_SIZE])
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
        return cls(page_id, page_size, _page_type_from_code(type_code), payload, lsn)

    @classmethod
    def empty(
        cls, page_id: int, page_size: int = 4096, page_type: PageType = PageType.FREE
    ) -> "Page":
        """创建指定大小的空页。"""
        return cls(page_id=page_id, page_size=page_size, page_type=page_type)


class SlottedPage:
    """双向增长的槽式页；写入优先保留现有记录的物理 offset。"""

    def __init__(
        self,
        page_id: int,
        page_size: int = 4096,
        slots: list[bytes | None] | None = None,
        *,
        entries: list[SlotEntry] | None = None,
        _payload: bytes | None = None,
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        self.page_id = int(page_id)
        self.page_size = int(page_size)
        self.slots: list[bytes | None] = list(slots or [])
        self._entries: list[SlotEntry] = list(entries or [])
        self._payload: bytearray | None = (
            None if _payload is None else bytearray(_payload)
        )
        if self._payload is not None and len(self._payload) != self._payload_capacity:
            raise StorageError("双向槽式页负载长度错误")

    @property
    def storage_format(self) -> str:
        """返回当前页内编码名称。"""

        return "double_ended_v2"

    @property
    def _payload_capacity(self) -> int:
        """返回页载荷区域可容纳的最大字节数。"""
        return self.page_size - HEADER_SIZE

    @classmethod
    def from_page(cls, page: Page) -> "SlottedPage":
        """从完整数据库页恢复槽式页。"""
        if page.page_type is not PageType.HEAP:
            raise StorageError(f"页 {page.page_id} 不是 HEAP 页")
        if not page.payload:
            return cls(page.page_id, page.page_size)
        if page.payload.startswith(SLOTTED_MAGIC):
            return cls._from_binary(page)
        raise StorageError(f"HEAP 页 {page.page_id} 不是双向槽式格式")

    @classmethod
    def _from_binary(cls, page: Page) -> "SlottedPage":
        """从二进制页载荷解析槽目录和记录。"""
        capacity = page.page_size - HEADER_SIZE
        if len(page.payload) != capacity:
            raise StorageError(f"HEAP 页 {page.page_id} 双向槽式负载长度错误")
        if capacity > 0xFFFF:
            raise StorageError("页容量超过双向槽式格式的偏移上限")
        try:
            magic, version, _flags, slot_count, free_start, free_end = (
                SLOTTED_HEADER.unpack(page.payload[:SLOTTED_HEADER_SIZE])
            )
        except struct.error as exc:
            raise StorageError(f"HEAP 页 {page.page_id} 双向槽式页头损坏") from exc
        if magic != SLOTTED_MAGIC or version != SLOTTED_VERSION:
            raise StorageError(f"HEAP 页 {page.page_id} 双向槽式版本不支持")
        directory_end = SLOTTED_HEADER_SIZE + slot_count * SLOT_ENTRY_SIZE
        if (
            directory_end > capacity
            or free_start != directory_end
            or free_start > free_end
            or free_end > capacity
        ):
            raise StorageError(f"HEAP 页 {page.page_id} 空闲区边界非法")

        slots: list[bytes | None] = []
        entries: list[SlotEntry] = []
        ranges: list[tuple[int, int]] = []
        for slot_id in range(slot_count):
            entry_offset = SLOTTED_HEADER_SIZE + slot_id * SLOT_ENTRY_SIZE
            try:
                record_offset, record_length, flags, _reserved = SLOT_ENTRY.unpack(
                    page.payload[entry_offset : entry_offset + SLOT_ENTRY_SIZE]
                )
            except struct.error as exc:
                raise StorageError(f"HEAP 页 {page.page_id} 槽目录损坏") from exc
            deleted = bool(flags & SLOT_DELETED)
            if deleted:
                if record_length and (
                    record_offset < directory_end
                    or record_offset + record_length > capacity
                ):
                    raise StorageError(f"HEAP 页 {page.page_id} 删除槽记录范围非法")
                slots.append(None)
                entries.append(SlotEntry(record_offset, record_length, True))
                continue
            if (
                record_length <= 0
                or record_offset < free_end
                or record_offset + record_length > capacity
            ):
                raise StorageError(f"HEAP 页 {page.page_id} 槽 {slot_id} 记录范围非法")
            ranges.append((record_offset, record_offset + record_length))
            slots.append(
                bytes(page.payload[record_offset : record_offset + record_length])
            )
            entries.append(SlotEntry(record_offset, record_length, False))
        # HOW：排序结果复用一次；早先写成 `sorted(ranges)` 调两遍，全表扫描时每页白排一遍。
        ordered_ranges = sorted(ranges)
        for left, right in zip(ordered_ranges, ordered_ranges[1:]):
            if left[1] > right[0]:
                raise StorageError(f"HEAP 页 {page.page_id} 记录范围重叠")
        return cls(
            page.page_id, page.page_size, slots, entries=entries, _payload=page.payload
        )

    def _compact_binary(
        self, slots: list[bytes | None] | None = None
    ) -> SlottedPageBinary:
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
        entries: list[SlotEntry] = [SlotEntry(0, 0, True) for _ in selected]
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
            entries[slot_id] = SlotEntry(cursor, len(record), False)

        payload = bytearray(capacity)
        SLOTTED_HEADER.pack_into(
            payload,
            0,
            SLOTTED_MAGIC,
            SLOTTED_VERSION,
            0,
            len(selected),
            directory_end,
            cursor,
        )
        for slot_id, entry in enumerate(entries):
            entry_offset = SLOTTED_HEADER_SIZE + slot_id * SLOT_ENTRY_SIZE
            SLOT_ENTRY.pack_into(
                payload,
                entry_offset,
                entry.offset,
                entry.length,
                SLOT_DELETED if entry.deleted else 0,
                0,
            )
        for entry, record in zip(entries, selected, strict=True):
            if not entry.deleted and record is not None:
                payload[entry.offset : entry.offset + len(record)] = record
        return SlottedPageBinary(
            bytes(payload),
            tuple(entries),
            SlottedPageBinaryLayout(
                directory_start=SLOTTED_HEADER_SIZE,
                directory_end=directory_end,
                free_start=directory_end,
                free_end=cursor,
                record_start=cursor,
                record_end=capacity,
            ),
        )

    def _active_ranges(
        self, entries: list[SlotEntry], directory_end: int
    ) -> list[tuple[int, int]]:
        """收集未删除记录的物理范围，并检查范围是否重叠。"""
        ranges: list[tuple[int, int]] = []
        for entry in entries:
            if entry.deleted or entry.length == 0:
                continue
            record_end = entry.offset + entry.length
            if entry.offset < directory_end or record_end > self._payload_capacity:
                raise StorageError("槽式页记录范围非法")
            ranges.append((entry.offset, record_end))
        ranges.sort()
        for left, right in zip(ranges, ranges[1:]):
            if left[1] > right[0]:
                raise StorageError("槽式页记录范围重叠")
        return ranges

    def _free_extents(
        self, entries: list[SlotEntry], directory_end: int
    ) -> list[tuple[int, int]]:
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

    def _current_binary(
        self,
    ) -> SlottedPageBinary:
        """序列化当前物理布局，不主动压缩已有记录。"""

        if self._payload is None or len(self._entries) != len(self.slots):
            # WHY：物理 payload 不存在或槽目录与逻辑槽列表不同步时，无法安全保留当前布局；
            # 先重建紧凑布局，确保槽目录、记录 offset 和空闲区边界一致。
            return self._compact_binary()
        capacity = self._payload_capacity
        directory_end = SLOTTED_HEADER_SIZE + len(self.slots) * SLOT_ENTRY_SIZE
        if directory_end > capacity:
            raise StorageError("槽目录超过页容量")
        entries = list(self._entries)
        ranges: list[tuple[int, int]] = []
        for slot_id, entry in enumerate(entries):
            if entry.length:
                if (
                    entry.offset < directory_end
                    or entry.offset + entry.length > capacity
                ):
                    raise StorageError(f"槽 {slot_id} 记录范围非法")
                ranges.append((entry.offset, entry.offset + entry.length))
            if not entry.deleted:
                record = self.slots[slot_id]
                if record is None or len(record) != entry.length:
                    raise StorageError(f"槽 {slot_id} 记录长度与槽目录不一致")
        for left, right in zip(sorted(ranges), sorted(ranges)[1:]):
            if left[1] > right[0]:
                raise StorageError("槽式页记录范围重叠")

        payload = bytearray(self._payload)
        live_starts: list[int] = []
        for slot_id, entry in enumerate(entries):
            if not entry.deleted:
                record = self.slots[slot_id]
                if record is None:
                    raise StorageError(f"槽 {slot_id} 缺少记录")
                payload[entry.offset : entry.offset + entry.length] = record
                live_starts.append(entry.offset)
            entry_offset = SLOTTED_HEADER_SIZE + slot_id * SLOT_ENTRY_SIZE
            SLOT_ENTRY.pack_into(
                payload,
                entry_offset,
                entry.offset,
                entry.length,
                SLOT_DELETED if entry.deleted else 0,
                0,
            )
        free_end = min(live_starts, default=capacity)
        SLOTTED_HEADER.pack_into(
            payload,
            0,
            SLOTTED_MAGIC,
            SLOTTED_VERSION,
            0,
            len(entries),
            directory_end,
            free_end,
        )
        return SlottedPageBinary(
            bytes(payload),
            tuple(entries),
            SlottedPageBinaryLayout(
                directory_start=SLOTTED_HEADER_SIZE,
                directory_end=directory_end,
                free_start=directory_end,
                free_end=free_end,
                record_start=free_end,
                record_end=capacity,
            ),
        )

    def _build_binary(
        self, slots: list[bytes | None] | None = None
    ) -> SlottedPageBinary:
        """根据当前或候选槽状态构造二进制页布局。"""
        # 带候选 slots 的调用按候选布局试算，不改变当前页的物理记录位置。
        if slots is not None:
            return self._compact_binary(slots)
        return self._current_binary()

    def _set_compact(self, slots: list[bytes | None] | None = None) -> None:
        """压缩槽式页并更新当前记录布局。"""
        selected = list(self.slots if slots is None else slots)
        # WHY：压缩结果必须同时更新逻辑槽、槽目录和二进制 payload；先在局部构建，
        # 只有完整布局成功后再替换实例状态，避免失败时留下相互不一致的半成品。
        binary = self._compact_binary(selected)
        self.slots = selected
        self._payload = bytearray(binary.payload)
        self._entries = list(binary.entries)

    def _ensure_binary(self) -> None:
        """确保页已拥有可用的二进制布局缓存。"""
        if self._payload is None or len(self._entries) != len(self.slots):
            self._set_compact()

    def _allocate_record(
        self,
        record_length: int,
        directory_end: int,
        entries: list[SlotEntry],
    ) -> int | None:
        """返回可以分配的槽的offset"""
        if record_length <= 0 or record_length > 0xFFFF:
            raise StorageError("记录长度非法")
        for start, end in reversed(self._free_extents(entries, directory_end)):
            if end - start >= record_length:
                return end - record_length
        return None

    @staticmethod
    def _clear_deleted_overlaps(
        entries: list[SlotEntry], record_offset: int, record_length: int
    ) -> None:
        """清理被新记录覆盖的删除槽旧范围，避免目录范围与新记录重叠。"""

        record_end = record_offset + record_length
        for slot_id, entry in enumerate(entries):
            if not entry.deleted or entry.length == 0:
                continue
            old_end = entry.offset + entry.length
            if entry.offset < record_end and record_offset < old_end:
                entries[slot_id] = SlotEntry(0, 0, True)

    def _place_record(
        self, slot_id: int, record: bytes, *, append_slot: bool = False
    ) -> int:
        """把记录写入指定槽并更新其物理范围。"""
        self._ensure_binary()
        if not isinstance(record, bytes):
            raise TypeError("record 必须是 bytes")
        old_entries = list(self._entries)
        if append_slot:
            old_entries.append(SlotEntry(0, 0, False))
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
            # WHY：只有没有可用连续片段时才搬移整页旧记录，正常路径保留现有 offset，
            # 避免无谓移动记录。压缩使用包含待插入记录的 candidate，一次重建目录和记录区；
            # 若压缩后仍放不下，异常交给 TableHeap 尝试其他页。
            self._set_compact(candidate)
            return slot_id

        if self._payload is None:
            raise StorageError("槽式页负载尚未初始化")
        self._clear_deleted_overlaps(old_entries, offset, len(record))
        if not append_slot:
            old_entry = old_entries[slot_id]
            if old_entry.deleted and old_entry.length:
                self._payload[
                    old_entry.offset : old_entry.offset + old_entry.length
                ] = b"\x00" * old_entry.length
        self.slots = candidate
        self._entries = old_entries
        self._entries[slot_id] = SlotEntry(offset, len(record), False)
        self._payload[offset : offset + len(record)] = record
        return slot_id

    @property
    def free_space(self) -> int:
        """返回当前页可用于写入的空间大小。"""
        self._ensure_binary()
        directory_end = SLOTTED_HEADER_SIZE + len(self._entries) * SLOT_ENTRY_SIZE
        return sum(
            end - start
            for start, end in self._free_extents(self._entries, directory_end)
        )

    def insert(self, record: bytes) -> int:
        """向页内插入记录并返回槽编号。"""
        # WHY：页层只处理已经编码好的字节记录和槽目录，不依赖表结构或索引元数据；
        # 索引入口由上层在拿到最终 RowId 后维护，避免存储层反向依赖执行层。
        if not isinstance(record, bytes):
            raise TypeError("record 必须是 bytes")
        for index, value in enumerate(self.slots):
            if value is None:
                return self._place_record(index, record)
        return self._place_record(len(self.slots), record, append_slot=True)

    def get(self, slot_id: int) -> bytes | None:
        """读取指定键或槽中的对象；找不到时返回空值。"""
        if slot_id < 0 or slot_id >= len(self.slots):
            raise StorageError(f"槽号 {slot_id} 越界")
        return self.slots[slot_id]

    def update(self, slot_id: int, record: bytes) -> None:
        """更新槽内记录，并维护页内物理布局。"""
        # WHY：变长记录可能原地覆盖、迁移到其他空闲片段或触发页内压缩；
        # 更新过程保持槽编号不变，索引是否需要重建由上层根据旧值和新值处理。
        if slot_id < 0 or slot_id >= len(self.slots) or self.slots[slot_id] is None:
            raise StorageError(f"槽号 {slot_id} 不存在")
        if not isinstance(record, bytes):
            raise TypeError("record 必须是 bytes")
        self._ensure_binary()
        old_entry = self._entries[slot_id]
        old_offset = old_entry.offset
        old_length = old_entry.length
        if len(record) <= old_length:
            if self._payload is None:
                raise StorageError("槽式页负载尚未初始化")
            self._payload[old_offset : old_offset + old_length] = record + b"\x00" * (
                old_length - len(record)
            )
            self.slots[slot_id] = record
            self._entries[slot_id] = SlotEntry(old_offset, len(record), False)
            return

        entries_for_space = list(self._entries)
        entries_for_space[slot_id] = SlotEntry(old_offset, old_length, True)
        offset = self._allocate_record(
            len(record),
            SLOTTED_HEADER_SIZE + len(self._entries) * SLOT_ENTRY_SIZE,
            entries_for_space,
        )
        candidate = list(self.slots)
        candidate[slot_id] = record
        if offset is None:
            self._set_compact(candidate)
            return
        if self._payload is None:
            raise StorageError("槽式页负载尚未初始化")
        self._clear_deleted_overlaps(self._entries, offset, len(record))
        self._payload[old_offset : old_offset + old_length] = b"\x00" * old_length
        self._payload[offset : offset + len(record)] = record
        self.slots[slot_id] = record
        self._entries[slot_id] = SlotEntry(offset, len(record), False)

    def delete(self, slot_id: int) -> None:
        """删除指定槽中的记录并保留槽编号。"""
        if slot_id < 0 or slot_id >= len(self.slots):
            raise StorageError(f"槽号 {slot_id} 越界")
        if self.slots[slot_id] is None:
            return
        self._ensure_binary()
        entry = self._entries[slot_id]
        if self._payload is not None and entry.length:
            self._payload[entry.offset : entry.offset + entry.length] = (
                b"\x00" * entry.length
            )
        self.slots[slot_id] = None
        self._entries[slot_id] = SlotEntry(entry.offset, entry.length, True)

    def slot_layout(self) -> list[SlotLocation]:
        """【前端特供】返回每个槽的物理范围，offset 相对于完整数据库页。"""

        binary = self._build_binary()
        return [
            SlotLocation(
                slot_id=slot_id,
                offset=0 if entry.deleted else HEADER_SIZE + entry.offset,
                length=0 if entry.deleted else entry.length,
                deleted=entry.deleted,
            )
            for slot_id, entry in enumerate(binary.entries)
        ]

    def layout_info(self) -> SlottedPageLayoutInfo:
        """【前端特供】返回工作台绘制双向页布局所需的带名结构。"""

        binary = self._build_binary()
        entries = binary.entries
        layout = binary.layout
        page_offset = HEADER_SIZE
        slot_directory_start = page_offset + layout.directory_start
        slot_directory_end = page_offset + layout.directory_end
        free_regions = [
            PageRegion(
                start=page_offset + start,
                end=page_offset + end,
                size=end - start,
                direction="free",
            )
            for start, end in self._free_extents(list(entries), layout.directory_end)
            if end > start
        ]
        free_start = page_offset + layout.free_start
        free_end = page_offset + layout.free_end
        record_start = page_offset + layout.record_start
        record_end = page_offset + layout.record_end
        return SlottedPageLayoutInfo(
            format=self.storage_format,
            physical=True,
            payload_offset=page_offset,
            payload_capacity=self._payload_capacity,
            inner_header_size=SLOTTED_HEADER_SIZE,
            slot_entry_size=SLOT_ENTRY_SIZE,
            slot_count=len(entries),
            slot_directory=PageRegion(
                start=slot_directory_start,
                end=slot_directory_end,
                size=slot_directory_end - slot_directory_start,
                direction="forward",
            ),
            free_region=PageRegion(
                start=free_start,
                end=free_end,
                size=max(0, free_end - free_start),
                direction="free",
            ),
            free_regions=tuple(free_regions),
            record_region=PageRegion(
                start=record_start,
                end=record_end,
                size=max(0, record_end - record_start),
                direction="backward",
            ),
            slots=tuple(
                SlotLocation(
                    slot_id=slot_id,
                    offset=0 if entry.deleted else HEADER_SIZE + entry.offset,
                    length=0 if entry.deleted else entry.length,
                    deleted=entry.deleted,
                    directory_offset=slot_directory_start
                    + slot_id * SLOT_ENTRY_SIZE,
                )
                for slot_id, entry in enumerate(entries)
            ),
        )

    def layout_metadata(self) -> dict[str, object]:
        """【前端特供】返回工作台协议使用的 JSON 映射；内部代码使用 :meth:`layout_info`。"""

        return self.layout_info().to_dict()

    def to_page(self) -> Page:
        """将槽式页转换为完整数据库页。"""
        binary = self._build_binary()
        self._payload = bytearray(binary.payload)
        self._entries = list(binary.entries)
        return Page(self.page_id, self.page_size, PageType.HEAP, binary.payload)

    def live_slots(self) -> tuple[LiveSlot, ...]:
        """遍历当前页中仍然有效的记录槽。"""
        return tuple(
            LiveSlot(index, value)
            for index, value in enumerate(self.slots)
            if value is not None
        )

    def live_count(self) -> int:
        """统计活槽个数，不切出记录内容。

        HOW：只依赖已解析的槽目录项，不触碰记录区，因此调用方无需为每一行付出
        JSON 解码成本。供 ``COUNT(*)`` 这类只关心行数的扫描使用。
        """

        return sum(1 for value in self.slots if value is not None)
