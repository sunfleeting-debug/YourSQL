"""Row 与页内二进制记录之间的安全转换。

页面层只保存 bytes，不理解 SQL 类型。这里定义一个很小的自描述编码：

    row   := uint16 count | value * count
    value := tag | payload

每个字段都有类型标签，因此解码不依赖当前 Catalog，也不会使用 pickle。
"""

from __future__ import annotations

import struct

from database_system.utils.constants import PAGE_SIZE
from database_system.utils.errors import ExecutionError

TAG_NULL = 0
TAG_INT = 1
TAG_STR = 2
TAG_BOOL = 3


def encode_value(value) -> bytes:
    """编码一个 Python 值。"""
    if value is None:
        return bytes([TAG_NULL])
    if isinstance(value, bool):
        return bytes([TAG_BOOL, 1 if value else 0])
    if isinstance(value, int):
        try:
            return bytes([TAG_INT]) + struct.pack("<i", value)
        except struct.error as exc:
            raise ExecutionError(f"integer out of range: {value}") from exc
    if isinstance(value, str):
        try:
            raw = value.encode("utf-8")
        except UnicodeError as exc:
            raise ExecutionError("string cannot be encoded as UTF-8") from exc
        if len(raw) > 65535:
            raise ExecutionError("string value too long (max 65535 bytes)")
        return bytes([TAG_STR]) + struct.pack("<H", len(raw)) + raw
    raise ExecutionError(f"unsupported value type: {type(value).__name__}")


def _take(buf: bytes, offset: int, size: int) -> tuple[bytes, int]:
    """安全读取一段字节，统一把截断数据转换成 ExecutionError。"""
    if size < 0 or offset < 0 or offset + size > len(buf):
        raise ExecutionError("truncated binary row")
    return buf[offset : offset + size], offset + size


def decode_value(buf: bytes, offset: int) -> tuple[object, int]:
    """解码一个值，返回 ``(value, next_offset)``。"""
    raw_tag, offset = _take(buf, offset, 1)
    tag = raw_tag[0]
    if tag == TAG_NULL:
        return None, offset
    if tag == TAG_INT:
        raw, offset = _take(buf, offset, 4)
        try:
            return struct.unpack("<i", raw)[0], offset
        except struct.error as exc:
            raise ExecutionError("invalid INT payload") from exc
    if tag == TAG_BOOL:
        raw, offset = _take(buf, offset, 1)
        if raw[0] not in (0, 1):
            raise ExecutionError("invalid BOOL payload")
        return bool(raw[0]), offset
    if tag == TAG_STR:
        raw_length, offset = _take(buf, offset, 2)
        length = struct.unpack("<H", raw_length)[0]
        raw, offset = _take(buf, offset, length)
        try:
            return raw.decode("utf-8"), offset
        except UnicodeError as exc:
            raise ExecutionError("invalid UTF-8 string payload") from exc
    raise ExecutionError(f"unknown value tag {tag}")


def encode_row(values) -> bytes:
    """将一行编码为自描述二进制记录。"""
    if len(values) > 65535:
        raise ExecutionError("too many columns in one row")
    out = bytearray(struct.pack("<H", len(values)))
    for value in values:
        out.extend(encode_value(value))
    return bytes(out)


def decode_row(data: bytes) -> list:
    """严格解码一行，拒绝截断、非法标签和尾部多余字节。"""
    raw_count, offset = _take(data, 0, 2)
    count = struct.unpack("<H", raw_count)[0]
    values = []
    for _ in range(count):
        value, offset = decode_value(data, offset)
        values.append(value)
    if offset != len(data):
        raise ExecutionError("trailing bytes after binary row")
    return values


def row_size(values) -> int:
    return len(encode_row(values))


# 留出页头、槽位和少量安全空间，避免记录刚好顶到边界。
MAX_ROW_SIZE = PAGE_SIZE - 64
