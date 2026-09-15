"""存储热路径的编解码与索引节点校验回归。

WHY：单表扫描和索引查找都压在 ``codec.loads`` 与 ``_IndexNode.from_page`` 上，
这两个函数同时承担"正确无损"和"够快"两个目标，改动很容易只顾一头。这里把
两条底线固定住：

1. 解码仍然无损——DECIMAL 精确、尾部垃圾要报错、结构损坏要报错；
2. 校验仍然分层——读路径上的 O(1) 结构自检不能放过会越界读的损坏，
   排序这类 O(n) 不变量留在 ``verify=True`` 与 ``validate()``。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from yoursql.common.codec import JsonPayloadCodec
from yoursql.common.errors import StorageError
from yoursql.common.types import PageId, RowId
from yoursql.storage import Page, PageType, TableHeap, codec
from yoursql.storage.index import INDEX_MAGIC, _IndexNode
from yoursql.storage.index.inspection import index_page_info


def test_codec_round_trip_is_lossless_for_typed_values() -> None:
    row = [1, "text", Decimal("0.07"), Decimal("1193053.2253"), 2.5, True, None]
    encoded = codec.dumps(row)
    assert codec.loads(encoded) == row
    assert codec.loads(encoded)[2] == Decimal("0.07")
    # 定点数必须原样保留文本，不能被转成 float 而丢掉精度。
    assert str(codec.loads(encoded)[3]) == "1193053.2253"


def test_codec_decodes_plain_rows_without_decimal_marker() -> None:
    assert codec.loads(b'[1,"a",2.5,null,true]') == [1, "a", 2.5, None, True]
    assert codec.load_strings('[1,"a"]') == [1, "a"]


def test_codec_reads_legacy_decimal_marker() -> None:
    assert codec.loads(b'[{"__yoursql_decimal__":"0.07"}]') == [Decimal("0.07")]


def test_common_json_codec_reads_both_decimal_markers() -> None:
    payload_codec = JsonPayloadCodec()
    assert payload_codec.decode(b'[{"$decimal":"0.07"}]') == [Decimal("0.07")]
    assert payload_codec.decode(b'[{"__yoursql_decimal__":"0.08"}]') == [
        Decimal("0.08")
    ]
    assert TableHeap._decode(b'[{"$decimal":"0.09"}]') == (Decimal("0.09"),)


def test_codec_rejects_corrupt_and_trailing_payloads() -> None:
    with pytest.raises(StorageError):
        codec.loads(b"[1,2")
    with pytest.raises(StorageError):
        codec.loads(b"[1,2] garbage")
    with pytest.raises(StorageError):
        codec.loads(b'[{"$decimal":"not-a-number"}]')
    # 尾部空白与 json.loads 行为一致，仍然接受。
    assert codec.loads(b"[1,2] ") == [1, 2]


def _index_page(payload: bytes, page_id: int = 12) -> Page:
    return Page(page_id, 4096, PageType.INDEX, INDEX_MAGIC + payload)


def _leaf_payload(keys: list[tuple], row_ids: list[tuple]) -> bytes:
    return codec.dumps(
        {
            "version": 1,
            "kind": "leaf",
            "level": 0,
            "parent": None,
            "next": None,
            "prev": None,
            "keys": [list(key) for key in keys],
            "row_ids": [list(row_id) for row_id in row_ids],
        }
    )


def test_index_node_read_path_keeps_structural_checks() -> None:
    """读路径省略 O(n) 排序检查，但"数量不一致"这类会越界读的损坏必须挡住。"""

    payload = _leaf_payload([(1,), (2,)], [(7, 0)])
    with pytest.raises(StorageError):
        _IndexNode.from_page(_index_page(payload))


def test_index_node_full_verification_is_opt_in() -> None:
    """默认读路径放过乱序，``verify=True`` 与 ``validate()`` 仍然报错。"""

    payload = _leaf_payload([(2,), (1,)], [(7, 0), (7, 1)])
    node = _IndexNode.from_page(_index_page(payload))
    assert [tuple(key) for key in node.keys] == [(2,), (1,)]

    with pytest.raises(StorageError):
        _IndexNode.from_page(_index_page(payload), verify=True)

    with pytest.raises(StorageError):
        index_page_info(_index_page(payload))

    with pytest.raises(StorageError):
        node.validate()
    # 结构自检通过，说明两类校验的边界确实分开了。
    node.validate_structure()


def test_index_node_round_trips_through_page_payload() -> None:
    node = _IndexNode(
        page_id=3,
        leaf=True,
        keys=[(1, "a"), (2, "b")],
        row_ids=[RowId(PageId(5), 0), RowId(PageId(5), 1)],
    )
    rebuilt = _IndexNode.from_page(Page(3, 4096, PageType.INDEX, node.payload()))
    assert rebuilt.keys == node.keys
    assert rebuilt.row_ids == node.row_ids
