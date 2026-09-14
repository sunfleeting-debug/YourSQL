"""B+Tree 叶子定位、叶子链遍历和索引扫描。"""

from __future__ import annotations

from collections.abc import Iterator

from yoursql.common.errors import StorageError
from yoursql.common.types import RowId
from yoursql.storage.index.node import _IndexNode
from yoursql.storage.index.ordering import (
    Key,
    _compare_entries,
    _compare_keys,
    _compare_values,
    _key,
    _memory_key,
)
from yoursql.storage.index.protocols import _TreeContext
from yoursql.storage.index.types import IndexEntry, IndexPayloadEntry


class _TreeSearchMixin(_TreeContext):
    """负责叶子定位、叶子链遍历和索引扫描。"""

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
                        IndexPayloadEntry(key, row_id, leaf.payload_at(position))
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
                        result.append(IndexPayloadEntry(key, row_id, payload))
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
                            result.append(IndexPayloadEntry(key, row_id, payload))
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
