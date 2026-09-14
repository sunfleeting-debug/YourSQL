from pathlib import Path

import pytest

from yoursql.common import RowId, StorageError
from yoursql.storage import BufferPool, DiskManager, Page, PageType, SlottedPage, TableHeap
from yoursql.storage.page import decode_free_page_next


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


def test_linked_free_list_persists_without_superblock_growth(tmp_path: Path) -> None:
    path = tmp_path / "linked-free-list.db"
    with DiskManager(path, page_size=1024) as disk:
        pages = [disk.allocate(PageType.CATALOG, b"payload") for _ in range(300)]
        page_ids = [page.page_id for page in pages]

        disk.free_many(page_ids)

        metadata = disk.metadata()
        assert metadata.free_page_count == len(page_ids)
        assert metadata.free_list_head == page_ids[-1]
        assert metadata.free_list_format == "linked_page_v1"
        assert len(disk.peek(0).payload) < disk.page_size - Page.HEADER_SIZE
        assert decode_free_page_next(disk.read(page_ids[-1]).payload) == page_ids[-2]

    with DiskManager(path, page_size=1024) as disk:
        metadata = disk.metadata()
        assert metadata.free_page_count == len(page_ids)
        assert metadata.free_list_head == page_ids[-1]
        reused = disk.allocate(PageType.HEAP, b"reused")
        assert reused.page_id == page_ids[-1]


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


def test_buffer_pool_can_resize_without_resetting_hot_pages(tmp_path: Path) -> None:
    path = tmp_path / "resize.db"
    with DiskManager(path) as disk:
        pages = [disk.allocate(PageType.CATALOG, str(index).encode()) for index in range(3)]
        page_ids = [page.page_id for page in pages]
        buffer = BufferPool(disk, capacity=3)
        for page_id in page_ids:
            buffer.get_page(page_id)
            buffer.unpin(page_id)
        buffer.get_page(page_ids[0])
        buffer.unpin(page_ids[0])

        before = buffer.stats()
        assert buffer.resize(2) == 1
        assert buffer.capacity == 2
        assert buffer.stats().hits == before.hits
        assert page_ids[1] not in buffer
        assert buffer.events()[-1]["reason"] == "resize"

        assert buffer.resize(5) == 0
        assert buffer.capacity == 5
        assert buffer.stats().size == 2

        dirty = BufferPool(disk, capacity=2)
        dirty.get_page(page_ids[1])
        dirty.unpin(page_ids[1])
        dirty.put_page(Page(page_ids[1], 4096, PageType.CATALOG, b"updated"))
        dirty.get_page(page_ids[0])
        dirty.unpin(page_ids[0])
        assert dirty.resize(1) == 1
        assert disk.read(page_ids[1]).payload == b"updated"
        assert dirty.events()[-1]["writeback"] is True

        pinned = BufferPool(disk, capacity=2)
        pinned.get_page(page_ids[0])
        pinned.get_page(page_ids[1])
        with pytest.raises(StorageError):
            pinned.resize(1)
        assert pinned.capacity == 2
        assert pinned.stats().size == 2
        pinned.unpin(page_ids[0])
        pinned.unpin(page_ids[1])


def test_2q_promotes_reused_pages_and_resists_scan_pollution(tmp_path: Path) -> None:
    path = tmp_path / "2q.db"
    with DiskManager(path) as disk:
        pages = [disk.allocate(PageType.HEAP, str(index).encode()) for index in range(4)]
        page_ids = [page.page_id for page in pages]
        buffer = BufferPool(disk, capacity=3, replacement_policy="2q")

        buffer.get_page(page_ids[0])
        buffer.unpin(page_ids[0])
        buffer.get_page(page_ids[0])
        buffer.unpin(page_ids[0])
        assert buffer.snapshot()["frames"][0]["queue"] == "am"
        assert buffer.stats().promotions == 1

        for page_id in page_ids[1:]:
            buffer.get_page(page_id)
            buffer.unpin(page_id)

        assert page_ids[0] in buffer
        assert page_ids[1] not in buffer
        assert buffer.events()[-1]["queue"] == "a1in"


