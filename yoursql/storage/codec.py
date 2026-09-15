"""落盘 JSON 里的值编解码。

WHY：行记录与索引条目都以 JSON 落盘，而 JSON 没有定点数。DECIMAL 必须无损
往返，否则 Q6 那类 ``BETWEEN 0.05 AND 0.07`` 的边界会在落盘/回读之间漂移。

HOW：只在真的出现 Decimal 时挂上 ``object_hook`` 标记还原器，普通行直接走
纯 C 扫描；解码统一用 ``JSONDecoder.raw_decode``，绕开 ``json.loads`` 每次
都要跑的 Python 包装层（两次 ``WHITESPACE.match`` 正则调用）。
"""

from __future__ import annotations

import json
from decimal import Decimal, InvalidOperation

from yoursql.common.errors import StorageError

# HOW：键名带前缀，避免与用户可见的字符串内容混淆（行的元素都是标量，
# 不会出现 dict，因此这个标记在行/键的取值位置上不会有歧义）。
DECIMAL_MARKER = "$decimal"
LEGACY_DECIMAL_MARKERS = frozenset({"__yoursql_decimal__"})
_DECIMAL_MARKERS = frozenset({DECIMAL_MARKER, *LEGACY_DECIMAL_MARKERS})


def _default(value: object) -> object:
    if isinstance(value, Decimal):
        return {DECIMAL_MARKER: str(value)}
    raise TypeError(f"不能把 {type(value).__name__} 编码进 JSON 记录")


def dumps(values: object) -> bytes:
    """把行/索引键编码成紧凑 JSON 字节。"""

    return json.dumps(
        values,
        ensure_ascii=False,
        separators=(",", ":"),
        default=_default,
    ).encode("utf-8")


def _decimal_hook(mapping: dict) -> object:
    """把 ``{"$decimal": "..."}`` 直接还原成 Decimal，其余对象原样返回。

    WHY：JSON 扫描器只在遇到对象时回调，普通行/键里没有任何对象，因此这条
    路径不会产生逐元素遍历。早先的实现是在 ``loads`` 之后用递归 ``_revive``
    整体重写一遍结构，实测占 60,175 行 lineitem 全表扫描耗时的约 50%
    （代码对象级开销从 72 万次调用降到 24 万次）。
    """

    if len(mapping) == 1:
        marker = next((key for key in _DECIMAL_MARKERS if key in mapping), None)
        if marker is not None:
            text = mapping[marker]
            try:
                return Decimal(text)
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise StorageError(f"记录里的定点数损坏: {text!r}") from exc
    return mapping


# HOW：解码器实例复用，避免每次解码都新建 JSONDecoder（其 __init__ 会重新
# 绑定 scan_once 等属性）。``raw_decode`` 返回 ``(对象, 结束位置)``，比
# ``json.loads`` 少了包装层的正则跳空白与长度校验，实测快 1.5–1.8 倍。
_PLAIN_DECODER = json.JSONDecoder()
_DECIMAL_DECODER = json.JSONDecoder(object_hook=_decimal_hook)


def _decode_text(text: str) -> object:
    decoder = (
        _DECIMAL_DECODER
        if any(marker in text for marker in _DECIMAL_MARKERS)
        else _PLAIN_DECODER
    )
    try:
        value, end = decoder.raw_decode(text)
    except ValueError as exc:
        raise StorageError("记录 JSON 损坏") from exc
    # HOW：热路径只做一次整数比较；``json.loads`` 允许尾部空白，这里保持一致，
    # 但只在长度不符时才付出 strip 的代价。
    if end != len(text) and text[end:].strip():
        raise StorageError("记录 JSON 损坏")
    return value


def loads(raw: bytes) -> object:
    """解码 JSON 字节，并把 DECIMAL 标记还原成 Decimal。"""

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StorageError("记录 JSON 损坏") from exc
    return _decode_text(text)


def dump_strings(values: object) -> str:
    """编码成字符串（索引签名、诊断输出等需要文本时使用）。"""

    return json.dumps(
        values, ensure_ascii=False, separators=(",", ":"), default=_default
    )


def load_strings(text: str) -> object:
    """解码字符串形式，并还原 DECIMAL 标记。"""

    return _decode_text(text)


__all__ = [
    "DECIMAL_MARKER",
    "dump_strings",
    "dumps",
    "load_strings",
    "loads",
]
