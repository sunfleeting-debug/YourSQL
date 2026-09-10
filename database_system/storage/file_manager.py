"""数据库文件和页面分配器。

文件只做三件事：

1. 把文件视为固定大小的页面数组；
2. 在第 0 页保存超级块（文件元数据）；
3. 用持久化的 LIFO 空闲链表分配和回收页面。

缓冲池不应绕过本类直接操作文件。所有普通数据页都必须先通过
``validate_page``，这样被释放的页面不会被误读或误写。
"""

from __future__ import annotations

import os
import struct

from database_system.utils.constants import INVALID_PAGE_ID, PAGE_SIZE, PageType
from database_system.utils.errors import StorageError

# 新文件格式。旧数据文件允许废弃，因此这里使用更完整、更直观的超级块。
MAGIC = b"MINIDB02"
VERSION = 2
META_FMT = "<8sIIIii"  # magic, version, page_size, page_count, free_head, catalog_root
META_SIZE = struct.calcsize(META_FMT)
FREE_MAGIC = b"FREE"
FREE_FMT = "<4si"  # free marker, next free page id


class DiskManager:
    """负责数据库文件、超级块和空闲页链表。"""

    def __init__(self, path: str, create_if_missing: bool = True):
        self.path = path
        self.closed = False
        self.free_pages: set[int] = set()

        directory = os.path.dirname(os.path.abspath(path))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory, exist_ok=True)

        new_file = not os.path.exists(path) or os.path.getsize(path) == 0
        if new_file and not create_if_missing:
            raise StorageError(f"database file not found: {path}")

        try:
            self.file = open(path, "w+b" if new_file else "r+b")
            if new_file:
                self.page_count = 1
                self.free_list_head = INVALID_PAGE_ID
                self.catalog_root = INVALID_PAGE_ID
                self._write_meta()
            else:
                self._read_meta()
                self._load_free_list()
        except (OSError, struct.error, StorageError) as exc:
            if hasattr(self, "file"):
                self.file.close()
            self.closed = True
            if isinstance(exc, StorageError):
                raise
            raise StorageError(f"cannot open database file: {exc}") from exc

    # ------------------------------ 生命周期 ------------------------------

    def _ensure_open(self) -> None:
        if self.closed:
            raise StorageError("database file is closed")

    # ------------------------------ 超级块 ------------------------------

    def _read_meta(self) -> None:
        self._ensure_open()
        self.file.seek(0, os.SEEK_END)
        file_size = self.file.tell()
        if file_size < PAGE_SIZE:
            raise StorageError("corrupted database file: superblock is incomplete")

        self.file.seek(0)
        raw = self.file.read(PAGE_SIZE)
        if len(raw) != PAGE_SIZE:
            raise StorageError("corrupted database file: short superblock read")

        magic, version, page_size, page_count, free_head, catalog_root = struct.unpack_from(
            META_FMT, raw, 0
        )
        if magic != MAGIC or version != VERSION:
            raise StorageError("unsupported database file format")
        if page_size != PAGE_SIZE:
            raise StorageError(f"unsupported page size {page_size}")
        if page_count < 1 or file_size != page_count * PAGE_SIZE:
            raise StorageError("database file size does not match page count")
        if free_head < INVALID_PAGE_ID or free_head >= page_count:
            raise StorageError("invalid free-list head")
        if catalog_root < INVALID_PAGE_ID or catalog_root >= page_count:
            raise StorageError("invalid catalog root")

        self.page_count = page_count
        self.free_list_head = free_head
        self.catalog_root = catalog_root

    def _write_meta(self) -> None:
        raw = bytearray(PAGE_SIZE)
        struct.pack_into(
            META_FMT,
            raw,
            0,
            MAGIC,
            VERSION,
            PAGE_SIZE,
            self.page_count,
            self.free_list_head,
            self.catalog_root,
        )
        self._write_page_raw(0, bytes(raw))
        self.file.flush()

    def write_meta(self) -> None:
        """将超级块内存字段写回 page 0。"""
        self._ensure_open()
        self._write_meta()

    # ------------------------------ 原始页 I/O ------------------------------

    def _read_page_raw(self, page_id: int) -> bytes:
        self.file.seek(page_id * PAGE_SIZE)
        data = self.file.read(PAGE_SIZE)
        if len(data) != PAGE_SIZE:
            raise StorageError(f"incomplete read on page {page_id}")
        return data

    def _write_page_raw(self, page_id: int, data: bytes) -> None:
        if len(data) != PAGE_SIZE:
            raise StorageError(f"page data must be exactly {PAGE_SIZE} bytes")
        self.file.seek(page_id * PAGE_SIZE)
        written = self.file.write(data)
        if written != PAGE_SIZE:
            raise StorageError(f"incomplete write on page {page_id}")

    @staticmethod
    def _empty_data_page() -> bytes:
        """返回一个可被 Page.decode() 接受的空 DATA 页。"""
        raw = bytearray(PAGE_SIZE)
        raw[0] = PageType.DATA
        struct.pack_into("<i", raw, 2, INVALID_PAGE_ID)
        struct.pack_into("<H", raw, 6, 0)
        struct.pack_into("<H", raw, 8, PAGE_SIZE)
        return bytes(raw)

    def validate_page(self, page_id: int, allow_meta: bool = False) -> None:
        """检查页号、文件状态和页面是否已释放。"""
        self._ensure_open()
        if type(page_id) is not int:
            raise StorageError(f"invalid page id {page_id!r}")
        if allow_meta and page_id == 0:
            return
        if page_id <= 0 or page_id >= self.page_count:
            raise StorageError(f"page {page_id} out of range [1, {self.page_count})")
        if page_id in self.free_pages:
            raise StorageError(f"page {page_id} has been freed")

    def read_page(self, page_id: int) -> bytes:
        self.validate_page(page_id)
        return self._read_page_raw(page_id)

    def write_page(self, page_id: int, data: bytes) -> None:
        self.validate_page(page_id)
        if len(data) != PAGE_SIZE:
            raise StorageError(f"page data must be exactly {PAGE_SIZE} bytes")
        if data[:4] == FREE_MAGIC:
            raise StorageError("cannot write a FREE page through write_page")
        self._write_page_raw(page_id, bytes(data))
        self.file.flush()

    # ------------------------------ 空闲页链表 ------------------------------

    def _load_free_list(self) -> None:
        """启动时校验空闲链表，避免循环或重复释放被静默接受。"""
        current = self.free_list_head
        while current != INVALID_PAGE_ID:
            if current <= 0 or current >= self.page_count or current in self.free_pages:
                raise StorageError("invalid or cyclic free-page chain")
            raw = self._read_page_raw(current)
            marker, next_page = struct.unpack_from(FREE_FMT, raw, 0)
            if marker != FREE_MAGIC:
                raise StorageError(f"page {current} is missing FREE marker")
            if next_page < INVALID_PAGE_ID or next_page >= self.page_count:
                raise StorageError(f"invalid next free page {next_page}")
            self.free_pages.add(current)
            current = next_page

    def allocate_page(self) -> int:
        """优先复用空闲页，否则扩展文件；返回一个已清零页号。"""
        self._ensure_open()
        if self.free_list_head != INVALID_PAGE_ID:
            page_id = self.free_list_head
            raw = self._read_page_raw(page_id)
            marker, next_page = struct.unpack_from(FREE_FMT, raw, 0)
            if marker != FREE_MAGIC:
                raise StorageError(f"page {page_id} is missing FREE marker")
            self.free_list_head = next_page
            self.free_pages.remove(page_id)
        else:
            page_id = self.page_count
            self.page_count += 1

        self._write_page_raw(page_id, self._empty_data_page())
        self._write_meta()
        return page_id

    def deallocate_page(self, page_id: int) -> None:
        """将一个已分配页面加入持久化 LIFO 空闲链表。"""
        self.validate_page(page_id)
        raw = bytearray(PAGE_SIZE)
        struct.pack_into(FREE_FMT, raw, 0, FREE_MAGIC, self.free_list_head)
        self._write_page_raw(page_id, bytes(raw))
        self.free_pages.add(page_id)
        self.free_list_head = page_id
        self._write_meta()

    # ------------------------------ 其他 ------------------------------

    def flush(self) -> None:
        self._ensure_open()
        self.file.flush()

    def size(self) -> int:
        return self.page_count * PAGE_SIZE

    def close(self) -> None:
        if self.closed:
            return
        try:
            self._write_meta()
            self.file.flush()
        finally:
            self.file.close()
            self.closed = True

    def __enter__(self) -> "DiskManager":
        self._ensure_open()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def __str__(self) -> str:
        return (
            f"DiskManager({self.path}, pages={self.page_count}, "
            f"free_head={self.free_list_head}, catalog_root={self.catalog_root})"
        )