def test_page_type_protection_prefers_heap_victims(tmp_path: Path) -> None:
    path = tmp_path / "type-aware-buffer.db"
    with DiskManager(path) as disk:
        index_pages = [
            disk.allocate(PageType.INDEX, str(index).encode()) for index in range(2)
        ]
        heap_pages = [
            disk.allocate(PageType.HEAP, str(index).encode()) for index in range(3)
        ]
        index_ids = [page.page_id for page in index_pages]
        heap_ids = [page.page_id for page in heap_pages]
        buffer = BufferPool(disk, capacity=4, protect_page_types=True)

        for page_id in (index_ids[0], index_ids[1], heap_ids[0], heap_ids[1]):
            buffer.get_page(page_id)
            buffer.unpin(page_id)
        buffer.get_page(heap_ids[2])
        buffer.unpin(heap_ids[2])

        assert index_ids[0] in buffer
        assert index_ids[1] in buffer
        assert heap_ids[0] not in buffer
        assert buffer.stats().type_protection_skips == 2
        assert buffer.snapshot()["eviction_order"][:2] == [heap_ids[1], heap_ids[2]]


def test_page_type_protection_has_bounded_index_budget(tmp_path: Path) -> None:
    path = tmp_path / "bounded-type-aware-buffer.db"
    with DiskManager(path) as disk:
        index_pages = [
            disk.allocate(PageType.INDEX, str(index).encode()) for index in range(4)
        ]
        heap_pages = [
            disk.allocate(PageType.HEAP, str(index).encode()) for index in range(2)
        ]
        index_ids = [page.page_id for page in index_pages]
        heap_ids = [page.page_id for page in heap_pages]
        buffer = BufferPool(disk, capacity=4)

        for page_id in (index_ids[0], index_ids[1], index_ids[2], heap_ids[0]):
            buffer.get_page(page_id)
            buffer.unpin(page_id)

        assert buffer.set_protect_page_types(True)
        snapshot = buffer.snapshot()
        assert snapshot["protected_page_limit"] == 2
        assert index_ids[0] not in buffer
        assert index_ids[1] in buffer
        assert index_ids[2] in buffer
        assert index_ids[3] not in buffer
        assert heap_ids[0] in buffer

        buffer.get_page(heap_ids[1])
        buffer.unpin(heap_ids[1])
        assert heap_ids[0] in buffer
        assert heap_ids[1] in buffer

        buffer.get_page(index_ids[0])
        buffer.unpin(index_ids[0])
        assert index_ids[0] in buffer
        assert index_ids[1] in buffer
        assert index_ids[2] in buffer
        assert heap_ids[0] not in buffer
        assert heap_ids[1] in buffer

        buffer.get_page(index_ids[3])
        buffer.unpin(index_ids[3])
        assert index_ids[1] not in buffer
        assert index_ids[2] in buffer
        assert index_ids[3] in buffer
        assert heap_ids[1] in buffer


def test_buffer_policy_switch_to_2q_preserves_existing_frames(tmp_path: Path) -> None:
    path = tmp_path / "switch-2q.db"
    with DiskManager(path) as disk:
        pages = [disk.allocate(PageType.HEAP, str(index).encode()) for index in range(3)]
        page_ids = [page.page_id for page in pages]
        buffer = BufferPool(disk, capacity=3)
        for page_id in page_ids:
            buffer.get_page(page_id)
            buffer.unpin(page_id)

        before = set(page_ids)
        assert buffer.set_replacement_policy("2q")
        assert set(buffer.snapshot()["frames"][index]["page_id"] for index in range(3)) == before
        assert all(
            frame["queue"] == "a1in" for frame in buffer.snapshot()["frames"]
        )


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


def test_table_heap_reclaims_empty_pages_into_free_list(tmp_path: Path) -> None:
    path = tmp_path / "heap-reclaim.db"
    with DiskManager(path) as disk:
        buffer = BufferPool(disk, capacity=2)
        heap = TableHeap(buffer)
        first = heap.insert((1, "Alice"))
        second = heap.insert((2, "Bob"))
        page_id = int(first.page_id)
        assert int(second.page_id) == page_id

        assert heap.delete(first)
        assert heap.delete(second)
        assert heap.reclaim_empty_pages([page_id]) == (page_id,)
        assert heap.page_ids == []
        assert disk.read(page_id).page_type is PageType.FREE
        assert disk.metadata().free_list_head == page_id


def test_table_heap_reclaim_ignores_pages_outside_table(tmp_path: Path) -> None:
    path = tmp_path / "heap-reclaim-filter.db"
    with DiskManager(path) as disk:
        buffer = BufferPool(disk, capacity=2)
        heap = TableHeap(buffer)
        row_id = heap.insert((1, "Alice"))
        page_id = int(row_id.page_id)

        assert heap.reclaim_empty_pages([page_id + 100, page_id]) == ()
        assert heap.page_ids == [page_id]
        assert disk.read(page_id).page_type is PageType.HEAP


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
