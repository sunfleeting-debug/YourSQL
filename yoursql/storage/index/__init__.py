"""索引存储包；保留旧的 `yoursql.storage.index` 导入接口。

索引页使用独立的 ``MBIX`` payload；节点模型、键排序、扫描、修改和持久化
辅助逻辑分别放在小模块中，B+Tree 只负责组合这些职责并保留原有 API。
"""

from .codec import INDEX_LINK_RESERVE, INDEX_MAGIC, INDEX_VERSION
from .inspection import index_page_info
from .manager import IndexManager
from .node import _IndexNode
from .ordering import Key, MemoryKey
from .tree import BPlusTree
from .types import (
    BPlusTreeSnapshot,
    IndexEntry,
    IndexNodeSnapshot,
    IndexPageEntry,
    IndexPageInfo,
    IndexPayloadEntry,
    IndexSnapshotEntry,
)

__all__ = [
    "BPlusTree",
    "BPlusTreeSnapshot",
    "INDEX_LINK_RESERVE",
    "INDEX_MAGIC",
    "INDEX_VERSION",
    "IndexEntry",
    "IndexManager",
    "IndexNodeSnapshot",
    "IndexPageEntry",
    "IndexPageInfo",
    "IndexPayloadEntry",
    "IndexSnapshotEntry",
    "Key",
    "MemoryKey",
    "_IndexNode",
    "index_page_info",
]
