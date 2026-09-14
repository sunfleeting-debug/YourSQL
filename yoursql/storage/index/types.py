"""索引对外结果类型和物理检查快照。"""

from __future__ import annotations

from dataclasses import dataclass

from yoursql.common.types import RowId
from yoursql.storage.index.ordering import Key


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


@dataclass(frozen=True)
class IndexPageEntry:
    """【前端特供】索引页检查结果中的叶子条目。"""

    key: Key
    row_id: RowId

    def to_dict(self) -> dict[str, object]:
        """将对象转换为可序列化的字典。"""
        return {"key": list(self.key), "row_id": self.row_id.as_tuple()}


@dataclass(frozen=True)
class IndexPageInfo:
    """【前端特供】单个 INDEX 页的有界结构化检查结果。"""

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
    """【前端特供】B+Tree 按键聚合后的检查条目。"""

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
    """【前端特供】B+Tree 物理节点的检查摘要。"""

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
    """【前端特供】B+Tree 的内存/落盘统一检查快照。"""

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
