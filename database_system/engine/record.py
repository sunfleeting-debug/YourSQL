"""记录（Row）与页（Page）之间的序列化 / 反序列化。

编码格式（紧凑二进制，定长头 + 变长体）：
    row    := uint16 count | value × count
    value  := tag(1B) | payload
        tag 0 NULL    : 无 payload
        tag 1 INT     : int32 (4B)
        tag 2 VARCHAR : uint16 length + UTF-8 字节
        tag 3 BOOL    : uint8 (0/1)
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
    if value is None:
        return bytes([TAG_NULL])
    if isinstance(value, bool):
        return bytes([TAG_BOOL, 1 if value else 0])
    if isinstance(value, int):
        try:
            return bytes([TAG_INT]) + struct.pack("<i", value)
        except struct.error:
            raise ExecutionError(f"integer out of range: {value}")
    if isinstance(value, str):
        raw = value.encode("utf-8")
        if len(raw) > 65535:
            raise ExecutionError("string value too long (max 65535 bytes)")
        return bytes([TAG_STR]) + struct.pack("<H", len(raw)) + raw
    raise ExecutionError(f"unsupported value type: {type(value).__name__}")


def decode_value(buf, offset: int):
    """返回 (value, 新偏移)。"""
    tag = buf[offset]
    offset += 1
    if tag == TAG_NULL:
        return None, offset
    if tag == TAG_INT:
        (value,) = struct.unpack_from("<i", buf, offset)
        return value, offset + 4
    if tag == TAG_BOOL:
        return bool(buf[offset]), offset + 1
    if tag == TAG_STR:
        (length,) = struct.unpack_from("<H", buf, offset)
        offset += 2
        raw = buf[offset : offset + length]
        return raw.decode("utf-8"), offset + length
    raise ExecutionError(f"unknown value tag {tag}")


def encode_row(values) -> bytes:
    if len(values) > 65535:
        raise ExecutionError("too many columns in one row")
    out = bytearray(struct.pack("<H", len(values)))
    for v in values:
        out.extend(encode_value(v))
    return bytes(out)


def decode_row(data) -> list:
    (count,) = struct.unpack_from("<H", data, 0)
    offset = 2
    values = []
    for _ in range(count):
        value, offset = decode_value(data, offset)
        values.append(value)
    return values


def row_size(values) -> int:
    return len(encode_row(values))


MAX_ROW_SIZE = PAGE_SIZE - 64  # 预留页头与槽目录空间
