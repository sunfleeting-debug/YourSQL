"""索引存储包；保留旧的 `yoursql.storage.index` 导入接口。

索引页使用独立的 ``MBIX`` payload；节点模型、键排序、扫描、修改和持久化
辅助逻辑分别放在小模块中，B+Tree 只负责组合这些职责并保留原有 API。
"""

from yoursql.storage.index.codec import INDEX_LINK_RESERVE, INDEX_MAGIC, INDEX_VERSION
from yoursql.storage.index.inspection import index_page_info
from yoursql.storage.index.manager import IndexManager
from yoursql.storage.index.node import _IndexNode
from yoursql.storage.index.ordering import Key, MemoryKey
from yoursql.storage.index.tree import BPlusTree
from yoursql.storage.index.types import (
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
