from pathlib import Path

import pytest

from yoursql.common import RowId
from yoursql.storage import BufferPool, DiskManager, Page, PageType, SlottedPage, TableHeap


def test_page_round_trip_and_crc(tmp_path: Path) -> None:
    assert Page.HEADER_SIZE == 30
    page = Page(3, 4096, PageType.CATALOG, b"catalog")
    encoded = page.to_bytes()
    assert encoded[26:30] == b"\x00" * 4
    restored = Page.from_bytes(encoded, page_size=4096)
    assert restored.page_id == 3
    assert restored.page_type is PageType.CATALOG
    assert restored.payload == b"catalog"

    broken = bytearray(page.to_bytes())
    broken[-1] ^= 1
    # 尾部填充不参与 CRC，修改有效负载才应失败。
    broken[Page.HEADER_SIZE] ^= 1
    with pytest.raises(Exception):
        Page.from_bytes(bytes(broken), page_size=4096)


def test_disk_buffer_pool_and_reuse(tmp_path: Path) -> None:
    path = tmp_path / "demo.db"
    with DiskManager(path) as disk:
        first = disk.allocate(PageType.CATALOG, b"one")
        disk.write(Page(first.page_id, 4096, PageType.CATALOG, b"two"))
        assert disk.read(first.page_id).payload == b"two"
        disk.free(first.page_id)
        reused = disk.allocate(PageType.HEAP, b"three")
        assert reused.page_id == first.page_id

        buffer = BufferPool(disk, capacity=1, replacement_policy="fifo")
        buffer.get_page(reused.page_id)
        buffer.unpin(reused.page_id)
        other = disk.allocate(PageType.CATALOG, b"other")
        buffer.get_page(other.page_id)
        buffer.unpin(other.page_id)
        assert buffer.stats()["evictions"] == 1
        assert buffer.stats()["misses"] == 2
        event = buffer.events()[-1]
        assert event["action"] == "evict"
        assert event["policy"] == "fifo"
        assert event["writeback"] is False


def test_buffer_snapshot_exposes_eviction_order_for_lru_and_fifo(tmp_path: Path) -> None:
    path = tmp_path / "eviction-order.db"
    with DiskManager(path) as disk:
        pages = [disk.allocate(PageType.CATALOG, str(index).encode()) for index in range(3)]
        page_ids = [page.page_id for page in pages]

        lru = BufferPool(disk, capacity=3, replacement_policy="lru")
        for page_id in page_ids:
            lru.get_page(page_id)
            lru.unpin(page_id)
        lru.get_page(page_ids[0])
        lru.unpin(page_ids[0])
        assert lru.snapshot()["eviction_order"] == [page_ids[1], page_ids[2], page_ids[0]]

        fifo = BufferPool(disk, capacity=3, replacement_policy="fifo")
        for page_id in page_ids:
            fifo.get_page(page_id)
            fifo.unpin(page_id)
        fifo.get_page(page_ids[0])
        fifo.unpin(page_ids[0])
        assert fifo.snapshot()["eviction_order"] == page_ids

        fifo.get_page(page_ids[1])
        assert page_ids[1] not in fifo.snapshot()["eviction_order"]


def test_table_heap_reuses_slots_and_persists(tmp_path: Path) -> None:
    path = tmp_path / "heap.db"
    with DiskManager(path) as disk:
        buffer = BufferPool(disk, capacity=2)
        heap = TableHeap(buffer)
        first = heap.insert((1, "Alice"))
        second = heap.insert((2, "Bob"))
        assert [row for _rid, row in heap.scan()] == [(1, "Alice"), (2, "Bob")]
        assert heap.delete(first)
        assert heap.read(first) is None
        replacement = heap.insert((3, "Carol"))
        assert replacement.page_id == first.page_id
        assert replacement.slot_id == first.slot_id
        buffer.flush_all()
        page_ids = list(heap.page_ids)

    with DiskManager(path) as disk:
        buffer = BufferPool(disk)
        restored = TableHeap(buffer, page_ids)
        assert [row for _rid, row in restored.scan()] == [(3, "Carol"), (2, "Bob")]


def test_double_ended_slotted_page_layout() -> None:
    slotted = SlottedPage(7, 4096)
    first = slotted.insert(b"[1,\"Alice\"]")
    second = slotted.insert(b"[2,\"Bob\"]")
    page = slotted.to_page()

    assert page.payload[:4] == b"MSP2"
    assert len(page.payload) == 4096 - Page.HEADER_SIZE
    restored = SlottedPage.from_page(page)
    layout = restored.layout_metadata()
    assert layout["physical"] is True
    assert layout["slot_entry_size"] == 6
    directory = layout["slot_directory"]
    free = layout["free_region"]
    records = layout["record_region"]
    assert isinstance(directory, dict) and directory["direction"] == "forward"
    assert isinstance(free, dict) and free["start"] < free["end"]
    assert isinstance(records, dict) and records["direction"] == "backward"
    positions = restored.slot_layout()
    assert positions[first]["offset"] != positions[second]["offset"]
    first_end = positions[first]["offset"] + positions[first]["length"]
    second_end = positions[second]["offset"] + positions[second]["length"]
    assert first_end <= positions[second]["offset"] or second_end <= positions[first]["offset"]

    assert restored.storage_format == "double_ended_v2"


def test_slotted_insert_preserves_existing_offsets() -> None:
    slotted = SlottedPage(9, 4096)
    first = slotted.insert(b"A" * 20)
    second = slotted.insert(b"B" * 30)
    before = slotted.slot_layout()
    slotted.delete(second)

    replacement = slotted.insert(b"C" * 10)
    after = slotted.slot_layout()

    assert replacement == second
    assert after[first]["offset"] == before[first]["offset"]
    assert after[first]["length"] == before[first]["length"]
    assert after[second]["offset"] != 0
    assert after[second]["length"] == 10

    persisted = SlottedPage.from_page(slotted.to_page())
    persisted_before_append = persisted.slot_layout()
    third = persisted.insert(b"D" * 15)
    persisted_after_append = persisted.slot_layout()
    assert third == 2
    assert persisted_after_append[first]["offset"] == persisted_before_append[first]["offset"]
    assert persisted_after_append[second]["offset"] == persisted_before_append[second]["offset"]
