"""索引页 payload 编码和容量估算。"""

from __future__ import annotations

from collections.abc import Iterable

from ...common.codec import PayloadCodec
from ...common.codec import payload_codec as get_payload_codec
from ...common.types import RowId
from .ordering import Key

INDEX_MAGIC = b"MBIX"
INDEX_VERSION = 1
INDEX_LINK_RESERVE = 64


def _entry_payload_size(
    key: Key,
    row_id: RowId,
    payload: Iterable[object] = (),
    codec: PayloadCodec | str = "json",
) -> int:
    """单条目在叶页 JSON 载荷里的字节数（含分隔逗号）；覆盖列值一并计入。

    HOW：`bulk_load` 用它做增量容量核算，避免“每行都序列化整块候选节点”。
    """

    entry: list[object] = [list(key), [int(row_id.page_id), row_id.slot_id]]
    payload_values = list(payload)
    if payload_values:
        entry.append(payload_values)
    selected = codec if isinstance(codec, PayloadCodec) else get_payload_codec(codec)
    return len(selected.encode(entry)) + 1


def _leaf_payload_overhead(codec: PayloadCodec | str = "json") -> int:
    """叶页载荷里与条目无关的固定开销（取上界，宁宓勿溢）。"""

    selected = codec if isinstance(codec, PayloadCodec) else get_payload_codec(codec)
    return len(INDEX_MAGIC) + len(
        selected.encode(
            {
                "version": 99,
                "kind": "leaf",
                "level": 0,
                "parent": 1234567,
                "next": 1234567,
                "prev": 1234567,
                "keys": [],
                "row_ids": [],
            }
        )
    )


def _internal_payload_entry_size(
    key: Key, child_id: int, codec: PayloadCodec | str = "json"
) -> int:
    """内部页中“一个子页 + 对应分隔键”的字节数上界。"""

    selected = codec if isinstance(codec, PayloadCodec) else get_payload_codec(codec)
    key_size = len(selected.encode(list(key))) + 1
    return key_size + len(str(int(child_id))) + 4
