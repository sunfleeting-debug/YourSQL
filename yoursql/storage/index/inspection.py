"""索引物理页检查结果的构造。"""

from __future__ import annotations

from ...common.codec import PayloadCodec
from ..page import Page
from .node import _IndexNode
from .types import IndexPageEntry, IndexPageInfo


def index_page_info(
    page: Page,
    offset: int = 0,
    limit: int = 100,
    codec: PayloadCodec | str | None = None,
) -> IndexPageInfo:
    """返回有界的索引页结构，避免把整页重复展开到 HTTP 响应。"""

    node = _IndexNode.from_page(page, codec)
    safe_offset = max(0, int(offset))
    safe_limit = max(1, int(limit))
    common = {
        "format": "disk_bplus_tree_v1",
        "physical": True,
        "page_id": node.page_id,
        "node_type": "leaf" if node.leaf else "internal",
        "level": node.level,
        "parent_page_id": node.parent,
        "next_page_id": node.next_page,
        "prev_page_id": node.prev_page,
        "key_count": len(node.keys),
        "min_key": node.keys[0] if node.keys else None,
        "max_key": node.keys[-1] if node.keys else None,
    }
    if node.leaf:
        all_entries = [
            IndexPageEntry(key, row_id)
            for key, row_id in zip(node.keys, node.row_ids, strict=True)
        ]
        return IndexPageInfo(
            **common,
            entries=tuple(all_entries[safe_offset : safe_offset + safe_limit]),
            entry_offset=safe_offset,
            entry_limit=safe_limit,
            entry_count=len(all_entries),
            entries_truncated=safe_offset + safe_limit < len(all_entries),
            row_id_count=len(node.row_ids),
        )
    return IndexPageInfo(
        **common,
        children=tuple(node.children[safe_offset : safe_offset + safe_limit]),
        child_offset=safe_offset,
        child_limit=safe_limit,
        child_count=len(node.children),
        children_truncated=safe_offset + safe_limit < len(node.children),
    )
