"""内存和持久化 B+Tree 的核心对象。"""

from __future__ import annotations

from threading import RLock
from typing import TYPE_CHECKING, Callable

from ...common.codec import PayloadCodec
from ...common.codec import payload_codec as get_payload_codec
from ...common.errors import StorageError
from ...common.types import PageId, RowId
from ..page import PageType
from .node import _IndexNode
from .ordering import Key, MemoryKey
from .tree_bulk import _TreeBulkMixin
from .tree_inspection import _TreeInspectionMixin
from .tree_mutation import _TreeMutationMixin
from .tree_search import _TreeSearchMixin
from .tree_storage import _TreeStorageMixin

if TYPE_CHECKING:
    from ..buffer import BufferPool


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
