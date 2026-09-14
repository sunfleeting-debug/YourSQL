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
        """创建并注册新的索引。"""
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
        """删除索引并释放其资源。"""
        return self._indexes.pop(self._normalize(name), None)

    def get(self, name: str) -> BPlusTree:
        """按键查找索引条目。"""
        try:
            return self._indexes[self._normalize(name)]
        except KeyError as exc:
            raise ExecutionError(f"索引 {name!r} 不存在") from exc

    def items(self) -> Iterable[tuple[str, BPlusTree]]:
        """返回索引中的全部条目。"""
        return tuple(self._indexes.items())
