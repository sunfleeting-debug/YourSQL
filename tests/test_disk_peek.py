"""只读 peek 必须看到尚未 flush 的缓冲写入（回归测试）。

WHY：磁盘写入走 BufferedRandom，而只读调试路径用独立句柄；若不先 flush，
被缓冲池淘汰的页会被读成旧内容（曾导致 33 页索引在小缓冲池下读到空叶页）。
"""

from __future__ import annotations

from pathlib import Path

from yoursql.common.types import PageId, RowId
from yoursql.storage.buffer import BufferPool
from yoursql.storage.disk import DiskManager
from yoursql.storage.index import BPlusTree
from yoursql.storage.page import Page, PageType


def test_peek_sees_buffered_writes_without_sync(tmp_path: Path) -> None:
    path = tmp_path / "peek-buffer.db"
    with DiskManager(path, page_size=4096) as disk:
        page = disk.allocate(PageType.CATALOG, b"json-payload")
        assert len(disk.peek(page.page_id).payload) > 0
        disk.write(Page(page.page_id, 4096, PageType.CATALOG, b"updated-payload"))
        # 不调用 sync()：peek 也必须看到最新内容
        assert disk.peek(page.page_id).payload == b"updated-payload"


def test_readonly_index_traversal_after_eviction(tmp_path: Path) -> None:
    """树页数超过缓冲池容量时，只读遍历仍能读到已写入的叶页。"""

    path = tmp_path / "evicted-index.db"
    with DiskManager(path, page_size=512) as disk:
        pool = BufferPool(disk, capacity=4)  # 刻意小于树页数，强制淘汰
        tree = BPlusTree(buffer_pool=pool)
        rows = [RowId(PageId(index // 4 + 1), index % 4) for index in range(120)]
        tree.bulk_load(("same", row_id) for row_id in rows)
        assert len(tree.physical_page_ids()) > 4
        assert tree.all_items() == tuple((key, row_id) for key, row_id in tree.range_scan("same", "same"))
        assert len(tree.all_items()) == len(rows)
