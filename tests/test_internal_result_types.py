"""验证存储与执行层内部结果对象的字段可读性。"""

from pathlib import Path

from yoursql.common import PageId, RowId
from yoursql.storage import (
    BPlusTree,
    BufferPool,
    BufferPoolSnapshot,
    BufferPoolStats,
    DiskIOStats,
    DiskManager,
    DiskMetadata,
    HeapRecord,
    IndexEntry,
    IndexPayloadEntry,
    PageType,
    SlottedPage,
    SlottedPageLayoutInfo,
    TableHeap,
)


def test_storage_stats_and_layout_results_have_named_fields(tmp_path: Path) -> None:
    path = tmp_path / "typed-results.db"
    with DiskManager(path) as disk:
        assert isinstance(disk.metadata(), DiskMetadata)
        assert isinstance(disk.io_stats(), DiskIOStats)

        page = disk.allocate(PageType.HEAP)
        buffer_pool = BufferPool(disk, capacity=2)
        buffer_pool.get_page(page.page_id)
        buffer_pool.unpin(page.page_id)

        stats = buffer_pool.stats()
        snapshot = buffer_pool.snapshot()
        assert isinstance(stats, BufferPoolStats)
        assert isinstance(snapshot, BufferPoolSnapshot)
        assert stats.misses == 1
        assert snapshot.frames[0].page_id == page.page_id
        assert snapshot.to_dict()["stats"]["misses"] == 1

        slotted = SlottedPage.from_page(page)
        slotted.insert(b"[1,\"Alice\"]")
        layout = slotted.layout_info()
        assert isinstance(layout, SlottedPageLayoutInfo)
        assert layout.slot_directory.direction == "forward"
        assert layout.slots[0].length > 0
        assert layout.to_dict()["slot_directory"]["direction"] == "forward"


def test_heap_and_bplus_tree_results_expose_named_fields(tmp_path: Path) -> None:
    row_id = RowId(PageId(1), 0)
    entry = IndexEntry((1,), row_id)
    payload_entry = IndexPayloadEntry((1,), row_id, ["Alice"])
    assert tuple(entry) == ((1,), row_id)
    assert tuple(payload_entry) == ((1,), row_id, ["Alice"])

    with DiskManager(tmp_path / "typed-index.db") as disk:
        tree = BPlusTree(buffer_pool=BufferPool(disk))
        tree.insert((1,), row_id, ("Alice",))
        scan_entry = tree.range_scan((1,), (1,))[0]
        scan_payload = tree.range_scan_entries((1,), (1,))[0]
    assert isinstance(scan_entry, IndexEntry)
    assert isinstance(scan_payload, IndexPayloadEntry)
    assert scan_entry.key == (1,)
    assert scan_entry.row_id == row_id
    assert scan_payload.payload == ["Alice"]

    with DiskManager(tmp_path / "typed-heap.db") as disk:
        heap = TableHeap(BufferPool(disk))
        inserted = heap.insert((1, "Alice"))
        record = next(heap.scan())
    assert isinstance(record, HeapRecord)
    assert record.row_id == inserted
    assert record.row == (1, "Alice")
