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
    def page_size(self) -> int:
        """返回索引节点使用的页面大小。"""

        ...

    @property
    def payload_codec(self) -> PayloadCodec:
        """返回索引节点 payload 使用的编解码器。"""

        ...

    def _ensure_alive(self) -> None:
        """确认索引尚未被销毁。"""

        ...

    def _valid_index_page(self, page_id: int) -> bool:
        """判断页号是否指向有效的 INDEX 页。"""

        ...

    def _notify_root_change(self) -> None:
        """通知外部索引根页号发生变化。"""

        ...

    def _read_node(self, page_id: int, *, readonly: bool = False) -> _IndexNode:
        """读取并解析指定物理页中的索引节点。"""

        ...

    def _write_node(self, node: _IndexNode) -> None:
        """将索引节点编码后写入缓存。"""

        ...

    def _new_node(
        self, *, leaf: bool, level: int, parent: int | None = None
    ) -> _IndexNode:
        """分配一个新的索引页并创建节点对象。"""

        ...

    def _delete_page(self, page_id: int) -> None:
        """释放索引节点占用的物理页。"""

        ...

    def _node_fits(self, node: _IndexNode, page_size: int) -> bool:
        """判断节点编码后是否能放入指定页面。"""

        ...

    def _node_underfull(self, node: _IndexNode) -> bool:
        """判断非根节点是否低于最小负载。"""

        ...

    def _empty_root(self) -> _IndexNode:
        """返回当前根页对应的空叶节点。"""

        ...

    def _load_max_key(self, page_id: int, *, readonly: bool = False) -> Key:
        """读取指定子树的最大键。"""

        ...

    def _load_min_key(self, page_id: int, *, readonly: bool = False) -> Key:
        """读取指定子树的最小键。"""

        ...

    def _refresh_internal_keys(self, node: _IndexNode) -> None:
        """根据子树最大键重建内部页分隔键。"""

        ...

    def _refresh_ancestors(self, parent_id: int | None) -> None:
        """向上刷新祖先分隔键并处理溢出。"""

        ...

    def _find_leaf(self, target: Key, *, readonly: bool = False) -> _IndexNode:
        """沿内部页定位一个可能包含目标键的叶页。"""

        ...

    def _iter_leaf_nodes(self, *, readonly: bool = False) -> Iterator[_IndexNode]:
        """按叶子链顺序遍历索引节点。"""

        ...

    def _iter_entries(self, *, readonly: bool = False) -> Iterator[IndexEntry]:
        """按叶子链顺序遍历索引条目。"""

        ...

    def _find_equal_leaves(
        self, target: Key, *, readonly: bool = False
    ) -> Iterator[_IndexNode]:
        """遍历可能包含相同目标键的相邻叶页。"""

        ...

    def _find_insert_leaf(self, target: Key, row_id: RowId) -> _IndexNode:
        """按 key/RowId 全序定位插入叶页。"""

        ...

    def _insert_memory(self, normalized: Key, row_id: RowId) -> None:
        """向内存索引插入一个 key/RowId。"""

        ...

    def _delete_memory(self, normalized: Key, row_id: RowId | None) -> None:
        """从内存索引删除一个 key 或指定 RowId。"""

        ...

    def _split_position(self, node: _IndexNode, candidates: Iterable[int]) -> int:
        """从候选位置中选择可容纳两侧节点的分裂点。"""

        ...

    def _split_leaf_and_propagate(self, leaf: _IndexNode) -> None:
        """分裂叶页并向父节点传播结构变化。"""

        ...

    def _split_internal_and_propagate(self, node: _IndexNode) -> None:
        """分裂内部页并继续向上传播。"""

        ...

    def _create_root(self, left: _IndexNode, right: _IndexNode) -> None:
        """用两个子节点创建新的根页。"""

        ...

    def _delete_persistent(self, normalized: Key, row_id: RowId | None) -> None:
        """从持久化树删除记录并修复节点负载。"""

        ...

    def _rebalance_empty(self, node: _IndexNode) -> None:
        """修复低负载节点，必要时借位或合并。"""

        ...

    def _repair_parent_after_removal(self, parent: _IndexNode) -> None:
        """删除子页后修复父页及其祖先。"""

        ...

    def _reset_storage(self) -> None:
        """清空已有持久化节点并保留空根页。"""

        ...

    def search(self, key: object | tuple[object, ...]) -> tuple[RowId, ...]:
        """精确查找 key 对应的 RowId。"""

        ...

    def search_entries(
        self, key: object | tuple[object, ...]
    ) -> tuple[IndexPayloadEntry, ...]:
        """精确查找 key 并返回覆盖列 payload。"""

        ...

    def range_scan(
        self,
        low: object | tuple[object, ...] | None = None,
        high: object | tuple[object, ...] | None = None,
        *,
        include_low: bool = True,
        include_high: bool = True,
    ) -> tuple[IndexEntry, ...]:
        """按键范围返回 key/RowId 条目。"""

        ...

    def range_scan_entries(
        self,
        low: object | tuple[object, ...] | None = None,
        high: object | tuple[object, ...] | None = None,
        *,
        include_low: bool = True,
        include_high: bool = True,
    ) -> tuple[IndexPayloadEntry, ...]:
        """按键范围返回带覆盖列 payload 的条目。"""

        ...

    def prefix_scan(
        self, prefix: object | tuple[object, ...]
    ) -> tuple[IndexEntry, ...]:
        """扫描以联合键前缀开头的 key/RowId 条目。"""

        ...

    def range_scan_prefix(
        self,
        prefix: object | tuple[object, ...],
        low: object | None = None,
        high: object | None = None,
        *,
        include_low: bool = True,
        include_high: bool = True,
    ) -> tuple[IndexEntry, ...]:
        """扫描联合键前缀后的范围。"""

        ...

    def range_scan_prefix_entries(
        self,
        prefix: object | tuple[object, ...],
        low: object | None = None,
        high: object | None = None,
        *,
        include_low: bool = True,
        include_high: bool = True,
    ) -> tuple[IndexPayloadEntry, ...]:
        """扫描联合键前缀范围并返回覆盖列 payload。"""

        ...

    def all_items(self) -> tuple[IndexEntry, ...]:
        """返回全部 key/RowId 条目。"""

        ...

    def insert(
        self,
        key: object | tuple[object, ...],
        row_id: RowId,
        payload: Iterable[object] | None = None,
    ) -> None:
        """插入一条 key/RowId 记录及可选覆盖列值。"""

        ...

    def has_entries(self) -> bool:
        """判断索引是否至少包含一条记录。"""

        ...

    def delete(
        self, key: object | tuple[object, ...], row_id: RowId | None = None
    ) -> None:
        """删除指定 key 或 key 下的指定 RowId。"""

        ...

    def bulk_load(
        self,
        entries: Iterable[
            IndexPayloadEntry
            | tuple[object | tuple[object, ...], RowId]
            | tuple[object | tuple[object, ...], RowId, Iterable[object] | None]
        ],
    ) -> None:
        """批量消费条目并重建索引。"""

        ...

    def physical_page_ids(self, *, readonly: bool = False) -> tuple[int, ...]:
        """返回索引可达的物理页号。"""

        ...

    def _physical_page_ids_unlocked(
        self, *, readonly: bool = False
    ) -> tuple[int, ...]:
        """在已持有树锁时返回索引可达的物理页号。"""

        ...

    def snapshot(self, offset: int = 0, limit: int = 100) -> BPlusTreeSnapshot:
        """返回分页的索引检查快照。"""

        ...

    def destroy(self) -> None:
        """销毁索引并释放持久化页面。"""

        ...
