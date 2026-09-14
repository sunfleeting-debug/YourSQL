"""B+Tree mixin 共享的静态类型契约。"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Callable, Protocol

from yoursql.common.codec import PayloadCodec
from yoursql.common.types import RowId
from yoursql.storage.index.node import _IndexNode
from yoursql.storage.index.ordering import Key
from yoursql.storage.index.types import BPlusTreeSnapshot, IndexEntry, IndexPayloadEntry

if TYPE_CHECKING:
    from yoursql.storage.buffer import BufferPool


class _TreeContext(Protocol):
    """描述所有 mixin 都可以使用的 B+Tree 宿主成员。

    mixin 在运行时通过多重继承组合到 ``BPlusTree``；Pylance 无法仅凭继承顺序
    推断这些成员，所以集中声明一份契约。该类只提供类型信息，不承载业务实现。
    """

    unique: bool
    _buffer_pool: BufferPool | None
    _persistent: bool
    _on_root_change: Callable[[int], None] | None
    _lock: AbstractContextManager[object]
    _destroyed: bool
    _root_page_id: int | None
    _keys: list[Key]
    _values: dict[tuple[tuple[int, object], ...], set[RowId]]

    @property
    def page_size(self) -> int: ...

    @property
    def payload_codec(self) -> PayloadCodec: ...

    def _ensure_alive(self) -> None: ...

    def _valid_index_page(self, page_id: int) -> bool: ...

    def _notify_root_change(self) -> None: ...

    def _read_node(self, page_id: int, *, readonly: bool = False) -> _IndexNode: ...

    def _write_node(self, node: _IndexNode) -> None: ...

    def _new_node(
        self, *, leaf: bool, level: int, parent: int | None = None
    ) -> _IndexNode: ...

    def _delete_page(self, page_id: int) -> None: ...

    def _node_fits(self, node: _IndexNode, page_size: int) -> bool: ...

    def _node_underfull(self, node: _IndexNode) -> bool: ...

    def _empty_root(self) -> _IndexNode: ...

    def _load_max_key(self, page_id: int, *, readonly: bool = False) -> Key: ...

    def _load_min_key(self, page_id: int, *, readonly: bool = False) -> Key: ...

    def _refresh_internal_keys(self, node: _IndexNode) -> None: ...

    def _refresh_ancestors(self, parent_id: int | None) -> None: ...

    def _find_leaf(self, target: Key, *, readonly: bool = False) -> _IndexNode: ...

    def _iter_leaf_nodes(self, *, readonly: bool = False) -> Iterator[_IndexNode]: ...

    def _iter_entries(self, *, readonly: bool = False) -> Iterator[IndexEntry]: ...

    def _find_equal_leaves(
        self, target: Key, *, readonly: bool = False
    ) -> Iterator[_IndexNode]: ...

    def _find_insert_leaf(self, target: Key, row_id: RowId) -> _IndexNode: ...

    def _insert_memory(self, normalized: Key, row_id: RowId) -> None: ...

    def _delete_memory(self, normalized: Key, row_id: RowId | None) -> None: ...

    def _split_position(self, node: _IndexNode, candidates: Iterable[int]) -> int: ...

    def _split_leaf_and_propagate(self, leaf: _IndexNode) -> None: ...

    def _split_internal_and_propagate(self, node: _IndexNode) -> None: ...

    def _create_root(self, left: _IndexNode, right: _IndexNode) -> None: ...

    def _delete_persistent(self, normalized: Key, row_id: RowId | None) -> None: ...

    def _rebalance_empty(self, node: _IndexNode) -> None: ...

    def _repair_parent_after_removal(self, parent: _IndexNode) -> None: ...

    def _reset_storage(self) -> None: ...

    def search(self, key: object | tuple[object, ...]) -> tuple[RowId, ...]: ...

    def range_scan(
        self,
        low: object | tuple[object, ...] | None = None,
        high: object | tuple[object, ...] | None = None,
        *,
        include_low: bool = True,
        include_high: bool = True,
    ) -> tuple[IndexEntry, ...]: ...

    def range_scan_entries(
        self,
        low: object | tuple[object, ...] | None = None,
        high: object | tuple[object, ...] | None = None,
        *,
        include_low: bool = True,
        include_high: bool = True,
    ) -> tuple[IndexPayloadEntry, ...]: ...

    def prefix_scan(
        self, prefix: object | tuple[object, ...]
    ) -> tuple[IndexEntry, ...]: ...

    def range_scan_prefix(
        self,
        prefix: object | tuple[object, ...],
        low: object | None = None,
        high: object | None = None,
        *,
        include_low: bool = True,
        include_high: bool = True,
    ) -> tuple[IndexEntry, ...]: ...

    def range_scan_prefix_entries(
        self,
        prefix: object | tuple[object, ...],
        low: object | None = None,
        high: object | None = None,
        *,
        include_low: bool = True,
        include_high: bool = True,
    ) -> tuple[IndexPayloadEntry, ...]: ...

    def all_items(self) -> tuple[IndexEntry, ...]: ...

    def insert(
        self,
        key: object | tuple[object, ...],
        row_id: RowId,
        payload: Iterable[object] | None = None,
    ) -> None: ...

    def has_entries(self) -> bool: ...

    def delete(
        self, key: object | tuple[object, ...], row_id: RowId | None = None
    ) -> None: ...

    def bulk_load(
        self,
        entries: Iterable[
            IndexPayloadEntry
            | tuple[object | tuple[object, ...], RowId]
            | tuple[object | tuple[object, ...], RowId, Iterable[object] | None]
        ],
    ) -> None: ...

    def physical_page_ids(self, *, readonly: bool = False) -> tuple[int, ...]: ...

    def _physical_page_ids_unlocked(
        self, *, readonly: bool = False
    ) -> tuple[int, ...]: ...

    def snapshot(self, offset: int = 0, limit: int = 100) -> BPlusTreeSnapshot: ...

    def destroy(self) -> None: ...
