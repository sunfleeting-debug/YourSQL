"""【前端特供】单个索引页的只读检查。"""

from __future__ import annotations

from yoursql.common.codec import PayloadCodec
from yoursql.storage.page import Page
from yoursql.storage.index.node import _IndexNode
from yoursql.storage.index.types import IndexPageEntry, IndexPageInfo


def index_page_info(
    page: Page,
    offset: int = 0,
    limit: int = 100,
    codec: PayloadCodec | str | None = None,
) -> IndexPageInfo:
    """【前端特供】解析一个 INDEX 页并返回有界的结构化检查结果。

    Args:
        page: 待检查的 ``Page``；必须是 ``PageType.INDEX``，且 payload 使用当前
            支持的 MBIX 格式。
        offset: 叶子条目或内部子页的起始偏移；负值按 0 处理。
        limit: 最多返回的条目或子页数量；小于 1 时按 1 处理。
        codec: 页 payload 使用的编解码器；为空时自动兼容可识别的旧格式。

    Returns:
        ``IndexPageInfo``，包含节点类型、层级、父子页关系、边界键以及分页后的
        叶子条目或子页号。

    Raises:
        StorageError: 页面类型错误、MBIX 格式不支持或页面内容损坏。
    """

    node = _IndexNode.from_page(page, codec, verify=True)
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
