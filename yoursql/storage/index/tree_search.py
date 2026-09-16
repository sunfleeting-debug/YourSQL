"""B+Tree 叶子定位、叶子链遍历和索引扫描。

内部页约定：``keys[i]`` 保存 ``children[i]`` 子树的最大键，因此 n 个子页只保存
n - 1 个分隔键；最后一个子页没有对应的分隔键。
"""

from __future__ import annotations

from collections.abc import Iterator

from yoursql.common.errors import StorageError
from yoursql.common.types import RowId
from yoursql.storage.index.node import _IndexNode
from yoursql.storage.index.ordering import (
    Key,
    _compare_keys,
    _compare_values,
    _key,
    _lower_bound,
    _lower_bound_entries,
    _memory_key,
)
from yoursql.storage.index.protocols import _TreeContext
from yoursql.storage.index.types import IndexEntry, IndexPayloadEntry


class _TreeSearchMixin(_TreeContext):
    """负责叶子定位、叶子链遍历和索引扫描。

    宿主状态：
        _root_page_id: int | None
        _persistent: bool
        _lock: AbstractContextManager[object]
        _keys: list[Key]
        _values: dict[MemoryKey, set[RowId]]

    协作方法：
        _ensure_alive() -> None
        _read_node(page_id: int, *, readonly: bool = False) -> _IndexNode
    """

    def _find_leaf(self, target: Key, *, readonly: bool = False) -> _IndexNode:
        """沿 B+Tree 查找包含目标键的叶节点。"""
        if self._root_page_id is None:
            raise StorageError("索引没有根页")
        node = self._read_node(self._root_page_id, readonly=readonly)
        while not node.leaf:
            if not node.children:
                raise StorageError(f"索引内部页 {node.page_id} 没有子页")
            # WHY：内部页的 keys 是有序分隔键，children 数量恒为 keys + 1；
            # 用 lower_bound 找到第一个不小于目标键的分隔位置，避免宽节点逐项扫描。
            child_index = _lower_bound(node.keys, target)
            node = self._read_node(
                node.children[child_index],
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

        # 向前搜索
        while leaf.prev_page is not None:
            previous = self._read_node(leaf.prev_page, readonly=readonly)
            if not previous.keys or _compare_keys(previous.keys[-1], target) < 0:
                break
            leaf = previous

        # 向后搜索
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

        # 到达叶子结点
        leaf = self._find_leaf(target)

        # 前向搜索至最前面
        while leaf.prev_page is not None:
            previous = self._read_node(leaf.prev_page)
            if not previous.keys or _compare_keys(previous.keys[-1], target) < 0:
                break
            leaf = previous

        # 顺序遍历后续leaf，内部二分查找合适的position
        while True:
            if not leaf.keys:
                return leaf
            position = _lower_bound_entries(leaf.keys, leaf.row_ids, target, row_id)
            if position < len(leaf.keys):
                # HOW：叶页内按完整 (key, RowId) 二分；若当前页仍全小于目标，
                # 才沿重复 key 的叶链继续向后寻找。
                return leaf
            if leaf.next_page is None:
                return leaf
            next_leaf = self._read_node(leaf.next_page)
            if next_leaf.keys and _compare_keys(next_leaf.keys[0], target) > 0:
                return leaf
            leaf = next_leaf

    def search(self, key: object | tuple[object, ...]) -> tuple[RowId, ...]:
        """查找与指定逻辑键相等的全部 RowId。

        Args:
            key: 标量键，或按联合索引列顺序提供的元组键。输入会按索引的
                异构值排序规则规范化。

        Returns:
            按 ``RowId`` 排序的元组；没有匹配项时返回空元组。非唯一索引可能
            返回多个 RowId，唯一索引通常最多返回一个。

        Note:
            只返回物理位置，不返回覆盖索引的 payload；需要精确键对应的 payload
            时使用 ``search_entries()``。
        """

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

    def search_entries(
        self, key: object | tuple[object, ...]
    ) -> tuple[IndexPayloadEntry, ...]:
        """查找精确键对应的索引条目，并返回覆盖列值。

        Args:
            key: 标量键，或按联合索引列顺序提供的完整元组键。

        Returns:
            按 ``RowId`` 排序的 ``IndexPayloadEntry`` 元组。每项包含规范化后的
            key、堆表位置和按 INCLUDE 列顺序排列的 payload；纯键索引或内存索引
            的 payload 为空列表。

        Note:
            该接口是精确键查找的覆盖索引版本；它不会读取 heap。若需要范围、
            前缀或部分联合键查询，应使用相应的扫描接口。
        """

        return self.range_scan_entries(
            key,
            key,
            include_low=True,
            include_high=True,
        )

    def range_scan(
        self,
        low: object | tuple[object, ...] | None = None,
        high: object | tuple[object, ...] | None = None,
        *,
        include_low: bool = True,
        include_high: bool = True,
    ) -> tuple[IndexEntry, ...]:
        """扫描区间内的索引条目。

        Args:
            low: 下界键；为 ``None`` 时从索引最小键开始。
            high: 上界键；为 ``None`` 时扫描到索引末尾。
            include_low: 是否包含等于 ``low`` 的条目。
            include_high: 是否包含等于 ``high`` 的条目。

        Returns:
            按 ``(key, RowId)`` 排序的 ``IndexEntry`` 元组。每项包含索引键和
            对应堆表位置，不包含覆盖列 payload。

        Note:
            不传上下界时等价于全索引扫描；联合键的上下界必须使用与索引列顺序
            一致的元组。
        """

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
        """扫描键范围，并返回条目携带的覆盖列值。

        Args:
            low: 下界键；为 ``None`` 时从索引最小键开始。
            high: 上界键；为 ``None`` 时扫描到索引末尾。
            include_low: 是否包含等于 ``low`` 的条目。
            include_high: 是否包含等于 ``high`` 的条目。

        Returns:
            按 ``(key, RowId)`` 排序的 ``IndexPayloadEntry`` 元组。其 ``payload``
            与创建该索引时的 INCLUDE 列顺序一致；纯键索引或内存索引返回空列表。

        Note:
            该接口供覆盖索引和 Index Only Scan 使用；它不会自动读取 heap，
            未被索引覆盖的列仍需调用方根据 ``row_id`` 回表获取。
        """

        lower = _key(low) if low is not None else None
        upper = _key(high) if high is not None else None
        with self._lock:
            self._ensure_alive()

            # 测试用的内存模式，正常遍历
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

            # 找第一个叶子结点
            if lower is None:
                leaf = next(self._iter_leaf_nodes(readonly=False), None)
            else:
                leaf = self._find_leaf(lower)
                while leaf.prev_page is not None:
                    previous = self._read_node(leaf.prev_page)
                    if not previous.keys or _compare_keys(previous.keys[-1], lower) < 0:
                        break
                    leaf = previous

            # 正常遍历
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
        """扫描以指定联合键前缀开头的索引条目。

        Args:
            prefix: 联合索引的前缀键。例如索引为 ``(tenant_id, created_at)`` 时，
                可传 ``(tenant_id,)``；标量值表示单列索引前缀。

        Returns:
            按 ``(key, RowId)`` 排序的 ``IndexEntry`` 元组。

        Note:
            该方法不返回覆盖列值；需要覆盖列时使用
            ``range_scan_prefix_entries()``。
        """

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
        """在固定联合键前缀后的下一个键元素上执行范围扫描。

        Args:
            prefix: 联合索引前缀。例如索引为 ``(tenant_id, created_at)`` 时传
                ``(tenant_id,)``。
            low: 前缀后一个键元素的下界；为空时从该前缀的最小值开始。
            high: 前缀后一个键元素的上界；为空时扫描到该前缀的末尾。
            include_low: 是否包含等于 ``low`` 的条目。
            include_high: 是否包含等于 ``high`` 的条目。

        Returns:
            满足前缀和范围条件、并按 ``(key, RowId)`` 排序的 ``IndexEntry`` 元组。

        Example:
            ``range_scan_prefix((tenant_id,), low, high)`` 可扫描一个租户的时间
            范围，不需要构造后续键的正负无穷哨兵值。
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
        """执行联合键前缀范围扫描，并返回覆盖列值。

        Args:
            prefix: 联合索引前缀，按索引列顺序提供。
            low: 前缀后一个键元素的下界；为空时不限制下界。
            high: 前缀后一个键元素的上界；为空时不限制上界。
            include_low: 是否包含等于 ``low`` 的条目。
            include_high: 是否包含等于 ``high`` 的条目。

        Returns:
            按 ``(key, RowId)`` 排序的 ``IndexPayloadEntry`` 元组。每项的
            ``payload`` 顺序与索引定义中的 INCLUDE 列顺序一致。

        Note:
            只扫描满足前缀的键；如果没有 INCLUDE 列，返回项中的 ``payload``
            为空列表。
        """

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

            # 测试用的内存模式，正常遍历
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

            # 找第一个
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

                    # 处理key为（[prefix]）而不是（[prefix], k）的情况
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
        """返回索引中的全部键和 RowId。

        Returns:
            按 ``(key, RowId)`` 排序的 ``IndexEntry`` 元组；索引为空时返回空元组。

        Note:
            该方法会完整物化扫描结果，适合检查或小型索引；大索引的业务查询
            应优先使用范围扫描接口。
        """
        return self.range_scan()
