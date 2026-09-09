"""阶段 5：磁盘文件管理（页式存储模型）。

职责：
  * 把数据库文件看成「定长页数组」，提供 read_page / write_page
  * 页的分配 allocate_page 与释放 deallocate_page，维护空闲页链表
  * 第 0 页为元数据页（meta），保存 page_count / free_list_head / catalog_root

空闲页链表：被释放的页的头 4 个字节存放下一个空闲页号，形成单链表，
分配时优先从链表头取，保证空间复用。
"""

from __future__ import annotations

import os
import struct

from database_system.utils.constants import INVALID_PAGE_ID, PAGE_SIZE, PageType
from database_system.utils.errors import StorageError

MAGIC = b"MINISQL!"  # 必须正好 8 字节，与 META_FMT 中的 "8s" 对应
META_FMT = "<8sIIii"  # magic, page_count, version, free_list_head, catalog_root
META_SIZE = struct.calcsize(META_FMT)  # 24
VERSION = 1


class DiskManager:
    """磁盘管理器：文件 <-> 页。"""

    def __init__(self, path: str, create_if_missing: bool = True):
        self.path = path
        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)

        new_file = (not os.path.exists(path)) or os.path.getsize(path) == 0
        if new_file and not create_if_missing:
            raise StorageError(f"database file not found: {path}")

        self.file = open(path, "r+b" if not new_file else "w+b")
        if new_file:
            self.page_count = 1          # 第 0 页：元数据页
            self.free_list_head = INVALID_PAGE_ID
            self.catalog_root = INVALID_PAGE_ID
            self._write_meta()  # 第 0 页即元数据页，内容由 _write_meta 一次性写入
        else:
            self._read_meta()

    # ------------------------------ 元数据 ------------------------------

    def _read_meta(self) -> None:
        self.file.seek(0)
        raw = self.file.read(PAGE_SIZE)
        if len(raw) < META_SIZE:
            raise StorageError("corrupted database file: meta page too small")
        magic, page_count, version, free_head, catalog_root = struct.unpack_from(
            META_FMT, raw, 0
        )
        if magic != MAGIC:
            raise StorageError("not a MiniSQL database file (bad magic)")
        if version != VERSION:
            raise StorageError(f"unsupported database version {version}")
        self.page_count = page_count
        self.free_list_head = free_head
        self.catalog_root = catalog_root

    def _write_meta(self) -> None:
        raw = bytearray(PAGE_SIZE)
        struct.pack_into(
            META_FMT, raw, 0, MAGIC, self.page_count, VERSION,
            self.free_list_head, self.catalog_root,
        )
        self._write_page_raw(0, bytes(raw))
        self.file.flush()

    def write_meta(self) -> None:
        """把内存中的元数据（页数 / 空闲链表 / 目录根页）写回第 0 页。"""
        self._write_meta()

    # ------------------------------ 原始页读写 ------------------------------

    def _write_page_raw(self, page_id: int, data: bytes) -> None:
        if len(data) != PAGE_SIZE:
            raise StorageError(f"page data must be exactly {PAGE_SIZE} bytes")
        self.file.seek(page_id * PAGE_SIZE)
        self.file.write(data)

    def read_page(self, page_id: int) -> bytes:
        if page_id <= 0 or page_id >= self.page_count:
            raise StorageError(f"page {page_id} out of range [1, {self.page_count})")
        self.file.seek(page_id * PAGE_SIZE)
        data = self.file.read(PAGE_SIZE)
        if len(data) != PAGE_SIZE:
            raise StorageError(f"incomplete read on page {page_id}")
        return data

    def write_page(self, page_id: int, data) -> None:
        if page_id <= 0 or page_id >= self.page_count:
            raise StorageError(f"page {page_id} out of range [1, {self.page_count})")
        self._write_page_raw(page_id, bytes(data))
        self.file.flush()

    # ------------------------------ 页分配 / 释放 ------------------------------

    def allocate_page(self) -> int:
        """分配一个新页（优先复用空闲页链表），返回页号。"""
        if self.free_list_head != INVALID_PAGE_ID:
            page_id = self.free_list_head
            nxt = struct.unpack_from("<i", self.read_page(page_id), 0)[0]
            self.free_list_head = nxt
        else:
            page_id = self.page_count
            self.page_count += 1
        self._write_page_raw(page_id, bytes(PAGE_SIZE))
        self._write_meta()
        return page_id

    def deallocate_page(self, page_id: int) -> None:
        """释放页：挂入空闲页链表头部。"""
        if page_id <= 0 or page_id >= self.page_count:
            raise StorageError(f"page {page_id} out of range")
        raw = bytearray(PAGE_SIZE)
        struct.pack_into("<i", raw, 0, self.free_list_head)
        self._write_page_raw(page_id, bytes(raw))
        self.free_list_head = page_id
        self._write_meta()

    # ------------------------------ 其他 ------------------------------

    def size(self) -> int:
        return self.page_count * PAGE_SIZE

    def close(self) -> None:
        try:
            self._write_meta()
        finally:
            self.file.close()

    def __str__(self) -> str:
        return (
            f"DiskManager({self.path}, pages={self.page_count}, "
            f"free_head={self.free_list_head}, catalog_root={self.catalog_root})"
        )
