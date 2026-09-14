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
    _lower_bound,
    _memory_key,
)
from yoursql.storage.index.protocols import _TreeContext
from yoursql.storage.index.types import IndexPayloadEntry


class _TreeMutationMixin(_TreeContext):
    """负责索引插入、删除、分裂、合并和内存模式操作。"""

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
                kept.append(IndexPayloadEntry(key, value, leaf.payload_at(position)))
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
                    node.insert_entry(0, moved.key, moved.row_id, moved.payload)
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

    def destroy(self) -> None:
        """删除整个物理索引树，释放 DROP INDEX 使用的页。"""

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
