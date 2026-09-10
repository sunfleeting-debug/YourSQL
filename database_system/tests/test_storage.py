"""阶段 5 测试：页结构 / 磁盘管理 / 缓冲缓存（LRU 与 FIFO）。

运行：python -m unittest database_system.tests.test_storage -v
"""

from __future__ import annotations

import os
import shutil
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from database_system.engine.record import decode_row, encode_row
from database_system.storage.buffer import (
    BufferPoolManager,
    FIFOReplacer,
    LRUReplacer,
)
from database_system.storage.file_manager import DiskManager
from database_system.storage.page import HEADER_SIZE, SLOT_SIZE, Page
from database_system.utils.constants import INVALID_PAGE_ID, PAGE_SIZE, PageType
from database_system.utils.errors import StorageError


class PageTest(unittest.TestCase):
    def test_init_layout(self):
        page = Page(1)
        page.init(PageType.DATA)
        self.assertEqual(page.page_type, PageType.DATA)
        self.assertEqual(page.next_page_id, INVALID_PAGE_ID)
        self.assertEqual(page.num_slots, 0)
        self.assertGreater(page.free_space(), PAGE_SIZE - 100)

    def test_insert_and_get(self):
        page = Page(1)
        page.init(PageType.DATA)
        slot = page.insert_record(b"hello")
        self.assertEqual(slot, 0)
        self.assertEqual(page.get_record(slot), b"hello")
        self.assertEqual(page.num_slots, 1)

    def test_delete_and_slot_reuse(self):
        page = Page(1)
        page.init(PageType.DATA)
        s0 = page.insert_record(b"aaa")
        s1 = page.insert_record(b"bbb")
        self.assertTrue(page.delete_record(s0))
        self.assertIsNone(page.get_record(s0))
        self.assertEqual(page.get_record(s1), b"bbb")
        s2 = page.insert_record(b"ccc")
        self.assertEqual(s2, s0)  # 复用了被删除的槽
        self.assertEqual(page.num_slots, 2)

    def test_update_record(self):
        page = Page(1)
        page.init(PageType.DATA)
        slot = page.insert_record(b"12345")
        # 原地更新：槽长度不变，短于原记录时补 0；长于原记录则失败
        self.assertTrue(page.update_record(slot, b"12"))
        self.assertTrue(page.get_record(slot).startswith(b"12"))
        self.assertEqual(len(page.get_record(slot)), 5)
        self.assertFalse(page.update_record(slot, b"toolong"))
        self.assertFalse(page.update_record(99, b"x"))

    def test_page_full(self):
        page = Page(1)
        page.init(PageType.DATA)
        count = 0
        try:
            while True:
                page.insert_record(b"x" * 100)
                count += 1
        except StorageError:
            pass
        self.assertGreater(count, 30)
        with self.assertRaises(StorageError):
            page.insert_record(b"x" * 100)

    def test_header_offsets(self):
        page = Page(7)
        page.init(PageType.CATALOG, 42)
        self.assertEqual(page.page_type, PageType.CATALOG)
        self.assertEqual(page.next_page_id, 42)
        # 头 16 字节 + 槽目录，free_pointer 应指向页尾
        self.assertEqual(page.free_pointer, PAGE_SIZE)
        self.assertEqual(HEADER_SIZE + SLOT_SIZE * page.num_slots, HEADER_SIZE)

    def test_page_zero_is_not_a_valid_next_page(self):
        page = Page(7)
        page.init(PageType.DATA)
        with self.assertRaises(StorageError):
            page.next_page_id = 0

        broken = bytearray(page.encode())
        broken[2:6] = (0).to_bytes(4, "little", signed=True)
        with self.assertRaises(StorageError):
            Page.decode(bytes(broken), page_id=7)

    def test_page_encode_decode_validates_layout(self):
        page = Page(7)
        page.init(PageType.DATA, 42)
        page.insert_record(b"hello")
        restored = Page.decode(page.encode(), page_id=7)
        self.assertEqual(restored.page_id, 7)
        self.assertEqual(restored.next_page_id, 42)
        self.assertEqual(restored.get_record(0), b"hello")

        broken = bytearray(page.encode())
        broken[8:10] = (0).to_bytes(2, "little")
        with self.assertRaises(StorageError):
            Page.decode(bytes(broken), page_id=7)


class RecordTest(unittest.TestCase):
    def test_round_trip(self):
        values = [None, 42, "Tom's book", True, -7]
        self.assertEqual(decode_row(encode_row(values)), values)

    def test_empty_row(self):
        self.assertEqual(decode_row(encode_row([])), [])


class DiskManagerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minisql-storage-")
        self.path = os.path.join(self.tmp, "test.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_and_reopen(self):
        disk = DiskManager(self.path)
        self.assertEqual(disk.page_count, 1)
        disk.close()
        disk2 = DiskManager(self.path)
        self.assertEqual(disk2.page_count, 1)
        disk2.close()

    def test_allocate_grows(self):
        disk = DiskManager(self.path)
        p1, p2 = disk.allocate_page(), disk.allocate_page()
        self.assertEqual((p1, p2), (1, 2))
        self.assertEqual(disk.page_count, 3)
        disk.close()

    def test_free_list_reuse(self):
        disk = DiskManager(self.path)
        p1 = disk.allocate_page()
        p2 = disk.allocate_page()
        disk.deallocate_page(p1)
        self.assertEqual(disk.free_list_head, p1)
        p3 = disk.allocate_page()
        self.assertEqual(p3, p1)  # 复用被释放的页
        disk.close()

    def test_write_read_persist(self):
        disk = DiskManager(self.path)
        pid = disk.allocate_page()
        payload = bytes([0xAB]) * PAGE_SIZE
        disk.write_page(pid, payload)
        disk.close()
        disk2 = DiskManager(self.path)
        self.assertEqual(disk2.read_page(pid), payload)
        disk2.close()

    def test_out_of_range(self):
        disk = DiskManager(self.path)
        with self.assertRaises(StorageError):
            disk.read_page(999)
        disk.close()

    def test_free_page_cannot_be_read_or_freed_twice(self):
        disk = DiskManager(self.path)
        page_id = disk.allocate_page()
        disk.deallocate_page(page_id)
        with self.assertRaises(StorageError):
            disk.read_page(page_id)
        with self.assertRaises(StorageError):
            disk.deallocate_page(page_id)
        disk.close()


class BufferPoolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="minisql-buffer-")
        self.disk = DiskManager(os.path.join(self.tmp, "test.db"))
        self.pages = [self.disk.allocate_page() for _ in range(5)]

    def tearDown(self):
        self.disk.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, buf, order):
        for pid in order:
            buf.fetch_page(pid)
            buf.unpin_page(pid)

    def test_hit_and_miss_stats(self):
        buf = BufferPoolManager(self.disk, pool_size=4)
        buf.fetch_page(self.pages[0])
        buf.unpin_page(self.pages[0])
        buf.fetch_page(self.pages[0])
        buf.unpin_page(self.pages[0])
        self.assertEqual(buf.stats.misses, 1)
        self.assertEqual(buf.stats.hits, 1)
        self.assertAlmostEqual(buf.stats.hit_rate, 0.5)

    def test_lru_vs_fifo(self):
        # 引用序列：1,2,3,1,2,4（帧数 3）
        # LRU 淘汰 3（最久未用），FIFO 淘汰 1（最早进入）
        order = [self.pages[0], self.pages[1], self.pages[2],
                 self.pages[0], self.pages[1]]

        buf = BufferPoolManager(self.disk, pool_size=3, policy="LRU")
        self._touch(buf, order)
        buf.fetch_page(self.pages[3])
        self.assertNotIn(self.pages[2], buf.frames)   # 页 3 被淘汰
        self.assertIn(self.pages[0], buf.frames)
        buf.unpin_page(self.pages[3])

        buf2 = BufferPoolManager(self.disk, pool_size=3, policy="FIFO")
        self._touch(buf2, order)
        buf2.fetch_page(self.pages[3])
        self.assertNotIn(self.pages[0], buf2.frames)  # 页 1 被淘汰
        self.assertIn(self.pages[2], buf2.frames)
        buf2.unpin_page(self.pages[3])

    def test_eviction_log(self):
        buf = BufferPoolManager(self.disk, pool_size=2, policy="LRU")
        self._touch(buf, self.pages[:2])
        buf.fetch_page(self.pages[2])
        buf.unpin_page(self.pages[2])
        self.assertEqual(buf.stats.evictions, 1)
        self.assertTrue(any("EVICT" in line for line in buf.log))

    def test_pinned_page_not_evicted(self):
        buf = BufferPoolManager(self.disk, pool_size=1)
        buf.fetch_page(self.pages[0])           # 保持 pin
        with self.assertRaises(StorageError):
            buf.fetch_page(self.pages[1])

    def test_invalid_fetch_does_not_evict_existing_page(self):
        buf = BufferPoolManager(self.disk, pool_size=1)
        page_id = self.pages[0]
        buf.fetch_page(page_id)
        buf.unpin_page(page_id)
        with self.assertRaises(StorageError):
            buf.fetch_page(999)
        self.assertIn(page_id, buf.frames)
        self.assertEqual(buf.stats.evictions, 0)

    def test_delete_pinned_page_preserves_buffer_state(self):
        buf = BufferPoolManager(self.disk, pool_size=1)
        page_id = self.pages[0]
        buf.fetch_page(page_id)
        with self.assertRaises(StorageError):
            buf.delete_page(page_id)
        self.assertIn(page_id, buf.frames)
        self.assertEqual(buf.frames[page_id].pin_count, 1)
        buf.unpin_page(page_id)
        buf.delete_page(page_id)
        self.assertNotIn(page_id, buf.frames)

    def test_dirty_flush(self):
        buf = BufferPoolManager(self.disk, pool_size=2)
        page = buf.fetch_page(self.pages[0])
        page.data[100:103] = b"XYZ"
        buf.unpin_page(self.pages[0], True)
        buf.flush_page(self.pages[0])
        self.assertEqual(self.disk.read_page(self.pages[0])[100:103], b"XYZ")
        self.assertFalse(buf.frames[self.pages[0]].dirty)

    def test_new_page_pinned_then_unpinned(self):
        buf = BufferPoolManager(self.disk, pool_size=4)
        pid = buf.new_page()
        self.assertEqual(buf.frames[pid].pin_count, 1)
        buf.unpin_page(pid, True)
        self.assertEqual(buf.frames[pid].pin_count, 0)
        pid2 = buf.new_page_unpinned()
        self.assertEqual(buf.frames[pid2].pin_count, 0)

    def test_delete_page(self):
        buf = BufferPoolManager(self.disk, pool_size=4)
        victim = self.pages[0]
        buf.delete_page(victim)
        self.assertNotIn(victim, buf.frames)
        self.assertEqual(self.disk.free_list_head, victim)

    def test_set_policy(self):
        buf = BufferPoolManager(self.disk, pool_size=3, policy="LRU")
        buf.set_policy("FIFO")
        self.assertIsInstance(buf.replacer, FIFOReplacer)
        buf.set_policy("LRU")
        self.assertIsInstance(buf.replacer, LRUReplacer)


if __name__ == "__main__":
    unittest.main()
