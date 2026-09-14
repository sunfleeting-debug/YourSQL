"""可持久化 B+Tree 和索引管理器。

索引页使用独立的 ``MBIX`` JSON 负载。JSON 不是为了追求生产数据库的压缩率，
而是让课程工作台可以直接检查页内的节点、边界键和子页关系。真正的树结构、
叶子链和 RowId 均落在 ``PageType.INDEX`` 页面中；没有 BufferPool 时仍保留
轻量的内存模式与可持久化的 B+Tree。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from threading import RLock
from typing import TYPE_CHECKING, Callable, Iterable, Iterator

from ..common.codec import PayloadCodec, PayloadCodecError, decode_payload
from ..common.codec import payload_codec as get_payload_codec
from ..common.errors import ExecutionError, StorageError
from ..common.types import PageId, RowId
from .page import Page, PageType

if TYPE_CHECKING:
    from .buffer import BufferPool


INDEX_MAGIC = b"MBIX"
INDEX_VERSION = 1
# 页号、父子链接会在节点创建后变长；预留少量空间避免“刚好装满”的节点
# 在第一次分裂/合并更新链接时超过页容量。单条超大键仍按真实容量校验。
INDEX_LINK_RESERVE = 64

Key = tuple[object, ...]
MemoryKey = tuple[tuple[int, object], ...]


@dataclass(frozen=True, eq=False)
class IndexEntry:
    """索引扫描返回的 key 与 RowId 对。"""

    key: Key
    row_id: RowId

    def __iter__(self):
        """兼容旧的 ``for key, row_id in tree.range_scan()`` 调用。"""

        yield self.key
        yield self.row_id

    def __eq__(self, other: object) -> bool:
        """比较两个对象的键或结构是否相等。"""
        if isinstance(other, IndexEntry):
            return self.key == other.key and self.row_id == other.row_id
        if isinstance(other, tuple) and len(other) == 2:
            return (self.key, self.row_id) == other
        return NotImplemented


@dataclass(frozen=True, eq=False)
class IndexPayloadEntry:
    """索引扫描返回的 key、RowId 和覆盖列值。"""

    key: Key
    row_id: RowId
    payload: list[object]

    def __iter__(self):
        """兼容旧的三元组解包；业务代码应使用字段名。"""

        yield self.key
        yield self.row_id
        yield self.payload

    def __eq__(self, other: object) -> bool:
        """比较两个对象的键或结构是否相等。"""
        if isinstance(other, IndexPayloadEntry):
            return (
                self.key == other.key
                and self.row_id == other.row_id
                and self.payload == other.payload
            )
        if isinstance(other, tuple) and len(other) == 3:
            return (
                self.key,
                self.row_id,
                tuple(self.payload),
            ) == (other[0], other[1], tuple(other[2]))
        return NotImplemented


def _key(value: object | tuple[object, ...]) -> Key:
    """把标量和复合键统一为 tuple。"""

    return value if isinstance(value, tuple) else (value,)


def _memory_key(value: Key) -> MemoryKey:
    """为内存模式生成带类型标签的字典键，区分 ``False`` 与 ``0``。"""

    return tuple(_value_order(item) for item in value)


def _value_order(value: object) -> tuple[int, object]:
    """为 SQL 支持的值提供稳定的总序，避免 NULL 和异构值比较崩溃。"""

    if value is None:
        return (0, 0)
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, int) and not isinstance(value, bool):
        # 保留 Python int 的精度，不能先转 float，否则大于 2**53 的主键会折叠。
        return (2, (0, value))
    if isinstance(value, float):
        if math.isnan(value):
            return (2, (1, 0.0))
        return (2, (0, value))
    if isinstance(value, str):
        return (3, value)
    # SQL 行值来自 JSON，理论上只有上述类型；repr 让损坏/扩展值仍有确定顺序。
    return (4, repr(value))


def _compare_values(left: object, right: object) -> int:
    """比较两个键元素，返回 -1、0 或 1。"""

    left_order = _value_order(left)
    right_order = _value_order(right)
    if left_order[0] != right_order[0]:
        return -1 if left_order[0] < right_order[0] else 1
    try:
        if left_order[1] < right_order[1]:
            return -1
        if left_order[1] > right_order[1]:
            return 1
    except TypeError:
        left_text = repr(left_order[1])
        right_text = repr(right_order[1])
        if left_text < right_text:
            return -1
        if left_text > right_text:
            return 1
    return 0


def _compare_keys(left: Key, right: Key) -> int:
    """按字典序比较复合索引键。"""

    for left_value, right_value in zip(left, right, strict=False):
        result = _compare_values(left_value, right_value)
        if result:
            return result
    if len(left) < len(right):
        return -1
    if len(left) > len(right):
        return 1
    return 0


def _compare_entries(
    left_key: Key, left_row: RowId, right_key: Key, right_row: RowId
) -> int:
    """比较两个索引条目的键及其排序位置。"""
    result = _compare_keys(left_key, right_key)
    if result:
        return result
    left_tuple = left_row.as_tuple()
    right_tuple = right_row.as_tuple()
    if left_tuple < right_tuple:
        return -1
    if left_tuple > right_tuple:
        return 1
    return 0


def _lower_bound(keys: list[Key], target: Key) -> int:
    """在键数组中寻找第一个大于等于 target 的位置。"""

    low, high = 0, len(keys)
    while low < high:
        middle = (low + high) // 2
        if _compare_keys(keys[middle], target) < 0:
            low = middle + 1
        else:
            high = middle
    return low


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


@dataclass
class _IndexNode:
    """内存中的一个索引页快照。"""

    page_id: int
    leaf: bool
    level: int = 0
    parent: int | None = None
    next_page: int | None = None
    prev_page: int | None = None
    keys: list[Key] = field(default_factory=list)
    row_ids: list[RowId] = field(default_factory=list)
    children: list[int] = field(default_factory=list)
    # HOW：覆盖索引（INCLUDE 列）的条目携带值；与 keys/row_ids 平行，纯键索引时为空。
    payloads: list[list[object]] = field(default_factory=list)

    def ensure_payloads(self) -> None:
        """把 payloads 补齐到与 keys 等长，保持三个数组始终平行。"""

        if len(self.payloads) != len(self.keys):
            self.payloads = [
                self.payloads[index] if index < len(self.payloads) else []
                for index in range(len(self.keys))
            ]

    def payload_at(self, position: int) -> list[object]:
        """读取指定索引条目的覆盖列载荷。"""
        return self.payloads[position] if position < len(self.payloads) else []

    def insert_entry(
        self,
        position: int,
        key: Key,
        row_id: RowId,
        payload: list[object] | None = None,
    ) -> None:
        """插入条目；payload 为空时占位空列表，保持与 keys/row_ids 平行。"""

        self.ensure_payloads()
        self.keys.insert(position, key)
        self.row_ids.insert(position, row_id)
        self.payloads.insert(position, list(payload) if payload else [])

    def pop_entry(self, position: int = -1) -> IndexPayloadEntry:
        """弹出条目（默认末尾），连同覆盖列值一起返回。"""

        self.ensure_payloads()
        return IndexPayloadEntry(
            self.keys.pop(position),
            self.row_ids.pop(position),
            self.payloads.pop(position),
        )

    def extend_from(self, other: "_IndexNode") -> None:
        """追加另一个叶页的条目（合并时用）。"""

        self.ensure_payloads()
        other.ensure_payloads()
        self.keys.extend(other.keys)
        self.row_ids.extend(other.row_ids)
        self.payloads.extend(other.payloads)

    def prepend_from(self, other: "_IndexNode") -> None:
        """在头部插入另一个叶页的条目（合并时用）。"""

        self.ensure_payloads()
        other.ensure_payloads()
        self.keys[:0] = other.keys
        self.row_ids[:0] = other.row_ids
        self.payloads[:0] = other.payloads

    def validate(self) -> None:
        """校验索引页或索引节点的结构完整性。"""
        if self.leaf:
            if len(self.keys) != len(self.row_ids):
                raise StorageError(f"索引叶页 {self.page_id} 的 key/RowId 数量不一致")
            if self.payloads and len(self.payloads) != len(self.keys):
                raise StorageError(
                    f"索引叶页 {self.page_id} 的覆盖列值与 key 数量不一致"
                )
            if self.children:
                raise StorageError(f"索引叶页 {self.page_id} 不应包含子页")
            for left_key, right_key, left_row, right_row in zip(
                self.keys, self.keys[1:], self.row_ids, self.row_ids[1:], strict=False
            ):
                if _compare_entries(left_key, left_row, right_key, right_row) > 0:
                    raise StorageError(
                        f"索引叶页 {self.page_id} 的条目未按 key/RowId 排序"
                    )
        elif len(self.children) != len(self.keys) + 1:
            raise StorageError(f"索引内部页 {self.page_id} 的子页数量非法")
        elif any(
            _compare_keys(left, right) > 0
            for left, right in zip(self.keys, self.keys[1:], strict=False)
        ):
            raise StorageError(f"索引内部页 {self.page_id} 的分隔键未排序")

    def payload(self, codec: PayloadCodec | str = "json") -> bytes:
        """返回索引条目的覆盖列载荷。"""
        self.validate()
        value: dict[str, object] = {
            "version": INDEX_VERSION,
            "kind": "leaf" if self.leaf else "internal",
            "level": self.level,
            "parent": self.parent,
            "next": self.next_page,
            "prev": self.prev_page,
            "keys": [list(key) for key in self.keys],
        }
        if self.leaf:
            value["row_ids"] = [
                [int(row_id.page_id), row_id.slot_id] for row_id in self.row_ids
            ]
            # HOW：只有覆盖索引才写入 payloads，纯键索引的页布局与旧版完全一致。
            if self.payloads and any(payload for payload in self.payloads):
                self.ensure_payloads()
                value["payloads"] = [list(payload) for payload in self.payloads]
        else:
            value["children"] = list(self.children)
        selected = codec if isinstance(codec, PayloadCodec) else get_payload_codec(codec)
        try:
            encoded = selected.encode(value)
        except (PayloadCodecError, TypeError, ValueError) as exc:
            raise StorageError(f"索引页 {self.page_id} 含无法序列化的键值") from exc
        return INDEX_MAGIC + encoded

    @classmethod
    def from_page(
        cls, page: Page, preferred: PayloadCodec | str | None = None
    ) -> "_IndexNode":
        """从数据库页恢复索引节点。"""
        if page.page_type is not PageType.INDEX:
            raise StorageError(f"页 {page.page_id} 不是 INDEX 页")
        if not page.payload:
            raise StorageError(f"索引页 {page.page_id} 为空")
        if not page.payload.startswith(INDEX_MAGIC):
            raise StorageError(f"索引页 {page.page_id} 的格式版本不受支持")
        try:
            raw, _codec = decode_payload(page.payload[len(INDEX_MAGIC) :], preferred)
            if not isinstance(raw, dict) or int(raw.get("version", 0)) != INDEX_VERSION:
                raise ValueError("版本不匹配")
            kind = str(raw.get("kind"))
            if kind not in {"leaf", "internal"}:
                raise ValueError("节点类型非法")
            keys_raw = raw.get("keys", [])
            if not isinstance(keys_raw, list) or any(
                not isinstance(item, list) for item in keys_raw
            ):
                raise ValueError("keys 不是数组")
            keys = [tuple(item) for item in keys_raw]
            parent = raw.get("parent")
            next_page = raw.get("next")
            prev_page = raw.get("prev")
            node = cls(
                page_id=page.page_id,
                leaf=kind == "leaf",
                level=int(raw.get("level", 0)),
                parent=None if parent is None else int(parent),
                next_page=None if next_page is None else int(next_page),
                prev_page=None if prev_page is None else int(prev_page),
                keys=keys,
            )
            if node.leaf:
                raw_row_ids = raw.get("row_ids", [])
                if not isinstance(raw_row_ids, list):
                    raise ValueError("row_ids 不是数组")
                node.row_ids = [
                    RowId(PageId(int(item[0])), int(item[1])) for item in raw_row_ids
                ]
                # HOW：覆盖索引才有 payloads；旧页与纯键索引没有该字段，保持空列表。
                raw_payloads = raw.get("payloads", [])
                if not isinstance(raw_payloads, list):
                    raise ValueError("payloads 不是数组")
                node.payloads = [list(item) for item in raw_payloads]
            else:
                raw_children = raw.get("children", [])
                if not isinstance(raw_children, list):
                    raise ValueError("children 不是数组")
                node.children = [int(item) for item in raw_children]
            node.validate()
            return node
        except (
            UnicodeDecodeError,
            PayloadCodecError,
            TypeError,
            ValueError,
            KeyError,
            IndexError,
        ) as exc:
            raise StorageError(f"索引页 {page.page_id} 内容损坏") from exc


@dataclass(frozen=True)
class IndexPageEntry:
    """索引页检查结果中的叶子条目。"""

    key: Key
    row_id: RowId

    def to_dict(self) -> dict[str, object]:
        """将对象转换为可序列化的字典。"""
        return {"key": list(self.key), "row_id": self.row_id.as_tuple()}


@dataclass(frozen=True)
class IndexPageInfo:
    """单个 INDEX 页的有界结构化检查结果。"""

    format: str
    physical: bool
    page_id: int
    node_type: str
    level: int
    parent_page_id: int | None
    next_page_id: int | None
    prev_page_id: int | None
    key_count: int
    min_key: Key | None
    max_key: Key | None
    entries: tuple[IndexPageEntry, ...] = ()
    entry_offset: int = 0
    entry_limit: int = 0
    entry_count: int = 0
    entries_truncated: bool = False
    row_id_count: int = 0
    children: tuple[int, ...] = ()
    child_offset: int = 0
    child_limit: int = 0
    child_count: int = 0
    children_truncated: bool = False

    def __getitem__(self, key: str) -> object:
        """按键或下标读取对象中的元素。"""
        return self.to_dict()[key]

    def to_dict(self) -> dict[str, object]:
        """将对象转换为可序列化的字典。"""
        result: dict[str, object] = {
            "format": self.format,
            "physical": self.physical,
            "page_id": self.page_id,
            "node_type": self.node_type,
            "level": self.level,
            "parent_page_id": self.parent_page_id,
            "next_page_id": self.next_page_id,
            "prev_page_id": self.prev_page_id,
            "key_count": self.key_count,
            "min_key": list(self.min_key) if self.min_key is not None else None,
            "max_key": list(self.max_key) if self.max_key is not None else None,
        }
        if self.node_type == "leaf":
            result.update(
                {
                    "entries": [entry.to_dict() for entry in self.entries],
                    "entry_offset": self.entry_offset,
                    "entry_limit": self.entry_limit,
                    "entry_count": self.entry_count,
                    "entries_truncated": self.entries_truncated,
                    "row_id_count": self.row_id_count,
                }
            )
        else:
            result.update(
                {
                    "children": list(self.children),
                    "child_offset": self.child_offset,
                    "child_limit": self.child_limit,
                    "child_count": self.child_count,
                    "children_truncated": self.children_truncated,
                }
            )
        return result


@dataclass(frozen=True)
class IndexSnapshotEntry:
    """B+Tree 按键聚合后的检查条目。"""

    key: Key
    row_ids: tuple[RowId, ...]
    row_count: int
    rows_truncated: bool

    def to_dict(self) -> dict[str, object]:
        """将对象转换为可序列化的字典。"""
        return {
            "key": list(self.key),
            "row_ids": [row_id.as_tuple() for row_id in self.row_ids],
            "row_count": self.row_count,
            "rows_truncated": self.rows_truncated,
        }


@dataclass(frozen=True)
class IndexNodeSnapshot:
    """B+Tree 物理节点的检查摘要。"""

    page_id: int
    node_type: str
    level: int
    parent_page_id: int | None
    next_page_id: int | None
    prev_page_id: int | None
    key_count: int
    child_count: int
    children: tuple[int, ...]
    min_key: Key | None
    max_key: Key | None

    def to_dict(self) -> dict[str, object]:
        """将对象转换为可序列化的字典。"""
        return {
            "page_id": self.page_id,
            "node_type": self.node_type,
            "level": self.level,
            "parent_page_id": self.parent_page_id,
            "next_page_id": self.next_page_id,
            "prev_page_id": self.prev_page_id,
            "key_count": self.key_count,
            "child_count": self.child_count,
            "children": list(self.children),
            "min_key": list(self.min_key) if self.min_key is not None else None,
            "max_key": list(self.max_key) if self.max_key is not None else None,
        }


@dataclass(frozen=True)
class BPlusTreeSnapshot:
    """B+Tree 的内存/落盘统一检查快照。"""

    entries: tuple[IndexSnapshotEntry, ...]
    total: int
    offset: int
    limit: int
    representation: str
    unique: bool
    physical: bool
    format: str
    root_page_id: int | None = None
    height: int = 0
    page_count: int = 0
    page_ids: tuple[int, ...] = ()
    all_page_ids: tuple[int, ...] = ()
    pages_truncated: bool = False
    nodes: tuple[IndexNodeSnapshot, ...] = ()
    limitation: str | None = None

    def __getitem__(self, key: str) -> object:
        """按键或下标读取对象中的元素。"""
        return self.to_dict()[key]

    def to_dict(self) -> dict[str, object]:
        """将对象转换为可序列化的字典。"""
        result: dict[str, object] = {
            "entries": [entry.to_dict() for entry in self.entries],
            "total": self.total,
            "offset": self.offset,
            "limit": self.limit,
            "representation": self.representation,
            "unique": self.unique,
            "physical": self.physical,
            "format": self.format,
        }
        if self.physical:
            result.update(
                {
                    "root_page_id": self.root_page_id,
                    "height": self.height,
                    "page_count": self.page_count,
                    "page_ids": list(self.page_ids),
                    "all_page_ids": list(self.all_page_ids),
                    "pages_truncated": self.pages_truncated,
                    "nodes": [node.to_dict() for node in self.nodes],
                    "limitation": self.limitation,
                }
            )
        return result


def index_page_info(
    page: Page,
    offset: int = 0,
    limit: int = 100,
    codec: PayloadCodec | str | None = None,
) -> IndexPageInfo:
    """返回有界的索引页结构，避免把整页重复展开到 HTTP 响应。"""

    node = _IndexNode.from_page(page, codec)
    safe_offset = max(0, int(offset))
    safe_limit = max(1, int(limit))
    common = {
        "format": "disk_bplus_tree_v1",
        "physical": True,
        "page_id": node.page_id,
        "node_type": "leaf" if node.leaf else "internal",
        "level": node.level,
        "parent_page_id": node.parent,
        "next_page_id": node.next_page,
        "prev_page_id": node.prev_page,
        "key_count": len(node.keys),
        "min_key": node.keys[0] if node.keys else None,
        "max_key": node.keys[-1] if node.keys else None,
    }
    if node.leaf:
        all_entries = [
            IndexPageEntry(key, row_id)
            for key, row_id in zip(node.keys, node.row_ids, strict=True)
        ]
        return IndexPageInfo(
            **common,
            entries=tuple(all_entries[safe_offset : safe_offset + safe_limit]),
            entry_offset=safe_offset,
            entry_limit=safe_limit,
            entry_count=len(all_entries),
            entries_truncated=safe_offset + safe_limit < len(all_entries),
            row_id_count=len(node.row_ids),
        )
    return IndexPageInfo(
        **common,
        children=tuple(node.children[safe_offset : safe_offset + safe_limit]),
        child_offset=safe_offset,
        child_limit=safe_limit,
        child_count=len(node.children),
        children_truncated=safe_offset + safe_limit < len(node.children),
    )


class BPlusTree:
    """支持内存和落盘两种模式的 B+Tree。

    落盘模式按字节容量动态确定每页最大条目数。叶子页存储一个 key/RowId
    对，重复键通过多条叶子记录表示；内部页保存“左子树最大键”作为分隔键。
    """

    def __init__(
        self,
        unique: bool = False,
        *,
        buffer_pool: "BufferPool | None" = None,
        root_page_id: PageId | int | None = None,
        on_root_change: Callable[[int], None] | None = None,
    ) -> None:
        """初始化实例所需的状态和依赖。"""
        self.unique = bool(unique)
        self._buffer_pool = buffer_pool
        self._persistent = buffer_pool is not None
        self._on_root_change = on_root_change
        self._lock = RLock()
        self._destroyed = False
        self._root_page_id: int | None = None
        # 内存兼容模式保留旧的有序数组接口。
        self._keys: list[Key] = []
        # Python 字典会把 False/0、True/1 当成同一个键；使用带类型标签的
        # 比较表示，才能与落盘模式保持一致的异构键排序语义。
        self._values: dict[MemoryKey, set[RowId]] = {}
        if not self._persistent:
            return
        selected_root = None if root_page_id is None else int(root_page_id)
        if selected_root is None:
            page = self._buffer_pool.new_page(PageType.INDEX)
            selected_root = page.page_id
            self._root_page_id = selected_root
            self._write_node(_IndexNode(selected_root, leaf=True))
            self._notify_root_change()
            return
        if not self._valid_index_page(selected_root):
            raise StorageError(f"索引根页 {selected_root} 不存在或不是 INDEX 页")
        self._root_page_id = selected_root
        page = self._buffer_pool.peek_page(selected_root)
        if not page.payload:
            raise StorageError(f"索引根页 {selected_root} 为空")
        node = _IndexNode.from_page(page, self.payload_codec)
        if node.parent is not None:
            raise StorageError(f"索引根页 {selected_root} 不能拥有父页")

    @property
    def is_persistent(self) -> bool:
        """返回当前索引是否使用持久化存储。"""
        return self._persistent

    @property
    def root_page_id(self) -> int | None:
        """返回当前索引根页的页号。"""
        return self._root_page_id

    @property
    def page_size(self) -> int:
        """返回当前索引使用的页大小。"""
        return (
            self._buffer_pool.disk.page_size if self._buffer_pool is not None else 4096
        )

    @property
    def payload_codec(self) -> PayloadCodec:
        """返回当前持久化索引使用的 payload 编解码器。"""

        if self._buffer_pool is None:
            return get_payload_codec("json")
        return self._buffer_pool.disk.payload_codec

    def _ensure_alive(self) -> None:
        """检查索引仍处于可用状态。"""
        if self._destroyed:
            raise StorageError("索引已经释放")

    def _valid_index_page(self, page_id: int) -> bool:
        """判断页是否为有效的索引页。"""
        if (
            self._buffer_pool is None
            or page_id < 0
            or page_id >= self._buffer_pool.disk.page_count
        ):
            return False
        try:
            return self._buffer_pool.peek_page(page_id).page_type is PageType.INDEX
        except StorageError:
            return False

    def _notify_root_change(self) -> None:
        """通知外部目录索引根页发生变化。"""
        if self._on_root_change is not None and self._root_page_id is not None:
            self._on_root_change(self._root_page_id)

    def _read_node(self, page_id: int, *, readonly: bool = False) -> _IndexNode:
        """读取并解析指定索引节点。"""
        self._ensure_alive()
        if self._buffer_pool is None:
            raise StorageError("内存索引没有物理页")
        if readonly:
            return _IndexNode.from_page(
                self._buffer_pool.peek_page(page_id), self.payload_codec
            )
        page = self._buffer_pool.get_page(page_id, pin=True)
        try:
            return _IndexNode.from_page(page, self.payload_codec)
        finally:
            self._buffer_pool.unpin(page_id)

    def _write_node(self, node: _IndexNode) -> None:
        """将索引节点写回存储。"""
        self._ensure_alive()
        if self._buffer_pool is None:
            raise StorageError("内存索引没有物理页")
        payload = node.payload(self.payload_codec)
        if len(payload) > self.page_size - Page.HEADER_SIZE:
            raise StorageError(f"索引页 {node.page_id} 超过页容量，请缩短索引键")
        self._buffer_pool.put_page(
            Page(node.page_id, self.page_size, PageType.INDEX, payload), dirty=True
        )

    def _new_node(
        self, *, leaf: bool, level: int, parent: int | None = None
    ) -> _IndexNode:
        """创建并登记新的索引节点。"""
        if self._buffer_pool is None:
            raise StorageError("内存索引没有物理页")
        page = self._buffer_pool.new_page(PageType.INDEX)
        return _IndexNode(page.page_id, leaf=leaf, level=level, parent=parent)

    def _delete_page(self, page_id: int) -> None:
        """删除索引节点占用的页。"""
        if self._buffer_pool is not None and self._valid_index_page(page_id):
            self._buffer_pool.delete_page(page_id)

    def _node_fits(self, node: _IndexNode, page_size: int) -> bool:
        """判断索引节点是否能放入当前页。"""
        try:
            payload_size = len(node.payload(self.payload_codec))
            usable = page_size - Page.HEADER_SIZE
            if payload_size > usable:
                return False
            if node.leaf and len(node.keys) <= 1:
                return True
            if not node.leaf and len(node.children) <= 1:
                return True
            return payload_size + INDEX_LINK_RESERVE <= usable
        except (StorageError, TypeError, ValueError):
            return False

    def _node_underfull(self, node: _IndexNode) -> bool:
        """按页负载判断非根节点是否低于半页占用。

        键是变长字符串或复合值，不能用固定的“最小键数”描述占用率；
        以实际编码字节数判断，同时保留至少一个叶条目或两个子页。
        """

        if node.parent is None:
            return False
        if node.leaf and not node.keys:
            return True
        if not node.leaf and len(node.children) < 2:
            return True
        usable = self.page_size - Page.HEADER_SIZE
        return len(node.payload(self.payload_codec)) < max(1, usable // 2)

    def _empty_root(self) -> _IndexNode:
        """创建空的索引根节点。"""
        if self._root_page_id is None:
            raise StorageError("索引没有根页")
        return _IndexNode(self._root_page_id, leaf=True)

    def _load_max_key(self, page_id: int, *, readonly: bool = False) -> Key:
        """读取节点中的最大键。"""
        node = self._read_node(page_id, readonly=readonly)
        if node.leaf:
            if not node.keys:
                raise StorageError(f"索引页 {page_id} 为空，无法生成分隔键")
            return node.keys[-1]
        if not node.children:
            raise StorageError(f"索引内部页 {page_id} 没有子页")
        # 内部页的 keys 只保存除最后一个子页外的左子树最大键，
        # 所以父页需要递归读取最右子树的真实最大键。
        return self._load_max_key(node.children[-1], readonly=readonly)

    def _load_min_key(self, page_id: int, *, readonly: bool = False) -> Key:
        """读取节点中的最小键。"""
        node = self._read_node(page_id, readonly=readonly)
        if node.leaf:
            if not node.keys:
                raise StorageError(f"索引页 {page_id} 为空，无法生成边界键")
            return node.keys[0]
        if not node.children:
            raise StorageError(f"索引内部页 {page_id} 没有子页")
        return self._load_min_key(node.children[0], readonly=readonly)

    def _refresh_internal_keys(self, node: _IndexNode) -> None:
        """刷新内部节点的分隔键。"""
        if node.leaf:
            return
        node.keys = [self._load_max_key(child_id) for child_id in node.children[:-1]]

    def _refresh_ancestors(self, parent_id: int | None) -> None:
        """更新祖先分隔键；键增长导致页溢出时继续分裂。"""

        current = parent_id
        while current is not None:
            node = self._read_node(current)
            old_max = self._load_max_key(node.page_id) if node.keys else None
            self._refresh_internal_keys(node)
            if not self._node_fits(node, self.page_size):
                self._split_internal_and_propagate(node)
                return
            self._write_node(node)
            new_max = self._load_max_key(node.page_id) if node.keys else None
            if old_max is None and new_max is None:
                return
            if (
                old_max is not None
                and new_max is not None
                and _compare_keys(old_max, new_max) == 0
            ):
                return
            current = node.parent

    def _find_leaf(self, target: Key, *, readonly: bool = False) -> _IndexNode:
        """沿 B+Tree 查找包含目标键的叶节点。"""
        if self._root_page_id is None:
            raise StorageError("索引没有根页")
        node = self._read_node(self._root_page_id, readonly=readonly)
        while not node.leaf:
            if not node.children:
                raise StorageError(f"索引内部页 {node.page_id} 没有子页")
            child_index = 0
            while (
                child_index < len(node.keys)
                and _compare_keys(target, node.keys[child_index]) > 0
            ):
                child_index += 1
            node = self._read_node(
                node.children[min(child_index, len(node.children) - 1)],
                readonly=readonly,
            )
        return node

    def _iter_leaf_nodes(self, *, readonly: bool = False) -> Iterator[_IndexNode]:
        """按叶节点链顺序遍历索引叶页。"""
        if self._root_page_id is None:
            return
        node = self._read_node(self._root_page_id, readonly=readonly)
        while not node.leaf:
            if not node.children:
                return
            node = self._read_node(node.children[0], readonly=readonly)
        visited: set[int] = set()
        while node.page_id not in visited:
            visited.add(node.page_id)
            yield node
            if node.next_page is None:
                break
            node = self._read_node(node.next_page, readonly=readonly)

    def _iter_entries(self, *, readonly: bool = False) -> Iterator[IndexEntry]:
        """顺序遍历叶子条目；检查接口使用 peek，避免改变缓存统计。"""

        for leaf in self._iter_leaf_nodes(readonly=readonly):
            yield from (
                IndexEntry(key, row_id)
                for key, row_id in zip(leaf.keys, leaf.row_ids, strict=True)
            )

    def _insert_memory(self, normalized: Key, row_id: RowId) -> None:
        """向内存索引插入键和值。"""
        values = self._values.setdefault(_memory_key(normalized), set())
        if self.unique and values and row_id not in values:
            raise ExecutionError(f"唯一索引冲突: {normalized!r}")
        position = _lower_bound(self._keys, normalized)
        if (
            position >= len(self._keys)
            or _compare_keys(self._keys[position], normalized) != 0
        ):
            self._keys.insert(position, normalized)
        values.add(row_id)

    def _delete_memory(self, normalized: Key, row_id: RowId | None) -> None:
        """从内存索引删除指定键和值。"""
        memory_key = _memory_key(normalized)
        values = self._values.get(memory_key)
        if values is None:
            return
        if row_id is None:
            values.clear()
        else:
            values.discard(row_id)
        if not values:
            self._values.pop(memory_key, None)
            position = _lower_bound(self._keys, normalized)
            if (
                position < len(self._keys)
                and _compare_keys(self._keys[position], normalized) == 0
            ):
                self._keys.pop(position)

    def insert(
        self,
        key: object | tuple[object, ...],
        row_id: RowId,
        payload: Iterable[object] | None = None,
    ) -> None:
        """插入一个 key/RowId；payload 为覆盖索引（INCLUDE 列）携带的值。"""

        normalized = _key(key)
        normalized_payload = list(payload) if payload is not None else None
        with self._lock:
            self._ensure_alive()
            if not self._persistent:
                self._insert_memory(normalized, row_id)
                return
            leaf = self._find_insert_leaf(normalized, row_id)
            first = _lower_bound(leaf.keys, normalized)
            last = first
            while (
                last < len(leaf.keys)
                and _compare_keys(leaf.keys[last], normalized) == 0
            ):
                if leaf.row_ids[last] == row_id:
                    return
                last += 1
            if self.unique and first < last:
                raise ExecutionError(f"唯一索引冲突: {normalized!r}")
            position = first
            while (
                position < len(leaf.keys)
                and _compare_entries(
                    leaf.keys[position], leaf.row_ids[position], normalized, row_id
                )
                < 0
            ):
                position += 1
            leaf.insert_entry(position, normalized, row_id, normalized_payload)
            if self._node_fits(leaf, self.page_size):
                self._write_node(leaf)
                self._refresh_ancestors(leaf.parent)
                return
            self._split_leaf_and_propagate(leaf)

    def has_entries(self) -> bool:
        """索引是否已有条目；供批量导入决定能否延迟到装载结束再建树。"""

        with self._lock:
            self._ensure_alive()
            if self._root_page_id is None:
                return False
            node = self._read_node(self._root_page_id, readonly=True)
            return bool(node.keys or node.children)

    def _split_position(self, node: _IndexNode, candidates: Iterable[int]) -> int:
        """计算索引节点的分裂位置。"""
        selected: int | None = None
        middle = len(node.keys) // 2
        ordered = sorted(candidates, key=lambda value: (abs(value - middle), value))
        for position in ordered:
            node.ensure_payloads()
            left = _IndexNode(
                node.page_id,
                True,
                node.level,
                node.parent,
                node.next_page,
                node.prev_page,
                node.keys[:position],
                node.row_ids[:position],
                payloads=node.payloads[:position],
            )
            right = _IndexNode(
                -1,
                True,
                node.level,
                node.parent,
                node.next_page,
                node.page_id,
                node.keys[position:],
                node.row_ids[position:],
                payloads=node.payloads[position:],
            )
            if self._node_fits(left, self.page_size) and self._node_fits(
                right, self.page_size
            ):
                selected = position
                break
        if selected is None:
            raise StorageError(f"索引页 {node.page_id} 单条记录超过页容量，无法分裂")
        return selected

    def _split_leaf_and_propagate(self, leaf: _IndexNode) -> None:
        """分裂叶节点并向父节点传播分隔键。"""
        position = self._split_position(leaf, range(1, len(leaf.keys)))
        leaf.ensure_payloads()
        right = self._new_node(leaf=True, level=leaf.level, parent=leaf.parent)
        right.keys = leaf.keys[position:]
        right.row_ids = leaf.row_ids[position:]
        right.payloads = leaf.payloads[position:]
        leaf.keys = leaf.keys[:position]
        leaf.row_ids = leaf.row_ids[:position]
        leaf.payloads = leaf.payloads[:position]
        right.next_page = leaf.next_page
        right.prev_page = leaf.page_id
        if leaf.next_page is not None:
            next_node = self._read_node(leaf.next_page)
            next_node.prev_page = right.page_id
            self._write_node(next_node)
        leaf.next_page = right.page_id
        self._write_node(leaf)
        self._write_node(right)
        if leaf.parent is None:
            self._create_root(leaf, right)
            return
        parent = self._read_node(leaf.parent)
        index = parent.children.index(leaf.page_id)
        parent.children.insert(index + 1, right.page_id)
        self._refresh_internal_keys(parent)
        if self._node_fits(parent, self.page_size):
            self._write_node(parent)
            self._refresh_ancestors(parent.parent)
        else:
            self._split_internal_and_propagate(parent)

    def _split_internal_and_propagate(self, node: _IndexNode) -> None:
        """分裂内部节点并向上层传播分隔键。"""
        if node.leaf:
            raise StorageError("叶页不能使用内部页分裂流程")
        # 内部页按 child 数量切分；分隔键总是从子页最大键重新计算。
        middle = max(1, len(node.children) // 2)
        candidates = sorted(
            range(1, len(node.children)), key=lambda value: (abs(value - middle), value)
        )
        selected: int | None = None
        selected_left_keys: list[Key] = []
        selected_right_keys: list[Key] = []
        for position in candidates:
            left_children = node.children[:position]
            right_children = node.children[position:]
            if not left_children or not right_children:
                continue
            left_keys = [
                self._load_max_key(child_id) for child_id in left_children[:-1]
            ]
            right_keys = [
                self._load_max_key(child_id) for child_id in right_children[:-1]
            ]
            left = _IndexNode(
                node.page_id,
                False,
                node.level,
                node.parent,
                keys=left_keys,
                children=left_children,
            )
            right = _IndexNode(
                -1,
                False,
                node.level,
                node.parent,
                keys=right_keys,
                children=right_children,
            )
            if self._node_fits(left, self.page_size) and self._node_fits(
                right, self.page_size
            ):
                selected = position
                selected_left_keys = left_keys
                selected_right_keys = right_keys
                break
        if selected is None:
            raise StorageError(f"索引内部页 {node.page_id} 无法分裂")
        left_children = node.children[:selected]
        right_children = node.children[selected:]
        node.children = left_children
        node.keys = selected_left_keys
        right = self._new_node(leaf=False, level=node.level, parent=node.parent)
        right.children = right_children
        right.keys = selected_right_keys
        for child_id in right.children:
            child = self._read_node(child_id)
            child.parent = right.page_id
            self._write_node(child)
        self._write_node(node)
        self._write_node(right)
        if node.parent is None:
            self._create_root(node, right)
            return
        parent = self._read_node(node.parent)
        index = parent.children.index(node.page_id)
        parent.children.insert(index + 1, right.page_id)
        self._refresh_internal_keys(parent)
        if self._node_fits(parent, self.page_size):
            self._write_node(parent)
            self._refresh_ancestors(parent.parent)
        else:
            self._split_internal_and_propagate(parent)

    def _create_root(self, left: _IndexNode, right: _IndexNode) -> None:
        """创建新的根节点并连接原有子节点。"""
        root = self._new_node(leaf=False, level=max(left.level, right.level) + 1)
        root.children = [left.page_id, right.page_id]
        root.keys = [self._load_max_key(left.page_id)]
        left.parent = root.page_id
        right.parent = root.page_id
        self._write_node(left)
        self._write_node(right)
        self._write_node(root)
        self._root_page_id = root.page_id
        self._notify_root_change()

    def _find_equal_leaves(
        self, target: Key, *, readonly: bool = False
    ) -> Iterator[_IndexNode]:
        """查找可能包含相同键的叶节点。"""
        leaf = self._find_leaf(target, readonly=readonly)
        while leaf.prev_page is not None:
            previous = self._read_node(leaf.prev_page, readonly=readonly)
            if not previous.keys or _compare_keys(previous.keys[-1], target) < 0:
                break
            leaf = previous
        while True:
            if leaf.keys and _compare_keys(leaf.keys[0], target) > 0:
                break
            yield leaf
            if leaf.next_page is None:
                break
            next_node = self._read_node(leaf.next_page, readonly=readonly)
            if next_node.keys and _compare_keys(next_node.keys[0], target) > 0:
                break
            leaf = next_node

    def _find_insert_leaf(self, target: Key, row_id: RowId) -> _IndexNode:
        """按 ``(key, RowId)`` 顺序定位插入页。

        内部页的分隔键只保存逻辑 key，因此重复 key 可能跨越多个叶页。
        直接使用 ``_find_leaf`` 会把所有等值项送到左侧页，导致叶链中的
        RowId 顺序被打乱；沿等值叶链继续比较 RowId 可以保持稳定的全序。
        """

        leaf = self._find_leaf(target)
        while leaf.prev_page is not None:
            previous = self._read_node(leaf.prev_page)
            if not previous.keys or _compare_keys(previous.keys[-1], target) < 0:
                break
            leaf = previous
        while True:
            if not leaf.keys:
                return leaf
            for candidate_key, candidate_row_id in zip(
                leaf.keys, leaf.row_ids, strict=True
            ):
                comparison = _compare_keys(candidate_key, target)
                if comparison > 0:
                    return leaf
                if (
                    comparison == 0
                    and _compare_entries(
                        candidate_key, candidate_row_id, target, row_id
                    )
                    >= 0
                ):
                    return leaf
            if leaf.next_page is None:
                return leaf
            next_leaf = self._read_node(leaf.next_page)
            if next_leaf.keys and _compare_keys(next_leaf.keys[0], target) > 0:
                return leaf
            leaf = next_leaf

    def search(self, key: object | tuple[object, ...]) -> tuple[RowId, ...]:
        """返回精确键对应的所有 RowId。"""

        normalized = _key(key)
        with self._lock:
            self._ensure_alive()
            if not self._persistent:
                return tuple(sorted(self._values.get(_memory_key(normalized), set())))
            result: list[RowId] = []
            for leaf in self._find_equal_leaves(normalized):
                for candidate, row_id in zip(leaf.keys, leaf.row_ids, strict=True):
                    comparison = _compare_keys(candidate, normalized)
                    if comparison == 0:
                        result.append(row_id)
                    elif comparison > 0:
                        return tuple(sorted(result))
            return tuple(sorted(result))

    def range_scan(
        self,
        low: object | tuple[object, ...] | None = None,
        high: object | tuple[object, ...] | None = None,
        *,
        include_low: bool = True,
        include_high: bool = True,
    ) -> tuple[IndexEntry, ...]:
        """按键范围扫描，结果保持 key/RowId 顺序。"""

        return tuple(
            IndexEntry(entry.key, entry.row_id)
            for entry in self.range_scan_entries(
                low, high, include_low=include_low, include_high=include_high
            )
        )

    def range_scan_entries(
        self,
        low: object | tuple[object, ...] | None = None,
        high: object | tuple[object, ...] | None = None,
        *,
        include_low: bool = True,
        include_high: bool = True,
    ) -> tuple[IndexPayloadEntry, ...]:
        """按键范围扫描，同时返回条目携带的覆盖列值（IndexOnlyScan 使用）。"""

        lower = _key(low) if low is not None else None
        upper = _key(high) if high is not None else None
        with self._lock:
            self._ensure_alive()
            if not self._persistent:
                result: list[IndexPayloadEntry] = []
                for key in self._keys:
                    if lower is not None and (
                        _compare_keys(key, lower) < 0
                        or (not include_low and _compare_keys(key, lower) == 0)
                    ):
                        continue
                    if upper is not None and (
                        _compare_keys(key, upper) > 0
                        or (not include_high and _compare_keys(key, upper) == 0)
                    ):
                        continue
                    result.extend(
                        IndexPayloadEntry(key, row_id, [])
                        for row_id in sorted(self._values[_memory_key(key)])
                    )
                return tuple(result)
            result: list[IndexPayloadEntry] = []
            if lower is None:
                leaf = next(self._iter_leaf_nodes(readonly=False), None)
            else:
                leaf = self._find_leaf(lower)
                while leaf.prev_page is not None:
                    previous = self._read_node(leaf.prev_page)
                    if not previous.keys or _compare_keys(previous.keys[-1], lower) < 0:
                        break
                    leaf = previous
            while leaf is not None:
                leaf.ensure_payloads()
                for position, (key, row_id) in enumerate(
                    zip(leaf.keys, leaf.row_ids, strict=True)
                ):
                    if lower is not None:
                        comparison = _compare_keys(key, lower)
                        if comparison < 0 or (comparison == 0 and not include_low):
                            continue
                    if upper is not None:
                        comparison = _compare_keys(key, upper)
                        if comparison > 0 or (comparison == 0 and not include_high):
                            return tuple(result)
                    result.append(
                        IndexPayloadEntry(
                        key, row_id, leaf.payload_at(position)
                        )
                    )
                if leaf.next_page is None:
                    break
                leaf = self._read_node(leaf.next_page)
            return tuple(result)

    def prefix_scan(
        self, prefix: object | tuple[object, ...]
    ) -> tuple[IndexEntry, ...]:
        """返回以指定键前缀开头的索引条目。"""

        return self.range_scan_prefix(_key(prefix))

    def range_scan_prefix(
        self,
        prefix: object | tuple[object, ...],
        low: object | None = None,
        high: object | None = None,
        *,
        include_low: bool = True,
        include_high: bool = True,
    ) -> tuple[IndexEntry, ...]:
        """在固定前缀后的下一个键元素上执行范围扫描。

        例如联合索引 ``(tenant_id, created_at)`` 可以用
        ``range_scan_prefix((tenant_id,), low, high)`` 扫描一个租户的时间范围，
        不需要为后续键构造无法表达的正负无穷哨兵值。
        """

        return tuple(
            IndexEntry(entry.key, entry.row_id)
            for entry in self.range_scan_prefix_entries(
                prefix, low, high, include_low=include_low, include_high=include_high
            )
        )

    def range_scan_prefix_entries(
        self,
        prefix: object | tuple[object, ...],
        low: object | None = None,
        high: object | None = None,
        *,
        include_low: bool = True,
        include_high: bool = True,
    ) -> tuple[IndexPayloadEntry, ...]:
        """前缀范围扫描，并返回条目携带的覆盖列值（供 IndexOnlyScan 使用）。"""

        normalized_prefix = _key(prefix)
        with self._lock:
            self._ensure_alive()
            result: list[IndexPayloadEntry] = []
            include_exact = low is None and high is None

            def prefix_comparison(key: Key) -> int:
                """比较索引键与目标前缀的顺序关系。"""
                if len(key) < len(normalized_prefix):
                    return -1
                for key_value, prefix_value in zip(
                    key, normalized_prefix, strict=False
                ):
                    comparison = _compare_values(key_value, prefix_value)
                    if comparison:
                        return comparison
                return 0

            def accept(key: Key, row_id: RowId, payload: list[object]) -> bool:
                """判断当前索引条目是否满足扫描条件。"""
                comparison = prefix_comparison(key)
                if comparison != 0:
                    return False
                if len(key) == len(normalized_prefix):
                    if include_exact:
                        result.append(
                            IndexPayloadEntry(key, row_id, payload)
                        )
                        return True
                    return False
                value = key[len(normalized_prefix)]
                if low is not None:
                    comparison = _compare_values(value, low)
                    if comparison < 0 or (comparison == 0 and not include_low):
                        return False
                if high is not None:
                    comparison = _compare_values(value, high)
                    if comparison > 0 or (comparison == 0 and not include_high):
                        return False
                result.append(IndexPayloadEntry(key, row_id, payload))
                return True

            if not self._persistent:
                for key in self._keys:
                    comparison = prefix_comparison(key)
                    if comparison > 0:
                        break
                    if comparison == 0:
                        for row_id in sorted(self._values[_memory_key(key)]):
                            if high is not None and len(key) > len(normalized_prefix):
                                value_comparison = _compare_values(
                                    key[len(normalized_prefix)], high
                                )
                                if value_comparison > 0 or (
                                    value_comparison == 0 and not include_high
                                ):
                                    break
                            accept(key, row_id, [])
                return tuple(result)

            lower_key = (
                (*normalized_prefix, low) if low is not None else normalized_prefix
            )
            if low is None and not normalized_prefix:
                leaf = next(self._iter_leaf_nodes(readonly=False), None)
            else:
                leaf = self._find_leaf(lower_key)
                while leaf.prev_page is not None:
                    previous = self._read_node(leaf.prev_page)
                    if (
                        not previous.keys
                        or _compare_keys(previous.keys[-1], lower_key) < 0
                    ):
                        break
                    leaf = previous
            while leaf is not None:
                leaf.ensure_payloads()
                for position, (key, row_id) in enumerate(
                    zip(leaf.keys, leaf.row_ids, strict=True)
                ):
                    comparison = prefix_comparison(key)
                    if comparison > 0:
                        return tuple(result)
                    if comparison < 0:
                        continue
                    payload = leaf.payload_at(position)
                    if len(key) == len(normalized_prefix):
                        if include_exact:
                            result.append(
                                IndexPayloadEntry(key, row_id, payload)
                            )
                        continue
                    if low is not None:
                        value_comparison = _compare_values(
                            key[len(normalized_prefix)], low
                        )
                        if value_comparison < 0 or (
                            value_comparison == 0 and not include_low
                        ):
                            continue
                    if high is not None:
                        value_comparison = _compare_values(
                            key[len(normalized_prefix)], high
                        )
                        if value_comparison > 0 or (
                            value_comparison == 0 and not include_high
                        ):
                            return tuple(result)
                    result.append(IndexPayloadEntry(key, row_id, payload))
                if leaf.next_page is None:
                    break
                leaf = self._read_node(leaf.next_page)
            return tuple(result)

    def all_items(self) -> tuple[IndexEntry, ...]:
        """遍历索引中的全部键和值。"""
        return self.range_scan()

    def _delete_persistent(self, normalized: Key, row_id: RowId | None) -> None:
        """从持久化索引删除键和值。"""
        if row_id is None:
            # WHY：一个重复键可能横跨多个叶页。先批量清空这些叶页会让父节点
            # 同时看到多个空子页，无法计算分隔键；把剩余条目重新打包可以在
            # 整个操作期间保持 B+Tree 的叶链和内部页不变量，且根页仍复用原页号。
            entries = list(self._iter_entries(readonly=True))
            remaining = [
                (entry.key, entry.row_id)
                for entry in entries
                if _compare_keys(entry.key, normalized) != 0
            ]
            if len(remaining) != len(entries):
                self.bulk_load(remaining)
            return
        changed_leaves: list[int] = []
        for leaf in self._find_equal_leaves(normalized):
            old_count = len(leaf.keys)
            leaf.ensure_payloads()
            kept: list[IndexPayloadEntry] = []
            removed = False
            for position, (key, value) in enumerate(
                zip(leaf.keys, leaf.row_ids, strict=True)
            ):
                if (
                    not removed
                    and _compare_keys(key, normalized) == 0
                    and value == row_id
                ):
                    removed = True
                    continue
                kept.append(
                    IndexPayloadEntry(
                        key, value, leaf.payload_at(position)
                    )
                )
            leaf.keys = [entry.key for entry in kept]
            leaf.row_ids = [entry.row_id for entry in kept]
            leaf.payloads = [list(entry.payload) for entry in kept]
            if len(leaf.keys) != old_count:
                changed_leaves.append(leaf.page_id)
                # 先把空页写回，再让合并流程读取空节点；否则读取到删除前的旧条目。
                self._write_node(leaf)
                if row_id is not None:
                    break
        for page_id in changed_leaves:
            if self._valid_index_page(page_id):
                node = self._read_node(page_id)
                if node.parent is not None and self._node_underfull(node):
                    self._rebalance_empty(node)
                else:
                    self._refresh_ancestors(node.parent)

    def delete(
        self, key: object | tuple[object, ...], row_id: RowId | None = None
    ) -> None:
        """删除一个 RowId；row_id 为空时删除整个键的所有记录。"""

        normalized = _key(key)
        with self._lock:
            self._ensure_alive()
            if not self._persistent:
                self._delete_memory(normalized, row_id)
                return
            self._delete_persistent(normalized, row_id)

    def _rebalance_empty(self, node: _IndexNode) -> None:
        """修复低占用的非根页，先借位，无法借位时合并。"""

        if node.parent is None:
            return
        parent = self._read_node(node.parent)
        index = parent.children.index(node.page_id)
        left = self._read_node(parent.children[index - 1]) if index > 0 else None
        right = (
            self._read_node(parent.children[index + 1])
            if index + 1 < len(parent.children)
            else None
        )

        if node.leaf:
            # 只从借位后仍能保持非低占用的兄弟页借一条，避免把问题转移给兄弟。
            while self._node_underfull(node):
                borrowed = False
                if left is not None and len(left.keys) > 1:
                    left.ensure_payloads()
                    node.ensure_payloads()
                    moved = left.pop_entry()
                    node.insert_entry(
                        0, moved.key, moved.row_id, moved.payload
                    )
                    if self._node_fits(
                        node, self.page_size
                    ) and not self._node_underfull(left):
                        self._write_node(left)
                        self._write_node(node)
                        borrowed = True
                    else:
                        returned = node.pop_entry(0)
                        left.insert_entry(
                            len(left.keys),
                            returned.key,
                            returned.row_id,
                            returned.payload,
                        )
                if not borrowed and right is not None and len(right.keys) > 1:
                    right.ensure_payloads()
                    moved = right.pop_entry(0)
                    node.insert_entry(
                        len(node.keys), moved.key, moved.row_id, moved.payload
                    )
                    if self._node_fits(
                        node, self.page_size
                    ) and not self._node_underfull(right):
                        self._write_node(right)
                        self._write_node(node)
                        borrowed = True
                    else:
                        returned = node.pop_entry()
                        right.insert_entry(
                            0,
                            returned.key,
                            returned.row_id,
                            returned.payload,
                        )
                if not borrowed:
                    break
            if not self._node_underfull(node):
                self._refresh_internal_keys(parent)
                self._write_node(parent)
                self._refresh_ancestors(parent.parent)
                return
            if left is not None:
                original = len(left.keys)
                left.extend_from(node)
                if self._node_fits(left, self.page_size):
                    left.next_page = node.next_page
                    if node.next_page is not None:
                        neighbour = self._read_node(node.next_page)
                        neighbour.prev_page = left.page_id
                        self._write_node(neighbour)
                    self._write_node(left)
                    parent.children.pop(index)
                    self._delete_page(node.page_id)
                    self._repair_parent_after_removal(parent)
                    return
                del left.keys[original:]
                del left.row_ids[original:]
                del left.payloads[original:]
            if right is not None:
                original = len(node.keys)
                node.extend_from(right)
                if self._node_fits(node, self.page_size):
                    node.next_page = right.next_page
                    if right.next_page is not None:
                        neighbour = self._read_node(right.next_page)
                        neighbour.prev_page = node.page_id
                        self._write_node(neighbour)
                    self._write_node(node)
                    parent.children.pop(index + 1)
                    self._delete_page(right.page_id)
                    self._repair_parent_after_removal(parent)
                    return
                del node.keys[original:]
                del node.row_ids[original:]
                del node.payloads[original:]
            # 变长键可能使两个合法页无法合并；保留当前页并更新边界，不能让删除失败。
            self._write_node(node)
            self._refresh_internal_keys(parent)
            self._write_node(parent)
            self._refresh_ancestors(parent.parent)
            return

        while self._node_underfull(node):
            borrowed = False
            if left is not None and len(left.children) > 2:
                moved = left.children.pop()
                node.children.insert(0, moved)
                moved_node = self._read_node(moved)
                moved_node.parent = node.page_id
                self._refresh_internal_keys(left)
                self._refresh_internal_keys(node)
                if not self._node_underfull(left):
                    self._write_node(left)
                    self._write_node(moved_node)
                    self._write_node(node)
                    borrowed = True
                else:
                    left.children.append(node.children.pop(0))
                    moved_node.parent = left.page_id
                    self._refresh_internal_keys(left)
                    self._refresh_internal_keys(node)
            if not borrowed and right is not None and len(right.children) > 2:
                moved = right.children.pop(0)
                node.children.append(moved)
                moved_node = self._read_node(moved)
                moved_node.parent = node.page_id
                self._refresh_internal_keys(right)
                self._refresh_internal_keys(node)
                if not self._node_underfull(right):
                    self._write_node(right)
                    self._write_node(moved_node)
                    self._write_node(node)
                    borrowed = True
                else:
                    right.children.insert(0, node.children.pop())
                    moved_node.parent = right.page_id
                    self._refresh_internal_keys(right)
                    self._refresh_internal_keys(node)
            if not borrowed:
                break
        if not self._node_underfull(node):
            self._refresh_internal_keys(parent)
            self._write_node(parent)
            self._refresh_ancestors(parent.parent)
            return
        if left is not None:
            original = len(left.children)
            left.children.extend(node.children)
            self._refresh_internal_keys(left)
            if self._node_fits(left, self.page_size):
                for child_id in node.children:
                    child = self._read_node(child_id)
                    child.parent = left.page_id
                    self._write_node(child)
                self._write_node(left)
                parent.children.pop(index)
                self._delete_page(node.page_id)
                self._repair_parent_after_removal(parent)
                return
            del left.children[original:]
            self._refresh_internal_keys(left)
        if right is not None:
            original = len(node.children)
            node.children.extend(right.children)
            self._refresh_internal_keys(node)
            if self._node_fits(node, self.page_size):
                for child_id in right.children:
                    child = self._read_node(child_id)
                    child.parent = node.page_id
                    self._write_node(child)
                self._write_node(node)
                parent.children.pop(index + 1)
                self._delete_page(right.page_id)
                self._repair_parent_after_removal(parent)
                return
            del node.children[original:]
            self._refresh_internal_keys(node)
        # 变长键/子页地址也可能阻止合并，保留结构并继续维护祖先边界。
        self._write_node(node)
        self._refresh_internal_keys(parent)
        self._write_node(parent)
        self._refresh_ancestors(parent.parent)

    def _repair_parent_after_removal(self, parent: _IndexNode) -> None:
        """删除子节点后修复父节点结构。"""
        if parent.parent is None:
            if len(parent.children) == 1:
                child = self._read_node(parent.children[0])
                child.parent = None
                self._write_node(child)
                old_root = parent.page_id
                self._root_page_id = child.page_id
                self._delete_page(old_root)
                self._notify_root_change()
            elif parent.children:
                self._refresh_internal_keys(parent)
                self._write_node(parent)
            else:
                self._root_page_id = parent.page_id
                self._write_node(self._empty_root())
            return
        self._refresh_internal_keys(parent)
        self._write_node(parent)
        if self._node_underfull(parent):
            self._rebalance_empty(parent)
        else:
            self._refresh_ancestors(parent.parent)

    def _reset_storage(self) -> None:
        """清理旧占位树，保留 root page 作为最终根页。"""

        if self._buffer_pool is None or self._root_page_id is None:
            return
        page_ids = self.physical_page_ids()
        for page_id in page_ids:
            if page_id != self._root_page_id:
                self._delete_page(page_id)
        self._write_node(self._empty_root())

    def bulk_load(
        self,
        entries: Iterable[
            IndexPayloadEntry
            | tuple[object | tuple[object, ...], RowId]
            | tuple[object | tuple[object, ...], RowId, Iterable[object] | None]
        ],
    ) -> None:
        """按排序后的输入一次性建立树；每个条目为 (key, row_id, payload)。

        HOW：payload 为空表示纯键索引，此时叶页载荷与旧版完全一致。
        """

        normalized_entries: list[IndexPayloadEntry] = []
        for raw_entry in entries:
            # HOW：同时接受 (key, row_id) 与 (key, row_id, payload)，兼容纯键索引调用方。
            if isinstance(raw_entry, IndexPayloadEntry):
                key = raw_entry.key
                row_id = raw_entry.row_id
                payload = raw_entry.payload
            else:
                if len(raw_entry) == 3:
                    key, row_id, payload = raw_entry
                else:
                    key, row_id = raw_entry
                    payload = None
            normalized_entries.append(
                IndexPayloadEntry(
                    _key(key),
                    row_id,
                    list(payload) if payload is not None else [],
                )
            )
        with self._lock:
            self._ensure_alive()
            if not self._persistent:
                self._keys.clear()
                self._values.clear()
                normalized_entries.sort(
                    key=lambda item: (
                        tuple(_value_order(value) for value in item.key),
                        item.row_id.as_tuple(),
                    )
                )
                for entry in normalized_entries:
                    self._insert_memory(entry.key, entry.row_id)
                return
            normalized_entries.sort(
                key=lambda item: (
                    tuple(_value_order(value) for value in item.key),
                    item.row_id.as_tuple(),
                )
            )
            deduplicated: list[IndexPayloadEntry] = []
            for entry in normalized_entries:
                if (
                    deduplicated
                    and _compare_entries(
                        deduplicated[-1].key,
                        deduplicated[-1].row_id,
                        entry.key,
                        entry.row_id,
                    )
                    == 0
                ):
                    continue
                deduplicated.append(entry)
            normalized_entries = deduplicated
            if self.unique:
                previous: Key | None = None
                for entry in normalized_entries:
                    if (
                        previous is not None
                        and _compare_keys(previous, entry.key) == 0
                    ):
                        raise ExecutionError(f"唯一索引冲突: {entry.key!r}")
                    previous = entry.key
            self._reset_storage()
            if not normalized_entries:
                self._write_node(self._empty_root())
                return
            # HOW：按“条目编码长度”做增量容量核算。原实现每行都复制当前块并调 `_node_fits`，
            # 而 `_node_fits` 会 json.dumps 整个候选节点 → O(行数 × 叶大小)，60k 行要十几分钟。
            usable = self.page_size - Page.HEADER_SIZE
            leaf_overhead = _leaf_payload_overhead(self.payload_codec)
            chunks: list[list[IndexPayloadEntry]] = []
            current: list[IndexPayloadEntry] = []
            used = leaf_overhead
            for entry in normalized_entries:
                entry_bytes = _entry_payload_size(
                    entry.key, entry.row_id, entry.payload, self.payload_codec
                )
                if entry_bytes + leaf_overhead > usable:
                    raise StorageError("单条索引键和 RowId 超过页容量")
                if current and used + entry_bytes + INDEX_LINK_RESERVE > usable:
                    chunks.append(current)
                    current = []
                    used = leaf_overhead
                current.append(entry)
                used += entry_bytes
            if current:
                chunks.append(current)
            leaf_nodes: list[_IndexNode] = []
            if len(chunks) == 1:
                leaf = self._empty_root()
                leaf.keys = [item.key for item in chunks[0]]
                leaf.row_ids = [item.row_id for item in chunks[0]]
                leaf.payloads = [list(item.payload) for item in chunks[0]]
                self._write_node(leaf)
                return
            # root 页将改成内部页，因此所有叶子都使用新页。
            for chunk in chunks:
                leaf = self._new_node(leaf=True, level=0)
                leaf.keys = [item.key for item in chunk]
                leaf.row_ids = [item.row_id for item in chunk]
                leaf.payloads = [list(item.payload) for item in chunk]
                leaf_nodes.append(leaf)
            for index, leaf in enumerate(leaf_nodes):
                leaf.prev_page = leaf_nodes[index - 1].page_id if index else None
                leaf.next_page = (
                    leaf_nodes[index + 1].page_id
                    if index + 1 < len(leaf_nodes)
                    else None
                )
                self._write_node(leaf)
            level_children = [leaf.page_id for leaf in leaf_nodes]
            level = 1
            max_keys: dict[int, Key] = {}

            def child_max_key(child_id: int) -> Key:
                """每个子页的最大键只读一次；原实现每个候选都重读之前的子页。"""

                cached = max_keys.get(child_id)
                if cached is None:
                    cached = self._load_max_key(child_id)
                    max_keys[child_id] = cached
                return cached

            while len(level_children) > 1:
                internal_overhead = _leaf_payload_overhead(self.payload_codec)
                groups: list[list[int]] = []
                current_children: list[int] = []
                used = internal_overhead
                for child_id in level_children:
                    entry_bytes = _internal_payload_entry_size(
                        child_max_key(child_id), child_id, self.payload_codec
                    )
                    if entry_bytes + internal_overhead > usable:
                        raise StorageError("索引内部节点无法容纳单个子页")
                    if (
                        len(current_children) >= 2
                        and used + entry_bytes + INDEX_LINK_RESERVE > usable
                    ):
                        groups.append(current_children)
                        current_children = []
                        used = internal_overhead
                    current_children.append(child_id)
                    used += entry_bytes
                if current_children:
                    groups.append(current_children)
                if len(groups) == 1:
                    root = self._empty_root()
                    root.leaf = False
                    root.level = level
                    root.children = groups[0]
                    root.keys = [
                        self._load_max_key(value) for value in root.children[:-1]
                    ]
                    for child_id in root.children:
                        child = self._read_node(child_id)
                        child.parent = root.page_id
                        self._write_node(child)
                    self._write_node(root)
                    return
                created: list[_IndexNode] = []
                for group in groups:
                    internal = self._new_node(leaf=False, level=level)
                    internal.children = group
                    internal.keys = [self._load_max_key(value) for value in group[:-1]]
                    for child_id in group:
                        child = self._read_node(child_id)
                        child.parent = internal.page_id
                        self._write_node(child)
                    self._write_node(internal)
                    created.append(internal)
                level_children = [node.page_id for node in created]
                level += 1

    def _physical_page_ids_unlocked(self, *, readonly: bool = False) -> tuple[int, ...]:
        """返回无需再次加锁的索引物理页号。"""
        if self._root_page_id is None:
            return ()
        queue = [self._root_page_id]
        result: list[int] = []
        visited: set[int] = set()
        while queue:
            page_id = queue.pop(0)
            if page_id in visited or not self._valid_index_page(page_id):
                continue
            visited.add(page_id)
            result.append(page_id)
            node = self._read_node(page_id, readonly=readonly)
            if not node.leaf:
                queue.extend(node.children)
        return tuple(result)

    def physical_page_ids(self, *, readonly: bool = False) -> tuple[int, ...]:
        """返回索引物理页；只读检查可避免触碰 BufferPool 的访问顺序。"""

        with self._lock:
            if not self._persistent:
                return ()
            return self._physical_page_ids_unlocked(readonly=readonly)

    def destroy(self) -> None:
        """删除整个物理索引树，释放 DROP INDEX 使用的页。"""

        with self._lock:
            if self._destroyed:
                return
            if self._persistent:
                for page_id in self._physical_page_ids_unlocked():
                    self._delete_page(page_id)
            self._keys.clear()
            self._values.clear()
            self._destroyed = True

    def snapshot(self, offset: int = 0, limit: int = 100) -> BPlusTreeSnapshot:
        """按键分页返回真实叶子记录，并附带物理节点摘要。"""

        safe_offset = max(0, int(offset))
        safe_limit = max(1, int(limit))
        with self._lock:
            self._ensure_alive()
            if not self._persistent:
                entries: list[IndexSnapshotEntry] = []
                for key in self._keys[safe_offset : safe_offset + safe_limit]:
                    values = self._values[_memory_key(key)]
                    entries.append(
                        IndexSnapshotEntry(
                            key=key,
                            row_ids=tuple(sorted(values)[:100]),
                            row_count=len(values),
                            rows_truncated=len(values) > 100,
                        )
                    )
                return BPlusTreeSnapshot(
                    entries=tuple(entries),
                    total=len(self._keys),
                    offset=safe_offset,
                    limit=safe_limit,
                    representation="ordered_leaf_array",
                    unique=self.unique,
                    physical=False,
                    format="memory_ordered_leaf",
                )
            entries = []
            total = 0
            current_key: Key | None = None
            current_rows: list[RowId] = []
            current_row_count = 0
            for entry in self._iter_entries(readonly=True):
                if (
                    current_key is None
                    or _compare_keys(current_key, entry.key) != 0
                ):
                    if current_key is not None:
                        if safe_offset <= total < safe_offset + safe_limit:
                            entries.append(
                                IndexSnapshotEntry(
                                    key=current_key,
                                    row_ids=tuple(current_rows[:100]),
                                    row_count=current_row_count,
                                    rows_truncated=current_row_count > 100,
                                )
                            )
                        total += 1
                    current_key = entry.key
                    current_rows = [entry.row_id]
                    current_row_count = 1
                else:
                    current_row_count += 1
                    if len(current_rows) < 101:
                        current_rows.append(entry.row_id)
            if current_key is not None:
                if safe_offset <= total < safe_offset + safe_limit:
                    entries.append(
                        IndexSnapshotEntry(
                            key=current_key,
                            row_ids=tuple(current_rows[:100]),
                            row_count=current_row_count,
                            rows_truncated=current_row_count > 100,
                        )
                    )
                total += 1
            page_ids = self._physical_page_ids_unlocked(readonly=True)
            nodes: list[IndexNodeSnapshot] = []
            for page_id in page_ids[:256]:
                node = self._read_node(page_id, readonly=True)
                nodes.append(
                    IndexNodeSnapshot(
                        page_id=node.page_id,
                        node_type="leaf" if node.leaf else "internal",
                        level=node.level,
                        parent_page_id=node.parent,
                        next_page_id=node.next_page,
                        prev_page_id=node.prev_page,
                        key_count=len(node.keys),
                        child_count=len(node.children),
                        children=tuple(node.children) if not node.leaf else (),
                        min_key=self._load_min_key(page_id, readonly=True)
                        if node.keys
                        else None,
                        max_key=self._load_max_key(page_id, readonly=True)
                        if node.keys
                        else None,
                    )
                )
            root = (
                self._read_node(self._root_page_id, readonly=True)
                if self._root_page_id is not None
                else None
            )
            return BPlusTreeSnapshot(
                entries=tuple(entries),
                total=total,
                offset=safe_offset,
                limit=safe_limit,
                # 该字段是旧工作台 API 的兼容值；physical/format 才是新语义。
                representation="ordered_leaf_array",
                format="disk_bplus_tree_v1",
                physical=True,
                unique=self.unique,
                root_page_id=self._root_page_id,
                height=0 if root is None else root.level + 1,
                page_count=len(page_ids),
                page_ids=tuple(page_ids[:256]),
                # 页面地图需要把当前索引的所有物理页和表绑定，不能复用节点图的 256 页展示上限。
                all_page_ids=tuple(page_ids),
                pages_truncated=len(page_ids) > 256,
                nodes=tuple(nodes),
                limitation="索引键、RowId、内部节点和叶子链均已落盘；单页采用可检查的 MBIX JSON 编码。",
            )


class IndexManager:
    """按索引名管理内存或落盘 B+Tree。"""

    def __init__(self) -> None:
        """初始化实例所需的状态和依赖。"""
        self._indexes: dict[str, BPlusTree] = {}

    @staticmethod
    def _normalize(name: str) -> str:
        """将索引键规范化为统一的元组表示。"""
        return name.strip().lower()

    def create(
        self,
        name: str,
        *,
        unique: bool = False,
        buffer_pool: "BufferPool | None" = None,
        root_page_id: PageId | int | None = None,
        on_root_change: Callable[[int], None] | None = None,
    ) -> BPlusTree:
        """创建并注册新的索引。"""
        key = self._normalize(name)
        if key in self._indexes:
            raise ExecutionError(f"索引 {name!r} 已存在")
        tree = BPlusTree(
            unique=unique,
            buffer_pool=buffer_pool,
            root_page_id=root_page_id,
            on_root_change=on_root_change,
        )
        self._indexes[key] = tree
        return tree

    def drop(self, name: str) -> BPlusTree | None:
        """删除索引并释放其资源。"""
        return self._indexes.pop(self._normalize(name), None)

    def get(self, name: str) -> BPlusTree:
        """按键查找索引条目。"""
        try:
            return self._indexes[self._normalize(name)]
        except KeyError as exc:
            raise ExecutionError(f"索引 {name!r} 不存在") from exc

    def items(self) -> Iterable[tuple[str, BPlusTree]]:
        """返回索引中的全部条目。"""
        return tuple(self._indexes.items())


__all__ = ["BPlusTree", "IndexManager", "index_page_info"]
