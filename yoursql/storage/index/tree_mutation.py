"""B+Tree 插入、删除、分裂和重平衡逻辑。"""

from __future__ import annotations

from collections.abc import Iterable

from yoursql.common.errors import ExecutionError, StorageError
from yoursql.common.types import RowId
from yoursql.storage.index.node import _IndexNode
from yoursql.storage.index.ordering import (
    Key,
    _compare_entries,
    _compare_keys,
    _key,
    _lower_bound_entries,
    _lower_bound,
    _memory_key,
)
from yoursql.storage.index.protocols import _TreeContext
from yoursql.storage.index.types import IndexPayloadEntry


class _TreeMutationMixin(_TreeContext):
    """负责索引插入、删除、分裂、合并和内存模式操作。

    宿主状态：
        unique: bool
        _persistent: bool
        _lock: AbstractContextManager[object]
        _root_page_id: int | None
        _keys: list[Key]
        _values: dict[MemoryKey, set[RowId]]

    协作方法：
        _find_insert_leaf(target: Key, row_id: RowId) -> _IndexNode
        _find_equal_leaves(target: Key) -> Iterator[_IndexNode]
        _read_node(page_id: int, *, readonly: bool = False) -> _IndexNode
        _write_node(node: _IndexNode) -> None
        _split_internal_and_propagate(node: _IndexNode) -> None
        bulk_load(entries: Iterable[object]) -> None
    """

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
        """插入一条索引记录。

        Args:
            key: 标量索引键，或按联合索引列顺序排列的元组键。
            row_id: 堆表记录的物理位置；同一个 key 的多个 RowId 可用于非唯一索引。
            payload: 按 INCLUDE 列定义顺序排列的附加列值。纯键索引传 ``None``
                或空迭代值；持久化索引会将其保存到叶页，内存索引不保存该值。

        Raises:
            ExecutionError: 唯一索引中已经存在相同 key 的其它 RowId。
            StorageError: 索引已释放、页面无效，或新条目导致节点无法容纳。

        Note:
            相同的 ``(key, row_id)`` 已存在时视为幂等操作，不会重复插入。
        """

        normalized = _key(key)
        normalized_payload = list(payload) if payload is not None else None
        with self._lock:
            self._ensure_alive()
            if not self._persistent:
                self._insert_memory(normalized, row_id)
                return
            leaf = self._find_insert_leaf(normalized, row_id)
            insert_position = _lower_bound_entries(
                leaf.keys,
                leaf.row_ids,
                normalized,
                row_id,
            )
            current_key = (
                leaf.keys[insert_position]
                if insert_position < len(leaf.keys)
                else None
            )
            if current_key is not None:
                current_row_id = leaf.row_ids[insert_position]
                if _compare_entries(
                    current_key,
                    current_row_id,
                    normalized,
                    row_id,
                ) == 0:
                    return

            # unique的检测
            same_key_at_position = current_key is not None and _compare_keys(
                current_key,
                normalized,
            ) == 0
            previous_key = (
                leaf.keys[insert_position - 1]
                if insert_position > 0
                else None
            )
            same_key_before_position = previous_key is not None and _compare_keys(
                previous_key,
                normalized,
            ) == 0
            if self.unique and (same_key_at_position or same_key_before_position):
                raise ExecutionError(f"唯一索引冲突: {normalized!r}")

            # 插入
            leaf.insert_entry(
                insert_position,
                normalized,
                row_id,
                normalized_payload,
            )
            if self._node_fits(leaf, self.page_size):
                self._write_node(leaf)
                self._refresh_ancestors(leaf.parent)
                return
            self._split_leaf_and_propagate(leaf)

    def has_entries(self) -> bool:
        """判断索引当前是否至少包含一条索引记录。

        Returns:
            索引有条目时返回 ``True``，空索引或尚未写入条目时返回 ``False``。

        Note:
            持久化模式只读取根页及其结构信息，不遍历全部叶子；主要供批量导入
            判断是否可以延迟建树。
        """

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

        # right
        right = self._new_node(leaf=True, level=leaf.level, parent=leaf.parent)
        right.keys = leaf.keys[position:]
        right.row_ids = leaf.row_ids[position:]
        right.payloads = leaf.payloads[position:]

        # left/self
        leaf.keys = leaf.keys[:position]
        leaf.row_ids = leaf.row_ids[:position]
        leaf.payloads = leaf.payloads[:position]

        # 双向链表
        right.next_page = leaf.next_page
        right.prev_page = leaf.page_id
        if leaf.next_page is not None:
            next_node = self._read_node(leaf.next_page)
            next_node.prev_page = right.page_id
            self._write_node(next_node)
        leaf.next_page = right.page_id

        # updeta
        self._write_node(leaf)
        self._write_node(right)

        # parent
        if leaf.parent is None:
            self._create_root(leaf, right)
            return
        parent = self._read_node(leaf.parent)
        index = parent.children.index(leaf.page_id)
        parent.children.insert(index + 1, right.page_id)

        # 递归向上
        self._refresh_internal_keys(parent)
        if self._node_fits(parent, self.page_size):
            self._write_node(parent)
            self._refresh_ancestors(parent.parent)
        else:
            self._split_internal_and_propagate(parent)

    def _split_internal_and_propagate(self, node: _IndexNode) -> None:
        """
        分裂内部节点并向上层传播分隔键。
        由于插入是单条记录插入的，所以仅考虑一分为二，不考虑多路分裂。
        对于批量插入，不走该分裂过程。
        """
        if node.leaf:
            raise StorageError("叶页不能使用内部页分裂流程")
        # 内部页按 child 数量切分；分隔键总是从子页最大键重新计算。
        middle = max(1, len(node.children) // 2)

        # 候选切分位置
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

        # 开始分裂
        left_children = node.children[:selected]
        right_children = node.children[selected:]

        # left
        node.children = left_children
        node.keys = selected_left_keys

        # right
        right = self._new_node(leaf=False, level=node.level, parent=node.parent)
        right.children = right_children
        right.keys = selected_right_keys
        for child_id in right.children:
            child = self._read_node(child_id)
            child.parent = right.page_id
            self._write_node(child)
        self._write_node(node)
        self._write_node(right)

        # parent
        if node.parent is None:
            self._create_root(node, right)
            return
        parent = self._read_node(node.parent)
        index = parent.children.index(node.page_id)
        parent.children.insert(index + 1, right.page_id)
        self._refresh_internal_keys(parent)

        # 递归
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

    def _delete_persistent(self, normalized: Key, row_id: RowId | None) -> None:
        """从持久化索引删除键和值。"""
        if row_id is None:
            # WHY：一个重复键可能横跨多个叶页。先批量清空这些叶页会让父节点
            # 同时看到多个空子页，无法计算分隔键；把剩余条目重新打包可以在
            # 整个操作期间保持 B+Tree 的叶链和内部页不变量，且根页仍复用原页号。
            entries: list[IndexPayloadEntry] = []
            for leaf in self._iter_leaf_nodes(readonly=True):
                leaf.ensure_payloads()
                entries.extend(
                    IndexPayloadEntry(
                        key,
                        row_id,
                        list(leaf.payload_at(position)),
                    )
                    for position, (key, row_id) in enumerate(
                        zip(leaf.keys, leaf.row_ids, strict=True)
                    )
                )
            remaining = [
                entry
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
                kept.append(IndexPayloadEntry(key, value, leaf.payload_at(position)))

            # 更新
            leaf.keys = [entry.key for entry in kept]
            leaf.row_ids = [entry.row_id for entry in kept]
            leaf.payloads = [list(entry.payload) for entry in kept]

            # 页数变化，确认删除成功！
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
        """删除索引记录，并按需重新平衡 B+Tree。

        Args:
            key: 要删除的标量键，或按联合索引列顺序排列的元组键。
            row_id: 指定时只删除该 key 对应的一个物理记录；为 ``None`` 时删除
                该 key 的全部 RowId 记录。

        Note:
            删除只修改索引，不删除 heap 中的实际记录。目标不存在时保持幂等，
            不会因为没有匹配项而报错。

        Raises:
            StorageError: 索引已释放、索引页损坏，或持久化页面操作失败。
        """

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

        # 叶子
        if node.leaf:
            # 只从借位后仍能保持非低占用的兄弟页借一条，避免把问题转移给兄弟。
            while self._node_underfull(node):
                borrowed = False

                # 向左兄弟借
                if left is not None and len(left.keys) > 1:
                    left.ensure_payloads()
                    node.ensure_payloads()
                    moved = left.pop_entry()
                    node.insert_entry(0, moved.key, moved.row_id, moved.payload)
                    if self._node_fits(
                        node, self.page_size
                    ) and not self._node_underfull(left):
                        self._write_node(left)
                        self._write_node(node)
                        borrowed = True
                    else:
                        # 借了有明显影响就回退
                        returned = node.pop_entry(0)
                        left.insert_entry(
                            len(left.keys),
                            returned.key,
                            returned.row_id,
                            returned.payload,
                        )

                # 向右兄弟借
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
                        # 借了有明显影响就回退
                        returned = node.pop_entry()
                        right.insert_entry(
                            0,
                            returned.key,
                            returned.row_id,
                            returned.payload,
                        )

                # 借失败
                if not borrowed:
                    break

            # 满足了，就刷盘返回
            if not self._node_underfull(node):
                self._refresh_internal_keys(parent)
                self._write_node(parent)
                self._refresh_ancestors(parent.parent)
                return
            # 借记录失败，说明左右侧都不能借
            # 要么左右侧兄弟的记录数低于一半，要么都不存在

            # 没满足就和左兄弟合并
            if left is not None:
                original = len(left.keys)
                left.extend_from(node)  # 合并
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
                # 回退合并
                del left.keys[original:]
                del left.row_ids[original:]
                del left.payloads[original:]

            # 和右兄弟合并
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
                # 回退合并
                del node.keys[original:]
                del node.row_ids[original:]
                del node.payloads[original:]
            # 变长键可能使两个合法页无法合并；保留当前页并更新边界，不能让删除失败。
            self._write_node(node)
            self._refresh_internal_keys(parent)
            self._write_node(parent)
            self._refresh_ancestors(parent.parent)
            return

        # 非叶子
        while self._node_underfull(node):
            borrowed = False

            # 借左兄弟
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
                    # 回退
                    left.children.append(node.children.pop(0))
                    moved_node.parent = left.page_id
                    self._refresh_internal_keys(left)
                    self._refresh_internal_keys(node)

            # 借右兄弟
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
                    # 回退
                    right.children.insert(0, node.children.pop())
                    moved_node.parent = right.page_id
                    self._refresh_internal_keys(right)
                    self._refresh_internal_keys(node)
            if not borrowed:
                # 解不了，break;
                break

        # 满足，写，return
        if not self._node_underfull(node):
            self._refresh_internal_keys(parent)
            self._write_node(parent)
            self._refresh_ancestors(parent.parent)
            return

        # 接了之后依然不满足，尝试合并
        # 合并left
        if left is not None:
            original = len(left.children)
            left.children.extend(node.children) # 试合并
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
            # 回退
            del left.children[original:]
            self._refresh_internal_keys(left)

        # 合并right
        if right is not None:
            original = len(node.children)
            node.children.extend(right.children) # 试合并
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
            # 回退
            del node.children[original:]
            self._refresh_internal_keys(node)

        # 虽然低负载，但无法借也无法合并，直接最终兜底写入。
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

    def destroy(self) -> None:
        """销毁索引并释放其持久化节点页面。

        持久化模式会遍历索引占用的 INDEX 页并交给 BufferPool 释放；内存模式
        只清空内存中的键和值。调用后索引进入不可用状态，后续读写操作会失败。
        重复调用是安全的，不会重复释放页面。
        """

        with self._lock:
            if self._destroyed:
                return
            if self._persistent:
                page_ids = self._physical_page_ids_unlocked()
                if self._buffer_pool is not None:
                    self._buffer_pool.delete_pages(page_ids)
            self._keys.clear()
            self._values.clear()
            self._destroyed = True
