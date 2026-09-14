"""B+Tree 批量装载逻辑。"""

from __future__ import annotations

from collections.abc import Iterable

from ...common.errors import ExecutionError, StorageError
from ...common.types import RowId
from ..page import Page
from .codec import (
    INDEX_LINK_RESERVE,
    _entry_payload_size,
    _internal_payload_entry_size,
    _leaf_payload_overhead,
)
from .node import _IndexNode
from .ordering import Key, _compare_entries, _compare_keys, _key, _value_order
from .protocols import _TreeContext
from .types import IndexPayloadEntry


class _TreeBulkMixin(_TreeContext):
    """负责索引批量装载。"""

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
                    if previous is not None and _compare_keys(previous, entry.key) == 0:
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
