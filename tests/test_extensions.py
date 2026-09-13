from pathlib import Path

import pytest

from yoursql.common import PageId, RowId, TableStats
from yoursql.engine.optimizer import Optimizer, PlanCache, StatisticsStore
from yoursql.storage import BPlusTree, BufferPool, DiskManager


def test_bplus_tree_exact_range_unique_and_composite() -> None:
    first = RowId(PageId(1), 0)
    second = RowId(PageId(1), 1)
    tree = BPlusTree(unique=True)
    tree.insert((1, "a"), first)
    with pytest.raises(Exception):
        tree.insert((1, "a"), second)
    tree.insert((2, "b"), second)
    assert tree.search((1, "a")) == (first,)
    assert [row_id for _key, row_id in tree.prefix_scan((1,))] == [first]
    assert [row_id for _key, row_id in tree.range_scan_prefix((1,), "a", "a")] == [first]
    assert [row_id for _key, row_id in tree.range_scan((1, "a"), (2, "b"))] == [first, second]
    tree.delete((1, "a"), first)
    assert tree.search((1, "a")) == ()

    heterogeneous = BPlusTree()
    heterogeneous.insert(False, first)
    heterogeneous.insert(0, second)
    assert heterogeneous.search(False) == (first,)
    assert heterogeneous.search(0) == (second,)


def test_disk_bplus_tree_pages_split_reload_and_collapse(tmp_path: Path) -> None:
    """验证真实 INDEX 页中的多页树、叶链、重启读取和删除收缩。"""

    path = tmp_path / "disk-index.db"
    root_page_id: int | None = None
    with DiskManager(path, page_size=1024) as disk:
        buffer_pool = BufferPool(disk, capacity=32)
        tree = BPlusTree(buffer_pool=buffer_pool)
        tree.bulk_load((index, RowId(PageId(7), index)) for index in range(2500))
        snapshot = tree.snapshot(0, 2)
        root_page_id = tree.root_page_id
        assert snapshot["physical"] is True
        assert snapshot["height"] >= 2
        assert snapshot["page_count"] > 2
        assert tree.search((1234,)) == (RowId(PageId(7), 1234),)
        assert len(tree.range_scan((100,), (199,))) == 100
        buffer_pool.flush_all()

    assert root_page_id is not None
    with DiskManager(path, page_size=1024) as disk:
        buffer_pool = BufferPool(disk, capacity=32)
        restored = BPlusTree(buffer_pool=buffer_pool, root_page_id=root_page_id)
        assert restored.search((1234,)) == (RowId(PageId(7), 1234),)
        for index in range(2500):
            restored.delete((index,), RowId(PageId(7), index))
        assert restored.all_items() == ()
        assert len(restored.physical_page_ids()) == 1
        buffer_pool.flush_all()


def test_disk_bplus_tree_duplicate_keys_keep_leaf_order(tmp_path: Path) -> None:
    """重复键跨叶页时仍按 RowId 排序，并可逐条删除。"""

    path = tmp_path / "duplicate-index.db"
    with DiskManager(path, page_size=512) as disk:
        buffer_pool = BufferPool(disk, capacity=16)
        tree = BPlusTree(buffer_pool=buffer_pool)
        rows = [RowId(PageId(9), slot_id) for slot_id in range(80)]
        for row_id in reversed(rows):
            tree.insert("same", row_id)
        assert tree.search("same") == tuple(rows)
        assert [row_id for _key, row_id in tree.range_scan("same", "same")] == rows
        for row_id in rows[::2]:
            tree.delete("same", row_id)
        assert tree.search("same") == tuple(rows[1::2])


def test_disk_bplus_tree_delete_duplicate_key_across_all_leaves(tmp_path: Path) -> None:
    """一次删除跨多叶页的重复键后，树应收缩为可重启的空根。"""

    path = tmp_path / "delete-duplicate-index.db"
    root_page_id: int | None = None
    with DiskManager(path, page_size=512) as disk:
        buffer_pool = BufferPool(disk, capacity=32)
        tree = BPlusTree(buffer_pool=buffer_pool)
        rows = [RowId(PageId(index // 4 + 1), index % 4) for index in range(500)]
        tree.bulk_load(("same", row_id) for row_id in rows)
        root_page_id = tree.root_page_id
        tree.delete("same")
        assert tree.search("same") == ()
        assert tree.all_items() == ()
        assert len(tree.physical_page_ids()) == 1
        buffer_pool.flush_all()

    assert root_page_id is not None
    with DiskManager(path, page_size=512) as disk:
        buffer_pool = BufferPool(disk, capacity=32)
        restored = BPlusTree(buffer_pool=buffer_pool, root_page_id=root_page_id)
        assert restored.all_items() == ()
        assert len(restored.physical_page_ids()) == 1


def test_optimizer_statistics_and_plan_cache() -> None:
    statistics = StatisticsStore()
    statistics.update("t", TableStats(row_count=1000, page_count=10))
    optimizer = Optimizer(statistics)
    assert optimizer.choose_scan("t", has_usable_index=True) == "IndexScan"
    assert optimizer.choose_scan("t", has_usable_index=True, selectivity=0.25) == "SeqScan"
    assert optimizer.should_use_index("t", 0) is True
    # HOW：页级代价模型下，小表（10 页，能全部驻留缓存）回表单价低，可承受更高候选占比。
    assert optimizer.should_use_index("t", 200) is True
    assert optimizer.should_use_index("t", 300) is True

    # 大表且表页超出缓存时，随机回表单价高（实测 800 行 78.8 ms）：候选一多就该改走顺序扫描。
    huge = StatisticsStore()
    huge.update("big", TableStats(row_count=60175, page_count=533))
    heavy = Optimizer(huge, buffer_pool_pages=256)
    assert heavy.should_use_index("big", 800) is True  # Q6 三索引交集后的候选行
    assert heavy.should_use_index("big", 800, index_cached=True) is True
    assert heavy.should_use_index("big", 10969) is False  # l_discount 单索引：实测比顺序扫慢 3 倍
    assert heavy.should_use_index("big", 27627) is False

    cache = PlanCache(capacity=1)
    cache.put("SELECT 1", {"plan": 1})
    assert cache.get(" select   1 ") == {"plan": 1}
    cache.invalidate()
    assert len(cache) == 0
