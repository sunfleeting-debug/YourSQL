"""YourSQL 内部 payload 编解码器。"""

from __future__ import annotations

import math
import struct
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Literal

from yoursql.common.contracts import JsonValue

PayloadCodecName = Literal["json", "manual"]
MANUAL_PAYLOAD_MAGIC = b"YSPL"
MANUAL_CONTENT_TYPE = "application/x-yoursql; version=1"


class PayloadCodecError(ValueError):
    """payload 编码或解码失败。"""


class PayloadCodec:
    """JSON-like payload 编解码器接口。"""

    name: PayloadCodecName

    def encode(self, value: object) -> bytes:
        """编码值。"""

        raise NotImplementedError

    def decode(self, payload: bytes) -> object:
        """解码值。"""

        raise NotImplementedError


class JsonPayloadCodec(PayloadCodec):
    """兼容现有文件的 JSON 编解码器；只在实际使用时导入 json。"""

    name: PayloadCodecName = "json"

    _DECIMAL_KEY = "$decimal"
    _LEGACY_DECIMAL_KEYS = frozenset({"__yoursql_decimal__"})

    def encode(self, value: object) -> bytes:
        """编码紧凑 UTF-8 JSON。"""

        import json

        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
                allow_nan=False,
                default=self._default,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise PayloadCodecError("值无法编码为 JSON payload") from exc

    def decode(self, payload: bytes) -> object:
        """解码 UTF-8 JSON。"""

        import json

        try:
            return json.loads(payload.decode("utf-8"), object_hook=self._object_hook)
        except (UnicodeDecodeError, TypeError, ValueError) as exc:
            raise PayloadCodecError("JSON payload 损坏") from exc

    @classmethod
    def _default(cls, value: object) -> object:
        """把内部定点数编码成带标签的 JSON 对象，避免落盘时折成 float。"""

        if isinstance(value, Decimal):
            return {cls._DECIMAL_KEY: str(value)}
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

    @classmethod
    def _object_hook(cls, value: dict[str, object]) -> object:
        """还原 JSON payload 中的定点数标签。"""

        if len(value) != 1:
            return value
        marker = next(
            (
                key
                for key in (cls._DECIMAL_KEY, *cls._LEGACY_DECIMAL_KEYS)
                if key in value
            ),
            None,
        )
        if marker is None:
            return value
        raw = value[marker]
        if not isinstance(raw, str):
            raise PayloadCodecError("Decimal payload 标签值不是字符串")
        try:
            return Decimal(raw)
        except InvalidOperation as exc:
            raise PayloadCodecError("Decimal payload 标签值无效") from exc


class _Writer:
    def __init__(self) -> None:
        self.data = bytearray(MANUAL_PAYLOAD_MAGIC)

    def u32(self, value: int) -> None:
        if not 0 <= value <= 0xFFFFFFFF:
            raise PayloadCodecError("manual payload 长度超出 uint32")
        self.data.extend(struct.pack("<I", value))

    def varuint(self, value: int) -> None:
        if value < 0:
            raise PayloadCodecError("manual payload 变长整数不能为负")
        while value >= 0x80:
            self.data.append((value & 0x7F) | 0x80)
            value >>= 7
        self.data.append(value)


class _Reader:
    def __init__(self, payload: bytes) -> None:
        if not payload.startswith(MANUAL_PAYLOAD_MAGIC):
            raise PayloadCodecError("manual payload 魔数错误")
        self.payload = payload
        self.offset = len(MANUAL_PAYLOAD_MAGIC)

    def take(self, length: int) -> bytes:
        if length < 0 or self.offset + length > len(self.payload):
            raise PayloadCodecError("manual payload 截断")
        value = self.payload[self.offset : self.offset + length]
        self.offset += length
        return value

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def varuint(self) -> int:
        value = 0
        shift = 0
        for _ in range(1024):
            item = self.take(1)[0]
            value |= (item & 0x7F) << shift
            if not item & 0x80:
                return value
            shift += 7
        raise PayloadCodecError("manual payload 变长整数过长")


