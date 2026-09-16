"""内存和持久化 B+Tree 的核心对象。"""

from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING, Callable

from yoursql.common.codec import PayloadCodec
from yoursql.common.codec import payload_codec as get_payload_codec
from yoursql.common.errors import StorageError
from yoursql.common.types import PageId, RowId
from yoursql.storage.page import PageType
from yoursql.storage.index.node import _IndexNode
from yoursql.storage.index.ordering import Key, MemoryKey
from yoursql.storage.index.tree_bulk import _TreeBulkMixin
from yoursql.storage.index.tree_inspection import _TreeInspectionMixin
from yoursql.storage.index.tree_mutation import _TreeMutationMixin
from yoursql.storage.index.tree_search import _TreeSearchMixin
from yoursql.storage.index.tree_storage import _TreeStorageMixin

if TYPE_CHECKING:
    from yoursql.storage.buffer import BufferPool


# WHY：BPlusTree 只保留共享状态和公共入口；按职责拆分 mixin，降低单文件复杂度，
# 同时让存储、查询、修改、批量装载和检查逻辑可以独立阅读与测试。
class BPlusTree(
    _TreeStorageMixin,
    _TreeSearchMixin,
    _TreeMutationMixin,
    _TreeBulkMixin,
    _TreeInspectionMixin,
):
    """支持内存和持久化两种模式的 B+Tree。"""

    def __init__(
        self,
        unique: bool = False,
        *,
        buffer_pool: "BufferPool | None" = None,
        root_page_id: PageId | int | None = None,
        on_root_change: Callable[[int], None] | None = None,
    ) -> None:
        """创建内存索引或绑定到已有物理根页的持久化索引。

        Args:
            unique: 为真时，同一个逻辑 key 只能关联一个 RowId；不同 RowId
                的重复 key 会在插入或批量装载时抛出冲突异常。
            buffer_pool: 持久化模式使用的页缓存。为空时使用内存中的有序结构，
                不产生 INDEX 页面。
            root_page_id: 要恢复的持久化根页号。持久化新索引为空时传空值，
                构造函数会分配一个空的 INDEX 叶页作为根。
            on_root_change: 根页因分裂或收缩发生变化时调用，参数为新的根页号。

        Raises:
            StorageError: 根页不存在、不是 INDEX 页、内容损坏，或无法分配物理页。
        """
        self.unique = bool(unique)
        self._buffer_pool = buffer_pool
        self._persistent = buffer_pool is not None
        self._on_root_change = on_root_change
        self._lock = RLock()
        self._destroyed = False
        self._root_page_id: int | None = None
        # === 兼容旧内存索引表示 ===
        # 内存模式保留旧的有序数组接口；持久化模式使用 B+Tree 页结构。
        self._keys: list[Key] = []
        # Python 字典会把 False/0、True/1 当成同一个键；使用带类型标签的
        # 比较表示，才能与落盘模式保持一致的异构键排序语义。
        self._values: dict[MemoryKey, set[RowId]] = {}
        if not self._persistent:
            return
        selected_root = None if root_page_id is None else int(root_page_id)

        # 若无根节点，new一个
        if selected_root is None:
            # WHY：空树用一个空叶子作为根即可直接承载第一次插入；若先创建内部根，
            # 既会增加无意义的树层下降，也无法满足内部节点 children = keys + 1 的结构约束。
            page = self._buffer_pool.new_page(PageType.INDEX)
            selected_root = page.page_id
            self._root_page_id = selected_root
            self._write_node(_IndexNode(selected_root, leaf=True))
            self._notify_root_change()
            return
        if not self._valid_index_page(selected_root):
            raise StorageError(f"索引根页 {selected_root} 不存在或不是 INDEX 页")
        self._root_page_id = selected_root

        # 校验
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
