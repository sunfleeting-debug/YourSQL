"""B+Tree 节点页存储、容量判断和根节点辅助逻辑。"""

from __future__ import annotations

from yoursql.common.errors import StorageError
from yoursql.storage.page import Page, PageType
from yoursql.storage.index.codec import INDEX_LINK_RESERVE
from yoursql.storage.index.node import _IndexNode
from yoursql.storage.index.ordering import Key, _compare_keys
from yoursql.storage.index.protocols import _TreeContext


class _TreeStorageMixin(_TreeContext):
    """负责索引节点的页存储、容量判断和根节点辅助操作。"""

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