class ManualPayloadCodec(PayloadCodec):
    """手写 TLV；标签 8 专门保存 Decimal 的十进制定点文本。"""

    name: PayloadCodecName = "manual"
    max_depth = 64
    max_items = 1_000_000

    def encode(self, value: object) -> bytes:
        """编码 JSON-like 值。"""

        writer = _Writer()
        self._write(writer, value, 0)
        return bytes(writer.data)

    def decode(self, payload: bytes) -> object:
        """严格解码并拒绝尾部字节。"""

        reader = _Reader(payload)
        value = self._read(reader, 0)
        if reader.offset != len(payload):
            raise PayloadCodecError("manual payload 含尾部字节")
        return value

    def _write(self, writer: _Writer, value: object, depth: int) -> None:
        if depth > self.max_depth:
            raise PayloadCodecError("manual payload 嵌套过深")
        if value is None:
            writer.data.append(0)
        elif value is False:
            writer.data.append(1)
        elif value is True:
            writer.data.append(2)
        elif isinstance(value, int):
            writer.data.append(3)
            writer.varuint(value * 2 if value >= 0 else -value * 2 - 1)
        elif isinstance(value, Decimal):
            writer.data.append(8)
            encoded = str(value).encode("ascii")
            writer.u32(len(encoded))
            writer.data.extend(encoded)
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise PayloadCodecError("manual payload 不支持非有限浮点数")
            writer.data.append(4)
            writer.data.extend(struct.pack("<d", value))
        elif isinstance(value, str):
            writer.data.append(5)
            encoded = value.encode("utf-8")
            writer.u32(len(encoded))
            writer.data.extend(encoded)
        elif isinstance(value, Mapping):
            self._write_mapping(writer, value, depth)
        elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
            if len(value) > self.max_items:
                raise PayloadCodecError("manual payload 数组过长")
            writer.data.append(6)
            writer.u32(len(value))
            for item in value:
                self._write(writer, item, depth + 1)
        else:
            raise PayloadCodecError(f"manual payload 不支持 {type(value).__name__}")

    def _write_mapping(self, writer: _Writer, value: Mapping[object, object], depth: int) -> None:
        keys = list(value)
        if len(keys) > self.max_items or any(not isinstance(key, str) for key in keys):
            raise PayloadCodecError("manual payload 对象键必须是字符串")
        writer.data.append(7)
        writer.u32(len(keys))
        for key in sorted(keys):
            self._write(writer, key, depth + 1)
            self._write(writer, value[key], depth + 1)

    def _read(self, reader: _Reader, depth: int) -> JsonValue:
        if depth > self.max_depth:
            raise PayloadCodecError("manual payload 嵌套过深")
        tag = reader.take(1)[0]
        if tag == 0:
            return None
        if tag == 1:
            return False
        if tag == 2:
            return True
        if tag == 3:
            encoded = reader.varuint()
            return encoded // 2 if encoded % 2 == 0 else -(encoded // 2) - 1
        if tag == 4:
            value = struct.unpack("<d", reader.take(8))[0]
            if not math.isfinite(value):
                raise PayloadCodecError("manual payload 含非有限浮点数")
            return value
        if tag == 5:
            try:
                return reader.take(reader.u32()).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise PayloadCodecError("manual payload 字符串不是 UTF-8") from exc
        if tag == 8:
            try:
                return Decimal(reader.take(reader.u32()).decode("ascii"))
            except (UnicodeDecodeError, InvalidOperation) as exc:
                raise PayloadCodecError("manual payload Decimal 无效") from exc
        if tag == 6:
            count = reader.u32()
            if count > self.max_items:
                raise PayloadCodecError("manual payload 数组过长")
            return [self._read(reader, depth + 1) for _ in range(count)]
        if tag == 7:
            count = reader.u32()
            if count > self.max_items:
                raise PayloadCodecError("manual payload 对象过长")
            result: dict[str, JsonValue] = {}
            for _ in range(count):
                key = self._read(reader, depth + 1)
                if not isinstance(key, str) or key in result:
                    raise PayloadCodecError("manual payload 对象键无效或重复")
                result[key] = self._read(reader, depth + 1)
            return result
        raise PayloadCodecError(f"manual payload 标签 {tag} 未知")


def payload_codec(name: str) -> PayloadCodec:
    """按配置名取得编解码器。"""

    normalized = name.strip().lower()
    if normalized == "json":
        return JsonPayloadCodec()
    if normalized == "manual":
        return ManualPayloadCodec()
    raise ValueError("payload_codec 只能是 json 或 manual")


def validate_payload_codec(name: str) -> PayloadCodecName:
    """校验并规范化 payload 编码名。"""

    normalized = name.strip().lower()
    if normalized not in {"json", "manual"}:
        raise ValueError("payload_codec 只能是 json 或 manual")
    return normalized  # type: ignore[return-value]


def decode_payload(
    payload: bytes, preferred: PayloadCodec | str | None = None
) -> tuple[object, PayloadCodec]:
    """按首选编码解码，并回退读取另一种格式的旧 payload。"""

    selected = payload_codec(preferred) if isinstance(preferred, str) else preferred
    candidates = [selected] if selected is not None else []
    for codec in (payload_codec("json"), payload_codec("manual")):
        if codec.name not in {item.name for item in candidates}:
            candidates.append(codec)
    for codec in candidates:
        try:
            return codec.decode(payload), codec
        except (PayloadCodecError, UnicodeError, TypeError, ValueError):
            continue
    raise PayloadCodecError("payload 无法用 JSON 或 manual 解码")


__all__ = [
    "MANUAL_CONTENT_TYPE",
    "MANUAL_PAYLOAD_MAGIC",
    "JsonPayloadCodec",
    "ManualPayloadCodec",
    "PayloadCodec",
    "PayloadCodecError",
    "PayloadCodecName",
    "decode_payload",
    "payload_codec",
    "validate_payload_codec",
]
