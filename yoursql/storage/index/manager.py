"""按名称管理内存或持久化 B+Tree。"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Callable

from yoursql.common.errors import ExecutionError
from yoursql.common.types import PageId
from yoursql.storage.index.tree import BPlusTree

if TYPE_CHECKING:
    from yoursql.storage.buffer import BufferPool


class IndexManager:
    """按索引名管理内存或落盘 B+Tree。"""

    def __init__(self) -> None:
        """初始化实例所需的状态和依赖。"""
        self._indexes: dict[str, BPlusTree] = {}

    @staticmethod
    def _normalize(name: str) -> str:
        """将索引键规范化为统一的元组表示。"""
        return name.strip().lower()

    def create(
        self,
        name: str,
        *,
        unique: bool = False,
        buffer_pool: "BufferPool | None" = None,
        root_page_id: PageId | int | None = None,
        on_root_change: Callable[[int], None] | None = None,
    ) -> BPlusTree:
        """创建并注册一个索引。

        Args:
            name: 索引名称。查找时会去除首尾空白并按小写形式比较。
            unique: 是否禁止不同 RowId 使用相同的索引键。
            buffer_pool: 持久化模式使用的缓存池；为 ``None`` 时创建内存索引。
            root_page_id: 持久化索引已有的根页号。为空时创建空的 INDEX 根页。
            on_root_change: 根页更换后的回调，通常用于更新 Catalog 中的根页号。

        Returns:
            新建并已登记的 ``BPlusTree``。

        Raises:
            ExecutionError: 规范化后的索引名称已经存在。
            StorageError: 持久化模式的根页无效或无法创建根页。
        """
        key = self._normalize(name)
        if key in self._indexes:
            raise ExecutionError(f"索引 {name!r} 已存在")
        tree = BPlusTree(
            unique=unique,
            buffer_pool=buffer_pool,
            root_page_id=root_page_id,
            on_root_change=on_root_change,
        )
        self._indexes[key] = tree
        return tree

    def drop(self, name: str) -> BPlusTree | None:
        """从名称注册表中移除索引并返回原对象。

        Args:
            name: 要移除的索引名称；匹配时忽略首尾空白和大小写。

        Returns:
            找到的 ``BPlusTree``；名称不存在时返回 ``None``。

        Note:
            该方法只移除注册表条目，不自动释放持久化索引页。
            需要物理删除时，应由调用方继续调用返回对象的 ``destroy()``。
        """
        return self._indexes.pop(self._normalize(name), None)

    def get(self, name: str) -> BPlusTree:
        """按名称获取已注册的索引对象。

        Args:
            name: 索引名称；匹配时忽略首尾空白和大小写。

        Returns:
            对应的 ``BPlusTree`` 实例。

        Raises:
            ExecutionError: 索引名称未登记。
        """
        try:
            return self._indexes[self._normalize(name)]
        except KeyError as exc:
            raise ExecutionError(f"索引 {name!r} 不存在") from exc

    def items(self) -> Iterable[tuple[str, BPlusTree]]:
        """返回当前注册表中的全部索引条目。

        Returns:
            一个元组迭代值；每项为规范化索引名和 ``BPlusTree`` 的二元组。
            返回结果是当前注册表的快照，不会随着后续注册表变化而变化。
        """
        return tuple(self._indexes.items())
